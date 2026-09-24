from __future__ import annotations

import hashlib
from uuid import uuid4

from .document_types import DocumentError, Json, JsonMap, Node, NodeKind, mapping, positive, sequence, text


class TreeBuilder:
    def __init__(self, snapshot_id: str, asset_ids: dict[str, str]) -> None:
        self.snapshot_id = snapshot_id
        self.asset_ids = asset_ids
        self.nodes: list[Node] = []

    def add(self, kind: NodeKind, payload: JsonMap, pointer: str) -> Node:
        node: Node = {'id': str(uuid4()), 'kind': kind, 'children': [], 'payload': payload,
                      'provenance': {'snapshot_id': self.snapshot_id, 'original_pointer': pointer}}
        self.nodes.append(node)
        return node

    def block(self, raw: Json, pointer: str) -> str:
        if pointer.count('/') > 100 or len(self.nodes) >= 30000:
            raise DocumentError('document_limit', 'Source nesting or node count exceeds document limit')
        source = mapping(raw, pointer)
        kind = source.get('type')
        match kind:  # noqa: MATCH_OK - unknown legacy source kinds must produce a typed import error.
            case 'text':
                return self.add('text', {'text': text(source.get('text'), 'text'), 'marks': sequence(source.get('marks', []), 'marks')}, pointer)['id']
            case 'line_break':
                return self.add('line_break', {}, pointer)['id']
            case 'equation':
                latex = source.get('latex')
                native = source.get('hwp_script')
                plain = source.get('plain_text')
                if source.get('source_format') == 'plain':
                    authority = 'plain_only'
                elif latex and native and source.get('hwp_script_mode') == 'manual':
                    authority = 'manual_disagreement'
                elif latex:
                    authority = 'ordinary_tex'
                else:
                    authority = 'native_hwp_only'
                derivative = source.get('hwp_derivation')
                if derivative is not None and (not latex or mapping(derivative, 'derivation').get('latex_sha256') != hashlib.sha256(text(latex, 'latex').encode('utf-8')).hexdigest()):
                    derivative = None
                return self.add('equation', {'authority': authority, 'latex': latex, 'hwp_script': native,
                    'plain_text': plain, 'display': 'inline', 'validation': 'unverified', 'derivation': derivative}, pointer)['id']
            case 'figure':
                original = text(source.get('asset_id'), 'asset_id')
                if original not in self.asset_ids:
                    raise DocumentError('document_asset_corrupt', f'Figure asset is unavailable: {original}')
                return self.add('figure', {'asset_id': self.asset_ids[original], 'alt_text': text(source.get('alt_text', ''), 'alt_text'),
                    'role': text(source.get('role', 'figure'), 'role'), 'width_mm': 65, 'height_mm': None, 'keep_aspect': True}, pointer)['id']
            case 'paragraph':
                node = self.add('paragraph', {'role': text(source.get('role', 'body'), 'role')}, pointer)
                node['children'] = self.blocks(source.get('inlines', []), pointer + '/inlines')
            case 'box':
                title = mapping(source.get('title', {'visible': False, 'text': ''}), 'title')
                node = self.add('box', {'role': text(source.get('role', 'box'), 'role'), 'title': text(title.get('text', ''), 'title.text'),
                    'title_visible': title.get('visible', False), 'source_layout': None}, pointer)
                node['children'] = self.blocks(source.get('blocks', []), pointer + '/blocks')
            case 'bogi':
                title = mapping(source.get('title', {'visible': True, 'text': '보기'}), 'title')
                node = self.add('box', {'role': 'bogi', 'title': text(title.get('text', ''), 'title.text'),
                    'title_visible': title.get('visible', True), 'source_layout': None}, pointer)
                for index, item in enumerate(sequence(source.get('items'), 'bogi.items')):
                    statement = mapping(item, 'bogi.item')
                    location = f'{pointer}/items/{index}'
                    child = self.add('box', {'role': 'statement', 'title': text(statement.get('label'), 'label'),
                        'title_visible': True, 'source_layout': None}, location)
                    child['children'] = self.blocks(statement.get('blocks'), location + '/blocks')
                    node['children'].append(child['id'])
            case 'choices':
                node = self.add('box', {'role': 'choices', 'title': '', 'title_visible': False, 'source_layout': source.get('source_layout')}, pointer)
                for index, item in enumerate(sequence(source.get('items'), 'choices.items')):
                    choice = mapping(item, 'choice')
                    location = f'{pointer}/items/{index}'
                    child = self.add('choice', {'label': text(choice.get('label'), 'label'), 'choice_id': text(choice.get('choice_id'), 'choice_id')}, location)
                    child['children'] = self.blocks(choice.get('blocks'), location + '/blocks')
                    node['children'].append(child['id'])
            case 'table':
                node = self.add('table', {'column_count': positive(source.get('column_count'), 'column_count'),
                    'semantic_type': text(source.get('semantic_type', 'data'), 'semantic_type')}, pointer)
                for index, raw_row in enumerate(sequence(source.get('rows'), 'rows')):
                    row = mapping(raw_row, 'row')
                    location = f'{pointer}/rows/{index}'
                    child = self.add('row', {}, location)
                    for cell_index, raw_cell in enumerate(sequence(row.get('cells'), 'cells')):
                        cell = mapping(raw_cell, 'cell')
                        cell_path = f'{location}/cells/{cell_index}'
                        cell_node = self.add('cell', {'role': text(cell.get('role', 'data'), 'role'),
                            'row_span': positive(cell.get('row_span', 1), 'row_span'), 'col_span': positive(cell.get('col_span', 1), 'col_span')}, cell_path)
                        cell_node['children'] = self.blocks(cell.get('blocks'), cell_path + '/blocks')
                        child['children'].append(cell_node['id'])
                    node['children'].append(child['id'])
                if source.get('caption'):
                    caption = self.add('paragraph', {'role': 'caption'}, pointer + '/caption')
                    caption['children'] = self.blocks(source['caption'], pointer + '/caption')
                    node['children'].append(caption['id'])
            case _:
                raise DocumentError('document_import_unsupported', f'Unsupported source node type: {kind}')
        return node['id']

    def blocks(self, raw: Json, pointer: str) -> list[str]:
        return [self.block(value, f'{pointer}/{index}') for index, value in enumerate(sequence(raw, pointer))]

    def answer(self, raw: Json, pointer: str) -> str:
        source = mapping(raw, 'answer')
        kind = text(source.get('kind'), 'answer.kind')
        payload: JsonMap = {'kind': kind, 'label': None, 'text': None, 'choice_ids': [], 'value': None, 'part_id': None}
        node = self.add('answer', payload, pointer)
        match kind:  # noqa: MATCH_OK - unknown source answer kinds must produce a typed import error.
            case 'choice':
                payload['choice_ids'] = sequence(source.get('choice_ids'), 'choice_ids')
            case 'text':
                payload['text'] = text(source.get('text'), 'answer.text')
            case 'content':
                node['children'] = self.blocks(source.get('blocks'), pointer + '/blocks')
            case 'composite':
                for index, raw_item in enumerate(sequence(source.get('items'), 'items')):
                    item = mapping(raw_item, 'answer.item')
                    child_id = self.answer(item.get('answer'), f'{pointer}/items/{index}/answer')
                    child = next(value for value in self.nodes if value['id'] == child_id)
                    child['payload']['label'] = item.get('label')
                    child['payload']['part_id'] = item.get('item_id')
                    node['children'].append(child_id)
            case 'true_false':
                for index, raw_item in enumerate(sequence(source.get('items'), 'items')):
                    item = mapping(raw_item, 'answer.item')
                    child = self.add('answer', {'kind': 'true_false', 'label': None, 'text': None, 'choice_ids': [],
                        'value': item.get('value'), 'part_id': item.get('statement_id')}, f'{pointer}/items/{index}')
                    node['children'].append(child['id'])
            case 'unknown':
                pass
            case _:
                raise DocumentError('document_import_unsupported', f'Unsupported answer kind: {kind}')
        return node['id']

    def question(self, package: JsonMap, selected: bool = True) -> str:
        content = mapping(package.get('content'), 'content')
        identity = mapping(package.get('identity', {}), 'identity')
        if selected:
            root = self.add('question', {'label': text(identity.get('printed_number', package.get('item_id', '')), 'printed_number'),
                                       'selected': True, 'diagnostics': []}, '/content/question')
        else:
            root = self.add('box', {'role': 'shared_context', 'title': '', 'title_visible': False, 'source_layout': None}, '/content/question')
        question = content.get('question')
        if question is not None:
            root['children'] = self.blocks(mapping(question, 'question').get('blocks', []), '/content/question/blocks')
        elif selected:
            root['payload']['diagnostics'] = ['question_content_missing']
        if selected:
            for role in ('answer', 'explanation'):
                raw = content.get(role)
                if raw is None:
                    sequence(root['payload']['diagnostics'], 'diagnostics').append(f'{role}_missing')
                elif role == 'answer':
                    root['children'].append(self.answer(raw, '/content/answer'))
                else:
                    explanation = self.add('explanation', {}, '/content/explanation')
                    explanation['children'] = self.blocks(mapping(raw, 'explanation').get('blocks', []), '/content/explanation/blocks')
                    root['children'].append(explanation['id'])
        return root['id']
