from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .document_types import DocumentError, JsonMap, positive
from .documents import create_document, freeze_document, get_document, save_document


@dataclass(frozen=True, slots=True)
class DocumentRequest:
    method: str
    parts: list[str]
    body: JsonMap


def document_response(workspace: Path, request: DocumentRequest) -> tuple[int, JsonMap]:
    match (request.method, request.parts):  # noqa: MATCH_OK - open HTTP paths require a typed 404, not an unreachable assertion.
        case ('GET', []):
            from .document_read import document_list

            return 200, {'documents': document_list(workspace)}
        case ('POST', []):
            return 201, {'document': create_document(workspace, request.body)}
        case ('GET', [identifier]):
            return 200, {'document': get_document(workspace, identifier)}
        case ('GET', [identifier, 'math', node_id, 'source']):
            from .document_read import original_equation

            return 200, original_equation(workspace, identifier, node_id)
        case ('PATCH', [identifier]):
            return 200, {'document': save_document(workspace, identifier, request.body)}
        case ('POST', [identifier, 'math', 'validate']):
            from .document_math import validate_math

            get_document(workspace, identifier)
            return 200, {'equation': validate_math(request.body)}
        case ('POST', [identifier, 'refresh']):
            from .document_refresh import refresh_document

            return 200, refresh_document(workspace, identifier, request.body)
        case ('POST', [identifier, 'exports']):
            if set(request.body) != {'expected_revision', 'format', 'output_role'} or not isinstance(request.body['format'], str) or request.body['format'] not in {'pdf', 'hwpx', 'zip'} or not isinstance(request.body['output_role'], str) or request.body['output_role'] not in {'question_only', 'explanation_only', 'combined_end_section'}:
                raise DocumentError('invalid_document_request', 'Invalid export request')
            expected = positive(request.body['expected_revision'], 'expected_revision')
            if request.body['format'] == 'pdf':
                from .document_pdf import start_pdf

                return 202, start_pdf(workspace, identifier, {**request.body, 'expected_revision': expected})
            from .document_artifacts import start_artifact

            return 202, start_artifact(workspace, identifier, {**request.body, 'expected_revision': expected})
        case ('GET', [identifier, 'artifacts']):
            from .document_artifacts import latest_artifacts

            return 200, latest_artifacts(workspace, identifier)
        case ('GET', [identifier, 'artifacts', build_id]):
            from .document_artifacts import artifact_status

            return 200, artifact_status(workspace, identifier, build_id)
        case ('POST', [identifier, 'artifacts', build_id, 'cancel']):
            from .document_artifacts import cancel_artifact

            if request.body:
                raise DocumentError('invalid_document_request', 'Cancel accepts an empty object')
            return 200, cancel_artifact(workspace, identifier, build_id)
        case ('GET', [identifier, 'exports', build_id]):
            from .document_pdf import pdf_status

            return 200, pdf_status(workspace, identifier, build_id)
        case ('GET', [identifier, 'exports']):
            from .document_pdf import latest_pdfs

            return 200, latest_pdfs(workspace, identifier)
        case ('POST', [identifier, 'exports', build_id, 'cancel']):
            from .document_pdf import cancel_pdf

            if request.body:
                raise DocumentError('invalid_document_request', 'Cancel accepts an empty object')
            return 200, cancel_pdf(workspace, identifier, build_id)
        case _:
            raise DocumentError('document_route_not_found', 'Document API route was not found', 404)
