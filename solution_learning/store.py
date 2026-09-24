from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Final, Iterator, Self

from .assets import BlobStore
from .contracts import (
    AuthorityAssertion,
    Document,
    DocumentRelation,
    Example,
    ExampleRef,
    IngestCheckpoint,
    LabelScope,
    Profile,
    QualityLevel,
    ReviewEvent,
    RunSnapshot,
    StoredExample,
    StoredSnapshot,
    canonical_json,
    canonical_sha256,
    require_identifier,
    require_sha256,
)
from .database import PathBoundaryError, backup_connection, open_database, validate_source_path
from .authority import human_authority_is_valid, relation_authority_is_valid
from .queries import LearningQueries

BACKUP_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
COUNTABLE_TABLES: Final = frozenset({
    "documents", "review_events", "authority_assertions", "document_relations", "examples",
    "profiles", "run_snapshots", "example_refs", "ingest_checkpoints",
})


class LearningStoreError(RuntimeError): pass


class RecordConflictError(LearningStoreError): pass


class AuthorityError(LearningStoreError): pass


class SnapshotConflictError(LearningStoreError): pass


class LearningStore:
    def __init__(self, root: Path, *, allowed_source_roots: tuple[Path, ...]) -> None:
        if not allowed_source_roots:
            raise PathBoundaryError("at least one source root is required")
        self.allowed_source_roots = tuple(path.resolve(strict=True) for path in allowed_source_roots)
        if any(path.is_symlink() or not path.is_dir() for path in allowed_source_roots):
            raise PathBoundaryError("source roots must be real directories")
        self.root, self.database_path, self._connection = open_database(root)
        self.blobs = BlobStore(self.root)
        self.queries = LearningQueries(self._connection, self.allowed_source_roots)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        row = self._connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row[0])

    @property
    def corpus_epoch(self) -> int:
        row = self._connection.execute("SELECT value FROM meta WHERE key='corpus_epoch'").fetchone()
        return int(row[0])

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def atomic(self) -> Iterator[None]:
        if self._connection.in_transaction:
            raise LearningStoreError("nested learning transactions are unsupported")
        self._connection.execute("BEGIN IMMEDIATE")
        with self._connection:
            yield

    def count(self, table: str) -> int:
        if table not in COUNTABLE_TABLES:
            raise LearningStoreError("unsupported table")
        row = self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0])

    def add_document(self, document: Document) -> None:
        validate_source_path(document.locator, document.source_sha, self.allowed_source_roots)
        if document.page_count < 1 or document.role not in {"problem", "solution", "combined"} or not document.dataset_group:
            raise LearningStoreError("document fields are invalid")
        values = (
            document.source_sha, document.role, document.family_id, document.edition_id,
            document.page_count, str(Path(document.locator).resolve(strict=True)), document.dataset_group,
        )
        existing = self._connection.execute("SELECT * FROM documents WHERE source_sha=?", (document.source_sha,)).fetchone()
        if existing is not None:
            if tuple(existing) != values:
                raise RecordConflictError("document SHA already has different metadata")
            return
        with self._connection:
            self._connection.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?)", values)

    def append_review_event(self, event: ReviewEvent) -> bool:
        require_identifier(event.event_id, "event id")
        require_sha256(event.source_event_sha, "source event sha")
        require_sha256(event.before_sha, "before sha")
        require_sha256(event.after_sha, "after sha")
        payload = canonical_json(event.payload)
        values = (
            event.event_id, event.source_event_sha, event.operation, event.actor_kind.value,
            event.label_scope.value, event.source_revision, event.before_sha, event.after_sha,
            payload, event.applied_at, event.supersedes_event_id,
        )
        existing = self._connection.execute(
            "SELECT * FROM review_events WHERE event_id=? OR source_event_sha=?",
            (event.event_id, event.source_event_sha),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != values:
                raise RecordConflictError("review event identity has conflicting content")
            return False
        with self._write_scope():
            self._connection.execute("INSERT INTO review_events VALUES(?,?,?,?,?,?,?,?,?,?,?)", values)
        return True

    def add_authority_assertion(self, assertion: AuthorityAssertion) -> None:
        require_sha256(assertion.source_event_sha, "authoritative event sha")
        require_sha256(assertion.source_sha, "source sha")
        require_sha256(assertion.expected_effect_sha, "expected effect sha")
        values = (
            assertion.assertion_id, assertion.reviewer_id, assertion.review_origin.value, assertion.action_id,
            assertion.source_event_sha, assertion.source_sha, assertion.target_id, assertion.target_revision,
            assertion.expected_effect_sha, assertion.label_scope.value, assertion.reviewed_at, assertion.state,
        )
        self._insert_immutable("authority_assertions", "assertion_id", assertion.assertion_id, values)

    def add_document_relation(self, relation: DocumentRelation) -> None:
        if relation.state == "reviewed" and relation.authority_assertion_id is None:
            raise AuthorityError("reviewed document relation requires an authority assertion")
        if relation.state == "reviewed" and not relation_authority_is_valid(self._connection, relation):
            raise AuthorityError("reviewed document relation lacks matching user-attested source authority")
        for digest in relation.evidence_hashes:
            require_sha256(digest, "relation evidence sha")
        values = (
            relation.relation_id, relation.revision, relation.left_source_sha, relation.right_source_sha,
            relation.relation_kind, canonical_json(list(relation.evidence_hashes)),
            relation.authority_assertion_id, relation.supersedes_id, relation.state,
        )
        with self._connection:
            self._connection.execute("INSERT INTO document_relations VALUES(?,?,?,?,?,?,?,?,?)", values)
            self._bump_epoch()

    def add_example(self, example: Example) -> None:
        require_sha256(example.source_sha, "source sha")
        require_sha256(example.payload_sha, "payload sha")
        if canonical_sha256(example.reviewed_value) != example.payload_sha:
            raise LearningStoreError("example payload SHA does not match reviewed value")
        if example.region is not None and (
            len(example.region) != 4 or any(value < 0 or value > 1 for value in example.region)
            or example.region[0] >= example.region[2] or example.region[1] >= example.region[3]
        ):
            raise LearningStoreError("example region must be a non-empty ratio rectangle")
        for digest in example.blob_hashes:
            self.blobs.read(require_sha256(digest, "blob sha"))
        if example.quality_level is QualityLevel.HUMAN_CONFIRMED and not human_authority_is_valid(self._connection, example):
            raise AuthorityError("human-confirmed example lacks matching user-attested source authority")
        values = (
            example.example_id, example.revision, example.event_id, example.kind.value, example.source_sha,
            example.pdf_index, canonical_json(list(example.region)) if example.region is not None else None,
            example.unit_id, example.asset_id, canonical_json(example.reviewed_value),
            canonical_json(list(example.blob_hashes)), example.quality_level.value, example.payload_sha,
            example.effective_state, example.authority_assertion_id,
        )
        with self._write_scope():
            self._connection.execute("INSERT INTO examples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
            self._bump_epoch()

    def add_profile(self, profile: Profile) -> None:
        require_sha256(profile.evaluation_hash, "evaluation hash")
        values = (
            profile.profile_id, profile.revision, profile.region_kind, canonical_json(profile.constrained_params),
            canonical_json([list(value) for value in profile.supporting_examples]),
            canonical_json(profile.applicability), profile.evaluation_hash, profile.state,
        )
        with self._connection:
            self._connection.execute("INSERT INTO profiles VALUES(?,?,?,?,?,?,?,?)", values)
            for example_id, revision in profile.supporting_examples:
                self._connection.execute(
                    "INSERT INTO example_refs(example_id,example_revision,profile_id,profile_revision) VALUES(?,?,?,?)",
                    (example_id, revision, profile.profile_id, profile.revision),
                )
            self._bump_epoch()

    @staticmethod
    def get_snapshot_hash(snapshot: RunSnapshot) -> str:
        payload = {
            "snapshot_id": snapshot.snapshot_id, "run_id": snapshot.run_id,
            "corpus_epoch": snapshot.corpus_epoch, "retriever_version": snapshot.retriever_version,
            "profiles": [list(value) for value in snapshot.profiles],
            "examples": [list(value) for value in snapshot.examples], "created_at": snapshot.created_at,
        }
        return canonical_sha256(payload)

    def create_snapshot(self, snapshot: RunSnapshot) -> StoredSnapshot:
        digest = self.get_snapshot_hash(snapshot)
        existing = self._connection.execute("SELECT snapshot_sha FROM run_snapshots WHERE snapshot_id=?", (snapshot.snapshot_id,)).fetchone()
        if existing is not None:
            if existing[0] != digest:
                raise SnapshotConflictError("snapshot id already has different immutable content")
            return StoredSnapshot(snapshot, digest)
        for example_id, revision, payload_sha in snapshot.examples:
            row = self._connection.execute(
                "SELECT payload_sha FROM examples WHERE example_id=? AND revision=?", (example_id, revision),
            ).fetchone()
            if row is None or row[0] != payload_sha:
                raise SnapshotConflictError("snapshot example revision or hash is unavailable")
        for profile_id, revision in snapshot.profiles:
            if self._connection.execute(
                "SELECT 1 FROM profiles WHERE profile_id=? AND revision=?", (profile_id, revision),
            ).fetchone() is None:
                raise SnapshotConflictError("snapshot profile revision is unavailable")
        values = (
            snapshot.snapshot_id, snapshot.run_id, snapshot.corpus_epoch, snapshot.retriever_version,
            canonical_json([list(value) for value in snapshot.profiles]),
            canonical_json([list(value) for value in snapshot.examples]), snapshot.created_at, digest,
        )
        with self._connection:
            self._connection.execute("INSERT INTO run_snapshots VALUES(?,?,?,?,?,?,?,?)", values)
            for example_id, revision, _ in snapshot.examples:
                self._connection.execute(
                    "INSERT INTO example_refs(example_id,example_revision,snapshot_id,run_id) VALUES(?,?,?,?)",
                    (example_id, revision, snapshot.snapshot_id, snapshot.run_id),
                )
        return StoredSnapshot(snapshot, digest)

    def get_example(self, example_id: str, revision: int) -> StoredExample:
        return self.queries.get_example(example_id, revision)

    def list_available_examples(self, *, human_confirmed_only: bool = True) -> tuple[StoredExample, ...]:
        return self.queries.list_available_examples(human_confirmed_only=human_confirmed_only)

    def get_snapshot(self, snapshot_id: str) -> StoredSnapshot:
        return self.queries.get_snapshot(snapshot_id)

    def get_checkpoint(self, source_id: str) -> IngestCheckpoint | None:
        return self.queries.get_checkpoint(source_id)

    def references_for(self, example_id: str, revision: int) -> tuple[ExampleRef, ...]:
        return self.queries.references_for(example_id, revision)

    def save_checkpoint(self, checkpoint: IngestCheckpoint) -> None:
        values = (checkpoint.source_id, checkpoint.last_event_id, checkpoint.last_revision, checkpoint.retry_state)
        with self._connection:
            self._connection.execute(
                "INSERT INTO ingest_checkpoints VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET last_event_id=excluded.last_event_id,last_revision=excluded.last_revision,retry_state=excluded.retry_state",
                values,
            )

    def _bump_epoch(self) -> None:
        self._connection.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='corpus_epoch'")

    def backup(self, name: str) -> Path:
        if BACKUP_NAME.fullmatch(name) is None:
            raise PathBoundaryError("backup name is invalid")
        destination = self.root / "backups" / f"{name}.sqlite3"
        backup_connection(self._connection, destination)
        return destination

    def _insert_immutable(self, table: str, key: str, identity: str, values: tuple[str | int | None, ...]) -> None:
        existing = self._connection.execute(f"SELECT * FROM {table} WHERE {key}=?", (identity,)).fetchone()
        if existing is not None:
            if tuple(existing) != values:
                raise RecordConflictError(f"{table} identity has conflicting content")
            return
        placeholders = ",".join("?" for _ in values)
        with self._write_scope():
            self._connection.execute(f"INSERT INTO {table} VALUES({placeholders})", values)

    @contextmanager
    def _write_scope(self) -> Iterator[None]:
        if self._connection.in_transaction:
            yield
        else:
            with self._connection:
                yield
