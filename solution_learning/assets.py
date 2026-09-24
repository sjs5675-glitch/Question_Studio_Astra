from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .contracts import require_sha256


class BlobError(RuntimeError):
    pass


class BlobCorruptError(BlobError):
    pass


class BlobPathError(BlobError):
    pass


@dataclass(frozen=True, slots=True)
class BlobRef:
    sha256: str
    path: Path
    size: int


class BlobStore:
    def __init__(self, learning_root: Path) -> None:
        self.root = learning_root / "blobs" / "sha256"
        self._ensure_safe_directory(self.root)

    @staticmethod
    def _ensure_safe_directory(path: Path) -> None:
        existing = path
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        if existing.is_symlink():
            raise BlobPathError("blob root cannot contain a symlink")
        path.mkdir(parents=True, exist_ok=True)
        current = path
        while current != current.parent:
            if current.is_symlink():
                raise BlobPathError("blob root cannot contain a symlink")
            current = current.parent

    def path_for(self, sha256: str) -> Path:
        digest = require_sha256(sha256, "blob sha256")
        path = self.root / digest[:2] / digest
        if not path.resolve(strict=False).is_relative_to(self.root.resolve(strict=True)):
            raise BlobPathError("blob path escaped the store")
        return path

    def stage(self, content: bytes, expected_sha256: str | None = None) -> BlobRef:
        digest = hashlib.sha256(content).hexdigest()
        if expected_sha256 is not None and require_sha256(expected_sha256, "expected sha256") != digest:
            raise BlobCorruptError("blob does not match expected SHA-256")
        path = self.path_for(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.parent.is_symlink():
            raise BlobPathError("blob path cannot be a symlink")
        if path.exists():
            self.read(digest)
            return BlobRef(digest, path, path.stat().st_size)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".blob-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return BlobRef(digest, path, len(content))

    def read(self, sha256: str) -> bytes:
        path = self.path_for(sha256)
        if path.is_symlink() or not path.is_file():
            raise BlobCorruptError("blob is missing or is not a regular file")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != sha256:
            raise BlobCorruptError("blob SHA-256 changed")
        return content
