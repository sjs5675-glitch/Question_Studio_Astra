from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Mapping

from .assets import BlobError
from .authority import human_authority_is_valid
from .contracts import ExampleKind, JsonValue, QualityLevel, RunSnapshot, canonical_json
from .database import PathBoundaryError, validate_source_path
from .profiles import PROFILE_DESCRIPTOR_VERSION
from .queries import QueryError
from .retrieval import MAX_EXAMPLES, MAX_IMAGES, MAX_REFERENCE_CHARS, RetrievalResult
from .store import LearningStore

SNAPSHOT_CONTRACT_VERSION: Final = "solution-learning-snapshot-v1"


class SnapshotError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SnapshotExample:
    example_id: str
    revision: int
    payload_sha: str
    source_sha: str
    kind: ExampleKind


@dataclass(frozen=True, slots=True)
class RetrievalSnapshot:
    snapshot_id: str
    run_id: str
    current_source_sha: str
    corpus_epoch: int
    retriever_version: str
    profile_version: str
    prompt_version: str
    schema_version: str
    profiles: tuple[tuple[str, int], ...]
    examples: tuple[SnapshotExample, ...]
    created_at: str
    snapshot_sha: str


@dataclass(frozen=True, slots=True)
class ApprovalStatus:
    status: str
    reasons: tuple[str, ...]


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotError("snapshot time is invalid") from exc
    if parsed.tzinfo is None:
        raise SnapshotError("snapshot time must include a timezone")
    return parsed


def _version_envelope(
    current_source_sha: str,
    retriever_version: str,
    prompt_version: str,
    schema_version: str,
) -> str:
    return canonical_json({
        "contract": SNAPSHOT_CONTRACT_VERSION,
        "current_source_sha": current_source_sha,
        "retriever": retriever_version,
        "profile": PROFILE_DESCRIPTOR_VERSION,
        "prompt": prompt_version,
        "schema": schema_version,
    })


def _read_versions(value: str) -> Mapping[str, JsonValue]:
    try:
        raw: JsonValue = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SnapshotError("snapshot version envelope is invalid") from exc
    if not isinstance(raw, dict) or raw.get("contract") != SNAPSHOT_CONTRACT_VERSION:
        raise SnapshotError("snapshot contract is unsupported")
    return raw


def _required_string(values: Mapping[str, JsonValue], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise SnapshotError(f"snapshot {key} is invalid")
    return value


def _validate_current_source(store: LearningStore, source_sha: str) -> None:
    try:
        document = store.queries.get_document(source_sha)
        validate_source_path(document.locator, source_sha, store.allowed_source_roots)
    except (OSError, PathBoundaryError, QueryError) as exc:
        raise SnapshotError("current source is missing or stale") from exc


def create_retrieval_snapshot(
    store: LearningStore,
    *,
    snapshot_id: str,
    run_id: str,
    current_source_sha: str,
    retrieval: RetrievalResult,
    profiles: tuple[tuple[str, int], ...],
    prompt_version: str,
    schema_version: str,
    created_at: str,
) -> RetrievalSnapshot:
    _validate_current_source(store, current_source_sha)
    cutoff = _parse_time(created_at)
    if not prompt_version or not schema_version:
        raise SnapshotError("prompt and schema versions are required")
    if len(retrieval.selected) > MAX_EXAMPLES:
        raise SnapshotError("retrieval exceeds the example limit")
    if sum(len(item.image_hashes) for item in retrieval.selected) > MAX_IMAGES:
        raise SnapshotError("retrieval exceeds the image limit")
    if sum(len(item.reference_text) for item in retrieval.selected) > MAX_REFERENCE_CHARS:
        raise SnapshotError("retrieval exceeds the reference text limit")
    examples: list[tuple[str, int, str]] = []
    for selected in retrieval.selected:
        try:
            record = store.get_example(selected.example_id, selected.revision)
            example = record.example
            assertion = store.queries.get_authority_assertion(example.authority_assertion_id or "")
        except QueryError as exc:
            raise SnapshotError("selected example is unavailable") from exc
        authority = store.queries.connection.execute(
            "SELECT assertion_id,state FROM authority_assertions WHERE target_id=? AND target_revision=? "
            "ORDER BY reviewed_at DESC,assertion_id DESC LIMIT 1",
            (example.example_id, example.revision),
        ).fetchone()
        metadata_matches = (
            selected.source_sha == example.source_sha
            and selected.pdf_index == example.pdf_index
            and selected.image_hashes == example.blob_hashes
            and selected.reference_text == example.reviewed_value.get("reference_text")
        )
        eligible = (
            record.source_available
            and example.effective_state == "active"
            and example.quality_level is QualityLevel.HUMAN_CONFIRMED
            and human_authority_is_valid(store.queries.connection, example)
            and authority is not None
            and authority["assertion_id"] == example.authority_assertion_id
            and authority["state"] == "active"
        )
        if not metadata_matches or not eligible:
            raise SnapshotError("selected example is not an eligible retrieval result")
        if _parse_time(assertion.reviewed_at) > cutoff:
            raise SnapshotError("selected example was reviewed after the snapshot cutoff")
        examples.append((example.example_id, example.revision, example.payload_sha))
    stored = store.create_snapshot(RunSnapshot(
        snapshot_id,
        run_id,
        store.corpus_epoch,
        _version_envelope(current_source_sha, retrieval.retriever_version, prompt_version, schema_version),
        profiles,
        tuple(examples),
        created_at,
    ))
    return load_retrieval_snapshot(store, stored.snapshot.snapshot_id)


def load_retrieval_snapshot(store: LearningStore, snapshot_id: str) -> RetrievalSnapshot:
    stored = store.get_snapshot(snapshot_id)
    if store.get_snapshot_hash(stored.snapshot) != stored.snapshot_sha:
        raise SnapshotError("snapshot hash does not match immutable content")
    versions = _read_versions(stored.snapshot.retriever_version)
    examples: list[SnapshotExample] = []
    for example_id, revision, payload_sha in stored.snapshot.examples:
        try:
            example = store.get_example(example_id, revision).example
        except QueryError as exc:
            raise SnapshotError("snapshot example revision is unavailable") from exc
        if example.payload_sha != payload_sha:
            raise SnapshotError("snapshot example hash changed")
        examples.append(SnapshotExample(
            example.example_id, example.revision, example.payload_sha, example.source_sha, example.kind,
        ))
    return RetrievalSnapshot(
        stored.snapshot.snapshot_id,
        stored.snapshot.run_id,
        _required_string(versions, "current_source_sha"),
        stored.snapshot.corpus_epoch,
        _required_string(versions, "retriever"),
        _required_string(versions, "profile"),
        _required_string(versions, "prompt"),
        _required_string(versions, "schema"),
        stored.snapshot.profiles,
        tuple(examples),
        stored.snapshot.created_at,
        stored.snapshot_sha,
    )


def approval_status(store: LearningStore, snapshot: RetrievalSnapshot) -> ApprovalStatus:
    try:
        _validate_current_source(store, snapshot.current_source_sha)
    except SnapshotError:
        return ApprovalStatus("abstain", ("current_source_unavailable",))
    reasons: list[str] = []
    for selected in snapshot.examples:
        record = store.get_example(selected.example_id, selected.revision)
        example = record.example
        latest = store.queries.connection.execute(
            "SELECT effective_state FROM examples WHERE example_id=? ORDER BY revision DESC LIMIT 1",
            (selected.example_id,),
        ).fetchone()
        authority = store.queries.connection.execute(
            "SELECT state FROM authority_assertions WHERE target_id=? AND target_revision=? "
            "ORDER BY reviewed_at DESC,assertion_id DESC LIMIT 1",
            (selected.example_id, selected.revision),
        ).fetchone()
        if latest is None or latest["effective_state"] != "active" or authority is None or authority["state"] != "active":
            reasons.append("learning_reference_retracted")
        elif not record.source_available or example.payload_sha != selected.payload_sha:
            reasons.append("learning_reference_unavailable")
        else:
            try:
                for digest in example.blob_hashes:
                    store.blobs.read(digest)
            except BlobError:
                reasons.append("learning_reference_unavailable")
    for profile_id, revision in snapshot.profiles:
        row = store.queries.connection.execute(
            "SELECT state FROM profiles WHERE profile_id=? AND revision=?", (profile_id, revision),
        ).fetchone()
        if row is None or row["state"] != "active":
            reasons.append("learning_profile_retracted")
    unique = tuple(dict.fromkeys(reasons))
    return ApprovalStatus("review_required" if unique else "eligible", unique)
