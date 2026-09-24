from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final, TypeGuard

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from .document_types import Document, DocumentError, Json, JsonMap, Node, mapping, sequence, text

SCHEMA: Final = Path(__file__).resolve().parents[1] / 'schemas/document.schema.json'
VALIDATOR: Final = Draft202012Validator(json.loads(SCHEMA.read_text(encoding='utf-8')), format_checker=FormatChecker())
BLOCKS: Final = {'paragraph', 'equation', 'figure', 'table', 'box', 'line_break'}
CHILDREN: Final = {
    'question': BLOCKS | {'answer', 'explanation'}, 'paragraph': {'text', 'equation', 'line_break'},
    'text': set(), 'equation': set(), 'figure': set(), 'line_break': set(),
    'table': {'row', 'paragraph'}, 'row': {'cell'}, 'cell': BLOCKS, 'box': BLOCKS | {'choice'},
    'choice': BLOCKS, 'answer': BLOCKS | {'answer'}, 'explanation': BLOCKS,
}


def _schema_document(value: Json) -> TypeGuard[Document]:
    VALIDATOR.validate(value)
    return True


def validate_document(value: Json) -> Document:
    try:
        byte_count = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    except (ValueError, RecursionError) as exc:
        raise DocumentError('invalid_document', 'Document must contain finite JSON values within the depth limit') from exc
    if byte_count > 20 * 1024 * 1024:
        raise DocumentError('document_limit', 'Document exceeds 20 MiB')
    try:
        if not _schema_document(value):
            raise DocumentError('invalid_document', 'Invalid document')
    except ValidationError as exc:
        location = '/'.join(str(part) for part in exc.absolute_path)
        raise DocumentError('invalid_document', f'{location}: {exc.message}') from exc
    document = value
    _layout_geometry(document['layout'])
    nodes = {node['id']: node for node in document['content']['nodes']}
    if len(nodes) != len(document['content']['nodes']):
        raise DocumentError('invalid_document', 'Duplicate node ID')
    imports = {item['snapshot_id']: item for item in document['imports']}
    assets = {item['id']: item for item in document['assets']}
    groups = {item['id']: item for item in document['groups']}
    if any(len(index) != len(items) for index, items in [(imports, document['imports']), (assets, document['assets']), (groups, document['groups'])]):
        raise DocumentError('invalid_document', 'Duplicate import, asset or group ID')
    for item in imports.values():
        if item['relative_path'] != f"imports/{item['snapshot_id']}.json":
            raise DocumentError('invalid_document', 'Import path does not match snapshot ID')
        if any(asset_id not in assets for asset_id in item['asset_bindings'].values()):
            raise DocumentError('invalid_document', 'Import references a missing owned asset')
    extensions = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif'}
    for asset in assets.values():
        if asset['relative_path'] != f"assets/{asset['sha256']}.{extensions[asset['mime_type']]}":
            raise DocumentError('invalid_document', 'Asset path does not match hash and MIME')
    roots = document['content']['roots']
    seen: set[str] = set()
    owners: dict[str, str] = {}
    roles: dict[str, str] = {}
    stack = [(node_id, node_id, 0, 'question' if nodes.get(node_id, {}).get('kind') == 'question' else 'shared') for node_id in roots]
    while stack:
        node_id, owner, depth, role = stack.pop()
        if node_id not in nodes or node_id in seen or depth > 64:
            raise DocumentError('invalid_document', 'Missing node, cycle, shared ownership or depth overflow')
        seen.add(node_id)
        owners[node_id] = owner
        node = nodes[node_id]
        if node['kind'] == 'explanation':
            role = 'explanation'
        if node['kind'] == 'answer' and role == 'question':
            role = 'answer'
        roles[node_id] = role
        for child in node['children']:
            if child not in nodes or nodes[child]['kind'] not in CHILDREN[node['kind']]:
                raise DocumentError('invalid_document', f'Invalid child of {node_id}')
            stack.append((child, owner, depth + 1, role))
        provenance = node['provenance']
        if provenance is not None and provenance['snapshot_id'] not in imports:
            raise DocumentError('invalid_document', 'Unknown source snapshot')
        if node['kind'] == 'figure' and node['payload']['asset_id'] not in assets:
            raise DocumentError('invalid_document', f'Missing figure asset: {node_id}')
        if node['kind'] == 'equation':
            _equation(node)
    if seen != set(nodes):
        raise DocumentError('invalid_document', 'Unowned nodes are not allowed')
    common: set[str] = set()
    grouped: set[str] = set()
    for group in groups.values():
        for node_id in group['common_node_ids']:
            if node_id not in roots or nodes[node_id]['kind'] != 'box' or nodes[node_id]['payload']['role'] != 'shared_context':
                raise DocumentError('invalid_document', 'Common context must be an owned unnumbered root')
            common.add(node_id)
        for node_id in group['child_question_ids']:
            if node_id not in roots or nodes[node_id]['kind'] != 'question' or node_id in grouped:
                raise DocumentError('invalid_document', 'Invalid or multiply grouped child question')
            grouped.add(node_id)
    if any(nodes[node_id]['kind'] != 'question' and node_id not in common for node_id in roots):
        raise DocumentError('invalid_document', 'Only questions and declared common context may be roots')
    for node_id in document['styles']['node_overrides']:
        if node_id not in nodes:
            raise DocumentError('invalid_document', 'Style references an unknown node')
    for node_id in sequence(document['layout']['break_before'], 'break_before'):
        if node_id not in roots:
            raise DocumentError('invalid_document', 'Break references a non-root node')
    for repeat in sequence(document['layout']['context_repeats'], 'context_repeats'):
        record = mapping(repeat, 'context_repeat')
        group_id = text(record['group_id'], 'group_id')
        if group_id not in groups or record['before_question_id'] not in groups[group_id]['child_question_ids']:
            raise DocumentError('invalid_document', 'Invalid repeated-context placement')
    choices: dict[tuple[str, str], set[str]] = {}
    for node in nodes.values():
        if node['kind'] == 'choice':
            choice = text(node['payload']['choice_id'], 'choice_id')
            available = choices.setdefault((owners[node['id']], roles[node['id']]), set())
            if choice in available:
                raise DocumentError('invalid_document', 'Duplicate choice within question')
            available.add(choice)
    for node in nodes.values():
        if node['kind'] == 'answer':
            requested = sequence(node['payload']['choice_ids'], 'choice_ids')
            if any(choice not in choices.get((owners[node['id']], 'question'), set()) for choice in requested):
                raise DocumentError('invalid_document', 'Answer refers to another or missing question choice')
    return document


def _layout_geometry(layout: JsonMap) -> None:
    def number(value: Json) -> float:
        match value:  # noqa: MATCH_OK - malformed JSON numeric values are a typed boundary error.
            case bool():
                raise DocumentError('invalid_document_layout', '지면 치수는 숫자여야 합니다.')
            case int() | float():
                return float(value)
            case _:
                raise DocumentError('invalid_document_layout', '지면 치수는 숫자여야 합니다.')

    margins = mapping(layout['margins_mm'], 'margins_mm')
    width = number(layout['width_mm']) - number(margins['left']) - number(margins['right']) - number(layout['column_gap_mm'])
    height = number(layout['height_mm']) - number(margins['top']) - number(margins['bottom'])
    if width <= 0 or height <= 0:
        raise DocumentError('invalid_document_layout', '용지 크기, 여백과 단 간격을 조정해 본문 영역을 확보하세요.')


def _equation(node: Node) -> None:
    payload = node['payload']
    authority = payload['authority']
    if authority in {'ordinary_tex', 'unsupported_tex', 'manual_disagreement'} and not payload['latex']:
        raise DocumentError('invalid_document', 'TeX authority requires exact current TeX')
    if authority in {'native_hwp_only', 'manual_disagreement'} and not payload['hwp_script']:
        raise DocumentError('invalid_document', 'Native authority requires preserved HWP script')
    if authority in {'plain_only', 'plain_literal'} and payload['plain_text'] is None:
        raise DocumentError('invalid_document', 'Plain authority requires exact plain text')
    if payload['derivation'] is not None:
        receipt = mapping(payload['derivation'], 'derivation')
        latex = text(payload['latex'], 'latex')
        if receipt['latex_sha256'] != hashlib.sha256(latex.encode('utf-8')).hexdigest():
            raise DocumentError('invalid_document', 'Stale equation derivation')
