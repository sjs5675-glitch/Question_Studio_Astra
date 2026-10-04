from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pypdfium2 as pdfium
from PIL import Image, ImageChops, UnidentifiedImageError

from vendor.pnkmath.image_cleaner import ImageCleanerError, ImageProvider, process_question
from vendor.pnkmath.image_provider_adapter import CodexCliImageProvider

from .exam_region import (
    _LOCK as _DRAFT_LOCK,
    ExamDraftConflict,
    ExamDraftError,
    PageChoice,
    load_exam_draft,
    render_exam_page,
)
from .model import sha256_file


TILE_SIDE = 1536
PAGE_SIDE = 3072
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "clean_exam_tile.md"
_Status = Literal["pending", "running", "ready", "failed"]


class ExamCleaningError(ExamDraftError):
    pass


class ExamCleaningConflict(ExamDraftConflict):
    pass


@dataclass(frozen=True, slots=True)
class CleanedPage:
    page_number: int
    status: _Status
    revision: int
    selection_revision: int
    rotation: int
    source_sha256: str
    cleaned_path: Path | None
    cleaned_sha256: str | None
    error_code: str | None


def _state_path(source_path: Path) -> Path:
    return source_path.parent / "cleaning.json"


def _read_state(path: Path) -> dict[str, dict[str, str | int | None]]:
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_size > 256_000:
        raise ExamCleaningError("cleaning state is unsafe")
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExamCleaningError("cannot read cleaning state") from exc
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, dict) for k, v in value.items()):
        raise ExamCleaningError("invalid cleaning state")
    return value


def _write_state(path: Path, state: dict[str, dict[str, str | int | None]]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _choice(draft_pages: tuple[PageChoice, ...], page_number: int) -> PageChoice:
    for choice in draft_pages:
        if choice.number == page_number:
            return choice
    raise ExamCleaningError("page is not selected")


def load_cleaned_page(workspace_root: Path | str, draft_id: str, page_number: int) -> CleanedPage:
    """Return a ready result only when its selection, source, and file hash remain current."""
    draft = load_exam_draft(workspace_root, draft_id)
    choice = _choice(draft.selected_pages, page_number)
    record = _read_state(_state_path(draft.source_path)).get(str(page_number))
    if record is None:
        return CleanedPage(page_number, "pending", 0, draft.revision, choice.rotation,
                           draft.source_sha256, None, None, None)
    try:
        revision = record["revision"]
        status = record["status"]
        selection_revision = record["selection_revision"]
        rotation = record["rotation"]
        source_sha = record["source_sha256"]
        name = record.get("cleaned_name")
        image_sha = record.get("cleaned_sha256")
        error = record.get("error_code")
    except KeyError as exc:
        raise ExamCleaningError("incomplete cleaning state") from exc
    if (type(revision) is not int or revision < 1 or status not in ("running", "ready", "failed")
            or type(selection_revision) is not int or type(rotation) is not int
            or not isinstance(source_sha, str)):
        raise ExamCleaningError("invalid cleaning state")
    if selection_revision != draft.revision or rotation != choice.rotation or source_sha != draft.source_sha256:
        return CleanedPage(page_number, "pending", revision, draft.revision, choice.rotation,
                           draft.source_sha256, None, None, None)
    path = None
    if status == "ready":
        expected_name = f"clean-page-{page_number:04d}-r{revision}.png"
        path = draft.source_path.parent / expected_name
        if (name != expected_name or path.is_symlink() or not path.is_file()
                or not isinstance(image_sha, str) or sha256_file(path) != image_sha):
            raise ExamCleaningError("cleaned image is missing or changed")
    return CleanedPage(page_number, status, revision, selection_revision, rotation,
                       source_sha, path, image_sha if isinstance(image_sha, str) else None,
                       error if isinstance(error, str) else None)


def _claim(workspace_root: Path | str, draft_id: str, page_number: int) -> tuple[PageChoice, int, int, str]:
    with _DRAFT_LOCK:
        draft = load_exam_draft(workspace_root, draft_id)
        choice = _choice(draft.selected_pages, page_number)
        path = _state_path(draft.source_path)
        state = _read_state(path)
        old = state.get(str(page_number), {})
        revision = int(old.get("revision", 0)) + 1
        attempt = uuid.uuid4().hex
        state[str(page_number)] = {"status": "running", "revision": revision,
                                    "selection_revision": draft.revision, "rotation": choice.rotation,
                                    "source_sha256": draft.source_sha256, "attempt": attempt,
                                    "cleaned_name": None, "cleaned_sha256": None, "error_code": None}
        _write_state(path, state)
        return choice, draft.revision, revision, attempt


def _finish(
    workspace_root: Path | str, draft_id: str, page_number: int, selection_revision: int,
    revision: int, attempt: str, *, image: Image.Image | None, error_code: str | None,
) -> CleanedPage:
    with _DRAFT_LOCK:
        draft = load_exam_draft(workspace_root, draft_id)
        choice = _choice(draft.selected_pages, page_number)
        path = _state_path(draft.source_path)
        state = _read_state(path)
        old = state.get(str(page_number), {})
        if (draft.revision != selection_revision or old.get("attempt") != attempt
                or old.get("revision") != revision or old.get("status") != "running"
                or old.get("rotation") != choice.rotation):
            raise ExamCleaningConflict("stale cleaning result")
        record = state[str(page_number)]
        if image is not None:
            name = f"clean-page-{page_number:04d}-r{revision}.png"
            destination = draft.source_path.parent / name
            temporary = destination.with_name(f".{name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as output:
                    image.save(output, format="PNG")
                    output.flush()
                    os.fsync(output.fileno())
                temporary.rename(destination)
            finally:
                temporary.unlink(missing_ok=True)
            record.update(status="ready", cleaned_name=name, cleaned_sha256=sha256_file(destination), error_code=None)
        else:
            record.update(status="failed", cleaned_name=None, cleaned_sha256=None, error_code=error_code)
        record.pop("attempt", None)
        _write_state(path, state)
    return load_cleaned_page(workspace_root, draft_id, page_number)


def _tile_shape(width: int, height: int) -> tuple[int, int, str]:
    ratios = {"16:9": 16 / 9, "4:3": 4 / 3, "1:1": 1, "3:4": 3 / 4, "9:16": 9 / 16}
    aspect = min(ratios, key=lambda name: abs(math.log((width / height) / ratios[name])))
    tile_width = min(width, TILE_SIDE)
    tile_height = round(tile_width / ratios[aspect])
    if tile_height > min(height, TILE_SIDE):
        tile_height = min(height, TILE_SIDE)
        tile_width = round(tile_height * ratios[aspect])
    return max(1, tile_width), max(1, tile_height), aspect


def _tile_starts(length: int, tile_length: int) -> list[int]:
    count = math.ceil(length / tile_length)
    return [round(index * (length - tile_length) / (count - 1)) for index in range(count)] if count > 1 else [0]


def _paste_bounds(starts: list[int], tile_length: int, length: int, index: int) -> tuple[int, int]:
    left = 0 if index == 0 else (starts[index - 1] + tile_length + starts[index]) // 2
    right = length if index == len(starts) - 1 else (starts[index] + tile_length + starts[index + 1]) // 2
    return left, right


def _clean_tile(tile: Image.Image, tile_path: Path, adapter: ImageProvider,
                prompt: str, aspect_ratio: str) -> Image.Image:
    tile.save(tile_path, format="PNG")
    output_path = tile_path.with_name(f"clean-attempt-{tile_path.stem}.png")
    try:
        process_question(adapter, tile_path, output_path, aspect_ratio, prompt)
    except ImageCleanerError:
        raise
    except OSError as exc:
        errno = exc.errno if exc.errno is not None else "unknown"
        raise ExamCleaningError(f"provider_io_{type(exc).__name__}_{errno}") from exc
    except ValueError as exc:
        raise ExamCleaningError("provider_value_error") from exc
    try:
        with Image.open(output_path) as opened:
            opened.load()
            cleaned = opened.convert("RGB")
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ExamCleaningError("invalid_cleaned_image") from exc
    iw, ih = tile.size
    ow, oh = cleaned.size
    if (ow < max(1, iw // 2) or oh < max(1, ih // 2) or ow > 4096 or oh > 4096
            or not math.isclose(ow / oh, iw / ih, rel_tol=0.01)):
        raise ExamCleaningError("cleaned_dimensions_changed")
    cleaned = cleaned.resize(tile.size, Image.Resampling.LANCZOS) if cleaned.size != tile.size else cleaned
    white = Image.new("RGB", tile.size, "white")
    if ImageChops.difference(tile, white).getbbox() and not ImageChops.difference(cleaned, white).getbbox():
        raise ExamCleaningError("cleaned_image_blank")
    return cleaned


def _page_canvas(original: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int], str]:
    scale = min(1.0, PAGE_SIDE / max(original.size))
    width, height = (max(1, round(side * scale)) for side in original.size)
    _, _, aspect = _tile_shape(width, height)
    numerator, denominator = (int(part) for part in aspect.split(":"))
    canvas_width = max(width, math.ceil(height * numerator / denominator))
    canvas_height = max(height, math.ceil(width * denominator / numerator))
    left, top = (canvas_width - width) // 2, (canvas_height - height) // 2
    canvas = Image.new("RGB", (canvas_width, canvas_height), "white")
    canvas.paste(original.resize((width, height), Image.Resampling.LANCZOS), (left, top))
    return canvas, (left, top, left + width, top + height), aspect


def clean_exam_page(
    workspace_root: Path | str, draft_id: str, page_number: int, *,
    adapter: ImageProvider | None = None,
) -> CleanedPage:
    """Retry one page; publish only a complete, checked image for the current selection."""
    choice, selection_revision, revision, attempt = _claim(workspace_root, draft_id, page_number)
    try:
        draft = load_exam_draft(workspace_root, draft_id)
        prompt = PROMPT_PATH.read_text("utf-8")
        if not prompt.strip():
            raise ExamCleaningError("cleaning prompt is empty")
        original = render_exam_page(draft, choice, scale=3.0).convert("RGB")
        if original.width * original.height > 80_000_000:
            raise ExamCleaningError("rendered_page_too_large")
        provider = adapter or CodexCliImageProvider()
        if 0.45 <= original.width / original.height <= 2.2:
            canvas, bounds, aspect = _page_canvas(original)
            tile_path = draft.source_path.parent / f"tile-{page_number:04d}-r{revision}-0-0.png"
            try:
                cleaned = _clean_tile(canvas, tile_path, provider, prompt, aspect)
            finally:
                tile_path.unlink(missing_ok=True)
            assembled = cleaned.crop(bounds).resize(original.size, Image.Resampling.LANCZOS)
        else:
            # ponytail: Extreme strips need tiles until the image provider supports a taller readable page.
            assembled = Image.new("RGB", original.size)
            tile_width, tile_height, aspect = _tile_shape(original.width, original.height)
            lefts = _tile_starts(original.width, tile_width)
            tops = _tile_starts(original.height, tile_height)
            for row, top in enumerate(tops):
                for column, left in enumerate(lefts):
                    tile = original.crop((left, top, left + tile_width, top + tile_height))
                    tile_path = draft.source_path.parent / f"tile-{page_number:04d}-r{revision}-{row}-{column}.png"
                    try:
                        cleaned = _clean_tile(tile, tile_path, provider, prompt, aspect)
                    finally:
                        tile_path.unlink(missing_ok=True)
                    x0, x1 = _paste_bounds(lefts, tile_width, original.width, column)
                    y0, y1 = _paste_bounds(tops, tile_height, original.height, row)
                    assembled.paste(cleaned.crop((x0 - left, y0 - top, x1 - left, y1 - top)), (x0, y0))
        return _finish(workspace_root, draft_id, page_number, selection_revision,
                       revision, attempt, image=assembled, error_code=None)
    except FileNotFoundError:
        code = "codex_cli_unavailable"
    except ImageCleanerError as exc:
        code = next((known for known in ("codex_cli_timeout", "codex_cli_launch_failed",
                                          "codex_cli_failed", "codex_cli_no_image",
                                          "codex_cli_invalid_image") if known in str(exc)),
                    "provider_response_invalid")
    except ExamCleaningError as exc:
        code = str(exc).replace(" ", "_")
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, pdfium.PdfiumError):
        code = "page_image_failed"
    return _finish(workspace_root, draft_id, page_number, selection_revision,
                   revision, attempt, image=None, error_code=code)


def clean_selected_exam_pages(
    workspace_root: Path | str, draft_id: str, *, adapter: ImageProvider | None = None,
) -> tuple[CleanedPage, ...]:
    draft = load_exam_draft(workspace_root, draft_id)
    return tuple(clean_exam_page(workspace_root, draft_id, choice.number, adapter=adapter)
                 for choice in draft.selected_pages
                 if load_cleaned_page(workspace_root, draft_id, choice.number).status in ("pending", "running"))
