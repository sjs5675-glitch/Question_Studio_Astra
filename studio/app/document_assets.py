from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from typing import Final
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from .document_types import Asset, DocumentError, JsonMap, mapping, sequence, text

FORMATS: Final = {'PNG': ('image/png', 'png'), 'JPEG': ('image/jpeg', 'jpg'), 'WEBP': ('image/webp', 'webp'), 'GIF': ('image/gif', 'gif')}


def asset_hashes(assets: list[Asset], bindings: dict[str, str]) -> dict[str, str]:
    by_id = {asset['id']: asset['sha256'] for asset in assets}
    return {source_id: by_id[asset_id] for source_id, asset_id in bindings.items()}


def image_identity(data: bytes) -> tuple[str, str, int, int]:
    if not data or len(data) > 20 * 1024 * 1024:
        raise DocumentError('document_asset_limit', 'Image exceeds 20 MiB or is empty')
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in FORMATS or image.width * image.height > 40_000_000:
                raise DocumentError('document_asset_unsupported', 'Unsupported image format or image dimensions')
            mime, extension = FORMATS[image.format]
            width, height = image.size
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise DocumentError('document_asset_corrupt', 'Image cannot be decoded') from exc
    return mime, extension, width, height


def collect_assets(item_root: Path, package: JsonMap) -> tuple[list[Asset], dict[str, bytes], dict[str, str]]:
    from .document_storage import confined

    records: list[Asset] = []
    files: dict[str, bytes] = {}
    identifiers: dict[str, str] = {}
    resources = mapping(package.get('resources', {}), 'resources')
    for raw in sequence(resources.get('assets', []), 'assets'):
        source = mapping(raw, 'asset')
        asset_id = text(source.get('asset_id'), 'asset_id')
        if asset_id in identifiers:
            raise DocumentError('document_asset_corrupt', 'Duplicate source asset ID')
        declared = text(source.get('media_type'), 'media_type')
        if not declared.startswith('image/'):
            continue
        source_path = confined(item_root, text(source.get('path'), 'path'))
        if not source_path.is_file():
            if source.get('state') == 'missing':
                continue
            raise DocumentError('document_asset_corrupt', f'Missing source image {asset_id}')
        if source_path.stat().st_size > 20 * 1024 * 1024:
            raise DocumentError('document_asset_limit', 'Source image exceeds 20 MiB')
        with source_path.open('rb') as stream:
            data = stream.read(20 * 1024 * 1024 + 1)
        mime, extension, width, height = image_identity(data)
        digest = hashlib.sha256(data).hexdigest()
        if declared != mime or source.get('sha256') is not None and source['sha256'] != digest:
            raise DocumentError('document_asset_corrupt', f'Image MIME or hash differs: {asset_id}')
        for key, actual in [('width_px', width), ('height_px', height)]:
            if source.get(key) is not None and source[key] != actual:
                raise DocumentError('document_asset_corrupt', f'Image dimensions differ: {asset_id}')
        identifier = str(uuid4())
        relative = f'assets/{digest}.{extension}'
        records.append({'id': identifier, 'role': text(source.get('role', 'figure_original'), 'role'),
            'section': text(source.get('section', 'question'), 'section'), 'relative_path': relative,
            'mime_type': mime, 'size': len(data), 'sha256': digest, 'width_px': width, 'height_px': height})
        files[relative] = data
        identifiers[asset_id] = identifier
    if sum(len(data) for data in files.values()) > 200 * 1024 * 1024:
        raise DocumentError('document_asset_limit', 'Document images exceed 200 MiB')
    return records, files, identifiers
