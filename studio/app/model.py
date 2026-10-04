"""Small, file-backed domain model for Question JSON Studio."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .equation_bridge import normalize_equations

from astra_runtime import inside
from astra_cli.quality_manifest import stored_reason
from astra_cli.quality_reads import file_sha256
from astra_cli.quality_studio import current_receipt_result


SCHEMA_VERSION = "0.1.0"
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def solution_content_hash(document: Mapping[str, Any]) -> str:
    content = document.get("content", {})
    value = {"explanation": content.get("explanation"), "answer": content.get("answer")}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class ModelError(ValueError):
    """Raised when a persisted model does not satisfy the storage contract."""


class RevisionConflict(ModelError):
    """Raised when a compare-and-swap save observes a stale revision."""

    def __init__(self, expected: int, current: int) -> None:
        self.expected = expected
        self.current = current
        super().__init__(f"revision conflict: expected {expected}, current {current}")


class ModelNotFound(ModelError):
    """Raised when a requested item-owned resource does not exist."""


# ponytail: process-local locks fit the single desktop server; add an OS lock if
# multiple Question JSON Studio processes must edit the same workspace.
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
FIGURE_ASSET_ROLES = frozenset({"figure_original", "figure_regenerated"})
FIGURE_REVIEWABLE_ASSET_ROLES = FIGURE_ASSET_ROLES | {"table_snapshot"}
FIGURE_VISUAL_KINDS = frozenset(
    {"pure_figure", "mixed_text_figure", "table_snapshot", "unknown"}
)
FIGURE_PLACEMENT_CONTEXTS = frozenset(
    {"body", "bogi", "choice", "shared_box", "table_cell"}
)
_ADVISORY_REVIEW_ISSUE = re.compile(
    r"^(?:OCR warning:|Ignored source region:|Pruned \d+|Filtered \d+|"
    r"Filtered sibling |solution_link:|quick_answer:|box role |Ignored empty OCR box)",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def is_advisory_review_issue(value: Any) -> bool:
    return isinstance(value, str) and (
        bool(_ADVISORY_REVIEW_ISSUE.match(value.strip())) or "�" in value
    )


def validate_id(value: str, *, field: str = "id") -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ModelError(f"{field} must match {ID_PATTERN.pattern}")
    return value


def stable_item_id(source_sha256: str, unit_id: str) -> str:
    """Return the UUIDv5 identity defined by source document + legacy unit."""

    if not re.fullmatch(r"[0-9a-fA-F]{64}", source_sha256 or ""):
        raise ModelError("source_sha256 must be 64 hexadecimal characters")
    if not isinstance(unit_id, str) or not unit_id:
        raise ModelError("unit_id must be a non-empty string")
    name = f"question-json-studio:item:{source_sha256.lower()}:{unit_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def stable_project_id(import_fingerprint_sha256: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", import_fingerprint_sha256 or ""):
        raise ModelError("import fingerprint must be 64 hexadecimal characters")
    name = f"question-json-studio:project:{import_fingerprint_sha256.lower()}"
    return f"project-{uuid.uuid5(uuid.NAMESPACE_URL, name)}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json_object(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ModelError(f"JSON root must be an object: {path}")
    return value


def atomic_write_json(path: Path | str, document: Mapping[str, Any]) -> None:
    """Write JSON through a same-directory temporary file and atomic promote."""

    path = inside(Path(path).resolve(), "output")
    if not isinstance(document, Mapping):
        raise ModelError("document must be an object")
    io_path = Path("\\\\?\\" + str(path)) if os.name == "nt" else path
    io_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = io_path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        from .atomic_file import replace_file
        replace_file(temporary, io_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _path_lock(path: Path) -> threading.Lock:
    resolved = path.resolve()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(resolved, threading.Lock())


def _workflow_revision(document: Mapping[str, Any], *, path: Path) -> int:
    workflow = document.get("workflow")
    revision = workflow.get("revision") if isinstance(workflow, Mapping) else None
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ModelError(f"workflow.revision must be a positive integer: {path}")
    return revision


def save_json_cas(
    path: Path | str,
    document: Mapping[str, Any],
    expected_revision: int,
    *,
    preserve_approval: bool = False,
) -> dict[str, Any]:
    """Atomically save a project/item when its revision still matches.

    A missing file has revision 0. The returned document receives the next
    revision; the caller's mapping is never mutated.
    """

    path = Path(path)
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise ModelError("expected_revision must be an integer")
    if expected_revision < 0:
        raise ModelError("expected_revision cannot be negative")
    if not isinstance(document, Mapping):
        raise ModelError("document must be an object")

    with _path_lock(path):
        current_document = load_json_object(path) if path.exists() else None
        current_revision = (
            _workflow_revision(current_document, path=path) if current_document else 0
        )
        if current_revision != expected_revision:
            raise RevisionConflict(expected_revision, current_revision)

        candidate = copy.deepcopy(dict(document))
        workflow = candidate.get("workflow")
        if not isinstance(workflow, dict):
            raise ModelError("document.workflow must be an object")

        if current_document:
            for identity_field in ("project_id", "item_id"):
                old_identity = current_document.get(identity_field)
                new_identity = candidate.get(identity_field)
                if old_identity is not None and old_identity != new_identity:
                    raise ModelError(f"cannot change {identity_field} during save")
            old_workflow = current_document["workflow"]
            workflow["created_at"] = old_workflow.get("created_at")
        else:
            workflow.setdefault("created_at", utc_now())

        workflow["revision"] = current_revision + 1
        workflow["updated_at"] = utc_now()
        if not preserve_approval and "item_id" in candidate:
            review = candidate.get("review")
            if isinstance(review, dict):
                review["approved_revision"] = None
                review["approved_sha256"] = None
                review["reviewer"] = None
                review["reviewed_at"] = None
                coverage = review.get("coverage")
                if isinstance(coverage, dict):
                    if coverage.get("question") != "not_applicable":
                        coverage["question"] = "unreviewed"
                    if coverage.get("explanation") != "not_applicable":
                        coverage["explanation"] = "unreviewed"
            content = candidate.get("content")
            has_transcribed_content = isinstance(content, Mapping) and any(
                content.get(section) is not None
                for section in ("question", "answer", "explanation")
            )
            if has_transcribed_content or workflow.get("status") != "source_only":
                workflow["status"] = "needs_review"
        if not preserve_approval:
            normalize_equations(candidate.get("content"))
        atomic_write_json(path, candidate)
        return candidate


def update_tags_cas(
    path: Path | str,
    expected_revision: int,
    tags: Mapping[str, Any],
) -> dict[str, Any]:
    """Replace item tags without invalidating an approved content revision."""

    if not isinstance(tags, Mapping):
        raise ModelError("tags must be an object")
    path = Path(path)
    current = load_json_object(path)
    current_revision = _workflow_revision(current, path=path)
    if current_revision != expected_revision:
        raise RevisionConflict(expected_revision, current_revision)

    candidate = copy.deepcopy(current)
    candidate["tags"] = copy.deepcopy(dict(tags))
    workflow = candidate.get("workflow")
    if isinstance(workflow, Mapping) and workflow.get("status") == "content_ready":
        review = candidate.get("review")
        if not isinstance(review, dict):
            raise ModelError("content_ready document.review must be an object")
        review["approved_revision"] = expected_revision + 1
        review["approved_sha256"] = approval_digest(candidate)
    return save_json_cas(
        path,
        candidate,
        expected_revision,
        preserve_approval=True,
    )


def approval_digest(document: Mapping[str, Any]) -> str:
    assets = []
    resources = document.get("resources")
    if isinstance(resources, Mapping) and isinstance(resources.get("assets"), list):
        for asset in resources["assets"]:
            if not isinstance(asset, Mapping):
                continue
            assets.append(
                {
                    key: asset.get(key)
                    for key in (
                        "asset_id",
                        "section",
                        "role",
                        "state",
                        "path",
                        "media_type",
                        "sha256",
                        "width_px",
                        "height_px",
                        "source_region",
                        "derived_from_asset_id",
                    )
                }
            )
    assets.sort(key=lambda item: str(item.get("asset_id")))
    provenance = document.get("provenance")
    payload = {
        "identity": document.get("identity"),
        "content": document.get("content"),
        "source": {
            "problem": provenance.get("problem") if isinstance(provenance, Mapping) else None,
            "solution_links": provenance.get("solution_links") if isinstance(provenance, Mapping) else None,
        },
        "assets": assets,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def registration_summary(
    document: Mapping[str, Any], *, warning_count: int | None = None
) -> dict[str, Any]:
    """Describe whether a stored item can be used without changing review evidence."""

    workflow = document.get("workflow")
    workflow_status = (
        str(workflow.get("status")) if isinstance(workflow, Mapping) else "unknown"
    )
    content = document.get("content")
    question = content.get("question") if isinstance(content, Mapping) else None
    blocks = question.get("blocks") if isinstance(question, Mapping) else None
    has_structured_question = isinstance(blocks, list) and bool(blocks)

    resources = document.get("resources")
    assets = resources.get("assets") if isinstance(resources, Mapping) else None
    source_fallback_available = any(
        isinstance(asset, Mapping)
        and asset.get("role") == "source_crop"
        and asset.get("section") in {"question", "shared"}
        and asset.get("state") == "ready"
        for asset in (assets if isinstance(assets, list) else [])
    )
    usable = has_structured_question or source_fallback_available

    if warning_count is None:
        review = document.get("review")
        issues = review.get("issues") if isinstance(review, Mapping) else None
        warning_count = len(issues) if isinstance(issues, list) else 0
    if isinstance(warning_count, bool) or not isinstance(warning_count, int):
        raise ModelError("warning_count must be an integer")
    warning_count = max(warning_count, 0)
    if workflow_status not in {"content_ready", "export_ready"} or not usable:
        warning_count = max(warning_count, 1)

    return {
        "status": "registered" if usable else "unavailable",
        "usable": usable,
        "content_mode": (
            "structured" if has_structured_question
            else "source_fallback" if source_fallback_available
            else "unavailable"
        ),
        "source_fallback_available": source_fallback_available,
        "warning_count": warning_count,
        "workflow_status": workflow_status,
    }


def solution_link_summary(document: Mapping[str, Any], *, item_root: Path | None = None) -> dict[str, Any]:
    """Derive the visible solution-link tag without changing stored evidence."""

    provenance = document.get("provenance")
    links = provenance.get("solution_links") if isinstance(provenance, Mapping) else None
    if isinstance(links, list):
        review = document.get("review")
        approved = isinstance(review, Mapping) and type(review.get("approved_revision")) is int
        needs_current_check = any(
            isinstance(link, Mapping)
            and link.get("status") == "verified"
            and "manual_user_confirmation" not in (link.get("evidence") or [])
            for link in links
        )
        reason = stored_reason(document, item_root) if needs_current_check and not approved else None
        links = [dict(link, status="review", review_reasons=[*(link.get("review_reasons") or []), reason])
                 if isinstance(link, Mapping) and link.get("status") == "verified"
                 and "manual_user_confirmation" not in (link.get("evidence") or []) and reason is not None
                 else link for link in links]
    detailed = [
        link
        for link in (links if isinstance(links, list) else [])
        if isinstance(link, Mapping) and link.get("kind") in {"detailed_solution", "mixed"}
    ]
    quick_answers = [
        link
        for link in (links if isinstance(links, list) else [])
        if isinstance(link, Mapping) and link.get("kind") == "quick_answer"
    ]
    raw_snapshot = provenance.get("raw_snapshot") if isinstance(provenance, Mapping) else None
    raw_link = raw_snapshot.get("link_item") if isinstance(raw_snapshot, Mapping) else None
    raw_candidates = raw_link.get("candidate_ids") if isinstance(raw_link, Mapping) else None
    attempts = raw_link.get("attempts") if isinstance(raw_link, Mapping) else None
    content = document.get("content")
    explanation = content.get("explanation") if isinstance(content, Mapping) else None
    explanation_blocks = explanation.get("blocks") if isinstance(explanation, Mapping) else None
    selection = provenance.get("solution_selection") if isinstance(provenance, Mapping) else None
    assets = document.get("resources", {}).get("assets", [])
    selected_id = selection.get("source_asset_id") if isinstance(selection, Mapping) else None
    selected_sha = selection.get("source_sha256") if isinstance(selection, Mapping) else None
    if not isinstance(selection, Mapping):
        # A printed answer key is not an alternative detailed-solution source.
        # Keep explicit selections authoritative and leave multiple detail candidates unresolved.
        preferred_links = [
            link for link in detailed
            if link.get("status") in {"verified", "review"}
        ]
        if not preferred_links:
            preferred_links = [
                link for link in quick_answers
                if link.get("status") in {"verified", "review"}
            ]
        preferred = preferred_links[0] if len(preferred_links) == 1 else None
        selected_id = preferred.get("source_asset_id") if preferred else None
        selected_sha = next(
            (asset.get("sha256") for asset in assets
             if isinstance(asset, Mapping) and asset.get("asset_id") == selected_id), None
        )
    current_candidate_ids = {
        value for value in raw_candidates if isinstance(value, str)
    } if isinstance(raw_candidates, list) else None
    physical_links = [*detailed, *quick_answers]
    physical_link = next(
        (link for link in physical_links if link.get("source_asset_id") == selected_id), None
    )
    if physical_link is None:
        physical_link = next(
            (link for link in physical_links
             if current_candidate_ids is None or link.get("external_asset_id") in current_candidate_ids), None
        )
    physical_asset_id = physical_link.get("source_asset_id") if isinstance(physical_link, Mapping) else None
    selected_asset = next(
        (asset for asset in assets if isinstance(asset, Mapping) and asset.get("asset_id") == physical_asset_id),
        None,
    )
    source_asset_present = selected_asset is not None
    source_integrity = "ready" if source_asset_present or physical_link is None else "missing_file"
    if isinstance(selected_asset, Mapping) and isinstance(selected_asset.get("path"), str) and item_root is not None:
        root = item_root.resolve()
        source_path = (root / selected_asset["path"]).resolve()
        source_asset_present = source_path.is_relative_to(root) and source_path.is_file()
        source_integrity = "ready" if source_asset_present else "missing_file"
        if source_asset_present:
            actual_sha = file_sha256(source_path)
            expected_sha = (
                selected_sha if selected_asset.get("asset_id") == selected_id
                else selected_asset.get("sha256")
            )
            if isinstance(expected_sha, str) and actual_sha != expected_sha:
                source_integrity = "hash_mismatch"
    selected_link = next(
        (link for link in detailed if link.get("source_asset_id") == selected_id), None
    )
    selected_reasons = selected_link.get("review_reasons") or [] if isinstance(selected_link, Mapping) else []
    receipt_result = (
        current_receipt_result(dict(document), item_root)
        if isinstance(selection, Mapping) and item_root is not None else None
    )
    receipt_identity_mismatch = isinstance(receipt_result, Mapping) and any(
        receipt_result.get(field) == "mismatch"
        for field in ("same_question", "content_match", "number_consistency")
    )
    receipt_answer_conflict = (
        isinstance(receipt_result, Mapping)
        and receipt_result.get("same_question") == "match"
        and receipt_result.get("content_match") == "match"
        and receipt_result.get("number_consistency") != "mismatch"
        and receipt_result.get("answer_consistency") == "conflict"
    )
    if receipt_identity_mismatch:
        link_state = "mismatch"
    elif receipt_answer_conflict:
        link_state = "conflict"
    elif any("pair_mismatch" in str(reason) or "wrong_link" in str(reason) for reason in selected_reasons):
        link_state = "mismatch"
    elif any("conflict" in str(reason) for reason in selected_reasons):
        link_state = "conflict"
    elif isinstance(selected_link, Mapping) and selected_link.get("status") == "verified" and source_integrity == "ready":
        link_state = "verified"
    elif isinstance(selected_link, Mapping) and (selected_link.get("status") == "review" or selected_reasons):
        link_state = "review"
    elif detailed or (isinstance(raw_candidates, list) and raw_candidates):
        link_state = "unverified"
    else:
        link_state = "no_candidate"
    if any(link.get("source_asset_id") == selected_id and "pair_mismatch_rejected" in (link.get("review_reasons") or [])
           for link in detailed):
        selected_id = selected_sha = None
    attempts_for_source = provenance.get("transcriptions", []) if isinstance(provenance, Mapping) else []
    current_transcription = any(
        isinstance(attempt, Mapping)
        and attempt.get("section") == "explanation"
        and attempt.get("status") == "succeeded"
        and selected_id in (attempt.get("input_asset_ids") or [])
        and attempt.get("shared_source_sha256") == selected_sha
        and (not isinstance(selection, Mapping) or attempt.get("selection_revision") == selection.get("revision"))
        for attempt in attempts_for_source if isinstance(attempts_for_source, list)
    )
    source_present = bool(selected_id and selected_sha)
    current_attempts = [attempt for attempt in attempts_for_source
                        if isinstance(attempt, Mapping) and attempt.get("section") == "explanation"
                        and selected_id in (attempt.get("input_asset_ids") or [])
                        and (not isinstance(selection, Mapping) or attempt.get("selection_revision") == selection.get("revision"))]
    answer = content.get("answer") if isinstance(content, Mapping) else None
    answer_fields = {"choice": "choice_ids", "content": "blocks", "true_false": "items"}
    answer_values = (
        answer.get(answer_fields.get(str(answer.get("kind")), ""))
        if isinstance(answer, Mapping) else None
    )
    has_answer = isinstance(answer_values, list) and bool(answer_values)
    answer_only_transcribed = bool(
        has_answer and not explanation_blocks and current_transcription
        and source_present and source_integrity == "ready"
        and link_state not in {"mismatch", "conflict"}
    )
    transcribed = bool(
        (isinstance(explanation_blocks, list) and explanation_blocks and current_transcription)
        or answer_only_transcribed
    )
    transcription_state = ("complete" if transcribed else
                           "failed" if current_attempts and current_attempts[-1].get("status") == "failed" else
                           "pending" if source_present else "no_source")

    current_links = [link for link in detailed if not isinstance(selection, Mapping) or link.get("source_asset_id") == selected_id]
    verified = [link for link in current_links if link.get("status") == "verified"]
    review = [link for link in current_links if link.get("status") == "review"]
    selected = verified or review
    manually_confirmed = any(
        "manual_user_confirmation" in (link.get("evidence") or [])
        for link in detailed
    )
    status = "verified" if verified else "review" if review else "missing"
    confidence = max(
        (
            float(link.get("confidence") or 0)
            for link in selected
            if type(link.get("confidence")) in {int, float}
        ),
        default=0.0,
    )
    reasons = {
        str(reason)
        for link in selected
        for reason in (link.get("review_reasons") or [])
        if isinstance(reason, str) and reason
    }
    if selected_id is None and isinstance(selection, Mapping):
        reasons.update(str(reason) for link in detailed
                       if link.get("source_asset_id") == selection.get("source_asset_id")
                       for reason in (link.get("review_reasons") or []) if isinstance(reason, str))
    if not selected and isinstance(raw_link, Mapping):
        reasons.update(
            str(reason)
            for reason in (raw_link.get("review_reasons") or [])
            if isinstance(reason, str) and reason
        )
    candidate_count = max(
        len(detailed), len(raw_candidates) if isinstance(raw_candidates, list) else 0
    )
    review_candidate_count = max(
        sum(link.get("status") == "review" for link in [*detailed, *quick_answers]),
        (
            len(raw_candidates)
            if isinstance(raw_candidates, list) and not verified
            else 0
        ),
    )
    reconciliation = (
        raw_link.get("best_effort_reconciliation")
        if isinstance(raw_link, Mapping)
        else None
    )
    first_blocker = raw_link.get("first_blocker") if isinstance(raw_link, Mapping) else None
    if not isinstance(first_blocker, str) and isinstance(reconciliation, Mapping):
        first_blocker = reconciliation.get("first_blocker")
    return {
        "status": status,
        "confidence": max(0.0, min(1.0, confidence)),
        "candidate_count": candidate_count,
        "detailed_solution_count": len(detailed),
        "quick_answer_count": len(quick_answers),
        "detailed_solution_present": bool(detailed),
        "quick_answer_only": bool(quick_answers) and not detailed,
        "review_candidate_count": review_candidate_count,
        "review_candidate_present": review_candidate_count > 0,
        "detailed_solution_missing": not detailed,
        "first_blocker": (
            first_blocker
            if isinstance(first_blocker, str) and first_blocker not in {"", "none"}
            else None
        ),
        "review_reason_count": len(reasons),
        "requires_review": status != "verified" and bool(reasons or candidate_count),
        "attempt_count": len(attempts) if isinstance(attempts, list) else 0,
        "best_effort_exhausted": bool(
            isinstance(raw_link, Mapping) and raw_link.get("best_effort_exhausted") is True
        ),
        "manually_confirmed": manually_confirmed,
        "transcribed": transcribed,
        "answer_only_transcribed": answer_only_transcribed,
        "source_present": source_present,
        "source_asset_present": source_asset_present,
        "source_integrity": source_integrity,
        "link_state": link_state,
        "transcription_state": transcription_state,
        "previous_transcription_present": bool(explanation_blocks or has_answer) and not current_transcription,
        "source_asset_id": selected_id,
        "source_sha256": selected_sha,
        "selection_revision": selection.get("revision") if isinstance(selection, Mapping) else None,
    }


def _approve_blocks(value: Any) -> None:
    if isinstance(value, dict):
        if "review_status" in value:
            value["review_status"] = "approved"
        for child in value.values():
            _approve_blocks(child)
    elif isinstance(value, list):
        for child in value:
            _approve_blocks(child)


def _asset_map(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    resources = document.get("resources")
    assets = resources.get("assets") if isinstance(resources, Mapping) else None
    result: dict[str, Mapping[str, Any]] = {}
    if not isinstance(assets, list):
        raise ModelError("document.resources.assets must be an array")
    for asset in assets:
        if not isinstance(asset, Mapping) or not isinstance(asset.get("asset_id"), str):
            raise ModelError("every asset must have an asset_id")
        asset_id = asset["asset_id"]
        if asset_id in result:
            raise ModelError(f"duplicate asset_id: {asset_id}")
        result[asset_id] = asset
    return result


def _figure_blocks(document: Mapping[str, Any]):
    content = document.get("content")
    if not isinstance(content, Mapping):
        return

    def walk(value: Any):
        if isinstance(value, Mapping):
            if value.get("type") == "figure" and isinstance(value.get("block_id"), str):
                yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    for content_section in ("question", "answer", "explanation"):
        section = content.get(content_section)
        if section is None:
            continue
        asset_section = "question" if content_section == "answer" else content_section
        for block in walk(section):
            yield asset_section, block


def _asset_descends_from(
    assets: Mapping[str, Mapping[str, Any]], asset_id: str, original_id: str
) -> bool:
    current: Any = asset_id
    seen: set[str] = set()
    while isinstance(current, str) and current not in seen:
        if current == original_id:
            return True
        seen.add(current)
        asset = assets.get(current)
        current = asset.get("derived_from_asset_id") if asset else None
    return False


def _question_figure_entries(
    document: Mapping[str, Any],
) -> list[tuple[Mapping[str, Any], str]]:
    content = document.get("content")
    question = content.get("question") if isinstance(content, Mapping) else None
    blocks = question.get("blocks") if isinstance(question, Mapping) else None
    if not isinstance(blocks, list):
        return []

    result: list[tuple[Mapping[str, Any], str]] = []

    def placement(ancestors: tuple[str, ...]) -> str:
        if "choices" in ancestors:
            return "choice"
        if "bogi" in ancestors:
            return "bogi"
        if "box" in ancestors:
            return "shared_box"
        if "table_cell" in ancestors:
            return "table_cell"
        return "body"

    def walk(values: Any, ancestors: tuple[str, ...] = ()) -> None:
        if not isinstance(values, list):
            return
        for block in values:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "figure":
                result.append((block, placement(ancestors)))
                continue
            next_ancestors = ancestors + ((str(block_type),) if block_type else ())
            if block_type == "box":
                walk(block.get("blocks"), next_ancestors)
            if block_type in {"bogi", "choices"}:
                items = block.get("items")
                for item in items if isinstance(items, list) else []:
                    if isinstance(item, Mapping):
                        walk(item.get("blocks"), next_ancestors)
            if block_type == "table":
                rows = block.get("rows")
                for row in rows if isinstance(rows, list) else []:
                    cells = row.get("cells") if isinstance(row, Mapping) else None
                    for cell in cells if isinstance(cells, list) else []:
                        if isinstance(cell, Mapping):
                            walk(cell.get("blocks"), next_ancestors + ("table_cell",))

    walk(blocks)
    return result


def _safe_asset_index(
    document: Mapping[str, Any],
) -> tuple[dict[str, Mapping[str, Any]], bool]:
    resources = document.get("resources")
    values = resources.get("assets") if isinstance(resources, Mapping) else None
    if not isinstance(values, list):
        return {}, True
    result: dict[str, Mapping[str, Any]] = {}
    malformed = False
    for asset in values:
        asset_id = asset.get("asset_id") if isinstance(asset, Mapping) else None
        if not isinstance(asset_id, str) or not asset_id or asset_id in result:
            malformed = True
            continue
        result[asset_id] = asset
    return result, malformed


def _safe_asset_descends_from(
    assets: Mapping[str, Mapping[str, Any]], asset_id: Any, original_id: Any
) -> bool:
    if not isinstance(asset_id, str) or not isinstance(original_id, str):
        return False
    current = asset_id
    seen: set[str] = set()
    while current not in seen:
        if current == original_id:
            return True
        seen.add(current)
        asset = assets.get(current)
        parent = asset.get("derived_from_asset_id") if isinstance(asset, Mapping) else None
        if not isinstance(parent, str):
            return False
        current = parent
    return False


def _safe_figure_asset_descends_from(
    assets: Mapping[str, Mapping[str, Any]], asset_id: Any, original_id: Any
) -> bool:
    if not isinstance(asset_id, str) or not isinstance(original_id, str):
        return False
    original = assets.get(original_id)
    if (
        not isinstance(original, Mapping)
        or original.get("role") not in {"figure_original", "table_snapshot"}
        or original.get("section") not in {"question", "shared"}
    ):
        return False
    allowed_sections = {"question", "shared"}
    current = asset_id
    seen: set[str] = set()
    chain: list[str] = []
    while current not in seen:
        if current == original_id:
            chain.append(original_id)
            break
        seen.add(current)
        asset = assets.get(current)
        if (
            not isinstance(asset, Mapping)
            or asset.get("role") != "figure_regenerated"
            or asset.get("section") not in allowed_sections
            or asset.get("state") != "ready"
        ):
            return False
        parent = asset.get("derived_from_asset_id")
        if not isinstance(parent, str):
            return False
        chain.append(current)
        current = parent
    else:
        return False
    for index, candidate_id in enumerate(chain[:-1]):
        candidate = assets.get(candidate_id)
        generation = (
            candidate.get("generation") if isinstance(candidate, Mapping) else None
        )
        fallback_id = (
            generation.get("fallback_asset_id")
            if isinstance(generation, Mapping)
            else None
        )
        if fallback_id is not None and fallback_id not in chain[index + 1 :]:
            return False
    return True


def _safe_fallback_is_ancestor(
    assets: Mapping[str, Mapping[str, Any]],
    asset_id: Any,
    fallback_id: Any,
    original_id: Any,
) -> bool:
    if (
        not isinstance(asset_id, str)
        or not isinstance(fallback_id, str)
        or fallback_id == asset_id
        or not _safe_figure_asset_descends_from(assets, asset_id, original_id)
        or not _safe_figure_asset_descends_from(assets, fallback_id, original_id)
    ):
        return False
    current = asset_id
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        asset = assets.get(current)
        parent = asset.get("derived_from_asset_id") if isinstance(asset, Mapping) else None
        if not isinstance(parent, str):
            return False
        if parent == fallback_id:
            return _safe_asset_descends_from(assets, fallback_id, original_id)
        current = parent
    return False


def _region_key(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    source_asset_id = value.get("source_asset_id")
    bbox = value.get("bbox")
    if not isinstance(source_asset_id, str) or not isinstance(bbox, Mapping):
        return None
    unit = bbox.get("unit")
    origin = bbox.get("origin")
    if unit not in {"ratio", "pixel"} or origin != "top-left":
        return None
    try:
        coordinates = tuple(float(bbox[key]) for key in ("left", "top", "right", "bottom"))
    except (KeyError, TypeError, ValueError):
        return None
    if coordinates[0] >= coordinates[2] or coordinates[1] >= coordinates[3]:
        return None
    return json.dumps(
        [source_asset_id, unit, origin, *coordinates],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _question_table_regions(document: Mapping[str, Any]) -> set[str]:
    content = document.get("content")
    question = content.get("question") if isinstance(content, Mapping) else None
    blocks = question.get("blocks") if isinstance(question, Mapping) else None
    result: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            if value.get("type") == "table":
                key = _region_key(value.get("source_region"))
                if key is not None:
                    result.add(key)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(blocks)
    return result


def _paragraph_text(document: Mapping[str, Any]) -> str:
    content = document.get("content")
    question = content.get("question") if isinstance(content, Mapping) else None
    values: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            if value.get("type") == "paragraph":
                for inline in value.get("inlines", []):
                    if isinstance(inline, Mapping) and isinstance(inline.get("text"), str):
                        values.append(inline["text"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(question)
    return " ".join(values)


def _legacy_figure_metadata(
    document: Mapping[str, Any],
    block: Mapping[str, Any],
    placement_context: str,
    original: Mapping[str, Any] | None,
    table_regions: set[str],
    paragraph_text: str,
) -> tuple[str, str]:
    region = _region_key(block.get("source_region"))
    if (
        block.get("role") == "table_image"
        or isinstance(original, Mapping)
        and original.get("role") == "table_snapshot"
        or region is not None
        and region in table_regions
    ):
        return "table_snapshot", "original_only"
    alt_text = str(block.get("alt_text") or "").strip()
    speakers = set(re.findall(r"학생\s*([A-C])", alt_text, flags=re.IGNORECASE))
    student_triad = bool(
        re.search(
            r"학생\s*\(?\s*A\s*[,·/]\s*B\s*[,·/]\s*C\s*\)?",
            alt_text,
            flags=re.IGNORECASE,
        )
    )
    dialogue_layout = bool(
        re.search(
            r"(?:말풍선|대화창|채팅창|온라인\s*수업|대화\s*(?:내용|장면|모습)|"
            r"대화(?:하|를|가|에))",
            alt_text,
        )
    )
    complete_sentences = len(
        re.findall(r"(?:이다|한다|있다|없다|된다|필요하다|해당한다)[.!?]", alt_text)
    )
    dialogue = bool(
        dialogue_layout
        and (
            len(speakers) >= 2
            or student_triad
            or "말풍선" in alt_text
            or "대화창" in alt_text
        )
        or len(speakers) >= 2 and complete_sentences >= 2
    )
    normalized_alt = re.sub(r"\s+", "", alt_text)
    normalized_paragraphs = re.sub(r"\s+", "", paragraph_text)
    text_preserved = bool(
        len(normalized_alt) >= 20 and normalized_alt in normalized_paragraphs
    )
    if dialogue and not text_preserved:
        return "mixed_text_figure", "original_only"
    review = original.get("review") if isinstance(original, Mapping) else None
    if (
        original is None
        or original.get("role") not in {"figure_original", "table_snapshot"}
        or original.get("section") not in {"question", "shared"}
        or original.get("state") != "ready"
        or region is None
        or not isinstance(review, Mapping)
        or review.get("boundary_uncertain") is not False
    ):
        return "unknown", "original_only"
    return "pure_figure", "ai_allowed"


def _figure_target_details(
    document: Mapping[str, Any],
    block: Mapping[str, Any],
    placement_context: str,
    assets: Mapping[str, Mapping[str, Any]],
    *,
    assets_malformed: bool,
    table_regions: set[str],
    paragraph_text: str,
) -> dict[str, Any]:
    original_id = block.get("original_asset_id")
    selected_id = block.get("asset_id")
    original = assets.get(original_id) if isinstance(original_id, str) else None
    stored_visual_kind = block.get("visual_kind")
    stored_policy = block.get("regeneration_policy")
    legacy_visual_kind, legacy_policy = _legacy_figure_metadata(
        document,
        block,
        placement_context,
        original,
        table_regions,
        paragraph_text,
    )
    if legacy_visual_kind in {"mixed_text_figure", "table_snapshot"}:
        visual_kind, regeneration_policy = legacy_visual_kind, legacy_policy
    elif stored_visual_kind in FIGURE_VISUAL_KINDS and stored_policy in {
        "ai_allowed",
        "original_only",
    }:
        visual_kind, regeneration_policy = stored_visual_kind, stored_policy
    else:
        visual_kind, regeneration_policy = legacy_visual_kind, legacy_policy
    stored_placement = block.get("placement_context")
    placement_malformed = bool(
        stored_placement is not None and stored_placement != placement_context
    )

    candidates: list[Mapping[str, Any]] = []
    invalid_lineage = assets_malformed or placement_malformed
    nonready_candidate_pending = False
    for asset in assets.values():
        if asset.get("role") != "figure_regenerated":
            continue
        generation = asset.get("generation")
        fallback_id = (
            generation.get("fallback_asset_id")
            if isinstance(generation, Mapping)
            else None
        )
        if asset.get("state") == "failed":
            continue
        if asset.get("state") != "ready":
            nonready_candidate_pending = True
            continue
        if not _safe_figure_asset_descends_from(
            assets, asset.get("asset_id"), original_id
        ):
            if (
                asset.get("derived_from_asset_id") == original_id
                or fallback_id == original_id
            ):
                invalid_lineage = True
            continue
        if fallback_id is not None and not _safe_fallback_is_ancestor(
            assets, asset.get("asset_id"), fallback_id, original_id
        ):
            invalid_lineage = True
            continue
        if asset.get("section") not in {"question", "shared"}:
            invalid_lineage = True
            continue
        candidates.append(asset)

    selected = assets.get(selected_id) if isinstance(selected_id, str) else None
    selected_regenerated = bool(
        isinstance(selected, Mapping)
        and selected.get("role") == "figure_regenerated"
        and selected in candidates
    )
    block_region = block.get("source_region")
    original_review = original.get("review") if isinstance(original, Mapping) else None
    original_valid = bool(
        isinstance(original, Mapping)
        and original.get("role") in {"figure_original", "table_snapshot"}
        and original.get("section") in {"question", "shared"}
        and original.get("state") == "ready"
        and isinstance(original_review, Mapping)
        and original_review.get("boundary_uncertain") is False
        and _region_key(block_region) is not None
        and _region_key(original.get("source_region")) == _region_key(block_region)
        and isinstance(block_region, Mapping)
        and original.get("derived_from_asset_id") == block_region.get("source_asset_id")
    )
    selected_valid = bool(
        isinstance(selected, Mapping)
        and (
            selected_id == original_id
            and original_valid
            or selected_regenerated
        )
    )
    pending_candidate = any(
        not isinstance(asset.get("review"), Mapping)
        or asset["review"].get("status") != "approved"
        or asset["review"].get("boundary_uncertain") is not False
        for asset in candidates
    )
    blocked_mixed = visual_kind in {"mixed_text_figure", "table_snapshot"}
    needs_review = bool(
        invalid_lineage
        or not original_valid
        or not selected_valid
        or pending_candidate
        or nonready_candidate_pending
        or blocked_mixed
        or visual_kind == "unknown"
    )
    eligible = bool(
        original_valid
        and selected_valid
        and not invalid_lineage
        and visual_kind == "pure_figure"
        and regeneration_policy == "ai_allowed"
    )
    reason = (
        "eligible"
        if eligible
        else visual_kind
        if visual_kind in {"mixed_text_figure", "table_snapshot", "unknown"}
        else "original_only"
        if regeneration_policy == "original_only"
        else "needs_review"
    )
    return {
        "eligible": eligible,
        "reason": reason,
        "visual_kind": visual_kind,
        "placement_context": placement_context,
        "regeneration_policy": regeneration_policy,
        "regenerated": bool(candidates),
        "selected_regenerated": selected_regenerated,
        "review_pending": needs_review,
        "blocked_mixed": blocked_mixed,
    }


def inspect_figure_regeneration_target(
    package: Mapping[str, Any], block_id: str
) -> dict[str, Any]:
    """Inspect one problem figure without raising on malformed legacy lineage."""

    entries = [
        (block, placement)
        for block, placement in _question_figure_entries(package)
        if block.get("block_id") == block_id
    ]
    if len(entries) != 1:
        return {
            "eligible": False,
            "reason": "not_found" if not entries else "needs_review",
            "visual_kind": "unknown",
            "placement_context": "body",
            "regenerated": False,
            "selected_regenerated": False,
            "review_pending": True,
        }
    assets, malformed = _safe_asset_index(package)
    block, placement = entries[0]
    details = _figure_target_details(
        package,
        block,
        placement,
        assets,
        assets_malformed=malformed,
        table_regions=_question_table_regions(package),
        paragraph_text=_paragraph_text(package),
    )
    return {
        key: details[key]
        for key in (
            "eligible",
            "reason",
            "visual_kind",
            "placement_context",
            "regenerated",
            "selected_regenerated",
            "review_pending",
        )
    }


def _question_regenerated_selection_policy(
    document: Mapping[str, Any],
    block: Mapping[str, Any],
    assets: Mapping[str, Mapping[str, Any]],
    target: Mapping[str, Any],
) -> tuple[bool, str]:
    block_id = block.get("block_id")
    entries = [
        (candidate, placement)
        for candidate, placement in _question_figure_entries(document)
        if candidate.get("block_id") == block_id
    ]
    if not isinstance(block_id, str) or not entries:
        return True, "not_question_figure"
    if len(entries) != 1:
        return False, "needs_review"
    stored_placement = block.get("placement_context")
    if stored_placement is not None and stored_placement != entries[0][1]:
        return False, "needs_review"
    inspection = inspect_figure_regeneration_target(document, block_id)
    if inspection.get("visual_kind") in {"mixed_text_figure", "table_snapshot"}:
        return False, str(inspection.get("reason") or "unknown")
    if block.get("visual_kind") == "unknown":
        return False, "unknown"
    if block.get("regeneration_policy") == "original_only":
        return False, "original_only"
    original_id = block.get("original_asset_id")
    original = assets.get(original_id) if isinstance(original_id, str) else None
    block_region = block.get("source_region")
    if not isinstance(original, Mapping):
        return False, "needs_review"
    if isinstance(block_region, Mapping) and (
        _region_key(original.get("source_region")) != _region_key(block_region)
        or original.get("derived_from_asset_id") != block_region.get("source_asset_id")
    ):
        return False, "needs_review"
    target_id = target.get("asset_id")
    if not _safe_figure_asset_descends_from(assets, target_id, original_id):
        return False, "needs_review"
    generation = target.get("generation")
    fallback_id = (
        generation.get("fallback_asset_id")
        if isinstance(generation, Mapping)
        else None
    )
    if fallback_id is not None and not _safe_fallback_is_ancestor(
        assets, target_id, fallback_id, original_id
    ):
        return False, "needs_review"
    return True, "eligible"


def figure_generation_summary(package: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize only recursively nested problem figures and their safe lineage."""

    entries = _question_figure_entries(package)
    assets, malformed = _safe_asset_index(package)
    table_regions = _question_table_regions(package)
    paragraph_text = _paragraph_text(package)
    details = [
        _figure_target_details(
            package,
            block,
            placement,
            assets,
            assets_malformed=malformed,
            table_regions=table_regions,
            paragraph_text=paragraph_text,
        )
        for block, placement in entries
    ]
    result = {
        "figure_count": len(details),
        "regenerated_count": sum(value["regenerated"] for value in details),
        "missing_regeneration_count": sum(not value["regenerated"] for value in details),
        "selected_regenerated_count": sum(
            value["selected_regenerated"] for value in details
        ),
        "review_pending_count": sum(value["review_pending"] for value in details),
        "blocked_mixed_count": sum(value["blocked_mixed"] for value in details),
    }
    result.update(
        has_figures=result["figure_count"] > 0,
        has_regenerated=result["regenerated_count"] > 0,
        has_missing_regeneration=result["missing_regeneration_count"] > 0,
        has_selected_regenerated=result["selected_regenerated_count"] > 0,
        needs_review=result["review_pending_count"] > 0,
        has_blocked_mixed=result["blocked_mixed_count"] > 0,
    )
    return result


def _apply_manual_confirmation(document: dict[str, Any]) -> None:
    review = document.get("review")
    if isinstance(review, dict) and isinstance(review.get("issues"), list):
        review["issues"] = [
            issue for issue in review["issues"] if not is_advisory_review_issue(issue)
        ]

    assets = _asset_map(document)
    for _, block in _figure_blocks(document):
        selected = assets.get(block.get("asset_id"))
        selected_review = selected.get("review") if isinstance(selected, Mapping) else None
        if (
            isinstance(selected, dict)
            and selected.get("role") in FIGURE_ASSET_ROLES
            and selected.get("state") == "ready"
            and isinstance(selected_review, dict)
            and selected_review.get("status") == "unreviewed"
            and selected_review.get("boundary_uncertain") is False
        ):
            selected_review["status"] = "approved"

    provenance = document.get("provenance")
    links = provenance.get("solution_links") if isinstance(provenance, Mapping) else None
    for link in links if isinstance(links, list) else []:
        if (
            isinstance(link, dict)
            and link.get("kind") in {"detailed_solution", "mixed"}
            and link.get("status") == "review"
        ):
            link["status"] = "verified"
            evidence = link.get("evidence") if isinstance(link.get("evidence"), list) else []
            link["evidence"] = list(dict.fromkeys([*evidence, "manual_user_confirmation"]))

def review_figure_asset_cas(
    path: Path | str,
    expected_revision: int,
    asset_id: str,
    status: str,
    boundary_uncertain: bool,
    notes: list[str],
) -> dict[str, Any]:
    """Review a local figure asset without changing its file or lineage."""

    path = Path(path)
    validate_id(asset_id, field="asset_id")
    if status not in {"approved", "rejected", "unreviewed"}:
        raise ModelError("figure asset review status is invalid")
    if not isinstance(boundary_uncertain, bool):
        raise ModelError("boundary_uncertain must be a boolean")
    if not isinstance(notes, list) or any(
        not isinstance(note, str) or not note.strip() for note in notes
    ):
        raise ModelError("notes must be an array of non-empty strings")

    current = load_json_object(path)
    current_revision = _workflow_revision(current, path=path)
    if current_revision != expected_revision:
        raise RevisionConflict(expected_revision, current_revision)
    asset = _asset_map(current).get(asset_id)
    if asset is None:
        raise ModelNotFound(f"figure asset not found: {asset_id}")
    if asset.get("role") not in FIGURE_REVIEWABLE_ASSET_ROLES:
        raise ModelError(
            "only figure_original, figure_regenerated, and table_snapshot assets can be reviewed"
        )

    candidate = copy.deepcopy(current)
    candidate_asset = _asset_map(candidate)[asset_id]
    review = candidate_asset.get("review")
    if not isinstance(review, dict):
        raise ModelError("figure asset review must be an object")
    review.update(
        status=status,
        boundary_uncertain=boundary_uncertain,
        notes=[note.strip() for note in notes],
    )
    return save_json_cas(path, candidate, expected_revision)


def select_figure_asset_cas(
    path: Path | str,
    expected_revision: int,
    block_id: str,
    asset_id: str,
) -> dict[str, Any]:
    """Select an approved ready variant belonging to a figure block's lineage."""

    path = Path(path)
    validate_id(block_id, field="block_id")
    validate_id(asset_id, field="asset_id")
    current = load_json_object(path)
    current_revision = _workflow_revision(current, path=path)
    if current_revision != expected_revision:
        raise RevisionConflict(expected_revision, current_revision)

    matches = [entry for entry in _figure_blocks(current) if entry[1]["block_id"] == block_id]
    if not matches:
        raise ModelNotFound(f"figure block not found: {block_id}")
    if len(matches) != 1:
        raise ModelError(f"duplicate figure block_id: {block_id}")
    section, block = matches[0]
    assets = _asset_map(current)
    target = assets.get(asset_id)
    if target is None:
        raise ModelNotFound(f"figure asset not found: {asset_id}")
    original_id = block.get("original_asset_id")
    original = assets.get(original_id) if isinstance(original_id, str) else None
    if original is None or original.get("role") != "figure_original":
        raise ModelError("figure block original_asset_id must identify a figure_original asset")
    if original.get("section") not in {section, "shared"}:
        raise ModelError("figure original asset must belong to the same section as the block")
    if target.get("role") not in FIGURE_ASSET_ROLES:
        raise ModelError("selected asset must be a figure_original or figure_regenerated asset")
    if target.get("role") == "figure_regenerated":
        allowed, reason = _question_regenerated_selection_policy(
            current, block, assets, target
        )
        if not allowed:
            raise ModelError(
                "figure regeneration target is not eligible: "
                + reason
            )
    if target.get("section") not in {section, "shared"}:
        raise ModelError("selected figure asset must belong to the same section as the block")
    if target.get("state") != "ready":
        raise ModelError("selected figure asset must be ready")
    target_review = target.get("review")
    if not isinstance(target_review, Mapping) or target_review.get("status") != "approved":
        raise ModelError("selected figure asset must be approved")
    if target_review.get("boundary_uncertain") is True:
        raise ModelError("selected figure asset boundary must be confirmed")

    if not _asset_descends_from(assets, asset_id, original_id):
        raise ModelError("selected figure asset does not descend from original_asset_id")

    candidate = copy.deepcopy(current)
    candidate_matches = [
        entry for entry in _figure_blocks(candidate) if entry[1]["block_id"] == block_id
    ]
    candidate_matches[0][1]["asset_id"] = asset_id
    return save_json_cas(path, candidate, expected_revision)


def approve_question_cas(
    path: Path | str,
    expected_revision: int,
    reviewer: str,
    *,
    validate_candidate: Callable[[Mapping[str, Any]], Iterable[str]] | None = None,
    manual_confirmation: bool = False,
) -> dict[str, Any]:
    path = Path(path)
    current = load_json_object(path)
    if _workflow_revision(current, path=path) != expected_revision:
        raise RevisionConflict(expected_revision, _workflow_revision(current, path=path))
    current_review = current.get("review")
    if (
        current.get("workflow", {}).get("status") == "content_ready"
        and isinstance(current_review, Mapping)
        and current_review.get("approved_revision") == expected_revision
        and current_review.get("approved_sha256") == approval_digest(current)
    ):
        return copy.deepcopy(current)
    candidate = copy.deepcopy(current)
    if manual_confirmation:
        _apply_manual_confirmation(candidate)
    content = candidate.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("question"), dict):
        raise ModelError("cannot approve before question transcription exists")
    question_blocks = content["question"].get("blocks")
    if not isinstance(question_blocks, list) or not question_blocks:
        raise ModelError("cannot approve an empty question transcription")
    resources = candidate.get("resources")
    assets = resources.get("assets") if isinstance(resources, Mapping) else []
    explanation_source_exists = any(
        isinstance(asset, Mapping)
        and asset.get("role") == "source_crop"
        and asset.get("section") in {"answer", "explanation"}
        for asset in assets
    )
    explanation = content.get("explanation")
    if explanation_source_exists:
        explanation_blocks = (
            explanation.get("blocks") if isinstance(explanation, Mapping) else None
        )
        if not isinstance(explanation_blocks, list) or not explanation_blocks:
            raise ModelError(
                "cannot approve before the available explanation source is transcribed"
            )
    assets_by_id = _asset_map(candidate)
    for section, block in _figure_blocks(candidate):
        original_id = block.get("original_asset_id")
        selected_id = block.get("asset_id")
        original = assets_by_id.get(original_id) if isinstance(original_id, str) else None
        selected = assets_by_id.get(selected_id) if isinstance(selected_id, str) else None
        if original is None or original.get("role") not in {"figure_original", "table_snapshot"}:
            raise ModelError("figure block original_asset_id must identify a figure_original or table_snapshot asset")
        if original.get("section") not in {section, "shared"} or original.get("state") != "ready":
            raise ModelError(f"cannot approve invalid figure original asset {original_id}")
        if original.get("role") == "table_snapshot":
            if selected_id != original_id or selected is None or selected.get("role") != "table_snapshot":
                raise ModelError("table_snapshot figure must keep its original asset selected")
            selected_review = selected.get("review")
            if isinstance(selected_review, Mapping) and selected_review.get("boundary_uncertain") is True:
                raise ModelError(f"cannot approve boundary-uncertain asset {selected_id}")
            if isinstance(selected_review, Mapping) and selected_review.get("status") == "rejected":
                raise ModelError(f"cannot approve rejected figure asset {selected_id}")
            if isinstance(selected_review, dict) and selected_review.get("status") == "unreviewed":
                selected_review["status"] = "approved"
            continue
        if selected is None or selected.get("role") not in FIGURE_ASSET_ROLES:
            raise ModelError("figure block asset_id must identify a figure asset")
        if selected.get("role") == "figure_regenerated":
            allowed, reason = _question_regenerated_selection_policy(
                candidate, block, assets_by_id, selected
            )
            if not allowed:
                raise ModelError(
                    "cannot approve ineligible regenerated figure: "
                    + reason
                )
        if selected.get("section") not in {section, "shared"} or selected.get("state") != "ready":
            raise ModelError(f"cannot approve non-ready figure asset {selected_id}")
        if not _asset_descends_from(assets_by_id, str(selected_id), str(original_id)):
            raise ModelError("selected figure asset does not descend from original_asset_id")
        selected_review = selected.get("review")
        if not isinstance(selected_review, Mapping) or selected_review.get("status") != "approved":
            raise ModelError(f"cannot approve unreviewed figure asset {selected_id}")
        if selected_review.get("boundary_uncertain") is True:
            raise ModelError(f"cannot approve boundary-uncertain asset {selected_id}")
    _approve_blocks(content)
    review = candidate.get("review")
    if not isinstance(review, dict):
        raise ModelError("document.review must be an object")
    coverage = review.get("coverage")
    if isinstance(coverage, Mapping):
        required_sections = ["question"]
        if explanation_source_exists:
            required_sections.append("explanation")
        incomplete = [
            section for section in required_sections if coverage.get(section) == "incomplete"
        ]
        if incomplete:
            raise ModelError(
                "cannot approve incomplete OCR coverage: " + ", ".join(incomplete)
            )
    issues = review.get("issues")
    if isinstance(issues, list) and issues:
        raise ModelError("cannot approve while review issues remain")
    review["question"] = "approved"
    review["answer"] = "approved" if content.get("answer") is not None else "not_applicable"
    review["explanation"] = (
        "approved" if content.get("explanation") is not None else "not_applicable"
    )
    review["assets"] = "approved"
    review["coverage"] = {
        "question": "complete",
        "explanation": (
            "complete" if content.get("explanation") is not None else "not_applicable"
        ),
    }
    for asset in assets:
        if isinstance(asset, dict) and isinstance(asset.get("review"), dict):
            if asset.get("role") in FIGURE_REVIEWABLE_ASSET_ROLES:
                # Figure review is always explicit; selected assets were checked
                # above and original/unselected audit variants stay untouched.
                continue
            if asset["review"].get("boundary_uncertain"):
                raise ModelError(f"cannot approve boundary-uncertain asset {asset.get('asset_id')}")
            asset["review"]["status"] = "approved"
    workflow = candidate["workflow"]
    workflow["status"] = "content_ready"
    review["approved_revision"] = expected_revision + 1
    review["reviewer"] = reviewer.strip() or "local-user"
    review["reviewed_at"] = utc_now()
    review["approved_sha256"] = approval_digest(candidate)
    if validate_candidate is not None:
        validation_candidate = copy.deepcopy(candidate)
        validation_candidate["workflow"]["revision"] = expected_revision + 1
        errors = list(validate_candidate(validation_candidate))
        if errors:
            raise ModelError("cannot approve invalid content_ready package: " + str(errors[0]))
    return save_json_cas(
        path,
        candidate,
        expected_revision,
        preserve_approval=True,
    )


def source_only_workflow(timestamp: str | None = None) -> dict[str, Any]:
    timestamp = timestamp or utc_now()
    return {
        "status": "source_only",
        "revision": 1,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def make_source_only_project(
    *,
    project_id: str,
    item_ids: Iterable[str],
    groups: Iterable[Mapping[str, Any]],
    source_import: Mapping[str, Any],
    timestamp: str | None = None,
) -> dict[str, Any]:
    timestamp = timestamp or utc_now()
    validated_items = [validate_id(item_id, field="item_id") for item_id in item_ids]
    if not validated_items:
        raise ModelError("a project must contain at least one item")
    return {
        "$schema": "../../schemas/project.schema.json",
        "schema_version": SCHEMA_VERSION,
        "project_id": validate_id(project_id, field="project_id"),
        "language": "ko-KR",
        "workflow": source_only_workflow(timestamp),
        "source_import": copy.deepcopy(dict(source_import)),
        "metadata": {
            "year": None,
            "semester": None,
            "exam_type": None,
            "school": None,
            "school_level": None,
            "grade": None,
            "subject": None,
            "filename_base": None,
            "range": None,
            "publisher": None,
            "book_title": None,
            "book_volume": None,
            "book_type": None,
        },
        "item_order": validated_items,
        "groups": [copy.deepcopy(dict(group)) for group in groups],
        "rendering": {
            "question_design_profile_ref": None,
            "explanation_design_profile_ref": None,
        },
        "export": {
            "renderer": "native_ordered",
            "output_mode": "separate_hwpx",
            "shared_item_policy": "explicit_item_order",
            "include_answers": True,
            "include_explanations": True,
            "engine_id": None,
            "engine_fingerprint_sha256": None,
        },
    }


def make_source_only_question(
    *,
    item_id: str,
    identity: Mapping[str, Any],
    provenance: Mapping[str, Any],
    assets: Iterable[Mapping[str, Any]],
    issues: Iterable[str] = (),
    has_answer_source: bool = False,
    has_explanation_source: bool = False,
    timestamp: str | None = None,
) -> dict[str, Any]:
    timestamp = timestamp or utc_now()
    asset_list = [copy.deepcopy(dict(asset)) for asset in assets]
    unique_issues = list(dict.fromkeys(issue for issue in issues if issue))
    return {
        "$schema": "../../../../schemas/question-package.schema.json",
        "schema_version": SCHEMA_VERSION,
        "item_id": validate_id(item_id, field="item_id"),
        "language": "ko-KR",
        "workflow": source_only_workflow(timestamp),
        "identity": copy.deepcopy(dict(identity)),
        "provenance": copy.deepcopy(dict(provenance)),
        "content": {"question": None, "answer": None, "explanation": None},
        "resources": {"assets": asset_list},
        "review": {
            "question": "unreviewed",
            "answer": "unreviewed" if has_answer_source else "not_applicable",
            "explanation": (
                "unreviewed" if has_explanation_source else "not_applicable"
            ),
            "assets": "unreviewed" if asset_list else "not_applicable",
            "coverage": {
                "question": "unreviewed",
                "explanation": (
                    "unreviewed" if has_explanation_source else "not_applicable"
                ),
            },
            "approved_revision": None,
            "approved_sha256": None,
            "reviewer": None,
            "reviewed_at": None,
            "issues": unique_issues,
        },
    }
