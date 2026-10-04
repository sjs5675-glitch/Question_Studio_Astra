from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from uuid import uuid4

from astra_runtime import inside

from .document_imports import import_content, selected_sources
from .document_storage import confined, document_root, encoded, immutable, load_document, lock_for, publish_revision, read_object, verify_owned
from .document_types import Document, DocumentError, Json, JsonMap, Node, mapping, positive, text
from .document_validation import validate_document
from .model import utc_now
from .question_bank import get_question_set


def create_document(workspace: Path, request: JsonMap) -> Document:
    if set(request) != {'set_id', 'expected_revision'}:
        raise DocumentError('invalid_document_request', 'Create requires only set_id and expected_revision')
    workspace = inside(workspace, 'workspace_root')
    set_id = text(request['set_id'], 'set_id')
    expected = positive(request['expected_revision'], 'expected_revision')
    selected, sources = selected_sources(workspace, set_id, expected)
    imports, assets, content, groups, files = import_content(workspace, sources)
    identifier = str(uuid4())
    now = utc_now()
    document: Document = {'schema_version': 1, 'document_id': identifier, 'revision': 1,
        'created_at': now, 'updated_at': now, 'origin': {'set_id': set_id, 'set_revision': expected},
        'imports': imports, 'assets': assets, 'content': content, 'groups': groups,
        'styles': {'defaults': {'font_family': 'Noto Sans KR', 'font_size_pt': 11, 'line_spacing': 1.5, 'paragraph_spacing_mm': 0.6}, 'node_overrides': {}},
        'layout': {'template': 'mock_exam', 'paper': 'A4', 'width_mm': 210, 'height_mm': 297,
            'margins_mm': {'top': 15, 'right': 15, 'bottom': 15, 'left': 15}, 'columns': 2, 'column_gap_mm': 8,
            'question_gap_mm': 2, 'break_before': [], 'first_page': 'title_questions', 'title': selected['title'], 'instructions': '',
            'unit': '', 'type': '', 'header': '', 'footer': '', 'page_start': 1, 'cover_number_visible': False, 'context_repeats': []},
        'output_role': 'combined_end_section' if selected['output_mode'] == 'combined_end_section' else 'question_only'}
    document = validate_document(document)
    if len(encoded(document)) > 20 * 1024 * 1024:
        raise DocumentError('document_limit', 'Document exceeds 20 MiB')
    if get_question_set(workspace, set_id, resolve_items=False) != selected:
        raise DocumentError('document_source_revision_conflict', 'Set changed during snapshot creation', 409)
    for item in imports:
        source_path = confined(workspace, f"{item['project_id']}/items/{item['item_id']}/question.json")
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != item['package_sha256']:
            raise DocumentError('document_source_revision_conflict', 'Source changed during snapshot creation', 409)
    root = document_root(workspace, identifier)
    root.mkdir(parents=True, exist_ok=False)
    for relative, data in files.items():
        immutable(confined(root, relative), data)
    verify_owned(root, document)
    publish_revision(root, document)
    return document


def get_document(workspace: Path, identifier: str) -> Document:
    root = document_root(workspace, identifier)
    with lock_for(root):
        return load_document(root)


def check_revision(document: Document, expected: int) -> None:
    if document['revision'] != expected:
        raise DocumentError('document_revision_conflict', f"Expected revision {expected}; current revision {document['revision']}", 409)


def _provenance_unchanged(root: Path, current: Document, candidate: Document) -> None:
    known = {node['id']: node['provenance'] for node in current['content']['nodes']}
    restored = [node for node in candidate['content']['nodes'] if node['id'] not in known]
    if restored:
        for revision in range(current['revision'] - 1, 0, -1):
            past = validate_document(read_object(confined(root, f'revisions/{revision}.json')))
            for node in past['content']['nodes']:
                known.setdefault(node['id'], node['provenance'])
    for node in candidate['content']['nodes']:
        if node['id'] in known and node['provenance'] != known[node['id']]:
            raise DocumentError('document_provenance_conflict', 'Imported provenance is immutable during ordinary saves')
        if node['id'] not in known and node['provenance'] is not None:
            raise DocumentError('document_provenance_conflict', 'New nodes must have no forged import provenance')


def save_document(workspace: Path, identifier: str, request: JsonMap) -> Document:
    if set(request) != {'expected_revision', 'document'}:
        raise DocumentError('invalid_document_request', 'Save requires expected_revision and document')
    expected = positive(request['expected_revision'], 'expected_revision')
    candidate = validate_document(copy.deepcopy(request['document']))
    root = document_root(workspace, identifier)
    with lock_for(root):
        current = load_document(root)
        check_revision(current, expected)
        for field in ('document_id', 'schema_version', 'created_at', 'origin', 'imports', 'assets'):
            if candidate[field] != current[field]:
                raise DocumentError('document_provenance_conflict', f'Cannot change {field} during ordinary save')
        if candidate['revision'] != expected:
            raise DocumentError('document_revision_conflict', 'Candidate revision differs from expected_revision', 409)
        _provenance_unchanged(root, current, candidate)
        verify_owned(root, candidate)
        from .document_math import validate_math_changes

        validate_math_changes(root, current, candidate)
        candidate['revision'] = expected + 1
        candidate['updated_at'] = utc_now()
        publish_revision(root, candidate)
        return candidate


def freeze_document(workspace: Path, identifier: str, expected: int) -> tuple[Document, str]:
    root = document_root(workspace, identifier)
    with lock_for(root):
        current = load_document(root)
        check_revision(current, positive(expected, 'expected_revision'))
        return copy.deepcopy(current), hashlib.sha256(encoded(current)).hexdigest()
