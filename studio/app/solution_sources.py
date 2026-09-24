from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageOps

from .model import atomic_write_json, load_json_object, save_json_cas, sha256_file, solution_content_hash, utc_now
from .transcription import ImageValidationError, inspect_image

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_COMPOSITE_PIXELS = 80_000_000
_MIMES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


class SolutionSourceError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _asset(item_path: Path, asset_id: str) -> dict[str, Any]:
    document = load_json_object(item_path)
    for asset in document.get("resources", {}).get("assets", []):
        if isinstance(asset, dict) and asset.get("asset_id") == asset_id:
            relative = asset.get("path")
            if not isinstance(relative, str) or "\\" in relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
                break
            path = (item_path.parent / relative).resolve()
            if not path.is_relative_to(item_path.parent.resolve()) or not path.is_file() or sha256_file(path) != asset.get("sha256"):
                break
            return asset
    raise SolutionSourceError("이 문항에서 사용할 수 있는 이미지가 없습니다.", 404)


def stage_image(item_path: Path, body: bytes, media_type: str, filename: str = "") -> dict[str, Any]:
    if media_type not in _MIMES:
        raise SolutionSourceError("PNG, JPEG, WEBP 이미지만 사용할 수 있습니다.", 400)
    if not body or len(body) > MAX_UPLOAD_BYTES:
        raise SolutionSourceError("이미지는 25 MiB 이하여야 합니다.", 413)
    if shutil.disk_usage(item_path.parent).free < max(64 * 1024 * 1024, len(body) * 4):
        raise SolutionSourceError("이미지를 저장할 공간이 부족합니다.", 413)
    upload_id = f"solution-upload-{uuid.uuid4().hex}"
    root = item_path.parent / "jobs" / "solution_uploads" / upload_id
    root.mkdir(parents=True, exist_ok=False)
    suffix = _MIMES[media_type]
    original = root / f"original{suffix}"
    original.write_bytes(body)
    try:
        checked = inspect_image(original, expected_media_type=media_type)
        with Image.open(original) as opened:
            normalized = ImageOps.exif_transpose(opened)
            normalized.load()
            normalized = normalized.convert("RGB") if normalized.mode not in {"RGB", "RGBA"} else normalized
            normalized.save(root / "normalized.png", format="PNG")
        normalized_checked = inspect_image(root / "normalized.png", expected_media_type="image/png")
    except (ImageValidationError, OSError, ValueError) as exc:
        if root.resolve().is_relative_to(item_path.parent.resolve()):
            shutil.rmtree(root)
        raise SolutionSourceError("이미지를 안전하게 읽을 수 없습니다.", 422) from exc
    record = {"upload_id": upload_id, "sha256": checked.sha256, "media_type": media_type,
              "width": checked.width_px, "height": checked.height_px, "size": checked.size_bytes,
              "normalized_sha256": normalized_checked.sha256, "normalized_width": normalized_checked.width_px,
              "normalized_height": normalized_checked.height_px, "filename": Path(filename).name[:255]}
    atomic_write_json(root / "upload.json", record)
    return {key: record[key] for key in ("upload_id", "sha256", "media_type", "width", "height", "size")}


def _upload(item_path: Path, upload_id: str) -> tuple[dict[str, Any], Path]:
    if not isinstance(upload_id, str) or not upload_id.startswith("solution-upload-") or len(upload_id) != 48 or not upload_id[16:].isalnum():
        raise SolutionSourceError("업로드 ID가 올바르지 않습니다.", 400)
    root = item_path.parent / "jobs" / "solution_uploads" / upload_id
    record_path = root / "upload.json"
    if not record_path.is_file():
        raise SolutionSourceError("이 문항의 업로드를 찾을 수 없습니다.", 404)
    record = load_json_object(record_path)
    original = root / f"original{_MIMES.get(record.get('media_type'), '.invalid')}"
    normalized = root / "normalized.png"
    if not original.is_file() or sha256_file(original) != record.get("sha256") or not normalized.is_file() or sha256_file(normalized) != record.get("normalized_sha256"):
        raise SolutionSourceError("업로드 파일이 변경되었습니다.", 409)
    return record, normalized


def _write_immutable(item_path: Path, image: Image.Image) -> dict[str, Any]:
    output = io.BytesIO()
    image.save(output, format="PNG")
    data = output.getvalue()
    if len(data) > MAX_UPLOAD_BYTES or image.width * image.height > MAX_COMPOSITE_PIXELS:
        raise SolutionSourceError("합성 이미지가 한도를 넘었습니다. 영역을 나누거나 크기를 조정해 주세요.", 422)
    digest = hashlib.sha256(data).hexdigest()
    asset_id = f"solution-image-{digest[:32]}"
    relative = f"assets/{asset_id}.png"
    target = item_path.parent / relative
    target.parent.mkdir(exist_ok=True)
    try:
        with target.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        if sha256_file(target) != digest:
            raise SolutionSourceError("기존 이미지 자산과 충돌했습니다.", 409)
    return {"asset_id": asset_id, "section": "explanation", "role": "source_crop", "state": "ready",
            "path": relative, "media_type": "image/png", "sha256": digest,
            "width_px": image.width, "height_px": image.height,
            "review": {"status": "unreviewed", "boundary_uncertain": False, "notes": []}}


def _prepare_uploads(item_path: Path, upload_ids: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(upload_ids, list) or not 1 <= len(upload_ids) <= 8 or len(set(upload_ids)) != len(upload_ids):
        raise SolutionSourceError("이미지는 중복 없이 1~8장이어야 합니다.")
    sources = [_upload(item_path, value) for value in upload_ids]
    width = max(record["normalized_width"] for record, _ in sources)
    height = sum(record["normalized_height"] for record, _ in sources)
    if width * height > MAX_COMPOSITE_PIXELS:
        raise SolutionSourceError("합성 이미지가 한도를 넘었습니다. 영역을 나누거나 크기를 조정해 주세요.", 422)
    canvas = Image.new("RGB", (width, height), "white")
    offset = 0
    image_sources = []
    for order, (record, path) in enumerate(sources):
        with Image.open(path) as opened:
            canvas.paste(opened.convert("RGB"), (0, offset))
        offset += record["normalized_height"]
        original_asset = _write_immutable_from_file(item_path, path)
        image_sources.append({"asset_id": original_asset["asset_id"], "sha256": original_asset["sha256"],
                              "width": original_asset["width_px"], "height": original_asset["height_px"], "order": order})
    return _write_immutable(item_path, canvas), image_sources


def _pdf_pages(item_path: Path, link: Mapping[str, Any], import_roots: tuple[Path, ...]) -> list[dict[str, Any]]:
    from .solution_reconciliation import locate_leaf_run

    project_path = item_path.parents[2] / "project.json"
    if not project_path.is_file():
        return []
    leaf = locate_leaf_run(project_path, import_roots)
    if leaf is None:
        return []
    manifest = load_json_object(leaf / "solutions" / "link_manifest.json")
    solution = manifest.get("solution", {})
    source = Path(str(solution.get("source_pdf", ""))).expanduser()
    digest = link.get("document_sha256")
    if not source.is_file() or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or sha256_file(source) != digest:
        return []
    indexes = {part.get("pdf_index") for part in link.get("parts", []) if isinstance(part, dict)}
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(source))
    try:
        indexes = {index for index in indexes if type(index) is int and 0 <= index < len(document)}
        if not indexes:
            return []
        indexes |= {neighbor for index in tuple(indexes) for neighbor in (index - 1, index + 1) if 0 <= neighbor < len(document)}
        pages = []
        for index in sorted(indexes):
            page_id = f"solution-page-{digest}-{index + 1}"
            target = item_path.parent / "jobs" / "solution_pages" / f"{page_id}.png"
            receipt = target.with_suffix(".json")
            if not target.resolve().is_relative_to(item_path.parent.resolve()):
                raise SolutionSourceError("원본 페이지 경로가 올바르지 않습니다.", 409)
            if not target.is_file() or not receipt.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                page = document[index]
                try:
                    bitmap = page.render(scale=200 / 72)
                    try:
                        image = bitmap.to_pil()
                        image.save(target, format="PNG")
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                atomic_write_json(receipt, {"source_sha256": digest, "page": index + 1,
                                            "sha256": sha256_file(target)})
            record = load_json_object(receipt)
            if record.get("source_sha256") != digest or record.get("page") != index + 1 or sha256_file(target) != record.get("sha256"):
                raise SolutionSourceError("원본 페이지가 변경되었습니다.", 409)
            with Image.open(target) as image:
                pages.append({"page_ref": page_id, "width": image.width, "height": image.height,
                              "pdf_page": index + 1, "sha256": record["sha256"]})
        return pages
    finally:
        document.close()


def source_pages(item_path: Path, project_id: str, item_id: str, import_roots: tuple[Path, ...] = ()) -> dict[str, Any]:
    document = load_json_object(item_path)
    selection = document.get("provenance", {}).get("solution_selection")
    selected_id = selection.get("source_asset_id") if isinstance(selection, dict) else None
    links = document.get("provenance", {}).get("solution_links", [])
    selected_link = next((link for link in links if isinstance(link, dict) and link.get("source_asset_id") == selected_id), None)
    allowed: set[str] = set()
    if isinstance(selected_link, dict) and selected_link.get("source_origin") == "uploaded_image":
        allowed.update(entry["asset_id"] for entry in selected_link.get("image_sources", []) if isinstance(entry, dict) and isinstance(entry.get("asset_id"), str))
    elif isinstance(selected_link, dict) and selected_link.get("document_sha256"):
        pages = _pdf_pages(item_path, selected_link, import_roots)
        for page in pages:
            page["asset_url"] = f"/api/projects/{project_id}/items/{item_id}/assets/{page['page_ref']}"
        return {"pages": pages}
    pages = []
    for asset in document.get("resources", {}).get("assets", []):
        if not isinstance(asset, dict) or asset.get("asset_id") not in allowed:
            continue
        try:
            checked = _asset(item_path, asset["asset_id"])
        except SolutionSourceError:
            continue
        pages.append({"page_ref": checked["asset_id"], "width": checked["width_px"], "height": checked["height_px"],
            "asset_url": f"/api/projects/{project_id}/items/{item_id}/assets/{checked['asset_id']}"})
    return {"pages": pages}


def _render_pdf_page(item_path: Path, pdf_page: int, import_roots: tuple[Path, ...]) -> Image.Image:
    from .solution_reconciliation import locate_leaf_run
    import pypdfium2 as pdfium

    document = load_json_object(item_path)
    selection = document.get("provenance", {}).get("solution_selection", {})
    link = next((entry for entry in document.get("provenance", {}).get("solution_links", [])
                 if isinstance(entry, dict) and entry.get("source_asset_id") == selection.get("source_asset_id")), None)
    leaf = locate_leaf_run(item_path.parents[2] / "project.json", import_roots)
    if not isinstance(link, dict) or leaf is None:
        raise SolutionSourceError("현재 PDF 해설 원본을 찾을 수 없습니다.", 409)
    manifest = load_json_object(leaf / "solutions" / "link_manifest.json")
    pdf_path = Path(str(manifest.get("solution", {}).get("source_pdf", ""))).expanduser()
    if not pdf_path.is_file() or sha256_file(pdf_path) != link.get("document_sha256"):
        raise SolutionSourceError("PDF 해설 원본이 변경되었습니다.", 409)
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        if len(pdf) != link.get("document_page_count") or not 1 <= pdf_page <= len(pdf):
            raise SolutionSourceError("PDF 해설 페이지가 변경되었습니다.", 409)
        page = pdf[pdf_page - 1]
        try:
            bitmap = page.render(scale=450 / 72)
            try:
                image = bitmap.to_pil()
                try:
                    return image.copy()
                finally:
                    image.close()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        pdf.close()


def _prepare_crop(item_path: Path, source: Mapping[str, Any], import_roots: tuple[Path, ...], *, high_res_pdf: bool = False) -> dict[str, Any]:
    if set(source) != {"kind", "base_source_asset_id", "parts"} or not isinstance(source.get("parts"), list) or not 1 <= len(source["parts"]) <= 8:
        raise SolutionSourceError("자를 원본과 영역이 올바르지 않습니다.")
    base = _asset(item_path, source["base_source_asset_id"])
    if base.get("section") not in {"explanation", "shared"}:
        raise SolutionSourceError("해설 원본이 아닙니다.", 404)
    pages = source_pages(item_path, "p", "i", import_roots)["pages"]
    allowed = {page["page_ref"]: page for page in pages}
    if not allowed:
        raise SolutionSourceError("현재 원본에서 사용할 수 없는 영역입니다.", 404)
    cropped = []
    for part in source["parts"]:
        if not isinstance(part, dict) or set(part) != {"page_ref", "bbox"} or part["page_ref"] not in allowed:
            raise SolutionSourceError("현재 원본의 페이지 ID가 아닙니다.", 404)
        box = part["bbox"]
        if not isinstance(box, dict) or box.get("unit") != "ratio" or box.get("origin") != "top-left" or any(type(box.get(key)) not in {int, float} for key in ("left", "top", "right", "bottom")):
            raise SolutionSourceError("영역 좌표가 올바르지 않습니다.", 422)
        left, top, right, bottom = (box[key] for key in ("left", "top", "right", "bottom"))
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise SolutionSourceError("이미지 밖이나 빈 영역은 선택할 수 없습니다.", 422)
        page_ref = part["page_ref"]
        path = (item_path.parent / "jobs" / "solution_pages" / f"{page_ref}.png") if page_ref.startswith("solution-page-") else item_path.parent / _asset(item_path, page_ref)["path"]
        if page_ref.startswith("solution-page-") and sha256_file(path) != allowed[page_ref]["sha256"]:
            raise SolutionSourceError("원본 페이지가 변경되었습니다.", 409)
        opened = (_render_pdf_page(item_path, allowed[page_ref]["pdf_page"], import_roots)
                  if high_res_pdf and page_ref.startswith("solution-page-") else Image.open(path))
        try:
            opened.load()
            pixel_box = (round(left * opened.width), round(top * opened.height), round(right * opened.width), round(bottom * opened.height))
            if pixel_box[0] >= pixel_box[2] or pixel_box[1] >= pixel_box[3]:
                raise SolutionSourceError("빈 영역은 선택할 수 없습니다.", 422)
            cropped.append(opened.convert("RGB").crop(pixel_box))
        finally:
            opened.close()
    width = max(image.width for image in cropped)
    height = sum(image.height for image in cropped)
    if width * height > MAX_COMPOSITE_PIXELS:
        raise SolutionSourceError("합성 이미지가 한도를 넘었습니다. 영역을 나누거나 크기를 조정해 주세요.", 422)
    canvas = Image.new("RGB", (width, height), "white")
    offset = 0
    for image in cropped:
        canvas.paste(image, (0, offset))
        offset += image.height
    return _write_immutable(item_path, canvas)


def _write_immutable_from_file(item_path: Path, path: Path) -> dict[str, Any]:
    with Image.open(path) as opened:
        image = opened.copy()
    asset = _write_immutable(item_path, image)
    return asset


def select_source(item_path: Path, payload: Mapping[str, Any], *, revert: bool = False, import_roots: tuple[Path, ...] = (), auto_crop: bool = False) -> dict[str, Any]:
    required = {"operation_id", "expected_revision", "expected_selection_revision", "target_selection_revision", "restore_content"} if revert else {"operation_id", "expected_revision", "expected_selection_revision", "source"}
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise SolutionSourceError("원본 선택 요청 필드가 올바르지 않습니다.")
    if auto_crop and (revert or not isinstance(payload.get("source"), Mapping) or payload["source"].get("kind") != "crop"):
        raise SolutionSourceError("자동 영역 보정에는 현재 해설 crop만 사용할 수 있습니다.")
    operation_id = payload["operation_id"]
    if not isinstance(operation_id, str) or len(operation_id) > 100 or not operation_id or not all(c.isalnum() or c in "-_" for c in operation_id):
        raise SolutionSourceError("작업 ID가 올바르지 않습니다.")
    operation_path = item_path.parent / "jobs" / "solution_operations" / operation_id / "operation.json"
    payload_sha = _digest(payload)
    pending = None
    if operation_path.is_file():
        existing = load_json_object(operation_path)
        if existing.get("payload_sha256") != payload_sha:
            raise SolutionSourceError("같은 작업 ID에 다른 요청을 사용할 수 없습니다.", 409)
        if isinstance(existing.get("response"), dict):
            return existing["response"]
        pending = existing
    document = load_json_object(item_path)
    current_revision = document["workflow"]["revision"]
    selection = document.get("provenance", {}).get("solution_selection")
    selection_revision = selection.get("revision", 0) if isinstance(selection, dict) else 0
    if pending is not None and isinstance(selection, dict) and selection_revision == pending.get("selection_revision") and selection.get("source_asset_id") == pending.get("source_asset_id") and selection.get("source_sha256") == pending.get("source_sha256"):
        response = {"operation_id": operation_id, "item_revision": current_revision,
            "selection_revision": selection_revision, "source_asset_id": pending["source_asset_id"],
            "job_id": None, "transcription_state": "pending"}
        atomic_write_json(operation_path, {**pending, "response": response, "state": "selected"})
        return response
    if type(payload["expected_revision"]) is not int or payload["expected_revision"] != current_revision or type(payload["expected_selection_revision"]) is not int or payload["expected_selection_revision"] != selection_revision:
        raise SolutionSourceError("문항 또는 원본 선택 버전이 변경되었습니다.", 409)
    prior = {"selection": copy.deepcopy(selection), "explanation": copy.deepcopy(document.get("content", {}).get("explanation")),
             "answer": copy.deepcopy(document.get("content", {}).get("answer")), "content_hash": solution_content_hash(document),
             "last_auto_applied_hash": document.get("provenance", {}).get("last_auto_applied_hash"), "item_revision": current_revision}
    if revert:
        if payload["restore_content"] not in {"auto_only", "including_manual"}:
            raise SolutionSourceError("복원 범위가 올바르지 않습니다.")
        target_revision = payload["target_selection_revision"]
        if type(target_revision) is not int or target_revision < 1 or target_revision >= selection_revision:
            raise SolutionSourceError("복원할 원본 버전이 없습니다.", 404)
        previous_path = item_path.parent / "jobs" / "solution_versions" / str(target_revision) / "snapshot.json"
        if not previous_path.is_file():
            raise SolutionSourceError("복원할 원본 기록이 없습니다.", 404)
        previous = load_json_object(previous_path)
        chosen = previous.get("selection")
        if not isinstance(chosen, dict):
            raise SolutionSourceError("복원할 원본 기록이 없습니다.", 404)
        asset = _asset(item_path, chosen["source_asset_id"])
        origin = chosen["origin"]
        image_sources = None
    else:
        source = payload["source"]
        if not isinstance(source, dict) or source.get("kind") not in {"candidate", "uploads", "crop"}:
            raise SolutionSourceError("지원하지 않는 원본 종류입니다.")
        image_sources = None
        if source["kind"] == "candidate" and set(source) == {"kind", "source_asset_id"}:
            asset = _asset(item_path, source["source_asset_id"])
            if asset.get("section") not in {"explanation", "shared"}:
                raise SolutionSourceError("해설 이미지가 아닙니다.", 404)
            eligible = any(isinstance(link, dict) and link.get("source_asset_id") == asset["asset_id"]
                and link.get("status") in {"review", "verified"}
                and not any("rejected" in str(reason) or "excluded" in str(reason)
                    for reason in link.get("review_reasons", []))
                for link in document.get("provenance", {}).get("solution_links", []))
            if not eligible:
                raise SolutionSourceError("거절되었거나 연결되지 않은 해설 후보입니다.", 422)
            origin = "candidate_choice"
        elif source["kind"] == "uploads" and set(source) == {"kind", "upload_ids"}:
            asset, image_sources = _prepare_uploads(item_path, source["upload_ids"])
            origin = "user_upload"
        elif source["kind"] == "crop":
            asset = _prepare_crop(item_path, source, import_roots, high_res_pdf=auto_crop)
            origin = "auto_candidate" if auto_crop else "user_crop"
        else:
            raise SolutionSourceError("원본 선택 요청 필드가 올바르지 않습니다.")
    candidate = copy.deepcopy(document)
    assets = candidate["resources"]["assets"]
    if not any(existing.get("asset_id") == asset["asset_id"] for existing in assets):
        assets.append(asset)
    if image_sources:
        for entry in image_sources:
            if not any(a.get("asset_id") == entry["asset_id"] for a in assets):
                assets.append({"asset_id": entry["asset_id"], "section": "explanation", "role": "source_crop", "state": "ready", "path": f"assets/{entry['asset_id']}.png", "media_type": "image/png", "sha256": entry["sha256"], "width_px": entry["width"], "height_px": entry["height"], "review": {"status": "unreviewed", "boundary_uncertain": False, "notes": []}})
        candidate["provenance"].setdefault("solution_links", []).append({"kind": "detailed_solution", "external_asset_id": f"upload-{uuid.uuid4().hex}",
            "source_asset_id": asset["asset_id"], "status": "review", "confidence": 0, "evidence": [], "review_reasons": ["user_selected_unverified"],
            "parts": [], "source_origin": "uploaded_image", "image_sources": image_sources})
    if not revert and source["kind"] == "crop":
        base_link = next((link for link in document["provenance"].get("solution_links", [])
            if isinstance(link, dict) and link.get("source_asset_id") == source["base_source_asset_id"]), None)
        if base_link is None:
            raise SolutionSourceError("자를 해설 연결을 찾을 수 없습니다.", 404)
        new_link = copy.deepcopy(base_link)
        new_link.update(external_asset_id=f"{'auto-crop' if auto_crop else 'crop'}-{uuid.uuid4().hex}", source_asset_id=asset["asset_id"],
            status="review", confidence=0, evidence=[], review_reasons=["auto_crop_unverified" if auto_crop else "user_crop_unverified"])
        new_link["crop_parts"] = copy.deepcopy(source["parts"])
        if auto_crop and len(source["parts"]) == 1 and len(new_link.get("parts", [])) == 1:
            new_link["parts"][0]["bbox"] = copy.deepcopy(source["parts"][0]["bbox"])
            new_link["parts"][0]["render_dpi"] = 450
        candidate["provenance"].setdefault("solution_links", []).append(new_link)
    candidate["provenance"]["solution_selection"] = {"source_asset_id": asset["asset_id"], "source_sha256": asset["sha256"],
        "revision": selection_revision + 1, "origin": origin, "selected_at": utc_now()}
    if revert and payload["restore_content"] == "including_manual":
        candidate["content"]["explanation"] = copy.deepcopy(previous["explanation"])
        candidate["content"]["answer"] = copy.deepcopy(previous["answer"])
        if previous.get("last_auto_applied_hash") is not None:
            candidate["provenance"]["last_auto_applied_hash"] = previous["last_auto_applied_hash"]
        else:
            candidate["provenance"].pop("last_auto_applied_hash", None)
    snapshot_path = item_path.parent / "jobs" / "solution_versions" / str(selection_revision) / "snapshot.json"
    if selection_revision > 0 and not snapshot_path.is_file():
        atomic_write_json(snapshot_path, prior)
    source_event = {"event_id": f"source-{operation_id}", "operation": "source_revert" if revert else "auto_crop_repair" if auto_crop else "source_selection",
        "authority": "auto_selected_unverified" if auto_crop else "user_selected_unverified", "before_sha256": _digest(prior["selection"]),
        "after_sha256": _digest(candidate["provenance"]["solution_selection"]),
        "before_asset_id": prior["selection"].get("source_asset_id") if isinstance(prior["selection"], dict) else None,
        "after_asset_id": asset["asset_id"], "source_sha256": asset["sha256"],
        "selection_revision": selection_revision + 1, "applied_at": utc_now()}
    atomic_write_json(operation_path, {"operation_id": operation_id, "payload_sha256": payload_sha,
        "source_asset_id": asset["asset_id"], "source_sha256": asset["sha256"],
        "selection_revision": selection_revision + 1, "state": "pending", "created_at": utc_now(),
        "source_event": source_event, "learning_sync": "not_applicable" if auto_crop else "pending"})
    saved = save_json_cas(item_path, candidate, current_revision, preserve_approval=True)
    response = {"operation_id": operation_id, "item_revision": saved["workflow"]["revision"],
        "selection_revision": selection_revision + 1, "source_asset_id": asset["asset_id"], "job_id": None, "transcription_state": "pending"}
    atomic_write_json(operation_path, {"operation_id": operation_id, "payload_sha256": payload_sha,
        "source_asset_id": asset["asset_id"], "source_sha256": asset["sha256"],
        "selection_revision": selection_revision + 1, "response": response,
        "state": "selected", "created_at": utc_now(), "source_event": source_event,
        "learning_sync": "not_applicable" if auto_crop else "pending"})
    return response



