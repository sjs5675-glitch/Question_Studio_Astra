"""Small, tamper-evident storage for browser-uploaded PDF files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


MAX_PDF_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_UPLOAD_STORAGE_BYTES = 4 * 1024 * 1024 * 1024
MIN_FREE_BYTES_AFTER_UPLOAD = 256 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_METADATA_MAX_BYTES = 16 * 1024
_UPLOAD_ID_RE = re.compile(r"^upload-[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_RESERVED = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE
)
_RESERVATION_LOCK = threading.Lock()
_CLEANUP_LOCK = threading.Lock()
_RESERVED_BYTES: dict[Path, int] = {}


class PdfUploadError(ValueError):
    """Raised when an upload or its persisted record is invalid."""


class PdfUploadTimeout(PdfUploadError):
    """Raised when an upload stops delivering data."""


class PdfUploadNotFound(PdfUploadError):
    """Raised when a syntactically valid upload ID does not exist."""


@dataclass(frozen=True)
class PdfUpload:
    upload_id: str
    original_name: str
    path: Path
    sha256: str
    size: int


def safe_display_filename(value: str) -> str:
    """Return a harmless, PDF-suffixed name for display and downloads."""

    name = re.split(
        r"[\\/]", unicodedata.normalize("NFC", value) if isinstance(value, str) else ""
    )[-1]
    name = "".join(
        "_"
        if unicodedata.category(char).startswith("C") or char in '<>:"/\\|?*'
        else char
        for char in name
    )
    name = re.sub(r"\s+", " ", name).strip(" .") or "upload.pdf"
    if _WINDOWS_RESERVED.fullmatch(name):
        name = f"_{name}"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    if len(name) > 180:
        name = name[:176].rstrip(" .") + ".pdf"
    return name


def store_pdf_upload(
    workspace_root: Path | str,
    stream: BinaryIO,
    *,
    content_length: int,
    original_name: str,
    max_bytes: int = MAX_PDF_UPLOAD_BYTES,
    storage_max_bytes: int = MAX_UPLOAD_STORAGE_BYTES,
    min_free_bytes: int = MIN_FREE_BYTES_AFTER_UPLOAD,
) -> PdfUpload:
    """Stream exactly ``content_length`` bytes into an atomically published upload."""

    if (
        isinstance(content_length, bool)
        or not isinstance(content_length, int)
        or content_length < 5
        or content_length > max_bytes
    ):
        raise PdfUploadError(
            f"Content-Length must be between 5 and {max_bytes} bytes"
        )
    if not hasattr(stream, "read"):
        raise PdfUploadError("upload stream must be binary-readable")

    root, uploads, staging_root = _storage_roots(workspace_root, create=True)
    _reserve_upload(
        root,
        uploads,
        content_length,
        storage_max_bytes=storage_max_bytes,
        min_free_bytes=min_free_bytes,
    )
    upload_id = f"upload-{uuid.uuid4().hex}"
    staging = staging_root / upload_id
    final = uploads / upload_id
    source_path = staging / "source.pdf"
    digest = hashlib.sha256()
    remaining = content_length
    prefix = bytearray()

    try:
        staging.mkdir()
        with source_path.open("xb") as destination:
            while remaining:
                chunk = stream.read(min(_CHUNK_BYTES, remaining))
                if not isinstance(chunk, bytes) or not chunk:
                    raise PdfUploadError("upload body ended before Content-Length")
                if len(chunk) > remaining:
                    raise PdfUploadError("upload stream exceeded Content-Length")
                if len(prefix) < 5:
                    prefix.extend(chunk[: 5 - len(prefix)])
                destination.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            destination.flush()
            os.fsync(destination.fileno())

        if bytes(prefix) != b"%PDF-":
            raise PdfUploadError("uploaded file is not a PDF")

        display_name = safe_display_filename(original_name)
        metadata = {
            "upload_id": upload_id,
            "original_name": display_name,
            "path": "source.pdf",
            "sha256": digest.hexdigest(),
            "size": content_length,
        }
        metadata_path = staging / "metadata.json"
        _write_metadata(metadata_path, metadata)
        _require_plain_path(staging, root=staging_root, kind="directory")
        _require_plain_path(source_path, root=staging, kind="file")
        _require_plain_path(metadata_path, root=staging, kind="file")
        if final.exists() or final.is_symlink():
            raise PdfUploadError("generated upload ID already exists")
        staging.rename(final)
        return PdfUpload(
            upload_id=upload_id,
            original_name=display_name,
            path=(final / "source.pdf").resolve(strict=True),
            sha256=metadata["sha256"],
            size=content_length,
        )
    except PdfUploadError:
        _discard_staging(staging, staging_root)
        raise
    except TimeoutError as exc:
        _discard_staging(staging, staging_root)
        raise PdfUploadTimeout("PDF 전송이 멈춰 업로드를 중단했습니다. 다시 시도해 주세요.") from exc
    except OSError as exc:
        _discard_staging(staging, staging_root)
        raise PdfUploadError("cannot store PDF upload") from exc
    finally:
        _release_upload(root, content_length)


def resolve_pdf_upload(
    workspace_root: Path | str, upload_id: str
) -> PdfUpload:
    """Resolve an upload ID and revalidate its path, size, magic, and SHA-256."""

    if not isinstance(upload_id, str) or not _UPLOAD_ID_RE.fullmatch(upload_id):
        raise PdfUploadError("invalid upload ID")
    root, uploads, _ = _storage_roots(workspace_root, create=False)
    upload_dir = uploads / upload_id
    if not upload_dir.exists():
        raise PdfUploadNotFound(f"upload not found: {upload_id}")
    _require_plain_path(upload_dir, root=root, kind="directory")

    metadata_path = upload_dir / "metadata.json"
    _require_plain_path(metadata_path, root=upload_dir, kind="file")
    try:
        if metadata_path.stat().st_size > _METADATA_MAX_BYTES:
            raise PdfUploadError("upload metadata is too large")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except PdfUploadError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PdfUploadError("cannot read upload metadata") from exc
    if not isinstance(metadata, dict):
        raise PdfUploadError("upload metadata must be an object")

    original_name = metadata.get("original_name")
    size = metadata.get("size")
    sha256 = metadata.get("sha256")
    if metadata.get("upload_id") != upload_id or metadata.get("path") != "source.pdf":
        raise PdfUploadError("upload metadata identity or path is invalid")
    if not isinstance(original_name, str) or safe_display_filename(original_name) != original_name:
        raise PdfUploadError("upload display filename is invalid")
    if isinstance(size, bool) or not isinstance(size, int) or size < 5 or size > MAX_PDF_UPLOAD_BYTES:
        raise PdfUploadError("upload metadata size is invalid")
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise PdfUploadError("upload metadata SHA-256 is invalid")

    source_path = upload_dir / "source.pdf"
    resolved_source = _require_plain_path(source_path, root=upload_dir, kind="file")
    if resolved_source.stat().st_size != size:
        raise PdfUploadError("uploaded PDF size does not match metadata")
    digest = hashlib.sha256()
    with resolved_source.open("rb") as source:
        if source.read(5) != b"%PDF-":
            raise PdfUploadError("stored upload is not a PDF")
        source.seek(0)
        for chunk in iter(lambda: source.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    if digest.hexdigest() != sha256:
        raise PdfUploadError("uploaded PDF SHA-256 does not match metadata")

    return PdfUpload(upload_id, original_name, resolved_source, sha256, size)


def delete_pdf_upload(workspace_root: Path | str, upload_id: str) -> None:
    """Delete one exact published upload after moving it out of the live namespace."""

    if not isinstance(upload_id, str) or not _UPLOAD_ID_RE.fullmatch(upload_id):
        raise PdfUploadError("invalid upload ID")
    _, uploads, staging = _storage_roots(workspace_root, create=True)
    upload_dir = uploads / upload_id
    if not upload_dir.exists():
        return
    _require_plain_path(upload_dir, root=uploads, kind="directory")
    trash = staging / f"delete-{uuid.uuid4().hex}"
    try:
        upload_dir.rename(trash)
        shutil.rmtree(trash)
    except OSError as exc:
        raise PdfUploadError("cannot delete PDF upload") from exc


def cleanup_pdf_uploads(
    workspace_root: Path | str,
    *,
    retained_ids: set[str] | frozenset[str] = frozenset(),
    orphan_age: timedelta = timedelta(hours=24),
) -> None:
    """Remove crash staging and old published uploads not needed for resume."""

    with _CLEANUP_LOCK:
        _cleanup_pdf_uploads_locked(
            workspace_root,
            retained_ids=retained_ids,
            orphan_age=orphan_age,
        )


def _cleanup_pdf_uploads_locked(
    workspace_root: Path | str,
    *,
    retained_ids: set[str] | frozenset[str],
    orphan_age: timedelta,
) -> None:

    root, uploads, staging = _storage_roots(workspace_root, create=True)
    try:
        with _RESERVATION_LOCK:
            if _RESERVED_BYTES.get(root, 0) == 0:
                for child in list(staging.iterdir()):
                    _require_plain_path(child, root=staging, kind="directory")
                    shutil.rmtree(child)
        cutoff = datetime.now(timezone.utc).timestamp() - orphan_age.total_seconds()
        for child in list(uploads.iterdir()):
            if child.name == ".staging" or child.name in retained_ids:
                continue
            _require_plain_path(child, root=uploads, kind="directory")
            if child.stat().st_mtime <= cutoff:
                delete_pdf_upload(workspace_root, child.name)
    except OSError as exc:
        raise PdfUploadError("cannot clean PDF upload storage") from exc


def _storage_roots(
    workspace_root: Path | str, *, create: bool
) -> tuple[Path, Path, Path]:
    raw_root = Path(workspace_root)
    if create:
        raw_root.mkdir(parents=True, exist_ok=True)
    if not raw_root.exists() or raw_root.is_symlink() or not raw_root.is_dir():
        raise PdfUploadError("workspace root must be a real directory")
    root = raw_root.resolve(strict=True)
    uploads = root / ".uploads"
    staging = uploads / ".staging"
    for directory in (uploads, staging):
        if create and not directory.exists():
            directory.mkdir()
        _require_plain_path(directory, root=root, kind="directory")
    return root, uploads, staging


def _reserve_upload(
    root: Path,
    uploads: Path,
    size: int,
    *,
    storage_max_bytes: int,
    min_free_bytes: int,
) -> None:
    if storage_max_bytes < size or min_free_bytes < 0:
        raise PdfUploadError("upload storage limits are invalid")
    with _RESERVATION_LOCK:
        reserved = _RESERVED_BYTES.get(root, 0)
        if _published_bytes(uploads) + reserved + size > storage_max_bytes:
            raise PdfUploadError("PDF upload storage quota would be exceeded")
        if shutil.disk_usage(uploads).free - reserved - size < min_free_bytes:
            raise PdfUploadError("not enough free disk space for PDF upload")
        _RESERVED_BYTES[root] = reserved + size


def _release_upload(root: Path, size: int) -> None:
    with _RESERVATION_LOCK:
        remaining = _RESERVED_BYTES.get(root, 0) - size
        if remaining > 0:
            _RESERVED_BYTES[root] = remaining
        else:
            _RESERVED_BYTES.pop(root, None)


def _published_bytes(uploads: Path) -> int:
    total = 0
    for upload_dir in uploads.iterdir():
        if upload_dir.name == ".staging":
            continue
        _require_plain_path(upload_dir, root=uploads, kind="directory")
        source = _require_plain_path(
            upload_dir / "source.pdf", root=upload_dir, kind="file"
        )
        total += source.stat().st_size
    return total


def _require_plain_path(path: Path, *, root: Path, kind: str) -> Path:
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise PdfUploadError(f"upload {kind} must not be a link")
    if kind == "file" and not path.is_file():
        raise PdfUploadError("required upload file is missing")
    if kind == "directory" and not path.is_dir():
        raise PdfUploadError("required upload directory is missing")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PdfUploadError(f"upload {kind} escapes its storage root") from exc
    return resolved


def _write_metadata(path: Path, metadata: dict[str, str | int]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(metadata, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _discard_staging(path: Path, staging_root: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(staging_root.resolve(strict=True))
    except (OSError, ValueError):
        return
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
