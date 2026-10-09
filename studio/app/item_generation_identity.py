"""Content identities and whitelisted image reads shared by generation and reviews."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .item_generation import ItemGenerationError


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def safe_path(base: Path, relative: str) -> Path:
    base = Path(base).absolute()
    part = Path(relative)
    if not relative or part.is_absolute() or part.drive or '..' in part.parts \
            or any(':' in p or p.endswith((' ', '.')) or any(ord(c) < 32 for c in p) for p in part.parts):
        raise ItemGenerationError('asset_path_invalid')
    path = base / part
    # Test BEFORE resolve: resolving first hides Windows junctions and symlinks.
    for component in [path, *path.parents]:
        if component.is_symlink() or (hasattr(component, 'is_junction') and component.is_junction()):
            raise ItemGenerationError('asset_link_rejected')
    resolved = path.resolve()
    if base.resolve() not in resolved.parents:
        raise ItemGenerationError('asset_path_escape')
    return path


def image_bytes(base: Path, relative: str, expected: str | None, *, allow_legacy: bool = False) -> tuple[bytes, str]:
    path = safe_path(base, relative)
    if not path.is_file():
        raise ItemGenerationError('asset_missing')
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if (expected is not None and sha != expected) or (expected is None and not allow_legacy):
        raise ItemGenerationError('asset_sha_mismatch')
    from io import BytesIO
    from PIL import Image
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in {'PNG', 'JPEG', 'WEBP'}:
                raise ValueError('unsupported')
            image.verify()
    except Exception as exc:
        raise ItemGenerationError('asset_invalid_image') from exc
    return data, sha


def draft_identity(draft: Mapping[str, Any], base: Path, *, allow_legacy: bool = False) -> dict[str, Any]:
    question = {k: draft.get(k) for k in ('stem', 'instruction', 'table', 'bogi', 'choices', 'format')}
    assets = []
    figure = draft.get('figure')
    if isinstance(figure, Mapping):
        question['figure'] = {k: figure.get(k) for k in ('method', 'spec', 'alt_text')}
        if figure.get('file'):
            _, sha = image_bytes(base, figure['file'], figure.get('sha256'), allow_legacy=allow_legacy)
            assets.append(sha)
    return {'content_sha256': digest(question), 'answer_sha256': digest(draft.get('answer')),
            'assets_sha256': digest(assets), 'asset_shas': assets,
            'source_revision': int(draft.get('content_revision', 0))}


def receipt_model(receipt: Mapping[str, Any]) -> str | None:
    """Resolve identity without presenting a CLI selection as provider metadata."""
    if receipt.get('model_evidence') == 'cli_startup_receipt':
        return receipt.get('cli_selected_model')
    return receipt.get('actual_model')


def model_family(model: Any) -> str | None:
    model = str(model or '').lower()
    if model.startswith('claude-'):
        return 'claude'
    if model.startswith('gpt-') or model.startswith('codex'):
        return 'codex'
    return None


def contributors(draft: Mapping[str, Any]) -> list[str]:
    if draft.get('model_confirmed') is False:
        return ['unknown']
    values = list(draft.get('contributors') or [])
    if not values:
        values = [draft.get('generator_model') or 'unknown']
        values += [(h.get('previous') or {}).get('generator_model') or 'unknown'
                   for h in draft.get('history') or []]
    if draft.get('edited_by_user') and 'human' not in values:
        values.append('human')
    return sorted(set(str(v) for v in values))


def independence(authors: list[str], actual: str | None, *, confirmed: bool = True) -> str:
    family = model_family(actual) if confirmed else None
    families = {model_family(m) for m in authors if m != 'human'}
    if family is None or not families or None in families or 'unknown' in authors:
        return 'unknown'
    if len(families) > 1:
        return 'mixed'
    return 'self_check' if family in families else 'cross_check'


@contextmanager
def image_sandbox(parent: Path, images: list[bytes]):
    """No answers, job JSON, canonical files, names or previous verdicts in this cwd."""
    parent.mkdir(parents=True, exist_ok=True)
    folder = parent / ('blind-' + uuid.uuid4().hex)
    os.mkdir(folder)
    paths = []
    try:
        for index, data in enumerate(images, 1):
            path = folder / f'image-{index:02d}.png'
            path.write_bytes(data)
            path.chmod(0o444)
            paths.append(path)
        yield folder, paths
        if any(p.read_bytes() != data for p, data in zip(paths, images)):
            raise ItemGenerationError('review_snapshot_mutated')
    finally:
        for path in paths:
            if path.exists():
                path.chmod(0o666)
        shutil.rmtree(folder, ignore_errors=True)
