from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astra_cli import quality
from astra_cli.quality_manifest import decision_body, decision_reason
from astra_cli.contracts import JsonMap
from printed_identifiers import normalize_parent_number
from solution_scope import SCOPE_INDEX_VERSION as SCOPE_INDEX_CONTRACT_VERSION

import argparse
import copy
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterator, Mapping, Sequence

from scope_alignment import (
    ROLE_DETAILED as EPISODE_ROLE_DETAILED,
    ROLE_QUICK as EPISODE_ROLE_QUICK,
    ScopeAlignment as ScopeAlignmentLike,
)

from recovery_progress import ProgressError, refresh_recovery_progress
from astra_runtime import inside, validate_parsed_paths


SCHEMA_VERSION = 1
CORE_VERSION = "5"
ASSET_SELECTION_CONTRACT = "explicit-selection-v1"
SCOPE_ALIGNMENT_CONTRACT = "independent-scope-v1"
SCOPE_ALIGNMENT_CONTRACT_VERSION = "1"
MIN_ANCHOR_CONFIDENCE = 0.8
MIN_LINK_CONFIDENCE = 0.8
DUAL_VERIFIER = "gemini+qwen"
DUAL_SCOPE_VERIFIER = "scope-gemini+qwen"
PROPAGATION_UNAVAILABLE_REASONS = frozenset(
    {
        "scope_verification_refs_unavailable",
        "snapshot_build_failed",
    }
)
QUICK_ANSWER_LOOKBACK_PAGES = 4
QUICK_ANSWER_FRONT_MATTER_PAGES = 8
SEARCH_POLICY = 'astra-complete-role-search-v1'


class LinkStatus(str, Enum):
    VERIFIED = "verified"
    REVIEW = "review"
    UNMATCHED = "unmatched"
    CONFLICT = "conflict"


class OverallLinkStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    REVIEW = "review"
    UNMATCHED = "unmatched"
    CONFLICT = "conflict"


class RangeMode(str, Enum):
    EXACT = "exact"
    RANGE = "range"
    START = "start"


class SolutionRole(str, Enum):
    QUICK_ANSWER = "quick_answer"
    DETAILED_SOLUTION = "detailed_solution"
    MIXED = "mixed"


class ManifestError(ValueError):
    pass


class StageHookError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if number < 1:
        raise ValueError(f"{name} must be >= 1")
    return number


def normalize_pdf_index(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("pdf_index must be an integer")
    try:
        index = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("pdf_index must be an integer") from exc
    if index < 0:
        raise ValueError("pdf_index must be >= 0")
    return index


def normalize_pdf_page(value: Any) -> int:
    return _positive_int(value, "pdf_page")


@dataclass(frozen=True)
class PrintedPageRange:
    start: int
    end: int | None
    mode: RangeMode
    raw: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _positive_int(self.start, "printed_start"))
        if self.end is not None:
            object.__setattr__(self, "end", _positive_int(self.end, "printed_end"))
            if self.end < self.start:
                raise ValueError("printed page range is reversed")
        if self.mode == RangeMode.START and self.end is not None:
            raise ValueError("start range must not have an end before expansion")
        if self.mode != RangeMode.START and self.end is None:
            raise ValueError("exact/range must have an end")

    @property
    def resolved(self) -> bool:
        return self.end is not None

    def contains(self, printed_page: int) -> bool:
        page = _positive_int(printed_page, "printed_page")
        return self.end is not None and self.start <= page <= self.end

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "mode": self.mode.value,
            "printed_start": self.start,
            "printed_end": self.end,
        }


def normalize_printed_range(
    value: PrintedPageRange | int | str | Sequence[int],
    *,
    single_mode: RangeMode | str = RangeMode.EXACT,
) -> PrintedPageRange:
    if isinstance(value, PrintedPageRange):
        return value
    mode = RangeMode(single_mode)
    raw = str(value)
    if isinstance(value, bool):
        raise ValueError("printed page cannot be boolean")
    if isinstance(value, int):
        numbers = [value]
    elif isinstance(value, str):
        text = unicodedata.normalize("NFKC", value).replace(",", "")
        numbers = [int(number) for number in re.findall(r"\d+", text)]
    elif isinstance(value, Sequence):
        numbers = list(value)
    else:
        raise ValueError("unsupported printed page range")
    if len(numbers) not in {1, 2}:
        raise ValueError("printed page range must contain one or two page numbers")
    start = _positive_int(numbers[0], "printed_start")
    if len(numbers) == 1:
        return PrintedPageRange(
            start=start,
            end=None if mode == RangeMode.START else start,
            mode=mode if mode == RangeMode.START else RangeMode.EXACT,
            raw=raw,
        )
    end = _positive_int(numbers[1], "printed_end")
    return PrintedPageRange(start, end, RangeMode.RANGE, raw)


def normalize_printed_page(value: int | str) -> int:
    page_range = normalize_printed_range(value)
    if page_range.start != page_range.end:
        raise ValueError("expected one printed page, not a range")
    return page_range.start


@dataclass(frozen=True)
class PageIdentity:
    pdf_index: int
    pdf_page: int
    printed_page: int | None
    segment_id: str = "default"

    def __post_init__(self) -> None:
        index = normalize_pdf_index(self.pdf_index)
        page = normalize_pdf_page(self.pdf_page)
        if page != index + 1:
            raise ValueError("pdf_page must equal pdf_index + 1")
        object.__setattr__(self, "pdf_index", index)
        object.__setattr__(self, "pdf_page", page)
        if self.printed_page is not None:
            object.__setattr__(
                self, "printed_page", normalize_printed_page(self.printed_page)
            )
        if not self.segment_id.strip():
            raise ValueError("segment_id is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdf_index": self.pdf_index,
            "pdf_page": self.pdf_page,
            "printed_page": self.printed_page,
            "segment_id": self.segment_id,
        }


def page_identity(
    pdf_index: int, printed_page: int | str | None, segment_id: str = "default"
) -> PageIdentity:
    index = normalize_pdf_index(pdf_index)
    return PageIdentity(
        pdf_index=index,
        pdf_page=index + 1,
        printed_page=None if printed_page is None else normalize_printed_page(printed_page),
        segment_id=segment_id,
    )


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).strip()).casefold()


def normalize_subquestion_label(value: str | None) -> str | None:
    if value is None:
        return None
    text = _normalized_text(value).strip("()[]{}.")
    if not text:
        return None
    return str(int(text)) if text.isdigit() else text


@dataclass(frozen=True)
class NumberingScope:
    book_id: str
    section_path: tuple[str, ...]
    occurrence: int

    def __post_init__(self) -> None:
        if not self.book_id.strip():
            raise ValueError("book_id is required")
        object.__setattr__(
            self,
            "section_path",
            tuple(part.strip() for part in self.section_path if part.strip()),
        )
        object.__setattr__(self, "occurrence", _positive_int(self.occurrence, "occurrence"))

    @property
    def identity(self) -> str:
        payload = json.dumps(
            [self.book_id, list(self.section_path), self.occurrence],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return "scope-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class NumberedItem:
    item_id: str
    printed_number: str
    sequence: int
    section_path: tuple[str, ...] = ()
    subquestion_label: str | None = None
    owner_id: str | None = None


@dataclass(frozen=True)
class ScopedNumberedItem:
    item: NumberedItem
    scope: NumberingScope
    parent_number: str
    subquestion_label: str | None


def assign_numbering_scopes(
    items: Sequence[NumberedItem], book_id: str
) -> tuple[ScopedNumberedItem, ...]:
    ordered = sorted(items, key=lambda item: item.sequence)
    if len({item.item_id for item in ordered}) != len(ordered):
        raise ValueError("numbered item ids must be unique")
    occurrences: dict[tuple[str, ...], int] = {}
    current_scope: NumberingScope | None = None
    previous: tuple[NumberedItem, str, str | None] | None = None
    result: list[ScopedNumberedItem] = []
    for item in ordered:
        path = tuple(part.strip() for part in item.section_path if part.strip())
        parent = normalize_parent_number(item.printed_number)
        sublabel = normalize_subquestion_label(item.subquestion_label)
        reset = current_scope is None or current_scope.section_path != path
        if previous is not None and not reset:
            previous_item, previous_parent, previous_sub = previous
            same_owner = bool(item.owner_id and item.owner_id == previous_item.owner_id)
            sibling = parent == previous_parent and bool(
                sublabel and previous_sub and sublabel != previous_sub
            )
            current_rank = int(parent) if parent.isdigit() else None
            previous_rank = int(previous_parent) if previous_parent.isdigit() else None
            reset = (
                not same_owner
                and not sibling
                and (
                    parent == previous_parent
                    or (
                        current_rank is not None
                        and previous_rank is not None
                        and current_rank < previous_rank
                    )
                )
            )
        if reset:
            occurrences[path] = occurrences.get(path, 0) + 1
            current_scope = NumberingScope(book_id, path, occurrences[path])
        assert current_scope is not None
        result.append(ScopedNumberedItem(item, current_scope, parent, sublabel))
        previous = (item, parent, sublabel)
    return tuple(result)


@dataclass(frozen=True)
class AnchorBoundary:
    segment_id: str
    printed_page: int
    kind: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "printed_page", normalize_printed_page(self.printed_page)
        )
        if not self.segment_id.strip() or not self.kind.strip():
            raise ValueError("boundary segment_id and kind are required")


@dataclass(frozen=True)
class ProblemBoundary:
    sequence: int
    kind: str

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("problem boundary kind is required")


@dataclass(frozen=True)
class StartAnchor:
    anchor_id: str
    item_ids: tuple[str, ...]
    sequence: int
    scope_id: str
    segment_id: str
    page_range: PrintedPageRange
    confidence: float = 1.0
    reference_document: str = "unknown"

    def __post_init__(self) -> None:
        if not self.anchor_id or not self.item_ids or not self.scope_id or not self.segment_id:
            raise ValueError("anchor identity fields are required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("anchor confidence must be 0..1")
        document = str(self.reference_document).strip().casefold()
        if document not in {"problem", "solution", "unknown"}:
            raise ValueError("anchor reference_document must be problem/solution/unknown")
        object.__setattr__(self, "reference_document", document)


@dataclass(frozen=True)
class AnchorSpan:
    anchor_ids: tuple[str, ...]
    item_ids: tuple[str, ...]
    scope_id: str
    segment_id: str
    page_range: PrintedPageRange
    confidence: float
    resolved: bool
    boundary_source: str | None
    review_reasons: tuple[str, ...] = ()
    reference_document: str = "unknown"

    def __post_init__(self) -> None:
        document = str(self.reference_document).strip().casefold()
        if document not in {"problem", "solution", "unknown"}:
            raise ValueError("anchor reference_document must be problem/solution/unknown")
        object.__setattr__(self, "reference_document", document)


def _scope_identity_tokens(scope: NumberingScope) -> frozenset[str]:
    values: set[str] = set()
    for raw in scope.section_path:
        text = unicodedata.normalize("NFKC", raw).casefold().split(":", 1)[-1]
        text = text.replace("대학수학능력시험", "수능").replace("학년도", "")
        normalized = re.sub(r"[^0-9a-z가-힣]+", "", text)
        if normalized:
            values.add(normalized)
    return frozenset(values)


def propagate_problem_anchors(
    anchors: Sequence[StartAnchor],
    items: Sequence[ScopedNumberedItem],
    boundaries: Sequence[ProblemBoundary] = (),
) -> dict[str, StartAnchor]:
    """Carry a problem-side hint until the next hint, scope, or section boundary."""
    ordered_anchors = sorted(anchors, key=lambda anchor: anchor.sequence)
    ordered_items = sorted(items, key=lambda item: item.item.sequence)
    boundary_sequences = sorted(boundary.sequence for boundary in boundaries)
    result: dict[str, StartAnchor] = {}
    active: StartAnchor | None = None
    anchor_index = 0
    previous_sequence: int | None = None
    for scoped in ordered_items:
        sequence = scoped.item.sequence
        while anchor_index < len(ordered_anchors) and ordered_anchors[anchor_index].sequence <= sequence:
            active = ordered_anchors[anchor_index]
            anchor_index += 1
        if active is not None and any(
            active.sequence < boundary <= sequence
            and (previous_sequence is None or boundary > previous_sequence)
            for boundary in boundary_sequences
        ):
            active = None
        if active is not None and active.scope_id != scoped.scope.identity:
            active = None
        if active is not None:
            result[scoped.item.item_id] = replace(
                active,
                item_ids=tuple(dict.fromkeys(active.item_ids + (scoped.item.item_id,))),
            )
        previous_sequence = sequence
    first_anchor_by_scope: dict[str, StartAnchor] = {}
    for anchor in ordered_anchors:
        first_anchor_by_scope.setdefault(anchor.scope_id, anchor)
    for scoped in ordered_items:
        item_id = scoped.item.item_id
        anchor = first_anchor_by_scope.get(scoped.scope.identity)
        if anchor is None:
            current_tokens = _scope_identity_tokens(scoped.scope)
            for candidate in ordered_anchors:
                if candidate.sequence <= scoped.item.sequence:
                    continue
                candidate_item = next(
                    (
                        value
                        for value in ordered_items
                        if value.item.sequence == candidate.sequence
                    ),
                    None,
                )
                if (
                    candidate_item is None
                    or not current_tokens
                    or not (
                        current_tokens
                        & _scope_identity_tokens(candidate_item.scope)
                    )
                    or any(
                        scoped.item.sequence < boundary <= candidate.sequence
                        for boundary in boundary_sequences
                    )
                ):
                    continue
                run = [
                    value
                    for value in ordered_items
                    if scoped.item.sequence
                    <= value.item.sequence
                    <= candidate.sequence
                ]
                numbers = [
                    int(value.parent_number)
                    for value in run
                    if value.parent_number.isdigit()
                ]
                if len(numbers) == len(run) and all(
                    right == left + 1
                    for left, right in zip(numbers, numbers[1:], strict=False)
                ):
                    anchor = candidate
                    break
        if (
            item_id in result
            or anchor is None
            or scoped.item.sequence >= anchor.sequence
            or any(
                scoped.item.sequence < boundary <= anchor.sequence
                for boundary in boundary_sequences
            )
        ):
            continue
        result[item_id] = replace(
            anchor,
            item_ids=tuple(dict.fromkeys((item_id,) + anchor.item_ids)),
        )
    return result


def expand_start_anchors(
    anchors: Sequence[StartAnchor],
    solution_page_map: Sequence[PageIdentity],
    boundaries: Sequence[AnchorBoundary] = (),
) -> tuple[AnchorSpan, ...]:
    ordered = sorted(anchors, key=lambda anchor: anchor.sequence)
    merged: list[StartAnchor] = []
    merged_ids: list[list[str]] = []
    for anchor in ordered:
        key = (
            anchor.scope_id,
            anchor.segment_id,
            anchor.page_range.start,
            anchor.page_range.end,
            anchor.page_range.mode,
            anchor.reference_document,
        )
        previous_key = None
        if merged:
            previous = merged[-1]
            previous_key = (
                previous.scope_id,
                previous.segment_id,
                previous.page_range.start,
                previous.page_range.end,
                previous.page_range.mode,
                previous.reference_document,
            )
        if key == previous_key:
            prior = merged[-1]
            merged[-1] = replace(
                prior,
                item_ids=tuple(dict.fromkeys(prior.item_ids + anchor.item_ids)),
                confidence=min(prior.confidence, anchor.confidence),
            )
            merged_ids[-1].append(anchor.anchor_id)
        else:
            merged.append(anchor)
            merged_ids.append([anchor.anchor_id])

    printed_by_segment: dict[str, list[int]] = {}
    for page in solution_page_map:
        if page.printed_page is not None:
            printed_by_segment.setdefault(page.segment_id, []).append(page.printed_page)
    for values in printed_by_segment.values():
        values.sort()

    result: list[AnchorSpan] = []
    for index, anchor in enumerate(merged):
        current_range = anchor.page_range
        if current_range.end is not None:
            available = set(printed_by_segment.get(anchor.segment_id, ()))
            covered = current_range.start in available and current_range.end in available
            result.append(
                AnchorSpan(
                    tuple(merged_ids[index]),
                    anchor.item_ids,
                    anchor.scope_id,
                    anchor.segment_id,
                    current_range,
                    anchor.confidence,
                    covered,
                    "explicit_range" if covered else None,
                    () if covered else ("explicit_range_not_in_page_map",),
                    anchor.reference_document,
                )
            )
            continue
        same_start_conflict = any(
            other_index != index
            and
            other.scope_id == anchor.scope_id
            and
            other.segment_id == anchor.segment_id
            and other.page_range.start == current_range.start
            for other_index, other in enumerate(merged)
        )
        if same_start_conflict:
            result.append(
                AnchorSpan(
                    tuple(merged_ids[index]),
                    anchor.item_ids,
                    anchor.scope_id,
                    anchor.segment_id,
                    current_range,
                    anchor.confidence,
                    False,
                    None,
                    ("same_start_scope_conflict",),
                    anchor.reference_document,
                )
            )
            continue
        boundary_candidates: list[tuple[int, str]] = [
            (boundary.printed_page, boundary.kind)
            for boundary in boundaries
            if boundary.segment_id == anchor.segment_id
            and boundary.printed_page > current_range.start
        ]
        boundary_candidates += [
            (other.page_range.start, "next_anchor")
            for other in merged[index + 1 :]
            if other.segment_id == anchor.segment_id
            and other.page_range.start > current_range.start
        ]
        next_boundary = min(boundary_candidates, default=None, key=lambda item: item[0])
        available = [
            page
            for page in printed_by_segment.get(anchor.segment_id, [])
            if page >= current_range.start
            and (next_boundary is None or page < next_boundary[0])
        ]
        if not available or current_range.start not in available:
            result.append(
                AnchorSpan(
                    tuple(merged_ids[index]),
                    anchor.item_ids,
                    anchor.scope_id,
                    anchor.segment_id,
                    current_range,
                    anchor.confidence,
                    False,
                    None,
                    ("anchor_range_cannot_be_expanded",),
                    anchor.reference_document,
                )
            )
            continue
        expanded = PrintedPageRange(
            current_range.start,
            max(available),
            RangeMode.RANGE if max(available) > current_range.start else RangeMode.EXACT,
            current_range.raw,
        )
        result.append(
            AnchorSpan(
                tuple(merged_ids[index]),
                anchor.item_ids,
                anchor.scope_id,
                anchor.segment_id,
                expanded,
                anchor.confidence,
                True,
                next_boundary[1] if next_boundary else "segment_end",
                (),
                anchor.reference_document,
            )
        )
    return tuple(result)


def _bbox(value: Sequence[int]) -> tuple[int, int, int, int]:
    if len(value) != 4 or any(isinstance(number, bool) or not isinstance(number, int) for number in value):
        raise ValueError("bbox must contain four integers")
    x0, y0, x1, y1 = value
    if not all(0 <= number <= 1000 for number in value) or x0 >= x1 or y0 >= y1:
        raise ValueError("bbox must satisfy 0<=x0<x1<=1000 and 0<=y0<y1<=1000")
    return x0, y0, x1, y1


@dataclass(frozen=True)
class SolutionPart:
    page: PageIdentity
    bbox: tuple[int, int, int, int]
    order: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "bbox", _bbox(self.bbox))

    def to_dict(self) -> dict[str, Any]:
        return {"page": self.page.to_dict(), "bbox": list(self.bbox), "order": self.order}


@dataclass(frozen=True, slots=True)
class ExpandedSourceBinding:
    problem_source_sha256: str
    solution_source_sha256: str
    problem_signature_sha256: str
    asset_signature_sha256: str

    def __post_init__(self) -> None:
        if any(re.fullmatch(r'[0-9a-f]{64}', value) is None for value in (
            self.problem_source_sha256, self.solution_source_sha256,
            self.problem_signature_sha256, self.asset_signature_sha256,
        )):
            raise ValueError('expanded source binding requires exact SHA256 identities')


@dataclass(frozen=True)
class SolutionAsset:
    asset_id: str
    role: SolutionRole
    sequence: int
    scope_id: str
    parent_number: str
    subquestion_label: str | None
    parts: tuple[SolutionPart, ...]
    fingerprints: tuple[str, ...] = ()
    explicit_answer: str | None = None
    confidence: float = 1.0
    covered_child_labels: tuple[str, ...] = ()
    source_document: str = "solution"
    review_reason: str | None = None
    expanded_source_binding: ExpandedSourceBinding | None = None

    def __post_init__(self) -> None:
        if not self.asset_id or not self.scope_id or not self.parts:
            raise ValueError("solution asset identity and parts are required")
        if self.expanded_source_binding is not None and not isinstance(self.expanded_source_binding, ExpandedSourceBinding):
            raise ValueError('expanded source binding must be typed')
        object.__setattr__(self, "role", SolutionRole(self.role))
        object.__setattr__(self, "parent_number", normalize_parent_number(self.parent_number))
        object.__setattr__(
            self, "subquestion_label", normalize_subquestion_label(self.subquestion_label)
        )
        if isinstance(self.covered_child_labels, (str, bytes)):
            raise ValueError("covered child labels must be a sequence")
        covered_child_labels = tuple(
            dict.fromkeys(
                label
                for value in self.covered_child_labels
                if (label := normalize_subquestion_label(value)) is not None
            )
        )
        if covered_child_labels and (
            self.role != SolutionRole.DETAILED_SOLUTION
            or self.subquestion_label is not None
        ):
            raise ValueError("shared parent solutions must be parent-level detailed assets")
        source_document = str(self.source_document).strip().casefold()
        if source_document not in {"solution", "problem"}:
            raise ValueError("solution asset source_document must be solution/problem")
        review_reason = (
            str(self.review_reason).strip() if self.review_reason is not None else None
        )
        if review_reason == "":
            raise ValueError("solution asset review_reason must not be empty")
        object.__setattr__(self, "covered_child_labels", covered_child_labels)
        object.__setattr__(self, "source_document", source_document)
        object.__setattr__(self, "review_reason", review_reason)
        if len({part.page.segment_id for part in self.parts}) != 1:
            raise ValueError("one solution asset cannot cross page-map segments")
        if not 0 <= self.confidence <= 1:
            raise ValueError("asset confidence must be 0..1")

    @property
    def segment_id(self) -> str:
        return self.parts[0].page.segment_id

    @property
    def printed_pages(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    part.page.printed_page
                    for part in self.parts
                    if part.page.printed_page is not None
                }
            )
        )

    def signature(self) -> dict[str, Any]:
        signature = {
            "asset_id": self.asset_id,
            "role": self.role.value,
            "sequence": self.sequence,
            "scope_id": self.scope_id,
            "parent_number": self.parent_number,
            "subquestion_label": self.subquestion_label,
            "parts": [part.to_dict() for part in self.parts],
            "fingerprints": list(self.fingerprints),
            "explicit_answer": self.explicit_answer,
            "confidence": self.confidence,
        }
        if self.covered_child_labels:
            signature["covered_child_labels"] = list(self.covered_child_labels)
        if self.source_document != "solution":
            signature["source_document"] = self.source_document
        if self.review_reason is not None:
            signature["review_reason"] = self.review_reason
        if self.expanded_source_binding is not None:
            signature['expanded_source_binding'] = asdict(self.expanded_source_binding)
        return signature

    def manifest_record(
        self, output_file: str | Path, output_sha256: str | None = None
    ) -> dict[str, Any]:
        relative = Path(output_file)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("solution asset output_file must be a safe relative path")
        record = {
            "kind": self.role.value,
            "output_file": relative.as_posix(),
            "confidence": self.confidence,
            "parts": [
                {
                    "pdf_index": part.page.pdf_index,
                    "pdf_page": part.page.pdf_page,
                    "printed_page": part.page.printed_page,
                    "segment_id": part.page.segment_id,
                    "bbox": list(part.bbox),
                }
                for part in sorted(self.parts, key=lambda value: value.order)
            ],
        }
        if output_sha256 is not None:
            record["output_sha256"] = output_sha256
        if self.covered_child_labels:
            record["covered_child_labels"] = list(self.covered_child_labels)
        if self.source_document != "solution":
            record["source_document"] = self.source_document
        if self.review_reason is not None:
            record["review_reason"] = self.review_reason
        return record


@dataclass(frozen=True)
class ProblemItem:
    item_id: str
    sequence: int
    scope_id: str
    parent_number: str
    subquestion_label: str | None
    anchor: AnchorSpan | None
    fingerprints: tuple[str, ...] = ()
    crop_verified: bool = True
    group_label: str | None = None
    column: str | None = None
    owner_id: str | None = None
    crop_search_eligible: bool | None = None
    cache_only_detailed_review: bool = False

    def __post_init__(self) -> None:
        if not self.item_id or not self.scope_id:
            raise ValueError("problem item identity is required")
        object.__setattr__(self, "parent_number", normalize_parent_number(self.parent_number))
        object.__setattr__(
            self, "subquestion_label", normalize_subquestion_label(self.subquestion_label)
        )
        group_label = str(self.group_label or "").strip() or None
        column = str(self.column or "").strip().casefold() or None
        owner_id = str(self.owner_id or "").strip() or None
        search_eligible = (
            self.crop_verified
            if self.crop_search_eligible is None
            else self.crop_search_eligible
        )
        if (
            not isinstance(self.crop_verified, bool)
            or not isinstance(search_eligible, bool)
            or not isinstance(self.cache_only_detailed_review, bool)
        ):
            raise ValueError("problem crop eligibility must be boolean")
        if self.crop_verified and not search_eligible:
            raise ValueError("verified problem crops must remain search eligible")
        if column not in {None, "left", "right"}:
            raise ValueError("problem column must be left/right")
        object.__setattr__(self, "group_label", group_label)
        object.__setattr__(self, "column", column)
        object.__setattr__(self, "owner_id", owner_id)

    @property
    def crop_is_search_eligible(self) -> bool:
        return (
            self.crop_verified
            if self.crop_search_eligible is None
            else self.crop_search_eligible
        )

    def signature(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "sequence": self.sequence,
            "scope_id": self.scope_id,
            "parent_number": self.parent_number,
            "subquestion_label": self.subquestion_label,
            "anchor": None
            if self.anchor is None
            else {
                "anchor_ids": list(self.anchor.anchor_ids),
                "item_ids": list(self.anchor.item_ids),
                "scope_id": self.anchor.scope_id,
                "segment_id": self.anchor.segment_id,
                "range": self.anchor.page_range.to_dict(),
                "confidence": self.anchor.confidence,
                "resolved": self.anchor.resolved,
                "boundary_source": self.anchor.boundary_source,
                "review_reasons": list(self.anchor.review_reasons),
                "reference_document": self.anchor.reference_document,
            },
            "fingerprints": list(self.fingerprints),
            "crop_verified": self.crop_verified,
            "crop_search_eligible": self.crop_is_search_eligible,
            "group_label": self.group_label,
            "column": self.column,
            "owner_id": self.owner_id,
            "cache_only_detailed_review": self.cache_only_detailed_review,
        }


@dataclass(frozen=True)
class PairVerification:
    same_item: bool
    confidence: float
    verifier: str
    explicit_answer_conflict: bool = False
    corroborating_asset_id: str | None = None
    quality_receipt: JsonMap | None = None
    evidence_root: Path | None = None
    quality_reason: str | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1 or not self.verifier:
            raise ValueError("verification confidence/verifier is invalid")
        if self.corroborating_asset_id is not None and not self.corroborating_asset_id:
            raise ValueError("corroborating asset id must be non-empty")


@dataclass(frozen=True)
class CandidatePair:
    problem_id: str
    asset_id: str
    role: SolutionRole
    fingerprint_similarity: float


@dataclass(frozen=True)
class ScopeCandidatePair:
    problem_id: str
    asset_id: str
    role: SolutionRole
    fingerprint_similarity: float
    problem_scope_id: str
    solution_scope_id: str
    alignment_roles: tuple[str, ...]
    alignment_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class LinkDecision:
    item_id: str
    status: LinkStatus
    answer_ids: tuple[str, ...] = ()
    explanation_ids: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()
    solution_printed_pages: tuple[int, ...] = ()
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", LinkStatus(self.status))
        if not 0 <= self.confidence <= 1:
            raise ValueError("link confidence must be 0..1")
        linked = bool(self.answer_ids or self.explanation_ids)
        if self.status == LinkStatus.VERIFIED and not linked:
            raise ValueError("verified link requires an answer or explanation asset")
        if self.status != LinkStatus.VERIFIED and linked:
            raise ValueError("non-verified links cannot expose confirmed asset ids")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "answer_ids": list(self.answer_ids),
            "explanation_ids": list(self.explanation_ids),
            "candidate_ids": list(self.candidate_ids),
            "solution_printed_pages": list(self.solution_printed_pages),
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "review_reasons": list(self.review_reasons),
        }


LINK_ROLE_STATUS_FIELDS = (
    "answer_status",
    "explanation_status",
    "overall_status",
)


def _manifest_asset_role(asset: object) -> SolutionRole | None:
    if isinstance(asset, SolutionAsset):
        return asset.role
    if not isinstance(asset, Mapping):
        return None
    try:
        return SolutionRole(str(asset.get("kind") or asset.get("role")))
    except ValueError:
        return None


def derive_link_statuses(
    link: Mapping[str, Any], assets: Mapping[str, object]
) -> dict[str, str]:
    """Derive user-facing role completeness without changing legacy status."""

    legacy = LinkStatus(str(link.get("status") or LinkStatus.UNMATCHED.value))
    candidate_roles = {
        role
        for asset_id in link.get("candidate_ids", [])
        if (role := _manifest_asset_role(assets.get(str(asset_id)))) is not None
    }

    def role_status(field: str, roles: set[SolutionRole]) -> LinkStatus:
        if link.get(field):
            return LinkStatus.VERIFIED
        if candidate_roles & roles:
            return (
                LinkStatus.CONFLICT
                if legacy == LinkStatus.CONFLICT
                else LinkStatus.REVIEW
            )
        if not candidate_roles and legacy in {LinkStatus.REVIEW, LinkStatus.CONFLICT}:
            return legacy
        return LinkStatus.UNMATCHED

    answer = role_status(
        "answer_ids", {SolutionRole.QUICK_ANSWER, SolutionRole.MIXED}
    )
    explanation = role_status(
        "explanation_ids", {SolutionRole.DETAILED_SOLUTION, SolutionRole.MIXED}
    )
    if answer == explanation == LinkStatus.VERIFIED:
        overall = OverallLinkStatus.COMPLETE
    elif LinkStatus.CONFLICT in {answer, explanation}:
        overall = OverallLinkStatus.CONFLICT
    elif LinkStatus.VERIFIED in {answer, explanation}:
        overall = OverallLinkStatus.PARTIAL
    elif LinkStatus.REVIEW in {answer, explanation}:
        overall = OverallLinkStatus.REVIEW
    else:
        overall = OverallLinkStatus.UNMATCHED
    return {
        "answer_status": answer.value,
        "explanation_status": explanation.value,
        "overall_status": overall.value,
    }


def serialize_link_decision(
    decision: LinkDecision, assets: Mapping[str, object]
) -> dict[str, Any]:
    record = decision.to_dict()
    record.update(derive_link_statuses(record, assets))
    return record


def _fingerprint_tokens(values: Sequence[str]) -> set[str]:
    text = " ".join(unicodedata.normalize("NFKC", value).casefold() for value in values)
    return set(re.findall(r"[0-9a-z가-힣]+", text))


def fingerprint_similarity(left: Sequence[str], right: Sequence[str]) -> float:
    first, second = _fingerprint_tokens(left), _fingerprint_tokens(right)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def neighbor_evidence_state(result: Mapping[str, Any]) -> str | None:
    """Classify explicit neighboring-question claims in verifier evidence."""

    states: set[str] = set()
    for lowered in (
        clause
        for sentence in result.get("evidence", [])
        for clause in re.split(r"[.;,!?]|\b(?:but|and|yet|whereas|except|however)\b|하지만|그러나|반면|그리고|또한", str(sentence).casefold())
    ):
        if not any(
            token in lowered
            for token in (
                "다음 문제",
                "다음 문항",
                "인접 문제",
                "인접 문항",
                "next question",
                "neighboring question",
                "neighboring entry",
            )
        ):
            continue
        if re.search(r"\bnot\s+(?:\w+\s+){0,4}(?:without|no)\s+(?:any\s+)?(?:next|neighboring)\s+(?:question|entry)", lowered) or re.search(r"(?:않|없).{0,20}(?:것[은이]\s*아니|않지)|없지\s*않", lowered):
            return "conflict"
        direct_absence = r'(?:다음|인접)\s*(?:문제|문항)[은는이가]?\s*없(?:다|음|습니다)(?![가-힣])'
        if re.search(direct_absence, lowered):
            states.add('absent')
            remaining = re.sub(direct_absence, '', lowered)
            if re.search(r'(?:다음|인접)\s*(?:문제|문항).{0,20}(?:포함|보이|보인|보여|침범|노출)', remaining):
                states.add('present')
            continue
        absent = bool(re.search(r"\bwithout\s+(?:any\s+)?(?:next|neighboring)\s+(?:question|entry)\b", lowered)) or any(
            token in lowered
            for token in (
                "포함하지",
                "포함되지",
                "보이지 않",
                "간섭 없음",
                "간섭이 없음",
                "흔적 없음",
                "흔적이 없음",
                "not included",
                "not visible",
                "not present",
                "no neighboring",
            )
        )
        present = any(
            token in lowered
            for token in (
                "포함",
                "보이",
                "보인",
                "보여",
                "침범",
                "노출",
                "included",
                "visible",
                "present",
            )
        )
        if absent:
            states.add("absent")
        elif present:
            states.add("present")
    if len(states) == 1:
        return next(iter(states))
    return "conflict" if states else None


def _anchor_review_reason(problem: ProblemItem) -> str | None:
    anchor = problem.anchor
    if anchor is None or not anchor.resolved or anchor.page_range.end is None:
        return "anchor_unresolved"
    if anchor.scope_id != problem.scope_id or problem.item_id not in anchor.item_ids:
        return "anchor_provenance_mismatch"
    if anchor.confidence < MIN_ANCHOR_CONFIDENCE:
        return "anchor_confidence_below_threshold"
    return None


def _location_compatible(problem: ProblemItem, asset: SolutionAsset) -> bool:
    anchor = problem.anchor
    if _anchor_review_reason(problem) is not None:
        return False
    assert anchor is not None
    if (
        problem.scope_id != asset.scope_id
        or problem.parent_number != asset.parent_number
        or anchor.reference_document not in {"unknown", asset.source_document}
        or anchor.reference_document != "unknown"
        and anchor.segment_id != asset.segment_id
        or not asset.printed_pages
    ):
        return False
    if asset.role == SolutionRole.QUICK_ANSWER:
        lower = max(1, anchor.page_range.start - QUICK_ANSWER_LOOKBACK_PAGES)
        return all(
            lower <= page <= anchor.page_range.end
            or 1 <= page <= QUICK_ANSWER_FRONT_MATTER_PAGES
            and page < anchor.page_range.start
            for page in asset.printed_pages
        )
    return all(anchor.page_range.contains(page) for page in asset.printed_pages)


def _strict_candidate_compatible(problem: ProblemItem, asset: SolutionAsset) -> bool:
    return (
        asset.review_reason is None
        and problem.subquestion_label == asset.subquestion_label
        and _location_compatible(problem, asset)
    )


def _strict_compatible(problem: ProblemItem, asset: SolutionAsset) -> bool:
    return problem.crop_is_search_eligible and _strict_candidate_compatible(problem, asset)


def _shared_parent_compatible(problem: ProblemItem, asset: SolutionAsset) -> bool:
    return (
        asset.role == SolutionRole.DETAILED_SOLUTION
        and asset.subquestion_label is None
        and problem.subquestion_label is not None
        and problem.subquestion_label in asset.covered_child_labels
        and _location_compatible(problem, asset)
    )


def _forced_review_compatible(problem: ProblemItem, asset: SolutionAsset) -> bool:
    if (
        asset.review_reason is None
        or problem.scope_id != asset.scope_id
        or problem.parent_number != asset.parent_number
    ):
        return False
    exact = problem.subquestion_label == asset.subquestion_label
    shared = (
        asset.role == SolutionRole.DETAILED_SOLUTION
        and asset.subquestion_label is None
        and problem.subquestion_label is not None
        and problem.subquestion_label in asset.covered_child_labels
    )
    if not (exact or shared):
        return False
    if problem.anchor is None:
        return asset.source_document == "problem"
    if _anchor_review_reason(problem) is not None:
        return False
    return asset.source_document == "problem" or _location_compatible(problem, asset)


def _compatible_assets(
    problem: ProblemItem, assets: Sequence[SolutionAsset]
) -> tuple[SolutionAsset, ...]:
    strict = tuple(asset for asset in assets if _strict_candidate_compatible(problem, asset))
    forced = tuple(asset for asset in assets if _forced_review_compatible(problem, asset))
    shared = tuple(asset for asset in assets if _shared_parent_compatible(problem, asset))
    strict_quick = any(
        asset.role in {SolutionRole.QUICK_ANSWER, SolutionRole.MIXED}
        for asset in strict
    )
    strict_detail = any(
        asset.role in {SolutionRole.DETAILED_SOLUTION, SolutionRole.MIXED}
        for asset in strict
    )
    result = list(strict)
    if not strict_quick:
        result.extend(
            asset
            for asset in forced
            if asset.role in {SolutionRole.QUICK_ANSWER, SolutionRole.MIXED}
        )
    if not strict_detail:
        forced_detail = tuple(
            asset
            for asset in forced
            if asset.role in {SolutionRole.DETAILED_SOLUTION, SolutionRole.MIXED}
        )
        result.extend(forced_detail or shared)
    return tuple(dict.fromkeys(result))


def candidate_pairs(
    problems: Sequence[ProblemItem], assets: Sequence[SolutionAsset]
) -> tuple[CandidatePair, ...]:
    ordered_assets = sorted(assets, key=lambda item: item.sequence)
    pairs: list[CandidatePair] = []
    for problem in sorted(problems, key=lambda item: item.sequence):
        compatible = _compatible_assets(problem, ordered_assets)
        if (
            not problem.crop_is_search_eligible
            or problem.cache_only_detailed_review
        ):
            continue
        pairs.extend(
            CandidatePair(
                problem.item_id,
                asset.asset_id,
                asset.role,
                fingerprint_similarity(problem.fingerprints, asset.fingerprints),
            )
            for asset in compatible
            if asset.review_reason is None and not _shared_parent_compatible(problem, asset)
        )
    return tuple(pairs)


def expanded_candidate_pairs(
    problems: Sequence[ProblemItem], assets: Sequence[SolutionAsset], *,
    problem_source_sha256: str | None = None,
    solution_source_sha256: str | None = None,
) -> tuple[CandidatePair, ...]:
    return tuple(
        CandidatePair(problem.item_id, asset.asset_id, asset.role,
                      fingerprint_similarity(problem.fingerprints, asset.fingerprints))
        for problem in problems for asset in assets
        if problem.crop_is_search_eligible and not problem.cache_only_detailed_review
        and ((_anchor_review_reason(problem) is None
              and problem.anchor.reference_document in {"unknown", asset.source_document}
              and (problem.anchor.reference_document == "unknown"
                   or asset.segment_id == problem.anchor.segment_id))
             or (problem.anchor is None and problem_source_sha256 is not None and solution_source_sha256 is not None
                 and asset.expanded_source_binding == ExpandedSourceBinding(
                     problem_source_sha256, solution_source_sha256,
                     quality.identity(problem.signature()),
                     quality.identity(replace(asset, expanded_source_binding=None).signature()))))
        and asset.review_reason is None
        and asset.scope_id == problem.scope_id
        and asset.parent_number == problem.parent_number
        and (asset.subquestion_label == problem.subquestion_label
             or asset.role == SolutionRole.DETAILED_SOLUTION
             and asset.subquestion_label is None
             and problem.subquestion_label in asset.covered_child_labels)
    )


Score = tuple[int, int, int, int]
PairKey = tuple[str, str]
ZERO_SCORE: Score = (0, 0, 0, 0)


def _add_score(left: Score, right: Score) -> Score:
    return tuple(first + second for first, second in zip(left, right, strict=True))


def _confirmed_verification(verification: PairVerification | None,
                            problem: ProblemItem | None = None,
                            asset: SolutionAsset | None = None,
                            *, conflict: bool = False,
                            corroborating: SolutionAsset | None = None) -> bool:
    return bool(
        verification is not None
        and verification.evidence_root is not None
        and problem is not None and asset is not None
        and verification.explicit_answer_conflict is conflict
        and (verification.same_item or conflict)
        and quality.validate_receipt(verification.quality_receipt, verification.evidence_root,
                                     problem=problem.signature(), asset=asset.signature(),
                                     conflict=conflict, min_confidence=0.0,
                                     corroborating=corroborating.signature() if corroborating is not None else None) is None
    )


def _strong_verification(
    verification: PairVerification | None, min_confidence: float,
    problem: ProblemItem | None = None, asset: SolutionAsset | None = None,
) -> bool:
    return bool(_confirmed_verification(verification, problem, asset)
                and verification is not None and verification.confidence >= min_confidence)


def _edge_score(
    problem: ProblemItem,
    asset: SolutionAsset,
    verifications: Mapping[PairKey, PairVerification],
) -> Score:
    verification = verifications.get((problem.item_id, asset.asset_id))
    confirmed = _confirmed_verification(verification, problem, asset)
    return (
        int(confirmed),
        1,
        round(verification.confidence * 1000) if confirmed and verification else 0,
        round(fingerprint_similarity(problem.fingerprints, asset.fingerprints) * 1000),
    )


def _score_margin(optimum: Score, runner_up: Score) -> float:
    if runner_up[0] < optimum[0] or runner_up[1] < optimum[1]:
        return 1.0
    if runner_up[2] < optimum[2]:
        return min(1.0, max(0.0, (optimum[2] - runner_up[2]) / 1000))
    return min(1.0, max(0.0, (optimum[3] - runner_up[3]) / 1000))


@dataclass(frozen=True)
class _GroupAlignment:
    selected: Mapping[str, SolutionAsset]
    possible: Mapping[str, tuple[SolutionAsset, ...]]
    runner_up_margin: Mapping[str, float]


def _align_group(
    problems: Sequence[ProblemItem],
    assets: Sequence[SolutionAsset],
    verifications: Mapping[PairKey, PairVerification],
    blocked_pairs: set[PairKey],
    admitted_pairs: frozenset[PairKey] = frozenset(),
) -> _GroupAlignment:
    def compatible(problem: ProblemItem, asset: SolutionAsset) -> bool:
        return problem.crop_is_search_eligible and (
            _strict_compatible(problem, asset)
            or (problem.item_id, asset.asset_id) in admitted_pairs
        )
    ordered_problems = sorted(problems, key=lambda item: item.sequence)
    ordered_assets = sorted(assets, key=lambda item: item.sequence)
    n, m = len(ordered_problems), len(ordered_assets)
    forward: list[list[Score | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    forward[0][0] = ZERO_SCORE
    for i in range(n + 1):
        for j in range(m + 1):
            current = forward[i][j]
            if current is None:
                continue
            if i < n and (forward[i + 1][j] is None or current > forward[i + 1][j]):
                forward[i + 1][j] = current
            if j < m and (forward[i][j + 1] is None or current > forward[i][j + 1]):
                forward[i][j + 1] = current
            pair_key = (
                (ordered_problems[i].item_id, ordered_assets[j].asset_id)
                if i < n and j < m
                else None
            )
            if (
                pair_key is not None
                and pair_key not in blocked_pairs
                and compatible(ordered_problems[i], ordered_assets[j])
            ):
                matched = _add_score(
                    current,
                    _edge_score(
                        ordered_problems[i],
                        ordered_assets[j],
                        verifications,
                    ),
                )
                if forward[i + 1][j + 1] is None or matched > forward[i + 1][j + 1]:
                    forward[i + 1][j + 1] = matched

    backward: list[list[Score]] = [[ZERO_SCORE] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            options = [backward[i + 1][j], backward[i][j + 1]]
            if (
                (ordered_problems[i].item_id, ordered_assets[j].asset_id)
                not in blocked_pairs
                and compatible(ordered_problems[i], ordered_assets[j])
            ):
                options.append(
                    _add_score(
                        _edge_score(
                            ordered_problems[i],
                            ordered_assets[j],
                            verifications,
                        ),
                        backward[i + 1][j + 1],
                    )
                )
            backward[i][j] = max(options)

    optimum = forward[n][m] or ZERO_SCORE
    selected: dict[str, SolutionAsset] = {}
    possible: dict[str, tuple[SolutionAsset, ...]] = {}
    margins: dict[str, float] = {}
    for i, problem in enumerate(ordered_problems):
        pair_scores: list[tuple[SolutionAsset, Score]] = []
        for j, asset in enumerate(ordered_assets):
            prefix = forward[i][j]
            if (
                prefix is None
                or (problem.item_id, asset.asset_id) in blocked_pairs
                or not compatible(problem, asset)
            ):
                continue
            score = _add_score(
                _add_score(
                    prefix,
                    _edge_score(problem, asset, verifications),
                ),
                backward[i + 1][j + 1],
            )
            pair_scores.append((asset, score))
        candidates = [asset for asset, score in pair_scores if score == optimum]
        unmatched_scores = [
            _add_score(forward[i][j] or ZERO_SCORE, backward[i + 1][j])
            for j in range(m + 1)
            if forward[i][j] is not None
        ]
        unmatched_is_optimal = optimum in unmatched_scores
        possible[problem.item_id] = tuple(candidates)
        if len(candidates) == 1 and not unmatched_is_optimal:
            selected[problem.item_id] = candidates[0]
            alternatives = [
                score for asset, score in pair_scores if asset.asset_id != candidates[0].asset_id
            ] + unmatched_scores
            runner_up = max(alternatives, default=(-1, 0, 0, 0))
            margins[problem.item_id] = _score_margin(optimum, runner_up)
    return _GroupAlignment(selected, possible, margins)


def _align_role(
    problems: Sequence[ProblemItem],
    assets: Sequence[SolutionAsset],
    verifications: Mapping[PairKey, PairVerification],
    blocked_pairs: set[PairKey],
    admitted_pairs: frozenset[PairKey] = frozenset(),
) -> _GroupAlignment:
    selected: dict[str, SolutionAsset] = {}
    possible: dict[str, tuple[SolutionAsset, ...]] = {item.item_id: () for item in problems}
    margins: dict[str, float] = {}
    groups: dict[tuple[str, str], list[ProblemItem]] = {}
    for problem in problems:
        if problem.anchor is not None and problem.anchor.resolved or any(pair[0] == problem.item_id for pair in admitted_pairs):
            groups.setdefault((problem.scope_id, problem.anchor.segment_id if problem.anchor else ''), []).append(problem)
    assets_by_group: dict[tuple[str, str], list[SolutionAsset]] = {}
    for asset in assets:
        assets_by_group.setdefault((asset.scope_id, asset.segment_id), []).append(asset)
    for key, group_problems in groups.items():
        aligned = _align_group(
            group_problems,
            tuple(dict.fromkeys((*assets_by_group.get(key, ()), *(asset for asset in assets
                if any((problem.item_id, asset.asset_id) in admitted_pairs for problem in group_problems))))),
            verifications,
            blocked_pairs,
            admitted_pairs,
        )
        selected.update(aligned.selected)
        possible.update(aligned.possible)
        margins.update(aligned.runner_up_margin)
    return _GroupAlignment(selected, possible, margins)


def _normalize_answer(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


def _canonical_answer(value: str) -> str | None:
    text = unicodedata.normalize("NFKC", value).casefold().strip()
    ratios = re.findall(
        r"(-?\d+(?:\.\d+)?)\s*[:：]\s*(-?\d+(?:\.\d+)?)", text
    )
    if ratios:
        left, right = ratios[-1]
        return f"ratio:{left}:{right}"
    temperatures = re.findall(r"(-?\d+(?:\.\d+)?)\s*(?:℃|°\s*c)", text)
    if temperatures:
        return f"temperature:{temperatures[-1]}c"
    ox = [
        "o" if character in {"○", "◯", "o"} else "x"
        for character in text
        if character in {"○", "◯", "o", "×", "x"}
    ]
    if len(ox) >= 2:
        return "ox:" + "".join(ox)
    right_hand = text.rsplit("=", 1)[-1]
    cleaned = _normalize_answer(right_hand)
    if cleaned and (
        (any(character.isdigit() for character in cleaned) and len(cleaned) <= 20)
        or len(cleaned) <= 6
    ):
        return "literal:" + cleaned
    return None


def _explicit_answers_conflict(
    quick_values: Sequence[str], detail_values: Sequence[str]
) -> bool:
    if not quick_values or not detail_values:
        return False
    if {
        _normalize_answer(value) for value in quick_values
    } & {_normalize_answer(value) for value in detail_values}:
        return False
    quick_canonical = {
        canonical for value in quick_values if (canonical := _canonical_answer(value))
    }
    detail_canonical = {
        canonical for value in detail_values if (canonical := _canonical_answer(value))
    }
    return bool(
        quick_canonical
        and detail_canonical
        and quick_canonical.isdisjoint(detail_canonical)
    )


def align_solution_assets(
    problems: Sequence[ProblemItem],
    assets: Sequence[SolutionAsset],
    verifications: Mapping[tuple[str, str], PairVerification] | None = None,
    *,
    min_fingerprint: float = 0.25,
    min_confidence: float = MIN_LINK_CONFIDENCE,
    min_runner_up_margin: float = 0.15,
    scope_alignment: "ScopeAlignmentLike | None" = None,
    episode_assets: Mapping[str, tuple[SolutionAsset, ...]] | None = None,
    admitted_pairs: frozenset[PairKey] = frozenset(),
    problem_source_sha256: str | None = None,
    solution_source_sha256: str | None = None,
) -> tuple[LinkDecision, ...]:
    if scope_alignment is not None and episode_assets is None:
        raise ValueError("scope_alignment requires episode_assets")
    if len({item.item_id for item in problems}) != len(problems):
        raise ValueError("problem ids must be unique")
    if len({asset.asset_id for asset in assets}) != len(assets):
        raise ValueError("solution asset ids must be unique")
    if admitted_pairs:
        eligible = {(pair.problem_id, pair.asset_id) for pair in expanded_candidate_pairs(
            problems, assets, problem_source_sha256=problem_source_sha256,
            solution_source_sha256=solution_source_sha256)}
        if scope_alignment is not None and episode_assets is not None:
            eligible.update((pair.problem_id, pair.asset_id) for pair in scope_candidate_pairs(
                problems, tuple(LinkDecision(item.item_id, LinkStatus.REVIEW) for item in problems),
                scope_alignment, episode_assets))
        admitted_pairs = admitted_pairs.intersection(eligible)
    verifications = verifications or {}
    quick_assets = [
        asset
        for asset in assets
        if asset.role in {SolutionRole.QUICK_ANSWER, SolutionRole.MIXED}
    ]
    detail_assets = [
        asset
        for asset in assets
        if asset.role in {SolutionRole.DETAILED_SOLUTION, SolutionRole.MIXED}
    ]
    blocked_pairs: set[PairKey] = set()
    quick = _align_role(
        problems,
        quick_assets,
        verifications,
        blocked_pairs,
        admitted_pairs,
    )
    detail = _align_role(
        problems,
        detail_assets,
        verifications,
        blocked_pairs,
        admitted_pairs,
    )
    blocked_pairs = {
        pair_key
        for pair_key, verification in verifications.items()
        if not verification.same_item and not verification.explicit_answer_conflict
    }
    if blocked_pairs:
        quick = _align_role(
            problems,
            quick_assets,
            verifications,
            blocked_pairs,
            admitted_pairs,
        )
        detail = _align_role(
            problems,
            detail_assets,
            verifications,
            blocked_pairs,
            admitted_pairs,
        )
    compatible = {
        problem.item_id: tuple(dict.fromkeys((*_compatible_assets(problem, assets),
            *(asset for asset in assets if (problem.item_id, asset.asset_id) in admitted_pairs))))
        for problem in problems
    }

    decisions: list[LinkDecision] = []
    for problem in sorted(problems, key=lambda item: item.sequence):
        all_candidates = compatible[problem.item_id]
        anchor_reason = _anchor_review_reason(problem)
        anchorless_inline = bool(
            problem.anchor is None
            and all_candidates
            and all(
                asset.source_document == "problem" and asset.review_reason is not None
                for asset in all_candidates
            )
        )
        if anchor_reason is not None and not anchorless_inline and not any(
            pair[0] == problem.item_id for pair in admitted_pairs
        ):
            decisions.append(
                LinkDecision(
                    problem.item_id,
                    LinkStatus.REVIEW,
                    review_reasons=(anchor_reason,),
                )
            )
            continue
        eligibility_reasons = []
        if not problem.crop_is_search_eligible:
            eligibility_reasons.append("problem_crop_unverified")
        if problem.cache_only_detailed_review:
            eligibility_reasons.append("cached_detailed_solution_review")
        if problem.anchor is not None:
            eligibility_reasons.extend(problem.anchor.review_reasons)
        if not all_candidates:
            decisions.append(
                LinkDecision(
                    problem.item_id,
                    LinkStatus.REVIEW if eligibility_reasons else LinkStatus.UNMATCHED,
                    review_reasons=tuple(dict.fromkeys(eligibility_reasons))
                    or ("no_candidate",),
                )
            )
            continue

        review_only_reasons = [
            asset.review_reason
            for asset in all_candidates
            if asset.review_reason is not None
        ]
        if any(_shared_parent_compatible(problem, asset) for asset in all_candidates):
            review_only_reasons.append("shared_parent_solution")
        if (review_only_reasons and not any(
            asset.review_reason is None and (_strict_candidate_compatible(problem, asset)
                or (problem.item_id, asset.asset_id) in admitted_pairs)
            for asset in all_candidates
        )) or not problem.crop_is_search_eligible:
            evidence: list[str] = []
            confidence_values: list[float] = []
            for asset in all_candidates:
                similarity = fingerprint_similarity(problem.fingerprints, asset.fingerprints)
                evidence.append(f"{asset.asset_id}:fingerprint={similarity:.3f}")
                confidence_values.extend((asset.confidence, similarity))
                verification = verifications.get((problem.item_id, asset.asset_id))
                if verification is not None:
                    confidence_values.append(verification.confidence)
                    evidence.append(
                        f"{asset.asset_id}:{verification.verifier}="
                        f"{str(verification.same_item).lower()}"
                    )
            decisions.append(
                LinkDecision(
                    problem.item_id,
                    LinkStatus.REVIEW,
                    candidate_ids=tuple(asset.asset_id for asset in all_candidates),
                    solution_printed_pages=tuple(
                        sorted(
                            {
                                page
                                for asset in all_candidates
                                for page in asset.printed_pages
                            }
                        )
                    ),
                    confidence=min(confidence_values, default=0.0),
                    evidence=tuple(evidence),
                    review_reasons=tuple(
                        dict.fromkeys((*eligibility_reasons, *review_only_reasons))
                    ),
                )
            )
            continue

        viable_candidates = tuple(
            asset
            for asset in all_candidates
            if (problem.item_id, asset.asset_id) not in blocked_pairs
        )
        rejected_candidates = tuple(
            asset
            for asset in all_candidates
            if (problem.item_id, asset.asset_id) in blocked_pairs
        )
        quick_asset = quick.selected.get(problem.item_id)
        detail_asset = detail.selected.get(problem.item_id)
        selected_assets = tuple(
            dict.fromkeys(
                asset
                for asset in (quick_asset, detail_asset)
                if asset is not None
            )
        )
        possible_assets = tuple(
            dict.fromkeys(
                quick.possible.get(problem.item_id, ())
                + detail.possible.get(problem.item_id, ())
            )
        )
        candidate_ids = tuple(
            asset.asset_id
            for asset in possible_assets or viable_candidates or all_candidates
        )
        if rejected_candidates:
            candidate_ids = tuple(
                dict.fromkeys(
                    (*candidate_ids, *(asset.asset_id for asset in rejected_candidates))
                )
            )
        if not selected_assets:
            failed_reasons = tuple(
                f"{asset.asset_id}:independent_verification_failed"
                for asset in rejected_candidates
            )
            missing_reasons = tuple(
                f"{asset.asset_id}:independent_verification_missing"
                for asset in viable_candidates
                if (problem.item_id, asset.asset_id) not in verifications
            )
            ambiguity_reasons = (
                ("ambiguous_or_non_monotonic_candidates",)
                if viable_candidates
                else ()
            )
            decisions.append(
                LinkDecision(
                    problem.item_id,
                    LinkStatus.REVIEW,
                    candidate_ids=candidate_ids,
                    review_reasons=tuple(
                        dict.fromkeys(
                            (
                                *eligibility_reasons,
                                *failed_reasons,
                                *missing_reasons,
                                *ambiguity_reasons,
                            )
                        )
                    )
                    or ("no_viable_candidate",),
                )
            )
            continue

        detail_verification = (
            verifications.get((problem.item_id, detail_asset.asset_id))
            if detail_asset is not None
            else None
        )
        unique_strong_detail = bool(
            detail_asset is not None
            and detail.possible.get(problem.item_id) == (detail_asset,)
            and _strong_verification(detail_verification, min_confidence, problem, detail_asset)
        )
        if unique_strong_detail:
            eligibility_reasons = [
                reason
                for reason in eligibility_reasons
                if reason != "anchor_window_requires_verification"
            ]

        reasons: list[str] = list(eligibility_reasons) + list(review_only_reasons)
        evidence: list[str] = [
            f"{asset.asset_id}:independent_verification_rejected"
            for asset in rejected_candidates
        ]
        confidence_values: list[float] = []
        quick_candidates = [
            asset
            for asset in viable_candidates
            if asset.role in {SolutionRole.QUICK_ANSWER, SolutionRole.MIXED}
        ]
        detail_candidates = [
            asset
            for asset in viable_candidates
            if asset.role in {SolutionRole.DETAILED_SOLUTION, SolutionRole.MIXED}
        ]
        if quick_candidates and quick_asset is None:
            reasons.append("quick_answer_ambiguous_or_non_monotonic")
            reasons.extend(
                f"{asset.asset_id}:independent_verification_missing"
                for asset in quick_candidates
                if (problem.item_id, asset.asset_id) not in verifications
            )
        if detail_candidates and detail_asset is None:
            reasons.append("detailed_solution_ambiguous_or_non_monotonic")
            reasons.extend(
                f"{asset.asset_id}:independent_verification_missing"
                for asset in detail_candidates
                if (problem.item_id, asset.asset_id) not in verifications
            )
        if (
            quick_candidates
            and quick_asset is None
            or detail_candidates
            and detail_asset is None
        ):
            candidate_ids = tuple(
                dict.fromkeys(
                    (*candidate_ids, *(asset.asset_id for asset in all_candidates))
                )
            )
        quick_quality: dict[str, bool] = {}
        detail_quality: dict[str, bool] = {}
        for asset in selected_assets:
            common_reasons: list[str] = []
            similarity = fingerprint_similarity(problem.fingerprints, asset.fingerprints)
            verification = verifications.get((problem.item_id, asset.asset_id))
            quick_margin = (
                quick.runner_up_margin.get(problem.item_id)
                if quick_asset is asset
                else None
            )
            detail_margin = (
                detail.runner_up_margin.get(problem.item_id)
                if detail_asset is asset
                else None
            )
            evidence.append(f"{asset.asset_id}:fingerprint={similarity:.3f}")
            if quick_margin is not None:
                evidence.append(
                    f"{asset.asset_id}:quick_runner_up_margin={quick_margin:.3f}"
                )
            if detail_margin is not None:
                evidence.append(
                    f"{asset.asset_id}:detail_runner_up_margin={detail_margin:.3f}"
                )
            strong_verification = _strong_verification(
                verification, min_confidence, problem, asset
            )
            confidence_values.append(
                max(asset.confidence, verification.confidence)
                if strong_verification and verification is not None
                else asset.confidence
            )
            if similarity < min_fingerprint:
                if strong_verification:
                    confidence_values.append(verification.confidence)
                    evidence.append(
                        f"{asset.asset_id}:fingerprint_confirmed_by_{verification.verifier}"
                    )
                else:
                    confidence_values.append(similarity)
                    common_reasons.append(
                        f"{asset.asset_id}:fingerprint_below_threshold"
                    )
            else:
                confidence_values.append(similarity)
            if asset.confidence < min_confidence and not strong_verification:
                common_reasons.append(
                    f"{asset.asset_id}:asset_confidence_below_threshold"
                )
            elif asset.confidence < min_confidence:
                evidence.append(
                    f"{asset.asset_id}:asset_confidence_confirmed_by_{verification.verifier}"
                )
            if verification is None:
                common_reasons.append(
                    f"{asset.asset_id}:independent_verification_missing"
                )
            else:
                confidence_values.append(verification.confidence)
                evidence.append(
                    f"{asset.asset_id}:{verification.verifier}={str(verification.same_item).lower()}"
                )
                if not strong_verification:
                    if verification.quality_reason:
                        common_reasons.append(f"{asset.asset_id}:{verification.quality_reason}")
                    common_reasons.append(
                        f"{asset.asset_id}:independent_verification_failed"
                    )
            quick_reasons = list(common_reasons)
            detail_reasons = list(common_reasons)
            if quick_margin is not None and quick_margin < min_runner_up_margin:
                quick_reasons.append(
                    f"{asset.asset_id}:quick_runner_up_margin_below_threshold"
                )
            if detail_margin is not None and detail_margin < min_runner_up_margin:
                detail_reasons.append(
                    f"{asset.asset_id}:detail_runner_up_margin_below_threshold"
                )
            if quick_margin is not None:
                quick_quality[asset.asset_id] = not quick_reasons
                reasons.extend(quick_reasons)
            if detail_margin is not None:
                detail_quality[asset.asset_id] = not detail_reasons
                reasons.extend(detail_reasons)

        role_links_allowed = not eligibility_reasons
        detail_verified = bool(
            role_links_allowed
            and detail_asset is not None
            and detail_quality.get(detail_asset.asset_id, False)
        )
        quick_verified = bool(
            role_links_allowed
            and quick_asset is not None
            and quick_quality.get(quick_asset.asset_id, False)
            and (detail_verified or problem.crop_verified and all(
                quality.mapping(quality.mapping(verifications[(problem.item_id, quick_asset.asset_id)].quality_receipt).get('result')).get(field) == 'match'
                for field in ('hierarchy_scope_match', 'fingerprint_match')
            ))
        )
        if quick_asset is not None and not quick_verified:
            reasons.append(
                "quick_only" if detail_asset is None else "quick_requires_verified_detail"
            )

        conflict_candidates = tuple(
            asset
            for asset in all_candidates
            if asset.role in {SolutionRole.QUICK_ANSWER, SolutionRole.MIXED}
            and detail_verified
            and detail_asset is not None
            and (
                (verification := verifications.get((problem.item_id, asset.asset_id)))
                is not None
                and verification.explicit_answer_conflict
                and _confirmed_verification(verification, problem, asset, conflict=True, corroborating=detail_asset)
                and verification.corroborating_asset_id == detail_asset.asset_id
            )
        )
        evidence.extend(
            f"{asset.asset_id}:explicit_answer_conflict"
            for asset in conflict_candidates
        )
        confidence_values.extend(
            value
            for asset in conflict_candidates
            for value in (
                asset.confidence,
                verifications[(problem.item_id, asset.asset_id)].confidence,
            )
        )

        quick_answers = tuple(
            asset.explicit_answer
            for asset in (quick_asset,)
            if asset is not None and asset.explicit_answer
        )
        detail_answers = tuple(
            asset.explicit_answer
            for asset in (detail_asset,)
            if asset is not None and asset.explicit_answer
        )
        pages = tuple(
            sorted(
                {
                    page
                    for asset in (*selected_assets, *conflict_candidates)
                    for page in asset.printed_pages
                }
            )
        )
        confidence = min(confidence_values, default=0.0)
        answer_conflict = bool(
            conflict_candidates
            or quick_verified
            and detail_verified
            and _explicit_answers_conflict(quick_answers, detail_answers)
        )
        if answer_conflict:
            decisions.append(
                LinkDecision(
                    problem.item_id,
                    LinkStatus.CONFLICT,
                    candidate_ids=tuple(
                        dict.fromkeys(
                            asset.asset_id
                            for asset in (*selected_assets, *conflict_candidates)
                        )
                    ),
                    solution_printed_pages=pages,
                    confidence=confidence,
                    evidence=tuple(evidence),
                    review_reasons=("explicit_answer_conflict",),
                )
            )
        elif reasons:
            candidate_ids = tuple(
                dict.fromkeys(
                    (*candidate_ids, *(asset.asset_id for asset in all_candidates))
                )
            )
            if quick_verified or detail_verified:
                decisions.append(
                    LinkDecision(
                        problem.item_id,
                        LinkStatus.VERIFIED,
                        answer_ids=(quick_asset.asset_id,) if quick_verified else (),
                        explanation_ids=(
                            (detail_asset.asset_id,) if detail_verified else ()
                        ),
                        candidate_ids=candidate_ids,
                        solution_printed_pages=pages,
                        confidence=confidence,
                        evidence=tuple(evidence),
                        review_reasons=tuple(dict.fromkeys(reasons)),
                    )
                )
            else:
                decisions.append(
                    LinkDecision(
                        problem.item_id,
                        LinkStatus.REVIEW,
                        candidate_ids=candidate_ids,
                        solution_printed_pages=pages,
                        confidence=confidence,
                        evidence=tuple(evidence),
                        review_reasons=tuple(dict.fromkeys(reasons)),
                    )
                )
        else:
            decisions.append(
                LinkDecision(
                    problem.item_id,
                    LinkStatus.VERIFIED,
                    answer_ids=(quick_asset.asset_id,) if quick_asset else (),
                    explanation_ids=(detail_asset.asset_id,) if detail_asset else (),
                    candidate_ids=tuple(asset.asset_id for asset in selected_assets),
                    solution_printed_pages=pages,
                    confidence=confidence,
                    evidence=tuple(evidence),
                )
            )
    if scope_alignment is not None and episode_assets is not None:
        return augment_decisions_with_episode_candidates(
            tuple(decisions), problems, scope_alignment, episode_assets
        )
    return tuple(decisions)


_EPISODE_BLOCKER_MARKERS = (
    "scope_alignment_ambiguous",
    "scope_alignment_untrusted",
    "scope_alignment_unresolved",
    "episode_coverage_incomplete",
    "scope_alignment_low_margin",
    "pooled_quick_candidate",
    "quick_uncorroborated",
)

_EPISODE_ROLE_POOLS = {
    EPISODE_ROLE_DETAILED: {SolutionRole.DETAILED_SOLUTION, SolutionRole.MIXED},
    EPISODE_ROLE_QUICK: {SolutionRole.QUICK_ANSWER, SolutionRole.MIXED},
}


def scope_candidate_pairs(
    problems: Sequence[ProblemItem],
    legacy_decisions: Sequence[LinkDecision],
    scope_alignment: ScopeAlignmentLike,
    episode_assets: Mapping[str, tuple[SolutionAsset, ...]],
) -> tuple[ScopeCandidatePair, ...]:
    """Return only hard-selected, provider-eligible Phase 2c scope pairs.

    Allows-only entries are deliberately absent: paying to verify an
    untrusted/ambiguous scope cannot make that scope auto-capable.
    """

    decisions = {decision.item_id: decision for decision in legacy_decisions}
    accumulated: dict[tuple[str, str], dict[str, Any]] = {}
    for problem in sorted(problems, key=lambda value: value.sequence):
        decision = decisions.get(problem.item_id)
        if (
            decision is None
            or decision.status == LinkStatus.CONFLICT
            or not problem.crop_is_search_eligible
            or problem.cache_only_detailed_review
            or _anchor_review_reason(problem) == 'anchor_provenance_mismatch'
        ):
            continue
        for alignment_role, role_pool in _EPISODE_ROLE_POOLS.items():
            if (decision.explanation_ids if alignment_role == EPISODE_ROLE_DETAILED else decision.answer_ids):
                continue
            entry = scope_alignment.get(problem.scope_id, alignment_role)
            if (
                entry is None
                or entry.aligned_episode_id is None
                or entry.ambiguous
                or any(
                    marker in entry.evidence for marker in _EPISODE_BLOCKER_MARKERS
                )
            ):
                continue
            episode_id = entry.aligned_episode_id
            for asset in episode_assets.get(episode_id, ()):
                if (
                    asset.role not in role_pool
                    or asset.role == SolutionRole.MIXED
                    or asset.review_reason is not None
                    or asset.source_document != 'solution'
                    or asset.scope_id != f'episode:{episode_id}'
                    or asset.parent_number != problem.parent_number
                    or asset.subquestion_label != problem.subquestion_label
                ):
                    continue
                key = (problem.item_id, asset.asset_id)
                value = accumulated.setdefault(
                    key,
                    {
                        "problem": problem,
                        "asset": asset,
                        "episode_id": episode_id,
                        "roles": set(),
                        "evidence": set(),
                    },
                )
                if value["episode_id"] != episode_id:
                    raise ValueError("one scope asset mapped to multiple solution episodes")
                value["roles"].add(alignment_role)
                value["evidence"].update(entry.evidence)
    return tuple(
        ScopeCandidatePair(
            problem_id=problem_id,
            asset_id=asset_id,
            role=value["asset"].role,
            fingerprint_similarity=fingerprint_similarity(
                value["problem"].fingerprints, value["asset"].fingerprints
            ),
            problem_scope_id=value["problem"].scope_id,
            solution_scope_id=value["episode_id"],
            alignment_roles=tuple(sorted(value["roles"])),
            alignment_evidence=tuple(sorted(value["evidence"])),
        )
        for (problem_id, asset_id), value in sorted(accumulated.items())
    )


def augment_decisions_with_episode_candidates(
    decisions: tuple[LinkDecision, ...],
    problems: Sequence[ProblemItem],
    scope_alignment: ScopeAlignmentLike,
    episode_assets: Mapping[str, tuple[SolutionAsset, ...]],
) -> tuple[LinkDecision, ...]:
    """Supplement empty-candidate REVIEW/UNMATCHED decisions with episode
    candidates.

    Fail-closed by construction: decisions that already carry candidates —
    including every auto-VERIFIED and every anchor-window decision — are
    returned untouched byte-for-byte, and supplemented decisions are always
    REVIEW (episode candidates have no verification evidence, so they can
    never become VERIFIED in this phase).
    """
    problems_by_id = {problem.item_id: problem for problem in problems}
    out: list[LinkDecision] = []
    for decision in decisions:
        problem = problems_by_id.get(decision.item_id)
        if (
            problem is None
            or decision.status not in (LinkStatus.REVIEW, LinkStatus.UNMATCHED)
            or decision.candidate_ids
        ):
            out.append(decision)
            continue
        additions: list[SolutionAsset] = []
        reasons: list[str] = list(decision.review_reasons)
        evidence: list[str] = list(decision.evidence)
        for role_name, role_pool in _EPISODE_ROLE_POOLS.items():
            entry = scope_alignment.get(problem.scope_id, role_name)
            if entry is None:
                continue
            if entry.aligned_episode_id is not None:
                targets: list[tuple[str, str | None]] = [
                    (entry.aligned_episode_id, None)
                ]
            else:
                blocker = next(
                    (
                        marker
                        for marker in _EPISODE_BLOCKER_MARKERS
                        if marker in entry.evidence
                    ),
                    "scope_alignment_unaligned",
                )
                targets = [(episode_id, blocker) for episode_id in entry.allows]
            for episode_id, blocker in targets:
                for asset in episode_assets.get(episode_id, ()):
                    if asset.role not in role_pool:
                        continue
                    if asset.parent_number != problem.parent_number:
                        continue
                    if asset.subquestion_label != problem.subquestion_label:
                        continue
                    if any(existing.asset_id == asset.asset_id for existing in additions):
                        continue
                    additions.append(asset)
                    evidence.append(f"{asset.asset_id}:episode={episode_id}")
                    if asset.review_reason is not None:
                        reasons.append(f"{asset.asset_id}:{asset.review_reason}")
                    if blocker is not None:
                        reasons.append(f"{asset.asset_id}:{blocker}")
                    else:
                        reasons.append(
                            f"{asset.asset_id}:independent_verification_missing"
                        )
                    anchor = problem.anchor
                    if (
                        anchor is not None
                        and anchor.resolved
                        and asset.printed_pages
                        and not all(
                            anchor.page_range.contains(page)
                            for page in asset.printed_pages
                        )
                    ):
                        reasons.append(f"{asset.asset_id}:anchor_disagreement")
        if not additions:
            out.append(decision)
            continue
        additions.sort(key=lambda asset: (asset.sequence, asset.asset_id))
        out.append(
            LinkDecision(
                decision.item_id,
                LinkStatus.REVIEW,
                answer_ids=(),
                explanation_ids=(),
                candidate_ids=tuple(asset.asset_id for asset in additions),
                solution_printed_pages=tuple(
                    sorted(
                        {
                            page
                            for asset in additions
                            for page in asset.printed_pages
                        }
                    )
                ),
                confidence=0.0,
                evidence=tuple(dict.fromkeys(evidence)),
                review_reasons=tuple(dict.fromkeys(reasons)),
            )
        )
    return tuple(out)


@dataclass(frozen=True)
class ProblemIndex:
    source_sha256: str
    page_map: tuple[PageIdentity, ...]
    items: tuple[ProblemItem, ...]

    def __post_init__(self) -> None:
        if len({page.pdf_index for page in self.page_map}) != len(self.page_map):
            raise ValueError("problem page_map pdf_index values must be unique")
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("problem index item ids must be unique")

    def signature(self) -> dict[str, Any]:
        return {
            "source_sha256": self.source_sha256,
            "page_map": [page.to_dict() for page in self.page_map],
            "items": [item.signature() for item in self.items],
        }


@dataclass(frozen=True)
class SolutionIndex:
    source_sha256: str
    page_map: tuple[PageIdentity, ...]
    assets: tuple[SolutionAsset, ...]

    def __post_init__(self) -> None:
        if len({page.pdf_index for page in self.page_map}) != len(self.page_map):
            raise ValueError("solution page_map pdf_index values must be unique")
        if len({asset.asset_id for asset in self.assets}) != len(self.assets):
            raise ValueError("solution index asset ids must be unique")
        known_pages = set(self.page_map)
        if any(
            part.page not in known_pages
            for asset in self.assets
            if asset.source_document == "solution"
            for part in asset.parts
        ):
            raise ValueError("solution asset part is outside solution page_map")

    def signature(self) -> dict[str, Any]:
        return {
            "source_sha256": self.source_sha256,
            "page_map": [page.to_dict() for page in self.page_map],
            "assets": [asset.signature() for asset in self.assets],
        }


def make_cache_key(
    problem_source_sha256: str,
    solution_source_sha256: str,
    *,
    crop_dpi: int,
    model: str | None,
    stage_version: str,
    input_signature: Mapping[str, Any] | None = None,
) -> str:
    if crop_dpi not in {300, 450}:
        raise ValueError("crop_dpi must be 300 or 450")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "core_version": CORE_VERSION,
        "problem_source_sha256": problem_source_sha256,
        "solution_source_sha256": solution_source_sha256,
        "search_policy": SEARCH_POLICY,
        "crop_dpi": crop_dpi,
        "model": model,
        "stage_version": stage_version,
        "input_signature": input_signature or {},
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_link_manifest(
    problem: ProblemIndex,
    solution: SolutionIndex,
    decisions: Sequence[LinkDecision],
    assets: Mapping[str, Mapping[str, Any]],
    *,
    cache_key: str,
    problem_path: Path | None = None,
    solution_path: Path,
    scope_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    decision_by_id = {decision.item_id: decision for decision in decisions}
    expected = {item.item_id for item in problem.items}
    if set(decision_by_id) != expected or len(decision_by_id) != len(decisions):
        raise ManifestError("every problem item must have exactly one link decision")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "asset_contract": ASSET_SELECTION_CONTRACT,
        "cache_key": cache_key,
        "created_at": utc_now(),
        "problem": {
            "source_sha256": problem.source_sha256,
            **({"source_pdf": str(problem_path)} if problem_path is not None else {}),
            "page_map": [page.to_dict() for page in problem.page_map],
            "item_ids": [item.item_id for item in problem.items],
        },
        "solution": {
            "source_sha256": solution.source_sha256,
            "source_pdf": str(solution_path),
            "page_map": [page.to_dict() for page in solution.page_map],
        },
        "assets": {key: dict(value) for key, value in assets.items()},
        "items": {
            item.item_id: serialize_link_decision(
                decision_by_id[item.item_id], assets
            )
            for item in problem.items
        },
    }
    if scope_contract is not None:
        # Additive (schema v1 preserved): alignment provenance for B4 —
        # legacy readers ignore it, replay tools verify against it.
        manifest["scope_contract"] = dict(scope_contract)
    validate_link_manifest(manifest)
    return manifest


def _canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_scope_contract(
    contract: Any, manifest: Mapping[str, Any]
) -> None:
    if contract is None:
        return
    if not isinstance(contract, dict):
        raise ManifestError("scope_contract must be an object")
    required = {
        "contract",
        "bundle_signature",
        "alignment_sha256",
        "alignment",
        "provenance",
    }
    if set(contract) != required:
        raise ManifestError("scope_contract fields are invalid")
    if contract.get("contract") != SCOPE_ALIGNMENT_CONTRACT:
        raise ManifestError("unsupported scope alignment contract")
    if not _valid_sha256(contract.get("bundle_signature")):
        raise ManifestError("scope bundle signature is invalid")
    alignment = contract.get("alignment")
    if (
        not isinstance(alignment, dict)
        or alignment.get("scope_alignment_version")
        != SCOPE_ALIGNMENT_CONTRACT_VERSION
        or not isinstance(alignment.get("entries"), list)
        or not isinstance(alignment.get("edges"), list)
        or contract.get("alignment_sha256")
        != _canonical_json_sha256(alignment)
    ):
        raise ManifestError("scope alignment snapshot is invalid")
    provenance = contract.get("provenance")
    provenance_fields = {
        "alignment_index_path",
        "alignment_index_sha256",
        "problem_source_sha256",
        "solution_source_sha256",
        "scope_index_version",
        "scope_alignment_version",
        "core_version",
        "runtime_version",
        "input_cache_versions",
        "input_inventory_sha256",
        "indexed_pages",
        "missing_pages",
    }
    profiled_fields = provenance_fields | {"provider_profile", "consensus_path"}
    if not isinstance(provenance, dict) or frozenset(provenance) not in {
        frozenset(provenance_fields),
        frozenset(profiled_fields),
    }:
        raise ManifestError("scope provenance fields are invalid")
    profile = provenance.get("provider_profile", "full")
    expected_consensus = (
        "solutions/detections/solution-blocks/consensus"
        if profile == "full"
        else "solutions/detections/solution-blocks/consensus/codex_only"
    )
    if profile not in {"full", "codex_only"} or provenance.get(
        "consensus_path", expected_consensus
    ) != expected_consensus:
        raise ManifestError("scope consensus profile provenance is invalid")
    relative = Path(str(provenance.get("alignment_index_path", "")))
    if (
        not relative.parts
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix()
        != (
            "solutions/alignment_index.json"
            if profile == "full"
            else "solutions/provider_profiles/codex_only/alignment_index.json"
        )
    ):
        raise ManifestError("alignment index path is invalid")
    if not _valid_sha256(provenance.get("alignment_index_sha256")):
        raise ManifestError("alignment index SHA-256 is invalid")
    if (
        provenance.get("problem_source_sha256")
        != manifest.get("problem", {}).get("source_sha256")
        or provenance.get("solution_source_sha256")
        != manifest.get("solution", {}).get("source_sha256")
    ):
        raise ManifestError("scope provenance source SHA-256 is inconsistent")
    if (
        provenance.get("scope_index_version") != SCOPE_INDEX_CONTRACT_VERSION
        or provenance.get("scope_alignment_version")
        != SCOPE_ALIGNMENT_CONTRACT_VERSION
        or not isinstance(provenance.get("core_version"), str)
        or not provenance["core_version"]
        or not isinstance(provenance.get("runtime_version"), str)
        or not provenance["runtime_version"]
        or not _valid_sha256(provenance.get("input_inventory_sha256"))
    ):
        raise ManifestError("scope provenance versions are invalid")
    cache_versions = provenance.get("input_cache_versions")
    if (
        not isinstance(cache_versions, dict)
        or set(cache_versions)
        != {"ai_contract_cache_version", "boundary_consensus_version"}
        or any(not isinstance(value, str) or not value for value in cache_versions.values())
    ):
        raise ManifestError("scope input cache versions are invalid")
    page_sets: list[set[int]] = []
    for field in ("indexed_pages", "missing_pages"):
        pages = provenance.get(field)
        if (
            not isinstance(pages, list)
            or any(not isinstance(page, int) or page < 0 for page in pages)
            or len(pages) != len(set(pages))
        ):
            raise ManifestError(f"scope {field} is invalid")
        page_sets.append(set(pages))
    if page_sets[0] & page_sets[1]:
        raise ManifestError("scope page coverage overlaps")


def _validate_manual_propagation_header(manifest: Mapping[str, Any]) -> None:
    fields = {
        "manual_propagation_contract",
        "alignment_snapshot",
        "manual_constraints",
        "manual_confirmation_events",
    }
    present = fields & set(manifest)
    unavailable = manifest.get("propagation_unavailable")
    if unavailable is not None:
        if present:
            raise ManifestError("propagation availability fields conflict")
        if (
            not isinstance(unavailable, Mapping)
            or set(unavailable) != {"reason"}
            or unavailable.get("reason") not in PROPAGATION_UNAVAILABLE_REASONS
            or not isinstance(manifest.get("scope_contract"), Mapping)
        ):
            raise ManifestError("propagation_unavailable reason is invalid")
        return
    if not present:
        return
    if present != fields or manifest.get("manual_propagation_contract") != "fixed-edge-v1":
        raise ManifestError("manual propagation header is incomplete")
    reference = manifest.get("alignment_snapshot")
    if not isinstance(reference, Mapping) or set(reference) != {
        "relative_path",
        "sha256",
        "schema_version",
    }:
        raise ManifestError("alignment snapshot reference is invalid")
    relative = Path(str(reference.get("relative_path", "")))
    expected_relative = (
        f"solutions/alignment_snapshot-{reference.get('sha256')}.json"
    )
    if (
        relative.as_posix() != expected_relative
        or relative.is_absolute()
        or ".." in relative.parts
        or not _valid_sha256(reference.get("sha256"))
        or reference.get("schema_version") != 1
    ):
        raise ManifestError("alignment snapshot reference is invalid")
    if not isinstance(manifest.get("scope_contract"), Mapping):
        raise ManifestError("manual propagation requires a scope contract")
    if not isinstance(manifest.get("manual_constraints"), list) or not isinstance(
        manifest.get("manual_confirmation_events"), list
    ):
        raise ManifestError("manual propagation audit fields must be arrays")


def validate_link_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict):
        raise ManifestError("link manifest must be an object")
    required = {
        "schema_version",
        "cache_key",
        "created_at",
        "problem",
        "solution",
        "assets",
        "items",
    }
    if not required.issubset(manifest) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError("link manifest header is invalid")
    if not isinstance(manifest["cache_key"], str) or not manifest["cache_key"]:
        raise ManifestError("cache_key is required")
    asset_contract = manifest.get("asset_contract")
    if asset_contract not in {None, ASSET_SELECTION_CONTRACT}:
        raise ManifestError("unsupported asset selection contract")
    strict_assets = asset_contract == ASSET_SELECTION_CONTRACT
    source_bound_assets = strict_assets and isinstance(
        manifest.get("problem", {}).get("source_pdf"), str
    )
    if not isinstance(manifest["assets"], dict) or not isinstance(manifest["items"], dict):
        raise ManifestError("assets/items must be objects")
    asset_ids = set(manifest["assets"])
    if strict_assets:
        for asset_id, asset in manifest["assets"].items():
            if not isinstance(asset_id, str) or not asset_id or not isinstance(asset, dict):
                raise ManifestError("invalid solution asset")
            try:
                SolutionRole(str(asset.get("kind")))
            except ValueError as exc:
                raise ManifestError("solution asset kind is invalid") from exc
            if asset.get("selection_status") not in {
                "auto_verified",
                "review_only",
                "manual_verified",
            }:
                raise ManifestError("solution asset selection_status is invalid")
            if not asset.get("output_file"):
                raise ManifestError("materialized solution assets require output_file")
            relative = Path(str(asset["output_file"]))
            if relative.is_absolute() or ".." in relative.parts:
                raise ManifestError("asset output_file must be a safe relative path")
            if source_bound_assets:
                if asset.get("source_document") not in {"problem", "solution"}:
                    raise ManifestError("materialized asset source_document is invalid")
                if not _valid_sha256(asset.get("source_sha256")):
                    raise ManifestError("materialized asset source_sha256 is invalid")
    for document_name in ("problem", "solution"):
        document = manifest.get(document_name)
        if not isinstance(document, dict) or not isinstance(document.get("page_map"), list):
            raise ManifestError(f"{document_name}.page_map is required")
        for page in document["page_map"]:
            try:
                PageIdentity(
                    page["pdf_index"],
                    page["pdf_page"],
                    page.get("printed_page"),
                    page["segment_id"],
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ManifestError(f"invalid {document_name} page map") from exc
    _validate_scope_contract(manifest.get("scope_contract"), manifest)
    _validate_manual_propagation_header(manifest)
    expected_item_ids = manifest["problem"].get("item_ids")
    if (
        not isinstance(expected_item_ids, list)
        or any(not isinstance(item_id, str) or not item_id for item_id in expected_item_ids)
        or len(set(expected_item_ids)) != len(expected_item_ids)
        or set(expected_item_ids) != set(manifest["items"])
    ):
        raise ManifestError("problem item_ids and link items must match exactly")
    required_item_fields = {
        "status",
        "answer_ids",
        "explanation_ids",
        "candidate_ids",
        "solution_printed_pages",
        "confidence",
        "evidence",
        "review_reasons",
    }
    for item_id, item in manifest["items"].items():
        if not item_id or not isinstance(item, dict) or not required_item_fields.issubset(item):
            raise ManifestError("invalid link item")
        try:
            status = LinkStatus(item["status"])
        except ValueError as exc:
            raise ManifestError("invalid link status") from exc
        def ids(field: str) -> list[str]:
            value = item[field]
            if strict_assets and (
                not isinstance(value, list)
                or any(not isinstance(asset_id, str) or not asset_id for asset_id in value)
                or len(value) != len(set(value))
            ):
                raise ManifestError(f"{field} must contain unique asset ids")
            return list(value)

        linked = ids("answer_ids") + ids("explanation_ids")
        candidates = ids("candidate_ids")
        allowed_link_roles = {
            "answer_ids": {SolutionRole.QUICK_ANSWER, SolutionRole.MIXED},
            "explanation_ids": {
                SolutionRole.DETAILED_SOLUTION,
                SolutionRole.MIXED,
            },
        }
        for field, allowed_roles in allowed_link_roles.items():
            for asset_id in ids(field):
                asset_role = _manifest_asset_role(manifest["assets"].get(asset_id))
                if strict_assets and asset_role is None:
                    raise ManifestError(f"{field} contains an asset without a valid role")
                if asset_role is not None and asset_role not in allowed_roles:
                    raise ManifestError(f"{field} contains an asset with the wrong role")
        if strict_assets and any(asset_id not in asset_ids for asset_id in candidates):
            raise ManifestError("candidate_ids require materialized assets")
        if status == LinkStatus.VERIFIED:
            if not linked or any(asset_id not in asset_ids for asset_id in linked):
                raise ManifestError("verified links require known assets")
            for asset_id in linked:
                asset = manifest["assets"][asset_id]
                if not isinstance(asset, dict) or not asset.get("output_file"):
                    raise ManifestError("verified assets require output_file")
                relative = Path(str(asset["output_file"]))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ManifestError("asset output_file must be a safe relative path")
                if strict_assets and asset.get("selection_status") == "review_only":
                    raise ManifestError("review-only assets cannot be auto-verified")
                if strict_assets and asset.get("selection_status") == "manual_verified":
                    confirmation = item.get("confirmation")
                    if not isinstance(confirmation, dict) or confirmation.get("method") != "manual":
                        raise ManifestError("manual assets require manual confirmation")
        elif linked:
            raise ManifestError("non-verified links cannot expose confirmed assets")
        if strict_assets and status != LinkStatus.VERIFIED:
            for asset_id in candidates:
                if manifest["assets"][asset_id].get("selection_status") != "review_only":
                    raise ManifestError("unverified candidates must be review-only")
        if status != LinkStatus.VERIFIED and not item["review_reasons"]:
            raise ManifestError("non-verified links require a reason")
        role_status_fields = [
            field for field in LINK_ROLE_STATUS_FIELDS if field in item
        ]
        if role_status_fields and len(role_status_fields) != len(
            LINK_ROLE_STATUS_FIELDS
        ):
            raise ManifestError("link role status fields must be written together")
        if role_status_fields:
            expected_statuses = derive_link_statuses(item, manifest["assets"])
            if any(
                item.get(field) != expected_statuses[field]
                for field in LINK_ROLE_STATUS_FIELDS
            ):
                raise ManifestError("link role status fields are inconsistent")


def link_manifest_path(run_dir: Path) -> Path:
    return run_dir / "solutions" / "link_manifest.json"


def write_link_manifest(run_dir: Path, manifest: Mapping[str, Any]) -> Path:
    data = dict(manifest)
    validate_link_manifest(data)
    output = link_manifest_path(run_dir)
    inside(output.resolve(), "link output")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=output.parent,
            prefix=output.name + ".",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output


def read_link_manifest(
    run_dir: Path, expected_cache_key: str | None = None
) -> dict[str, Any] | None:
    path = link_manifest_path(run_dir)
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError("cannot read link manifest") from exc
    validate_link_manifest(manifest)
    if expected_cache_key is not None and manifest["cache_key"] != expected_cache_key:
        if (manifest.get("manual_confirmation_events")
                or manifest.get("manual_constraints")
                or any(asset.get("selection_status") == "manual_verified"
                       for asset in manifest["assets"].values())):
            raise ManifestError("stale manifest contains manual selections")
        return None
    if manifest.get("manual_propagation_contract") == "fixed-edge-v1":
        import phase3_fixed_edges as phase3
        import scope_alignment
        import solution_runtime
        import solution_scope

        reference = manifest["alignment_snapshot"]
        snapshot_path = run_dir / reference["relative_path"]
        try:
            run_manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )
            problem_source = Path(str(run_manifest.get("source_pdf") or ""))
            solution_source = Path(str(manifest["solution"].get("source_pdf") or ""))
            if not problem_source.is_absolute():
                problem_source = run_dir / problem_source
            if not solution_source.is_absolute():
                solution_source = run_dir / solution_source
            if (
                not problem_source.is_file()
                or not solution_source.is_file()
                or _file_sha256(problem_source)
                != manifest["problem"]["source_sha256"]
                or _file_sha256(solution_source)
                != manifest["solution"]["source_sha256"]
            ):
                raise ManifestError("alignment snapshot source PDF bytes changed")
            scope = manifest["scope_contract"]["provenance"]
            phase3.read_alignment_snapshot(
                snapshot_path,
                run_dir=run_dir,
                expected_file_sha256=reference["sha256"],
                expected_problem_source_sha256=manifest["problem"]["source_sha256"],
                expected_solution_source_sha256=manifest["solution"]["source_sha256"],
                expected_link_cache_key=manifest["cache_key"],
                expected_versions={
                    "core_version": CORE_VERSION,
                    "runtime_version": solution_runtime.RUNTIME_VERSION,
                    "scope_index_version": solution_scope.SCOPE_INDEX_VERSION,
                    "scope_alignment_version": scope_alignment.SCOPE_ALIGNMENT_VERSION,
                    "verifier_contract_version": (
                        solution_runtime.SCOPE_VERIFY_CONTRACT_VERSION
                    ),
                },
                expected_alignment_index_sha256=scope["alignment_index_sha256"],
            )
        except (
            OSError,
            json.JSONDecodeError,
            ManifestError,
            phase3.SnapshotError,
        ) as exc:
            raise ManifestError("alignment snapshot is missing or invalid") from exc
    if expected_cache_key is not None and manifest["cache_key"] != expected_cache_key:
        return None
    return manifest


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decision_asset_ids(decision: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(asset_id)
            for field in ("answer_ids", "explanation_ids", "candidate_ids")
            for asset_id in decision.get(field, [])
        )
    )


def _detailed_candidate_ids(
    manifest: Mapping[str, Any], decision: Mapping[str, Any]
) -> tuple[str, ...]:
    assets = manifest.get("assets", {})
    return tuple(
        asset_id
        for asset_id in decision.get("candidate_ids", [])
        if isinstance(assets.get(asset_id), Mapping)
        and assets[asset_id].get("kind")
        in {SolutionRole.DETAILED_SOLUTION.value, SolutionRole.MIXED.value}
    )


def _merge_quick_only_decision(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    current_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    detailed_ids = _detailed_candidate_ids(current_manifest, current)
    if not detailed_ids:
        raise ManifestError("current decision has no detailed solution candidate")
    if not previous.get("candidate_ids"):
        return copy.deepcopy(dict(current))
    if previous.get("status") == LinkStatus.VERIFIED.value:
        merged = copy.deepcopy(dict(previous))
        merged["candidate_ids"] = list(
            dict.fromkeys((*previous.get("candidate_ids", []), *detailed_ids))
        )
        if current.get("status") == LinkStatus.VERIFIED.value:
            assets = current_manifest["assets"]
            merged["explanation_ids"] = list(
                dict.fromkeys(
                    (
                        *previous.get("explanation_ids", []),
                        *(
                            asset_id
                            for asset_id in current.get("explanation_ids", [])
                            if assets[asset_id].get("kind")
                            in {
                                SolutionRole.DETAILED_SOLUTION.value,
                                SolutionRole.MIXED.value,
                            }
                        ),
                    )
                )
            )
        else:
            merged["review_reasons"] = list(
                dict.fromkeys(
                    (
                        *previous.get("review_reasons", []),
                        *current.get("review_reasons", []),
                        "detailed_solution_review_required",
                    )
                )
            )
        merged["solution_printed_pages"] = sorted(
            {
                *previous.get("solution_printed_pages", []),
                *current.get("solution_printed_pages", []),
            }
        )
        merged["evidence"] = list(
            dict.fromkeys(
                (*previous.get("evidence", []), *current.get("evidence", []))
            )
        )
        return merged

    merged = copy.deepcopy(dict(current))
    merged["candidate_ids"] = list(
        dict.fromkeys((*previous.get("candidate_ids", []), *detailed_ids))
    )
    merged["solution_printed_pages"] = sorted(
        {
            *previous.get("solution_printed_pages", []),
            *current.get("solution_printed_pages", []),
        }
    )
    merged["evidence"] = list(
        dict.fromkeys((*previous.get("evidence", []), *current.get("evidence", [])))
    )
    if merged.get("status") != LinkStatus.VERIFIED.value:
        merged["review_reasons"] = list(
            dict.fromkeys(
                (*previous.get("review_reasons", []), *current.get("review_reasons", []))
            )
        )
    return merged


def _read_manifest_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read link manifest: {path}") from exc
    validate_link_manifest(value)
    return value


def _verified_asset_source(
    manifest_path: Path,
    asset_id: str,
    record: Mapping[str, Any],
) -> tuple[Path, str]:
    relative = Path(str(record.get("output_file") or ""))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ManifestError(f"invalid output_file for preserved asset: {asset_id}")
    expected = str(record.get("output_sha256") or "").casefold()
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ManifestError(f"output_sha256 is required for preserved asset: {asset_id}")
    source = manifest_path.parent / relative
    if not source.is_file():
        raise ManifestError(f"preserved asset file is missing: {asset_id}")
    if _file_sha256(source) != expected:
        raise ManifestError(f"preserved asset SHA-256 differs: {asset_id}")
    return source, expected


def _copy_verified_asset(source: Path, destination: Path, expected_sha256: str) -> None:
    inside(destination.resolve(), "asset output")
    if destination.exists():
        if not destination.is_file() or _file_sha256(destination) != expected_sha256:
            raise ManifestError(f"destination asset conflicts: {destination.name}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=destination.parent,
            prefix=destination.name + ".",
            suffix=".tmp",
            delete=False,
        ) as output, source.open("rb") as input_stream:
            temporary = Path(output.name)
            for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if _file_sha256(temporary) != expected_sha256:
            raise ManifestError(f"copied asset SHA-256 differs: {destination.name}")
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _additive_alias_asset_id(
    item_id: str, asset_id: str, record: Mapping[str, Any]
) -> str:
    safe_item = re.sub(r"[^0-9A-Za-z가-힣_-]+", "-", item_id).strip("-")
    safe_item = (safe_item or "item")[:48]
    digest = hashlib.sha256(
        json.dumps(
            [item_id, asset_id, record],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"additive-review-{safe_item}-{digest}"


def _drop_aliased_confirmed_ids(
    decision: dict[str, Any],
    aliases: Mapping[str, str],
    previous_decision: Mapping[str, Any],
) -> None:
    protected = {
        asset_id
        for field in ("answer_ids", "explanation_ids")
        for asset_id in previous_decision.get(field, [])
    }
    for field in ("answer_ids", "explanation_ids"):
        decision[field] = [
            asset_id
            for asset_id in decision.get(field, [])
            if asset_id not in aliases or asset_id in protected
        ]
    if aliases:
        decision["review_reasons"] = list(
            dict.fromkeys(
                (*decision.get("review_reasons", []), "additive_detail_asset_aliased")
            )
        )
    if (
        decision.get("status") == LinkStatus.VERIFIED.value
        and not decision.get("answer_ids")
        and not decision.get("explanation_ids")
    ):
        decision["status"] = LinkStatus.REVIEW.value
        decision["review_reasons"] = list(
            dict.fromkeys(
                (*decision.get("review_reasons", []), "detailed_solution_review_required")
            )
        )


def merge_additive_link_manifests(
    previous_manifest_path: Path,
    current_manifest_path: Path,
    output_run_dir: Path,
) -> dict[str, Any]:
    """Keep every prior non-empty decision and fill only its empty candidates."""

    previous_manifest_path = previous_manifest_path.resolve()
    current_manifest_path = current_manifest_path.resolve()
    output_run_dir = inside(output_run_dir.resolve(), "output run")
    previous = _read_manifest_file(previous_manifest_path)
    current = _read_manifest_file(current_manifest_path)
    previous_scope = previous.get("scope_contract")
    current_scope = current.get("scope_contract")
    if (previous_scope is None) != (current_scope is None):
        raise ManifestError("additive manifests use different scope contracts")
    if previous_scope is not None and previous_scope != current_scope:
        raise ManifestError("additive manifests use different scope contracts")
    if (
        previous.get("asset_contract") != current.get("asset_contract")
        or previous["problem"].get("source_sha256")
        != current["problem"].get("source_sha256")
        or previous["solution"].get("source_sha256")
        != current["solution"].get("source_sha256")
        or previous["problem"].get("item_ids")
        != current["problem"].get("item_ids")
    ):
        raise ManifestError("additive manifests do not describe the same source items")

    merged = copy.deepcopy(current)
    merged_items: dict[str, Any] = {}
    asset_sources: dict[str, tuple[dict[str, Any], Path, str]] = {}

    # Pass 1: every prior reference is immutable and wins its original id.
    for item_id in previous["problem"]["item_ids"]:
        for asset_id in _decision_asset_ids(previous["items"][item_id]):
            record = previous["assets"].get(asset_id)
            if not isinstance(record, Mapping):
                raise ManifestError(f"referenced asset is missing: {asset_id}")
            record_copy = copy.deepcopy(dict(record))
            source, expected_sha = _verified_asset_source(
                previous_manifest_path, asset_id, record_copy
            )
            protected = asset_sources.get(asset_id)
            if protected is not None and protected[0] != record_copy:
                raise ManifestError(f"previous asset id is inconsistent: {asset_id}")
            asset_sources[asset_id] = (record_copy, source, expected_sha)

    # Pass 2: add current detailed candidates, aliasing only true record collisions.
    for item_id in current["problem"]["item_ids"]:
        previous_decision = previous["items"][item_id]
        current_decision = current["items"][item_id]
        keep_previous = bool(
            _detailed_candidate_ids(previous, previous_decision)
        )
        if keep_previous:
            decision = copy.deepcopy(previous_decision)
        else:
            current_detail_ids = _detailed_candidate_ids(current, current_decision)
            try:
                decision = _merge_quick_only_decision(
                    previous_decision, current_decision, current
                )
            except ManifestError as exc:
                raise ManifestError(
                    f"additive recovery left detailed candidate empty: {item_id}"
                ) from exc
            aliases: dict[str, str] = {}
            mapped_detail_ids: list[str] = []
            for asset_id in current_detail_ids:
                record = current["assets"].get(asset_id)
                if not isinstance(record, Mapping):
                    raise ManifestError(f"referenced asset is missing: {asset_id}")
                current_record = copy.deepcopy(dict(record))
                source, expected_sha = _verified_asset_source(
                    current_manifest_path, asset_id, current_record
                )
                protected = asset_sources.get(asset_id)
                if protected is None:
                    asset_sources[asset_id] = (
                        current_record,
                        source,
                        expected_sha,
                    )
                    mapped_detail_ids.append(asset_id)
                    continue
                if protected[0] == current_record:
                    mapped_detail_ids.append(asset_id)
                    continue
                alias_id = _additive_alias_asset_id(
                    item_id, asset_id, current_record
                )
                alias_record = copy.deepcopy(current_record)
                alias_record["selection_status"] = "review_only"
                alias_prior = asset_sources.get(alias_id)
                if alias_prior is not None and alias_prior[0] != alias_record:
                    raise ManifestError(
                        f"additive alias id collision: {alias_id}"
                    )
                asset_sources[alias_id] = (
                    alias_record,
                    source,
                    expected_sha,
                )
                aliases[asset_id] = alias_id
                mapped_detail_ids.append(alias_id)
            decision["candidate_ids"] = list(
                dict.fromkeys(
                    (
                        *previous_decision.get("candidate_ids", []),
                        *mapped_detail_ids,
                    )
                )
            )
            _drop_aliased_confirmed_ids(
                decision, aliases, previous_decision
            )
        merged_items[item_id] = decision
        previous_ids = set(_decision_asset_ids(previous_decision))
        for asset_id in _decision_asset_ids(decision):
            if asset_id in asset_sources:
                continue
            use_previous = asset_id in previous_ids
            source_manifest = previous if use_previous else current
            source_path = (
                previous_manifest_path if use_previous else current_manifest_path
            )
            record = source_manifest["assets"].get(asset_id)
            if not isinstance(record, Mapping):
                raise ManifestError(f"referenced asset is missing: {asset_id}")
            record_copy = copy.deepcopy(dict(record))
            asset_source, expected_sha = _verified_asset_source(
                source_path, asset_id, record_copy
            )
            asset_sources[asset_id] = (record_copy, asset_source, expected_sha)

    merged["items"] = merged_items
    merged["assets"] = {
        asset_id: record for asset_id, (record, _source, _sha) in asset_sources.items()
    }
    for decision in merged_items.values():
        decision.update(derive_link_statuses(decision, merged["assets"]))
    if merged["cache_key"] != current["cache_key"]:
        raise ManifestError("current cache_key must be retained")
    validate_link_manifest(merged)
    incomplete = [
        item_id
        for item_id, decision in merged_items.items()
        if not _detailed_candidate_ids(merged, decision)
    ]
    if incomplete:
        raise ManifestError(
            "additive manifest contains quick-only items: "
            + ", ".join(incomplete[:10])
        )

    output_assets = output_run_dir / "solutions"
    for asset_id, (record, source, expected_sha) in asset_sources.items():
        destination = output_assets / str(record["output_file"])
        _copy_verified_asset(source, destination, expected_sha)
    write_link_manifest(output_run_dir, merged)
    return merged


def link_manifest_needs_verification_retry(manifest: Mapping[str, Any]) -> bool:
    """Transient/missing independent verification must not become a sticky cache hit."""

    items = manifest.get("items")
    if not isinstance(items, dict):
        return True
    return any(
        "independent_verification_missing" in str(reason)
        or "quality_invocation_target_mismatch" in str(reason)
        for item in items.values()
        if isinstance(item, dict)
        for reason in item.get("review_reasons", [])
    )


@dataclass(frozen=True)
class LinkContext:
    run_dir: Path
    solution_path: Path
    crop_dpi: int
    workers: int
    model: str | None
    force: bool = False


ProblemIndexHook = Callable[[LinkContext], ProblemIndex]
SolutionIndexHook = Callable[[LinkContext], SolutionIndex]
VerificationHook = Callable[
    [LinkContext, tuple[CandidatePair, ...]], Mapping[tuple[str, str], PairVerification]
]
RenderHook = Callable[
    [LinkContext, tuple[SolutionAsset, ...], tuple[LinkDecision, ...]],
    Mapping[str, Mapping[str, Any]],
]


@dataclass(frozen=True)
class AlignmentSnapshotMetadata:
    pair_verifications: tuple[Mapping[str, Any], ...]
    versions: Mapping[str, str]


AlignmentSnapshotMetadataHook = Callable[
    [LinkContext, Mapping[tuple[str, str], PairVerification]],
    AlignmentSnapshotMetadata | None,
]


@dataclass(frozen=True)
class ScopeAlignmentBundle:
    """Episode-path inputs produced by the runtime (Phase 2b).

    ``episode_assets`` maps episode_id -> native SolutionAssets. The bundle
    signature participates in the link cache key so a changed alignment can
    never silently reuse a stale manifest.
    """

    alignment: ScopeAlignmentLike
    episode_assets: Mapping[str, tuple[SolutionAsset, ...]]
    provenance: Mapping[str, Any] | None = None

    def flat_assets(self) -> tuple[SolutionAsset, ...]:
        seen: dict[str, SolutionAsset] = {}
        for episode_id in sorted(self.episode_assets):
            for asset in self.episode_assets[episode_id]:
                seen.setdefault(asset.asset_id, asset)
        return tuple(seen.values())

    def signature(self) -> str:
        """Covers the alignment AND every native asset's full identity —
        a changed bbox/part/fingerprint must invalidate the link cache."""
        payload = {
            "alignment": self.alignment.signature(),
            "episode_assets": {
                episode_id: [
                    asset.signature()
                    for asset in sorted(
                        self.episode_assets[episode_id],
                        key=lambda a: a.asset_id,
                    )
                ]
                for episode_id in sorted(self.episode_assets)
            },
            "provenance": dict(self.provenance or {}),
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


ScopeAlignmentHook = Callable[
    [LinkContext, ProblemIndex, SolutionIndex], "ScopeAlignmentBundle | None"
]


@dataclass(frozen=True)
class LinkStageHooks:
    detect_problem_index: ProblemIndexHook
    detect_solution_index: SolutionIndexHook
    verify_candidate_pairs: VerificationHook
    render_solution_assets: RenderHook
    version: str
    build_scope_alignment: ScopeAlignmentHook | None = None
    load_alignment_snapshot_metadata: AlignmentSnapshotMetadataHook | None = None
    provider_profile: str = "full"
    expand_solution_index: Callable[[LinkContext, tuple[ProblemItem, ...], str], SolutionIndex] | None = None
    verify_scope_pairs: Callable[[LinkContext, tuple[ScopeCandidatePair, ...], ProblemIndex, ScopeAlignmentBundle], Mapping[PairKey, PairVerification]] | None = None
    solution_search_trace: Callable[[LinkContext], Sequence[Mapping[str, Any]]] | None = None


def _missing_stage(name: str) -> Callable[..., Any]:
    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise StageHookError(f"stage hook is not configured: {name}")

    return unavailable


DEFAULT_STAGE_HOOKS = LinkStageHooks(
    detect_problem_index=_missing_stage("detect_problem_index"),
    detect_solution_index=_missing_stage("detect_solution_index"),
    verify_candidate_pairs=_missing_stage("verify_candidate_pairs"),
    render_solution_assets=_missing_stage("render_solution_assets"),
    version="unconfigured",
)


def run_link_pipeline(
    context: LinkContext, hooks: LinkStageHooks = DEFAULT_STAGE_HOOKS
) -> dict[str, Any]:
    if not context.solution_path.is_file():
        raise StageHookError('solution_source_absent')
    problem = hooks.detect_problem_index(context)
    solution = hooks.detect_solution_index(context)
    bundle = (
        hooks.build_scope_alignment(context, problem, solution)
        if hooks.build_scope_alignment is not None
        else None
    )
    signature = {"problem": problem.signature(), "solution": solution.signature()}
    if bundle is not None:
        signature["scope_alignment"] = bundle.signature()
    cache_key = make_cache_key(
        problem.source_sha256,
        solution.source_sha256,
        crop_dpi=context.crop_dpi,
        model=context.model,
        stage_version=hooks.version,
        input_signature=signature,
    )
    if not context.force:
        cached = read_link_manifest(context.run_dir, cache_key)
        if (cached is not None and not link_manifest_needs_verification_retry(cached)
                and all(item.get("status") != "verified" or decision_reason(context.run_dir, cached, unit_id) is None
                        for unit_id, item in cached["items"].items())):
            return cached
    verifications: dict[PairKey, PairVerification] = {}
    attempted: set[tuple[str, str, str]] = set()
    admitted: set[PairKey] = set()
    pairs: tuple[CandidatePair, ...] = ()
    search_trace: list[dict[str, Any]] = []
    unresolved = problem.items
    for stage in ('current', 'neighbor', 'unit', 'document'):
        resolved_roles: set[tuple[str, SolutionRole]] = set()
        if stage != 'current':
            if not unresolved or hooks.expand_solution_index is None:
                break
            solution = hooks.expand_solution_index(context, unresolved, stage)
            bundle = hooks.build_scope_alignment(context, problem, solution) if hooks.build_scope_alignment else None
            current_assets = {asset.asset_id: asset for asset in solution.assets}
            if bundle is not None:
                current_assets.update({asset.asset_id: asset for asset in bundle.flat_assets()})
            current_problems = {item.item_id: item for item in problem.items}
            for decision in decisions:
                if decision.status != LinkStatus.VERIFIED:
                    continue
                for role, selected in ((SolutionRole.QUICK_ANSWER, decision.answer_ids),
                                       (SolutionRole.DETAILED_SOLUTION, decision.explanation_ids)):
                    if selected and all(asset_id in current_assets
                            and current_assets[asset_id].role == role
                            and _strong_verification(verifications.get((decision.item_id, asset_id)),
                                MIN_LINK_CONFIDENCE, current_problems[decision.item_id], current_assets[asset_id])
                            for asset_id in selected):
                        resolved_roles.add((decision.item_id, role))
        ordinary = expanded_candidate_pairs(unresolved, solution.assets,
            problem_source_sha256=problem.source_sha256, solution_source_sha256=solution.source_sha256)
        by_id = {asset.asset_id: asset for asset in solution.assets}
        fresh = tuple(pair for pair in ordinary if (
            pair.problem_id, pair.asset_id, quality.identity(by_id[pair.asset_id].signature())
        ) not in attempted and (pair.problem_id, pair.role) not in resolved_roles)
        attempted.update((pair.problem_id, pair.asset_id, quality.identity(by_id[pair.asset_id].signature())) for pair in fresh)
        admitted.update((pair.problem_id, pair.asset_id) for pair in ordinary)
        pairs = tuple(dict.fromkeys((*pairs, *fresh)))
        verifications.update(hooks.verify_candidate_pairs(context, fresh))
        solution = hooks.detect_solution_index(context)
        decisions = align_solution_assets(problem.items, solution.assets, verifications,
            admitted_pairs=frozenset(admitted),
            problem_source_sha256=problem.source_sha256, solution_source_sha256=solution.source_sha256,
            scope_alignment=bundle.alignment if bundle else None,
            episode_assets=bundle.episode_assets if bundle else None)
        scope_pairs = scope_candidate_pairs(problem.items, decisions, bundle.alignment, bundle.episode_assets) if bundle else ()
        native_by_id = {asset.asset_id: asset for asset in bundle.flat_assets()} if bundle else {}
        scope_fresh = tuple(pair for pair in scope_pairs if (
            pair.problem_id, pair.asset_id, quality.identity(native_by_id[pair.asset_id].signature())
        ) not in attempted and (pair.problem_id, pair.role) not in resolved_roles)
        if scope_fresh and hooks.verify_scope_pairs is not None:
            attempted.update((pair.problem_id, pair.asset_id, quality.identity(native_by_id[pair.asset_id].signature())) for pair in scope_fresh)
            verifications.update(hooks.verify_scope_pairs(context, scope_fresh, problem, bundle))
            admitted.update((pair.problem_id, pair.asset_id) for pair in scope_pairs)
        all_assets = solution.assets + tuple(asset for asset in native_by_id.values()
                                            if asset.asset_id not in {value.asset_id for value in solution.assets})
        decisions = align_solution_assets(problem.items, all_assets, verifications,
            admitted_pairs=frozenset(admitted),
            problem_source_sha256=problem.source_sha256, solution_source_sha256=solution.source_sha256,
            scope_alignment=bundle.alignment if bundle else None,
            episode_assets=bundle.episode_assets if bundle else None)
        search_trace.append({'stage': stage, 'problem_ids': [item.item_id for item in unresolved],
            'ordinary_pairs': [[pair.problem_id, pair.asset_id] for pair in fresh],
            'scope_pairs': [[pair.problem_id, pair.asset_id] for pair in scope_fresh],
            'verified_roles': {decision.item_id: {'answer': list(decision.answer_ids),
                'explanation': list(decision.explanation_ids)} for decision in decisions}})
        incomplete = {decision.item_id for decision in decisions if decision.status != LinkStatus.CONFLICT
                      and not (decision.answer_ids and decision.explanation_ids)}
        unresolved = tuple(item for item in problem.items if item.item_id in incomplete
                           and item.crop_is_search_eligible and not item.cache_only_detailed_review)
    rendered_assets = hooks.render_solution_assets(context, all_assets, decisions)
    scope_contract = None
    if bundle is not None:
        alignment = bundle.alignment.to_dict()
        scope_contract = {
            "contract": SCOPE_ALIGNMENT_CONTRACT,
            "bundle_signature": bundle.signature(),
            "alignment_sha256": _canonical_json_sha256(alignment),
            "alignment": alignment,
            "provenance": dict(bundle.provenance or {}),
        }
    manifest_assets = {
        asset_id: {
            key: value
            for key, value in record.items()
            if key != "_snapshot_signature"
        }
        for asset_id, record in rendered_assets.items()
    }
    run_manifest = json.loads(
        (context.run_dir / "manifest.json").read_text(encoding="utf-8")
    )
    problem_source_value = run_manifest.get("source_pdf")
    if not problem_source_value:
        run_state = json.loads(
            (context.run_dir / "run_state.json").read_text(encoding="utf-8")
        )
        problem_source_value = run_state.get("source_pdf")
    if not isinstance(problem_source_value, str) or not problem_source_value.strip():
        raise ManifestError("problem source PDF path is missing")
    problem_path = Path(problem_source_value)
    if not problem_path.is_absolute():
        problem_path = context.run_dir / problem_path
    manifest = build_link_manifest(
        problem,
        solution,
        decisions,
        manifest_assets,
        cache_key=cache_key,
        problem_path=problem_path.resolve(),
        solution_path=context.solution_path,
        scope_contract=scope_contract,
    )
    manifest["provider_profile"] = hooks.provider_profile
    manifest["stage_version"] = hooks.version
    manifest['search_policy'] = SEARCH_POLICY
    manifest['search_trace'] = search_trace
    manifest['page_search_trace'] = list(hooks.solution_search_trace(context)) if hooks.solution_search_trace else []
    for unit_id, link in manifest["items"].items():
        selected = link["answer_ids"] + link["explanation_ids"]
        page_trace = manifest['page_search_trace']
        completed_search = bool(page_trace and page_trace[-1].get('stage') == 'document'
                                and not page_trace[-1].get('uninspected_pages'))
        search_states = {}
        for role, selected_field in ((SolutionRole.QUICK_ANSWER, 'answer_ids'),
                                     (SolutionRole.DETAILED_SOLUTION, 'explanation_ids')):
            role_ids = [asset_id for asset_id in link['candidate_ids']
                        if manifest_assets.get(asset_id, {}).get('kind') in {role.value, SolutionRole.MIXED.value}]
            if link[selected_field]:
                search_states[role.value] = 'verified'
            elif not context.solution_path.is_file():
                search_states[role.value] = 'source_absent'
            elif any((unit_id, asset_id) not in verifications
                     and any(item[:2] == (unit_id, asset_id) for item in attempted) for asset_id in role_ids):
                search_states[role.value] = 'provider_failed'
            elif (len(role_ids) == 1 and not manifest_assets[role_ids[0]].get('review_reason')
                  and not any(item[:2] == (unit_id, role_ids[0]) for item in attempted)):
                search_states[role.value] = 'not_searched'
            elif role_ids:
                search_states[role.value] = 'candidate_ambiguous'
            elif any(trace.get('failed_pages') for trace in page_trace):
                search_states[role.value] = 'provider_failed'
            else:
                search_states[role.value] = 'no_candidate' if completed_search else 'not_searched'
        link['search_states'] = search_states
        selected_by_id = {asset.asset_id: asset for asset in all_assets}
        explicit_quick = [selected_by_id[asset_id].explicit_answer for asset_id in link['answer_ids']]
        explicit_detail = [selected_by_id[asset_id].explicit_answer for asset_id in link['explanation_ids']]
        link['answer_consistency'] = ('conflict' if link['status'] == 'conflict' else
            'consistent' if explicit_quick and explicit_detail and all(explicit_quick + explicit_detail)
            and len({_normalize_answer(value) for value in explicit_quick + explicit_detail}) == 1 else 'not_checked')
        link["quality_decision"] = {
            "policy": quality.QUALITY_POLICY,
            "decision_sha256": quality.identity(decision_body(link)),
            "receipts": {asset_id: verification.quality_receipt
                         for asset_id in selected
                         if (verification := verifications.get((unit_id, asset_id))) is not None},
        }
        reason = decision_reason(context.run_dir, manifest, unit_id) if link["status"] == "verified" else None
        if reason is not None:
            link['search_states'] = {role: 'evidence_invalid' if state == 'verified' else state
                                     for role, state in link['search_states'].items()}
            link['answer_consistency'] = 'not_checked'
            link.update(status="review", answer_ids=[], explanation_ids=[],
                        candidate_ids=list(dict.fromkeys(link["candidate_ids"] + selected)),
                        review_reasons=list(dict.fromkeys(link["review_reasons"] + [reason])))
            link.update(derive_link_statuses(link, manifest["assets"]))
            link["quality_decision"]["decision_sha256"] = quality.identity(decision_body(link))
    verified_assets = {asset_id for link in manifest["items"].values()
                       for asset_id in link["answer_ids"] + link["explanation_ids"]}
    for asset_id, record in manifest["assets"].items():
        if record.get("selection_status") == "auto_verified" and asset_id not in verified_assets:
            record["selection_status"] = "review_only"
    snapshot_path: Path | None = None
    snapshot_preexisting = False
    if bundle is not None and scope_contract is not None:
        import phase3_fixed_edges as phase3

        unavailable_reason: str | None = None
        metadata = None
        if hooks.load_alignment_snapshot_metadata is None:
            unavailable_reason = "scope_verification_refs_unavailable"
        else:
            try:
                metadata = hooks.load_alignment_snapshot_metadata(context, verifications)
            except (OSError, ValueError):
                unavailable_reason = "scope_verification_refs_unavailable"
            if metadata is None:
                unavailable_reason = "scope_verification_refs_unavailable"
        if metadata is not None:
            try:
                snapshot = phase3.build_pipeline_alignment_snapshot(
                    problem_index=problem,
                    materialized_assets=all_assets,
                    legacy_candidate_pairs=pairs,
                    verifications=verifications,
                    pair_verification_records=metadata.pair_verifications,
                    decisions=decisions,
                    rendered_assets=rendered_assets,
                    scope_contract=scope_contract,
                    link_cache_key=cache_key,
                    versions=metadata.versions,
                )
                snapshot_sha256 = phase3.alignment_snapshot_file_sha256(snapshot)
                snapshot_relative = phase3.alignment_snapshot_relative_path(
                    snapshot_sha256
                )
                snapshot_path = context.run_dir / snapshot_relative
                snapshot_preexisting = snapshot_path.exists()
                phase3.write_alignment_snapshot(
                    snapshot_path, snapshot, run_dir=context.run_dir
                )
                manifest = phase3.add_alignment_snapshot_contract(
                    manifest,
                    phase3.alignment_snapshot_reference(
                        snapshot_relative, snapshot_sha256
                    ),
                )
            except phase3.SnapshotError:
                if snapshot_path is not None and not snapshot_preexisting:
                    snapshot_path.unlink(missing_ok=True)
                    snapshot_path = None
                unavailable_reason = "snapshot_build_failed"
        if unavailable_reason is not None:
            manifest["propagation_unavailable"] = {"reason": unavailable_reason}
    try:
        write_link_manifest(context.run_dir, manifest)
    except Exception:
        if snapshot_path is not None and not snapshot_preexisting:
            snapshot_path.unlink(missing_ok=True)
        raise
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="문제 crop과 정답·해설 PDF를 연결합니다.")
    commands = parser.add_subparsers(dest="command", required=True)
    link = commands.add_parser("link", help="정답·해설 연결 sidecar 생성")
    link.add_argument("--run-dir", required=True)
    link.add_argument("--solution", required=True)
    link.add_argument("--crop-dpi", type=int, choices=(300, 450), default=300)
    link.add_argument("--workers", type=int, default=2)
    link.add_argument("--model", default=None)
    link.add_argument(
        "--provider-profile", choices=("full", "codex_only"), default="full"
    )
    link.add_argument("--force", action="store_true", help="같은 cache key도 다시 연결")
    merge = commands.add_parser(
        "merge-additive", help="기존 연결을 고정하고 빈 문항만 복구"
    )
    merge.add_argument("--previous", required=True)
    merge.add_argument("--current", required=True)
    merge.add_argument("--output-run-dir", required=True)
    repair = commands.add_parser(
        "repair-seams", help="기존 캐시만으로 잘린 해설 경계를 새 leaf에 복구"
    )
    repair.add_argument("--run-dir", required=True)
    repair.add_argument("--output-run-dir", required=True)
    repair.add_argument("--unit-id", action="append", required=True)
    return parser


def _solution_learning_enabled() -> bool:
    return os.environ.get("ASTRA_SOLUTION_LEARNING", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


@contextmanager
def _solution_adapter(provider_profile: str) -> Iterator[Any]:
    from solution_runtime import SolutionRuntimeAdapter

    store = None
    learning_runtime = None
    if _solution_learning_enabled():
        try:
            from astra_runtime import load_settings
            from solution_learning.store import LearningStore
            from solution_learning_runtime import SolutionLearningRuntime

            settings = load_settings()
            database = settings.learning_root / "library.sqlite3"
            source_roots = tuple(
                root
                for root in (settings.crop_root, settings.workspace)
                if root.is_dir()
            )
            if database.is_file() and source_roots:
                store = LearningStore(
                    settings.learning_root,
                    allowed_source_roots=source_roots,
                )
                learning_runtime = SolutionLearningRuntime(store, enabled=True)
        except Exception:
            if store is not None:
                store.close()
            store = None
            learning_runtime = None

    adapter = SolutionRuntimeAdapter(
        provider_profile=provider_profile,
        learning_runtime=learning_runtime,
    )
    try:
        yield adapter
    finally:
        if store is not None:
            store.close()


def main(
    argv: Sequence[str] | None = None, hooks: LinkStageHooks | None = None
) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    validate_parsed_paths(args, ("output_run_dir", "run_dir") if args.command == "link" else ("output_run_dir",))
    if args.command == "merge-additive":
        try:
            output_run_dir = Path(args.output_run_dir).expanduser().resolve()
            previous_path = Path(args.previous).expanduser().resolve()
            previous_for_count = _read_manifest_file(previous_path)
            merged = merge_additive_link_manifests(
                previous_path,
                Path(args.current).expanduser(),
                output_run_dir,
            )
            preserved = sum(
                bool(decision.get("candidate_ids"))
                for decision in previous_for_count["items"].values()
            )
            print(
                f"추가 병합 완료: {link_manifest_path(output_run_dir)} "
                f"(기존 보존 {preserved}, 총 {len(merged['items'])})"
            )
            return 0
        except (ValueError, ManifestError, OSError) as exc:
            print(f"추가 병합 실패: {exc}", file=sys.stderr)
            return 2
    if args.command == "repair-seams":
        try:
            from solution_runtime import repair_solution_seams_offline

            repaired = repair_solution_seams_offline(
                args.run_dir,
                args.output_run_dir,
                args.unit_id,
            )
            metadata = repaired.get("seam_repair", {})
            print(
                f"해설 seam 복구 완료: {link_manifest_path(Path(args.output_run_dir))} "
                f"(변경 문항 {len(metadata.get('changed_item_ids', []))}, "
                f"새 후보 {len(metadata.get('replacements', {}))})"
            )
            return 0
        except (ValueError, ManifestError, OSError) as exc:
            print(f"해설 seam 복구 실패: {exc}", file=sys.stderr)
            return 2
    run_dir = Path(args.run_dir).expanduser().resolve()
    solution = Path(args.solution).expanduser().resolve()
    try:
        if not run_dir.is_dir():
            raise ValueError(f"run directory not found: {run_dir}")
        if not solution.is_file() or solution.suffix.casefold() != ".pdf":
            raise ValueError(f"solution PDF not found: {solution}")
        if args.workers < 1:
            raise ValueError("workers must be >= 1")
        context = LinkContext(
            run_dir=run_dir,
            solution_path=solution,
            crop_dpi=args.crop_dpi,
            workers=args.workers,
            model=args.model,
            force=args.force,
        )
        adapter_context = (
            _solution_adapter(args.provider_profile)
            if hooks is None
            else nullcontext(None)
        )
        with adapter_context as adapter:
            active_hooks = hooks if hooks is not None else adapter.hooks()
            try:
                refresh_recovery_progress(
                    run_dir,
                    status="running",
                    stage="linking",
                    stage_text="문제와 해설 후보를 연결하고 있습니다.",
                )
            except (OSError, ProgressError):
                pass
            run_link_pipeline(context, active_hooks)
            try:
                refresh_recovery_progress(
                    run_dir,
                    status="completed",
                    stage="completed",
                    stage_text="자동 해설 복구를 완료했습니다.",
                )
            except (OSError, ProgressError):
                pass
        print(f"해설 연결 완료: {link_manifest_path(run_dir)}")
        return 0
    except (ValueError, ManifestError, StageHookError, OSError) as exc:
        try:
            refresh_recovery_progress(
                run_dir,
                status="failed",
                stage="failed",
                stage_text="자동 해설 복구를 완료하지 못했습니다.",
            )
        except (OSError, ProgressError):
            pass
        print(f"해설 연결 실패: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
