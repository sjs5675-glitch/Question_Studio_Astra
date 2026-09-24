from __future__ import annotations

import sqlite3
from typing import Final

SCHEMA_VERSION: Final = 1

SCHEMA_V1: Final = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE documents (
 source_sha TEXT PRIMARY KEY CHECK(length(source_sha)=64), role TEXT NOT NULL,
 family_id TEXT, edition_id TEXT, page_count INTEGER NOT NULL CHECK(page_count>0),
 locator TEXT NOT NULL, dataset_group TEXT NOT NULL
);
CREATE TABLE review_events (
 event_id TEXT PRIMARY KEY, source_event_sha TEXT NOT NULL UNIQUE CHECK(length(source_event_sha)=64),
 operation TEXT NOT NULL, actor_kind TEXT NOT NULL, label_scope TEXT NOT NULL,
 source_revision INTEGER NOT NULL CHECK(source_revision>0), before_sha TEXT NOT NULL CHECK(length(before_sha)=64),
 after_sha TEXT NOT NULL CHECK(length(after_sha)=64), payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 applied_at TEXT NOT NULL, supersedes_event_id TEXT REFERENCES review_events(event_id)
);
CREATE TRIGGER review_events_no_update BEFORE UPDATE ON review_events BEGIN SELECT RAISE(ABORT, 'review_events are append-only'); END;
CREATE TRIGGER review_events_no_delete BEFORE DELETE ON review_events BEGIN SELECT RAISE(ABORT, 'review_events are append-only'); END;
CREATE TABLE authority_assertions (
 assertion_id TEXT PRIMARY KEY, reviewer_id TEXT NOT NULL, review_origin TEXT NOT NULL,
 action_id TEXT NOT NULL, source_event_sha TEXT NOT NULL REFERENCES review_events(source_event_sha),
 source_sha TEXT NOT NULL REFERENCES documents(source_sha), target_id TEXT NOT NULL,
 target_revision INTEGER NOT NULL CHECK(target_revision>0), expected_effect_sha TEXT NOT NULL CHECK(length(expected_effect_sha)=64),
 label_scope TEXT NOT NULL, reviewed_at TEXT NOT NULL, state TEXT NOT NULL
);
CREATE TRIGGER authority_assertions_no_update BEFORE UPDATE ON authority_assertions BEGIN SELECT RAISE(ABORT, 'authority_assertions are append-only'); END;
CREATE TRIGGER authority_assertions_no_delete BEFORE DELETE ON authority_assertions BEGIN SELECT RAISE(ABORT, 'authority_assertions are append-only'); END;
CREATE TABLE document_relations (
 relation_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
 left_source_sha TEXT NOT NULL REFERENCES documents(source_sha), right_source_sha TEXT NOT NULL REFERENCES documents(source_sha),
 relation_kind TEXT NOT NULL, evidence_hashes_json TEXT NOT NULL CHECK(json_valid(evidence_hashes_json)),
 authority_assertion_id TEXT REFERENCES authority_assertions(assertion_id), supersedes_id TEXT,
 state TEXT NOT NULL, PRIMARY KEY(relation_id, revision), CHECK(left_source_sha<>right_source_sha)
);
CREATE TABLE examples (
 example_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0), event_id TEXT NOT NULL REFERENCES review_events(event_id),
 kind TEXT NOT NULL, source_sha TEXT NOT NULL REFERENCES documents(source_sha), pdf_index INTEGER NOT NULL CHECK(pdf_index>=0),
 region_json TEXT CHECK(region_json IS NULL OR json_valid(region_json)), unit_id TEXT, asset_id TEXT,
 reviewed_value_json TEXT NOT NULL CHECK(json_valid(reviewed_value_json)), blob_hashes_json TEXT NOT NULL CHECK(json_valid(blob_hashes_json)),
 quality_level TEXT NOT NULL, payload_sha TEXT NOT NULL CHECK(length(payload_sha)=64), effective_state TEXT NOT NULL,
 authority_assertion_id TEXT REFERENCES authority_assertions(assertion_id), PRIMARY KEY(example_id, revision),
 CHECK(quality_level<>'human_confirmed' OR authority_assertion_id IS NOT NULL)
);
CREATE INDEX examples_source_kind ON examples(source_sha, kind, effective_state, quality_level);
CREATE TABLE profiles (
 profile_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0), region_kind TEXT NOT NULL,
 constrained_params_json TEXT NOT NULL CHECK(json_valid(constrained_params_json)),
 supporting_examples_json TEXT NOT NULL CHECK(json_valid(supporting_examples_json)),
 applicability_json TEXT NOT NULL CHECK(json_valid(applicability_json)), evaluation_hash TEXT NOT NULL CHECK(length(evaluation_hash)=64),
 state TEXT NOT NULL, PRIMARY KEY(profile_id, revision)
);
CREATE TABLE run_snapshots (
 snapshot_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, corpus_epoch INTEGER NOT NULL CHECK(corpus_epoch>=0),
 retriever_version TEXT NOT NULL, profiles_json TEXT NOT NULL CHECK(json_valid(profiles_json)),
 examples_json TEXT NOT NULL CHECK(json_valid(examples_json)), created_at TEXT NOT NULL,
 snapshot_sha TEXT NOT NULL UNIQUE CHECK(length(snapshot_sha)=64)
);
CREATE TABLE example_refs (
 example_id TEXT NOT NULL, example_revision INTEGER NOT NULL, profile_id TEXT, profile_revision INTEGER,
 snapshot_id TEXT REFERENCES run_snapshots(snapshot_id), run_id TEXT,
 FOREIGN KEY(example_id, example_revision) REFERENCES examples(example_id, revision),
 FOREIGN KEY(profile_id, profile_revision) REFERENCES profiles(profile_id, revision),
 CHECK((profile_id IS NULL)=(profile_revision IS NULL)),
 UNIQUE(example_id, example_revision, profile_id, profile_revision, snapshot_id, run_id)
);
CREATE INDEX example_refs_example ON example_refs(example_id, example_revision);
CREATE INDEX example_refs_profile ON example_refs(profile_id, profile_revision);
CREATE INDEX example_refs_snapshot ON example_refs(snapshot_id);
CREATE INDEX example_refs_run ON example_refs(run_id);
CREATE TABLE ingest_checkpoints (
 source_id TEXT PRIMARY KEY, last_event_id TEXT, last_revision INTEGER NOT NULL CHECK(last_revision>=0), retry_state TEXT NOT NULL
);
INSERT INTO meta(key, value) VALUES('corpus_epoch', '0') ON CONFLICT(key) DO NOTHING;
"""


def migrate(connection: sqlite3.Connection, current_version: int) -> None:
    if current_version > SCHEMA_VERSION:
        raise sqlite3.DatabaseError("learning database schema is newer than this runtime")
    if current_version == SCHEMA_VERSION:
        return
    connection.executescript(SCHEMA_V1)
    connection.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
