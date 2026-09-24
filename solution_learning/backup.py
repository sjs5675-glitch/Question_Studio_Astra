from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .assets import BlobError, BlobStore
from .contracts import ContractError, canonical_json, require_sha256
from .database import PathBoundaryError, backup_connection
from .store import LearningStore

FORMAT: Final = "solution-learning-backup"
FORMAT_VERSION: Final = 1


class BackupError(RuntimeError):
    """Raised when a backup package cannot be safely created or restored."""


@dataclass(frozen=True, slots=True)
class BackupPackage:
    root: Path
    database_path: Path
    manifest_path: Path


def create_backup(store: LearningStore, destination: Path) -> BackupPackage:
    """Create a self-contained backup package from the store's SQLite snapshot."""
    destination = _new_destination(destination)
    staging = Path(tempfile.mkdtemp(prefix=".solution-learning-backup-", dir=destination.parent))
    try:
        database = staging / "library.sqlite3"
        backup_connection(store._connection, database)
        blobs = _manifest_for(database, store.blobs)
        snapshot_blobs = BlobStore(staging)
        for entry in blobs["blobs"]:
            digest = require_sha256(str(entry["sha256"]), "blob sha256")
            snapshot_blobs.stage(store.blobs.read(digest), digest)
        manifest = staging / "manifest.json"
        manifest.write_text(canonical_json(_manifest_for(database, snapshot_blobs)), encoding="utf-8")
        os.replace(staging, destination)
        return BackupPackage(destination, destination / "library.sqlite3", destination / "manifest.json")
    except (BlobError, ContractError, OSError, sqlite3.Error, ValueError) as exc:
        raise BackupError("backup package could not be created") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def restore_backup(package_root: Path, destination: Path) -> BackupPackage:
    """Restore a verified package only into a previously nonexistent destination."""
    package = _safe_existing_directory(package_root, "backup package")
    destination = _new_destination(destination)
    try:
        manifest = _read_manifest(package / "manifest.json")
        database = _safe_regular_file(package / "library.sqlite3", package, "backup database")
        expected = _manifest_for(database, BlobStore(package))
        if canonical_json(manifest) != canonical_json(expected):
            raise BackupError("backup manifest does not match database or blobs")
    except (BlobError, ContractError, OSError, sqlite3.Error, ValueError) as exc:
        raise BackupError("backup package is invalid") from exc

    staging = Path(tempfile.mkdtemp(prefix=".solution-learning-restore-", dir=destination.parent))
    try:
        shutil.copyfile(package / "library.sqlite3", staging / "library.sqlite3")
        shutil.copyfile(package / "manifest.json", staging / "manifest.json")
        source_blobs = BlobStore(package)
        target_blobs = BlobStore(staging)
        for entry in expected["blobs"]:
            digest = require_sha256(str(entry["sha256"]), "blob sha256")
            content = source_blobs.read(digest)
            target_blobs.stage(content, digest)
        if canonical_json(_manifest_for(staging / "library.sqlite3", target_blobs)) != canonical_json(expected):
            raise BackupError("restored package verification failed")
        os.replace(staging, destination)
        return BackupPackage(destination, destination / "library.sqlite3", destination / "manifest.json")
    except (BackupError, BlobError, ContractError, OSError, sqlite3.Error, ValueError) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError("backup package could not be restored") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _manifest_for(database: Path, blobs: BlobStore) -> dict[str, int | str | list[dict[str, int | str]]]:
    schema_version, corpus_epoch, blob_hashes = _database_metadata(database)
    entries = []
    for digest in blob_hashes:
        content = blobs.read(digest)
        entries.append({"sha256": digest, "size": len(content)})
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "schema_version": schema_version,
        "corpus_epoch": corpus_epoch,
        "database_sha256": _file_sha256(database),
        "blobs": entries,
    }


def _database_metadata(database: Path) -> tuple[int, int, tuple[str, ...]]:
    database = _safe_regular_file(database, database.parent, "backup database")
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        rows = connection.execute("SELECT key,value FROM meta WHERE key IN ('schema_version','corpus_epoch')").fetchall()
        metadata = {str(key): str(value) for key, value in rows}
        schema_version = int(metadata["schema_version"])
        corpus_epoch = int(metadata["corpus_epoch"])
        if schema_version < 1 or corpus_epoch < 0:
            raise BackupError("backup database metadata is invalid")
        blob_hashes: set[str] = set()
        for (payload,) in connection.execute("SELECT blob_hashes_json FROM examples ORDER BY example_id,revision"):
            parsed = json.loads(str(payload))
            if not isinstance(parsed, list) or not all(isinstance(value, str) for value in parsed):
                raise BackupError("backup database has invalid blob references")
            blob_hashes.update(require_sha256(value, "blob sha256") for value in parsed)
    return schema_version, corpus_epoch, tuple(sorted(blob_hashes))


def _read_manifest(path: Path) -> dict[str, int | str | list[dict[str, int | str]]]:
    path = _safe_regular_file(path, path.parent, "backup manifest")
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise BackupError("backup manifest is not an object")
    return parsed


def _new_destination(path: Path) -> Path:
    candidate = path.absolute()
    if candidate.exists() or _is_link(candidate):
        raise PathBoundaryError("backup destination must not already exist")
    parent = _safe_existing_directory(candidate.parent, "backup destination parent")
    return parent / candidate.name


def _safe_existing_directory(path: Path, label: str) -> Path:
    candidate = path.absolute()
    if _is_link(candidate) or not candidate.is_dir():
        raise PathBoundaryError(f"{label} must be a real directory")
    current = candidate
    while current != current.parent:
        if _is_link(current):
            raise PathBoundaryError(f"{label} cannot traverse a symlink")
        current = current.parent
    for directory, names, files in os.walk(candidate, followlinks=False):
        directory_path = Path(directory)
        if _is_link(directory_path) or any(_is_link(directory_path / name) for name in names + files):
            raise PathBoundaryError(f"{label} cannot contain a symlink")
    return candidate.resolve(strict=True)


def _safe_regular_file(path: Path, root: Path, label: str) -> Path:
    if _is_link(path) or not path.is_file() or not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        raise PathBoundaryError(f"{label} must be a regular file inside its package")
    return path.resolve(strict=True)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
