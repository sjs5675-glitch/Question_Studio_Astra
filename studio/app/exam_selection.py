from __future__ import annotations

import copy
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .exam_region import _LOCK as _DRAFT_LOCK, ExamDraftConflict, ExamDraftError, load_exam_draft, render_exam_page, validate_region_box
from .model import sha256_file


class ExamSelectionError(ExamDraftError):
    pass


class ExamSelectionConflict(ExamDraftConflict):
    pass


@dataclass(frozen=True, slots=True)
class SelectionPage:
    page_number: int
    revision: int
    status: str
    source_mode: str
    source_path: Path | None
    source_sha256: str | None
    source_revision: int
    proposal_revision: int
    regions: tuple[dict[str, Any], ...]


def _state_path(source_path: Path) -> Path:
    return source_path.parent / "selections.json"


def _read_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_size > 4_000_000:
        raise ExamSelectionError("selection state is unsafe")
    try:
        state = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExamSelectionError("cannot read selection state") from exc
    if not isinstance(state, dict) or not all(isinstance(k, str) and isinstance(v, dict) for k, v in state.items()):
        raise ExamSelectionError("invalid selection state")
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


def original_exam_page(workspace_root: Path | str, draft_id: str, page_number: int) -> tuple[Path, str, int]:
    """Return a persisted original-page raster for explicit unprocessed use and comparison."""
    with _DRAFT_LOCK:
        draft = load_exam_draft(workspace_root, draft_id)
        choice = next((page for page in draft.selected_pages if page.number == page_number), None)
        if choice is None:
            raise ExamSelectionError("page is not selected")
        path = draft.source_path.parent / f"original-page-{page_number:04d}-r{draft.revision}.png"
        if not path.exists():
            image = render_exam_page(draft, choice, scale=3.0)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as output:
                    image.save(output, format="PNG")
                    output.flush()
                    os.fsync(output.fileno())
                temporary.rename(path)
            finally:
                temporary.unlink(missing_ok=True)
        if path.is_symlink() or not path.is_file():
            raise ExamSelectionError("original page raster is unsafe")
        return path, sha256_file(path), draft.revision


def no_clean_source(workspace_root: Path | str, draft_id: str, page_number: int) -> tuple[Path, str, int] | None:
    draft = load_exam_draft(workspace_root, draft_id)
    record = _read_state(_state_path(draft.source_path)).get(str(page_number))
    if not record or record.get("source_mode") != "original_unprocessed":
        return None
    if record.get("draft_revision") != draft.revision:
        return None
    expected = f"original-page-{page_number:04d}-r{draft.revision}.png"
    if record.get("source_name") != expected:
        raise ExamSelectionError("unsafe original page reference")
    path = draft.source_path.parent / expected
    if path.is_symlink() or not path.is_file() or sha256_file(path) != record.get("source_sha256"):
        raise ExamSelectionError("original page raster changed")
    return path, record["source_sha256"], draft.revision


def _part_sources(workspace_root: Path | str, draft_id: str, regions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    from .exam_detection import _input_page
    result: dict[str, dict[str, Any]] = {}
    for region in regions:
        for part in region.get("parts", []):
            number = part["page_number"]
            _, digest, revision, mode = _input_page(workspace_root, draft_id, number)
            result[str(number)] = {"sha256": digest, "revision": revision, "source_mode": mode}
    return result


def _parts_current(workspace_root: Path | str, draft_id: str, record: dict[str, Any]) -> bool:
    from .exam_detection import _input_page, ExamDetectionError
    sources = record.get("part_sources", {})
    if not isinstance(sources, dict):
        return False
    for page, expected in sources.items():
        try:
            _, digest, revision, mode = _input_page(workspace_root, draft_id, int(page))
        except (ValueError, ExamDetectionError):
            return False
        if expected != {"sha256": digest, "revision": revision, "source_mode": mode}:
            return False
    return True


def skip_exam_cleaning(
    workspace_root: Path | str, draft_id: str, page_number: int, *, expected_revision: int,
) -> SelectionPage:
    """Explicitly choose the original raster; it is never labeled cleaned."""
    path, digest, draft_revision = original_exam_page(workspace_root, draft_id, page_number)
    with _DRAFT_LOCK:
        draft = load_exam_draft(workspace_root, draft_id)
        if draft.revision != draft_revision:
            raise ExamSelectionConflict("page selection changed")
        state_path = _state_path(draft.source_path)
        state = _read_state(state_path)
        old = state.get(str(page_number), {})
        revision = old.get("revision", 0)
        if type(expected_revision) is not int or expected_revision != revision:
            raise ExamSelectionConflict("stale region selection")
        state[str(page_number)] = {"revision": revision + 1, "draft_revision": draft.revision,
                                   "source_mode": "original_unprocessed", "source_name": path.name,
                                   "source_sha256": digest, "source_revision": draft.revision,
                                   "proposal_revision": 0, "regions": [], "history": []}
        _write_state(state_path, state)
    return load_exam_selection(workspace_root, draft_id, page_number)


def use_cleaned_exam_page(
    workspace_root: Path | str, draft_id: str, page_number: int, *, expected_revision: int,
) -> SelectionPage:
    """Switch an explicit original bypass back to a verified cleaned page."""
    from .exam_cleaning import load_cleaned_page
    with _DRAFT_LOCK:
        draft = load_exam_draft(workspace_root, draft_id)
        cleaned = load_cleaned_page(workspace_root, draft_id, page_number)
        if cleaned.status != "ready" or cleaned.cleaned_path is None or cleaned.cleaned_sha256 is None:
            raise ExamSelectionError("cleaned page is not ready")
        path = _state_path(draft.source_path)
        state = _read_state(path)
        old = state.get(str(page_number), {})
        revision = old.get("revision", 0)
        if type(expected_revision) is not int or expected_revision != revision:
            raise ExamSelectionConflict("stale region selection")
        state[str(page_number)] = {"revision": revision + 1, "draft_revision": draft.revision,
                                   "source_mode": "cleaned", "source_name": cleaned.cleaned_path.name,
                                   "source_sha256": cleaned.cleaned_sha256,
                                   "source_revision": cleaned.revision,
                                   "proposal_revision": 0, "regions": [], "part_sources": {}, "history": []}
        _write_state(path, state)
    return load_exam_selection(workspace_root, draft_id, page_number)


def load_exam_selection(workspace_root: Path | str, draft_id: str, page_number: int) -> SelectionPage:
    draft = load_exam_draft(workspace_root, draft_id)
    state = _read_state(_state_path(draft.source_path))
    record = state.get(str(page_number))
    if record is not None and record.get("source_mode") == "original_unprocessed":
        source = no_clean_source(workspace_root, draft_id, page_number)
        if source is None:
            return SelectionPage(page_number, record.get("revision", 0), "stale", "original_unprocessed",
                                 None, None, 0, record.get("proposal_revision", 0), ())
        path, digest, source_revision = source
        if not _parts_current(workspace_root, draft_id, record):
            return SelectionPage(page_number, record["revision"], "stale", "original_unprocessed",
                                 path, digest, source_revision, record.get("proposal_revision", 0), ())
        if not record.get("proposal_revision") and not record.get("history") and not record.get("regions"):
            from .exam_detection import load_exam_proposals
            proposal = load_exam_proposals(workspace_root, draft_id, page_number)
            if proposal.status == "ready":
                return SelectionPage(page_number, record["revision"], "ready", "original_unprocessed",
                                     path, digest, source_revision, proposal.revision,
                                     tuple(copy.deepcopy(proposal.suggestions)))
        return SelectionPage(page_number, record["revision"], "ready", "original_unprocessed",
                             path, digest, source_revision, record.get("proposal_revision", 0),
                             tuple(record.get("regions", [])))
    from .exam_cleaning import load_cleaned_page
    cleaned = load_cleaned_page(workspace_root, draft_id, page_number)
    if cleaned.status != "ready":
        return SelectionPage(page_number, record.get("revision", 0) if record else 0,
                             "pending", "cleaned", None, None, 0, 0, ())
    from .exam_detection import load_exam_proposals
    proposal = load_exam_proposals(workspace_root, draft_id, page_number)
    if (record and (record.get("draft_revision") != draft.revision
                    or record.get("source_revision") != cleaned.revision
                    or record.get("source_sha256") != cleaned.cleaned_sha256)):
        return SelectionPage(page_number, record["revision"], "stale", "cleaned",
                             cleaned.cleaned_path, cleaned.cleaned_sha256, cleaned.revision,
                             proposal.revision, ())
    if record:
        if not _parts_current(workspace_root, draft_id, record):
            return SelectionPage(page_number, record["revision"], "stale", "cleaned",
                                 cleaned.cleaned_path, cleaned.cleaned_sha256, cleaned.revision,
                                 record.get("proposal_revision", 0), ())
        if not record.get("proposal_revision") and not record.get("history") and not record.get("regions") and proposal.status == "ready":
            return SelectionPage(page_number, record["revision"], "ready", "cleaned",
                                 cleaned.cleaned_path, cleaned.cleaned_sha256, cleaned.revision,
                                 proposal.revision, tuple(copy.deepcopy(proposal.suggestions)))
        return SelectionPage(page_number, record["revision"], "ready", "cleaned",
                             cleaned.cleaned_path, cleaned.cleaned_sha256, cleaned.revision,
                             record.get("proposal_revision", 0), tuple(record.get("regions", [])))
    return SelectionPage(page_number, 0, "ready", "cleaned", cleaned.cleaned_path,
                         cleaned.cleaned_sha256, cleaned.revision, proposal.revision,
                         tuple(copy.deepcopy(proposal.suggestions)))


def _validate_part(part: dict[str, Any], selected_pages: set[int]) -> dict[str, Any]:
    if not isinstance(part, dict):
        raise ExamSelectionError("invalid region part")
    page = part.get("page_number")
    if type(page) is not int or page not in selected_pages or not isinstance(part.get("role"), str):
        raise ExamSelectionError("invalid region part")
    box = part.get("box")
    if not isinstance(box, (list, tuple)):
        raise ExamSelectionError("invalid region box")
    validate_region_box(tuple(box))
    return {"page_number": page, "role": part["role"], "box": list(box),
            "subquestion_label": part.get("subquestion_label")}


def edit_exam_selection(
    workspace_root: Path | str, draft_id: str, page_number: int, *,
    expected_revision: int, operation: dict[str, Any],
) -> SelectionPage:
    """Apply one user edit with CAS and one-step persistent undo history."""
    if not isinstance(operation, dict):
        raise ExamSelectionError("operation must be an object")
    action = operation.get("action")
    with _DRAFT_LOCK:
        draft = load_exam_draft(workspace_root, draft_id)
        current = load_exam_selection(workspace_root, draft_id, page_number)
        if (current.status != "ready" and not (current.status == "stale" and action == "reset")) or current.source_sha256 is None:
            raise ExamSelectionConflict("selection source is stale or unavailable")
        if type(expected_revision) is not int or expected_revision != current.revision:
            raise ExamSelectionConflict("stale region selection")
        path = _state_path(draft.source_path)
        state = _read_state(path)
        old = state.get(str(page_number), {})
        regions = copy.deepcopy(list(current.regions))
        region_id = operation.get("region_id")
        target = next((region for region in regions if region.get("region_id") == region_id), None)
        if action == "undo":
            history = old.get("history", [])
            if not history:
                raise ExamSelectionError("nothing to undo")
            regions = history[-1]
            history = history[:-1]
        elif action == "select_all":
            value = operation.get("value")
            if type(value) is not bool:
                raise ExamSelectionError("selection value must be boolean")
            history = old.get("history", []) + [list(current.regions)]
            for region in regions:
                region["selected"] = value
                if not value:
                    region["confirmed"] = False
        elif action == "reset":
            from .exam_detection import load_exam_proposals
            proposal = load_exam_proposals(workspace_root, draft_id, page_number)
            if proposal.status != "ready":
                raise ExamSelectionError("run detection before resetting selections")
            regions = copy.deepcopy(list(proposal.suggestions))
            history = old.get("history", []) + [list(current.regions)]
        elif action == "add":
            parts = operation.get("parts")
            if not isinstance(parts, list) or not parts:
                raise ExamSelectionError("new region needs parts")
            regions.append({"region_id": f"region-{uuid.uuid4().hex}", "page_number": page_number,
                            "printed_number": str(operation.get("printed_number", ""))[:80],
                            "order": len(regions) + 1, "parts": [_validate_part(p, {q.number for q in draft.selected_pages}) for p in parts],
                            "selected": True, "confirmed": False, "group_label": None})
            history = old.get("history", []) + [list(current.regions)]
        else:
            if target is None:
                raise ExamSelectionError("region not found")
            history = old.get("history", []) + [list(current.regions)]
            if action == "delete":
                regions.remove(target)
            elif action in ("select", "confirm"):
                value = operation.get("value")
                if type(value) is not bool:
                    raise ExamSelectionError("selection value must be boolean")
                target["selected" if action == "select" else "confirmed"] = value
                if action == "confirm" and value and not target.get("selected"):
                    raise ExamSelectionError("select a region before confirming it")
                if action == "select" and not value:
                    target["confirmed"] = False
            elif action == "number":
                value = operation.get("value")
                if not isinstance(value, str) or len(value) > 80:
                    raise ExamSelectionError("invalid printed number")
                target["printed_number"] = value
            elif action == "order":
                value = operation.get("value")
                if type(value) is not int or value < 1:
                    raise ExamSelectionError("invalid order")
                target["order"] = value
            elif action == "group":
                value = operation.get("value")
                if value is not None and (not isinstance(value, str) or len(value) > 80):
                    raise ExamSelectionError("invalid group")
                target["group_label"] = value
            elif action == "box":
                index = operation.get("part_index")
                if type(index) is not int or not 0 <= index < len(target["parts"]):
                    raise ExamSelectionError("invalid part index")
                box = operation.get("box")
                if not isinstance(box, (list, tuple)):
                    raise ExamSelectionError("invalid region box")
                target["parts"][index]["box"] = list(validate_region_box(tuple(box)))
            elif action == "add_part":
                target["parts"].append(_validate_part(operation.get("part"), {q.number for q in draft.selected_pages}))
            elif action == "delete_part":
                index = operation.get("part_index")
                if type(index) is not int or not 0 <= index < len(target["parts"]) or len(target["parts"]) == 1:
                    raise ExamSelectionError("invalid part deletion")
                target["parts"].pop(index)
            else:
                raise ExamSelectionError("unknown edit action")
        if len(regions) > 500:
            raise ExamSelectionError("too many regions")
        try:
            part_sources = _part_sources(workspace_root, draft_id, regions)
        except ExamDraftError as exc:
            raise ExamSelectionError("region part source is not ready") from exc
        state[str(page_number)] = {"revision": current.revision + 1, "draft_revision": draft.revision,
                                   "source_mode": current.source_mode,
                                   "source_name": current.source_path.name, "source_sha256": current.source_sha256,
                                   "source_revision": current.source_revision,
                                   "proposal_revision": current.proposal_revision, "regions": regions,
                                   "part_sources": part_sources, "history": history[-20:]}
        _write_state(path, state)
    return load_exam_selection(workspace_root, draft_id, page_number)
