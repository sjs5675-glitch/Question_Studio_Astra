"""Explicit, immutable Gemini redraw jobs for canonical figure blocks."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import threading
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import quote

from .figure_recrop import _assets, _figure, _safe_asset_path, _workflow_revision
from .model import (
    ModelError,
    ModelNotFound,
    RevisionConflict,
    atomic_write_json,
    load_json_object,
    save_json_cas,
    sha256_file,
    utc_now,
    validate_id,
)
from .provider_http import JsonPost, ProviderHTTPError, post_json
from .transcription import ImageValidationError, ValidatedImage, inspect_image


APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_PATH = APP_ROOT / "prompts" / "regenerate_science_figure.md"
PROMPT_VERSION = "regenerate-science-figure-v1"
IMAGE_SIZE = "2K"
MAX_PROMPT_BYTES = 128 * 1024
MAX_SOURCE_IMAGE_BYTES = 18 * 1024 * 1024
MAX_SOURCE_IMAGE_PIXELS = 80_000_000
MAX_PROVIDER_REQUEST_BYTES = 26 * 1024 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_GENERATED_IMAGE_BYTES = 20 * 1024 * 1024
MAX_GENERATED_IMAGE_PIXELS = 40_000_000
MAX_ENCODED_IMAGE_CHARS = 4 * ((MAX_GENERATED_IMAGE_BYTES + 2) // 3) + 4
MANUAL_REVIEW_NOTE = (
    "AI 재생성 결과입니다. 원본과 글자·화살표·요소 수·기하 구조를 수동 비교하세요."
)
REMOVE_BLUE_TEXT_RULE = (
    "As the sole exception to label preservation, remove only text glyphs visibly "
    "rendered in blue and restore only their plain background. Preserve blue lines, "
    "arrows, plots, symbols, and all non-text geometry; do not infer or reveal content "
    "behind removed glyphs."
)
PRESERVE_BLUE_TEXT_RULE = (
    "Preserve every visible blue text glyph and every other colored element exactly."
)


class FigureGenerationError(ModelError):
    """The requested redraw cannot safely become a canonical candidate."""


class FigureJobIntegrityError(FigureGenerationError):
    """An immutable redraw job or its output no longer matches its metadata."""


class FigureProviderNotConfigured(FigureGenerationError):
    """Gemini credentials are unavailable for an explicit redraw request."""


class FigureProviderRequestError(RuntimeError):
    """Gemini failed or returned an unsafe/malformed image response."""


@dataclass(frozen=True)
class FigureProfile:
    model: str
    estimated_output_usd: float


FIGURE_PROFILES = {
    "balanced": FigureProfile("gemini-3.1-flash-image", 0.101),
    "quality": FigureProfile("gemini-3-pro-image", 0.134),
}
PRICING_AS_OF = "2026-08-25"


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    media_type: str
    provider_result: Mapping[str, Any]


@dataclass(frozen=True)
class FigureGenerationJob:
    path: Path
    metadata: dict[str, Any]
    prompt: str
    provider_result: dict[str, Any]
    output: ValidatedImage
    cached: bool = False


@dataclass(frozen=True)
class FigureArtifact:
    path: Path
    metadata: dict[str, Any]
    prompt: str
    provider_result: dict[str, Any]
    output: ValidatedImage
    cached: bool = False


class FigureGenerationAdapter(Protocol):
    provider_name: str

    def generate(
        self,
        *,
        image: ValidatedImage,
        prompt: str,
        model: str,
        image_size: str,
    ) -> GeneratedImage: ...


def _figure_post_json(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: float,
) -> Mapping[str, Any]:
    return post_json(
        url,
        headers,
        payload,
        timeout,
        max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
    )


def _provider_inline_image(image: ValidatedImage) -> tuple[bytes, str]:
    try:
        raw = image.path.read_bytes()
    except OSError as exc:
        raise FigureGenerationError("검증한 원본 그림을 읽을 수 없습니다.") from exc
    if len(raw) != image.size_bytes or hashlib.sha256(raw).hexdigest() != image.sha256:
        raise FigureGenerationError("검증 후 원본 그림 파일이 변경되었습니다.")
    if image.media_type != "image/bmp":
        return raw, image.media_type
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as opened:
            opened.load()
            has_alpha = "A" in opened.getbands() or "transparency" in opened.info
            normalized = opened.convert("RGBA" if has_alpha else "RGB")
            output = io.BytesIO()
            normalized.save(output, format="PNG")
            converted = output.getvalue()
    except (OSError, ValueError) as exc:
        raise FigureGenerationError("BMP 재생성 원본을 PNG로 변환할 수 없습니다.") from exc
    if not converted or len(converted) > MAX_SOURCE_IMAGE_BYTES:
        raise FigureGenerationError("변환한 PNG 재생성 원본이 안전 제한을 초과합니다.")
    return converted, "image/png"


class GeminiFigureAdapter:
    provider_name = "gemini"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        http_post: JsonPost | None = None,
        timeout: float = 180.0,
    ) -> None:
        self._configured_api_key = api_key
        self._http_post = http_post or _figure_post_json
        self._timeout = timeout

    def generate(
        self,
        *,
        image: ValidatedImage,
        prompt: str,
        model: str,
        image_size: str,
    ) -> GeneratedImage:
        if not isinstance(image, ValidatedImage):
            raise FigureGenerationError("figure generation requires a validated image")
        if not isinstance(prompt, str) or not prompt.strip():
            raise FigureGenerationError("figure generation prompt must be non-empty")
        stable_models = {profile.model for profile in FIGURE_PROFILES.values()}
        if not isinstance(model, str) or model not in stable_models:
            raise FigureGenerationError("figure generation model is not an allowed stable model")
        if not isinstance(image.media_type, str) or image.media_type not in {
            "image/png",
            "image/jpeg",
            "image/bmp",
        }:
            raise FigureGenerationError("figure generation source MIME is unsupported")
        api_key = self._configured_api_key
        if api_key is None:
            api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not isinstance(api_key, str) or not api_key.strip():
            raise FigureProviderNotConfigured(
                "Gemini 그림 재생성에는 GEMINI_API_KEY 또는 GOOGLE_API_KEY가 필요합니다."
            )
        if image_size != IMAGE_SIZE:
            raise FigureGenerationError("figure generation image_size must be 2K")
        raw, request_media_type = _provider_inline_image(image)
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inlineData": {
                                "mimeType": request_media_type,
                                "data": base64.b64encode(raw).decode("ascii"),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"imageSize": image_size},
            },
        }
        request_size = len(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if request_size > MAX_PROVIDER_REQUEST_BYTES:
            raise FigureGenerationError("Gemini 그림 요청이 안전 제한을 초과합니다.")
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{quote(model, safe='-._')}:generateContent"
        )
        try:
            response = self._http_post(
                url,
                {"x-goog-api-key": api_key.strip()},
                payload,
                self._timeout,
            )
        except ProviderHTTPError as exc:
            raise FigureProviderRequestError(f"Gemini 그림 재생성 요청 실패: {exc}") from exc
        if not isinstance(response, Mapping):
            raise FigureProviderRequestError("Gemini 그림 응답 루트는 JSON 객체여야 합니다.")
        return _gemini_generated_image(response)


def _gemini_generated_image(response: Mapping[str, Any]) -> GeneratedImage:
    feedback = response.get("promptFeedback")
    if isinstance(feedback, Mapping) and feedback.get("blockReason"):
        raise FigureProviderRequestError(
            f"Gemini가 그림 요청을 거부했습니다: {feedback['blockReason']}"
        )
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise FigureProviderRequestError("Gemini 응답에 그림 후보가 없습니다.")
    images: list[tuple[int, int, str, str, Any]] = []
    for candidate_index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            raise FigureProviderRequestError("Gemini 그림 후보 형식이 올바르지 않습니다.")
        finish_reason = candidate.get("finishReason")
        if finish_reason is not None and finish_reason != "STOP":
            raise FigureProviderRequestError(
                f"Gemini 그림 생성이 정상 완료되지 않았습니다: {finish_reason}"
            )
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, Mapping) else None
        if not isinstance(parts, list):
            continue
        for part_index, part in enumerate(parts):
            if not isinstance(part, Mapping) or part.get("thought") is True:
                continue
            inline = part.get("inlineData")
            if not isinstance(inline, Mapping):
                inline = part.get("inline_data")
            if not isinstance(inline, Mapping):
                continue
            mime_type = inline.get("mimeType")
            if not isinstance(mime_type, str):
                mime_type = inline.get("mime_type")
            data = inline.get("data")
            if isinstance(mime_type, str) and isinstance(data, str):
                images.append((candidate_index, part_index, mime_type, data, finish_reason))
    if len(images) != 1:
        raise FigureProviderRequestError(
            "Gemini 응답에는 정확히 하나의 그림이 있어야 합니다."
        )
    candidate_index, part_index, mime_type, encoded, finish_reason = images[0]
    if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise FigureProviderRequestError(f"지원하지 않는 Gemini 그림 MIME입니다: {mime_type}")
    if len(encoded) > MAX_ENCODED_IMAGE_CHARS:
        raise FigureProviderRequestError("Gemini 그림 응답이 안전 제한을 초과합니다.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise FigureProviderRequestError("Gemini 그림 응답의 base64가 올바르지 않습니다.") from exc
    if not raw or len(raw) > MAX_GENERATED_IMAGE_BYTES:
        raise FigureProviderRequestError("Gemini 그림 바이트 크기가 안전 제한을 초과합니다.")
    result = {
        "candidate_index": candidate_index,
        "part_index": part_index,
        "finish_reason": finish_reason,
        "mime_type": mime_type,
        "response_id": response.get("responseId") if isinstance(response.get("responseId"), str) else None,
        "model_version": response.get("modelVersion") if isinstance(response.get("modelVersion"), str) else None,
        "usage_metadata": _usage_metadata(response.get("usageMetadata")),
    }
    return GeneratedImage(raw, mime_type, result)


def _usage_metadata(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    allowed = {
        "promptTokenCount",
        "candidatesTokenCount",
        "totalTokenCount",
        "cachedContentTokenCount",
        "thoughtsTokenCount",
    }
    return {
        key: number
        for key, number in value.items()
        if key in allowed and isinstance(number, int) and not isinstance(number, bool) and number >= 0
    }


def _render_prompt(alt_text: Any, remove_blue_text: bool, prompt_path: Path | str) -> str:
    if not isinstance(alt_text, str):
        raise FigureGenerationError("figure alt_text must be a string")
    try:
        template = Path(prompt_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FigureGenerationError("과학 그림 재생성 프롬프트를 읽을 수 없습니다.") from exc
    if template.count("{{ALT_TEXT}}") != 1 or template.count("{{BLUE_TEXT_RULE}}") != 1:
        raise FigureGenerationError("과학 그림 재생성 프롬프트 placeholder가 올바르지 않습니다.")
    blue_rule = REMOVE_BLUE_TEXT_RULE if remove_blue_text else PRESERVE_BLUE_TEXT_RULE
    prompt = template.replace("{{ALT_TEXT}}", json.dumps(alt_text, ensure_ascii=False))
    prompt = prompt.replace("{{BLUE_TEXT_RULE}}", blue_rule)
    if "{{" in prompt or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise FigureGenerationError("과학 그림 재생성 프롬프트가 안전 제한을 초과합니다.")
    return prompt


def _verified_figure_image(item_path: Path, asset: Mapping[str, Any]) -> ValidatedImage:
    if asset.get("state") != "ready":
        raise FigureGenerationError("재생성 원본 그림 asset은 ready 상태여야 합니다.")
    media_type = asset.get("media_type")
    if not isinstance(media_type, str) or media_type not in {
        "image/png",
        "image/jpeg",
        "image/bmp",
    }:
        raise FigureGenerationError("재생성 원본 그림 형식이 지원되지 않습니다.")
    try:
        image = inspect_image(
            _safe_asset_path(item_path.parent, asset.get("path")),
            expected_media_type=media_type,
            max_bytes=MAX_SOURCE_IMAGE_BYTES,
            max_pixels=MAX_SOURCE_IMAGE_PIXELS,
        )
    except ImageValidationError as exc:
        raise FigureGenerationError(str(exc)) from exc
    for field in ("sha256", "width_px", "height_px"):
        if asset.get(field) != getattr(image, field):
            raise FigureGenerationError(f"재생성 원본 그림 {field} metadata가 파일과 다릅니다.")
    return image


def _lineage_reaches(
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


def _safe_source(
    item_path: Path,
    document: Mapping[str, Any],
    block_id: str,
) -> tuple[str, Mapping[str, Any], Mapping[str, Any], ValidatedImage]:
    section, block = _figure(document, block_id)
    _, by_id = _assets(document)
    original_id = block.get("original_asset_id")
    selected_id = block.get("asset_id")
    try:
        validate_id(original_id, field="original_asset_id")
        validate_id(selected_id, field="asset_id")
    except ModelError as exc:
        raise FigureGenerationError(str(exc)) from exc
    original = by_id.get(original_id) if isinstance(original_id, str) else None
    if original is None:
        raise ModelNotFound("figure original asset not found")
    if original.get("role") != "figure_original" or original.get("section") not in {section, "shared"}:
        raise FigureGenerationError("figure original asset contract is invalid")
    original_image = _verified_figure_image(item_path, original)
    selected = by_id.get(selected_id) if isinstance(selected_id, str) else None
    if selected is None:
        raise ModelNotFound("selected figure asset not found")
    if selected.get("section") not in {section, "shared"} or selected.get("state") != "ready":
        raise FigureGenerationError("selected figure asset is not a ready same-section asset")
    if selected.get("role") == "figure_original":
        if selected_id != original_id:
            raise FigureGenerationError("selected figure original does not match original_asset_id")
        source_id, source = original_id, original
    elif selected.get("role") == "figure_regenerated":
        if not _lineage_reaches(by_id, str(selected_id), str(original_id)):
            raise FigureGenerationError("selected figure lineage does not reach original_asset_id")
        generation = selected.get("generation")
        review = selected.get("review")
        approved_crop = (
            isinstance(generation, Mapping)
            and generation.get("mode") == "crop"
            and generation.get("provider") is None
            and generation.get("model") is None
            and isinstance(review, Mapping)
            and review.get("status") == "approved"
            and review.get("boundary_uncertain") is False
        )
        source_id, source = (str(selected_id), selected) if approved_crop else (str(original_id), original)
    else:
        raise FigureGenerationError("selected asset is not a figure asset")
    try:
        validate_id(source.get("asset_id"), field="source asset_id")
    except ModelError as exc:
        raise FigureGenerationError(str(exc)) from exc
    image = original_image if source is original else _verified_figure_image(item_path, source)
    return section, block, source, image


def _idempotency_key(
    source_sha256: str, prompt_sha256: str, model: str, block_id: str
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "block_id": block_id,
                "model": model,
                "prompt_sha256": prompt_sha256,
                "source_sha256": source_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def figure_artifact_key(
    source_sha256: str,
    prompt_sha256: str,
    model: str,
    image_size: str = IMAGE_SIZE,
    prompt_version: str = PROMPT_VERSION,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "image_size": image_size,
                "model": model,
                "prompt_sha256": prompt_sha256,
                "prompt_version": prompt_version,
                "source_sha256": source_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _owned_path(item_dir: Path, *parts: str) -> Path:
    resolved = item_dir.joinpath(*parts).resolve()
    try:
        resolved.relative_to(item_dir.resolve())
    except ValueError as exc:
        raise FigureGenerationError("figure generation path escapes the item") from exc
    return resolved


def _owned_directory(item_dir: Path, *parts: str) -> Path:
    unresolved = _owned_path(item_dir, *parts)
    try:
        unresolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FigureGenerationError("figure generation directory cannot be created") from exc
    resolved = unresolved.resolve()
    if not resolved.is_dir():
        raise FigureGenerationError("figure generation path is not a directory")
    return resolved


def _normalize_png(data: bytes, media_type: str, destination: Path) -> ValidatedImage:
    if not isinstance(data, bytes) or not data or len(data) > MAX_GENERATED_IMAGE_BYTES:
        raise FigureProviderRequestError("생성된 그림 바이트 크기가 안전 제한을 초과합니다.")
    if not isinstance(media_type, str):
        raise FigureProviderRequestError("생성 그림 MIME 형식이 올바르지 않습니다.")
    expected_format = {
        "image/png": "PNG",
        "image/jpeg": "JPEG",
        "image/webp": "WEBP",
    }.get(media_type)
    if expected_format is None:
        raise FigureProviderRequestError(f"지원하지 않는 생성 그림 MIME입니다: {media_type}")
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover - app dependency
        raise FigureGenerationError("Pillow is required to verify generated figures") from exc
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                if probe.format != expected_format:
                    raise FigureProviderRequestError("생성 그림 MIME과 실제 형식이 다릅니다.")
                width, height = probe.size
                if width < 1 or height < 1 or width * height > MAX_GENERATED_IMAGE_PIXELS:
                    raise FigureProviderRequestError("생성 그림 픽셀 수가 안전 제한을 초과합니다.")
                probe.verify()
            with Image.open(io.BytesIO(data)) as opened:
                opened.load()
                has_alpha = "A" in opened.getbands() or "transparency" in opened.info
                normalized = opened.convert("RGBA" if has_alpha else "RGB")
                normalized.save(destination, format="PNG")
    except FigureProviderRequestError:
        raise
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ) as exc:
        raise FigureProviderRequestError("생성 그림을 안전하게 해석할 수 없습니다.") from exc
    try:
        return inspect_image(
            destination,
            expected_media_type="image/png",
            max_bytes=MAX_GENERATED_IMAGE_BYTES,
            max_pixels=MAX_GENERATED_IMAGE_PIXELS,
        )
    except ImageValidationError as exc:
        raise FigureProviderRequestError(str(exc)) from exc


def _sanitized_provider_result(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_index": value.get("candidate_index"),
        "part_index": value.get("part_index"),
        "finish_reason": value.get("finish_reason"),
        "mime_type": value.get("mime_type"),
        "response_id": value.get("response_id") if isinstance(value.get("response_id"), str) else None,
        "model_version": value.get("model_version") if isinstance(value.get("model_version"), str) else None,
        "usage_metadata": _usage_metadata(value.get("usage_metadata")),
    }


def _job_file(job_path: Path, name: str, expected_sha256: Any) -> Path:
    path = job_path / name
    if path.is_symlink() or not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise FigureJobIntegrityError(f"figure job has invalid {name} hash")
    if not path.is_file() or sha256_file(path) != expected_sha256:
        raise FigureJobIntegrityError(f"immutable figure job failed integrity check: {name}")
    return path


def _verify_job_seal(job_path: Path) -> None:
    metadata_path = job_path / "job.json"
    seal_path = job_path / "job.sha256"
    if (
        metadata_path.is_symlink()
        or not metadata_path.is_file()
        or seal_path.is_symlink()
        or not seal_path.is_file()
    ):
        raise FigureJobIntegrityError("figure job metadata seal is missing or unsafe")
    try:
        expected = seal_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise FigureJobIntegrityError("figure job metadata seal cannot be read") from exc
    if len(expected) != 64 or expected != sha256_file(metadata_path):
        raise FigureJobIntegrityError("immutable figure job metadata failed integrity check")


def _valid_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return None


def _validate_job_metadata(
    metadata: Mapping[str, Any], prompt: str, provider_result: Mapping[str, Any]
) -> None:
    profile_name = metadata.get("profile")
    profile = FIGURE_PROFILES.get(profile_name) if isinstance(profile_name, str) else None
    if (
        metadata.get("schema_version") != "0.1.0"
        or metadata.get("status") != "succeeded"
        or metadata.get("provider") != "gemini"
        or profile is None
        or metadata.get("model") != profile.model
        or metadata.get("image_size") != IMAGE_SIZE
        or metadata.get("prompt_version") != PROMPT_VERSION
    ):
        raise FigureJobIntegrityError("figure job provider/profile contract is invalid")
    remove_blue_text = metadata.get("remove_blue_text")
    if not isinstance(remove_blue_text, bool):
        raise FigureJobIntegrityError("figure job remove_blue_text is invalid")
    required_rule = REMOVE_BLUE_TEXT_RULE if remove_blue_text else PRESERVE_BLUE_TEXT_RULE
    forbidden_rule = PRESERVE_BLUE_TEXT_RULE if remove_blue_text else REMOVE_BLUE_TEXT_RULE
    if required_rule not in prompt or forbidden_rule in prompt:
        raise FigureJobIntegrityError("figure job blue-text rule does not match its prompt")
    requested_revision = metadata.get("requested_revision")
    if (
        isinstance(requested_revision, bool)
        or not isinstance(requested_revision, int)
        or requested_revision < 1
    ):
        raise FigureJobIntegrityError("figure job requested_revision is invalid")
    try:
        validate_id(metadata.get("block_id"), field="block_id")
        validate_id(metadata.get("source_asset_id"), field="source_asset_id")
    except ModelError as exc:
        raise FigureJobIntegrityError(str(exc)) from exc
    if metadata.get("source_media_type") not in {"image/png", "image/jpeg", "image/bmp"}:
        raise FigureJobIntegrityError("figure job source MIME is invalid")
    for field in (
        "source_width_px",
        "source_height_px",
        "source_size_bytes",
        "output_width_px",
        "output_height_px",
        "output_size_bytes",
    ):
        value = metadata.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise FigureJobIntegrityError(f"figure job {field} is invalid")
    started = _valid_timestamp(metadata.get("started_at"))
    completed = _valid_timestamp(metadata.get("completed_at"))
    if started is None or completed is None or completed < started:
        raise FigureJobIntegrityError("figure job timestamps are invalid")
    expected_result_fields = {
        "candidate_index",
        "part_index",
        "finish_reason",
        "mime_type",
        "response_id",
        "model_version",
        "usage_metadata",
    }
    if set(provider_result) != expected_result_fields:
        raise FigureJobIntegrityError("figure provider result contains unexpected fields")
    if provider_result.get("mime_type") != metadata.get("provider_output_mime_type"):
        raise FigureJobIntegrityError("figure provider result MIME does not match job metadata")
    usage = provider_result.get("usage_metadata")
    if not isinstance(usage, Mapping) or _usage_metadata(usage) != dict(usage):
        raise FigureJobIntegrityError("figure provider usage metadata is invalid")


def load_figure_job(job_path: Path | str, *, cached: bool = False) -> FigureGenerationJob:
    job_path = Path(job_path)
    if job_path.is_symlink() or not job_path.is_dir():
        raise FigureJobIntegrityError("figure job path is not an owned directory")
    _verify_job_seal(job_path)
    metadata = load_json_object(job_path / "job.json")
    if metadata.get("status") != "succeeded":
        raise FigureJobIntegrityError("figure job is not a completed success")
    prompt_path = _job_file(job_path, "prompt.txt", metadata.get("prompt_sha256"))
    result_path = _job_file(
        job_path, "provider-result.json", metadata.get("provider_result_sha256")
    )
    output_path = _job_file(job_path, "output.png", metadata.get("output_sha256"))
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FigureJobIntegrityError("figure job prompt is not valid UTF-8") from exc
    provider_result = load_json_object(result_path)
    _validate_job_metadata(metadata, prompt, provider_result)
    try:
        output = inspect_image(
            output_path,
            expected_media_type="image/png",
            max_bytes=MAX_GENERATED_IMAGE_BYTES,
            max_pixels=MAX_GENERATED_IMAGE_PIXELS,
        )
    except ImageValidationError as exc:
        raise FigureJobIntegrityError(str(exc)) from exc
    if any(
        metadata.get(field) != getattr(output, attribute)
        for field, attribute in (
            ("output_sha256", "sha256"),
            ("output_width_px", "width_px"),
            ("output_height_px", "height_px"),
            ("output_size_bytes", "size_bytes"),
        )
    ):
        raise FigureJobIntegrityError("figure job output metadata does not match its PNG")
    key = _idempotency_key(
        str(metadata.get("source_sha256")),
        str(metadata.get("prompt_sha256")),
        str(metadata.get("model")),
        str(metadata.get("block_id")),
    )
    expected_job_id = f"figure-generation-{key[:32]}"
    if (
        metadata.get("idempotency_key") != key
        or metadata.get("job_id") != expected_job_id
        or job_path.name != expected_job_id
    ):
        raise FigureJobIntegrityError("figure job identity does not match its immutable inputs")
    return FigureGenerationJob(
        job_path.resolve(), metadata, prompt, provider_result, output, cached=cached
    )


def _artifact_path(cache_root: Path | str, key: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise FigureGenerationError("figure artifact key is invalid")
    root = Path(cache_root)
    if root.is_symlink():
        raise FigureGenerationError("figure artifact cache cannot be a symlink")
    root = root.resolve()
    path = (root / key[:2] / key).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise FigureGenerationError("figure artifact path escapes its cache") from exc
    return path


def load_figure_artifact(
    artifact_path: Path | str, *, cached: bool = False
) -> FigureArtifact:
    path = Path(artifact_path)
    if path.is_symlink() or not path.is_dir():
        raise FigureJobIntegrityError("figure artifact path is unsafe")
    metadata_path = path / "artifact.json"
    seal_path = path / "artifact.sha256"
    if (
        metadata_path.is_symlink()
        or not metadata_path.is_file()
        or seal_path.is_symlink()
        or not seal_path.is_file()
    ):
        raise FigureJobIntegrityError("figure artifact seal is missing or unsafe")
    try:
        seal = seal_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise FigureJobIntegrityError("figure artifact seal cannot be read") from exc
    if len(seal) != 64 or seal != sha256_file(metadata_path):
        raise FigureJobIntegrityError("figure artifact metadata failed integrity check")
    metadata = load_json_object(metadata_path)
    prompt_path = _job_file(path, "prompt.txt", metadata.get("prompt_sha256"))
    result_path = _job_file(
        path, "provider-result.json", metadata.get("provider_result_sha256")
    )
    output_path = _job_file(path, "output.png", metadata.get("output_sha256"))
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FigureJobIntegrityError("figure artifact prompt is not valid UTF-8") from exc
    provider_result = load_json_object(result_path)
    profile_name = metadata.get("profile")
    profile = FIGURE_PROFILES.get(profile_name) if isinstance(profile_name, str) else None
    expected_key = figure_artifact_key(
        str(metadata.get("source_sha256")),
        str(metadata.get("prompt_sha256")),
        str(metadata.get("model")),
        str(metadata.get("image_size")),
        str(metadata.get("prompt_version")),
    )
    if (
        metadata.get("schema_version") != "0.1.0"
        or metadata.get("status") != "succeeded"
        or metadata.get("provider") != "gemini"
        or profile is None
        or metadata.get("model") != profile.model
        or metadata.get("image_size") != IMAGE_SIZE
        or metadata.get("prompt_version") != PROMPT_VERSION
        or metadata.get("artifact_key") != expected_key
        or path.name != expected_key
    ):
        raise FigureJobIntegrityError("figure artifact identity is invalid")
    if _sanitized_provider_result(provider_result) != provider_result:
        raise FigureJobIntegrityError("figure artifact provider result is invalid")
    started = _valid_timestamp(metadata.get("started_at"))
    completed = _valid_timestamp(metadata.get("completed_at"))
    if started is None or completed is None or completed < started:
        raise FigureJobIntegrityError("figure artifact timestamps are invalid")
    if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != metadata.get(
        "prompt_sha256"
    ):
        raise FigureJobIntegrityError("figure artifact prompt hash is invalid")
    output = inspect_image(
        output_path,
        expected_media_type="image/png",
        max_bytes=MAX_GENERATED_IMAGE_BYTES,
        max_pixels=MAX_GENERATED_IMAGE_PIXELS,
    )
    if any(
        metadata.get(field) != getattr(output, attribute)
        for field, attribute in (
            ("output_sha256", "sha256"),
            ("output_width_px", "width_px"),
            ("output_height_px", "height_px"),
            ("output_size_bytes", "size_bytes"),
        )
    ):
        raise FigureJobIntegrityError("figure artifact output metadata is invalid")
    return FigureArtifact(
        path.resolve(), metadata, prompt, provider_result, output, cached=cached
    )


def inspect_figure_generation_inputs(
    item_path: Path | str,
    expected_revision: int,
    block_id: str,
    cache_root: Path | str,
    *,
    profile: str = "quality",
    remove_blue_text: bool = False,
) -> dict[str, Any]:
    """Validate a target and calculate its semantic cache identity without writes."""

    path = Path(item_path).resolve()
    document = load_json_object(path)
    if _workflow_revision(document) != expected_revision:
        raise RevisionConflict(expected_revision, _workflow_revision(document))
    section, block, source, image = _safe_source(path, document, block_id)
    if section != "question":
        raise FigureGenerationError("selective figure generation is question-only")
    selected_profile = FIGURE_PROFILES.get(profile)
    if selected_profile is None or remove_blue_text is not False:
        raise FigureGenerationError("selective figure generation uses fixed quality options")
    prompt = _render_prompt(block.get("alt_text"), False, DEFAULT_PROMPT_PATH)
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    key = figure_artifact_key(
        image.sha256, prompt_sha256, selected_profile.model
    )
    artifact_path = _artifact_path(cache_root, key)
    cached = False
    cache_source = None
    if artifact_path.exists():
        load_figure_artifact(artifact_path, cached=True)
        cached = True
        cache_source = "global"
    else:
        local_key = _idempotency_key(
            image.sha256, prompt_sha256, selected_profile.model, block_id
        )
        local_path = (
            path.parent
            / "jobs"
            / "figure_generation"
            / f"figure-generation-{local_key[:32]}"
        )
        if local_path.exists():
            try:
                load_figure_job(local_path, cached=True)
            except (FigureGenerationError, OSError):
                pass
            else:
                cached = True
                cache_source = "item_local"
    return {
        "section": section,
        "block_id": block_id,
        "source_asset_id": source.get("asset_id"),
        "source_sha256": image.sha256,
        "prompt_sha256": prompt_sha256,
        "artifact_key": key,
        "cached": cached,
        "cache_source": cache_source,
        "model": selected_profile.model,
        "image_size": IMAGE_SIZE,
        "prompt_version": PROMPT_VERSION,
    }


def _publish_figure_artifact(
    cache_root: Path | str, job: FigureGenerationJob
) -> FigureArtifact:
    metadata = job.metadata
    key = figure_artifact_key(
        str(metadata["source_sha256"]),
        str(metadata["prompt_sha256"]),
        str(metadata["model"]),
    )
    final_path = _artifact_path(cache_root, key)
    with _job_lock(final_path):
        if final_path.exists():
            return load_figure_artifact(final_path, cached=True)
        root = Path(cache_root)
        if root.is_symlink():
            raise FigureGenerationError("figure artifact cache cannot be a symlink")
        parent = final_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise FigureGenerationError("figure artifact cache path is unsafe")
        with tempfile.TemporaryDirectory(prefix=".artifact-", dir=parent) as temporary:
            staging = Path(temporary)
            for name, field in (
                ("prompt.txt", "prompt_sha256"),
                ("provider-result.json", "provider_result_sha256"),
                ("output.png", "output_sha256"),
            ):
                shutil.copyfile(
                    _job_file(job.path, name, metadata[field]), staging / name
                )
            artifact_metadata = {
                "schema_version": "0.1.0",
                "artifact_key": key,
                "status": "succeeded",
                "provider": "gemini",
                "profile": metadata["profile"],
                "model": metadata["model"],
                "image_size": metadata["image_size"],
                "prompt_version": metadata["prompt_version"],
                "prompt_sha256": sha256_file(staging / "prompt.txt"),
                "source_sha256": metadata["source_sha256"],
                "remove_blue_text": metadata["remove_blue_text"],
                "provider_result_sha256": sha256_file(
                    staging / "provider-result.json"
                ),
                "output_sha256": sha256_file(staging / "output.png"),
                "output_width_px": job.output.width_px,
                "output_height_px": job.output.height_px,
                "output_size_bytes": job.output.size_bytes,
                "started_at": metadata["started_at"],
                "completed_at": metadata["completed_at"],
            }
            atomic_write_json(staging / "artifact.json", artifact_metadata)
            (staging / "artifact.sha256").write_text(
                sha256_file(staging / "artifact.json") + "\n",
                encoding="ascii",
                newline="\n",
            )
            try:
                os.rename(staging, final_path)
            except FileExistsError:
                pass
    return load_figure_artifact(final_path)


def get_or_create_figure_artifact(
    cache_root: Path | str,
    item_path: Path | str,
    expected_revision: int,
    block_id: str,
    *,
    profile: str = "quality",
    remove_blue_text: bool = False,
    adapter: FigureGenerationAdapter | None = None,
) -> FigureArtifact:
    inputs = inspect_figure_generation_inputs(
        item_path,
        expected_revision,
        block_id,
        cache_root,
        profile=profile,
        remove_blue_text=remove_blue_text,
    )
    artifact_path = _artifact_path(cache_root, str(inputs["artifact_key"]))
    if artifact_path.exists():
        return load_figure_artifact(artifact_path, cached=True)
    path = Path(item_path).resolve()
    document = load_json_object(path)
    _, block, source, image = _safe_source(path, document, block_id)
    prompt = _render_prompt(block.get("alt_text"), False, DEFAULT_PROMPT_PATH)
    job = _create_job(
        path,
        block_id=block_id,
        source_asset=source,
        source_image=image,
        prompt=prompt,
        profile_name=profile,
        profile=FIGURE_PROFILES[profile],
        remove_blue_text=False,
        requested_revision=expected_revision,
        adapter=adapter,
    )
    artifact = _publish_figure_artifact(cache_root, job)
    return FigureArtifact(
        artifact.path,
        artifact.metadata,
        artifact.prompt,
        artifact.provider_result,
        artifact.output,
        cached=job.cached,
    )


# ponytail: process-local locks match the single desktop server; use an OS lock
# only if multiple server processes are allowed to share one workspace.
_JOB_LOCKS: dict[Path, threading.Lock] = {}
_JOB_LOCKS_GUARD = threading.Lock()


def _job_lock(path: Path) -> threading.Lock:
    resolved = path.resolve()
    with _JOB_LOCKS_GUARD:
        return _JOB_LOCKS.setdefault(resolved, threading.Lock())


def _create_job(
    item_path: Path,
    *,
    block_id: str,
    source_asset: Mapping[str, Any],
    source_image: ValidatedImage,
    prompt: str,
    profile_name: str,
    profile: FigureProfile,
    remove_blue_text: bool,
    requested_revision: int,
    adapter: FigureGenerationAdapter | None,
) -> FigureGenerationJob:
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    key = _idempotency_key(source_image.sha256, prompt_sha256, profile.model, block_id)
    job_id = f"figure-generation-{key[:32]}"
    item_dir = item_path.parent.resolve()
    jobs_root = _owned_path(item_dir, "jobs", "figure_generation")
    final_path = jobs_root / job_id
    with _job_lock(final_path):
        if final_path.exists():
            return load_figure_job(final_path, cached=True)
        adapter = adapter or GeminiFigureAdapter()
        if getattr(adapter, "provider_name", None) != "gemini":
            raise FigureGenerationError("figure generation adapter must be gemini")
        created_jobs_root = _owned_directory(item_dir, "jobs", "figure_generation")
        if created_jobs_root != jobs_root:
            raise FigureGenerationError("figure generation job path changed during request")
        try:
            free_bytes = shutil.disk_usage(jobs_root).free
        except OSError as exc:
            raise FigureGenerationError("figure generation storage cannot be inspected") from exc
        if free_bytes < MAX_GENERATED_IMAGE_BYTES + 1024 * 1024:
            raise FigureGenerationError("figure generation job storage has insufficient free space")
        try:
            temporary_job = tempfile.TemporaryDirectory(prefix=".figure-", dir=jobs_root)
        except OSError as exc:
            raise FigureGenerationError("figure generation job storage is not writable") from exc
        with temporary_job as temporary:
            staging = Path(temporary)
            try:
                probe = staging / ".write-test"
                with probe.open("xb") as stream:
                    stream.write(b"figure-generation-preflight")
                    stream.flush()
                    os.fsync(stream.fileno())
                probe.unlink()
            except OSError as exc:
                raise FigureGenerationError("figure generation job storage is not writable") from exc
            started_at = utc_now()
            generated = adapter.generate(
                image=source_image,
                prompt=prompt,
                model=profile.model,
                image_size=IMAGE_SIZE,
            )
            if not isinstance(generated, GeneratedImage):
                raise FigureProviderRequestError("그림 provider가 올바른 결과 형식을 반환하지 않았습니다.")
            if not isinstance(generated.provider_result, Mapping):
                raise FigureProviderRequestError("그림 provider metadata 형식이 올바르지 않습니다.")
            provider_result = _sanitized_provider_result(generated.provider_result)
            provider_result["mime_type"] = generated.media_type
            output = _normalize_png(generated.data, generated.media_type, staging / "output.png")
            (staging / "prompt.txt").write_text(prompt, encoding="utf-8", newline="\n")
            atomic_write_json(staging / "provider-result.json", provider_result)
            completed_at = utc_now()
            metadata = {
                "schema_version": "0.1.0",
                "job_id": job_id,
                "idempotency_key": key,
                "status": "succeeded",
                "provider": "gemini",
                "profile": profile_name,
                "model": profile.model,
                "image_size": IMAGE_SIZE,
                "block_id": block_id,
                "requested_revision": requested_revision,
                "source_asset_id": source_asset.get("asset_id"),
                "source_sha256": source_image.sha256,
                "source_media_type": source_image.media_type,
                "source_width_px": source_image.width_px,
                "source_height_px": source_image.height_px,
                "source_size_bytes": source_image.size_bytes,
                "remove_blue_text": remove_blue_text,
                "prompt_version": PROMPT_VERSION,
                "prompt_sha256": sha256_file(staging / "prompt.txt"),
                "provider_result_sha256": sha256_file(staging / "provider-result.json"),
                "provider_output_mime_type": generated.media_type,
                "output_sha256": output.sha256,
                "output_width_px": output.width_px,
                "output_height_px": output.height_px,
                "output_size_bytes": output.size_bytes,
                "started_at": started_at,
                "completed_at": completed_at,
            }
            if metadata["prompt_sha256"] != prompt_sha256:
                raise FigureJobIntegrityError("stored figure prompt bytes changed unexpectedly")
            atomic_write_json(staging / "job.json", metadata)
            (staging / "job.sha256").write_text(
                sha256_file(staging / "job.json") + "\n",
                encoding="ascii",
                newline="\n",
            )
            try:
                os.rename(staging, final_path)
            except FileExistsError:
                pass
        return load_figure_job(final_path)


def _asset_record(
    job: FigureGenerationJob,
    *,
    asset_id: str,
    section: str,
    source_asset_id: str,
    original_asset_id: str,
    relative_path: str,
) -> dict[str, Any]:
    metadata = job.metadata
    return {
        "asset_id": asset_id,
        "section": section,
        "role": "figure_regenerated",
        "state": "ready",
        "path": relative_path,
        "media_type": "image/png",
        "sha256": job.output.sha256,
        "width_px": job.output.width_px,
        "height_px": job.output.height_px,
        "derived_from_asset_id": source_asset_id,
        "generation": {
            "mode": "regenerate",
            "provider": "gemini",
            "model": metadata["model"],
            "prompt_version": metadata["prompt_version"],
            "instruction": "reference pixels authoritative; no solving; no new labels",
            "remove_blue_text": metadata["remove_blue_text"],
            "status": "succeeded",
            "fallback_asset_id": original_asset_id,
            "failure_reason": None,
            "job_id": metadata["job_id"],
            "started_at": metadata["started_at"],
            "completed_at": metadata["completed_at"],
            "prompt_text": job.prompt,
            "prompt_sha256": metadata["prompt_sha256"],
        },
        "review": {
            "status": "unreviewed",
            "boundary_uncertain": True,
            "notes": [MANUAL_REVIEW_NOTE],
        },
    }


def materialize_figure_artifact_cas(
    item_path: Path | str,
    expected_revision: int,
    block_id: str,
    artifact: FigureArtifact,
) -> tuple[dict[str, Any], str, bool]:
    """Append one validated global artifact as an unselected local candidate."""

    path = Path(item_path).resolve()
    artifact = load_figure_artifact(artifact.path, cached=artifact.cached)
    current = load_json_object(path)
    if _workflow_revision(current) != expected_revision:
        raise RevisionConflict(expected_revision, _workflow_revision(current))
    section, block, source, image = _safe_source(path, current, block_id)
    if section != "question":
        raise FigureGenerationError("selective figure generation is question-only")
    prompt = _render_prompt(block.get("alt_text"), False, DEFAULT_PROMPT_PATH)
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    expected_key = figure_artifact_key(
        image.sha256,
        prompt_sha256,
        str(artifact.metadata["model"]),
    )
    if (
        artifact.metadata.get("artifact_key") != expected_key
        or artifact.metadata.get("profile") != "quality"
        or artifact.metadata.get("remove_blue_text") is not False
    ):
        raise FigureJobIntegrityError("figure artifact does not match this target")
    original_id = block.get("original_asset_id")
    source_id = source.get("asset_id")
    if not isinstance(original_id, str) or not isinstance(source_id, str):
        raise FigureGenerationError("figure lineage identifiers are invalid")
    assets, by_id = _assets(current)
    semantic_existing = sorted(
        (
            asset
            for asset in assets
            if isinstance(asset, Mapping)
            and asset.get("role") == "figure_regenerated"
            and asset.get("state") == "ready"
            and asset.get("section") in {section, "shared"}
            and asset.get("derived_from_asset_id") == source_id
            and isinstance(asset.get("generation"), Mapping)
            and asset["generation"].get("mode") == "regenerate"
            and asset["generation"].get("model") == artifact.metadata["model"]
            and asset["generation"].get("prompt_version")
            == artifact.metadata["prompt_version"]
            and asset["generation"].get("prompt_sha256")
            == artifact.metadata["prompt_sha256"]
            and asset["generation"].get("remove_blue_text") is False
        ),
        key=lambda asset: str(asset.get("asset_id")),
    )
    if semantic_existing:
        existing = semantic_existing[0]
        existing_path = _safe_asset_path(path.parent, existing.get("path"))
        if (
            existing_path.is_symlink()
            or not existing_path.is_file()
            or sha256_file(existing_path) != existing.get("sha256")
        ):
            raise FigureJobIntegrityError("existing semantic figure candidate is invalid")
        return current, str(existing["asset_id"]), False
    asset_key = hashlib.sha256(
        json.dumps(
            [artifact.metadata["artifact_key"], source_id],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    asset_id = validate_id(f"figure-ai-{asset_key}", field="asset_id")
    relative = f"assets/{asset_id}.png"
    job_metadata = {
        "model": artifact.metadata["model"],
        "prompt_version": artifact.metadata["prompt_version"],
        "remove_blue_text": False,
        "job_id": f"figure-artifact-{artifact.metadata['artifact_key'][:32]}",
        "started_at": artifact.metadata["started_at"],
        "completed_at": artifact.metadata["completed_at"],
        "prompt_sha256": artifact.metadata["prompt_sha256"],
    }
    job = FigureGenerationJob(
        artifact.path,
        job_metadata,
        artifact.prompt,
        artifact.provider_result,
        artifact.output,
        cached=True,
    )
    record = _asset_record(
        job,
        asset_id=asset_id,
        section=section,
        source_asset_id=source_id,
        original_asset_id=original_id,
        relative_path=relative,
    )
    existing = by_id.get(asset_id)
    if existing is not None:
        immutable_record = {key: value for key, value in record.items() if key != "review"}
        immutable_existing = {
            key: value for key, value in existing.items() if key != "review"
        }
        if immutable_existing != immutable_record:
            raise FigureJobIntegrityError(
                f"figure regenerated asset ID collision: {asset_id}"
            )
        existing_path = _safe_asset_path(path.parent, existing.get("path"))
        if (
            existing_path.is_symlink()
            or not existing_path.is_file()
            or sha256_file(existing_path) != artifact.output.sha256
        ):
            raise FigureJobIntegrityError("existing generated figure file is invalid")
        return current, asset_id, False

    candidate = copy.deepcopy(current)
    candidate_assets, _ = _assets(candidate)
    candidate_assets.append(record)
    review = candidate.get("review")
    if isinstance(review, dict):
        review["assets"] = "unreviewed"
    assets_dir = _owned_directory(path.parent.resolve(), "assets")
    destination = assets_dir / f"{asset_id}.png"
    if destination.is_symlink():
        raise FigureJobIntegrityError("generated figure destination cannot be a symlink")
    created_destination = False
    try:
        try:
            output_stream = destination.open("xb")
        except FileExistsError:
            if (
                destination.is_symlink()
                or not destination.is_file()
                or sha256_file(destination) != artifact.output.sha256
            ):
                raise FigureJobIntegrityError("generated figure file collision")
        else:
            created_destination = True
            with artifact.output.path.open("rb") as source_stream, output_stream:
                shutil.copyfileobj(source_stream, output_stream, length=1024 * 1024)
                output_stream.flush()
                os.fsync(output_stream.fileno())
        saved = save_json_cas(path, candidate, expected_revision)
        return saved, asset_id, True
    except Exception:
        if created_destination:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        raise


def regenerate_figure_cas(
    item_path: Path | str,
    expected_revision: int,
    block_id: str,
    profile: str,
    remove_blue_text: bool,
    *,
    adapter: FigureGenerationAdapter | None = None,
    prompt_path: Path | str = DEFAULT_PROMPT_PATH,
) -> tuple[dict[str, Any], str, FigureGenerationJob, bool]:
    """Generate once, then append an unselected candidate with CAS protection."""

    item_path = Path(item_path).resolve()
    validate_id(block_id, field="block_id")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise FigureGenerationError("expected_revision must be an integer")
    if expected_revision < 1:
        raise FigureGenerationError("expected_revision must be positive")
    if not isinstance(profile, str) or profile not in FIGURE_PROFILES:
        raise FigureGenerationError("unknown figure generation profile")
    if not isinstance(remove_blue_text, bool):
        raise FigureGenerationError("remove_blue_text must be a boolean")
    current = load_json_object(item_path)
    current_revision = _workflow_revision(current)
    if current_revision != expected_revision:
        raise RevisionConflict(expected_revision, current_revision)
    section, block, source_asset, source_image = _safe_source(item_path, current, block_id)
    prompt = _render_prompt(block.get("alt_text"), remove_blue_text, prompt_path)
    selected_profile = FIGURE_PROFILES[profile]
    try:
        job = _create_job(
            item_path,
            block_id=block_id,
            source_asset=source_asset,
            source_image=source_image,
            prompt=prompt,
            profile_name=profile,
            profile=selected_profile,
            remove_blue_text=remove_blue_text,
            requested_revision=expected_revision,
            adapter=adapter,
        )
    except OSError as exc:
        raise FigureGenerationError("figure generation job could not be stored") from exc
    latest_revision = _workflow_revision(load_json_object(item_path))
    if latest_revision != expected_revision:
        raise RevisionConflict(expected_revision, latest_revision)
    _, _, source_again, verified_again = _safe_source(item_path, current, block_id)
    if source_again.get("asset_id") != source_asset.get("asset_id"):
        raise FigureGenerationError("재생성 중 안전 원본 선택이 변경되었습니다.")
    if any(
        getattr(verified_again, field) != getattr(source_image, field)
        for field in ("sha256", "width_px", "height_px", "size_bytes")
    ):
        raise FigureGenerationError("재생성 중 원본 그림 파일이 변경되었습니다.")
    original_id = block.get("original_asset_id")
    if not isinstance(original_id, str):
        raise FigureGenerationError("figure original_asset_id must be a string")
    source_id = source_asset.get("asset_id")
    if not isinstance(source_id, str):
        raise FigureGenerationError("figure source asset_id must be a string")
    asset_key = hashlib.sha256(
        json.dumps(
            [job.metadata["idempotency_key"], source_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    asset_id = validate_id(f"figure-ai-{asset_key}", field="asset_id")
    relative = f"assets/{asset_id}.png"
    record = _asset_record(
        job,
        asset_id=asset_id,
        section=section,
        source_asset_id=source_id,
        original_asset_id=original_id,
        relative_path=relative,
    )
    _, by_id = _assets(current)
    existing = by_id.get(asset_id)
    if existing is not None:
        immutable_record = {key: value for key, value in record.items() if key != "review"}
        immutable_existing = {key: value for key, value in existing.items() if key != "review"}
        if immutable_existing != immutable_record:
            raise FigureJobIntegrityError(f"figure regenerated asset ID collision: {asset_id}")
        existing_path = _safe_asset_path(item_path.parent, existing.get("path"))
        if existing_path.is_symlink() or not existing_path.is_file():
            raise FigureJobIntegrityError("existing generated figure file is missing")
        existing_image = inspect_image(
            existing_path,
            expected_media_type="image/png",
            max_bytes=MAX_GENERATED_IMAGE_BYTES,
            max_pixels=MAX_GENERATED_IMAGE_PIXELS,
        )
        if any(existing.get(field) != getattr(existing_image, field) for field in ("sha256", "width_px", "height_px")):
            raise FigureJobIntegrityError("existing generated figure metadata does not match its file")
        return current, asset_id, job, False

    candidate = copy.deepcopy(current)
    candidate_assets, _ = _assets(candidate)
    candidate_assets.append(record)
    review = candidate.get("review")
    if isinstance(review, dict):
        review["assets"] = "unreviewed"

    assets_dir = _owned_directory(item_path.parent.resolve(), "assets")
    destination = assets_dir / f"{asset_id}.png"
    if destination.is_symlink():
        raise FigureJobIntegrityError("generated figure destination cannot be a symlink")
    with _job_lock(job.path):
        created_destination = False
        try:
            try:
                output_stream = destination.open("xb")
            except FileExistsError:
                if (
                    destination.is_symlink()
                    or not destination.is_file()
                    or sha256_file(destination) != job.output.sha256
                ):
                    raise FigureJobIntegrityError("generated figure file collision")
            else:
                created_destination = True
                with job.output.path.open("rb") as source_stream, output_stream:
                    shutil.copyfileobj(source_stream, output_stream, length=1024 * 1024)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
            try:
                copied = inspect_image(
                    destination,
                    expected_media_type="image/png",
                    max_bytes=MAX_GENERATED_IMAGE_BYTES,
                    max_pixels=MAX_GENERATED_IMAGE_PIXELS,
                )
            except ImageValidationError as exc:
                raise FigureJobIntegrityError(str(exc)) from exc
            if any(
                getattr(copied, field) != getattr(job.output, field)
                for field in ("sha256", "width_px", "height_px", "size_bytes")
            ):
                raise FigureJobIntegrityError("copied generated figure does not match its job output")
            saved = save_json_cas(item_path, candidate, expected_revision)
            return saved, asset_id, job, True
        except Exception as exc:
            if created_destination:
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
            if isinstance(exc, OSError):
                raise FigureGenerationError("generated figure asset could not be stored") from exc
            raise
