from __future__ import annotations

import copy
import hashlib
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

from .document_hwpx import build_current_hwpx
from .document_backup import build_document_backup, freeze_receipts
from .document_pdf import ACTIVE, ACTIVE_GUARD, STOPPING, WORKER, export_root
from .document_storage import confined, document_root, encoded, immutable, load_document, lock_for, read_object
from .document_types import Document, DocumentError, JsonMap, positive, text
from .documents import check_revision
from .hwpx_renderer import HwpxRenderError
from .model import atomic_write_json


def write_status(stage: Path, value: JsonMap) -> None:
    with lock_for(stage):
        atomic_write_json(stage / 'status.json', value)


def start_artifact(workspace: Path, identifier: str, request: JsonMap) -> JsonMap:
    if not WORKER.acquire(blocking=False):
        raise DocumentError('document_export_busy', 'Another document export is active', 409)
    stage = None
    launched = False
    try:
        if STOPPING.is_set():
            raise DocumentError('document_export_stopping', 'Studio is stopping; no new export was started', 503)
        root = document_root(workspace, identifier)
        with lock_for(root):
            current = load_document(root)
            check_revision(current, positive(request['expected_revision'], 'expected_revision'))
            current = copy.deepcopy(current)
            if request['format'] != 'zip':
                current['output_role'] = request['output_role']
            build = uuid4().hex
            stage = export_root(workspace, identifier, build)
            stage.mkdir(parents=True)
            immutable(stage / 'document.json', encoded(current))
            frozen: set[str] = set()
            for item in [*current['imports'], *current['assets']]:
                relative = item['relative_path']
                data = confined(root, relative).read_bytes()
                expected = item.get('sha256', item.get('package_sha256'))
                if hashlib.sha256(data).hexdigest() != expected:
                    raise DocumentError('document_input_changed', 'Input changed during export snapshot')
                # Assets are content-addressed: items sharing one source crop share one frozen file.
                if relative not in frozen:
                    immutable(confined(stage, relative), data)
                    frozen.add(relative)
            if request['format'] == 'zip':
                freeze_receipts(root, stage, current)
            state: JsonMap = {'build_id': build, 'document_id': identifier, 'revision': current['revision'], 'format': request['format'],
                'output_role': current['output_role'], 'status': 'building', 'status_url': f'/api/documents/{identifier}/artifacts/{build}',
                'cancel_url': f'/api/documents/{identifier}/artifacts/{build}/cancel'}
            write_status(stage, state)
        cancel = threading.Event()
        with ACTIVE_GUARD:
            ACTIVE[stage] = cancel
        try:
            threading.Thread(target=build_artifact, args=(current, stage, cancel, text(request['format'], 'format')), daemon=True).start()
        except RuntimeError as exc:
            raise DocumentError('document_export_start_failed', f'Document worker could not start: {exc}', 503) from exc
        launched = True
        return state
    finally:
        if not launched:
            with ACTIVE_GUARD:
                if stage is not None:
                    ACTIVE.pop(stage, None)
            WORKER.release()


def build_artifact(current: Document, stage: Path, cancel: threading.Event, kind: str) -> None:
    started = time.monotonic()
    root = stage.parents[1]
    state: JsonMap = {'build_id': stage.name, 'document_id': current['document_id'], 'revision': current['revision'],
        'format': kind, 'output_role': current['output_role'], 'status': 'building'}
    try:
        result = build_current_hwpx(current, stage, cancel) if kind == 'hwpx' else build_document_backup(current, stage, cancel)
        if cancel.is_set():
            raise DocumentError('document_export_canceled', 'Export canceled; previous artifact retained', 409)
        output = stage / f'output.{kind}'
        receipt: JsonMap = {**state, **result, 'status': 'ready', 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'bytes': output.stat().st_size,
            'download': f"/api/documents/{current['document_id']}/artifacts/{stage.name}/download", 'seconds': time.monotonic() - started}
        with lock_for(root):
            check_revision(load_document(root), current['revision'])
            if cancel.is_set():
                raise DocumentError('document_export_canceled', 'Export canceled', 409)
            write_status(stage, receipt)
            atomic_write_json(root / f"last-{kind}-{current['output_role']}.json", receipt)
    except (DocumentError, HwpxRenderError, OSError, ValueError, subprocess.SubprocessError) as exc:
        write_status(stage, {**state, 'status': 'failed', 'error': getattr(exc, 'code', 'document_export_failed'), 'message': str(exc)})
    finally:
        with ACTIVE_GUARD:
            ACTIVE.pop(stage, None)
        WORKER.release()


def artifact_status(workspace: Path, identifier: str, build: str) -> JsonMap:
    stage = export_root(workspace, identifier, build)
    with lock_for(stage):
        if not (stage / 'status.json').is_file():
            raise DocumentError('document_export_not_found', 'Export was not found', 404)
        state = read_object(stage / 'status.json')
    with ACTIVE_GUARD:
        active = stage in ACTIVE
    if state.get('status') == 'building' and not active:
        return {**state, 'status': 'failed', 'error': 'document_export_interrupted'}
    return state


def cancel_artifact(workspace: Path, identifier: str, build: str) -> JsonMap:
    stage = export_root(workspace, identifier, build)
    with lock_for(document_root(workspace, identifier)):
        with ACTIVE_GUARD:
            cancel = ACTIVE.get(stage)
            if cancel is not None:
                cancel.set()
    return artifact_status(workspace, identifier, build)


def artifact_download(workspace: Path, identifier: str, build: str) -> tuple[Path, str, str]:
    state = artifact_status(workspace, identifier, build)
    if state.get('status') != 'ready' or state.get('format') not in {'hwpx', 'zip'}:
        raise DocumentError('document_export_not_ready', 'Document artifact is not ready', 409)
    kind = text(state['format'], 'format')
    return export_root(workspace, identifier, build) / f'output.{kind}', text(state['sha256'], 'sha256'), kind


def latest_artifacts(workspace: Path, identifier: str) -> JsonMap:
    root = document_root(workspace, identifier)
    receipts: list[JsonMap] = []
    with lock_for(root):
        current = load_document(root)
        for kind in ('hwpx', 'zip'):
            for role in ('question_only', 'explanation_only', 'combined_end_section'):
                path = confined(root, f'last-{kind}-{role}.json')
                if not path.is_file():
                    continue
                receipt = read_object(path)
                if receipt.get('document_id') != identifier or receipt.get('format') != kind or receipt.get('output_role') != role:
                    raise DocumentError('document_export_receipt', 'Stored artifact identity is invalid')
                state = artifact_status(workspace, identifier, text(receipt.get('build_id'), 'build_id'))
                if state.get('status') == 'ready' and state.get('sha256') == receipt.get('sha256'):
                    receipts.append({**receipt, 'current': receipt.get('revision') == current['revision']})
    return {'exports': receipts}
