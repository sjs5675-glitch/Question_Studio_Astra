"""Revision-safe local recrops for existing figure blocks."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .model import (
    ModelError,
    ModelNotFound,
    RevisionConflict,
    load_json_object,
    save_json_cas,
    sha256_file,
    utc_now,
    validate_id,
)
from .transcription import ImageValidationError, inspect_image


MIN_CROP_DIMENSION_PX = 1
_BBOX_FIELDS = {"unit", "origin", "left", "top", "right", "bottom"}


class FigureRecropError(ModelError):
    """Raised when a requested recrop cannot safely become a candidate asset."""


def _workflow_revision(document: Mapping[str, Any]) -> int:
    workflow = document.get("workflow")
    revision = workflow.get("revision") if isinstance(workflow, Mapping) else None
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise FigureRecropError("workflow.revision must be a positive integer")
    return revision


def _ratio_bbox(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _BBOX_FIELDS:
        raise FigureRecropError("bbox must contain only unit, origin, left, top, right, bottom")
    if value.get("unit") != "ratio" or value.get("origin") != "top-left":
        raise FigureRecropError("bbox must use ratio units with top-left origin")
    coordinates: dict[str, float] = {}
    for name in ("left", "top", "right", "bottom"):
        raw = value.get(name)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise FigureRecropError(f"bbox.{name} must be a finite number")
        number = float(raw)
        if not math.isfinite(number) or number < 0 or number > 1:
            raise FigureRecropError(f"bbox.{name} must be between 0 and 1")
        coordinates[name] = 0.0 if number == 0 else number
    if coordinates["right"] <= coordinates["left"]:
        raise FigureRecropError("bbox.right must be greater than bbox.left")
    if coordinates["bottom"] <= coordinates["top"]:
        raise FigureRecropError("bbox.bottom must be greater than bbox.top")
    return {"unit": "ratio", "origin": "top-left", **coordinates}


def _assets(document: Mapping[str, Any]) -> tuple[list[Any], dict[str, Mapping[str, Any]]]:
    resources = document.get("resources")
    assets = resources.get("assets") if isinstance(resources, Mapping) else None
    if not isinstance(assets, list):
        raise FigureRecropError("document.resources.assets must be an array")
    by_id: dict[str, Mapping[str, Any]] = {}
    for asset in assets:
        asset_id = asset.get("asset_id") if isinstance(asset, Mapping) else None
        if not isinstance(asset_id, str):
            raise FigureRecropError("every asset must have an asset_id")
        if asset_id in by_id:
            raise FigureRecropError(f"duplicate asset_id: {asset_id}")
        by_id[asset_id] = asset
    return assets, by_id


def _figure(document: Mapping[str, Any], block_id: str) -> tuple[str, Mapping[str, Any]]:
    content = document.get("content")
    if not isinstance(content, Mapping):
        raise FigureRecropError("document.content must be an object")

    def walk(value: Any):
        if isinstance(value, Mapping):
            if value.get("type") == "figure" and value.get("block_id") == block_id:
                yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    matches: list[tuple[str, Mapping[str, Any]]] = []
    for content_section in ("question", "answer", "explanation"):
        section = content.get(content_section)
        if section is None:
            continue
        asset_section = "question" if content_section == "answer" else content_section
        matches.extend((asset_section, block) for block in walk(section))
    if not matches:
        raise ModelNotFound(f"figure block not found: {block_id}")
    if len(matches) != 1:
        raise FigureRecropError(f"duplicate figure block_id: {block_id}")
    return matches[0]


def _safe_asset_path(item_dir: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise FigureRecropError("asset path must be a POSIX relative path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or ":" in relative:
        raise FigureRecropError("asset path must stay inside the item directory")
    resolved = (item_dir / Path(*pure.parts)).resolve()
    try:
        resolved.relative_to(item_dir.resolve())
    except ValueError as exc:
        raise FigureRecropError("asset path must stay inside the item directory") from exc
    return resolved


def _verified_source(
    item_path: Path,
    source: Mapping[str, Any],
):
    if source.get("role") not in {"source_crop", "source_page"}:
        raise FigureRecropError("figure source must be a source_crop or source_page asset")
    if source.get("state") != "ready":
        raise FigureRecropError("figure source asset must be ready")
    media_type = source.get("media_type")
    if media_type not in {"image/png", "image/jpeg", "image/bmp"}:
        raise FigureRecropError("figure source has an unsupported image format")
    try:
        image = inspect_image(
            _safe_asset_path(item_path.parent, source.get("path")),
            expected_media_type=media_type,
        )
    except ImageValidationError as exc:
        raise FigureRecropError(str(exc)) from exc
    for field in ("sha256", "width_px", "height_px"):
        if source.get(field) != getattr(image, field):
            raise FigureRecropError(f"figure source {field} metadata does not match its file")
    return image


def _candidate_id(original_id: str, source_sha256: str, bbox: Mapping[str, Any]) -> str:
    payload = json.dumps(
        [original_id, source_sha256, bbox],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"figure-crop-{hashlib.sha256(payload).hexdigest()}"


def _pixel_bbox(bbox: Mapping[str, Any], image) -> tuple[int, int, int, int]:
    pixels = (
        max(0, math.floor(bbox["left"] * image.width_px)),
        max(0, math.floor(bbox["top"] * image.height_px)),
        min(image.width_px, math.ceil(bbox["right"] * image.width_px)),
        min(image.height_px, math.ceil(bbox["bottom"] * image.height_px)),
    )
    if (
        pixels[2] - pixels[0] < MIN_CROP_DIMENSION_PX
        or pixels[3] - pixels[1] < MIN_CROP_DIMENSION_PX
    ):
        raise FigureRecropError("bbox is smaller than one source pixel")
    return pixels


def _same_candidate(
    item_path: Path,
    asset: Mapping[str, Any],
    *,
    asset_id: str,
    original_id: str,
    source_id: str,
    section: str,
    bbox: Mapping[str, Any],
    source_image,
) -> bool:
    generation = asset.get("generation")
    expected = (
        asset.get("asset_id") == asset_id
        and asset.get("section") == section
        and asset.get("role") == "figure_regenerated"
        and asset.get("state") == "ready"
        and asset.get("path") == f"assets/{asset_id}.png"
        and asset.get("media_type") == "image/png"
        and asset.get("derived_from_asset_id") == original_id
        and asset.get("source_region") == {"source_asset_id": source_id, "bbox": bbox}
        and isinstance(generation, Mapping)
        and generation.get("mode") == "crop"
        and generation.get("provider") is None
        and generation.get("model") is None
        and generation.get("prompt_text") is None
        and generation.get("prompt_sha256") is None
        and generation.get("fallback_asset_id") == original_id
        and generation.get("status") == "succeeded"
        and generation.get("prompt_version") is None
        and generation.get("instruction") is None
        and generation.get("remove_blue_text") is False
        and generation.get("failure_reason") is None
        and generation.get("job_id") is None
        and isinstance(generation.get("started_at"), str)
        and generation.get("completed_at") == generation.get("started_at")
    )
    if not expected:
        return False
    try:
        image = inspect_image(
            _safe_asset_path(item_path.parent, asset.get("path")),
            expected_media_type="image/png",
        )
    except (FigureRecropError, ImageValidationError):
        return False
    if not all(
        asset.get(field) == getattr(image, field)
        for field in ("sha256", "width_px", "height_px")
    ):
        return False
    try:
        from PIL import Image, ImageChops

        with Image.open(source_image.path) as source_data, Image.open(image.path) as existing_data:
            expected_crop = source_data.crop(_pixel_bbox(bbox, source_image)).convert("RGBA")
            existing_crop = existing_data.convert("RGBA")
            difference = ImageChops.difference(expected_crop, existing_crop)
            return expected_crop.size == existing_crop.size and not any(
                band.getbbox() for band in difference.split()
            )
    except (OSError, ValueError):
        return False


def recrop_figure_cas(
    item_path: Path | str,
    expected_revision: int,
    block_id: str,
    bbox: Mapping[str, Any],
) -> tuple[dict[str, Any], str, bool]:
    """Create an unselected crop candidate and return (package, asset_id, created)."""

    item_path = Path(item_path).resolve()
    validate_id(block_id, field="block_id")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise FigureRecropError("expected_revision must be an integer")
    if expected_revision < 1:
        raise FigureRecropError("expected_revision must be positive")
    requested_bbox = _ratio_bbox(bbox)
    current = load_json_object(item_path)
    current_revision = _workflow_revision(current)
    if current_revision != expected_revision:
        raise RevisionConflict(expected_revision, current_revision)

    section, block = _figure(current, block_id)
    _, by_id = _assets(current)
    original_id = block.get("original_asset_id")
    original = by_id.get(original_id) if isinstance(original_id, str) else None
    if original is None:
        raise ModelNotFound("figure original asset not found")
    if original.get("role") != "figure_original":
        raise FigureRecropError("figure original_asset_id must identify a figure_original asset")
    if original.get("section") not in {section, "shared"}:
        raise FigureRecropError("figure original asset belongs to a different section")
    region = original.get("source_region")
    source_id = region.get("source_asset_id") if isinstance(region, Mapping) else None
    if not isinstance(source_id, str):
        raise FigureRecropError("figure original asset must identify its source asset")
    if original.get("derived_from_asset_id") != source_id:
        raise FigureRecropError("figure original asset source lineage is inconsistent")
    source = by_id.get(source_id)
    if source is None:
        raise ModelNotFound(f"figure source asset not found: {source_id}")
    if source.get("section") not in {section, "shared"}:
        raise FigureRecropError("figure source asset belongs to a different section")
    source_image = _verified_source(item_path, source)

    asset_id = _candidate_id(original_id, source_image.sha256, requested_bbox)
    existing = by_id.get(asset_id)
    if existing is not None:
        if not _same_candidate(
            item_path,
            existing,
            asset_id=asset_id,
            original_id=original_id,
            source_id=source_id,
            section=section,
            bbox=requested_bbox,
            source_image=source_image,
        ):
            raise FigureRecropError(f"figure crop asset ID collision: {asset_id}")
        _verified_source(item_path, source)
        return current, asset_id, False

    pixels = _pixel_bbox(requested_bbox, source_image)

    item_dir = item_path.parent.resolve()
    unresolved_output_dir = item_dir / "assets"
    unresolved_output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = unresolved_output_dir.resolve()
    try:
        output_dir.relative_to(item_dir)
    except ValueError as exc:
        raise FigureRecropError("figure crop output directory escapes the item") from exc
    relative = f"assets/{asset_id}.png"
    destination = output_dir / f"{asset_id}.png"
    temporary = output_dir / f".{asset_id}.{uuid.uuid4().hex}.tmp.png"
    created_destination = False
    try:
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - part of the app runtime
            raise FigureRecropError("Pillow is required to crop figures") from exc
        try:
            with Image.open(source_image.path) as source_image_data:
                source_image_data.load()
                source_image_data.crop(pixels).save(temporary, format="PNG")
        except (OSError, ValueError) as exc:
            raise FigureRecropError("figure crop could not be written safely") from exc
        try:
            crop_image = inspect_image(temporary, expected_media_type="image/png")
        except ImageValidationError as exc:
            raise FigureRecropError(str(exc)) from exc
        _verified_source(item_path, source)

        completed_at = utc_now()
        record = {
            "asset_id": asset_id,
            "section": section,
            "role": "figure_regenerated",
            "state": "ready",
            "path": relative,
            "media_type": "image/png",
            "sha256": crop_image.sha256,
            "width_px": crop_image.width_px,
            "height_px": crop_image.height_px,
            "source_region": {"source_asset_id": source_id, "bbox": requested_bbox},
            "derived_from_asset_id": original_id,
            "generation": {
                "mode": "crop",
                "provider": None,
                "model": None,
                "prompt_version": None,
                "instruction": None,
                "remove_blue_text": False,
                "status": "succeeded",
                "fallback_asset_id": original_id,
                "failure_reason": None,
                "job_id": None,
                "started_at": completed_at,
                "completed_at": completed_at,
                "prompt_text": None,
                "prompt_sha256": None,
            },
            "review": {
                "status": "unreviewed",
                "boundary_uncertain": False,
                "notes": [],
            },
        }
        candidate = copy.deepcopy(current)
        candidate_assets, _ = _assets(candidate)
        candidate_assets.append(record)
        review = candidate.get("review")
        if isinstance(review, dict):
            review["assets"] = "unreviewed"

        try:
            os.link(temporary, destination)
            created_destination = True
        except FileExistsError:
            if destination.is_symlink() or sha256_file(destination) != crop_image.sha256:
                raise FigureRecropError(f"figure crop file collision: {destination.name}")
        saved = save_json_cas(item_path, candidate, expected_revision)
        return saved, asset_id, True
    except Exception:
        if created_destination:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
