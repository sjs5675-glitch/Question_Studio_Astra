from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from crop.question_crop import (
    PROMPT_PATH,
    SCHEMA_PATH,
    build_prompt,
    load_detection,
    run_codex_detection,
    validate_detection,
)

from .exam_cleaning import load_cleaned_page
from .exam_region import _LOCK as _DRAFT_LOCK, ExamDraftConflict, ExamDraftError, load_exam_draft, stable_exam_region_id
from .model import sha256_file


class ExamDetectionError(ExamDraftError):
    pass


class ExamDetectionConflict(ExamDraftConflict):
    pass


@dataclass(frozen=True, slots=True)
class ProposalPage:
    page_number: int
    status: str
    revision: int
    selection_revision: int
    cleaned_revision: int
    cleaned_sha256: str
    source_mode: str
    suggestions: tuple[dict[str, Any], ...]
    error_code: str | None


DetectionRunner = Callable[[dict[str, Any]], dict[str, Any]]


def _input_page(workspace_root: Path | str, draft_id: str, page_number: int) -> tuple[Path, str, int, str]:
    from .exam_selection import no_clean_source
    original = no_clean_source(workspace_root, draft_id, page_number)
    if original is not None:
        return original[0], original[1], original[2], "original_unprocessed"
    cleaned = load_cleaned_page(workspace_root, draft_id, page_number)
    if cleaned.status != "ready" or cleaned.cleaned_path is None or cleaned.cleaned_sha256 is None:
        raise ExamDetectionError("page has no ready cleaned image or explicit original bypass")
    return cleaned.cleaned_path, cleaned.cleaned_sha256, cleaned.revision, "cleaned"


def _current_input(workspace_root: Path | str, draft_id: str, page_number: int) -> tuple[Path, str, int, str]:
    try:
        return _input_page(workspace_root, draft_id, page_number)
    except ExamDetectionError as exc:
        raise ExamDetectionConflict("detection input is no longer ready") from exc


def _state_path(source_path: Path) -> Path:
    return source_path.parent / "proposals.json"


def _read_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_size > 2_000_000:
        raise ExamDetectionError("proposal state is unsafe")
    try:
        state = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExamDetectionError("cannot read proposal state") from exc
    if not isinstance(state, dict) or not all(isinstance(k, str) and isinstance(v, dict) for k, v in state.items()):
        raise ExamDetectionError("invalid proposal state")
    return state


def _write_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_exam_proposals(workspace_root: Path | str, draft_id: str, page_number: int) -> ProposalPage:
    """Expose suggestions only while their cleaned image and selection are current."""
    draft = load_exam_draft(workspace_root, draft_id)
    _, input_sha, input_revision, source_mode = _input_page(workspace_root, draft_id, page_number)
    record = _read_state(_state_path(draft.source_path)).get(str(page_number))
    if record is None:
        return ProposalPage(page_number, "pending", 0, draft.revision, input_revision,
                            input_sha, source_mode, (), None)
    revision = record.get("revision")
    if type(revision) is not int or revision < 1 or record.get("status") not in ("running", "ready", "failed"):
        raise ExamDetectionError("invalid proposal revision or status")
    if (record.get("selection_revision") != draft.revision
            or record.get("cleaned_revision") != input_revision
            or record.get("cleaned_sha256") != input_sha
            or record.get("source_mode") != source_mode):
        return ProposalPage(page_number, "pending", revision, draft.revision, input_revision,
                            input_sha, source_mode, (), None)
    contexts = record.get("contexts", [])
    if not isinstance(contexts, list):
        raise ExamDetectionError("invalid proposal context")
    for context in contexts:
        if not isinstance(context, dict) or type(context.get("page_number")) is not int:
            raise ExamDetectionError("invalid proposal context")
        _, adjacent_sha, adjacent_revision, adjacent_mode = _input_page(
            workspace_root, draft_id, context["page_number"])
        if (adjacent_revision != context.get("revision") or adjacent_sha != context.get("sha256")
                or adjacent_mode != context.get("source_mode")):
            return ProposalPage(page_number, "pending", revision, draft.revision, input_revision,
                                input_sha, source_mode, (), None)
    suggestions = record.get("suggestions", [])
    if not isinstance(suggestions, list):
        raise ExamDetectionError("invalid proposal suggestions")
    return ProposalPage(page_number, record["status"], revision, draft.revision, input_revision,
                        input_sha, source_mode, tuple(suggestions), record.get("error_code"))


def _claim(workspace_root: Path | str, draft_id: str, page_number: int) -> tuple[str, ProposalPage]:
    with _DRAFT_LOCK:
        draft = load_exam_draft(workspace_root, draft_id)
        current = load_exam_proposals(workspace_root, draft_id, page_number)
        state_path = _state_path(draft.source_path)
        state = _read_state(state_path)
        attempt = uuid.uuid4().hex
        revision = current.revision + 1
        state[str(page_number)] = {"status": "running", "revision": revision,
                                   "selection_revision": current.selection_revision,
                                   "cleaned_revision": current.cleaned_revision,
                                   "cleaned_sha256": current.cleaned_sha256,
                                   "source_mode": current.source_mode,
                                   "attempt": attempt, "suggestions": [], "error_code": None}
        _write_state(state_path, state)
        return attempt, ProposalPage(page_number, "running", revision, current.selection_revision,
                                     current.cleaned_revision, current.cleaned_sha256,
                                     current.source_mode, (), None)


def _part(part: dict[str, Any], primary_page: int, available: set[int], label: str | None = None) -> dict[str, Any]:
    page_number = primary_page + part["page_offset"]
    if page_number not in available:
        raise ExamDetectionError("proposal references a page without a cleaned attachment")
    box = part["bbox"]
    return {"page_number": page_number, "role": part["role"],
            "box": [coordinate / 1000 for coordinate in box], "subquestion_label": label}


def _suggestions(result: dict[str, Any], draft_id: str, page_number: int, available: set[int]) -> list[dict[str, Any]]:
    errors = validate_detection(result)
    if errors:
        raise ExamDetectionError("invalid detection: " + "; ".join(errors[:3]))
    suggestions: list[dict[str, Any]] = []
    for order, question in enumerate(result["questions"], 1):
        parts = [_part(part, page_number, available) for part in question["shared_context_parts"]]
        if question["number_bbox"]:
            parts.append({"page_number": page_number, "role": "main_number",
                          "box": [value / 1000 for value in question["number_bbox"]],
                          "subquestion_label": None})
        parts.extend(_part(part, page_number, available) for part in question["main_parts"])
        for subquestion in question["subquestions"]:
            parts.extend(_part(part, page_number, available, subquestion["label"])
                         for part in subquestion["parts"])
        suggestions.append({"region_id": stable_exam_region_id(draft_id, page_number, question["local_id"]),
                            "page_number": page_number, "local_id": question["local_id"],
                            "printed_number": question["printed_number"], "order": order,
                            "kind": question["kind"], "answer_type": question["answer_type"],
                            "group_label": question["group_label"], "confidence": question["confidence"],
                            "starts_on_previous_page": question["starts_on_previous_page"],
                            "continues_next_page": question["continues_next_page"],
                            "parts": parts, "selected": False, "confirmed": False})
    return suggestions


def _bind_contexts(
    workspace_root: Path | str, draft_id: str, snapshot: ProposalPage, attempt: str,
    contexts: list[dict[str, Any]],
) -> None:
    with _DRAFT_LOCK:
        draft = load_exam_draft(workspace_root, draft_id)
        path = _state_path(draft.source_path)
        state = _read_state(path)
        record = state.get(str(snapshot.page_number), {})
        _, primary_sha, primary_revision, primary_mode = _current_input(
            workspace_root, draft_id, snapshot.page_number)
        if (draft.revision != snapshot.selection_revision or record.get("attempt") != attempt
                or primary_revision != snapshot.cleaned_revision
                or primary_sha != snapshot.cleaned_sha256 or primary_mode != snapshot.source_mode):
            raise ExamDetectionConflict("stale detection request")
        for context in contexts:
            _, adjacent_sha, adjacent_revision, adjacent_mode = _current_input(
                workspace_root, draft_id, context["page_number"])
            if (adjacent_revision != context["revision"] or adjacent_sha != context["sha256"]
                    or adjacent_mode != context["source_mode"]):
                raise ExamDetectionConflict("stale neighboring cleaned page")
        record["contexts"] = contexts
        _write_state(path, state)


def _finish(
    workspace_root: Path | str, draft_id: str, snapshot: ProposalPage, attempt: str,
    *, contexts: list[dict[str, Any]], suggestions: list[dict[str, Any]] | None,
    error_code: str | None,
) -> ProposalPage:
    with _DRAFT_LOCK:
        draft = load_exam_draft(workspace_root, draft_id)
        _, input_sha, input_revision, input_mode = _current_input(
            workspace_root, draft_id, snapshot.page_number)
        path = _state_path(draft.source_path)
        state = _read_state(path)
        record = state.get(str(snapshot.page_number), {})
        if (draft.revision != snapshot.selection_revision
                or input_revision != snapshot.cleaned_revision
                or input_sha != snapshot.cleaned_sha256 or input_mode != snapshot.source_mode
                or record.get("attempt") != attempt or record.get("revision") != snapshot.revision
                or record.get("status") != "running"):
            raise ExamDetectionConflict("stale detection result")
        for context in contexts:
            _, adjacent_sha, adjacent_revision, adjacent_mode = _current_input(
                workspace_root, draft_id, context["page_number"])
            if (adjacent_revision != context["revision"] or adjacent_sha != context["sha256"]
                    or adjacent_mode != context["source_mode"]):
                raise ExamDetectionConflict("stale neighboring cleaned page")
        record.update(status="ready" if suggestions is not None else "failed",
                      suggestions=suggestions if suggestions is not None else [],
                      contexts=contexts, error_code=error_code)
        record.pop("attempt", None)
        _write_state(path, state)
    return load_exam_proposals(workspace_root, draft_id, snapshot.page_number)


def propose_exam_page(
    workspace_root: Path | str, draft_id: str, page_number: int, *,
    runner: DetectionRunner | None = None,
) -> ProposalPage:
    """Run existing Question_Crop detection against verified cleaned page images only."""
    attempt, snapshot = _claim(workspace_root, draft_id, page_number)
    draft = load_exam_draft(workspace_root, draft_id)
    primary_path, _, _, _ = _input_page(workspace_root, draft_id, page_number)
    attachments = [primary_path]
    available = {page_number}
    contexts: list[dict[str, Any]] = []
    descriptions = [f"1. PRIMARY {snapshot.source_mode} page {page_number}; coordinate space [0,0,1000,1000]."]
    for neighbor, label in ((page_number - 1, "PREVIOUS"), (page_number + 1, "NEXT")):
        if neighbor < 1 or neighbor > draft.page_count or not any(p.number == neighbor for p in draft.selected_pages):
            continue
        try:
            adjacent_path, adjacent_sha, adjacent_revision, adjacent_mode = _input_page(
                workspace_root, draft_id, neighbor)
        except ExamDetectionError:
            continue
        if adjacent_path:
            attachments.append(adjacent_path)
            available.add(neighbor)
            contexts.append({"page_number": neighbor, "revision": adjacent_revision,
                             "sha256": adjacent_sha, "source_mode": adjacent_mode})
            descriptions.append(f"{len(attachments)}. {label} {adjacent_mode} page {neighbor}; "
                                f"page_offset={neighbor - page_number}.")
    _bind_contexts(workspace_root, draft_id, snapshot, attempt, contexts)
    try:
        template = PROMPT_PATH.read_text("utf-8")
        schema_sha = sha256_file(SCHEMA_PATH)
    except OSError:
        return _finish(workspace_root, draft_id, snapshot, attempt,
                       contexts=contexts, suggestions=None, error_code="detection_configuration_failed")
    prompt = build_prompt(template, Path(draft.original_name), page_number - 1,
                          draft.page_count, "\n".join(descriptions), "(텍스트 힌트 없음)")
    output = draft.source_path.parent / "detection-attempts" / f"page-{page_number:04d}-{attempt}.json"
    job = {"page_index": page_number - 1, "output": output,
           "log_dir": draft.source_path.parent / "detection-logs", "run_dir": draft.source_path.parent,
           "attachments": attachments, "prompt": prompt,
           "meta": {"source_sha256": draft.source_sha256, "input_sha256": snapshot.cleaned_sha256,
                    "input_revision": snapshot.cleaned_revision, "source_mode": snapshot.source_mode,
                    "selection_revision": snapshot.selection_revision,
                    "page": page_number, "schema_sha256": schema_sha},
           "context_pages": 1 if len(attachments) > 1 else 0, "model": None,
           "reasoning_effort": "medium", "timeout": 180, "retries": 0, "force": True}
    try:
        response = (runner or run_codex_detection)(job)
        if response.get("status") not in ("ok", "cached"):
            raise ExamDetectionError("detection runner failed")
        result = load_detection(output)["result"]
        suggestions = _suggestions(result, draft_id, page_number, available)
    except (OSError, ValueError, KeyError, TypeError, ExamDetectionError):
        return _finish(workspace_root, draft_id, snapshot, attempt,
                       contexts=contexts, suggestions=None, error_code="detection_failed")
    return _finish(workspace_root, draft_id, snapshot, attempt,
                   contexts=contexts, suggestions=suggestions, error_code=None)
