from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .document_assets import asset_hashes, collect_assets
from .document_imports import Snapshot
from .document_storage import confined, pointer, read_object
from .document_tree import TreeBuilder
from .document_types import Asset, Document, Import, Json, JsonMap, Node


def descendants(nodes: dict[str, Node], root_id: str) -> set[str]:
    found: set[str] = set()
    pending = [root_id]
    while pending:
        identifier = pending.pop()
        found.add(identifier)
        pending.extend(nodes[identifier]['children'])
    return found


def tree_shape(nodes: dict[str, Node], identifier: str) -> JsonMap:
    node = nodes[identifier]
    return {'kind': node['kind'], 'payload': node['payload'], 'children': [tree_shape(nodes, child) for child in node['children']]}


def changed_paths(before: Json, after: Json, location: str = '') -> list[str]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[str] = []
        for key in sorted(set(before) | set(after)):
            child = location + '/' + key.replace('~', '~0').replace('/', '~1')
            changes.extend(changed_paths(before.get(key), after.get(key), child))
            if len(changes) > 100:
                return changes[:100] + ['additional_changes']
        return changes
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        changes = []
        for index, (left, right) in enumerate(zip(before, after, strict=True)):
            changes.extend(changed_paths(left, right, f'{location}/{index}'))
            if len(changes) > 100:
                return changes[:100] + ['additional_changes']
        return changes
    return [location]


def _source_key(node: Node, package: JsonMap) -> str:
    origin = node['provenance']
    if origin is None:
        return node['id']
    current: Json = package
    anchors: list[str] = []
    tail: list[str] = []
    location = ''
    parts = origin['original_pointer'].split('/')[1:]
    for depth, encoded in enumerate(parts):
        location += '/' + encoded
        child = pointer(package, location)
        source_id = next((f'{field}:{child[field]}' for field in ('block_id', 'choice_id', 'item_id', 'statement_id')
                          if isinstance(child, dict) and isinstance(child.get(field), str)), None)
        if source_id is not None:
            anchors.append(source_id)
            tail = []
        elif depth >= 2:
            if isinstance(current, list) and len(current) > 1:
                tail.append('anonymous:' + hashlib.sha256(json.dumps(child, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
            else:
                tail.append(encoded)
        current = child
    return json.dumps([node['kind'], parts[:2], anchors, tail], ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class Replacement:
    preview: JsonMap
    snapshot: Import
    nodes: list[Node]
    assets: list[Asset]
    files: dict[str, bytes]


def replacement(root: Path, document: Document, source: Snapshot, root_id: str) -> Replacement:
    nodes = {node['id']: node for node in document['content']['nodes']}
    current_root = nodes[root_id]
    origin = current_root['provenance']
    assert origin is not None
    previous = next(item for item in document['imports'] if item['snapshot_id'] == origin['snapshot_id'])
    original = read_object(confined(root, previous['relative_path']))
    baseline_builder = TreeBuilder(previous['snapshot_id'], previous['asset_bindings'])
    baseline_root = baseline_builder.question(original, selected=current_root['kind'] == 'question')
    baseline_nodes = {node['id']: node for node in baseline_builder.nodes}
    local_changed = tree_shape(nodes, root_id) != tree_shape(baseline_nodes, baseline_root)
    copied, binaries, bindings = collect_assets(source.item_root, source.package)
    new_assets = asset_hashes(copied, bindings)
    existing = {(asset['sha256'], asset['mime_type'], asset['role'], asset['section']): asset for asset in document['assets']}
    aliases: dict[str, str] = {}
    additions: list[Asset] = []
    for asset in copied:
        match = existing.get((asset['sha256'], asset['mime_type'], asset['role'], asset['section']))
        aliases[asset['id']] = match['id'] if match is not None else asset['id']
        if match is None:
            additions.append(asset)
    bindings = {key: aliases[value] for key, value in bindings.items()}
    snapshot = copy.deepcopy(source.record)
    snapshot['asset_bindings'] = bindings
    builder = TreeBuilder(snapshot['snapshot_id'], bindings)
    fresh_root = builder.question(source.package, selected=current_root['kind'] == 'question')
    members = descendants(nodes, root_id)
    old_keys = {node_id: _source_key(nodes[node_id], original) for node_id in members
                if nodes[node_id]['provenance'] is not None and nodes[node_id]['provenance']['snapshot_id'] == previous['snapshot_id']}
    new_keys = {node['id']: _source_key(node, source.package) for node in builder.nodes}
    old_counts, new_counts = Counter(old_keys.values()), Counter(new_keys.values())
    stable = {key: node_id for node_id, key in old_keys.items() if old_counts[key] == 1 and new_counts[key] == 1}
    replacements = {node_id: stable.get(key, node_id) for node_id, key in new_keys.items()}
    identity_changed = any(key not in stable for node_id, key in new_keys.items() if node_id != fresh_root)
    replacements[fresh_root] = root_id
    for node in builder.nodes:
        node['id'] = replacements[node['id']]
        node['children'] = [replacements[child] for child in node['children']]
    old_assets = {key: next(asset['sha256'] for asset in document['assets'] if asset['id'] == value)
                  for key, value in previous['asset_bindings'].items()}
    source_fields = changed_paths(original.get('content'), source.package.get('content'), '/content')
    if original.get('identity') != source.package.get('identity'):
        source_fields.append('/identity')
    if old_assets != new_assets:
        source_fields.append('/resources/assets')
    structural = [(node['kind'], node['provenance']['original_pointer']) for node in baseline_builder.nodes] != [(node['kind'], node['provenance']['original_pointer']) for node in builder.nodes]
    old_identity = original.get('identity', {})
    new_identity = source.package.get('identity', {})
    grouping_changed = isinstance(old_identity, dict) and isinstance(new_identity, dict) and any(old_identity.get(key) != new_identity.get(key) for key in ('parent_item_id', 'group_id'))
    conflicts: list[Json] = []
    if local_changed:
        conflicts.append('local_content_changed')
    if structural:
        conflicts.append('source_structure_changed')
    if identity_changed:
        conflicts.append('source_identity_changed_or_ambiguous')
    if grouping_changed:
        conflicts.append('source_grouping_changed_document_grouping_preserved')
    preview: JsonMap = {'node_id': root_id, 'snapshot_id': previous['snapshot_id'], 'project_id': previous['project_id'],
        'item_id': previous['item_id'], 'old_sha256': previous['package_sha256'], 'new_sha256': snapshot['package_sha256'],
        'source_revision': snapshot['source_revision'], 'source_fields': source_fields, 'conflicts': conflicts,
        'asset_sha256': hashlib.sha256(json.dumps(new_assets, sort_keys=True).encode()).hexdigest(),
        'changed': previous['package_sha256'] != snapshot['package_sha256'] or old_assets != new_assets,
        'resolution_scope': 'keep_local or replace this source subtree; preserve document grouping and surviving node styles'}
    files = {**binaries, snapshot['relative_path']: source.data}
    return Replacement(preview, snapshot, builder.nodes, additions, files)
