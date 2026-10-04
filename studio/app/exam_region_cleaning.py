from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from vendor.pnkmath.image_cleaner import ImageCleanerError, ImageProvider

from .exam_cleaning import ExamCleaningError, _clean_tile, _page_canvas
from .exam_detection import _input_page
from .exam_region import _LOCK as _DRAFT_LOCK, ExamDraftConflict, ExamDraftError, load_exam_draft, validate_region_box
from .exam_selection import load_exam_selection
from .model import atomic_write_json, sha256_file


class ExamRegionCleaningError(ExamDraftError):
    pass


class ExamRegionCleaningConflict(ExamDraftConflict):
    pass


@dataclass(frozen=True, slots=True)
class RegionCrop:
    revision: int
    status: str
    base_path: Path
    base_sha256: str
    active_path: Path
    active_sha256: str
    adopted_revision: int | None
    previous_revision: int | None
    candidates: tuple[dict[str, Any], ...]
    error_code: str | None


def _state_path(source_path: Path) -> Path:
    return source_path.parent / "region-cleaning.json"


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"regions": {}}
    if path.is_symlink() or path.stat().st_size > 4_000_000:
        raise ExamRegionCleaningError("region cleaning state is unsafe")
    try:
        state = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExamRegionCleaningError("cannot read region cleaning state") from exc
    if not isinstance(state, dict) or not isinstance(state.get("regions"), dict):
        raise ExamRegionCleaningError("invalid region cleaning state")
    return state


def _snapshot(root: Path | str, draft_id: str, page_number: int, region_id: str) -> tuple[Any, dict[str, Any], str]:
    selected = load_exam_selection(root, draft_id, page_number)
    if selected.status != "ready":
        raise ExamRegionCleaningConflict("region selection is stale")
    region = next((value for value in selected.regions if value.get("region_id") == region_id), None)
    if not region or not region.get("selected") or not region.get("confirmed"):
        raise ExamRegionCleaningConflict("region is not selected and confirmed")
    parts = region.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ExamRegionCleaningError("confirmed region has no parts")
    sources = []
    for part in parts:
        _, digest, revision, mode = _input_page(root, draft_id, part["page_number"])
        sources.append((digest, revision, mode))
    payload = json.dumps({"parts": parts, "sources": sources}, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return selected, copy.deepcopy(region), fingerprint


def _crop_image(root: Path | str, draft_id: str, region: dict[str, Any]) -> Image.Image:
    images = []
    for part in region["parts"]:
        path, _, _, _ = _input_page(root, draft_id, part["page_number"])
        box = validate_region_box(tuple(part["box"]))
        with Image.open(path) as opened:
            image = opened.convert("RGB")
        bounds = (math.floor(box[0] * image.width), math.floor(box[1] * image.height),
                  math.ceil(box[2] * image.width), math.ceil(box[3] * image.height))
        images.append(image.crop(bounds))
    combined = Image.new("RGB", (max(image.width for image in images),
                                 sum(image.height for image in images) + 12 * (len(images) - 1)), "white")
    top = 0
    for image in images:
        combined.paste(image, (0, top))
        top += image.height + 12
    return combined


def _current(root: Path | str, draft_id: str, page_number: int, region_id: str) -> tuple[Path, dict[str, Any]]:
    draft = load_exam_draft(root, draft_id)
    selected, region, fingerprint = _snapshot(root, draft_id, page_number, region_id)
    path = _state_path(draft.source_path)
    state = _read_state(path)
    key = hashlib.sha256(region_id.encode("utf-8")).hexdigest()[:16]
    record = state["regions"].get(region_id)
    if record is None or record.get("fingerprint") != fingerprint:
        name = f"region-{key}-{fingerprint[:16]}-base.png"
        base = draft.source_path.parent / name
        if not base.exists():
            temporary = base.with_name(f".{name}.{uuid.uuid4().hex}.tmp")
            try:
                _crop_image(root, draft_id, region).save(temporary, "PNG")
                os.replace(temporary, base)
            finally:
                temporary.unlink(missing_ok=True)
        record = {"revision": (record or {}).get("revision", 0) + 1,
                  "fingerprint": fingerprint, "base_name": name,
                  "base_sha256": sha256_file(base), "selection_revision": selected.revision,
                  "candidates": (record or {}).get("candidates", []),
                  "adopted_revision": None, "previous_revision": None,
                  "status": "ready", "error_code": None}
        state["regions"][region_id] = record
        atomic_write_json(path, state)
    return path, record


def _image_path(source_path: Path, name: str, digest: str) -> Path:
    if not isinstance(name, str) or Path(name).name != name or not name.endswith(".png"):
        raise ExamRegionCleaningError("unsafe crop image reference")
    path = source_path.parent / name
    if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
        raise ExamRegionCleaningConflict("crop image is missing or changed")
    return path


def load_region_crop(root: Path | str, draft_id: str, page_number: int, region_id: str) -> RegionCrop:
    with _DRAFT_LOCK:
        state_path, record = _current(root, draft_id, page_number, region_id)
        base = _image_path(state_path, record["base_name"], record["base_sha256"])
        candidates = tuple(record["candidates"])
        adopted = record["adopted_revision"]
        active = base
        digest = record["base_sha256"]
        if adopted is not None:
            selected = next((item for item in candidates if item["revision"] == adopted
                             and item["fingerprint"] == record["fingerprint"]), None)
            if selected is None:
                raise ExamRegionCleaningConflict("adopted crop is stale")
            active = _image_path(state_path, selected["name"], selected["sha256"])
            digest = selected["sha256"]
        return RegionCrop(record["revision"], record["status"], base, record["base_sha256"],
                          active, digest, adopted, record.get("previous_revision"),
                          candidates, record.get("error_code"))


def _active_sha_unlocked(root: Path | str, draft_id: str, page_number: int, region_id: str) -> str:
    state_path, record = _current(root, draft_id, page_number, region_id)
    adopted = record["adopted_revision"]
    if adopted is None:
        return record["base_sha256"]
    candidate = next((item for item in record["candidates"] if item["revision"] == adopted
                      and item["fingerprint"] == record["fingerprint"]), None)
    if candidate is None:
        raise ExamRegionCleaningConflict("adopted crop is stale")
    _image_path(state_path, candidate["name"], candidate["sha256"])
    return candidate["sha256"]


def clean_region_crop(root: Path | str, draft_id: str, page_number: int, region_id: str, *,
                      expected_revision: int, area: tuple[float, float, float, float] | None = None,
                      provider: ImageProvider | None = None) -> RegionCrop:
    if area is not None:
        area = validate_region_box(tuple(area))
    with _DRAFT_LOCK:
        state_path, record = _current(root, draft_id, page_number, region_id)
        if expected_revision != record["revision"] or record["status"] == "running":
            raise ExamRegionCleaningConflict("stale crop cleaning revision")
        base = _image_path(state_path, record["base_name"], record["base_sha256"])
        fingerprint = record["fingerprint"]
        attempt = uuid.uuid4().hex
        state = _read_state(state_path)
        state["regions"][region_id].update(status="running", attempt=attempt, error_code=None,
                                             revision=record["revision"] + 1)
        atomic_write_json(state_path, state)
    try:
        with Image.open(base) as opened:
            canvas, bounds, aspect = _page_canvas(opened.convert("RGB"))
            base_size = opened.size
        prompt = (Path(__file__).resolve().parents[1] / "prompts" / "clean_exam_tile.md").read_text("utf-8")
        if area is not None:
            prompt += f" Focus removal in normalized crop rectangle {area}; preserve content outside it."
        from vendor.pnkmath.image_provider_adapter import CodexCliImageProvider
        image_provider = provider or CodexCliImageProvider()
        source = state_path.parent / f"tile-region-{hashlib.sha256(region_id.encode()).hexdigest()[:16]}-{attempt}.png"
        try:
            result = _clean_tile(canvas, source, image_provider, prompt, aspect)
        finally:
            source.unlink(missing_ok=True)
        cropped = result.crop(bounds).resize(base_size, Image.Resampling.LANCZOS)
    except (ImageCleanerError, OSError, ValueError, ExamRegionCleaningError, ExamCleaningError) as exc:
        code = str(exc) if isinstance(exc, ExamCleaningError) else type(exc).__name__
        if isinstance(exc, ImageCleanerError):
            code = next((known for known in ("codex_cli_timeout", "codex_cli_launch_failed",
                                              "codex_cli_failed", "codex_cli_no_image",
                                              "codex_cli_invalid_image") if known in str(exc)),
                        "provider_response_invalid")
        with _DRAFT_LOCK:
            state = _read_state(state_path)
            current = state["regions"].get(region_id, {})
            if current.get("attempt") == attempt:
                current.update(status="failed", error_code=code, revision=current["revision"] + 1)
                current.pop("attempt", None)
                atomic_write_json(state_path, state)
        return load_region_crop(root, draft_id, page_number, region_id)
    with _DRAFT_LOCK:
        current_path, current = _current(root, draft_id, page_number, region_id)
        if (current_path != state_path or current.get("attempt") != attempt
                or current["fingerprint"] != fingerprint):
            raise ExamRegionCleaningConflict("region changed during cleaning")
        next_revision = max((item["revision"] for item in current["candidates"]), default=0) + 1
        key = hashlib.sha256(region_id.encode("utf-8")).hexdigest()[:16]
        name = f"region-{key}-candidate-{next_revision}.png"
        destination = state_path.parent / name
        cropped.save(destination, "PNG")
        current["candidates"].append({"revision": next_revision, "name": name,
                                      "sha256": sha256_file(destination), "fingerprint": fingerprint,
                                      "base_sha256": current["base_sha256"],
                                      "selection_revision": current["selection_revision"], "area": area})
        current.update(status="ready", revision=current["revision"] + 1, error_code=None)
        current.pop("attempt", None)
        state = _read_state(state_path)
        state["regions"][region_id] = current
        atomic_write_json(state_path, state)
    return load_region_crop(root, draft_id, page_number, region_id)


def restore_region_crop(root: Path | str, draft_id: str, page_number: int, region_id: str, *,
                        expected_revision: int, candidate_revision: int | None = None) -> RegionCrop:
    with _DRAFT_LOCK:
        state_path, record = _current(root, draft_id, page_number, region_id)
        if expected_revision != record["revision"] or record["status"] == "running":
            raise ExamRegionCleaningConflict("stale crop cleaning revision")
        if candidate_revision is not None:
            candidate = next((item for item in record["candidates"]
                              if item["revision"] == candidate_revision
                              and item["fingerprint"] == record["fingerprint"]
                              and item["base_sha256"] == record["base_sha256"]), None)
            if candidate is None:
                raise ExamRegionCleaningConflict("candidate does not match current crop")
            _image_path(state_path, candidate["name"], candidate["sha256"])
        record.update(adopted_revision=candidate_revision,
                      previous_revision=record["adopted_revision"], revision=record["revision"] + 1)
        state = _read_state(state_path)
        state["regions"][region_id] = record
        atomic_write_json(state_path, state)
    return load_region_crop(root, draft_id, page_number, region_id)


def adopt_region_crop(root: Path | str, draft_id: str, page_number: int, region_id: str, *,
                      expected_revision: int, candidate_revision: int) -> RegionCrop:
    return restore_region_crop(root, draft_id, page_number, region_id,
                               expected_revision=expected_revision, candidate_revision=candidate_revision)
