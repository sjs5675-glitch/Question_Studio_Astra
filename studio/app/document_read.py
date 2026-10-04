from __future__ import annotations

from pathlib import Path

from astra_runtime import inside

from .document_storage import confined, document_root, load_document, lock_for, pointer, read_object
from .document_types import DocumentError, JsonMap


def document_list(workspace: Path) -> list[JsonMap]:
    root = confined(inside(workspace, 'workspace_root'), 'documents')
    if not root.exists():
        return []
    result: list[JsonMap] = []
    for entry in root.iterdir():
        if not (entry / 'document.json').is_file():
            continue
        owned = document_root(workspace, entry.name)
        with lock_for(owned):
            document = load_document(owned)
        result.append({'document_id': document['document_id'], 'revision': document['revision'],
            'title': document['layout']['title'], 'updated_at': document['updated_at']})
    return sorted(result, key=lambda item: str(item['updated_at']), reverse=True)


def document_asset(workspace: Path, identifier: str, asset_id: str) -> tuple[Path, str, str]:
    root = document_root(workspace, identifier)
    with lock_for(root):
        document = load_document(root)
        for asset in document['assets']:
            if asset['id'] == asset_id:
                return confined(root, asset['relative_path']), asset['mime_type'], asset['sha256']
    raise DocumentError('document_asset_not_found', 'Document image was not found', 404)


def original_equation(workspace: Path, identifier: str, node_id: str) -> JsonMap:
    root = document_root(workspace, identifier)
    with lock_for(root):
        document = load_document(root)
        node = next((node for node in document['content']['nodes'] if node['id'] == node_id and node['kind'] == 'equation'), None)
        if node is None:
            raise DocumentError('document_equation_not_found', 'Equation was not found', 404)
        origin = node['provenance']
        if origin is None:
            return {'source': None}
        snapshot = next(item for item in document['imports'] if item['snapshot_id'] == origin['snapshot_id'])
        return {'source': pointer(read_object(confined(root, snapshot['relative_path'])), origin['original_pointer'])}
