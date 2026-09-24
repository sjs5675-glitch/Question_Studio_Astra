from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from .authority import human_authority_is_valid, relation_authority_is_valid
from .contracts import Example, ExampleKind, JsonValue, QualityLevel, StoredExample
from .database import PathBoundaryError, validate_source_path
from .profiles import FormatFeatures
from .queries import QueryError
from .store import LearningStore

RETRIEVER_VERSION: Final = "solution-example-retriever-v1"
MAX_EXAMPLES: Final = 3
MAX_IMAGES: Final = 2
MAX_REFERENCE_CHARS: Final = 4_000
BLOCKED_DATASET_REASONS: Final = {
    "eval": "evaluation_data",
    "test_fixture": "test_fixture_data",
    "legacy": "legacy_data",
}
SOURCE_SPECIFIC_KINDS: Final = frozenset({
    ExampleKind.CORRECT_PAIR, ExampleKind.REJECTED_PAIR, ExampleKind.SOURCE_NUMBER_ALIAS,
})


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    source_sha: str
    kind: ExampleKind
    features: FormatFeatures
    current_case_id: str | None = None


@dataclass(frozen=True, slots=True)
class SelectedExample:
    example_id: str
    revision: int
    source_sha: str
    pdf_index: int
    tier: str
    matched_feature_count: int
    reference_text: str
    image_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExcludedExample:
    example_id: str
    revision: int
    reason: str


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    selected: tuple[SelectedExample, ...]
    excluded: tuple[ExcludedExample, ...]
    mode: str
    reason: str | None
    retriever_version: str = RETRIEVER_VERSION


@dataclass(frozen=True, slots=True)
class _Candidate:
    example: Example
    tier_rank: int
    tier: str
    matched_feature_count: int
    reference_text: str
    case_key: str
    reviewed_at: datetime


def _fts5_available(connection: sqlite3.Connection) -> bool:
    try:
        row = connection.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()
    except sqlite3.DatabaseError:
        return False
    return row is not None and row[0] == 1


def _string(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) and value else None


def _features(value: Mapping[str, JsonValue]) -> FormatFeatures | None:
    raw = value.get("format")
    if not isinstance(raw, Mapping):
        return None
    page_role = _string(raw.get("page_role"))
    numbering_kind = _string(raw.get("numbering_kind"))
    answer_layout = _string(raw.get("answer_layout"))
    column_raw = raw.get("column_count")
    column_count = column_raw if isinstance(column_raw, int) and not isinstance(column_raw, bool) else None
    return FormatFeatures(page_role, column_count, numbering_kind, answer_layout)


def _matches(expected: FormatFeatures, actual: FormatFeatures, kind: ExampleKind) -> bool:
    common = (
        expected.page_role == actual.page_role
        and expected.column_count == actual.column_count
        and expected.numbering_kind == actual.numbering_kind
    )
    return common and (kind is not ExampleKind.ANSWER_CELL or expected.answer_layout == actual.answer_layout)


def _relation_tiers(store: LearningStore, source_sha: str) -> dict[str, tuple[int, str]]:
    latest = {}
    for relation in store.queries.document_relations(source_sha):
        previous = latest.get(relation.relation_id)
        if previous is None or relation.revision > previous.revision:
            latest[relation.relation_id] = relation
    tiers: dict[str, tuple[int, str]] = {}
    for relation in latest.values():
        if relation.state != "reviewed" or not relation_authority_is_valid(store.queries.connection, relation):
            continue
        other = relation.right_source_sha if relation.left_source_sha == source_sha else relation.left_source_sha
        tier = {
            "same_edition": (1, "reviewed_same_edition"),
            "same_family": (2, "reviewed_same_family"),
        }.get(relation.relation_kind)
        if tier is None:
            continue
        if relation.relation_kind == "same_edition":
            tiers[other] = tier
        else:
            tiers.setdefault(other, tier)
    return tiers


def _latest_examples(store: LearningStore) -> tuple[StoredExample, ...]:
    rows = store.queries.connection.execute(
        "SELECT e.example_id,e.revision FROM examples e JOIN "
        "(SELECT example_id,MAX(revision) revision FROM examples GROUP BY example_id) latest "
        "USING(example_id,revision) ORDER BY e.example_id"
    ).fetchall()
    return tuple(store.get_example(row["example_id"], row["revision"]) for row in rows)


def _is_latest_active_authority(store: LearningStore, example: Example) -> bool:
    row = store.queries.connection.execute(
        "SELECT assertion_id,state FROM authority_assertions WHERE target_id=? AND target_revision=? "
        "ORDER BY reviewed_at DESC,assertion_id DESC LIMIT 1",
        (example.example_id, example.revision),
    ).fetchone()
    return row is not None and row["assertion_id"] == example.authority_assertion_id and row["state"] == "active"


def _conflicted_cases(store: LearningStore, records: tuple[StoredExample, ...]) -> frozenset[tuple[str, str]]:
    labels: dict[tuple[str, str], set[ExampleKind]] = {}
    for record in records:
        example = record.example
        if example.effective_state != "active" or not record.source_available:
            continue
        if not human_authority_is_valid(store.queries.connection, example):
            continue
        case_key = _string(example.reviewed_value.get("case_key"))
        if case_key is not None and example.kind in {ExampleKind.CORRECT_PAIR, ExampleKind.REJECTED_PAIR}:
            labels.setdefault((example.source_sha, case_key), set()).add(example.kind)
    return frozenset(key for key, kinds in labels.items() if len(kinds) > 1)


def retrieve_examples(store: LearningStore, request: RetrievalRequest) -> RetrievalResult:
    if not request.features.is_complete_for(request.kind):
        return RetrievalResult((), (), "normal_analysis", "unknown_format")
    if not _fts5_available(store.queries.connection):
        return RetrievalResult((), (), "normal_analysis", "fts5_unavailable")
    try:
        current_document = store.queries.get_document(request.source_sha)
        validate_source_path(current_document.locator, request.source_sha, store.allowed_source_roots)
    except (OSError, PathBoundaryError, QueryError):
        return RetrievalResult((), (), "normal_analysis", "current_source_unavailable")
    relations = _relation_tiers(store, request.source_sha)
    records = _latest_examples(store)
    conflicts = _conflicted_cases(store, records)
    excluded: list[ExcludedExample] = []
    candidates: list[_Candidate] = []
    for record in records:
        example = record.example
        reason, candidate = _qualify(store, request, record, relations, conflicts, current_document.role)
        if candidate is None:
            excluded.append(ExcludedExample(example.example_id, example.revision, reason))
        else:
            candidates.append(candidate)
    candidates.sort(key=lambda item: item.example.example_id)
    candidates.sort(key=lambda item: item.reviewed_at, reverse=True)
    candidates.sort(key=lambda item: item.matched_feature_count, reverse=True)
    candidates.sort(key=lambda item: item.tier_rank)
    selected: list[SelectedExample] = []
    dedup: set[str] = set()
    chars = images = 0
    for candidate in candidates:
        example = candidate.example
        key = f"image:{example.blob_hashes[0]}" if example.blob_hashes else f"payload:{example.payload_sha}"
        reason = None
        if key in dedup:
            reason = "duplicate"
        elif len(selected) >= MAX_EXAMPLES:
            reason = "limit"
        elif chars + len(candidate.reference_text) > MAX_REFERENCE_CHARS:
            reason = "text_budget"
        elif images + len(example.blob_hashes) > MAX_IMAGES:
            reason = "image_budget"
        if reason is not None:
            excluded.append(ExcludedExample(example.example_id, example.revision, reason))
            continue
        dedup.add(key)
        chars += len(candidate.reference_text)
        images += len(example.blob_hashes)
        selected.append(SelectedExample(
            example.example_id, example.revision, example.source_sha, example.pdf_index,
            candidate.tier, candidate.matched_feature_count, candidate.reference_text,
            example.blob_hashes,
        ))
    mode = "references" if selected else "normal_analysis"
    return RetrievalResult(tuple(selected), tuple(excluded), mode, None if selected else "no_eligible_examples")


def _qualify(
    store: LearningStore,
    request: RetrievalRequest,
    record: StoredExample,
    relations: Mapping[str, tuple[int, str]],
    conflicts: frozenset[tuple[str, str]],
    current_role: str,
) -> tuple[str, _Candidate | None]:
    example = record.example
    if example.effective_state != "active":
        return example.effective_state, None
    if not record.source_available:
        return "source_unavailable", None
    if example.quality_level is not QualityLevel.HUMAN_CONFIRMED or not human_authority_is_valid(store.queries.connection, example):
        return "untrusted_authority", None
    if not _is_latest_active_authority(store, example):
        return "superseded_authority", None
    document = store.queries.get_document(example.source_sha)
    if document.role != current_role:
        return "role_mismatch", None
    dataset = document.dataset_group.lower()
    for marker, reason in BLOCKED_DATASET_REASONS.items():
        if marker in dataset:
            return reason, None
    if example.kind is not request.kind:
        return "kind_mismatch", None
    actual = _features(example.reviewed_value)
    if actual is None or not actual.is_complete_for(example.kind) or not _matches(request.features, actual, request.kind):
        return "incompatible_format", None
    case_key = _string(example.reviewed_value.get("case_key")) or example.example_id
    if (example.source_sha, case_key) in conflicts:
        return "conflict", None
    if request.current_case_id in {case_key, example.unit_id, example.asset_id}:
        return "same_case", None
    if request.kind in SOURCE_SPECIFIC_KINDS and example.source_sha != request.source_sha:
        return "source_specific_mismatch", None
    if example.source_sha == request.source_sha:
        tier_rank, tier = 0, "exact_source"
    else:
        tier_rank, tier = relations.get(example.source_sha, (3, "compatible_layout"))
    reference_text = _string(example.reviewed_value.get("reference_text"))
    if reference_text is None:
        return "missing_reference_text", None
    assertion = store.queries.get_authority_assertion(example.authority_assertion_id or "")
    try:
        reviewed_at = datetime.fromisoformat(assertion.reviewed_at.replace("Z", "+00:00"))
    except ValueError:
        return "invalid_review_time", None
    matched_count = 4 if request.kind is ExampleKind.ANSWER_CELL else 3
    return "eligible", _Candidate(example, tier_rank, tier, matched_count, reference_text, case_key, reviewed_at)
