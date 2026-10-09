from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import sys
import time
from pathlib import Path
from typing import Final
from uuid import uuid4

import pypdfium2 as pdfium

from astra_runtime import inside

from .document_pdf_process import render_fresh_process as render_process
from .document_pdf_cache import read_pdf_cache
from .document_pdf_resources import WEB, print_surface, resource_snapshot
from .document_storage import confined, document_root, encoded, immutable, load_document, lock_for, read_object
from .document_types import Document, DocumentError, JsonMap, mapping, text
from .documents import check_revision
from .model import atomic_write_json

WORKER: Final = threading.Lock()
ACTIVE_GUARD: Final = threading.Lock()
ACTIVE: Final[dict[Path, threading.Event]] = {}
STOPPING: Final = threading.Event()


def export_root(workspace: Path, identifier: str, build_id: str) -> Path:
    if not re.fullmatch('[0-9a-f]{32}', build_id):
        raise DocumentError('unsafe_document_build', 'Invalid PDF build ID')
    return confined(document_root(workspace, identifier), f'exports/{build_id}')


def start_pdf(workspace: Path, identifier: str, request: JsonMap) -> JsonMap:
    if not WORKER.acquire(blocking=False):
        raise DocumentError('document_pdf_busy', 'A PDF build is active; wait for cleanup before retrying', 409)
    launched = False
    owner: Path | None = None
    try:
        if STOPPING.is_set():
            raise DocumentError('document_export_stopping', 'Studio is stopping; no new export was started', 503)
        root = document_root(workspace, identifier)
        with lock_for(root):
            current = load_document(root)
            check_revision(current, int(request['expected_revision']))
            document = copy.deepcopy(current)
            document['output_role'] = request['output_role']
            build_id = uuid4().hex
            owner = export_root(workspace, identifier, build_id)
            owner.mkdir(parents=True)
            immutable(owner / 'document.json', encoded(document))
            frozen: set[str] = set()
            for asset in document['assets']:
                data = confined(root, asset['relative_path']).read_bytes()
                if hashlib.sha256(data).hexdigest() != asset['sha256']:
                    raise DocumentError('document_asset_corrupt', 'Asset changed while freezing PDF input')
                # Assets are content-addressed: items sharing one source crop share one frozen file.
                if asset['relative_path'] not in frozen:
                    immutable(confined(owner, asset['relative_path']), data)
                    frozen.add(asset['relative_path'])
            state: JsonMap = {'build_id': build_id, 'document_id': identifier, 'revision': current['revision'], 'output_role': request['output_role'], 'status': 'building'}
            atomic_write_json(owner / 'status.json', state)
        cancel = threading.Event()
        with ACTIVE_GUARD:
            ACTIVE[owner] = cancel
        try:
            threading.Thread(target=_build, args=(root, owner, document, cancel), daemon=True).start()
        except RuntimeError as exc:
            atomic_write_json(owner / 'status.json', {**state, 'status': 'failed', 'error': 'document_pdf_start_failed'})
            raise DocumentError('document_pdf_start_failed', 'Could not start the PDF worker; previous artifact retained', 503) from exc
        launched = True
        return state
    finally:
        if not launched:
            with ACTIVE_GUARD:
                if owner is not None:
                    ACTIVE.pop(owner, None)
            WORKER.release()


def _build(root: Path, owner: Path, current: Document, cancel: threading.Event) -> None:
    started = time.monotonic()
    state: JsonMap = {'build_id': owner.name, 'document_id': current['document_id'], 'revision': current['revision'], 'output_role': current['output_role'], 'status': 'building'}
    try:
        identity, files = resource_snapshot()
        identity['document'] = hashlib.sha256(encoded(current)).hexdigest()
        cached = confined(root, f"last-pdf-{current['output_role']}.json")
        old = None
        if cached.is_file():
            try:
                old = read_pdf_cache(cached, current['document_id'])
            except (DocumentError, OSError) as exc:
                state['cacheWarning'] = {'code': 'document_pdf_cache_invalid', 'message': str(exc)}
        if old is not None:
            prior = confined(root, f"exports/{text(old['build_id'], 'build_id')}/output.pdf")
            if old.get('buildIdentity') == identity and prior.is_file() and hashlib.sha256(prior.read_bytes()).hexdigest() == old['pdfSha256']:
                with lock_for(root):
                    check_revision(load_document(root), current['revision'])
                    if cancel.is_set():
                        raise DocumentError('document_pdf_canceled', 'PDF canceled; previous artifact retained', 409)
                    atomic_write_json(owner / 'status.json', {**old, 'requested_build_id': owner.name, 'cacheHit': True})
                return
        with print_surface(owner, current, files) as (port, token, assets):
            result = render_process({'root': str(owner), 'port': port, 'token': token, 'assets': assets}, cancel)
        actual = owner / 'render.pdf'
        pdf = pdfium.PdfDocument(actual)
        try:
            pages = len(pdf)
            if not 1 <= pages <= 200 or pages != len(mapping(result['layout'], 'layout')['pages']):
                raise DocumentError('document_pdf_structure', 'PDF page count differs from measured pages')
        finally:
            pdf.close()
        after, _ = resource_snapshot()
        after['document'] = identity['document']
        if after != identity:
            raise DocumentError('document_pdf_identity_changed', 'Browser or renderer resources changed during PDF build', 409)
        if cancel.is_set():
            raise DocumentError('document_pdf_canceled', 'PDF canceled; previous artifact retained', 409)
        digest = hashlib.sha256(actual.read_bytes()).hexdigest()
        receipt: JsonMap = {**state, 'status': 'ready', 'cacheHit': False, 'buildIdentity': identity,
            'pdfSha256': digest, 'bytes': actual.stat().st_size, 'pages': pages, 'render': result,
            'seconds': time.monotonic() - started, 'download': f"/api/documents/{current['document_id']}/exports/{owner.name}/download",
            'warnings': ['Font loading receipts do not certify arbitrary glyph coverage; native HWPX is separate.']}
        with lock_for(root):
            check_revision(load_document(root), current['revision'])
            if cancel.is_set():
                raise DocumentError('document_pdf_canceled', 'PDF canceled; previous artifact retained', 409)
            actual.replace(owner / 'output.pdf')
            atomic_write_json(owner / 'status.json', receipt)
            atomic_write_json(cached, receipt)
    except (DocumentError, OSError, ValueError, RuntimeError) as exc:
        atomic_write_json(owner / 'status.json', {**state, 'status': 'failed', 'error': getattr(exc, 'code', 'document_pdf_failed'), 'message': str(exc), 'seconds': time.monotonic() - started})
    finally:
        try:
            atomic_write_json(owner / 'cleanup.json', {'workerSettled': True,
                'profileCleanup': read_object(owner / 'profile-cleanup.json') if (owner / 'profile-cleanup.json').is_file() else {'reason': 'no_profile_cleanup_receipt'}, 'seconds': time.monotonic() - started})
        except OSError as exc:
            diagnostic = {'event': 'document_pdf_cleanup_record_failed', 'build_id': owner.name, 'error': exc.strerror or str(exc)}
            print(json.dumps(diagnostic), file=sys.stderr)
            try:
                atomic_write_json(owner / 'status.json', {**read_object(owner / 'status.json'), 'cleanupWarning': diagnostic})
            except OSError as status_error:
                print(json.dumps({'event': 'document_pdf_cleanup_status_failed', 'build_id': owner.name, 'error': status_error.strerror or str(status_error)}), file=sys.stderr)
        finally:
            with ACTIVE_GUARD:
                ACTIVE.pop(owner, None)
            WORKER.release()


def pdf_status(workspace: Path, identifier: str, build_id: str) -> JsonMap:
    owner = export_root(workspace, identifier, build_id)
    if not (owner / 'status.json').is_file():
        raise DocumentError('document_pdf_not_found', 'PDF build not found', 404)
    result = read_object(owner / 'status.json')
    with ACTIVE_GUARD:
        active = owner in ACTIVE
    if result['status'] == 'building' and not active:
        return {**result, 'status': 'failed', 'error': 'document_pdf_interrupted', 'message': 'Build was interrupted; previous artifact retained'}
    return result


def cancel_pdf(workspace: Path, identifier: str, build_id: str) -> JsonMap:
    owner = export_root(workspace, identifier, build_id)
    with lock_for(document_root(workspace, identifier)):
        with ACTIVE_GUARD:
            event = ACTIVE.get(owner)
            if event is not None:
                event.set()
    return pdf_status(workspace, identifier, build_id)


def latest_pdfs(workspace: Path, identifier: str) -> JsonMap:
    root = document_root(workspace, identifier)
    with lock_for(root):
        load_document(root)
        receipts = []
        warnings = []
        for role in ('question_only', 'explanation_only', 'combined_end_section'):
            path = confined(root, f'last-pdf-{role}.json')
            if path.is_file():
                try:
                    receipts.append(read_pdf_cache(path, identifier))
                except (DocumentError, OSError):
                    warnings.append({'code': 'document_pdf_cache_invalid', 'output_role': role})
    return {'exports': receipts, 'warnings': warnings}


def pdf_download(workspace: Path, identifier: str, build_id: str) -> tuple[Path, str]:
    result = pdf_status(workspace, identifier, build_id)
    if result['status'] != 'ready':
        raise DocumentError('document_pdf_not_ready', 'PDF is not ready', 409)
    owner = export_root(workspace, identifier, text(result['build_id'], 'build_id'))
    return owner / 'output.pdf', text(result['pdfSha256'], 'PDF SHA')
