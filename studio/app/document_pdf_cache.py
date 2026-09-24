from __future__ import annotations

import re
from pathlib import Path

from .document_storage import read_object
from .document_types import DocumentError, JsonMap, mapping, positive, text


def read_pdf_cache(path: Path, identifier: str) -> JsonMap:
    receipt = read_object(path)
    required = {'status', 'build_id', 'document_id', 'revision', 'output_role', 'pdfSha256', 'buildIdentity', 'bytes', 'pages', 'download', 'render'}
    if not required.issubset(receipt):
        raise DocumentError('document_pdf_cache_invalid', 'Saved PDF cache receipt is incomplete')
    build_id = text(receipt['build_id'], 'build_id')
    digest = text(receipt['pdfSha256'], 'pdfSha256')
    role = text(receipt['output_role'], 'output_role')
    if not re.fullmatch('[a-f0-9]{32}', build_id) or not re.fullmatch('[a-f0-9]{64}', digest):
        raise DocumentError('document_pdf_cache_invalid', 'Saved PDF cache identity is malformed')
    if receipt['status'] != 'ready' or receipt['document_id'] != identifier or role not in {'question_only', 'explanation_only', 'combined_end_section'}:
        raise DocumentError('document_pdf_cache_invalid', 'Saved PDF cache ownership is invalid')
    if receipt['download'] != f'/api/documents/{identifier}/exports/{build_id}/download' or positive(receipt['pages'], 'pages') > 200:
        raise DocumentError('document_pdf_cache_invalid', 'Saved PDF download metadata is invalid')
    positive(receipt['revision'], 'revision')
    positive(receipt['bytes'], 'bytes')
    mapping(receipt['buildIdentity'], 'buildIdentity')
    mapping(receipt['render'], 'render')
    return receipt
