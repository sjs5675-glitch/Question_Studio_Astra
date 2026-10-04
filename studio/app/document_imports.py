from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .document_assets import collect_assets
from .document_storage import confined
from .document_tree import TreeBuilder
from .document_types import Asset, Content, DocumentError, Group, Import, Json, JsonMap, mapping, positive, sequence, text
from .model import validate_id
from .question_bank import _dependency_seed, get_question_set


@dataclass(frozen=True, slots=True)
class Snapshot:
    record: Import
    data: bytes
    package: JsonMap
    item_root: Path
    group_key: str | None
    parent_id: str | None


def source_snapshot(workspace: Path, project_id: str, item_id: str) -> Snapshot:
    validate_id(project_id, field='project_id')
    validate_id(item_id, field='item_id')
    if not project_id.startswith('project-'):
        raise DocumentError('invalid_document_origin', 'Project ID must identify a Studio project')
    project_path = confined(workspace, f'{project_id}/project.json')
    item_path = confined(workspace, f'{project_id}/items/{item_id}/question.json')
    if not project_path.is_file() or not item_path.is_file():
        raise DocumentError('document_source_missing', f'Missing source item {project_id}/{item_id}', 404)
    data = item_path.read_bytes()
    if len(data) > 20 * 1024 * 1024:
        raise DocumentError('document_limit', 'Source package exceeds 20 MiB')
    try:
        raw: Json = json.loads(data.decode('utf-8'))
        project_raw: Json = json.loads(project_path.read_text(encoding='utf-8'))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise DocumentError('document_source_corrupt', 'Source JSON cannot be decoded') from exc
    package = mapping(raw, 'package')
    project = mapping(project_raw, 'project')
    if package.get('item_id') != item_id or project.get('project_id') != project_id:
        raise DocumentError('document_source_corrupt', 'Source identity differs from its path')
    revision = positive(mapping(package.get('workflow'), 'workflow').get('revision'), 'source_revision')
    snapshot_id = str(uuid4())
    dependency = _dependency_seed(project_id, project, item_id, package)
    identity = mapping(package.get('identity', {}), 'identity')
    parent = identity.get('parent_item_id')
    parent_id = text(parent, 'parent_item_id') if parent is not None else None
    record: Import = {'snapshot_id': snapshot_id, 'project_id': project_id, 'item_id': item_id,
        'source_revision': revision, 'package_sha256': hashlib.sha256(data).hexdigest(), 'relative_path': f'imports/{snapshot_id}.json', 'asset_bindings': {}}
    return Snapshot(record, data, package, item_path.parent, dependency.get('group_key'), parent_id)


def selected_sources(workspace: Path, set_id: str, expected_revision: int) -> tuple[JsonMap, list[Snapshot]]:
    selected = get_question_set(workspace, set_id, resolve_items=False)
    if selected['revision'] != expected_revision:
        raise DocumentError('document_source_revision_conflict', 'Question set revision changed', 409)
    sources: list[Snapshot] = []
    for raw in sequence(selected['items'], 'set.items'):
        item = mapping(raw, 'set.item')
        source = source_snapshot(workspace, text(item.get('project_id'), 'project_id'), text(item.get('item_id'), 'item_id'))
        if source.record['source_revision'] != item.get('source_revision'):
            raise DocumentError('document_source_revision_conflict', 'A selected source revision changed; update the set first', 409)
        sources.append(source)
    if not sources:
        raise DocumentError('document_source_empty', 'Select at least one source question')
    return selected, sources


def _suffix_start(package: JsonMap) -> int:
    printed = mapping(package.get('identity', {}), 'identity').get('printed_number', '')
    label = re.search(r'(\([^()]+\))\s*$', str(printed))
    question = mapping(package.get('content'), 'content').get('question')
    if not label or question is None:
        return 0
    for index, raw_block in enumerate(sequence(mapping(question, 'question').get('blocks', []), 'question.blocks')):
        block = mapping(raw_block, 'block')
        if block.get('type') != 'paragraph':
            continue
        visible = ''.join(str(mapping(inline, 'inline').get('text', ''))
                          for inline in sequence(block.get('inlines', []), 'inlines'))
        if visible.lstrip().startswith(label.group(1)):
            return index
    return 0


def import_content(workspace: Path, sources: list[Snapshot]) -> tuple[list[Import], list[Asset], Content, list[Group], dict[str, bytes]]:
    imports: list[Import] = []
    assets: list[Asset] = []
    content: Content = {'roots': [], 'nodes': []}
    groups: dict[str, Group] = {}
    files: dict[str, bytes] = {}
    parent_roots: dict[tuple[str, str], str] = {}
    parent_sources: dict[tuple[str, str], Snapshot] = {}
    previous_group: str | None = None
    for ordinal, source in enumerate(sources, 1):
        record = source.record
        copied, binaries, asset_ids = collect_assets(source.item_root, source.package)
        record['asset_bindings'] = asset_ids
        assets.extend(copied)
        files.update(binaries)
        files[record['relative_path']] = source.data
        imports.append(record)
        widths = {asset['id']: asset['width_px'] for asset in copied}
        builder = TreeBuilder(record['snapshot_id'], asset_ids,
                              {source_id: widths[owned_id] for source_id, owned_id in asset_ids.items()})
        start = 0
        if source.group_key and source.group_key == previous_group and source.parent_id is None:
            start = _suffix_start(source.package)
        question = builder.question(source.package, question_start=start)
        next(node for node in builder.nodes if node['id'] == question)['payload']['label'] = str(ordinal)
        previous_group = source.group_key
        content['roots'].append(question)
        content['nodes'].extend(builder.nodes)
        if source.group_key is not None:
            group = groups.setdefault(source.group_key, {'id': str(uuid4()), 'source_key': source.group_key,
                'common_node_ids': [], 'child_question_ids': []})
            group['child_question_ids'].append(question)
            ancestor = source
            chain = {(record['project_id'], record['item_id'])}
            anchor = question
            common_ids: list[str] = []
            while ancestor.parent_id is not None:
                key = (record['project_id'], ancestor.parent_id)
                if key in chain or len(chain) > 64:
                    raise DocumentError('document_source_cycle', 'Source parent chain contains a cycle or excessive depth')
                chain.add(key)
                if key not in parent_roots:
                    parent = source_snapshot(workspace, *key)
                    parent_sources[key] = parent
                    copied, binaries, asset_ids = collect_assets(parent.item_root, parent.package)
                    parent.record['asset_bindings'] = asset_ids
                    assets.extend(copied)
                    files.update(binaries)
                    files[parent.record['relative_path']] = parent.data
                    imports.append(parent.record)
                    widths = {asset['id']: asset['width_px'] for asset in copied}
                    parent_builder = TreeBuilder(parent.record['snapshot_id'], asset_ids,
                                                 {source_id: widths[owned_id] for source_id, owned_id in asset_ids.items()})
                    parent_root = parent_builder.question(parent.package, selected=False)
                    parent_roots[key] = parent_root
                    content['nodes'].extend(parent_builder.nodes)
                    content['roots'].insert(content['roots'].index(anchor), parent_root)
                anchor = parent_roots[key]
                common_ids.insert(0, anchor)
                ancestor = parent_sources[key]
            for common_id in common_ids:
                if common_id not in group['common_node_ids']:
                    group['common_node_ids'].append(common_id)
    if sum(len(data) for name, data in files.items() if name.startswith('assets/')) > 200 * 1024 * 1024:
        raise DocumentError('document_asset_limit', 'Document images exceed 200 MiB')
    return imports, assets, content, list(groups.values()), files
