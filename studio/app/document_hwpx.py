from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import threading
from pathlib import Path

from .document_hwpx_content import CurrentContent
from .document_hwpx_math import prepare_math
from .document_hwpx_renderer import CurrentRenderer
from .document_hwpx_template import document_template
from .document_pdf_resources import WEB
from .document_storage import immutable
from .document_types import Document, DocumentError, JsonMap, mapping, sequence, text
from .hwpx_native import text_xml
from .hwpx_renderer import _LoadedItem, _Renderer, _inspect_package, _package_bytes


def build_current_hwpx(current: Document, stage: Path, cancel: threading.Event) -> JsonMap:
    node = shutil.which('node')
    if not node:
        raise DocumentError('document_dependency_missing', 'Node.js is required', 503)
    prepared = subprocess.run([node, str(WEB / 'scripts/document-view.mjs'), str(stage / 'document.json')], cwd=WEB,
        capture_output=True, text=True, encoding='utf-8', timeout=10, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    views = mapping(json.loads(prepared.stdout), 'role views')
    context = document_template(current, stage)
    prepared_math = prepare_math(current, views, stage, cancel)
    renderer = CurrentRenderer(context, current, stage)
    for key in ('header', 'title', 'instructions', 'unit', 'type', 'footer'):
        value = text(current['layout'][key], key)
        if value:
            renderer._paragraph(text_xml(value), value)
    equations = []
    warnings = [{'code': 'native_layout_reflow', 'controls': ['layout.first_page', 'layout.header', 'layout.footer', 'layout.page_start', 'layout.cover_number_visible', 'layout.context_repeats'], 'message': 'HWPX uses native flow; frontmatter is current text and requested shared-context copies are included, but repeating chrome and exact PDF page/column assignments are not reproduced.'}]
    rendered_roots = []
    for section in ('questions', 'solutions'):
        value = views[section]
        if value is None:
            continue
        content = CurrentContent(mapping(value, section), cancel, prepared_math)
        if section == 'solutions' and views['questions'] is not None:
            renderer._paragraph(text_xml('답과 해설'), '답과 해설', page_break=True)
        groups = [mapping(group, 'group') for group in sequence(mapping(value, section)['groups'], 'groups')]
        common = {text(identifier, 'common') for group in groups for identifier in sequence(group['common_node_ids'], 'common')}
        emitted: set[str] = set()
        ordered = []
        for identifier in content.roots:
            if identifier in common:
                continue
            group = next((group for group in groups if identifier in sequence(group['child_question_ids'], 'children')), None)
            if group:
                repeated = any(mapping(record, 'repeat')['group_id'] == group['id'] and mapping(record, 'repeat')['before_question_id'] == identifier for record in sequence(current['layout']['context_repeats'], 'repeats'))
                for raw in sequence(group['common_node_ids'], 'common'):
                    context_id = text(raw, 'common')
                    if context_id not in emitted or repeated:
                        ordered.append(context_id)
                        emitted.add(context_id)
            ordered.append(identifier)
        for index, identifier in enumerate(ordered):
            rendered_roots.append({'section': section, 'node_id': identifier})
            if index == 0 and current['layout']['first_page'] == 'cover':
                renderer._paragraph('', '', page_break=True)
            root_node = content.nodes[identifier]
            payload = mapping(root_node['payload'], 'payload')
            if root_node['kind'] == 'question':
                label = f"문항 {payload['label']}" + (' · 답과 해설' if section == 'solutions' else '')
                marker = renderer._paragraph(renderer.styled_text(label), label)
                if identifier in sequence(current['layout']['break_before'], 'breaks'):
                    marker.set('columnBreak', '1')
                if section == 'solutions' and not sequence(root_node['children'], 'children'):
                    warnings.append({'code': 'missing_solution', 'node_id': identifier, 'message': 'No saved answer or explanation was found.'})
                    renderer._paragraph(renderer.styled_text('정답·해설 없음 — 확인 필요'), '정답·해설 없음 — 확인 필요')
            blocks = content.blocks(identifier)
            item = _LoadedItem(identifier, stage / 'document.json', {'resources': {'assets': []}}, 'canonical', None, 'saved', 'saved')
            renderer._render_blocks(item, blocks, identifier, target=None, available_width=context.writable_width, depth=0, ancestors=frozenset())
        equations.extend(content.equations)
        warnings.extend(content.warnings)
    for owner, style in [('defaults', current['styles']['defaults']), *current['styles']['node_overrides'].items()]:
        for control, requested in style.items():
            if control in {'math_size', 'math_variant', 'superscript', 'subscript'}:
                warnings.append({'code': 'native_style_limit', 'node_id': owner, 'control': control, 'requested': requested, 'message': 'Text uses native character properties; native equation font/baseline remains the equation engine default unless a current image is recorded.'})
            if control == 'font_family':
                warnings.append({'code': 'native_font_not_embedded', 'node_id': owner, 'control': control, 'requested': requested, 'message': 'Font family is recorded in native font references; Hancom must have this font installed.'})
    section, hpf, binaries = renderer.finish()
    data = _package_bytes(context, section, hpf, '\n'.join(renderer.preview).encode('utf-8'), binaries)
    inspected = _inspect_package(data, dict(renderer.canonical), context.writable_width)
    immutable(stage / 'output.hwpx', data)
    source_names = ['document_hwpx.py', 'document_hwpx_content.py', 'document_hwpx_math.py', 'document_hwpx_renderer.py', 'document_hwpx_styles.py', 'document_hwpx_template.py', 'hwpx_renderer.py', 'hwpx_native.py', 'equation_bridge.py']
    sources = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in source_names}
    return {'format': 'hwpx', 'currentHash': views['currentHash'], 'equations': equations, 'images': renderer.image_receipts, 'warnings': warnings,
        'rendered_roots': rendered_roots, 'source_identity': sources, 'document_sha256': hashlib.sha256((stage / 'document.json').read_bytes()).hexdigest(),
        'imports': current['imports'], 'assets': current['assets'], 'template_sha256': hashlib.sha256((stage / 'template.hwpx').read_bytes()).hexdigest(),
        'nativeHancom': 'unverified', 'inspection': inspected, 'completeness': 'source_only' if any(item['code'] == 'source_only_equation' for item in warnings) else 'current_content'}
