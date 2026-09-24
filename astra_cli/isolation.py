"""Native process confinement and memory-only subscription authentication."""
from __future__ import annotations

import base64
import json
import math
import os
import time
from pathlib import Path
from typing import Final

from astra_runtime import ROOT, inside
from .contracts import AstraError, JsonMap, MODEL

NATIVE_SUFFIX: Final = 'AppData/Local/Programs/nodejs/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe'
DISABLED: Final = ('shell_tool', 'unified_exec', 'view_image', 'apps', 'multi_agent', 'multi_agent_v2', 'hooks', 'memories', 'goals', 'shell_snapshot', 'plugins', 'remote_plugin', 'plugin_sharing', 'browser_use', 'browser_use_external', 'browser_use_full_cdp_access', 'computer_use', 'in_app_browser', 'in_app_chat', 'in_app_dictation', 'in_app_local_automation', 'in_app_updates', 'image_generation', 'code_mode_host', 'code_mode', 'auth_elicitation', 'guardian_approval', 'skill_mcp_dependency_install', 'skill_search', 'sleep_tool', 'tool_suggest', 'tool_call_mcp_elicitation', 'workspace_dependencies', 'unbounded_connection_retries')
BASE_CONFIG: Final = {
    'model': MODEL, 'approval_policy': 'never', 'sandbox_mode': 'read-only',
    'web_search': 'disabled', 'project_doc_max_bytes': 0,
    'history.persistence': 'none', 'shell_environment_policy.inherit': 'none',
    'analytics.enabled': False, 'feedback.enabled': False,
    'tools.experimental_request_user_input.enabled': False,
    'tools.update_plan.enabled': False,
    'mcp_servers': {}, 'plugins': {}, 'features.skip_host_skill_discovery': True,
    **{f'features.{name}': False for name in DISABLED},
}


def native_executable() -> Path:
    candidate = Path(os.environ.get('ASTRA_CODEX_EXE') or Path.home() / NATIVE_SUFFIX).resolve()
    if not candidate.is_file() or candidate.name.lower() != 'codex.exe':
        raise AstraError('cli_missing')
    return candidate


def read_auth() -> JsonMap:
    """Read once in parent memory; never copy or relay refresh credentials."""
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    try:
        saved = json.loads((home / 'auth.json').read_text(encoding='utf-8-sig'))
    except FileNotFoundError as exc:
        code = 'auth_api_only' if any(os.environ.get(key, '').strip() for key in ('OPENAI_API_KEY', 'CODEX_API_KEY')) else 'auth_missing'
        raise AstraError(code) from exc
    except (OSError, ValueError) as exc:
        raise AstraError('auth_unreadable') from exc
    if not isinstance(saved, dict):
        raise AstraError('auth_unreadable')
    if saved.get('auth_mode') != 'chatgpt':
        raise AstraError('auth_api_only' if saved.get('OPENAI_API_KEY') or saved.get('auth_mode') == 'apikey' else 'auth_missing')
    tokens = saved.get('tokens')
    if tokens is None:
        raise AstraError('auth_missing')
    if not isinstance(tokens, dict):
        raise AstraError('auth_unreadable')
    access = tokens.get('access_token')
    account = tokens.get('account_id')
    if not isinstance(access, str) or not access or not isinstance(account, str) or not account:
        raise AstraError('auth_missing')
    try:
        part = access.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        expiry = claims['exp']
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AstraError('auth_expiry_unknown') from exc
    if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= time.time() + 30:
        raise AstraError('auth_refresh_needed')
    return {'type': 'chatgptAuthTokens', 'accessToken': access, 'chatgptAccountId': account}


def environment(root: Path) -> dict[str, str]:
    root = inside(root, 'CLI home')
    result = {key: value for key, value in os.environ.items() if key.upper() in {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'OS', 'PATHEXT'} and value}
    for key, child in {
        'CODEX_HOME': 'home', 'CODEX_SQLITE_HOME': 'sqlite',
        'TEMP': 'tmp', 'TMP': 'tmp', 'TMPDIR': 'tmp',
        'HOME': 'user', 'USERPROFILE': 'user', 'APPDATA': 'roaming',
        'LOCALAPPDATA': 'local', 'XDG_CACHE_HOME': 'cache',
        'XDG_CONFIG_HOME': 'config', 'XDG_DATA_HOME': 'data',
    }.items():
        directory = inside(root / child, key)
        directory.mkdir(parents=True, exist_ok=True)
        result[key] = str(directory)
    result['PATH'] = str(Path(result.get('SYSTEMROOT', 'C:/Windows')) / 'System32')
    result['RUST_LOG'] = 'off'
    return result


def command(executable: Path, root: Path) -> list[str]:
    config = {**BASE_CONFIG, 'log_dir': str(root / 'logs'), 'sqlite_home': str(root / 'sqlite')}
    arguments = [str(executable), 'app-server', '--stdio', '--strict-config']
    for key, value in config.items():
        encoded = '{}' if value == {} else json.dumps(value, ensure_ascii=False)
        arguments.extend(('-c', f'{key}={encoded}'))
    return arguments


def check_config(response: JsonMap, root: Path) -> JsonMap:
    config = response.get('config')
    if not isinstance(config, dict):
        raise AstraError('isolation_config_unavailable')
    for dotted, required in BASE_CONFIG.items():
        actual = config
        for key in dotted.split('.'):
            actual = actual.get(key) if isinstance(actual, dict) else None
        if actual is None and dotted.startswith('tools.'):
            layers = response.get('layers') or []
            for layer in layers:
                candidate = layer.get('config', {})
                for key in dotted.split('.'):
                    candidate = candidate.get(key) if isinstance(candidate, dict) else None
                if candidate is True:
                    raise AstraError('isolation_config_mismatch')
                if candidate is False and layer.get('name', {}).get('type') == 'sessionFlags':
                    actual = candidate
        if actual != required:
            raise AstraError('isolation_config_mismatch')
    for key in ('log_dir', 'sqlite_home'):
        value = config.get(key)
        if not isinstance(value, str) or not inside(value, key).is_relative_to(root):
            raise AstraError('isolation_path_mismatch')
    return {'effective_config_checked': True, 'disabled_features': list(DISABLED), 'mcp_servers': 0, 'plugins': 0, 'root': str(root)}


def auth_status() -> JsonMap:
    installed = False
    try:
        native_executable()
        installed = True
        auth = read_auth()
        auth.clear()
        return {'available': True, 'installed': True, 'authenticated': True, 'model': MODEL, 'inference_checked': False}
    except (AstraError, RuntimeError) as exc:
        code = exc.code if isinstance(exc, AstraError) else 'auth_home_unavailable'
        return {'available': False, 'installed': installed, 'authenticated': False, 'model': MODEL, 'error_code': code, 'inference_checked': False}
