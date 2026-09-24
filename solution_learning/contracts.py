from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Mapping, TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = Mapping[str, JsonValue]
SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}")


class ContractError(ValueError):
    pass


class ActorKind(StrEnum):
    MODEL = "model"
    HUMAN = "human"
    AGENT = "agent"
    LEGACY_UNKNOWN = "legacy_unknown"
    TEST_FIXTURE = "test_fixture"


class ReviewOrigin(StrEnum):
    USER_ATTESTED = "user_attested"
    AGENT = "agent"
    LEGACY_UNKNOWN = "legacy_unknown"
    TEST_FIXTURE = "test_fixture"


class QualityLevel(StrEnum):
    UNREVIEWED = "unreviewed"
    INDEPENDENTLY_CHECKED = "independently_checked"
    HUMAN_CONFIRMED = "human_confirmed"


class LabelScope(StrEnum):
    CONTENT_MATCH = "content_match"
    CROP_BOUNDS = "crop_bounds"
    UNIT_BOUNDARY = "unit_boundary"
    ANSWER_CELL = "answer_cell"
    NUMBER_MAPPING = "number_mapping"


class ExampleKind(StrEnum):
    HEADING_ROLE = "heading_role"
    NUMBERING_SCOPE = "numbering_scope"
    ANSWER_CELL = "answer_cell"
    DETAIL_BOUNDARY = "detail_boundary"
    CONTINUATION = "continuation"
    CORRECT_PAIR = "correct_pair"
    REJECTED_PAIR = "rejected_pair"
    SOURCE_NUMBER_ALIAS = "source_number_alias"


def require_identifier(value: str, field: str) -> str:
    if not value or len(value) > 200 or any(character.isspace() for character in value):
        raise ContractError(f"{field} must be a non-empty identifier")
    return value


def require_sha256(value: str, field: str) -> str:
    if SHA256_PATTERN.fullmatch(value) is None:
        raise ContractError(f"{field} must be a lowercase SHA-256")
    return value


def canonical_json(value: JsonValue | JsonObject) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ContractError("value must be canonical JSON") from exc


def canonical_sha256(value: JsonValue | JsonObject) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Document:
    source_sha: str
    role: str
    family_id: str | None
    edition_id: str | None
    page_count: int
    locator: str
    dataset_group: str


@dataclass(frozen=True, slots=True)
class ReviewEvent:
    event_id: str
    source_event_sha: str
    operation: str
    actor_kind: ActorKind
    label_scope: LabelScope
    source_revision: int
    before_sha: str
    after_sha: str
    payload: JsonObject
    applied_at: str
    supersedes_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class AuthorityAssertion:
    assertion_id: str
    reviewer_id: str
    review_origin: ReviewOrigin
    action_id: str
    source_event_sha: str
    source_sha: str
    target_id: str
    target_revision: int
    expected_effect_sha: str
    label_scope: LabelScope
    reviewed_at: str
    state: str


@dataclass(frozen=True, slots=True)
class DocumentRelation:
    relation_id: str
    revision: int
    left_source_sha: str
    right_source_sha: str
    relation_kind: str
    evidence_hashes: tuple[str, ...]
    authority_assertion_id: str | None
    supersedes_id: str | None
    state: str


@dataclass(frozen=True, slots=True)
class Example:
    example_id: str
    revision: int
    event_id: str
    kind: ExampleKind
    source_sha: str
    pdf_index: int
    region: tuple[float, float, float, float] | None
    unit_id: str | None
    asset_id: str | None
    reviewed_value: JsonObject
    blob_hashes: tuple[str, ...]
    quality_level: QualityLevel
    payload_sha: str
    effective_state: str
    authority_assertion_id: str | None


@dataclass(frozen=True, slots=True)
class Profile:
    profile_id: str
    revision: int
    region_kind: str
    constrained_params: JsonObject
    supporting_examples: tuple[tuple[str, int], ...]
    applicability: JsonObject
    evaluation_hash: str
    state: str


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    snapshot_id: str
    run_id: str
    corpus_epoch: int
    retriever_version: str
    profiles: tuple[tuple[str, int], ...]
    examples: tuple[tuple[str, int, str], ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class StoredSnapshot:
    snapshot: RunSnapshot
    snapshot_sha: str


@dataclass(frozen=True, slots=True)
class StoredExample:
    example: Example
    source_available: bool
    unavailable_reason: str | None


@dataclass(frozen=True, slots=True)
class ExampleRef:
    example_id: str
    example_revision: int
    profile_id: str | None
    profile_revision: int | None
    snapshot_id: str | None
    run_id: str | None


@dataclass(frozen=True, slots=True)
class IngestCheckpoint:
    source_id: str
    last_event_id: str | None
    last_revision: int
    retry_state: str
