from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path

from .document_types import DocumentError, JsonMap, mapping, sequence, text
from .equation_bridge import CONVERTER_VERSION, EquationBridgeError, convert_latex


class CurrentContent:
    def __init__(self, view: JsonMap, cancel: threading.Event, equations: dict[str, JsonMap] | None = None) -> None:
        content = mapping(view['content'], 'content')
        self.nodes = {text(mapping(node, 'node')['id'], 'node.id'): mapping(node, 'node') for node in sequence(content['nodes'], 'nodes')}
        self.roots = [text(value, 'root') for value in sequence(content['roots'], 'roots')]
        self.cancel = cancel
        self.deadline = time.monotonic() + 60
        self.equations: list[JsonMap] = []
        self.warnings: list[JsonMap] = []
        self.prepared = equations or {}
        styles = mapping(view['styles'], 'styles')
        defaults = mapping(styles['defaults'], 'defaults')
        overrides = mapping(styles['node_overrides'], 'overrides')
        self.styles: dict[str, JsonMap] = {}
        def inherit(identifier: str, parent: JsonMap) -> None:
            style = {**parent, **mapping(overrides.get(identifier, {}), 'override')}
            self.styles[identifier] = style
            for child in sequence(self.nodes[identifier]['children'], 'children'):
                inherit(text(child, 'child'), style)
        for identifier in self.roots:
            inherit(identifier, defaults)

    def check(self) -> None:
        if self.cancel.is_set():
            raise DocumentError('document_export_canceled', 'Export canceled; previous artifact retained', 409)
        if time.monotonic() >= self.deadline:
            raise DocumentError('document_export_timeout', 'HWPX preparation exceeded its time limit', 504)

    def inline(self, identifier: str) -> JsonMap:
        self.check()
        node = self.nodes[identifier]
        payload = mapping(node['payload'], 'payload')
        match node['kind']:
            case 'text':
                marks = sequence(payload['marks'], 'marks')
                style = {**self.styles[identifier], **{text(mark, 'mark'): True for mark in marks}}
                return {'type': 'text', 'text': payload['text'], 'marks': marks, 'document_style': style}
            case 'line_break':
                return {'type': 'line_break'}
            case 'equation':
                authority = payload['authority']
                if authority == 'plain_literal':
                    return {'type': 'text', 'text': payload['plain_text'], 'marks': []}
                if authority != 'ordinary_tex':
                    self.warnings.append({'code': 'source_only_equation', 'node_id': identifier, 'authority': authority})
                    raw = ' / '.join(f'{key}: {value}' for key in ('latex', 'hwp_script', 'plain_text') if (value := payload.get(key)) is not None)
                    return {'type': 'text', 'text': '[수식 확인 필요] ' + raw, 'marks': []}
                latex = text(payload['latex'], 'latex')
                if identifier in self.prepared:
                    record = self.prepared[identifier]
                    self.equations.append(record)
                    if record['kind'] == 'native':
                        self.warnings.append({'code': 'native_equation_metrics_limit', 'node_id': identifier, 'requested': self.styles[identifier], 'actual': {'base_unit': 1100, 'font': 'HYhwpEQ'}, 'message': 'Current equation is editable native math; native engine metrics differ from KaTeX.'})
                        return {'type': 'equation', 'latex': latex, 'hwp_script': record['script']}
                    image = mapping(record['image'], 'image')
                    self.warnings.append({'code': 'noneditable_current_formula', 'node_id': identifier, 'latex_sha256': record['latex_sha256']})
                    return {'type': 'document_formula_image', 'node_id': identifier, 'latex': latex,
                        'width': round(float(image['css_width']) * 75), 'height': round(float(image['css_height']) * 75),
                        'ascent': round(float(image['baseline_css']) * 75),
                        'asset': {'asset_id': identifier, 'state': 'ready', 'media_type': 'image/png', 'path': image['path'],
                            'sha256': image['sha256'], 'width_px': image['width_px'], 'height_px': image['height_px']}}
                try:
                    converted = convert_latex(latex, timeout_seconds=max(.1, min(15, self.deadline - time.monotonic())))
                except EquationBridgeError as exc:
                    raise DocumentError('document_formula_image_required', f'Current formula requires image mapping: {identifier}') from exc
                if converted.warnings or re.search(r'\\begin\s*\{', latex):
                    raise DocumentError('document_formula_image_required', f'Current formula requires image mapping: {identifier}')
                self.equations.append({'node_id': identifier, 'latex_sha256': hashlib.sha256(latex.encode()).hexdigest(), 'converter': CONVERTER_VERSION, 'script': converted.hwp_script, 'kind': 'native'})
                return {'type': 'equation', 'latex': latex, 'hwp_script': converted.hwp_script}
            case _:
                raise DocumentError('document_inline_unsupported', f'Unsupported current inline {identifier}')

    def blocks(self, identifier: str) -> list[JsonMap]:
        self.check()
        node = self.nodes[identifier]
        payload = mapping(node['payload'], 'payload')
        children = [text(value, 'child') for value in sequence(node['children'], 'children')]
        match node['kind']:
            case 'paragraph':
                values = [self.inline(child) for child in children]
                return [{'type': 'paragraph', 'document_style': self.styles[identifier], 'inlines': values or [{'type': 'text', 'text': '', 'marks': []}]}]
            case 'equation' | 'line_break':
                return [{'type': 'paragraph', 'document_style': self.styles[identifier], 'inlines': [self.inline(identifier)]}]
            case 'box':
                return [{'type': 'box', 'title': {'text': payload['title'], 'visible': payload['title_visible']}, 'blocks': [block for child in children for block in self.blocks(child)]}]
            case 'choice':
                return [{'type': 'paragraph', 'inlines': [{'type': 'text', 'text': text(payload['label'], 'label'), 'marks': []}]}] + [block for child in children for block in self.blocks(child)]
            case 'table':
                rows = []
                before = []
                for child in children:
                    row = self.nodes[child]
                    if row['kind'] == 'paragraph':
                        before.extend(self.blocks(child))
                        self.warnings.append({'code': 'native_table_caption_position', 'node_id': child, 'message': 'Current caption is a paragraph immediately before its table, including current math.'})
                        continue
                    cells = []
                    for cell_id in sequence(row['children'], 'cells'):
                        cell = self.nodes[text(cell_id, 'cell')]
                        spec = mapping(cell['payload'], 'cell payload')
                        cells.append({'row_span': spec['row_span'], 'col_span': spec['col_span'], 'role': spec.get('role', 'data'), 'blocks': [block for value in sequence(cell['children'], 'cell children') for block in self.blocks(text(value, 'child'))]})
                    rows.append({'cells': cells})
                return before + [{'type': 'table', 'column_count': payload['column_count'], 'rows': rows}]
            case 'answer':
                label = text(payload.get('label') or '', 'answer label')
                value = text(payload.get('text') or '', 'answer text')
                result = [{'type': 'paragraph', 'inlines': [{'type': 'text', 'text': f'답 {label} {value}', 'marks': []}]}]
                return result + [block for child in children for block in self.blocks(child)]
            case 'explanation' | 'question':
                return [block for child in children for block in self.blocks(child)]
            case 'figure':
                return [{'type': 'figure', 'node_id': identifier, **payload}]
            case _:
                raise DocumentError('document_block_unsupported', f'Unsupported current block {identifier}')
