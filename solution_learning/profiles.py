from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, assert_never

from .authority import human_authority_is_valid, relation_authority_is_valid
from .contracts import Example, ExampleKind, JsonValue, QualityLevel
from .database import PathBoundaryError, validate_source_path
from .queries import QueryError
from .store import LearningStore

PROFILE_DESCRIPTOR_VERSION: Final = "solution-format-profile-v1"
SAME_BOOK_MIN_CASES: Final = 3
SAME_BOOK_MIN_PAGES: Final = 3
FAMILY_MIN_CASES: Final = 6
FAMILY_MIN_DOCUMENTS: Final = 2
ProfileScope = Literal["same_book", "family"]


@dataclass(frozen=True, slots=True)
class FormatFeatures:
    page_role: str | None
    column_count: int | None
    numbering_kind: str | None
    answer_layout: str | None

    def is_complete_for(self, kind: ExampleKind) -> bool:
        common = (
            bool(self.page_role)
            and self.column_count is not None
            and self.column_count > 0
            and bool(self.numbering_kind)
        )
        return common and (kind is not ExampleKind.ANSWER_CELL or self.answer_layout in {"grid", "inline"})

    @property
    def observed_count(self) -> int:
        return sum(value is not None for value in (
            self.page_role, self.column_count, self.numbering_kind, self.answer_layout,
        ))


@dataclass(frozen=True, slots=True)
class ProfileSample:
    example_id: str
    revision: int


@dataclass(frozen=True, slots=True)
class _ResolvedSample:
    example_id: str
    revision: int
    source_sha: str
    pdf_index: int
    case_key: str


@dataclass(frozen=True, slots=True)
class ProfileDescriptor:
    profile_id: str
    version: str
    region_kind: str
    features: FormatFeatures
    supporting_examples: tuple[tuple[str, int], ...]
    source_shas: tuple[str, ...]
    scope: ProfileScope
    approves_current_answer: bool = False


@dataclass(frozen=True, slots=True)
class ProfilePreparation:
    status: str
    descriptor: ProfileDescriptor | None
    reasons: tuple[str, ...]


def _string(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) and value else None


def _sample_features(example: Example) -> FormatFeatures | None:
    raw = example.reviewed_value.get("format")
    if not isinstance(raw, Mapping):
        return None
    column = raw.get("column_count")
    return FormatFeatures(
        _string(raw.get("page_role")),
        column if isinstance(column, int) and not isinstance(column, bool) else None,
        _string(raw.get("numbering_kind")),
        _string(raw.get("answer_layout")),
    )


def _same_features(expected: FormatFeatures, actual: FormatFeatures, kind: ExampleKind) -> bool:
    common = (
        expected.page_role == actual.page_role
        and expected.column_count == actual.column_count
        and expected.numbering_kind == actual.numbering_kind
    )
    return common and (kind is not ExampleKind.ANSWER_CELL or expected.answer_layout == actual.answer_layout)


def _latest_active_authority(store: LearningStore, example: Example) -> bool:
    row = store.queries.connection.execute(
        "SELECT assertion_id,state FROM authority_assertions WHERE target_id=? AND target_revision=? "
        "ORDER BY reviewed_at DESC,assertion_id DESC LIMIT 1",
        (example.example_id, example.revision),
    ).fetchone()
    return row is not None and row["assertion_id"] == example.authority_assertion_id and row["state"] == "active"


def _reviewed_relation_exists(store: LearningStore, anchor: str, source_sha: str) -> bool:
    if anchor == source_sha:
        return True
    latest = {}
    for relation in store.queries.document_relations(anchor):
        previous = latest.get(relation.relation_id)
        if previous is None or relation.revision > previous.revision:
            latest[relation.relation_id] = relation
    return any(
        relation.state == "reviewed"
        and source_sha in {relation.left_source_sha, relation.right_source_sha}
        and relation.relation_kind in {"same_family", "same_edition"}
        and relation_authority_is_valid(store.queries.connection, relation)
        for relation in latest.values()
    )


def _resolve_samples(
    store: LearningStore,
    samples: tuple[ProfileSample, ...],
    kind: ExampleKind,
    features: FormatFeatures,
    anchor_role: str,
) -> tuple[tuple[_ResolvedSample, ...], tuple[str, ...]]:
    resolved: list[_ResolvedSample] = []
    reasons: list[str] = []
    for sample in samples:
        try:
            record = store.get_example(sample.example_id, sample.revision)
        except QueryError:
            reasons.append("missing_sample")
            continue
        example = record.example
        if not record.source_available:
            reasons.append("source_unavailable")
            continue
        trusted = (
            example.effective_state == "active"
            and example.quality_level is QualityLevel.HUMAN_CONFIRMED
            and human_authority_is_valid(store.queries.connection, example)
            and _latest_active_authority(store, example)
        )
        if not trusted:
            reasons.append("untrusted_sample")
            continue
        document = store.queries.get_document(example.source_sha)
        if document.role != anchor_role:
            reasons.append("role_mismatch")
            continue
        if any(marker in document.dataset_group.lower() for marker in ("eval", "test_fixture", "legacy")):
            reasons.append("blocked_dataset")
            continue
        actual = _sample_features(example)
        if example.kind is not kind or actual is None or not _same_features(features, actual, kind):
            reasons.append("incompatible_sample")
            continue
        case_key = _string(example.reviewed_value.get("case_key"))
        if case_key is None:
            reasons.append("missing_case_key")
            continue
        resolved.append(_ResolvedSample(
            example.example_id, example.revision, example.source_sha, example.pdf_index, case_key,
        ))
    return tuple(resolved), tuple(dict.fromkeys(reasons))


def prepare_profile_descriptor(
    store: LearningStore,
    profile_id: str,
    region_kind: str,
    features: FormatFeatures,
    samples: tuple[ProfileSample, ...],
    *,
    anchor_source_sha: str,
    scope: ProfileScope,
    evaluation_passed: bool,
) -> ProfilePreparation:
    aliases = {"detail": ExampleKind.DETAIL_BOUNDARY, "heading": ExampleKind.HEADING_ROLE}
    kind = aliases.get(region_kind)
    if kind is None:
        try:
            kind = ExampleKind(region_kind)
        except ValueError:
            return ProfilePreparation("abstain", None, ("unsupported_region_kind",))
    if not features.is_complete_for(kind):
        return ProfilePreparation("abstain", None, ("unknown_format",))
    try:
        anchor = store.queries.get_document(anchor_source_sha)
        validate_source_path(anchor.locator, anchor_source_sha, store.allowed_source_roots)
    except (OSError, PathBoundaryError, QueryError):
        return ProfilePreparation("abstain", None, ("source_unavailable",))
    resolved, sample_reasons = _resolve_samples(store, samples, kind, features, anchor.role)
    unique = {(sample.example_id, sample.revision): sample for sample in resolved}
    records = tuple(unique.values())
    reasons = list(sample_reasons)
    match scope:
        case "same_book":
            sources = {sample.source_sha for sample in records}
            pages = {(sample.source_sha, sample.pdf_index) for sample in records}
            cases = {sample.case_key for sample in records}
            if sources != {anchor_source_sha}:
                reasons.append("multiple_sources")
            if len(cases) < SAME_BOOK_MIN_CASES:
                reasons.append("insufficient_cases")
            if len(pages) < SAME_BOOK_MIN_PAGES:
                reasons.append("insufficient_pages")
            status = "draft_ready"
        case "family":
            sources = {sample.source_sha for sample in records}
            cases = {sample.case_key for sample in records}
            if any(not _reviewed_relation_exists(store, anchor_source_sha, source) for source in sources):
                reasons.append("unreviewed_source_relation")
            if len(sources) < FAMILY_MIN_DOCUMENTS:
                reasons.append("insufficient_documents")
            if len(cases) < FAMILY_MIN_CASES:
                reasons.append("insufficient_cases")
            if not evaluation_passed:
                reasons.append("evaluation_not_passed")
            status = "activation_review_ready"
        case unreachable:
            assert_never(unreachable)
    if reasons:
        return ProfilePreparation("abstain", None, tuple(dict.fromkeys(reasons)))
    descriptor = ProfileDescriptor(
        profile_id, PROFILE_DESCRIPTOR_VERSION, region_kind, features,
        tuple(sorted(unique)), tuple(sorted(sources)), scope,
    )
    return ProfilePreparation(status, descriptor, ())
