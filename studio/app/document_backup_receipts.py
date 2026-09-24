from __future__ import annotations

import math
import re

from .document_types import DocumentError, Json, JsonMap, mapping

TOP = {'build_id', 'document_id', 'revision', 'format', 'output_role', 'sha256', 'pdfSha256', 'currentHash', 'bytes', 'pages',
       'completeness', 'nativeHancom', 'warnings', 'equations', 'images', 'buildIdentity', 'render', 'source_identity', 'document_sha256', 'template_sha256', 'rendered_roots'}
FIELDS = TOP | {'code', 'node_id', 'authority', 'message', 'control', 'controls', 'requested', 'actual', 'kind', 'editable', 'latex_sha256', 'converter',
    'script', 'conversion_warnings', 'image', 'path', 'css_width', 'css_height', 'baseline_css', 'width_px', 'height_px', 'render_identity', 'fonts', 'faces',
    'descriptor', 'nodeIds', 'textHash', 'family', 'style', 'weight', 'unicodeRange', 'status', 'png_sha256', 'bmp_sha256', 'source_sha256', 'binary_id', 'width', 'height', 'ascent', 'background_policy', 'source_mode', 'image_converter',
    'font_family', 'font_size_pt', 'math_size', 'math_variant', 'line_spacing', 'paragraph_spacing_mm', 'align', 'bold', 'italic', 'underline', 'superscript', 'subscript', 'base_unit', 'font',
    'pdf', 'layout', 'identity', 'browserVersion', 'documentId', 'outputHash', 'role', 'layoutHash', 'assetHashes', 'snapshotHashes', 'manifestHash', 'bundleHash', 'cssHash', 'fontsHash', 'renderer', 'assignmentsHash',
    'assignments', 'repeated', 'selectedQuestionIds', 'outputNodeIds', 'renderedNodeIds', 'geometry', 'outside', 'page', 'column', 'top', 'left', 'right', 'bottom', 'x', 'y', 'cover', 'availableHeight',
    'key', 'rootIds', 'questionId', 'label', 'section', 'group_id', 'before_question_id'}
RESOURCE_KEYS = {'document', 'helper', 'printHtml', 'formulaHtml', 'formulaHelper', 'packageLock', 'edge', 'edgeFileVersion', 'converter',
                 '/styles.css', '/document.css', '/document-print.css', '/document-print.html', '/dist/document/manifest.json'}


def resource_identity(value: Json) -> JsonMap:
    result: JsonMap = {}
    for key, item in mapping(value, 'resource identity').items():
        local_asset = key.startswith('/dist/document/') and '..' not in key and '\\' not in key and ':' not in key and key.endswith(('.js', '.css', '.woff', '.woff2', '.ttf'))
        if key not in RESOURCE_KEYS and not local_asset:
            continue
        if isinstance(item, str) and (re.fullmatch('[a-f0-9]{64}', item) or key in {'edgeFileVersion', 'converter'}):
            result[key] = clean(item)
    return result


def clean(value: Json) -> Json:
    if isinstance(value, str):
        if re.search(r'(?:https?|wss?|file)://|[A-Za-z]:[/\\]|/document-print/[a-f0-9]{32}|/(?:tmp|\.runtime)/', value):
            return '[runtime location excluded]'
        return value
    if isinstance(value, float) and not math.isfinite(value):
        raise DocumentError('document_backup_receipt', 'Non-finite export receipt geometry')
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        result: JsonMap = {}
        for key, item in value.items():
            if key not in FIELDS:
                continue
            if key in {'render_identity', 'buildIdentity'}:
                result[key] = resource_identity(item)
            elif key == 'source_identity':
                result[key] = {name: digest for name, digest in mapping(item, 'source identity').items()
                    if re.fullmatch(r'[a-zA-Z0-9_.-]+\.(?:py|js|mjs|css|json)', name) and isinstance(digest, str) and re.fullmatch('[a-f0-9]{64}', digest)}
            else:
                result[key] = clean(item)
        return result
    return value


def project_receipt(value: JsonMap) -> JsonMap:
    return mapping(clean({key: item for key, item in value.items() if key in TOP}), 'receipt projection')
