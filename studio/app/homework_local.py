"""Same-PC, read-only homework synchronization. No operator credential sharing.

This is deliberately separate from the proposed authenticated homework-read v1.
The nonsecret client header prevents browser/simple-request access; it is not a
security boundary between programs running as the same Windows user.
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import re
import uuid
from typing import Any
from urllib.parse import parse_qs

from .model import ModelError, ModelNotFound, _path_lock, approval_digest, load_json_object, registration_summary, solution_link_summary, validate_id
from .question_bank import _bank_signature, _bank_summary, _project_and_item

PREFIX = '/api/integrations/homework/local/v1'
VERSION = 'astra-homework-local.v1'


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _source_id(app) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'astra-homework-local:' + str(app.config.workspace_root.resolve()).casefold()))


def _summaries(app) -> list[dict[str, Any]]:
    """Reuse canonical summary builders; reread only changed package files."""
    workspace = app.config.workspace_root
    signature = _bank_signature(workspace)
    if signature is None:
        raise ModelError('source scan unavailable')
    old = getattr(app, '_homework_local_cache', {})
    current = {}
    projects = {}
    for entry in signature:
        relative, *stamp = entry
        path = workspace / relative
        if path.name != 'project.json':
            continue
        project_id = validate_id(path.parent.name, field='project_id')
        cell = old.get(relative)
        if cell is None or cell[0] != stamp:
            if not path.resolve().is_relative_to(workspace):
                raise ModelError('unsafe source')
            project = load_json_object(path)
            if project.get('project_id') != project_id:
                raise ModelError('source identity mismatch')
            cell = (stamp, project)
        current[relative] = cell
        projects[project_id] = cell
    summaries = []
    for entry in signature:
        relative, *stamp = entry
        path = workspace / relative
        if path.name != 'question.json':
            continue
        project_id = path.parent.parent.parent.name
        item_id = validate_id(path.parent.name, field='item_id')
        if project_id not in projects:
            continue
        project_stamp, project = projects[project_id]
        token = (stamp, project_stamp)
        cell = old.get(relative)
        if cell is None or cell[0] != token:
            try:
                if not path.resolve().is_relative_to(workspace / project_id / 'items'):
                    raise ModelError('unsafe source')
                package = load_json_object(path)
                if package.get('item_id') != item_id:
                    raise ModelError('source identity mismatch')
                summary = _bank_summary(project_id, project, item_id, package, item_root=path.parent, lightweight=True)
                summary['package_sha256'] = _digest(package)
                cell = (token, summary)
            except (ModelError, OSError):
                continue
        current[relative] = cell
        summaries.append(copy.deepcopy(cell[1]))
    app._homework_local_cache = current
    return summaries


def _list(app) -> dict[str, Any]:
    with app.trash.lock:
        for journal in app.trash.journals.glob('purge-*.json'):
            if load_json_object(app.trash._safe(journal)).get('stage') != 'done':
                raise ModelError('purge pending')
        trash = app.trash._state()
        deleted_projects, deleted_items = trash['projects'], trash['items']
        summaries = _summaries(app)
        group_sizes = {}
        for summary in summaries:
            pid, iid = summary['project_id'], summary['item_id']
            key = summary['dependency']['group_key']
            if key and pid not in deleted_projects and iid not in deleted_items.get(pid, {}):
                group_sizes[key] = group_sizes.get(key, 0) + 1
        items = []
        observed = set()
        for summary in summaries:
            project_id, item_id = summary['project_id'], summary['item_id']
            if project_id in deleted_projects or item_id in deleted_items.get(project_id, {}):
                continue
            observed.add((project_id, item_id))
            item = {key: summary.get(key) for key in ('project_id', 'item_id', 'ref', 'revision', 'updated_at', 'identity', 'tags', 'dependency', 'preview', 'status', 'source', 'package_sha256')}
            group_key = item['dependency']['group_key']
            if group_key:
                item['dependency'].update(group_size=group_sizes[group_key], shared_context=True)
            item['change_token'] = _digest(item)
            items.append(item)
        items.sort(key=lambda item: (item['project_id'], item['item_id']))
        unavailable = []
        workspace = app.config.workspace_root
        for project_path in sorted(workspace.glob('project-*/project.json')):
            project_id = project_path.parent.name
            if project_id in deleted_projects:
                continue
            if not project_path.resolve().is_relative_to(workspace):
                raise ModelError('unsafe source')
            project = load_json_object(project_path)
            if project.get('project_id') != project_id:
                raise ModelError('source identity mismatch')
            ids = set(project.get('item_order') or [])
            ids.update(p.parent.name for p in (project_path.parent / 'items').glob('*/question.json'))
            for item_id in sorted(ids):
                if item_id not in deleted_items.get(project_id, {}) and (project_id, item_id) not in observed:
                    unavailable.append({'project_id': project_id, 'item_id': item_id})
        return {'contract_version': VERSION, 'source_id': _source_id(app), 'complete': not unavailable,
                'generation': _digest({'items': items, 'unavailable': unavailable}), 'items': items,
                'unavailable': unavailable}


def _send_json(handler, status: int, value: dict[str, Any]) -> None:
    raw = _bytes(value)
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('X-Astra-Contract-Version', VERSION)
    handler.send_header('X-Astra-DTO-SHA256', hashlib.sha256(raw).hexdigest())
    handler._security_headers(cache='no-store')
    handler.send_header('Content-Length', str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def handle(handler, method: str, parsed) -> None:
    try:
        host = (handler.headers.get('Host') or '').casefold()
        local_hosts = {f'127.0.0.1:{handler.app.config.port}', f'localhost:{handler.app.config.port}'}
        allowed = (ipaddress.ip_address(handler.client_address[0]).is_loopback and host in local_hosts
                   and handler.headers.get('X-Astra-Local-Client') == 'homework'
                   and not handler.headers.get('Origin') and not handler.headers.get('Sec-Fetch-Site'))
        if not allowed or method != 'GET':
            _send_json(handler, 403, {'contract_version': VERSION, 'error': {'code': 'forbidden'}})
            return
        if parsed.path == PREFIX + '/items' and not parsed.query:
            _send_json(handler, 200, _list(handler.app))
            return
        match = re.fullmatch(re.escape(PREFIX) + r'/projects/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/items/([A-Za-z0-9][A-Za-z0-9._-]{0,127})(?:/assets/([A-Za-z0-9][A-Za-z0-9._-]{0,127}))?', parsed.path)
        if match:
            project_id, item_id, asset_id = match.groups()
            query = parse_qs(parsed.query, keep_blank_values=True)
            expected_keys = {'revision', 'sha256'} if asset_id else {'revision'}
            if (set(query) != expected_keys or any(len(values) != 1 for values in query.values())
                    or not re.fullmatch(r'[1-9][0-9]{0,15}', query.get('revision', [''])[0])
                    or (asset_id and not re.fullmatch(r'[0-9a-f]{64}', query.get('sha256', [''])[0]))):
                _send_json(handler, 400, {'contract_version': VERSION, 'error': {'code': 'invalid_request'}})
                return
            with handler.app.trash.lock:
                if handler.app.trash.pending(project_id, item_id):
                    raise ModelError('purge pending')
                if handler.app.trash.is_deleted(project_id, item_id):
                    _send_json(handler, 404, {'contract_version': VERSION, 'error': {'code': 'not_found'}})
                    return
                _, _, path = _project_and_item(handler.app.config.workspace_root, project_id, item_id)
                with _path_lock(path):
                    package = load_json_object(path)
                    revision = package['workflow']['revision']
                    if revision != int(query['revision'][0]):
                        _send_json(handler, 409, {'contract_version': VERSION, 'error': {'code': 'revision_conflict', 'current_revision': revision}})
                        return
                    if asset_id:
                        asset = next((a for a in package.get('resources', {}).get('assets', [])
                                      if isinstance(a, dict) and a.get('asset_id') == asset_id and a.get('state') == 'ready'
                                      and a.get('media_type') in {'image/png', 'image/jpeg', 'image/bmp'}), None)
                        if asset is None or asset_id.startswith('solution-page-'):
                            _send_json(handler, 404, {'contract_version': VERSION, 'error': {'code': 'not_found'}})
                            return
                        if asset.get('sha256') != query['sha256'][0]:
                            _send_json(handler, 409, {'contract_version': VERSION, 'error': {'code': 'asset_sha_mismatch'}})
                            return
                        relative = asset.get('path')
                        if (not isinstance(relative, str) or not relative or ':' in relative or relative.startswith(('/', '\\'))
                                or any(part in {'', '.', '..'} for part in relative.replace('\\', '/').split('/'))):
                            raise ModelError('unsafe asset')
                        asset_path = (path.parent / relative.replace('\\', '/')).resolve()
                        if not asset_path.is_relative_to(path.parent.resolve()) or not asset_path.is_file():
                            _send_json(handler, 404, {'contract_version': VERSION, 'error': {'code': 'not_found'}})
                            return
                        with asset_path.open('rb') as stream:
                            raw = stream.read(64 * 1024 * 1024 + 1)
                        if len(raw) > 64 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != query['sha256'][0]:
                            _send_json(handler, 409, {'contract_version': VERSION, 'error': {'code': 'asset_sha_mismatch'}})
                            return
                        handler.send_response(200)
                        handler.send_header('Content-Type', asset['media_type'])
                        handler.send_header('Content-Length', str(len(raw)))
                        handler.send_header('X-Astra-Contract-Version', VERSION)
                        handler.send_header('X-Astra-Revision', str(revision))
                        handler.send_header('X-Astra-Asset-SHA256', query['sha256'][0])
                        handler._security_headers(cache='no-store')
                        handler.end_headers()
                        handler.wfile.write(raw)
                        return
                    issues = handler.app._validation_issues(path, package, check_files=True)
                    review = package.get('review') or {}
                    valid = (package['workflow'].get('status') == 'content_ready'
                             and type(review.get('approved_revision')) is int
                             and review['approved_revision'] == revision
                             and review.get('approved_sha256') == approval_digest(package)
                             and not any(issue.get('blocking') is True for issue in issues))
                    _send_json(handler, 200, {'contract_version': VERSION, 'source_id': _source_id(handler.app),
                        'package': package, 'revision': revision, 'package_sha256': _digest(package),
                        'approval_valid': valid, 'registration': registration_summary(package, warning_count=len(issues)),
                        'solution_link': solution_link_summary(package, item_root=path.parent),
                        'asset_urls': {a['asset_id']: PREFIX + f'/projects/{project_id}/items/{item_id}/assets/{a["asset_id"]}?revision={revision}&sha256={a["sha256"]}'
                                       for a in package.get('resources', {}).get('assets', [])
                                       if isinstance(a, dict) and a.get('state') == 'ready' and re.fullmatch(r'[0-9a-f]{64}', a.get('sha256') or '')}})
                    return
        _send_json(handler, 404, {'contract_version': VERSION, 'error': {'code': 'not_found'}})
    except ModelNotFound:
        _send_json(handler, 404, {'contract_version': VERSION, 'error': {'code': 'not_found'}})
    except (ModelError, OSError, ValueError, TypeError, KeyError):
        _send_json(handler, 503, {'contract_version': VERSION, 'error': {'code': 'source_unavailable'}})
