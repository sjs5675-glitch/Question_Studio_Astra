from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Final

from astra_runtime import inside

from .document_types import Document, DocumentError, Json, JsonMap, mapping
from .document_validation import validate_document
from .document_assets import image_identity
from .model import atomic_write_json

MAX_BYTES: Final = 20 * 1024 * 1024
LOCKS: Final[dict[Path, threading.RLock]] = {}
LOCK_GUARD: Final = threading.Lock()


def lock_for(path: Path) -> threading.RLock:
    with LOCK_GUARD:
        return LOCKS.setdefault(path.resolve(), threading.RLock())


def confined(root: Path, relative: str) -> Path:
    if not relative or relative.startswith('/') or '\\' in relative or ':' in relative or any(part in {'', '.', '..'} for part in relative.split('/')):
        raise DocumentError('unsafe_document_path', 'Only contained relative paths are allowed')
    candidate = root
    for part in relative.split('/'):
        candidate = candidate / part
        if candidate.is_symlink() or candidate.is_junction():
            raise DocumentError('unsafe_document_path', 'Linked paths are not allowed')
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise DocumentError('unsafe_document_path', 'Path escapes its owner')
    return candidate


def document_root(workspace: Path, document_id: str) -> Path:
    if not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', document_id):
        raise DocumentError('unsafe_document_id', 'Document ID must be a canonical UUID')
    return confined(inside(workspace, 'workspace_root'), f'documents/{document_id}')


def read_object(path: Path) -> JsonMap:
    if path.stat().st_size > MAX_BYTES:
        raise DocumentError('document_limit', 'JSON exceeds 20 MiB')
    try:
        value: Json = json.loads(path.read_text(encoding='utf-8'))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise DocumentError('invalid_document', 'Invalid stored JSON') from exc
    return mapping(value, str(path))


def encoded(value: Document | JsonMap) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def immutable(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def pointer(snapshot: Json, location: str) -> Json:
    if not location:
        return snapshot
    current = snapshot
    for component in location[1:].split('/'):
        if re.search(r'~(?![01])', component):
            raise DocumentError('invalid_document', 'Invalid source pointer escape')
        key = component.replace('~1', '/').replace('~0', '~')
        match current:  # noqa: MATCH_OK - scalar traversal is malformed external JSON pointer input, reported as 422.
            case dict():
                if key not in current:
                    raise DocumentError('invalid_document', 'Missing source pointer')
                current = current[key]
            case list():
                if not re.fullmatch(r'0|[1-9][0-9]*', key) or int(key) >= len(current):
                    raise DocumentError('invalid_document', 'Missing source array pointer')
                current = current[int(key)]
            case _:
                raise DocumentError('invalid_document', 'Source pointer traverses a scalar')
    return current


def verify_owned(root: Path, document: Document) -> None:
    snapshots: dict[str, JsonMap] = {}
    for item in document['imports']:
        path = confined(root, item['relative_path'])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item['package_sha256']:
            raise DocumentError('document_snapshot_corrupt', f"Missing or changed snapshot {item['snapshot_id']}")
        snapshots[item['snapshot_id']] = read_object(path)
    for asset in document['assets']:
        path = confined(root, asset['relative_path'])
        if not path.is_file() or path.stat().st_size != asset['size'] or hashlib.sha256(path.read_bytes()).hexdigest() != asset['sha256']:
            raise DocumentError('document_asset_corrupt', f"Missing or changed asset {asset['id']}")
        mime, _, width, height = image_identity(path.read_bytes())
        if (mime, width, height) != (asset['mime_type'], asset['width_px'], asset['height_px']):
            raise DocumentError('document_asset_corrupt', 'Asset metadata differs from image bytes')
    for node in document['content']['nodes']:
        origin = node['provenance']
        if origin is not None:
            pointer(snapshots[origin['snapshot_id']], origin['original_pointer'])


def load_document(root: Path) -> Document:
    path = confined(root, 'document.json')
    if not path.is_file():
        raise DocumentError('document_not_found', 'Document was not found', 404)
    document = validate_document(read_object(path))
    if document['document_id'] != root.name:
        raise DocumentError('invalid_document', 'Stored document identity differs from path')
    revision_path = confined(root, f"revisions/{document['revision']}.json")
    if not revision_path.is_file() or revision_path.read_bytes() != path.read_bytes():
        raise DocumentError('document_revision_corrupt', 'Current document differs from its immutable revision')
    verify_owned(root, document)
    return document


def publish_revision(root: Path, document: Document) -> None:
    data = encoded(document)
    if len(data) > MAX_BYTES:
        raise DocumentError('document_limit', 'Document exceeds 20 MiB')
    revision = confined(root, f"revisions/{document['revision']}.json")
    immutable(revision, data)
    try:
        atomic_write_json(confined(root, 'document.json'), document)
    except OSError as exc:
        try:
            revision.unlink()
        except OSError as cleanup_error:
            exc.add_note(f'Unpublished revision cleanup also failed: {cleanup_error}')
        raise
