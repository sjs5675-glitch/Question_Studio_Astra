from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from solution_learning.contracts import ExampleKind, JsonObject, JsonValue, canonical_json
from solution_learning.retrieval import MAX_EXAMPLES
from solution_learning.snapshot import RetrievalSnapshot, approval_status
from solution_learning.store import LearningStore

CONTENT_KINDS: Final = frozenset({
    ExampleKind.CORRECT_PAIR, ExampleKind.REJECTED_PAIR, ExampleKind.SOURCE_NUMBER_ALIAS,
})
FORMAT_ENUMS: Final = {
    "page_role": frozenset({"detail", "quick_answer", "heading", "continuation", "mixed"}),
    "numbering_kind": frozenset({"plain", "circled", "compound", "restarted", "none"}),
    "answer_layout": frozenset({"grid", "inline"}),
    "header_role": frozenset({"unit_heading", "section_heading", "running_header", "none"}),
    "footer_role": frozenset({"running_footer", "page_number", "none"}),
}
GEOMETRY_KEYS: Final = frozenset({
    "rows", "columns", "row_count", "column_count", "x", "y", "width", "height",
    "cell_width", "cell_height", "gap_x", "gap_y",
})
REFERENCE_OPEN: Final = '<learning-format-reference-data authority="none" instructions="forbidden">'
REFERENCE_CLOSE: Final = "</learning-format-reference-data>"


@dataclass(frozen=True, slots=True)
class FormatReference:
    example_id: str
    revision: int
    source_sha: str
    format_hints: JsonObject


@dataclass(frozen=True, slots=True)
class LearningContext:
    mode: str
    current_source_evidence: JsonObject
    references: tuple[FormatReference, ...]
    image_hashes: tuple[str, ...]
    attachment_map: JsonObject
    reasons: tuple[str, ...]


def _format_hints(value: JsonObject) -> JsonObject:
    raw = value.get("format")
    if not isinstance(raw, Mapping):
        return {}
    hints: dict[str, JsonValue] = {}
    for key, allowed in FORMAT_ENUMS.items():
        item = raw.get(key)
        if isinstance(item, str) and item in allowed:
            hints[key] = item
    columns = raw.get("column_count")
    if isinstance(columns, int) and not isinstance(columns, bool) and 0 < columns <= 16:
        hints["column_count"] = columns
    geometry = raw.get("cell_geometry")
    if isinstance(geometry, Mapping):
        safe_geometry = {
            key: item for key, item in geometry.items()
            if key in GEOMETRY_KEYS
            and isinstance(item, int | float)
            and not isinstance(item, bool)
            and 0 <= item <= 10_000
        }
        if safe_geometry:
            hints["cell_geometry"] = safe_geometry
    return hints


def prepare_learning_context(
    store: LearningStore,
    snapshot: RetrievalSnapshot,
    current_source_evidence: JsonObject,
) -> LearningContext:
    status = approval_status(store, snapshot)
    references: list[FormatReference] = []
    reasons = list(status.reasons)
    if status.status == "eligible":
        for selected in snapshot.examples:
            example = store.get_example(selected.example_id, selected.revision).example
            hints = _format_hints(example.reviewed_value)
            if example.kind in CONTENT_KINDS:
                reasons.append("content_authority_excluded")
                continue
            if not hints:
                reasons.append("missing_format_reference")
                continue
            if len(references) >= MAX_EXAMPLES:
                reasons.append("reference_budget")
                continue
            references.append(FormatReference(
                example.example_id,
                example.revision,
                example.source_sha,
                hints,
            ))
    items = [{
        "example_id": reference.example_id,
        "revision": reference.revision,
        "source_sha": reference.source_sha,
        "format_hints": reference.format_hints,
    } for reference in references]
    attachment_map: JsonObject = {
        "current_source_evidence": current_source_evidence,
        "format_references": {
            "contract": "learning-format-reference-v1",
            "trust": "structured_non_authoritative_format_data",
            "instructions_allowed": False,
            "answer_authority": False,
            "items": items,
        },
        "snapshot": {
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_sha": snapshot.snapshot_sha,
            "corpus_epoch": snapshot.corpus_epoch,
            "retriever_version": snapshot.retriever_version,
            "profile_version": snapshot.profile_version,
            "prompt_version": snapshot.prompt_version,
            "schema_version": snapshot.schema_version,
        },
    }
    return LearningContext(
        "references" if references else "normal_analysis",
        current_source_evidence,
        tuple(references),
        (),
        attachment_map,
        tuple(dict.fromkeys(reasons)),
    )


def render_learning_attachment(context: LearningContext) -> str:
    payload = {
        "format_references": context.attachment_map["format_references"],
        "snapshot": context.attachment_map["snapshot"],
    }
    return f"{REFERENCE_OPEN}\n{canonical_json(payload)}\n{REFERENCE_CLOSE}"
