from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .document_imports import source_snapshot
from .document_assets import asset_hashes, collect_assets
from .document_merge import Replacement, descendants, replacement
from .document_storage import confined, document_root, immutable, load_document, lock_for, publish_revision, verify_owned
from .document_types import DocumentError, JsonMap, mapping, positive, text
from .document_validation import validate_document
from .documents import check_revision, freeze_document
from .model import utc_now


def refresh_document(workspace: Path, identifier: str, request: JsonMap) -> JsonMap:
    if set(request) - {'expected_revision', 'apply', 'preview_sha256', 'resolutions'} or 'expected_revision' not in request:
        raise DocumentError('invalid_document_request', 'Invalid refresh request fields')
    expected = positive(request['expected_revision'], 'expected_revision')
    apply = request.get('apply', False)
    if not isinstance(apply, bool):
        raise DocumentError('invalid_document_request', 'apply must be boolean')
    document, _ = freeze_document(workspace, identifier, expected)
    root = document_root(workspace, identifier)
    nodes = {node['id']: node for node in document['content']['nodes']}
    imports = {item['snapshot_id']: item for item in document['imports']}
    changes: list[Replacement] = []
    for root_id in document['content']['roots']:
        origin = nodes[root_id]['provenance']
        if origin is None:
            continue
        item = imports[origin['snapshot_id']]
        source = source_snapshot(workspace, item['project_id'], item['item_id'])
        candidate = replacement(root, document, source, root_id)
        if candidate.preview['changed']:
            changes.append(candidate)
    public = [item.preview for item in changes]
    digest = hashlib.sha256(json.dumps({'revision': expected, 'changes': public}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if not apply:
        if set(request) - {'expected_revision', 'apply'}:
            raise DocumentError('invalid_document_request', 'Preview does not accept resolutions')
        freeze_document(workspace, identifier, expected)
        return {'document_revision': expected, 'preview_sha256': digest, 'changes': public, 'applied': False}
    if request.get('preview_sha256') != digest:
        raise DocumentError('document_refresh_conflict', 'Source or preview changed; preview again', 409)
    resolutions = mapping(request.get('resolutions', {}), 'resolutions')
    changed_ids = {text(item.preview['node_id'], 'node_id') for item in changes}
    if set(resolutions) - changed_ids or any(not isinstance(value, str) or value not in {'keep_local', 'take_source'} for value in resolutions.values()):
        raise DocumentError('invalid_document_request', 'Unknown refresh resolution')
    accepted: list[Replacement] = []
    for item in changes:
        root_id = text(item.preview['node_id'], 'node_id')
        if item.preview['conflicts'] and root_id not in resolutions:
            raise DocumentError('document_refresh_conflict', f'Explicit resolution required for {root_id}', 409)
        if resolutions.get(root_id, 'take_source') == 'take_source':
            accepted.append(item)
    if not accepted:
        freeze_document(workspace, identifier, expected)
        return {'document': document, 'applied': False, 'kept_local': sorted(changed_ids)}
    candidate = copy.deepcopy(document)
    files: dict[str, bytes] = {}
    for item in accepted:
        root_id = text(item.preview['node_id'], 'node_id')
        old_ids = descendants(nodes, root_id)
        candidate['content']['nodes'] = [node for node in candidate['content']['nodes'] if node['id'] not in old_ids] + item.nodes
        candidate['imports'].append(item.snapshot)
        candidate['assets'].extend(item.assets)
        files.update(item.files)
    live_ids = {node['id'] for node in candidate['content']['nodes']}
    candidate['styles']['node_overrides'] = {key: value for key, value in candidate['styles']['node_overrides'].items() if key in live_ids}
    candidate['revision'] = expected + 1
    candidate['updated_at'] = utc_now()
    candidate = validate_document(candidate)
    with lock_for(root):
        current = load_document(root)
        check_revision(current, expected)
        for item in accepted:
            source = source_snapshot(workspace, item.snapshot['project_id'], item.snapshot['item_id'])
            actual_assets, _, bindings = collect_assets(source.item_root, source.package)
            actual_asset_hash = hashlib.sha256(json.dumps(asset_hashes(actual_assets, bindings), sort_keys=True).encode()).hexdigest()
            source_path = confined(workspace, f"{item.snapshot['project_id']}/items/{item.snapshot['item_id']}/question.json")
            if source.record['package_sha256'] != item.snapshot['package_sha256'] or actual_asset_hash != item.preview['asset_sha256'] or hashlib.sha256(source_path.read_bytes()).hexdigest() != item.snapshot['package_sha256']:
                raise DocumentError('document_refresh_conflict', 'Source changed before refresh publication', 409)
        for relative, data in files.items():
            path = confined(root, relative)
            if path.exists():
                if path.read_bytes() != data:
                    raise DocumentError('document_asset_corrupt', 'An owned immutable file changed')
            else:
                immutable(path, data)
        verify_owned(root, candidate)
        publish_revision(root, candidate)
    return {'document': candidate, 'applied': True, 'replaced': [item.preview['node_id'] for item in accepted]}
