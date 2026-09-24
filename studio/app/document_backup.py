from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

from .document_storage import MAX_BYTES, confined, encoded, immutable
from .document_types import Document, DocumentError, JsonMap, positive
from .document_backup_receipts import project_receipt
from .project_archive import _InputFile, _cached_archive_is_valid, _write_archive


def freeze_receipts(root: Path, stage: Path, current: Document) -> None:
    receipts: list[JsonMap] = []
    for kind in ('pdf', 'hwpx', 'zip'):
        for role in ('question_only', 'explanation_only', 'combined_end_section'):
            path = confined(root, f'last-{kind}-{role}.json')
            if not path.is_file():
                continue
            raw = path.read_bytes()
            if len(raw) > MAX_BYTES:
                raise DocumentError('document_backup_receipt', 'Export receipt exceeds 20 MiB')
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise DocumentError('document_backup_receipt', 'Stored export receipt is not an object')
            revision = positive(value.get('revision'), 'receipt revision')
            digest = value.get('pdfSha256') if kind == 'pdf' else value.get('sha256')
            if value.get('document_id') != current['document_id'] or value.get('output_role') != role or value.get('format', kind) != kind or revision > current['revision']:
                raise DocumentError('document_backup_receipt', 'Stored export receipt identity conflicts with the saved document')
            if not isinstance(value.get('build_id'), str) or not re.fullmatch('[a-f0-9]{32}', value['build_id']) or not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
                raise DocumentError('document_backup_receipt', 'Stored export receipt has an invalid build or artifact hash')
            summary = project_receipt(value)
            summary['format'] = kind
            receipts.append({'source': path.name, 'source_sha256': hashlib.sha256(raw).hexdigest(), 'identity': summary,
                'matches_saved_revision': revision == current['revision'], 'asset_path_base': 'original_export_build_not_zip_root'})
    immutable(stage / 'backup-receipt.json', encoded({'document_id': current['document_id'], 'revision': current['revision'],
        'scope': 'full_saved_document_including_answers_and_imports', 'export_receipts': receipts,
        'receipt_policy': 'Allowlisted output/math provenance and completeness; authentication, capabilities, runtime paths and logs are excluded. Older revision receipts remain explicitly identified.'}))


def build_document_backup(current: Document, stage: Path, cancel: threading.Event) -> JsonMap:
    paths = ['document.json', 'backup-receipt.json', *[item['relative_path'] for item in current['imports']], *[item['relative_path'] for item in current['assets']]]
    files: list[_InputFile] = []
    for relative in sorted(set(paths)):
        if cancel.is_set():
            raise DocumentError('document_export_canceled', 'Backup canceled; previous artifact retained', 409)
        path = confined(stage, relative)
        raw = path.read_bytes()
        files.append(_InputFile(relative, path, len(raw), hashlib.sha256(raw).hexdigest()))
    manifest: JsonMap = {'schema': 'document-backup-v1', 'document_id': current['document_id'], 'revision': current['revision'],
        'scope': 'full_saved_document_including_answers_and_imports',
        'files': [{'path': item.member, 'bytes': item.size, 'sha256': item.sha256} for item in files]}
    manifest_bytes = encoded(manifest)
    destination = stage / 'output.zip'
    _write_archive(destination, manifest_bytes, files, stage)
    if not _cached_archive_is_valid(destination, manifest_bytes, files):
        raise DocumentError('document_backup_invalid', 'Document backup failed integrity verification')
    return {'format': 'zip', 'scope': manifest['scope'], 'files': manifest['files'], 'document_sha256': hashlib.sha256((stage / 'document.json').read_bytes()).hexdigest()}
