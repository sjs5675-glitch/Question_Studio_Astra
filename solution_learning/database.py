from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

from .contracts import require_sha256
from .migrations import SCHEMA_VERSION, migrate


class PathBoundaryError(RuntimeError):
    pass


def safe_store_root(path: Path) -> Path:
    candidate = path.absolute()
    existing = candidate
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if existing.is_symlink():
        raise PathBoundaryError("learning root cannot contain a symlink")
    candidate.mkdir(parents=True, exist_ok=True)
    if candidate.is_symlink():
        raise PathBoundaryError("learning root cannot be a symlink")
    return candidate.resolve(strict=True)


def backup_connection(connection: sqlite3.Connection, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or destination.parent.is_symlink():
        raise PathBoundaryError("backup path cannot be a symlink")
    with closing(sqlite3.connect(destination)) as target:
        with target:
            connection.backup(target)


def open_database(root: Path) -> tuple[Path, Path, sqlite3.Connection]:
    safe_root = safe_store_root(root)
    database_path = safe_root / "library.sqlite3"
    if database_path.is_symlink():
        raise PathBoundaryError("learning database cannot be a symlink")
    existed = database_path.exists()
    connection = sqlite3.connect(database_path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    current = int(row[0]) if row is not None else 0
    if existed and current < SCHEMA_VERSION:
        backup_connection(connection, safe_root / "backups" / f"pre-migration-v{current}.sqlite3")
    with connection:
        migrate(connection, current)
    return safe_root, database_path, connection


def validate_source_path(locator: str, source_sha: str, allowed_roots: tuple[Path, ...]) -> Path:
    candidate = Path(locator)
    if not candidate.is_absolute() or candidate.is_symlink():
        raise PathBoundaryError("source locator must be an absolute regular path")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file() or not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise PathBoundaryError("source locator is outside allowed roots")
    current = candidate
    while current != current.parent:
        if current.is_symlink():
            raise PathBoundaryError("source locator cannot traverse a symlink")
        current = current.parent
    if hashlib.sha256(resolved.read_bytes()).hexdigest() != require_sha256(source_sha, "source sha"):
        raise PathBoundaryError("source locator does not match source SHA-256")
    return resolved
