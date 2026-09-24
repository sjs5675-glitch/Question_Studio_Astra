"""One fresh, confined, ephemeral native CLI thread per structured text request."""
from __future__ import annotations

import json
import math
import sys
import tempfile
import threading
import time
from pathlib import Path

from astra_runtime import ROOT, inside
from .contracts import AstraError, INSTRUCTIONS, JsonMap, MODEL, Result, check_schema, digest, effort_for, input_receipt, json_object, validate_request_schema, validated_items
from .isolation import check_config, command, environment, native_executable, read_auth
from .rpc import Rpc


def _text_from_items(items) -> str | None:
    items = validated_items(items)
    values = [item.get('text') for item in items if isinstance(item, dict) and item.get('type') == 'agentMessage' and item.get('phase') in (None, 'final_answer')]
    texts = [value for value in values if isinstance(value, str) and value.strip()]
    return texts[-1] if texts else None


def _complete(rpc: Rpc, receipt: JsonMap) -> str:
    final = None
    while True:
        event = rpc.events.popleft() if rpc.events else rpc.receive()
        method = event.get('method')
        params = event.get('params')
        if not isinstance(params, dict):
            continue
        if params.get('threadId') != rpc.thread_id:
            continue
        if params.get('turnId', rpc.turn_id) != rpc.turn_id:
            continue
        if method == 'thread/tokenUsage/updated':
            token_usage = params.get('tokenUsage')
            last = token_usage.get('last') if isinstance(token_usage, dict) else None
            if isinstance(last, dict):
                usage = {key: last[key] if type(last.get(key)) is int and last[key] >= 0 else None for key in ('inputTokens', 'cachedInputTokens', 'cacheWriteInputTokens', 'outputTokens', 'reasoningOutputTokens', 'totalTokens')}
                receipt['usage'] = usage if any(value is not None for value in usage.values()) else None
                receipt['usage_source'] = 'thread/tokenUsage/updated.last'
        if method == 'item/completed':
            item = params.get('item')
            final = _text_from_items([item]) or final
        if method == 'turn/completed':
            turn = params.get('turn')
            if not isinstance(turn, dict) or turn.get('id') != rpc.turn_id:
                raise AstraError('terminal_mismatch')
            error = turn.get('error')
            if error is not None:
                raise rpc.failure(error, 'turn_completed_error')
            if turn.get('status') != 'completed':
                raise AstraError('cancelled' if turn.get('status') == 'interrupted' else 'turn_failed')
            final = _text_from_items(turn.get('items')) or final
            if final is None:
                raise AstraError('output_missing')
            receipt['terminal_completed'] = True
            return final


def run(*, stage: str, prompt: str, schema: JsonMap, images: tuple[Path, ...] = (), model: str | None = MODEL, timeout: float = 600, cancel: threading.Event | None = None) -> Result:
    """No REST/model fallback; every failure carries a sanitized runtime receipt."""
    if model not in (None, MODEL):
        raise AstraError('model_not_allowed')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode('utf-8')) > 8 * 1024 * 1024 or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0 or timeout > 1800:
        raise AstraError('request_invalid')
    validate_request_schema(schema)
    images = tuple(inside(image, 'CLI source image') for image in images)
    if any(not image.is_file() for image in images):
        raise AstraError('image_missing')
    receipt = input_receipt(stage, prompt, schema, images)
    started = time.monotonic()
    rpc = None
    try:
        if cancel is not None and cancel.is_set():
            raise AstraError('cancelled')
        executable = native_executable()
        auth = read_auth()
        parent = inside(ROOT / '.runtime/astra-cli', 'CLI runtime')
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='request-', dir=parent, ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            env = environment(root)
            work = root / 'work'
            work.mkdir()
            (work / '.git').mkdir()
            receipt['executable'] = str(executable)
            receipt['invocation'] = command(executable, root)
            receipt['runtime_root'] = str(root)
            rpc = Rpc(receipt['invocation'], work, env, timeout, cancel)
            try:
                initialized = rpc.request('initialize', {'clientInfo': {'name': 'question_studio_astra', 'version': '0.1.0'}, 'capabilities': {'experimentalApi': True}})
                receipt['cli_user_agent'] = initialized.get('userAgent')
                reported_home = initialized.get('codexHome')
                if not isinstance(reported_home, str) or Path(reported_home).resolve() != Path(env['CODEX_HOME']).resolve():
                    raise AstraError('isolation_home_mismatch')
                receipt['reported_codex_home'] = reported_home
                if '/0.153.2 ' not in str(initialized.get('userAgent')):
                    raise AstraError('cli_version_unsupported')
                rpc.send({'method': 'initialized', 'params': {}})
                receipt['isolation'] = check_config(rpc.request('config/read', {'cwd': str(work), 'includeLayers': True}), root)
                rpc.request('account/login/start', auth)
                receipt['isolation_after_auth'] = check_config(rpc.request('config/read', {'cwd': str(work), 'includeLayers': True}), root)
                auth.clear()
                receipt['auth_mode'] = 'chatgptAuthTokens:experimental'
                selected = rpc.request('thread/start', {'model': MODEL, 'cwd': str(work), 'approvalPolicy': 'never', 'sandbox': 'read-only', 'ephemeral': True, 'baseInstructions': INSTRUCTIONS, 'developerInstructions': INSTRUCTIONS, 'config': {'model_reasoning_effort': effort_for(stage)}, 'dynamicTools': [], 'environments': [], 'selectedCapabilityRoots': [], 'allowProviderModelFallback': False})
                thread = selected.get('thread')
                if not isinstance(thread, dict) or not isinstance(thread.get('id'), str) or thread.get('ephemeral') is not True:
                    raise AstraError('thread_contract_invalid')
                rpc.thread_id = thread['id']
                receipt.update({'selected_model': selected.get('model'), 'selected_effort': selected.get('reasoningEffort'), 'selected_identity_source': 'thread/start response; configuration, not serving telemetry'})
                if selected.get('model') != MODEL or selected.get('reasoningEffort') != effort_for(stage):
                    raise AstraError('selected_identity_mismatch')
                servers = rpc.request('mcpServerStatus/list', {})
                if not isinstance(servers.get('data'), list):
                    raise AstraError('isolation_mcp_unavailable')
                if servers.get('data') or servers.get('nextCursor'):
                    raise AstraError('isolation_mcp_active')
                inputs = [{'type': 'text', 'text': prompt}]
                inputs.extend({'type': 'localImage', 'path': str(image)} for image in images)
                turn_response = rpc.request('turn/start', {'threadId': rpc.thread_id, 'input': inputs, 'model': MODEL, 'effort': effort_for(stage), 'outputSchema': {key: value for key, value in schema.items() if key != 'allOf'}, 'environments': []})
                turn = turn_response.get('turn')
                if not isinstance(turn, dict) or not isinstance(turn.get('id'), str):
                    raise AstraError('turn_contract_invalid')
                rpc.turn_id = turn['id']
                raw = _complete(rpc, receipt)
                value = json_object(raw)
                check_schema(value, schema)
                if receipt['image_sha256'] != [digest(image.read_bytes()) for image in images]:
                    raise AstraError('source_changed')
                receipt.update({'status': 'succeeded', 'output_sha256': digest(raw)})
            finally:
                auth.clear()
                rpc.close()
                receipt['cleanup'] = rpc.cleanup
                if sys.exc_info()[0] is None and not all(rpc.cleanup.get(key) for key in ('reaped', 'readers_stopped', 'writers_stopped')):
                    raise AstraError('cleanup_failed')
        receipt['runtime_root_removed'] = not root.exists()
        if root.exists():
            raise AstraError('cleanup_failed')
        return Result(raw, value, receipt)
    except AstraError as exc:
        receipt.update({'status': 'failed', 'error_code': exc.code})
        if rpc is not None and rpc.failure_diagnostic:
            receipt['failure_diagnostic'] = rpc.failure_diagnostic.copy()
        exc.receipt = receipt
        raise
    except (OSError, ValueError) as exc:
        receipt.update({'status': 'failed', 'error_code': 'local_io_error'})
        raise AstraError('local_io_error', receipt) from exc
    finally:
        receipt['elapsed_seconds'] = round(time.monotonic() - started, 3)
