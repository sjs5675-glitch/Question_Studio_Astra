from __future__ import annotations

import copy
import json
import math
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from PIL import Image

from .exam_detection import _input_page
from .exam_region import _LOCK as _DRAFT_LOCK, ExamDraftConflict, ExamDraftError, load_exam_draft, validate_region_box
from .exam_region_cleaning import _active_sha_unlocked, load_region_crop
from .exam_selection import load_exam_selection, original_exam_page
from .model import (
    atomic_write_json,
    load_json_object,
    make_source_only_project,
    make_source_only_question,
    save_json_cas,
    sha256_file,
    utc_now,
)
from .transcription import TranscriptionAdapter, apply_candidate_job, create_transcription_job
from scripts.validate_package import DEFAULT_SCHEMA, Validator, json_schema_errors


class ExamPublicationError(ExamDraftError):
    pass


class ExamPublicationConflict(ExamDraftConflict):
    pass


def project_id_for_draft(draft_id: str) -> str:
    return f"project-{uuid.uuid5(uuid.NAMESPACE_URL, f'exam-region:{draft_id}')}"


def item_id_for_region(draft_id: str, region_id: str) -> str:
    return f"item-{uuid.uuid5(uuid.NAMESPACE_URL, f'exam-region:{draft_id}:{region_id}')}"


def _state_path(draft_path: Path) -> Path:
    return draft_path.parent / "publication.json"


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"items": {}}
    if path.is_symlink() or path.stat().st_size > 1_000_000:
        raise ExamPublicationError("publication state is unsafe")
    try:
        state = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExamPublicationError("cannot read publication state") from exc
    if not isinstance(state, dict) or not isinstance(state.get("items"), dict):
        raise ExamPublicationError("invalid publication state")
    return state


def _set_state(root: Path | str, draft_id: str, region_id: str, **changes: Any) -> None:
    with _DRAFT_LOCK:
        draft = load_exam_draft(root, draft_id)
        path = _state_path(draft.source_path)
        state = _read_state(path)
        current = state["items"].get(region_id, {})
        state["items"][region_id] = {**current, **changes, "updated_at": utc_now()}
        atomic_write_json(path, state)


def load_exam_publication(root: Path | str, draft_id: str) -> dict[str, Any]:
    draft = load_exam_draft(root, draft_id)
    selection_by_page = {}
    for page in draft.selected_pages:
        selected = load_exam_selection(root, draft_id, page.number)
        if selected.status == "ready":
            for region in selected.regions:
                if region.get("selected") and region.get("confirmed"):
                    selection_by_page[region["region_id"]] = selected.revision
    state = _read_state(_state_path(draft.source_path))
    items = []
    for region_id, selection_revision in selection_by_page.items():
        item = state["items"].get(region_id, {})
        items.append({"region_id": region_id, "item_id": item_id_for_region(draft_id, region_id),
                      "status": item.get("status", "pending"), "error_code": item.get("error_code"),
                      "selection_revision": selection_revision,
                      "saved_item_id": item.get("saved_item_id")})
    return {"draft_id": draft_id, "project_id": project_id_for_draft(draft_id), "items": items}


def _snapshot(root: Path | str, draft_id: str, page_number: int, region_id: str) -> tuple[Any, dict[str, Any]]:
    selected = load_exam_selection(root, draft_id, page_number)
    if selected.status != "ready":
        raise ExamPublicationConflict("selection source changed")
    region = next((value for value in selected.regions if value.get("region_id") == region_id), None)
    if not region or not region.get("selected") or not region.get("confirmed"):
        raise ExamPublicationConflict("region is not selected and confirmed")
    if not isinstance(region.get("parts"), list) or not region["parts"]:
        raise ExamPublicationError("confirmed region has no parts")
    return selected, copy.deepcopy(region)


def _asset(path: Path, item_root: Path, asset_id: str, role: str) -> dict[str, Any]:
    with Image.open(path) as opened:
        width, height = opened.size
    return {"asset_id": asset_id, "section": "question", "role": role, "state": "ready",
            "path": path.relative_to(item_root).as_posix(), "media_type": "image/png",
            "sha256": sha256_file(path), "width_px": width, "height_px": height,
            "generation": None,
            "review": {"status": "unreviewed", "boundary_uncertain": False, "notes": []}}


def _prepare_item(root: Path | str, draft_id: str, page_number: int, region_id: str,
                  selection: Any, region: dict[str, Any], stage: Path) -> Path:
    draft = load_exam_draft(root, draft_id)
    item_root = stage / "item"
    source_dir = item_root / "source"
    source_dir.mkdir(parents=True)
    images: list[Image.Image] = []
    part_records: list[dict[str, Any]] = []
    source_parts: list[dict[str, Any]] = []
    page_refs: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    seen_pages: set[int] = set()
    for part in region["parts"]:
        number = part["page_number"]
        box = validate_region_box(tuple(part["box"]))
        input_path, digest, input_revision, source_mode = _input_page(root, draft_id, number)
        with Image.open(input_path) as opened:
            image = opened.convert("RGB")
        left = math.floor(box[0] * image.width)
        top = math.floor(box[1] * image.height)
        right = math.ceil(box[2] * image.width)
        bottom = math.ceil(box[3] * image.height)
        images.append(image.crop((left, top, right, bottom)))
        bbox = {"unit": "ratio", "origin": "top-left", "left": box[0], "top": box[1],
                "right": box[2], "bottom": box[3]}
        part_records.append({"page_number": number, "role": part["role"], "bbox": bbox,
                             "source_mode": source_mode, "input_sha256": digest,
                             "input_revision": input_revision})
        source_parts.append({"role": part["role"], "raw_source_page": number,
                             "raw_page_base": "one_based", "pdf_page_index": number - 1,
                             "region": {"source_asset_id": f"source-page-{number}", "bbox": bbox}})
        if number in seen_pages:
            continue
        seen_pages.add(number)
        input_dest = source_dir / f"input-page-{number:04d}.png"
        shutil.copyfile(input_path, input_dest)
        if sha256_file(input_dest) != digest:
            raise ExamPublicationError("input page changed while copying")
        assets.append(_asset(input_dest, item_root, f"source-page-{number}", "source_page"))
        original_path, original_sha, _ = original_exam_page(root, draft_id, number)
        original_dest = source_dir / f"original-page-{number:04d}.png"
        shutil.copyfile(original_path, original_dest)
        if sha256_file(original_dest) != original_sha:
            raise ExamPublicationError("original page changed while copying")
        assets.append(_asset(original_dest, item_root, f"original-page-{number}", "reference"))
        rotation = next(page.rotation for page in draft.selected_pages if page.number == number)
        page_refs.append({"raw_source_page": number, "raw_page_base": "one_based",
                          "pdf_page_index": number - 1,
                          "page_asset_id": f"source-page-{number}" if draft.source_kind == "pdf" else None,
                          "render_dpi": 216 if draft.source_kind == "pdf" else None,
                          "rotation_degrees": rotation, "flip_horizontal": False})
    if not images:
        raise ExamPublicationError("region has no crop images")
    combined = Image.new("RGB", (max(image.width for image in images),
                                 sum(image.height for image in images) + 12 * (len(images) - 1)), "white")
    position = 0
    for image in images:
        combined.paste(image, (0, position))
        position += image.height + 12
    crop_path = source_dir / "question.png"
    combined.save(crop_path, "PNG")
    crop = load_region_crop(root, draft_id, page_number, region_id)
    if sha256_file(crop_path) != crop.base_sha256:
        raise ExamPublicationConflict("region crop changed while preparing publication")
    if crop.adopted_revision is not None:
        original_crop = source_dir / "question-original.png"
        crop_path.rename(original_crop)
        assets.append(_asset(original_crop, item_root, "source-question-original", "reference"))
        shutil.copyfile(crop.active_path, crop_path)
        if sha256_file(crop_path) != crop.active_sha256:
            raise ExamPublicationConflict("adopted crop changed while copying")
    assets.insert(0, _asset(crop_path, item_root, "source-question", "source_crop"))
    identity = {"printed_number": str(region.get("printed_number") or region.get("order") or "1"),
                "unit_kind": "question", "answer_type": region.get("answer_type", "unknown"),
                "score": None, "parent_item_id": None, "group_id": None}
    provenance = {"imported_at": utc_now(),
                  "source_system": {"name": "exam_region", "pipeline_version": "1"},
                  "problem": {"document_sha256": draft.source_sha256,
                              "document_page_count": draft.page_count, "original_filename": draft.original_name,
                              "unit_id": region_id, "owner_question_id": region_id,
                              "source_pages": page_refs, "source_parts": source_parts,
                              "crop_asset_id": "source-question", "rotation_degrees": 0,
                              "flip_horizontal": False},
                  "exam_region": {"draft_id": draft_id, "region_id": region_id,
                                  "selection_revision": selection.revision,
                                  "display_order": region.get("order", 1),
                                  "group_label": region.get("group_label"),
                                  "input_parts": part_records},
                  "solution_links": [], "transcriptions": [], "raw_snapshot": None}
    package = make_source_only_question(item_id=item_id_for_region(draft_id, region_id),
                                        identity=identity, provenance=provenance, assets=assets)
    path = item_root / "question.json"
    atomic_write_json(path, package)
    return path


def publish_exam_region(root: Path | str, draft_id: str, page_number: int, region_id: str, *,
                        adapter: TranscriptionAdapter | None = None) -> dict[str, Any]:
    draft = load_exam_draft(root, draft_id)
    project_id = project_id_for_draft(draft_id)
    item_id = item_id_for_region(draft_id, region_id)
    project_dir = Path(root).resolve() / project_id
    item_dir = project_dir / "items" / item_id
    if any(path.is_symlink() for path in (project_dir, item_dir.parent, item_dir, project_dir / "source")):
        raise ExamPublicationError("publication path must not be a link")
    if item_dir.exists():
        existing = load_json_object(item_dir / "question.json")
        if existing.get("provenance", {}).get("exam_region", {}).get("region_id") != region_id:
            raise ExamPublicationConflict("canonical item ID is occupied")
        if existing.get("content", {}).get("question") is not None:
            return {"status": "succeeded", "item_id": item_id, "project_id": project_id,
                    "item_revision": existing["workflow"]["revision"], "already_published": True}
        raise ExamPublicationConflict("incomplete canonical item needs manual recovery")
    selection, region = _snapshot(root, draft_id, page_number, region_id)
    stage_root = Path(root).resolve() / ".exam-stage"
    if stage_root.is_symlink():
        raise ExamPublicationError("publication staging path must not be a link")
    stage = stage_root / uuid.uuid4().hex[:12]
    stage.mkdir(parents=True)
    question_path = _prepare_item(root, draft_id, page_number, region_id, selection, region, stage)
    crop_input_sha = sha256_file(question_path.parent / "source" / "question.png")
    job = create_transcription_job(question_path, "source-question", "question", adapter=adapter)
    applied = apply_candidate_job(question_path, job.path, expected_base_revision=1,
                                  discard_candidate_answer=True)
    if applied["content"]["question"] is None or applied["content"]["answer"] is not None:
        raise ExamPublicationError("question transcription did not produce safe question content")
    validation = json_schema_errors(applied, DEFAULT_SCHEMA) + Validator(question_path, check_files=True).validate(applied)
    if validation:
        raise ExamPublicationError("canonical question validation failed: " + validation[0])
    with _DRAFT_LOCK:
        current, current_region = _snapshot(root, draft_id, page_number, region_id)
        if current.revision != selection.revision or current_region != region:
            raise ExamPublicationConflict("selection changed during transcription")
        if _active_sha_unlocked(root, draft_id, page_number, region_id) != crop_input_sha:
            raise ExamPublicationConflict("adopted crop changed during transcription")
        for part in applied["provenance"]["exam_region"]["input_parts"]:
            _, sha, revision, mode = _input_page(root, draft_id, part["page_number"])
            if (sha != part["input_sha256"] or revision != part["input_revision"]
                    or mode != part["source_mode"]):
                raise ExamPublicationConflict("input page changed during transcription")
        if item_dir.exists():
            raise ExamPublicationConflict("another publication already created this item")
        project_dir.mkdir(parents=True, exist_ok=True)
        project_path = project_dir / "project.json"
        if project_path.exists():
            project = load_json_object(project_path)
            source_import = project.get("source_import") or {}
            if (source_import.get("draft_id") != draft_id
                    or source_import.get("document_sha256") != draft.source_sha256):
                raise ExamPublicationConflict("project ID belongs to another draft")
            if item_id not in project["item_order"]:
                project["item_order"].append(item_id)
                save_json_cas(project_path, project, project["workflow"]["revision"])
        else:
            source_dir = project_dir / "source"
            source_dir.mkdir(exist_ok=True)
            target = source_dir / draft.source_path.name
            if target.is_symlink():
                raise ExamPublicationError("project original source must not be a link")
            if not target.exists():
                shutil.copyfile(draft.source_path, target)
            if sha256_file(target) != draft.source_sha256:
                raise ExamPublicationError("project original source SHA-256 mismatch")
            project = make_source_only_project(
                project_id=project_id, item_ids=[item_id], groups=[],
                source_import={"draft_id": draft_id, "document_sha256": draft.source_sha256,
                               "original_filename": draft.original_name, "imported_at": utc_now()})
            project["metadata"]["filename_base"] = Path(draft.original_name).stem
            project["metadata"]["book_title"] = Path(draft.original_name).stem
            atomic_write_json(project_path, project)
        item_dir.parent.mkdir(exist_ok=True)
        for attempt in range(5):
            try:
                (stage / "item").rename(item_dir)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(.05 * (attempt + 1))
    return {"status": "succeeded", "item_id": item_id, "project_id": project_id,
            "item_revision": applied["workflow"]["revision"], "already_published": False}


def publish_confirmed_exam_regions(root: Path | str, draft_id: str, *,
                                   adapter: TranscriptionAdapter | None = None) -> dict[str, Any]:
    draft = load_exam_draft(root, draft_id)
    targets: list[tuple[int, int, str]] = []
    for page in draft.selected_pages:
        selected = load_exam_selection(root, draft_id, page.number)
        if selected.status == "ready":
            targets.extend((region.get("order", 0), page.number, region["region_id"])
                           for region in selected.regions if region.get("selected") and region.get("confirmed"))
    for _, page_number, region_id in sorted(targets):
        _set_state(root, draft_id, region_id, status="running", error_code=None,
                   item_id=item_id_for_region(draft_id, region_id))
        try:
            result = publish_exam_region(root, draft_id, page_number, region_id, adapter=adapter)
        except (ExamDraftError, OSError, ValueError) as exc:
            _set_state(root, draft_id, region_id, status="failed", error_code=type(exc).__name__)
        else:
            _set_state(root, draft_id, region_id, status="succeeded", error_code=None,
                       saved_item_id=result["item_id"])
    return load_exam_publication(root, draft_id)
