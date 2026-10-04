from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Sequence

import pypdfium2 as pdfium
from PIL import Image, ImageOps, UnidentifiedImageError

from .model import sha256_file


MAX_SOURCE_BYTES = 512 * 1024 * 1024
_DRAFT_ID = re.compile(r"^exam-[0-9a-f]{32}$")
_SUFFIXES = {".pdf": "pdf", ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}
_LOCK = threading.Lock()


def _write_draft(path: Path, data: dict[str, str | int | list[dict[str, int]]]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ExamDraftError(ValueError):
    pass


class ExamDraftConflict(ExamDraftError):
    pass


@dataclass(frozen=True, slots=True)
class PageChoice:
    number: int
    rotation: int = 0


@dataclass(frozen=True, slots=True)
class ExamDraft:
    draft_id: str
    source_path: Path
    source_sha256: str
    source_kind: str
    original_name: str
    page_count: int
    selected_pages: tuple[PageChoice, ...]
    revision: int


def _draft_dir(workspace_root: Path | str, draft_id: str) -> Path:
    if not isinstance(draft_id, str) or not _DRAFT_ID.fullmatch(draft_id):
        raise ExamDraftError("invalid draft ID")
    root = Path(workspace_root).resolve() / ".exam-region"
    if root.is_symlink():
        raise ExamDraftError("exam storage must not be a link")
    return root / draft_id


def _read_draft(workspace_root: Path | str, draft_id: str) -> ExamDraft:
    directory = _draft_dir(workspace_root, draft_id)
    if directory.is_symlink() or not directory.is_dir():
        raise ExamDraftError("draft not found or unsafe")
    metadata = directory / "draft.json"
    if metadata.is_symlink() or not metadata.is_file() or metadata.stat().st_size > 65536:
        raise ExamDraftError("draft metadata missing or unsafe")
    try:
        data = json.loads(metadata.read_text("utf-8"))
        kind = data["source_kind"]
        if kind not in {"pdf", "png", "jpeg", "webp"}:
            raise ExamDraftError("invalid source kind")
        source = directory / f"source.{kind}"
        if source.is_symlink() or not source.is_file():
            raise ExamDraftError("source missing or unsafe")
        selected = tuple(PageChoice(**choice) for choice in data["selected_pages"])
        draft = ExamDraft(draft_id, source.resolve(strict=True), data["source_sha256"], kind,
                          data["original_name"], data["page_count"], selected, data["revision"])
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ExamDraftError("invalid draft metadata") from exc
    if (data.get("draft_id") != draft_id or not re.fullmatch(r"[0-9a-f]{64}", draft.source_sha256)
            or type(draft.page_count) is not int or draft.page_count < 1
            or type(draft.revision) is not int or draft.revision < 1
            or not isinstance(draft.original_name, str)
            or not draft.original_name or len(draft.original_name) > 180
            or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in draft.original_name)
            or not draft.selected_pages or len(draft.selected_pages) > draft.page_count):
        raise ExamDraftError("invalid draft identity or pages")
    _validate_pages(draft.selected_pages, draft.page_count)
    if sha256_file(draft.source_path) != draft.source_sha256:
        raise ExamDraftError("source SHA-256 mismatch")
    return draft


def load_exam_draft(workspace_root: Path | str, draft_id: str) -> ExamDraft:
    """Return a verified draft and its preserved source path."""
    return _read_draft(workspace_root, draft_id)


def _validate_pages(pages: Sequence[PageChoice], page_count: int) -> None:
    if not pages or len(pages) > page_count:
        raise ExamDraftError("select at least one valid page")
    seen: set[int] = set()
    for choice in pages:
        if not isinstance(choice, PageChoice):
            raise ExamDraftError("invalid page choice")
        if (type(choice.number) is not int or choice.number < 1 or choice.number > page_count
                or type(choice.rotation) is not int or choice.rotation not in (0, 90, 180, 270)
                or choice.number in seen):
            raise ExamDraftError("invalid or duplicate page choice")
        seen.add(choice.number)


def create_exam_draft(
    workspace_root: Path | str, stream: BinaryIO, *, content_length: int, original_name: str,
) -> ExamDraft:
    """Stream and verify a PDF or image, then publish its immutable source."""
    if type(content_length) is not int or not 1 <= content_length <= MAX_SOURCE_BYTES:
        raise ExamDraftError("invalid Content-Length")
    name = re.split(r"[\\/]", original_name)[-1] if isinstance(original_name, str) else ""
    suffix = Path(name).suffix.lower()
    kind = _SUFFIXES.get(suffix)
    if kind is None:
        raise ExamDraftError("unsupported source extension")
    name = "".join("_" if ord(c) < 32 or c in '<>:"|?*' else c for c in name).strip(" .")[:180]
    if not name:
        raise ExamDraftError("invalid source filename")
    draft_id = f"exam-{uuid.uuid4().hex}"
    directory = _draft_dir(workspace_root, draft_id)
    staging = directory.parent / f".{draft_id}.tmp"
    if directory.parent.is_symlink():
        raise ExamDraftError("exam storage must not be a link")
    staging.mkdir(parents=True, exist_ok=False)
    source = staging / f"source.{kind}"
    digest = hashlib.sha256()
    try:
        remaining = content_length
        with source.open("xb") as output:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not isinstance(chunk, bytes) or not chunk or len(chunk) > remaining:
                    raise ExamDraftError("upload body does not match Content-Length")
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if kind == "pdf":
            with source.open("rb") as input_file:
                signature = input_file.read(5)
            if signature != b"%PDF-":
                raise ExamDraftError("invalid PDF signature")
            with pdfium.PdfDocument(str(source)) as document:
                page_count = len(document)
        else:
            with Image.open(source) as image:
                if image.format != kind.upper() and not (kind == "jpeg" and image.format == "JPEG"):
                    raise ExamDraftError("image content does not match extension")
                if getattr(image, "n_frames", 1) != 1:
                    raise ExamDraftError("animated images are not supported")
                image.load()
            page_count = 1
        if not 1 <= page_count <= 1000:
            raise ExamDraftError("source page count is outside supported range")
        pages = tuple(PageChoice(n) for n in range(1, page_count + 1))
        data = {"draft_id": draft_id, "source_sha256": digest.hexdigest(), "source_kind": kind,
                "original_name": name, "page_count": page_count,
                "selected_pages": [vars_page(p) for p in pages], "revision": 1}
        _write_draft(staging / "draft.json", data)
        for attempt in range(5):
            try:
                staging.rename(directory)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
        return _read_draft(workspace_root, draft_id)
    except (OSError, pdfium.PdfiumError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ExamDraftError("cannot decode or store exam source") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def vars_page(choice: PageChoice) -> dict[str, int]:
    return {"number": choice.number, "rotation": choice.rotation}


def select_exam_pages(
    workspace_root: Path | str, draft_id: str, pages: Sequence[PageChoice], *, expected_revision: int,
) -> ExamDraft:
    """Compare and swap the page set and orientation; source bytes remain untouched."""
    with _LOCK:
        draft = _read_draft(workspace_root, draft_id)
        if type(expected_revision) is not int or expected_revision != draft.revision:
            raise ExamDraftConflict("stale page selection revision")
        _validate_pages(pages, draft.page_count)
        data = {"draft_id": draft_id, "source_sha256": draft.source_sha256,
                "source_kind": draft.source_kind, "original_name": draft.original_name,
                "page_count": draft.page_count, "selected_pages": [vars_page(p) for p in pages],
                "revision": draft.revision + 1}
        _write_draft(draft.source_path.parent / "draft.json", data)
        return _read_draft(workspace_root, draft_id)


def stable_exam_region_id(draft_id: str, page_number: int, local_key: str) -> str:
    """Stable identity for retries of one user-confirmed region."""
    if (not isinstance(draft_id, str) or not _DRAFT_ID.fullmatch(draft_id)
            or type(page_number) is not int or page_number < 1
            or not isinstance(local_key, str) or not local_key):
        raise ExamDraftError("invalid region identity")
    return f"region-{uuid.uuid5(uuid.NAMESPACE_URL, f'{draft_id}:{page_number}:{local_key}').hex}"


def validate_region_box(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Accept normalized clean-page coordinates as (left, top, right, bottom)."""
    if (len(box) != 4 or any(type(value) not in (int, float) or not math.isfinite(value) for value in box)
            or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)):
        raise ExamDraftError("invalid normalized region box")
    return box


def render_exam_page(draft: ExamDraft, choice: PageChoice, *, scale: float = 2.0) -> Image.Image:
    """Render one original page; rotation is clockwise and never changes the source."""
    _validate_pages((choice,), draft.page_count)
    if type(scale) not in (int, float) or not math.isfinite(scale) or not 0.5 <= scale <= 4:
        raise ExamDraftError("invalid render scale")
    if draft.source_kind == "pdf":
        with pdfium.PdfDocument(str(draft.source_path)) as document:
            page = document.get_page(choice.number - 1)
            try:
                image = page.render(scale=scale).to_pil()
            finally:
                page.close()
    else:
        with Image.open(draft.source_path) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
    return image.rotate(-choice.rotation, expand=True)
