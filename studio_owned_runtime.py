from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path

from astra_runtime import IsolationError, Settings, inside


def _tailnet_owner_login() -> str | None:
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        status = json.loads(result.stdout.decode("utf-8"))
        self = status["Self"]
        user = status["User"][str(self["UserID"])]
        login = user["LoginName"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(login, str):
        return None
    normalized = login.strip().casefold()
    return normalized or None


def owner_directory(settings: Settings, identifier: str) -> Path:
    if not re.fullmatch(r'[a-z0-9-]{1,64}', identifier):
        raise IsolationError('owner_id', 'expected a short lowercase owned-runtime identifier')
    path = inside(settings.runtime / 'owned-studio' / identifier, 'owned studio runtime')
    for candidate in [path, *path.parents]:
        if candidate.is_symlink() or candidate.is_junction():
            raise IsolationError('owner_id', 'linked runtime paths are forbidden')
    return path


def stop_owned(settings: Settings, identifier: str) -> int:
    directory = owner_directory(settings, identifier)
    state_path = directory / 'owner.json'
    state = json.loads(state_path.read_bytes())
    if state.get('status') != 'running' or state.get('owner_id') != identifier:
        raise IsolationError('owned_stop', 'no running owned studio receipt')
    nonce = state.get('nonce')
    if not isinstance(nonce, str) or not re.fullmatch(r'[a-f0-9]{64}', nonce):
        raise IsolationError('owned_stop', 'invalid owned stop capability')
    request = directory / f'stop-{nonce}.request'
    with request.open('x', encoding='utf-8') as output:
        output.write(nonce)
    deadline = time.monotonic() + 130
    while time.monotonic() < deadline:
        current = json.loads(state_path.read_bytes())
        if current.get('nonce') != nonce:
            raise IsolationError('owned_stop', 'runtime owner changed; no process was killed')
        if current.get('status') == 'stopped':
            print(json.dumps({'status': 'owned_stopped', 'owner_id': identifier, 'pid': state['pid']}))
            return 0
        time.sleep(.1)
    raise IsolationError('owned_stop', 'owned shutdown not acknowledged; no process was killed')


def _owned_session_token(directory: Path, identifier: str, workspace: Path) -> str:
    from app.model import atomic_write_json

    path = directory / 'session.json'
    if path.is_symlink() or path.is_junction():
        raise IsolationError('owned_session', 'linked session receipt is forbidden')
    path = inside(path, 'owned session')
    binding = {'version': 1, 'owner_id': identifier, 'workspace': str(inside(workspace, 'session workspace'))}
    if path.exists():
        record = json.loads(path.read_bytes())
        if not isinstance(record, dict) or set(record) != {*binding, 'token'} or any(record.get(key) != value for key, value in binding.items()):
            raise IsolationError('owned_session', 'session owner/workspace differs; retained receipt requires inspection')
        token = record['token']
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            raise IsolationError('owned_session', 'invalid retained session token; no token was replaced')
        return token
    token = secrets.token_urlsafe(32)
    atomic_write_json(path, {**binding, 'token': token})
    return token


def settle_owned_exports(workspace: Path, *, timeout: float = 120) -> dict[str, bool | int]:
    from app.document_pdf import ACTIVE, ACTIVE_GUARD, STOPPING, WORKER

    STOPPING.set()
    with ACTIVE_GUARD:
        owned = [event for stage, event in ACTIVE.items() if stage.resolve().is_relative_to(workspace.resolve())]
    for event in owned:
        event.set()
    if not WORKER.acquire(timeout=timeout):
        raise IsolationError('owned_shutdown', 'document export worker did not settle; owned lease retained')
    WORKER.release()
    return {'settled': True, 'cancellationRequests': len(owned)}


def serve_owned(settings: Settings, arguments: argparse.Namespace) -> int:
    from app.server import create_server
    from app.model import atomic_write_json

    directory = owner_directory(settings, arguments.owner_id)
    directory.mkdir(parents=True, exist_ok=True)
    lease = directory / 'lease.lock'
    try:
        lock = lease.open('x')
    except FileExistsError as exc:
        raise IsolationError('owned_start', 'owned lease exists; inspect its receipt, do not replace or kill it') from exc
    nonce = secrets.token_hex(32)
    server = None
    watcher = None
    finished = threading.Event()
    stop_file = directory / f'stop-{nonce}.request'
    state = {'owner_id': arguments.owner_id, 'nonce': nonce, 'pid': os.getpid(), 'python': str(settings.python), 'workspace': str(settings.workspace), 'status': 'starting'}
    try:
        atomic_write_json(directory / 'owner.json', state)
        server = create_server(workspace_root=settings.workspace, import_roots=[Path(value) for value in arguments.import_root] if arguments.import_root else [settings.crop_root],
            pdf_roots=[Path(value) for value in arguments.pdf_root] if arguments.pdf_root else [settings.workspace / '.uploads'], crop_root=settings.crop_root,
            recovery_root=Path(arguments.recovery_root) if arguments.recovery_root else None, web_root=Path(arguments.web_root) if arguments.web_root else None,
            host='127.0.0.1', port=settings.studio_port, token=_owned_session_token(directory, arguments.owner_id, settings.workspace),
            trusted_origins=[f'https://aidestkop.taile27542.ts.net:{settings.studio_port}'],
            tailnet_owner_login=_tailnet_owner_login())
        state.update(status='running', port=server.server_address[1])
        atomic_write_json(directory / 'owner.json', state)
        atomic_write_json(directory / 'access.json', {'url': f"http://127.0.0.1:{state['port']}/?token={server.app.config.token}"})
        def watch() -> None:
            while not finished.wait(.1):
                if stop_file.is_file() and stop_file.read_text(encoding='utf-8') == nonce:
                    server.shutdown()
                    return
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        print(json.dumps({'status': 'owned_running', 'owner_id': arguments.owner_id, 'pid': os.getpid(), 'port': state['port'], 'access_file': str(directory / 'access.json')}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        finished.set()
        if server is not None:
            server.server_close()
        if watcher is not None:
            watcher.join(timeout=2)
        settlement_error = None
        try:
            state['export_cleanup'] = settle_owned_exports(settings.workspace)
            state['status'] = 'stopped'
        except IsolationError as error:
            settlement_error = error
            state['status'] = 'shutdown_incomplete'
            state['error'] = str(error)
        try:
            atomic_write_json(directory / 'owner.json', state)
        finally:
            lock.close()
            if settlement_error is None:
                lease.unlink()
        if stop_file.exists():
            stop_file.unlink()
        if settlement_error is not None:
            raise settlement_error
    return 0
