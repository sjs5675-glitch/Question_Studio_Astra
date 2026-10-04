"""Reuse unchanged verification inputs only within one synchronous request."""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .contracts import AstraError, JsonMap, digest, json_object

FileStamp = tuple[int, int, int, int, int]


def _stamp(path: Path) -> FileStamp:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@dataclass(frozen=True, slots=True)
class _Reads:
    hashes: dict[Path, tuple[FileStamp, str]] = field(default_factory=dict)
    documents: dict[Path, tuple[FileStamp, JsonMap]] = field(default_factory=dict)


_CURRENT: Final[ContextVar[_Reads | None]] = ContextVar('quality_request_reads', default=None)


@contextmanager
def quality_read_scope() -> Iterator[None]:
    token = _CURRENT.set(_Reads())
    try:
        yield
    finally:
        _CURRENT.reset(token)


def file_sha256(path: Path) -> str:
    cache = _CURRENT.get()
    if cache is None:
        return digest(path.read_bytes())
    resolved = path.resolve(strict=True)
    stamp = _stamp(resolved)
    cached = cache.hashes.get(resolved)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    with resolved.open('rb') as stream:
        value = hashlib.file_digest(stream, 'sha256').hexdigest()
    if _stamp(resolved) != stamp:
        raise AstraError('quality_source_changed_during_read')
    cache.hashes[resolved] = stamp, value
    return value


def read_json(path: Path) -> JsonMap:
    cache = _CURRENT.get()
    if cache is None:
        return json_object(path.read_text(encoding='utf-8'))
    resolved = path.resolve(strict=True)
    stamp = _stamp(resolved)
    cached = cache.documents.get(resolved)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    value = json_object(resolved.read_text(encoding='utf-8'))
    if _stamp(resolved) != stamp:
        raise AstraError('quality_source_changed_during_read')
    cache.documents[resolved] = stamp, value
    return value
