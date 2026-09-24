from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .contracts import (
    ActorKind,
    AuthorityAssertion,
    Document,
    DocumentRelation,
    Example,
    ExampleKind,
    ExampleRef,
    IngestCheckpoint,
    LabelScope,
    QualityLevel,
    ReviewEvent,
    ReviewOrigin,
    RunSnapshot,
    StoredExample,
    StoredSnapshot,
)
from .database import PathBoundaryError, validate_source_path


class QueryError(RuntimeError):
    pass


class LearningQueries:
    def __init__(self, connection: sqlite3.Connection, allowed_source_roots: tuple[Path, ...]) -> None:
        self.connection = connection
        self.allowed_source_roots = allowed_source_roots

    def get_document(self, source_sha: str) -> Document:
        row = self.connection.execute("SELECT * FROM documents WHERE source_sha=?", (source_sha,)).fetchone()
        if row is None:
            raise QueryError("document was not found")
        return Document(*tuple(row))

    def get_review_event(self, source_event_sha: str) -> ReviewEvent:
        row = self.connection.execute("SELECT * FROM review_events WHERE source_event_sha=?", (source_event_sha,)).fetchone()
        if row is None:
            raise QueryError("review event was not found")
        return ReviewEvent(row["event_id"], row["source_event_sha"], row["operation"], ActorKind(row["actor_kind"]),
                           LabelScope(row["label_scope"]), row["source_revision"], row["before_sha"], row["after_sha"],
                           json.loads(row["payload_json"]), row["applied_at"], row["supersedes_event_id"])

    def get_authority_assertion(self, assertion_id: str) -> AuthorityAssertion:
        row = self.connection.execute("SELECT * FROM authority_assertions WHERE assertion_id=?", (assertion_id,)).fetchone()
        if row is None:
            raise QueryError("authority assertion was not found")
        return AuthorityAssertion(row["assertion_id"], row["reviewer_id"], ReviewOrigin(row["review_origin"]),
                                  row["action_id"], row["source_event_sha"], row["source_sha"], row["target_id"],
                                  row["target_revision"], row["expected_effect_sha"], LabelScope(row["label_scope"]),
                                  row["reviewed_at"], row["state"])

    def document_relations(self, source_sha: str) -> tuple[DocumentRelation, ...]:
        rows = self.connection.execute(
            "SELECT * FROM document_relations WHERE left_source_sha=? OR right_source_sha=? ORDER BY relation_id,revision",
            (source_sha, source_sha),
        ).fetchall()
        return tuple(DocumentRelation(row["relation_id"], row["revision"], row["left_source_sha"],
                                      row["right_source_sha"], row["relation_kind"], tuple(json.loads(row["evidence_hashes_json"])),
                                      row["authority_assertion_id"], row["supersedes_id"], row["state"]) for row in rows)

    def get_example(self, example_id: str, revision: int) -> StoredExample:
        row = self.connection.execute(
            "SELECT e.*, d.locator FROM examples e JOIN documents d ON d.source_sha=e.source_sha WHERE e.example_id=? AND e.revision=?",
            (example_id, revision),
        ).fetchone()
        if row is None:
            raise QueryError("example revision was not found")
        example = self._example_from_row(row)
        try:
            validate_source_path(row["locator"], example.source_sha, self.allowed_source_roots)
        except (OSError, PathBoundaryError):
            return StoredExample(example, False, "source_unavailable")
        return StoredExample(example, True, None)

    def list_available_examples(self, *, human_confirmed_only: bool) -> tuple[StoredExample, ...]:
        rows = self.connection.execute(
            "SELECT e.example_id,e.revision FROM examples e JOIN (SELECT example_id,MAX(revision) revision FROM examples GROUP BY example_id) latest USING(example_id,revision) WHERE e.effective_state='active' ORDER BY e.example_id"
        ).fetchall()
        records = tuple(self.get_example(row["example_id"], row["revision"]) for row in rows)
        if human_confirmed_only:
            records = tuple(record for record in records if record.example.quality_level is QualityLevel.HUMAN_CONFIRMED)
        return tuple(record for record in records if record.source_available)

    def get_snapshot(self, snapshot_id: str) -> StoredSnapshot:
        row = self.connection.execute("SELECT * FROM run_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise QueryError("snapshot was not found")
        profiles = tuple((str(value[0]), int(value[1])) for value in json.loads(row["profiles_json"]))
        examples = tuple((str(value[0]), int(value[1]), str(value[2])) for value in json.loads(row["examples_json"]))
        snapshot = RunSnapshot(
            row["snapshot_id"], row["run_id"], row["corpus_epoch"], row["retriever_version"],
            profiles, examples, row["created_at"],
        )
        return StoredSnapshot(snapshot, row["snapshot_sha"])

    def get_checkpoint(self, source_id: str) -> IngestCheckpoint | None:
        row = self.connection.execute("SELECT * FROM ingest_checkpoints WHERE source_id=?", (source_id,)).fetchone()
        return IngestCheckpoint(*tuple(row)) if row is not None else None

    def references_for(self, example_id: str, revision: int) -> tuple[ExampleRef, ...]:
        rows = self.connection.execute(
            "SELECT * FROM example_refs WHERE example_id=? AND example_revision=? ORDER BY profile_id,snapshot_id,run_id",
            (example_id, revision),
        ).fetchall()
        return tuple(ExampleRef(*tuple(row)) for row in rows)

    @staticmethod
    def _example_from_row(row: sqlite3.Row) -> Example:
        region_value = json.loads(row["region_json"]) if row["region_json"] is not None else None
        region = tuple(float(value) for value in region_value) if region_value is not None else None
        return Example(
            row["example_id"], row["revision"], row["event_id"], ExampleKind(row["kind"]), row["source_sha"],
            row["pdf_index"], region, row["unit_id"], row["asset_id"], json.loads(row["reviewed_value_json"]),
            tuple(json.loads(row["blob_hashes_json"])), QualityLevel(row["quality_level"]), row["payload_sha"],
            row["effective_state"], row["authority_assertion_id"],
        )
