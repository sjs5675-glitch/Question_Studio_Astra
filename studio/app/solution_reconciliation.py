"""Conservative, read-only reconciliation of Question_Crop solution candidates."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image
from astra_cli.quality_manifest import decision_body, decision_reason, stored_reason, bind_import_origin
from astra_cli.quality import identity
from printed_identifiers import PRINTED_IDENTIFIER, normalize_parent_number

from .model import load_json_object, save_json_cas, sha256_file, utc_now


MIN_CONSENSUS_CONFIDENCE = 0.90
SOURCE_PDF_RECROP_DPI = 450
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ROMAN_PREFIX = "Ⅰ|Ⅱ|Ⅲ|Ⅳ|Ⅴ|Ⅵ|Ⅶ|Ⅷ|Ⅸ|Ⅹ|Ⅺ|Ⅻ"
_PRINTED_NUMBER = PRINTED_IDENTIFIER
_SCOPE_TOKENS = re.compile(rf"[A-Za-z]+|(?:{_ROMAN_PREFIX})|[0-9]+|[가-힣]+")
_QUICK_ANSWER_RANGE = re.compile(
    rf"(?P<prefix>[A-Ma-m]|{_ROMAN_PREFIX})\s*(?P<start>[0-9]+)\s*[–—-]\s*"
    r"(?:(?P=prefix)\s*)?(?P<end>[0-9]+)"
)


class SolutionIntegrityError(ValueError):
    """Raised when a source path, symlink, or declared hash is unsafe."""


BOUNDARY_SEAM_REPAIRED = "boundary_seam_repaired"


def _contains_boundary_seam_marker(value: Any) -> bool:
    if isinstance(value, str):
        return value == BOUNDARY_SEAM_REPAIRED
    if isinstance(value, Mapping):
        return any(_contains_boundary_seam_marker(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_boundary_seam_marker(child) for child in value)
    return False


def _candidate_is_boundary_seam_repaired(candidate: "SolutionCandidate") -> bool:
    return "-seam-" in candidate.candidate_id or _contains_boundary_seam_marker(
        candidate.raw_asset
    )


def _link_is_boundary_seam_repaired(link: Mapping[str, Any]) -> bool:
    return "-seam-" in str(link.get("external_asset_id") or "") or any(
        BOUNDARY_SEAM_REPAIRED in (link.get(field) or [])
        for field in ("evidence", "review_reasons")
    )


@dataclass(frozen=True)
class PrintedNumber:
    prefix: str
    parent_number: str
    subquestion_label: str | None = None

    @property
    def number_key(self) -> str:
        return _number_key(self.parent_number)

    @property
    def subquestion_key(self) -> str | None:
        return _subquestion_key(self.subquestion_label)


@dataclass(frozen=True)
class SolutionCandidate:
    candidate_id: str
    origin: str
    source_relative_path: str
    source_sha256: str
    source_document: str
    document_sha256: str
    document_page_count: int
    bbox: tuple[int, int, int, int]
    pdf_index: int
    pdf_page: int
    printed_page: int | None
    segment_id: str | None
    render_dpi: int
    tokens: tuple[str, ...]
    answer_text: str | None
    confidence: float
    attempts: tuple[str, ...]
    source_is_precropped: bool
    raw_asset: Mapping[str, Any] = field(repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-safe metadata without persisting an absolute path."""

        return {
            "candidate_id": self.candidate_id,
            "origin": self.origin,
            "source_relative_path": self.source_relative_path,
            "source_sha256": self.source_sha256,
            "source_document": self.source_document,
            "document_sha256": self.document_sha256,
            "document_page_count": self.document_page_count,
            "bbox": list(self.bbox),
            "pdf_index": self.pdf_index,
            "pdf_page": self.pdf_page,
            "printed_page": self.printed_page,
            "segment_id": self.segment_id,
            "render_dpi": self.render_dpi,
            "tokens": list(self.tokens),
            "answer_text": self.answer_text,
            "confidence": self.confidence,
            "attempts": list(self.attempts),
            "source_is_precropped": self.source_is_precropped,
        }

    def to_link_asset(self) -> dict[str, Any]:
        """Return the upstream link asset unchanged (or the fallback equivalent)."""

        return copy.deepcopy(dict(self.raw_asset))

    def to_solution_parts(self) -> list[dict[str, Any]]:
        parts = self.raw_asset.get("parts")
        if not isinstance(parts, list) or not parts:
            return [self.to_solution_part()]
        return [
            {**self.to_solution_part(),
             "pdf_index": part["pdf_index"], "pdf_page": part["pdf_page"],
             "printed_page": part.get("printed_page"), "segment_id": part.get("segment_id"),
             "bbox": {"unit": "ratio", "origin": "top-left",
                      **{name: coordinate / 1000 for name, coordinate in
                         zip(("left", "top", "right", "bottom"), part["bbox"], strict=True)}}}
            for part in parts
        ]

    def to_solution_part(self) -> dict[str, Any]:
        left, top, right, bottom = self.bbox
        return {
            "pdf_index": self.pdf_index,
            "pdf_page": self.pdf_page,
            "printed_page": self.printed_page,
            "segment_id": self.segment_id,
            "render_dpi": self.render_dpi,
            "bbox": {
                "unit": "ratio",
                "origin": "top-left",
                "left": left / 1000,
                "top": top / 1000,
                "right": right / 1000,
                "bottom": bottom / 1000,
            },
        }


@dataclass(frozen=True)
class QuickAnswerAgreement:
    answer_text: str
    confidence: float
    providers: tuple[str, ...]
    pdf_index: int
    pdf_page: int
    answer_bboxes: Mapping[str, tuple[int, int, int, int]]
    source_relative_paths: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_text": self.answer_text,
            "confidence": self.confidence,
            "providers": list(self.providers),
            "pdf_index": self.pdf_index,
            "pdf_page": self.pdf_page,
            "answer_bboxes": {
                provider: list(bbox) for provider, bbox in sorted(self.answer_bboxes.items())
            },
            "source_relative_paths": list(self.source_relative_paths),
        }


@dataclass(frozen=True)
class SolutionDecision:
    unit_id: str
    printed_number: str
    status: str
    confidence: float
    candidates: tuple[SolutionCandidate, ...]
    evidence: tuple[str, ...]
    review_reasons: tuple[str, ...]
    attempts: tuple[str, ...]
    quick_answer_agreement: QuickAnswerAgreement | None = None

    @property
    def candidate(self) -> SolutionCandidate | None:
        return self.candidates[0] if self.candidates else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "printed_number": self.printed_number,
            "status": self.status,
            "confidence": self.confidence,
            "candidate_ids": [candidate.candidate_id for candidate in self.candidates],
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "evidence": list(self.evidence),
            "review_reasons": list(self.review_reasons),
            "attempts": list(self.attempts),
            "quick_answer_agreement": (
                self.quick_answer_agreement.to_dict()
                if self.quick_answer_agreement is not None
                else None
            ),
        }

    def to_link_item(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "answer_ids": [],
            "explanation_ids": (
                [candidate.candidate_id for candidate in self.candidates]
                if self.status == "verified"
                else []
            ),
            "candidate_ids": [candidate.candidate_id for candidate in self.candidates],
            "solution_printed_pages": list(
                dict.fromkeys(
                    candidate.printed_page
                    for candidate in self.candidates
                    if candidate.printed_page is not None
                )
            ),
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "review_reasons": list(self.review_reasons),
        }


@dataclass(frozen=True)
class ReconciliationResult:
    items: Mapping[str, SolutionDecision]

    def __getitem__(self, unit_id: str) -> SolutionDecision:
        return self.items[unit_id]

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": {
                unit_id: decision.to_dict()
                for unit_id, decision in sorted(self.items.items())
            }
        }


@dataclass(frozen=True)
class CroppedAsset:
    path: Path
    sha256: str
    size_bytes: int
    width_px: int
    height_px: int
    media_type: str = "image/png"
    created: bool = True


@dataclass(frozen=True)
class EnrichmentResult:
    updated_item_ids: tuple[str, ...]
    preserved_verified_item_ids: tuple[str, ...]
    missing_item_ids: tuple[str, ...]
    added_candidate_count: int
    skipped_changed_item_ids: tuple[str, ...] = ()


def parse_printed_number(value: str) -> PrintedNumber | None:
    if not isinstance(value, str):
        return None
    match = _PRINTED_NUMBER.fullmatch(value)
    if not match:
        return None
    prefix = match.group("prefix") or ''
    if prefix.isascii():
        prefix = prefix.upper()
    sub = match.group("sub")
    return PrintedNumber(prefix, match.group("number"), f"({sub})" if sub else None)


def _number_key(value: Any) -> str:
    text = str(value or "").strip()
    return normalize_parent_number(text) if text else text


def _subquestion_key(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    if not text:
        return None
    return _number_key(text).casefold()


def _unique_strings(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(value) for value in values if value is not None and str(value)
        )
    )


def _confidence(*values: Any) -> float:
    numbers = [
        float(value)
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    return max(0.0, min(1.0, min(numbers, default=0.0)))


def _is_reparse_point(path: Path) -> bool:
    try:
        details = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(
        getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _reject_symlink_chain(root: Path, path: Path) -> None:
    root = root.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise SolutionIntegrityError(f"path escapes leaf run: {path}") from exc
    current = root
    if _is_reparse_point(current):
        raise SolutionIntegrityError(f"symlink/reparse leaf is not allowed: {current}")
    for part in relative.parts:
        current /= part
        if _is_reparse_point(current):
            raise SolutionIntegrityError(f"symlink/reparse source is not allowed: {current}")


def _safe_relative_file(root: Path, relative: str) -> tuple[Path, str]:
    if not isinstance(relative, str) or not relative.strip():
        raise SolutionIntegrityError("source path is missing")
    normalized = relative.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or ":" in normalized:
        raise SolutionIntegrityError(f"unsafe source path: {relative}")
    candidate = root.joinpath(*pure.parts)
    _reject_symlink_chain(root, candidate)
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise SolutionIntegrityError(f"source path escapes leaf run: {relative}") from exc
    if not resolved.is_file():
        raise SolutionIntegrityError(f"source file does not exist: {relative}")
    return resolved, resolved.relative_to(resolved_root).as_posix()


def _load_safe_json(root: Path, relative: str) -> dict[str, Any]:
    path, _ = _safe_relative_file(root, relative)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SolutionIntegrityError(f"cannot read source JSON {relative}: {exc}") from exc
    if not isinstance(value, dict):
        raise SolutionIntegrityError(f"source JSON root must be an object: {relative}")
    return value


def _require_hash(path: Path, expected: Any) -> str:
    if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
        raise SolutionIntegrityError(f"missing or invalid SHA-256 for {path.name}")
    actual = sha256_file(path)
    if actual.casefold() != expected.casefold():
        raise SolutionIntegrityError(f"source hash mismatch: {path.name}")
    return actual


def _asset_file(leaf: Path, raw_asset: Mapping[str, Any]) -> tuple[Path, str, str]:
    output_file = raw_asset.get("output_file")
    if not isinstance(output_file, str):
        raise SolutionIntegrityError("solution asset output_file is missing")
    normalized = output_file.replace("\\", "/")
    relative = normalized if normalized.startswith("solutions/") else f"solutions/{normalized}"
    path, safe_relative = _safe_relative_file(leaf, relative)
    return path, safe_relative, _require_hash(path, raw_asset.get("output_sha256"))


def _valid_bbox(value: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        return None
    if any(isinstance(part, bool) or not isinstance(part, (int, float)) for part in value):
        return None
    left, top, right, bottom = (int(round(float(part))) for part in value)
    if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
        return None
    return left, top, right, bottom


def _document_page_count(
    link_manifest: Mapping[str, Any], fallback: int, *, source_document: str = "solution"
) -> int:
    source = link_manifest.get(source_document)
    page_map = source.get("page_map") if isinstance(source, Mapping) else None
    return max(len(page_map), fallback) if isinstance(page_map, list) else max(fallback, 1)


def _document_sha(link_manifest: Mapping[str, Any], raw_asset: Mapping[str, Any]) -> str:
    source_document = raw_asset.get("source_document", "solution")
    if source_document not in {"problem", "solution"}:
        raise SolutionIntegrityError("solution asset source_document is invalid")
    value = raw_asset.get("source_sha256")
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        source = link_manifest.get(source_document)
        value = source.get("source_sha256") if isinstance(source, Mapping) else None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise SolutionIntegrityError("solution document SHA-256 is missing or invalid")
    return value.lower()


def _existing_candidates(
    leaf: Path,
    link_manifest: Mapping[str, Any],
    manifest_item: Mapping[str, Any],
    link_item: Mapping[str, Any],
    asset_ids: Sequence[Any],
    *,
    origin: str,
    attempts: tuple[str, ...],
) -> tuple[SolutionCandidate, ...]:
    assets = link_manifest.get("assets")
    if not isinstance(assets, Mapping):
        return ()
    result: list[SolutionCandidate] = []
    for external_id in asset_ids:
        if not isinstance(external_id, str):
            continue
        raw_asset = assets.get(external_id)
        if raw_asset is None:
            raise SolutionIntegrityError(f"referenced solution asset is missing: {external_id}")
        if not isinstance(raw_asset, Mapping) or raw_asset.get("kind") != "detailed_solution":
            continue
        path, relative, source_hash = _asset_file(leaf, raw_asset)
        parts = raw_asset.get("parts")
        part = parts[0] if isinstance(parts, list) and parts and isinstance(parts[0], Mapping) else {}
        bbox = _valid_bbox(part.get("bbox")) or (0, 0, 1000, 1000)
        pdf_page = int(part.get("pdf_page") or int(part.get("pdf_index") or 0) + 1)
        pdf_index = int(part.get("pdf_index") or max(pdf_page - 1, 0))
        candidate_confidence = _confidence(
            manifest_item.get("confidence"), link_item.get("confidence"), raw_asset.get("confidence")
        )
        source_document = str(raw_asset.get("source_document") or "solution")
        result.append(
            SolutionCandidate(
                candidate_id=external_id,
                origin=origin,
                source_relative_path=relative,
                source_sha256=source_hash,
                source_document=source_document,
                document_sha256=_document_sha(link_manifest, raw_asset),
                document_page_count=_document_page_count(
                    link_manifest, pdf_page, source_document=source_document
                ),
                bbox=bbox,
                pdf_index=max(pdf_index, 0),
                pdf_page=max(pdf_page, 1),
                printed_page=(
                    int(part["printed_page"])
                    if isinstance(part.get("printed_page"), int)
                    else None
                ),
                segment_id=(str(part["segment_id"]) if part.get("segment_id") else None),
                render_dpi=int(raw_asset.get("dpi") or 300),
                tokens=(),
                answer_text=None,
                confidence=candidate_confidence,
                attempts=attempts,
                source_is_precropped=True,
                raw_asset=copy.deepcopy(dict(raw_asset)),
            )
        )
    return tuple(result)


def _independent_verification_consensus(
    leaf: Path, unit_id: str, candidate_id: str
) -> tuple[float, tuple[str, ...]] | None:
    manifest = _load_safe_json(leaf, "solutions/link_manifest.json")
    if decision_reason(leaf, manifest, unit_id) is not None:
        return None
    link = manifest["items"][unit_id]
    if candidate_id not in link.get("explanation_ids", []):
        return None
    return float(link["confidence"]), ("astra-current-evidence",)


def _scope_matches(prefix: str, result: Mapping[str, Any], block: Mapping[str, Any]) -> bool:
    values: list[Any] = []
    for owner in (result, block):
        hierarchy = owner.get("hierarchy_path")
        if isinstance(hierarchy, list):
            values.extend(hierarchy)
    numbering = result.get("numbering_scope")
    if isinstance(numbering, Mapping):
        values.append(numbering.get("scope_label"))
    values.append(block.get("numbering_scope_label"))
    expected = prefix if not prefix.isascii() else prefix.upper()
    for value in values:
        if not isinstance(value, str):
            continue
        for token in _SCOPE_TOKENS.findall(value):
            normalized = token if not token.isascii() else token.upper()
            if normalized == expected:
                return True
    return False


def _entry_text(entries: Sequence[Mapping[str, Any]], include_labels: bool) -> str | None:
    lines = []
    for entry in entries:
        answer = entry.get("explicit_answer_text")
        if not isinstance(answer, str) or not answer.strip():
            continue
        label = str(entry.get("subquestion_label") or "").strip()
        lines.append(f"{label} {answer.strip()}".strip() if include_labels else answer.strip())
    return "\n".join(lines) or None


def _render_page(
    leaf: Path, link_manifest: Mapping[str, Any], pdf_page: int
) -> tuple[Path, str, str]:
    solution = link_manifest.get("solution")
    document_sha = solution.get("source_sha256") if isinstance(solution, Mapping) else None
    candidates: list[Path] = []
    if isinstance(document_sha, str) and _SHA256.fullmatch(document_sha):
        expected = (
            leaf
            / "solutions"
            / "render"
            / document_sha[:12]
            / "pages"
            / f"page-{pdf_page:04d}-dpi200-rot000.jpg"
        )
        if expected.exists():
            candidates.append(expected)
    if not candidates:
        pattern = f"solutions/render/*/pages/page-{pdf_page:04d}-dpi200-rot000.jpg"
        candidates = sorted(leaf.glob(pattern), key=lambda path: path.as_posix().casefold())
    if len(candidates) != 1:
        raise SolutionIntegrityError(
            f"expected one source render for solution page {pdf_page}, found {len(candidates)}"
        )
    path = candidates[0]
    relative = path.absolute().relative_to(leaf.absolute()).as_posix()
    safe_path, safe_relative = _safe_relative_file(leaf, relative)
    return safe_path, safe_relative, sha256_file(safe_path)


def _fallback_matches(
    leaf: Path,
    link_manifest: Mapping[str, Any],
    manifest_item: Mapping[str, Any],
    target: PrintedNumber,
    attempts: tuple[str, ...],
) -> list[SolutionCandidate]:
    result_candidates: list[SolutionCandidate] = []
    consensus_root = leaf / "solutions" / "detections" / "solution-blocks" / "consensus"
    if not consensus_root.is_dir():
        return result_candidates
    for path in sorted(consensus_root.glob("page-*.json"), key=lambda value: value.name):
        relative = path.absolute().relative_to(leaf.absolute()).as_posix()
        document = _load_safe_json(leaf, relative)
        meta = document.get("meta")
        page_result = document.get("result")
        if not isinstance(meta, Mapping) or meta.get("complete_primary_agreement") is not True:
            continue
        if not isinstance(page_result, Mapping):
            continue
        numbering = page_result.get("numbering_scope")
        numbering_confidence = numbering.get("confidence") if isinstance(numbering, Mapping) else None
        blocks = page_result.get("blocks")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, Mapping) or block.get("role") != "detailed_solution":
                continue
            if not _scope_matches(target.prefix, page_result, block):
                continue
            coverage = block.get("coverage")
            if not isinstance(coverage, Mapping):
                continue
            if _number_key(coverage.get("parent_number")) != target.number_key:
                continue
            bbox = _valid_bbox(block.get("bbox"))
            block_confidence = block.get("confidence")
            if bbox is None or not isinstance(block_confidence, (int, float)):
                continue
            if float(block_confidence) < MIN_CONSENSUS_CONFIDENCE:
                continue
            entries = [entry for entry in block.get("entries", []) if isinstance(entry, Mapping)]
            matching_entries = [
                entry
                for entry in entries
                if _number_key(entry.get("parent_number")) == target.number_key
                and _subquestion_key(entry.get("subquestion_label")) == target.subquestion_key
            ]
            if len(matching_entries) != 1:
                continue
            entry = matching_entries[0]
            if target.subquestion_key is not None and len(entries) > 1:
                entry_bbox = _valid_bbox(entry.get("bbox"))
                if entry_bbox is None:
                    # A block-level crop would mix sibling subquestion explanations.
                    continue
                bbox = entry_bbox
            entry_confidence = entry.get("confidence")
            if not isinstance(entry_confidence, (int, float)) or float(entry_confidence) < MIN_CONSENSUS_CONFIDENCE:
                continue
            pdf_page = int(page_result.get("solution_pdf_page") or 0)
            pdf_index = int(page_result.get("solution_pdf_index") or max(pdf_page - 1, 0))
            if pdf_page < 1 or pdf_index < 0:
                continue
            render_path, render_relative, render_hash = _render_page(
                leaf, link_manifest, pdf_page
            )
            confidence = _confidence(
                manifest_item.get("confidence"),
                numbering_confidence,
                block_confidence,
                entry_confidence,
            )
            tokens = _unique_strings(entry.get("stem_fingerprint_tokens") or [])
            answer_text = _entry_text(matching_entries, include_labels=False)
            identity = "\0".join(
                [
                    str(manifest_item.get("unit_id") or ""),
                    target.prefix,
                    target.number_key,
                    target.subquestion_key or "",
                    str(pdf_page),
                    str(block.get("local_id") or ""),
                    str(entry.get("entry_id") or ""),
                ]
            )
            candidate_id = "reconciled-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            printed_page = page_result.get("solution_printed_page")
            solution = link_manifest.get("solution")
            document_sha = solution.get("source_sha256") if isinstance(solution, Mapping) else None
            if not isinstance(document_sha, str) or not _SHA256.fullmatch(document_sha):
                raise SolutionIntegrityError("solution document SHA-256 is missing or invalid")
            segment_id = None
            page_map = solution.get("page_map") if isinstance(solution, Mapping) else None
            if isinstance(page_map, list):
                page_record = next(
                    (
                        record
                        for record in page_map
                        if isinstance(record, Mapping)
                        and int(record.get("pdf_index") or 0) == pdf_index
                    ),
                    None,
                )
                if page_record and page_record.get("segment_id"):
                    segment_id = str(page_record["segment_id"])
            raw_asset = {
                "kind": "detailed_solution",
                "output_file": render_relative,
                "confidence": confidence,
                "parts": [
                    {
                        "pdf_index": pdf_index,
                        "pdf_page": pdf_page,
                        "printed_page": int(printed_page) if isinstance(printed_page, int) else None,
                        "segment_id": segment_id,
                        "bbox": list(bbox),
                    }
                ],
                "output_sha256": render_hash,
                "dpi": 200,
                "source_sha256": document_sha.lower(),
            }
            result_candidates.append(
                SolutionCandidate(
                    candidate_id=candidate_id,
                    origin="consensus_exact_match",
                    source_relative_path=render_relative,
                    source_sha256=render_hash,
                    source_document="solution",
                    document_sha256=document_sha.lower(),
                    document_page_count=_document_page_count(link_manifest, pdf_page),
                    bbox=bbox,
                    pdf_index=pdf_index,
                    pdf_page=pdf_page,
                    printed_page=int(printed_page) if isinstance(printed_page, int) else None,
                    segment_id=segment_id,
                    render_dpi=200,
                    tokens=tokens,
                    answer_text=answer_text,
                    confidence=confidence,
                    attempts=attempts,
                    source_is_precropped=False,
                    raw_asset=raw_asset,
                )
            )
    return result_candidates


def _quick_answer_consensus_evidence(
    leaf: Path, target: PrintedNumber | None
) -> tuple[str, ...]:
    """Record agreed quick-answer scope coverage without inventing an answer."""

    if target is None or target.subquestion_key is not None:
        return ()
    root = leaf / "solutions" / "detections" / "solution-blocks" / "consensus"
    if not root.is_dir():
        return ()
    expected_prefix = target.prefix if not target.prefix.isascii() else target.prefix.upper()
    expected_number = int(target.number_key) if target.number_key.isdecimal() else -1
    for path in sorted(root.glob("page-*.json"), key=lambda value: value.name):
        relative = path.absolute().relative_to(leaf.absolute()).as_posix()
        document = _load_safe_json(leaf, relative)
        meta = document.get("meta")
        result = document.get("result")
        if not isinstance(meta, Mapping) or not isinstance(result, Mapping):
            continue
        providers = {
            str(provider).casefold()
            for provider in meta.get("providers", [])
            if isinstance(provider, str)
        }
        if (
            meta.get("complete_primary_agreement") is not True
            or not {"codex", "gemini"}.issubset(providers)
            or result.get("page_role") != "quick_answer"
        ):
            continue
        numbering = result.get("numbering_scope")
        sequences = numbering.get("visible_number_sequence") if isinstance(numbering, Mapping) else None
        for sequence in sequences if isinstance(sequences, list) else []:
            if not isinstance(sequence, str):
                continue
            for match in _QUICK_ANSWER_RANGE.finditer(sequence):
                prefix = match.group("prefix")
                prefix = prefix if not prefix.isascii() else prefix.upper()
                if (
                    prefix == expected_prefix
                    and int(match.group("start")) <= expected_number <= int(match.group("end"))
                ):
                    return ("quick_answer_codex_gemini_scope_agreement",)
    return ()


def _quick_answer_provider_agreement(
    leaf: Path, target: PrintedNumber | None
) -> QuickAnswerAgreement | None:
    if target is None or target.subquestion_key is not None:
        return None
    matches: dict[str, tuple[str, float, int, int, tuple[int, int, int, int], str]] = {}
    for provider in ("codex", "gemini"):
        provider_root = (
            leaf
            / "solutions"
            / "detections"
            / "solution-quick-targets"
            / "raw"
            / provider
        )
        provider_matches = []
        if not provider_root.is_dir():
            return None
        for path in sorted(provider_root.glob("page-*.json"), key=lambda value: value.name):
            relative = path.absolute().relative_to(leaf.absolute()).as_posix()
            document = _load_safe_json(leaf, relative)
            meta = document.get("meta")
            result = document.get("result")
            if not isinstance(meta, Mapping) or not isinstance(result, Mapping):
                continue
            if str(meta.get("provider") or "").casefold() != provider:
                continue
            blocks = result.get("blocks")
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if (
                    not isinstance(block, Mapping)
                    or block.get("role") != "quick_answer"
                    or not _scope_matches(target.prefix, result, block)
                ):
                    continue
                block_confidence = block.get("confidence")
                for entry in block.get("entries", []):
                    if not isinstance(entry, Mapping):
                        continue
                    if (
                        _number_key(entry.get("parent_number")) != target.number_key
                        or _subquestion_key(entry.get("subquestion_label"))
                        != target.subquestion_key
                    ):
                        continue
                    answer = entry.get("explicit_answer_text")
                    bbox = _valid_bbox(entry.get("answer_bbox"))
                    entry_confidence = entry.get("confidence")
                    if (
                        not isinstance(answer, str)
                        or not answer.strip()
                        or bbox is None
                        or not isinstance(entry_confidence, (int, float))
                    ):
                        continue
                    pdf_page = int(result.get("solution_pdf_page") or 0)
                    pdf_index = int(result.get("solution_pdf_index") or max(pdf_page - 1, 0))
                    if pdf_page < 1 or pdf_index < 0:
                        continue
                    provider_matches.append(
                        (
                            answer.strip(),
                            _confidence(block_confidence, entry_confidence),
                            pdf_index,
                            pdf_page,
                            bbox,
                            relative,
                        )
                    )
        if len(provider_matches) != 1:
            return None
        matches[provider] = provider_matches[0]
    codex, gemini = matches["codex"], matches["gemini"]
    if codex[0] != gemini[0] or codex[2:4] != gemini[2:4]:
        return None
    return QuickAnswerAgreement(
        answer_text=codex[0],
        confidence=_confidence(codex[1], gemini[1]),
        providers=("codex", "gemini"),
        pdf_index=codex[2],
        pdf_page=codex[3],
        answer_bboxes={"codex": codex[4], "gemini": gemini[4]},
        source_relative_paths=(codex[5], gemini[5]),
    )


def reconcile_solutions(leaf_run: Path | str) -> ReconciliationResult:
    """Return a deterministic best-effort decision for every manifest item.

    Existing verified links keep their upstream status. Non-verified items exhaust
    stored candidates, independent verification, exact consensus, and quick-answer
    agreement before receiving a final status.
    """

    leaf = Path(leaf_run).expanduser().absolute()
    _reject_symlink_chain(leaf, leaf)
    manifest = _load_safe_json(leaf, "manifest.json")
    link_manifest = _load_safe_json(leaf, "solutions/link_manifest.json")
    manifest_items = manifest.get("items")
    link_items = link_manifest.get("items")
    if not isinstance(manifest_items, list):
        raise SolutionIntegrityError("manifest.items must be an array")
    if not isinstance(link_items, Mapping):
        raise SolutionIntegrityError("link_manifest.items must be an object")

    decisions: dict[str, SolutionDecision] = {}
    for manifest_item in manifest_items:
        if not isinstance(manifest_item, Mapping):
            continue
        unit_id = str(manifest_item.get("unit_id") or "")
        printed_number = str(manifest_item.get("printed_number") or "")
        if not unit_id:
            raise SolutionIntegrityError("manifest item unit_id is missing")
        raw_link = link_items.get(unit_id)
        link_item = raw_link if isinstance(raw_link, Mapping) else {}
        evidence = _unique_strings(link_item.get("evidence") or [])
        reasons = _unique_strings(link_item.get("review_reasons") or [])
        assets = link_manifest.get("assets")

        verified_ids = [
            external_id
            for field in ("answer_ids", "explanation_ids")
            for external_id in (
                link_item.get(field) if isinstance(link_item.get(field), list) else []
            )
        ]
        missing_verified_ids = [
            external_id
            for external_id in verified_ids
            if not isinstance(external_id, str)
            or not isinstance(assets, Mapping)
            or assets.get(external_id) is None
            or (
                isinstance(assets.get(external_id), Mapping)
                and assets[external_id].get("selection_status") == "review_only"
            )
        ]
        if link_item.get("status") == "verified" and missing_verified_ids:
            raise SolutionIntegrityError("verified solution asset is missing")

        attempts = ("verified_explanation_ids_checked",)
        authority_reason = decision_reason(leaf, link_manifest, unit_id)
        try:
            verified = (
                _existing_candidates(
                    leaf,
                    link_manifest,
                    manifest_item,
                    link_item,
                    link_item.get("explanation_ids") or [],
                    origin="link_verified",
                    attempts=attempts,
                )
                if link_item.get("status") == "verified"
                else ()
            )
        except SolutionIntegrityError:
            if link_item.get("quality_decision") is None or authority_reason is None:
                raise
            decisions[unit_id] = SolutionDecision(unit_id, printed_number, "review", 0.0,
                (), evidence, _unique_strings((*reasons, authority_reason)), attempts)
            continue
        if verified:
            authority_reason = decision_reason(leaf, link_manifest, unit_id)
            decisions[unit_id] = SolutionDecision(
                unit_id,
                printed_number,
                "verified" if authority_reason is None else "review",
                _confidence(*(candidate.confidence for candidate in verified)),
                verified,
                evidence,
                reasons if authority_reason is None else _unique_strings((*reasons, authority_reason)),
                attempts,
            )
            continue

        if link_item.get("status") == "verified" and link_item.get("explanation_ids") and authority_reason is not None:
            decisions[unit_id] = SolutionDecision(unit_id, printed_number, "review", 0.0,
                (), evidence, _unique_strings((*reasons, authority_reason)), attempts)
            continue

        attempts += ("review_candidate_ids_checked",)
        raw_review_ids = (
            link_item.get("candidate_ids")
            if isinstance(link_item.get("candidate_ids"), list)
            else []
        )
        review_ids = list(
            dict.fromkeys(
                external_id
                for external_id in raw_review_ids
                if isinstance(external_id, str)
            )
        )
        missing_review_ids = [
            external_id
            for external_id in review_ids
            if isinstance(external_id, str)
            and (not isinstance(assets, Mapping) or assets.get(external_id) is None)
        ]
        if missing_review_ids:
            reasons = _unique_strings((*reasons, "review_candidate_asset_missing"))
        available_review_ids = [
            external_id for external_id in review_ids if external_id not in missing_review_ids
        ]
        review_only_ids = {
            external_id
            for external_id in available_review_ids
            if isinstance(assets, Mapping)
            and isinstance(assets.get(external_id), Mapping)
            and assets[external_id].get("selection_status") == "review_only"
        }
        if review_only_ids:
            reasons = _unique_strings((*reasons, "review_only_solution_candidate"))
        eligible_review_ids = (
            available_review_ids
            if link_item.get("status") == "review"
            else [
                external_id
                for external_id in available_review_ids
                if external_id in review_only_ids
            ]
        )
        review_candidates = (
            _existing_candidates(
                leaf,
                link_manifest,
                manifest_item,
                link_item,
                eligible_review_ids,
                origin="link_review_candidate",
                attempts=attempts,
            )
            if eligible_review_ids
            else ()
        )
        attempts += ("independent_verification_reaggregated",)
        verification = (
            _independent_verification_consensus(
                leaf, unit_id, review_candidates[0].candidate_id
            )
            if len(review_candidates) == 1
            else None
        )
        attempts += ("consensus_exact_match_checked",)
        parsed = parse_printed_number(printed_number)
        matches = (
            _fallback_matches(leaf, link_manifest, manifest_item, parsed, attempts)
            if parsed is not None
            else []
        )
        attempts += ("quick_answer_consensus_checked",)
        quick_answer_agreement = _quick_answer_provider_agreement(leaf, parsed)
        quick_answer_evidence = (
            ("quick_answer_codex_gemini_agreement",)
            if quick_answer_agreement is not None
            else _quick_answer_consensus_evidence(leaf, parsed)
        )
        exact_evidence = (
            ("exact_scope_number", "provider_consensus") if len(matches) == 1 else ()
        )
        answer_agrees = bool(
            len(matches) == 1
            and quick_answer_agreement is not None
            and matches[0].answer_text
            and matches[0].answer_text.strip() == quick_answer_agreement.answer_text.strip()
        )
        if answer_agrees:
            exact_evidence += ("detailed_quick_answer_agreement",)

        if (
            verification is not None
            and len(review_candidates) == 1
            and not missing_review_ids
            and not review_only_ids
        ):
            verification_confidence, providers = verification
            decisions[unit_id] = SolutionDecision(
                unit_id,
                printed_number,
                "verified",
                verification_confidence,
                review_candidates,
                _unique_strings(
                    (
                        *evidence,
                        *exact_evidence,
                        *quick_answer_evidence,
                        "independent_verification_consensus",
                        *(f"verification_provider:{provider}" for provider in providers),
                    )
                ),
                (),
                attempts,
                quick_answer_agreement,
            )
            continue

        if review_candidates:
            review_reasons = _unique_strings(
                (*reasons, "independent_verification_inconclusive", "quality_crop_decision_required")
            )
            decisions[unit_id] = SolutionDecision(
                unit_id,
                printed_number,
                "review",
                _confidence(*(candidate.confidence for candidate in review_candidates)),
                review_candidates,
                _unique_strings((*evidence, *exact_evidence, *quick_answer_evidence)),
                review_reasons,
                attempts,
                quick_answer_agreement,
            )
            continue

        if len(matches) == 1:
            candidate = matches[0]
            problem_verification = manifest_item.get("verification")
            problem_verified = bool(
                isinstance(problem_verification, Mapping)
                and problem_verification.get("verdict") == "pass"
                and problem_verification.get("complete_question") is True
                and problem_verification.get("contains_expected_identifier") is True
                and problem_verification.get("contains_other_question") is False
                and problem_verification.get("target_content_clipped") is False
                and problem_verification.get("identifier_clipped") is False
                and problem_verification.get("cut_off_content") is False
            )
            status = "review"
            confidence = (
                _confidence(
                    candidate.confidence,
                    quick_answer_agreement.confidence,
                    problem_verification.get("confidence"),
                )
                if status == "verified" and quick_answer_agreement is not None
                else candidate.confidence
            )
            decisions[unit_id] = SolutionDecision(
                unit_id,
                printed_number,
                status,
                confidence,
                (candidate,),
                _unique_strings((*evidence, *exact_evidence, *quick_answer_evidence)),
                (
                    ()
                    if status == "verified"
                    else _unique_strings((*reasons, "cross_document_verification_pending", "quality_crop_decision_required"))
                ),
                attempts,
                quick_answer_agreement,
            )
            continue

        extra_reason = "ambiguous_exact_scope_number" if len(matches) > 1 else None
        decisions[unit_id] = SolutionDecision(
            unit_id,
            printed_number,
            "missing",
            0.0,
            (),
            _unique_strings((*evidence, *quick_answer_evidence)),
            _unique_strings((*reasons, extra_reason, "best_effort_exhausted")),
            attempts,
            quick_answer_agreement,
        )
    return ReconciliationResult(decisions)


def _destination_is_safe(leaf: Path, destination: Path) -> None:
    leaf_resolved = leaf.resolve()
    destination_absolute = destination.expanduser().absolute()
    try:
        destination_absolute.resolve(strict=False).relative_to(leaf_resolved)
    except ValueError:
        pass
    else:
        raise SolutionIntegrityError("destination must not modify the Question_Crop leaf")
    current = destination_absolute
    while current != current.parent:
        if current.exists() and _is_reparse_point(current):
            raise SolutionIntegrityError(f"symlink/reparse destination is not allowed: {current}")
        current = current.parent


def _source_pdf_crop(
    leaf: Path, candidate: SolutionCandidate
) -> Image.Image | None:
    """Render a high-resolution crop from the hash-bound source PDF when present."""

    link_manifest = _load_safe_json(leaf, "solutions/link_manifest.json")
    source_record = link_manifest.get(candidate.source_document)
    source_pdf = source_record.get("source_pdf") if isinstance(source_record, Mapping) else None
    if not isinstance(source_pdf, str) or not source_pdf.strip():
        return None
    source = Path(source_pdf).expanduser().absolute()
    if not source.is_file():
        return None
    _require_hash(source, candidate.document_sha256)
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(str(source))
        try:
            if len(document) != candidate.document_page_count:
                raise SolutionIntegrityError("solution PDF page count changed")
            page = document[candidate.pdf_index]
            try:
                bitmap = page.render(scale=SOURCE_PDF_RECROP_DPI / 72)
                try:
                    image = bitmap.to_pil()
                    try:
                        left, top, right, bottom = candidate.bbox
                        pixels = (
                            math.floor(image.width * left / 1000),
                            math.floor(image.height * top / 1000),
                            math.ceil(image.width * right / 1000),
                            math.ceil(image.height * bottom / 1000),
                        )
                        return image.crop(pixels)
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            document.close()
    except SolutionIntegrityError:
        raise
    except Exception:
        # The immutable 200-DPI evidence render remains a safe fallback.
        return None


def crop_candidate(
    candidate: SolutionCandidate,
    leaf_run: Path | str,
    destination: Path | str,
) -> CroppedAsset:
    """Create an atomic PNG crop outside the immutable Question_Crop leaf."""

    leaf = Path(leaf_run).expanduser().absolute()
    destination_path = Path(destination).expanduser().absolute()
    if destination_path.suffix.casefold() != ".png":
        raise SolutionIntegrityError("solution crop destination must end in .png")
    _destination_is_safe(leaf, destination_path)
    source, _ = _safe_relative_file(leaf, candidate.source_relative_path)
    _require_hash(source, candidate.source_sha256)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_name(f".{destination_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with Image.open(source) as image:
            image.load()
            if candidate.source_is_precropped:
                cropped = image.copy()
            else:
                cropped = _source_pdf_crop(leaf, candidate)
                if cropped is None:
                    left, top, right, bottom = candidate.bbox
                    pixels = (
                        math.floor(image.width * left / 1000),
                        math.floor(image.height * top / 1000),
                        math.ceil(image.width * right / 1000),
                        math.ceil(image.height * bottom / 1000),
                    )
                    cropped = image.crop(pixels)
            if candidate.source_is_precropped and image.format == "PNG":
                temporary.write_bytes(source.read_bytes())
            else:
                cropped.save(temporary, format="PNG")
            width, height = cropped.size
            cropped.close()
        generated_hash = sha256_file(temporary)
        if destination_path.exists():
            if _is_reparse_point(destination_path) or sha256_file(destination_path) != generated_hash:
                raise SolutionIntegrityError(f"destination already exists with different content: {destination_path}")
            temporary.unlink()
            return CroppedAsset(
                destination_path,
                generated_hash,
                destination_path.stat().st_size,
                width,
                height,
                created=False,
            )
        os.replace(temporary, destination_path)
        return CroppedAsset(
            destination_path,
            generated_hash,
            destination_path.stat().st_size,
            width,
            height,
        )
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def locate_leaf_run(
    project: Mapping[str, Any] | Path | str,
    import_roots: Iterable[Path | str],
) -> Path | None:
    """Locate a leaf by persisted relative file hashes, never by stored absolute path."""

    document = load_json_object(project) if isinstance(project, (Path, str)) else project
    source_import = document.get("source_import") if isinstance(document, Mapping) else None
    if not isinstance(source_import, Mapping):
        return None
    leaf_run_id = source_import.get("leaf_run_id")
    source_files = source_import.get("source_files")
    if not isinstance(leaf_run_id, str) or not isinstance(source_files, list):
        return None
    required: list[tuple[str, str]] = []
    for record in source_files:
        if not isinstance(record, Mapping) or record.get("role") not in {"manifest", "link_manifest"}:
            continue
        relative = record.get("relative_path")
        digest = record.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise SolutionIntegrityError("project source file identity is invalid")
        pure = PurePosixPath(relative.replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts or ":" in relative:
            raise SolutionIntegrityError(f"unsafe persisted source path: {relative}")
        required.append((pure.as_posix(), digest.lower()))
    if not any(relative == "manifest.json" for relative, _ in required):
        return None
    initial_manifest_sha = next(
        digest for relative, digest in required if relative == "manifest.json"
    )

    reconciliation = document.get("source_reconciliation")
    crop_job_id: str | None = None
    if reconciliation is not None:
        if not isinstance(reconciliation, Mapping):
            raise SolutionIntegrityError("project source reconciliation is invalid")
        crop_job_id = reconciliation.get("crop_job_id")
        current_leaf_id = reconciliation.get("leaf_run_id")
        manifest_sha = reconciliation.get("manifest_sha256")
        link_sha = reconciliation.get("current_link_manifest_sha256")
        if (
            not isinstance(crop_job_id, str)
            or _SAFE_ID.fullmatch(crop_job_id) is None
            or current_leaf_id != leaf_run_id
            or not isinstance(manifest_sha, str)
            or _SHA256.fullmatch(manifest_sha) is None
            or manifest_sha.casefold() != initial_manifest_sha
            or not isinstance(link_sha, str)
            or _SHA256.fullmatch(link_sha) is None
        ):
            raise SolutionIntegrityError("project source reconciliation identity is invalid")
        required = [
            ("manifest.json", manifest_sha.casefold()),
            ("solutions/link_manifest.json", link_sha.casefold()),
        ]

    matches: list[tuple[int, str, Path]] = []
    for raw_root in import_roots:
        root = Path(raw_root).expanduser().absolute()
        if not root.is_dir() or _is_reparse_point(root):
            continue
        for manifest_path in root.rglob("manifest.json"):
            leaf = manifest_path.parent.absolute()
            if leaf.name != leaf_run_id:
                continue
            if crop_job_id is not None and (
                leaf.parent.name != "output"
                or leaf.parent.parent.name != crop_job_id
            ):
                continue
            try:
                verified_paths = []
                for relative, digest in required:
                    path, _ = _safe_relative_file(leaf, relative)
                    if sha256_file(path) != digest:
                        raise SolutionIntegrityError("candidate hash mismatch")
                    verified_paths.append(path)
            except SolutionIntegrityError:
                continue
            newest = max(path.stat().st_mtime_ns for path in verified_paths)
            matches.append((newest, leaf.as_posix().casefold(), leaf.resolve()))
    if not matches:
        return None
    matches.sort(key=lambda entry: (-entry[0], entry[1]))
    if crop_job_id is not None and len({entry[2] for entry in matches}) != 1:
        return None
    return matches[0][2]


def record_source_reconciliation(
    project_dir: Path | str,
    leaf_run: Path | str,
    crop_job_id: str,
    *,
    expected_link_manifest_sha256: str | None = None,
    expected_project_revision: int | None = None,
) -> dict[str, Any]:
    """CAS-update the mutable upstream link identity without changing import identity."""

    if _SAFE_ID.fullmatch(crop_job_id) is None:
        raise SolutionIntegrityError("crop job id is invalid")
    project_root = Path(project_dir).expanduser().resolve()
    leaf = Path(leaf_run).expanduser().resolve(strict=True)
    project_path = project_root / "project.json"
    project = load_json_object(project_path)
    source_import = project.get("source_import")
    if (
        not isinstance(source_import, Mapping)
        or source_import.get("leaf_run_id") != leaf.name
        or leaf.parent.name != "output"
        or leaf.parent.parent.name != crop_job_id
    ):
        raise SolutionIntegrityError("project and crop run identity do not match")
    manifest_records = [
        record
        for record in source_import.get("source_files", [])
        if isinstance(record, Mapping) and record.get("role") == "manifest"
    ]
    if (
        len(manifest_records) != 1
        or not isinstance(manifest_records[0].get("sha256"), str)
        or _SHA256.fullmatch(str(manifest_records[0]["sha256"])) is None
    ):
        raise SolutionIntegrityError("project manifest identity is invalid")
    initial_manifest_sha = str(manifest_records[0]["sha256"]).casefold()
    manifest, _ = _safe_relative_file(leaf, "manifest.json")
    link_manifest, _ = _safe_relative_file(leaf, "solutions/link_manifest.json")
    if sha256_file(manifest) != initial_manifest_sha:
        raise SolutionIntegrityError("problem manifest changed after import")
    current_link_sha = sha256_file(link_manifest)
    if (
        expected_link_manifest_sha256 is not None
        and current_link_sha != expected_link_manifest_sha256
    ):
        raise SolutionIntegrityError("solution link manifest changed before anchor update")
    current = project.get("source_reconciliation")
    revision = (
        int(current.get("revision")) + 1
        if isinstance(current, Mapping)
        and isinstance(current.get("revision"), int)
        and not isinstance(current.get("revision"), bool)
        else 1
    )
    project["source_reconciliation"] = {
        "crop_job_id": crop_job_id,
        "leaf_run_id": leaf.name,
        "manifest_sha256": initial_manifest_sha,
        "current_link_manifest_sha256": current_link_sha,
        "revision": revision,
        "applied_at": utc_now(),
    }
    workflow = project.get("workflow")
    current_revision = workflow.get("revision") if isinstance(workflow, Mapping) else None
    if not isinstance(current_revision, int) or isinstance(current_revision, bool):
        raise SolutionIntegrityError("project revision is invalid")
    if (
        expected_project_revision is not None
        and current_revision != expected_project_revision
    ):
        raise SolutionIntegrityError("project changed after relink preflight")
    return save_json_cas(
        project_path,
        project,
        expected_project_revision
        if expected_project_revision is not None
        else current_revision,
    )


def _asset_id(candidate_id: str) -> str:
    return "source-solution-" + hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()[:20]


def refresh_replaced_auto_selection(document: dict[str, Any]) -> bool:
    """Move an automatic selection only to a replacement of the same candidate."""
    provenance = document.get("provenance", {})
    selection = provenance.get("solution_selection")
    review = document.get("review", {})
    if (not isinstance(selection, dict) or selection.get("origin") != "auto_candidate"
        or document.get("workflow", {}).get("status") in {"content_ready", "export_ready"}
        or any(review.get(key) is not None for key in
               ("approved_revision", "approved_sha256", "reviewer"))):
        return False
    selected_id = selection.get("source_asset_id")
    revision = selection.get("revision")
    if not isinstance(selected_id, str) or type(revision) is not int:
        return False
    matches = []
    for link in provenance.get("solution_links", []):
        external_id = link.get("external_asset_id")
        if (not isinstance(external_id, str) or link.get("status") not in {"review", "verified"}
            or any("rejected" in str(reason) or "excluded" in str(reason)
                   for reason in (link.get("review_reasons") or []))):
            continue
        base = _asset_id(external_id)
        if selected_id == base or re.fullmatch(re.escape(base) + r"-[0-9a-f]{12}", selected_id):
            matches.append(link)
    if len(matches) != 1:
        return False
    replacement_id = matches[0].get("source_asset_id")
    assets = [asset for asset in document.get("resources", {}).get("assets", [])
              if asset.get("asset_id") == replacement_id and asset.get("role") == "source_crop"
              and asset.get("state", "ready") == "ready"]
    if len(assets) != 1 or not _SHA256.fullmatch(str(assets[0].get("sha256", ""))):
        return False
    replacement_sha = assets[0]["sha256"]
    if replacement_id == selected_id and replacement_sha == selection.get("source_sha256"):
        return False
    provenance["solution_selection"] = {
        "source_asset_id": replacement_id, "source_sha256": replacement_sha,
        "revision": revision + 1, "origin": "auto_candidate", "selected_at": utc_now(),
    }
    return True


def _append_issue(document: dict[str, Any], issue: str) -> bool:
    review = document.get("review")
    if not isinstance(review, dict):
        return False
    issues = review.get("issues")
    if not isinstance(issues, list):
        return False
    if issue in issues:
        return False
    issues.append(issue)
    return True


def _remove_issue(document: dict[str, Any], issue: str) -> bool:
    review = document.get("review")
    issues = review.get("issues") if isinstance(review, dict) else None
    if not isinstance(issues, list) or issue not in issues:
        return False
    issues[:] = [value for value in issues if value != issue]
    return True


def enrich_project_from_leaf(
    project_dir: Path | str,
    leaf_run: Path | str,
    *,
    preserve_approved: bool = False,
    expected_link_manifest_sha256: str | None = None,
    expected_project_revision: int | None = None,
    item_ids: Sequence[str] | None = None,
    expected_item_sha256: Mapping[str, str] | None = None,
) -> EnrichmentResult:
    """Backfill review solution crops without overwriting verified links or content."""

    project_root = Path(project_dir).expanduser().resolve()
    leaf = Path(leaf_run).expanduser().absolute()
    link_manifest_path = leaf / "solutions" / "link_manifest.json"
    if (
        expected_link_manifest_sha256 is not None
        and sha256_file(link_manifest_path) != expected_link_manifest_sha256
    ):
        raise SolutionIntegrityError("solution link manifest changed before reconciliation")
    project = load_json_object(project_root / "project.json")
    project_order = project.get("item_order")
    if not isinstance(project_order, list):
        raise SolutionIntegrityError("project item order is invalid")
    scoped_items: set[str] | None = None
    if item_ids is not None:
        if (
            any(not isinstance(item_id, str) or _SAFE_ID.fullmatch(item_id) is None for item_id in item_ids)
            or len(set(item_ids)) != len(item_ids)
            or not set(item_ids).issubset(project_order)
        ):
            raise SolutionIntegrityError("recovery item scope is invalid")
        scoped_items = set(item_ids)
    expected_hashes: dict[str, str] | None = None
    if expected_item_sha256 is not None:
        if scoped_items is None or set(expected_item_sha256) != scoped_items or any(
            not isinstance(value, str) or _SHA256.fullmatch(value) is None
            for value in expected_item_sha256.values()
        ):
            raise SolutionIntegrityError("recovery item hashes are invalid")
        expected_hashes = {
            item_id: value.casefold() for item_id, value in expected_item_sha256.items()
        }
    project_workflow = project.get("workflow")
    if (
        expected_project_revision is not None
        and (
            not isinstance(project_workflow, Mapping)
            or project_workflow.get("revision") != expected_project_revision
        )
    ):
        raise SolutionIntegrityError("project changed after relink preflight")
    reconciliation = reconcile_solutions(leaf)
    if (
        expected_link_manifest_sha256 is not None
        and sha256_file(link_manifest_path) != expected_link_manifest_sha256
    ):
        raise SolutionIntegrityError("solution link manifest changed during reconciliation")
    updated: list[str] = []
    preserved: list[str] = []
    missing: list[str] = []
    skipped_changed: list[str] = []
    added_count = 0
    for item_id in project_order:
        if not isinstance(item_id, str):
            continue
        if scoped_items is not None and item_id not in scoped_items:
            continue
        item_path = project_root / "items" / item_id / "question.json"
        if expected_hashes is not None and (
            not item_path.is_file() or sha256_file(item_path) != expected_hashes[item_id]
        ):
            skipped_changed.append(item_id)
            continue
        item = load_json_object(item_path)
        workflow = item.get("workflow")
        review = item.get("review")
        if preserve_approved and (
            (
                isinstance(workflow, Mapping)
                and workflow.get("status") in {"content_ready", "export_ready"}
            )
            or (
                isinstance(review, Mapping)
                and (
                    (
                        isinstance(review.get("approved_revision"), int)
                        and not isinstance(review.get("approved_revision"), bool)
                    )
                    or isinstance(review.get("approved_sha256"), str)
                    or isinstance(review.get("reviewer"), str)
                )
            )
        ):
            preserved.append(item_id)
            continue
        provenance = item.get("provenance")
        if not isinstance(provenance, dict):
            continue
        problem = provenance.get("problem")
        raw_snapshot = provenance.get("raw_snapshot")
        manifest_item = raw_snapshot.get("manifest_item") if isinstance(raw_snapshot, Mapping) else None
        unit_id = (
            problem.get("unit_id")
            if isinstance(problem, Mapping) and isinstance(problem.get("unit_id"), str)
            else manifest_item.get("unit_id") if isinstance(manifest_item, Mapping) else None
        )
        decision = reconciliation.items.get(unit_id) if isinstance(unit_id, str) else None
        if decision is None:
            continue
        links = provenance.get("solution_links")
        if not isinstance(links, list):
            continue
        if any(
            isinstance(link, Mapping)
            and (
                "manual_user_confirmation" in (link.get("evidence") or [])
                or (
                    link.get("kind") in {"detailed_solution", "mixed"}
                    and link.get("status") == "verified"
                    and stored_reason(item, item_path.parent) is None
                )
            )
            for link in links
        ):
            preserved.append(item_id)
            continue

        candidate_document = copy.deepcopy(item)
        candidate_provenance = candidate_document["provenance"]
        candidate_links = candidate_provenance["solution_links"]
        candidate_snapshot = candidate_provenance.get("raw_snapshot")
        changed = False
        if decision.status != "verified":
            for existing_link in candidate_links:
                if isinstance(existing_link, dict) and existing_link.get("status") == "verified":
                    if (existing_link.get('kind') == 'quick_answer'
                            and stored_reason(item, item_path.parent) is None):
                        continue
                    existing_link.update(status="review", review_reasons=list(decision.review_reasons))
                    changed = True
        if isinstance(candidate_snapshot, dict):
            snapshot_link = candidate_snapshot.get("link_item")
            snapshot_link = copy.deepcopy(snapshot_link) if isinstance(snapshot_link, Mapping) else {}
            reconciliation_snapshot = decision.to_dict()
            current_manifest = _load_safe_json(leaf, "solutions/link_manifest.json")
            current_link = current_manifest["items"].get(unit_id, {})
            snapshot_update = {
                "quality_origin": {"leaf": str(leaf), "unit_id": unit_id,
                                   "decision_sha256": identity(current_link)},
                "best_effort_reconciliation": reconciliation_snapshot,
                "attempts": list(decision.attempts),
                "best_effort_exhausted": decision.status == "missing",
                "status": decision.status,
                "confidence": decision.confidence,
                "evidence": list(decision.evidence),
                "review_reasons": list(decision.review_reasons),
                "candidate_ids": [candidate.candidate_id for candidate in decision.candidates],
                "quick_answer_agreement": (
                    decision.quick_answer_agreement.to_dict()
                    if decision.quick_answer_agreement is not None
                    else None
                ),
            }
            if any(snapshot_link.get(key) != value for key, value in snapshot_update.items()):
                snapshot_link.setdefault("upstream_status", snapshot_link.get("status"))
                snapshot_link.setdefault(
                    "upstream_confidence", snapshot_link.get("confidence")
                )
                snapshot_link.setdefault(
                    "upstream_evidence", copy.deepcopy(snapshot_link.get("evidence"))
                )
                snapshot_link.setdefault(
                    "upstream_review_reasons",
                    copy.deepcopy(snapshot_link.get("review_reasons")),
                )
                snapshot_link.setdefault(
                    "upstream_candidate_ids",
                    copy.deepcopy(snapshot_link.get("candidate_ids")),
                )
                snapshot_link.update(snapshot_update)
                candidate_snapshot["link_item"] = snapshot_link
                changed = True

        missing_candidate_issue = "solution_link:review_candidate_asset_missing"
        if "review_candidate_asset_missing" in decision.review_reasons:
            changed |= _append_issue(candidate_document, missing_candidate_issue)
        else:
            changed |= _remove_issue(candidate_document, missing_candidate_issue)

        if decision.status == "missing":
            missing.append(item_id)
            changed |= _remove_issue(
                candidate_document, "solution_link:cross_document_verification_pending"
            )
            changed |= _append_issue(candidate_document, "solution_link:best_effort_exhausted")
            if decision.quick_answer_agreement is not None:
                changed |= _append_issue(
                    candidate_document, "quick_answer:codex_gemini_agreement"
                )
        else:
            resources = candidate_document.get("resources")
            assets = resources.get("assets") if isinstance(resources, dict) else None
            if not isinstance(assets, list):
                continue
            existing_external = {
                link.get("external_asset_id")
                for link in candidate_links
                if isinstance(link, Mapping)
            }
            existing_asset_ids = {
                asset.get("asset_id") for asset in assets if isinstance(asset, Mapping)
            }
            snapshot_assets = (
                candidate_snapshot.get("link_assets") if isinstance(candidate_snapshot, dict) else None
            )
            existing_by_external = {
                link.get("external_asset_id"): link
                for link in candidate_links
                if isinstance(link, dict) and isinstance(link.get("external_asset_id"), str)
            }
            repaired_candidate_ids = {
                candidate.candidate_id
                for candidate in decision.candidates
                if _candidate_is_boundary_seam_repaired(candidate)
            }
            for candidate in decision.candidates:
                local_asset_id = _asset_id(candidate.candidate_id)
                repaired = _candidate_is_boundary_seam_repaired(candidate)
                candidate_evidence = [
                    value
                    for value in decision.evidence
                    if value not in {BOUNDARY_SEAM_REPAIRED, "manual_user_confirmation"}
                ]
                candidate_review_reasons = [
                    value
                    for value in decision.review_reasons
                    if value != BOUNDARY_SEAM_REPAIRED
                ]
                if repaired:
                    candidate_evidence.append(BOUNDARY_SEAM_REPAIRED)
                    candidate_review_reasons.append(BOUNDARY_SEAM_REPAIRED)
                desired_link = {
                    "kind": "detailed_solution",
                    "external_asset_id": candidate.candidate_id,
                    "source_asset_id": local_asset_id,
                    "source_document": candidate.source_document,
                    "document_sha256": candidate.document_sha256,
                    "document_page_count": candidate.document_page_count,
                    "status": decision.status,
                    "confidence": decision.confidence,
                    "evidence": list(dict.fromkeys(candidate_evidence)),
                    "review_reasons": list(
                        dict.fromkeys(candidate_review_reasons)
                    ),
                    "parts": candidate.to_solution_parts(),
                }
                existing_link = existing_by_external.get(candidate.candidate_id)
                replaced_link = None
                if existing_link is not None:
                    existing_resource = next((entry for entry in assets
                        if isinstance(entry, Mapping) and entry.get("asset_id") == existing_link.get("source_asset_id")), None)
                    if candidate.source_is_precropped and isinstance(existing_resource, Mapping) and existing_resource.get("sha256") != candidate.source_sha256:
                        local_asset_id += "-" + candidate.source_sha256[:12]
                        desired_link["source_asset_id"] = local_asset_id
                        replaced_link, existing_link = existing_link, None
                if existing_link is not None:
                    existing_source_asset_id = existing_link.get("source_asset_id")
                    if existing_source_asset_id not in existing_asset_ids:
                        raise SolutionIntegrityError(
                            f"registered solution asset is missing: {candidate.candidate_id}"
                        )
                    desired_link["source_asset_id"] = existing_source_asset_id
                    if (
                        repaired_candidate_ids
                        and not _link_is_boundary_seam_repaired(existing_link)
                    ):
                        continue
                    if any(
                        existing_link.get(key) != value
                        for key, value in desired_link.items()
                    ):
                        existing_link.update(desired_link)
                        changed = True
                    continue
                if local_asset_id in existing_asset_ids:
                    raise SolutionIntegrityError(f"local solution asset ID conflict: {local_asset_id}")
                filename = f"explanation-{local_asset_id.removeprefix('source-solution-')}.png"
                destination = item_path.parent / "source" / filename
                cropped = crop_candidate(candidate, leaf, destination)
                assets.append(
                    {
                        "asset_id": local_asset_id,
                        "section": "explanation",
                        "role": "source_crop",
                        "state": "ready",
                        "path": destination.relative_to(item_path.parent).as_posix(),
                        "media_type": "image/png",
                        "width_px": cropped.width_px,
                        "height_px": cropped.height_px,
                        "sha256": cropped.sha256,
                        "generation": None,
                        "review": {
                            "status": "unreviewed",
                            "boundary_uncertain": False,
                            "notes": ["cross_document_verification_pending"],
                        },
                    }
                )
                if replaced_link is not None:
                    replaced_link.update(desired_link)
                else:
                    candidate_links.append(desired_link)
                if isinstance(snapshot_assets, list):
                    snapshot_assets.append(
                        {"asset_id": candidate.candidate_id, **candidate.to_link_asset()}
                    )
                existing_asset_ids.add(local_asset_id)
                existing_external.add(candidate.candidate_id)
                existing_by_external[candidate.candidate_id] = desired_link
                added_count += 1
                changed = True
            if decision.candidates and decision.status == "review":
                changed |= _remove_issue(
                    candidate_document, "solution_link:best_effort_exhausted"
                )
                changed |= _append_issue(
                    candidate_document, "solution_link:cross_document_verification_pending"
                )
            elif decision.candidates and decision.status == "verified":
                changed |= _remove_issue(
                    candidate_document, "solution_link:best_effort_exhausted"
                )
                changed |= _remove_issue(
                    candidate_document, "solution_link:cross_document_verification_pending"
                )
            if decision.candidates:
                review = candidate_document.get("review")
                if isinstance(review, dict):
                    review["assets"] = "unreviewed"
                    review["explanation"] = "unreviewed"
                    coverage = review.get("coverage")
                    if isinstance(coverage, dict):
                        coverage["explanation"] = "unreviewed"
        changed |= refresh_replaced_auto_selection(candidate_document)
        if changed:
            current_manifest = _load_safe_json(leaf, "solutions/link_manifest.json")
            bind_import_origin(candidate_document, leaf, unit_id,
                               current_manifest["items"].get(unit_id, {}), item_path.parent)
            revision = item.get("workflow", {}).get("revision")
            if not isinstance(revision, int):
                raise SolutionIntegrityError(f"invalid item revision: {item_id}")
            save_json_cas(item_path, candidate_document, revision)
            updated.append(item_id)
    if (
        expected_link_manifest_sha256 is not None
        and sha256_file(link_manifest_path) != expected_link_manifest_sha256
    ):
        raise SolutionIntegrityError("solution link manifest changed during project update")
    return EnrichmentResult(
        tuple(updated), tuple(preserved), tuple(missing), added_count,
        tuple(skipped_changed),
    )


# Explicit aliases make the intended importer-facing names easy to discover.
reconcile_leaf = reconcile_solutions
crop_solution_candidate = crop_candidate
