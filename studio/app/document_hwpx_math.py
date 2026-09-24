from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path

from astra_runtime import inside

from .document_pdf_process import render_fresh_process as render_process
from .document_pdf_resources import WEB, print_surface, resource_snapshot
from .document_types import Document, DocumentError, JsonMap, mapping, sequence, text
from .equation_bridge import CONVERTER_VERSION, EquationBridgeError, convert_latex
from .model import atomic_write_json


def prepare_math(current: Document, views: JsonMap, stage: Path, cancel: threading.Event) -> dict[str, JsonMap]:
    equations: dict[str, JsonMap] = {}
    requested: list[str] = []
    deadline = time.monotonic() + 60
    for role in ('questions', 'solutions'):
        if views[role] is None:
            continue
        view = mapping(views[role], role)
        for raw in sequence(mapping(view['content'], 'content')['nodes'], 'nodes'):
            node = mapping(raw, 'node')
            identifier = text(node['id'], 'id')
            if node['kind'] != 'equation' or identifier in equations:
                continue
            payload = mapping(node['payload'], 'payload')
            if payload['authority'] != 'ordinary_tex':
                continue
            if cancel.is_set() or time.monotonic() >= deadline:
                raise DocumentError('document_export_interrupted', 'Formula preparation canceled or exceeded its deadline')
            latex = text(payload['latex'], 'latex')
            record: JsonMap = {'node_id': identifier, 'latex_sha256': hashlib.sha256(latex.encode()).hexdigest(), 'converter': CONVERTER_VERSION}
            try:
                converted = convert_latex(latex, timeout_seconds=max(.1, min(15, deadline - time.monotonic())))
                warnings = list(converted.warnings)
                script = converted.hwp_script
            except EquationBridgeError as exc:
                warnings = [str(exc)]
                script = ''
            override = current['styles']['node_overrides'].get(identifier, {})
            image_required = bool(warnings or re.search(r'\\begin\s*\{', latex) or override.get('math_size') or override.get('math_variant') == 'bold')
            equations[identifier] = {**record, 'kind': 'image' if image_required else 'native', 'script': script, 'conversion_warnings': warnings}
            if image_required:
                requested.append(identifier)
    if not requested:
        return equations
    identity, files = resource_snapshot()
    html = (WEB / 'document-formulas.html').read_bytes()
    files['/document-print.html'] = html
    identity['formulaHtml'] = hashlib.sha256(html).hexdigest()
    identity['/document-print.html'] = identity['formulaHtml']
    identity['printHtml'] = identity['formulaHtml']
    identity['formulaHelper'] = hashlib.sha256((WEB / 'scripts/document-formula-images.mjs').read_bytes()).hexdigest()
    with print_surface(stage, current, files) as (port, token, assets):
        result = render_process({'mode': 'formula', 'root': str(stage), 'port': port, 'token': token, 'assets': assets,
            'equation_ids': requested, 'timeout_ms': max(1, round((deadline - time.monotonic()) * 1000))}, cancel)
    for raw in sequence(result['images'], 'images'):
        image = mapping(raw, 'image')
        identifier = text(image['node_id'], 'node_id')
        if image['latex_sha256'] != equations[identifier]['latex_sha256']:
            raise DocumentError('document_formula_stale', 'Rendered image differs from current TeX')
        equations[identifier].update({'image': image, 'render_identity': identity, 'fonts': result['fonts'], 'editable': False})
    if any('image' not in equations[identifier] for identifier in requested):
        raise DocumentError('document_formula_missing', 'Current formula image is missing')
    atomic_write_json(stage / 'formula-render.json', result)
    return equations
