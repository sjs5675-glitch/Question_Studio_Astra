from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .document_storage import confined, read_object
from .document_types import Document, DocumentError, JsonMap, Node, mapping, text
from .document_validation import validate_document

HELPER = Path(__file__).resolve().parents[1] / 'web/scripts/document-math.mjs'


def validate_math(request: JsonMap) -> JsonMap:
    if set(request) != {'mode', 'value', 'display'}:
        raise DocumentError('invalid_document_math', 'mode, value, display만 사용할 수 있습니다.')
    mode = text(request['mode'], 'mode')
    value = text(request['value'], 'value')
    display = text(request['display'], 'display')
    if mode not in {'tex', 'literal'} or display not in {'inline', 'display'} or not value.strip() or len(value) > 16384:
        raise DocumentError('invalid_document_math', '수식 입력 방식과 길이를 확인하세요.')
    node = shutil.which('node')
    if node is None:
        raise DocumentError('document_math_unavailable', '로컬 수식 검증 도구를 찾을 수 없습니다.', 503)
    environment = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP') if key in os.environ}
    try:
        process = subprocess.run([node, str(HELPER)], input=json.dumps(request, ensure_ascii=False), encoding='utf-8',
            capture_output=True, timeout=10, cwd=HELPER.parents[3], env=environment, check=False)
        result = mapping(json.loads(process.stdout), 'math result')
    except (OSError, subprocess.TimeoutExpired, UnicodeError, json.JSONDecodeError) as exc:
        raise DocumentError('document_math_unavailable', '로컬 수식 검증을 완료하지 못했습니다.', 503) from exc
    if process.returncode or result.get('valid') is not True:
        raise DocumentError('invalid_document_math', text(result.get('message', '수식을 확인하세요.'), 'message'))
    return {'authority': 'ordinary_tex' if mode == 'tex' else 'plain_literal', 'latex': value if mode == 'tex' else None,
        'hwp_script': None, 'plain_text': value if mode == 'literal' else None, 'display': display, 'validation': 'valid', 'derivation': None}


def _saved_equation(root: Path, current: Document, candidate: Node) -> bool:
    for revision in range(current['revision'] - 1, 0, -1):
        previous = validate_document(read_object(confined(root, f'revisions/{revision}.json')))
        if any(node == candidate for node in previous['content']['nodes']):
            return True
    return False


def validate_math_changes(root: Path, current: Document, candidate: Document) -> None:
    previous = {node['id']: node for node in current['content']['nodes'] if node['kind'] == 'equation'}
    for node in candidate['content']['nodes']:
        if node['kind'] != 'equation':
            continue
        payload = node['payload']
        old = previous.get(node['id'])
        if old is not None and old['payload'] == payload:
            continue
        if _saved_equation(root, current, node):
            continue
        authority = payload['authority']
        if authority in {'ordinary_tex', 'plain_literal'}:
            if authority == 'plain_literal' and old is not None and old['payload']['authority'] in {'native_hwp_only', 'manual_disagreement'}:
                raise DocumentError('document_math_resolution_required', '원본 HWP 수식은 현재 TeX를 명시적으로 입력해 주세요.')
            mode = 'tex' if authority == 'ordinary_tex' else 'literal'
            node['payload'] = validate_math({'mode': mode, 'value': payload['latex'] if mode == 'tex' else payload['plain_text'], 'display': payload['display']})
        elif old is None or any(payload[key] != old['payload'][key] for key in ('authority', 'latex', 'hwp_script', 'plain_text')):
            raise DocumentError('document_math_resolution_required', '현재 수식 값을 명시적으로 확정해 주세요.')
