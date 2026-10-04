from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path
import threading
from typing import Any
from urllib.parse import unquote

from .exam_cleaning import clean_exam_page, clean_selected_exam_pages, load_cleaned_page
from .exam_detection import load_exam_proposals, propose_exam_page
from .exam_publication import load_exam_publication, publish_confirmed_exam_regions
from .exam_region import (
    MAX_SOURCE_BYTES,
    ExamDraftConflict,
    ExamDraftError,
    PageChoice,
    create_exam_draft,
    load_exam_draft,
    select_exam_pages,
)
from .exam_region_cleaning import (
    adopt_region_crop,
    clean_region_crop,
    load_region_crop,
    restore_region_crop,
)
from .exam_selection import (
    edit_exam_selection,
    load_exam_selection,
    no_clean_source,
    original_exam_page,
    skip_exam_cleaning,
    use_cleaned_exam_page,
)


_WORKERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="exam-region")
_ACTIVE: set[tuple[str, str, int]] = set()
_ACTIVE_LOCK = threading.Lock()


def _submit(kind: str, root: Path, draft_id: str, page_number: int, action: Any) -> str:
    key = (kind, draft_id, page_number)
    with _ACTIVE_LOCK:
        if key in _ACTIVE:
            return "already_running"
        _ACTIVE.add(key)
    try:
        future = _WORKERS.submit(action, root, draft_id, page_number)
    except RuntimeError:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(key)
        raise
    def release(_future: Any) -> None:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(key)
    future.add_done_callback(release)
    return "queued"


def _draft_json(draft: Any) -> dict[str, Any]:
    with _ACTIVE_LOCK:
        cleaning = ("clean-all", draft.draft_id, 0) in _ACTIVE
    return {"draft_id": draft.draft_id, "source_sha256": draft.source_sha256,
            "source_kind": draft.source_kind, "name": draft.original_name,
            "page_count": draft.page_count, "revision": draft.revision,
            "cleaning_all": cleaning,
            "selected_pages": [{"number": p.number, "rotation": p.rotation} for p in draft.selected_pages]}


def _clean_json(page: Any) -> dict[str, Any]:
    return {"page_number": page.page_number, "status": page.status, "revision": page.revision,
            "selection_revision": page.selection_revision, "source_sha256": page.source_sha256,
            "cleaned_sha256": page.cleaned_sha256, "error_code": page.error_code}


def _proposal_json(page: Any) -> dict[str, Any]:
    return {"page_number": page.page_number, "status": page.status, "revision": page.revision,
            "selection_revision": page.selection_revision, "source_mode": page.source_mode,
            "input_revision": page.cleaned_revision, "input_sha256": page.cleaned_sha256,
            "suggestions": page.suggestions,
            "error_code": page.error_code}


def _selection_json(page: Any) -> dict[str, Any]:
    return {"page_number": page.page_number, "status": page.status, "revision": page.revision,
            "source_mode": page.source_mode, "source_sha256": page.source_sha256,
            "source_revision": page.source_revision, "proposal_revision": page.proposal_revision,
            "regions": page.regions}


def _region_crop_json(crop: Any) -> dict[str, Any]:
    return {"status": crop.status, "revision": crop.revision,
            "base_sha256": crop.base_sha256, "active_sha256": crop.active_sha256,
            "adopted_revision": crop.adopted_revision, "previous_revision": crop.previous_revision,
            "candidates": [{"revision": item["revision"], "sha256": item["sha256"],
                            "base_sha256": item["base_sha256"], "area": item["area"]}
                           for item in crop.candidates], "error_code": crop.error_code}


def _upload(handler: Any, root: Path) -> dict[str, Any]:
    from .server import ApiError
    if handler.headers.get("Transfer-Encoding"):
        handler.close_connection = True
        raise ApiError(HTTPStatus.LENGTH_REQUIRED, "Content-Length가 필요합니다.")
    raw = handler.headers.get("Content-Length")
    try:
        length = int(raw) if raw is not None else -1
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Content-Length가 올바르지 않습니다.") from exc
    if not 1 <= length <= MAX_SOURCE_BYTES:
        handler.close_connection = True
        raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "시험지 업로드 크기가 올바르지 않습니다.")
    name_header = handler.headers.get("X-Upload-Name", "")
    if len(name_header) > 1024 or "\x00" in name_header:
        handler.close_connection = True
        raise ApiError(HTTPStatus.BAD_REQUEST, "시험지 파일명이 올바르지 않습니다.")
    try:
        name = unquote(name_header, encoding="utf-8", errors="strict")
    except (UnicodeError, ValueError) as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "시험지 파일명이 올바르지 않습니다.") from exc
    media = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if media not in ("application/pdf", "image/png", "image/jpeg", "image/webp"):
        handler.close_connection = True
        raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "PDF, PNG, JPEG, WEBP만 허용됩니다.")
    suffix = Path(name.replace("\\", "/")).suffix.lower()
    if (media, suffix) not in {("application/pdf", ".pdf"), ("image/png", ".png"),
                               ("image/jpeg", ".jpg"), ("image/jpeg", ".jpeg"),
                               ("image/webp", ".webp")}:
        handler.close_connection = True
        raise ApiError(HTTPStatus.BAD_REQUEST, "파일명과 Content-Type이 일치하지 않습니다.")
    draft = create_exam_draft(root, handler.rfile, content_length=length, original_name=name)
    _submit("clean-all", root, draft.draft_id, 0,
            lambda workspace, selected_draft, _page: clean_selected_exam_pages(workspace, selected_draft))
    return _draft_json(draft)


def handle_exam_api(handler: Any, method: str, parts: list[str]) -> None:
    """Dispatch authenticated exam routes outside the global trash lock."""
    from .server import ApiError
    root = handler.app.config.workspace_root
    try:
        if parts == ["api", "exam", "drafts"]:
            if method == "POST":
                handler._send_json(HTTPStatus.CREATED, _upload(handler, root))
                return
            if method == "GET":
                base = root / ".exam-region"
                drafts = []
                if base.is_dir() and not base.is_symlink():
                    for directory in base.iterdir():
                        if directory.is_dir() and directory.name.startswith("exam-"):
                            try:
                                drafts.append(_draft_json(load_exam_draft(root, directory.name)))
                            except ValueError:
                                continue
                handler._send_json(HTTPStatus.OK, {"drafts": drafts})
                return
        if len(parts) < 4 or parts[:3] != ["api", "exam", "drafts"]:
            raise ApiError(HTTPStatus.NOT_FOUND, "시험지 API 경로를 찾을 수 없습니다.")
        draft_id = parts[3]
        if len(parts) == 4 and method == "GET":
            handler._send_json(HTTPStatus.OK, _draft_json(load_exam_draft(root, draft_id)))
            return
        if len(parts) == 5 and parts[4] == "clean" and method == "POST":
            draft = load_exam_draft(root, draft_id)
            pending = any(load_cleaned_page(root, draft_id, page.number).status in ("pending", "running")
                          for page in draft.selected_pages)
            status = (_submit("clean-all", root, draft_id, 0,
                              lambda workspace, selected_draft, _page: clean_selected_exam_pages(workspace, selected_draft))
                      if pending else "up_to_date")
            handler._send_json(HTTPStatus.ACCEPTED, {"queue_status": status, **_draft_json(draft)})
            return
        if len(parts) == 5 and parts[4] == "publication":
            if method == "GET":
                handler._send_json(HTTPStatus.OK, load_exam_publication(root, draft_id))
                return
            if method == "POST":
                current = load_exam_publication(root, draft_id)
                if not current["items"]:
                    raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "확정된 영역이 없습니다.")
                status = _submit("publication", root, draft_id, 0,
                                 lambda workspace, selected_draft, _page: publish_confirmed_exam_regions(workspace, selected_draft))
                handler._send_json(HTTPStatus.ACCEPTED, {"status": status, **current})
                return
        if len(parts) == 5 and parts[4] == "pages" and method == "PATCH":
            body = handler._read_json()
            pages = body.get("pages")
            if not isinstance(pages, list) or not all(isinstance(p, dict) for p in pages):
                raise ApiError(HTTPStatus.BAD_REQUEST, "페이지 목록이 올바르지 않습니다.")
            selected = [PageChoice(p.get("number"), p.get("rotation", 0)) for p in pages]
            handler._send_json(HTTPStatus.OK, _draft_json(select_exam_pages(
                root, draft_id, selected, expected_revision=body.get("expected_revision"))))
            return
        if len(parts) < 6 or parts[4] != "pages" or not parts[5].isascii() or not parts[5].isdecimal():
            raise ApiError(HTTPStatus.NOT_FOUND, "시험지 페이지 경로를 찾을 수 없습니다.")
        page_number = int(parts[5])
        tail = parts[6:]
        if len(tail) >= 3 and tail[0] == "regions" and tail[2] == "crop":
            region_id = tail[1]
            crop = load_region_crop(root, draft_id, page_number, region_id)
            if method == "GET" and len(tail) == 3:
                handler._send_file(crop.base_path, "image/png", cache="no-store",
                                   expected_sha256=crop.base_sha256)
                return
            if method == "GET" and tail[3:] == ["active"]:
                handler._send_file(crop.active_path, "image/png", cache="no-store",
                                   expected_sha256=crop.active_sha256)
                return
            if method == "GET" and tail[3:] == ["clean"]:
                handler._send_json(HTTPStatus.OK, _region_crop_json(crop))
                return
            if method == "GET" and len(tail) == 5 and tail[3] == "candidates" and tail[4].isascii() and tail[4].isdecimal():
                candidate = next((item for item in crop.candidates if item["revision"] == int(tail[4])), None)
                if candidate is None:
                    raise ApiError(HTTPStatus.NOT_FOUND, "정리 후보가 없습니다.")
                from .exam_region_cleaning import _image_path
                path = _image_path(crop.base_path, candidate["name"], candidate["sha256"])
                handler._send_file(path, "image/png", cache="no-store", expected_sha256=candidate["sha256"])
                return
            if method == "POST" and tail[3:] == ["clean"]:
                body = handler._read_json()
                if type(body.get("expected_revision")) is not int or body["expected_revision"] != crop.revision:
                    raise ExamDraftConflict("stale crop cleaning revision")
                area = body.get("area")
                if area is not None and (not isinstance(area, list) or len(area) != 4):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "정리 영역 좌표가 올바르지 않습니다.")
                status = _submit(f"region-clean:{region_id}", root, draft_id, page_number,
                                 lambda workspace, selected_draft, selected_page: clean_region_crop(
                                     workspace, selected_draft, selected_page, region_id,
                                     expected_revision=body.get("expected_revision"),
                                     area=tuple(area) if area is not None else None))
                handler._send_json(HTTPStatus.ACCEPTED, {**_region_crop_json(crop), "queue_status": status})
                return
            if method == "POST" and tail[3:] == ["adopt"]:
                body = handler._read_json()
                if type(body.get("candidate_revision")) is not int:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "정리 후보 번호가 올바르지 않습니다.")
                updated = adopt_region_crop(root, draft_id, page_number, region_id,
                                            expected_revision=body.get("expected_revision"),
                                            candidate_revision=body.get("candidate_revision"))
                handler._send_json(HTTPStatus.OK, _region_crop_json(updated))
                return
            if method == "POST" and tail[3:] == ["restore"]:
                body = handler._read_json()
                if body.get("candidate_revision") is not None and type(body["candidate_revision"]) is not int:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "정리 후보 번호가 올바르지 않습니다.")
                updated = restore_region_crop(root, draft_id, page_number, region_id,
                                              expected_revision=body.get("expected_revision"),
                                              candidate_revision=body.get("candidate_revision"))
                handler._send_json(HTTPStatus.OK, _region_crop_json(updated))
                return
        if method == "GET" and tail == ["original"]:
            path, sha, _ = original_exam_page(root, draft_id, page_number)
            handler._send_file(path, "image/png", cache="no-store", expected_sha256=sha)
            return
        if method == "GET" and tail == ["cleaned"]:
            cleaned = load_cleaned_page(root, draft_id, page_number)
            if cleaned.status != "ready":
                raise ApiError(HTTPStatus.CONFLICT, "정리된 페이지가 준비되지 않았습니다.")
            handler._send_file(cleaned.cleaned_path, "image/png", cache="no-store",
                               expected_sha256=cleaned.cleaned_sha256)
            return
        if tail == ["clean"]:
            if method == "POST":
                load_cleaned_page(root, draft_id, page_number)
                status = _submit("clean", root, draft_id, page_number, clean_exam_page)
                handler._send_json(HTTPStatus.ACCEPTED, {"status": status, "page_number": page_number})
                return
            if method == "GET":
                bypass = no_clean_source(root, draft_id, page_number)
                if bypass is not None:
                    underlying = load_cleaned_page(root, draft_id, page_number)
                    handler._send_json(HTTPStatus.OK, {"page_number": page_number, "status": "skipped",
                                                       "source_mode": "original_unprocessed",
                                                       "input_sha256": bypass[1], "input_revision": bypass[2],
                                                       "cleaning_status": underlying.status,
                                                       "cleaning_error_code": underlying.error_code})
                    return
                handler._send_json(HTTPStatus.OK, _clean_json(load_cleaned_page(root, draft_id, page_number)))
                return
        if method == "POST" and tail == ["skip-clean"]:
            body = handler._read_json()
            selected = skip_exam_cleaning(root, draft_id, page_number,
                                          expected_revision=body.get("expected_revision"))
            handler._send_json(HTTPStatus.OK, _selection_json(selected))
            return
        if method == "POST" and tail == ["use-cleaned"]:
            body = handler._read_json()
            selected = use_cleaned_exam_page(root, draft_id, page_number,
                                             expected_revision=body.get("expected_revision"))
            handler._send_json(HTTPStatus.OK, _selection_json(selected))
            return
        if tail == ["proposals"]:
            if method == "POST":
                load_exam_proposals(root, draft_id, page_number)
                status = _submit("detect", root, draft_id, page_number, propose_exam_page)
                handler._send_json(HTTPStatus.ACCEPTED, {"status": status, "page_number": page_number})
                return
            if method == "GET":
                handler._send_json(HTTPStatus.OK, _proposal_json(load_exam_proposals(root, draft_id, page_number)))
                return
        if tail == ["selection"]:
            if method == "GET":
                handler._send_json(HTTPStatus.OK, _selection_json(load_exam_selection(root, draft_id, page_number)))
                return
            if method == "PATCH":
                body = handler._read_json()
                selected = edit_exam_selection(root, draft_id, page_number,
                                               expected_revision=body.get("expected_revision"),
                                               operation=body.get("operation"))
                handler._send_json(HTTPStatus.OK, _selection_json(selected))
                return
        raise ApiError(HTTPStatus.NOT_FOUND, "시험지 API 경로를 찾을 수 없습니다.")
    except ExamDraftConflict as exc:
        handler._send_json(HTTPStatus.CONFLICT, {"error": "exam_revision_conflict", "message": str(exc)})
    except ExamDraftError as exc:
        handler._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "exam_invalid", "message": str(exc)})
