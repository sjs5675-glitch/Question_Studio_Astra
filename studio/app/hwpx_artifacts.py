"""Provision approved defaults and publish content-bound HWPX build artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .hwpx_export import (
    NATIVE_RENDERER_ENGINE_FINGERPRINT_SHA256,
    NATIVE_RENDERER_ENGINE_ID,
    ExportPreflightError,
    preflight_export,
)
from .hwpx_renderer import HwpxRenderError, render_export_plan
from .model import ModelError, RevisionConflict, load_json_object, save_json_cas


APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = APP_ROOT / "templates" / "neutral-a4-v1.hwpx"
DEFAULT_TEMPLATE_SHA256 = "f65d25302078b566ef0a456afc88d95adcf3ceccc700b9d434823297795aaf2e"
DEFAULT_PROFILE_REFS = {
    "question": "profiles/neutral-question-v1.json",
    "explanation": "profiles/neutral-explanation-v1.json",
}
LEGACY_DEFAULT_ENGINE_CONTRACTS = frozenset(
    {
        ("question-json-studio.native_ordered.v1.0", "fe0c725b292d271d7c7f952b0503eb23bf3038e9b8038cae0383bf820a443b5d"),
        ("question-json-studio.native_ordered.v0.9", "def5f90357e4ef2ca5741526d2e05ce65656acde74889f343d6391483e8b34b8"),
        (
            "question-json-studio.native_ordered.v0.1",
            "fd8bbfd55bdf6f52144f8f2dfa8b7692a05b3c3699977ef4bf802832d1c9a8b9",
        ),
        (
            "question-json-studio.native_ordered.v0.2",
            "b4777afe508a5a049e41817eba577ce73414df532dfe2afe6c067ca8069a2bf5",
        ),
        (
            "question-json-studio.native_ordered.v0.2",
            "b09f07a04f37580e240ff994231946e53b4c1427d68d995356b57a9aa1ba264e",
        ),
        (
            "question-json-studio.native_ordered.v0.2",
            "085ad7592718bd4f2a6bfc8ef65eede58c0f8cd86e30b3c80ea5dcb979c29819",
        ),
        (
            "question-json-studio.native_ordered.v0.3",
            "be5770717dce4a3d29f37452d4d36a65a126c10fea70a8977eefdb0460c6bafe",
        ),
        (
            "question-json-studio.native_ordered.v0.3",
            "4a99bf4a35a28d65188e66a2d40223f25ce1dfaf58b1439e0d9f0d833f23db96",
        ),
        (
            "question-json-studio.native_ordered.v0.4",
            "b021b4fbeb37179f52f2666bb493f1754bb6a7dc1d56d6984ef4e39549420e4a",
        ),
        (
            "question-json-studio.native_ordered.v0.5",
            "29a951198124dec1135b186d04a752af7490c9bc82d48db832e052f7e1aec615",
        ),
    }
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CHUNK_SIZE = 1024 * 1024


class HwpxArtifactError(ValueError):
    """Raised when an HWPX artifact path or immutable build cannot be trusted."""


@dataclass(frozen=True)
class HwpxArtifact:
    artifact_kind: str
    path: Path
    name: str
    attachment_name: str
    media_type: str
    size: int
    sha256: str


@dataclass(frozen=True)
class HwpxBuild:
    build_id: str
    build_fingerprint_sha256: str
    receipt: Mapping[str, Any]
    receipt_artifact: HwpxArtifact
    outputs: tuple[HwpxArtifact, HwpxArtifact]
    cached: bool


# ponytail: one process-local lock matches this single-user desktop server.
_BUILD_LOCK = threading.Lock()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _measure(path: Path) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            current = os.stat(path, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise HwpxArtifactError("HWPX 산출물 경로가 읽기 전에 변경되었습니다.")
            for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise HwpxArtifactError("HWPX 산출물을 읽을 수 없습니다.") from exc
    return size, digest.hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HwpxArtifactError("HWPX build receipt를 직렬화할 수 없습니다.") from exc


def _safe_root(project_dir: Path | str) -> Path:
    candidate = Path(project_dir)
    if _is_link(candidate):
        raise HwpxArtifactError("프로젝트 폴더에 링크를 사용할 수 없습니다.")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise HwpxArtifactError("프로젝트 폴더를 찾을 수 없습니다.") from exc
    if not resolved.is_dir():
        raise HwpxArtifactError("프로젝트 폴더를 찾을 수 없습니다.")
    return resolved


def _safe_directory(root: Path, *parts: str, create: bool = False) -> Path:
    current = root
    for part in parts:
        if not part or part in {".", ".."} or "/" in part or "\\" in part or ":" in part:
            raise HwpxArtifactError("HWPX 산출물 폴더 경로가 올바르지 않습니다.")
        current /= part
        if current.exists():
            if _is_link(current) or not current.is_dir():
                raise HwpxArtifactError("HWPX 산출물 경로가 안전한 폴더가 아닙니다.")
        elif create:
            try:
                current.mkdir()
            except FileExistsError:
                if _is_link(current) or not current.is_dir():
                    raise HwpxArtifactError("HWPX 산출물 경로가 안전한 폴더가 아닙니다.")
            except OSError as exc:
                raise HwpxArtifactError("HWPX 산출물 폴더를 만들 수 없습니다.") from exc
        else:
            raise HwpxArtifactError("HWPX 산출물 폴더를 찾을 수 없습니다.")
    try:
        current.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise HwpxArtifactError("HWPX 산출물 폴더가 프로젝트 밖을 가리킵니다.") from exc
    return current


def _safe_file(root: Path, name: str) -> Path:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
    ):
        raise HwpxArtifactError("HWPX 산출물 파일명이 올바르지 않습니다.")
    candidate = root / name
    if _is_link(candidate):
        raise HwpxArtifactError("HWPX 산출물에 링크를 사용할 수 없습니다.")
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise HwpxArtifactError("HWPX 산출물 파일을 찾을 수 없습니다.") from exc
    if not candidate.is_file():
        raise HwpxArtifactError("HWPX 산출물 파일을 찾을 수 없습니다.")
    return candidate


def _write_once(path: Path, data: bytes, *, label: str) -> None:
    if path.exists():
        if _is_link(path) or not path.is_file():
            raise HwpxArtifactError(f"{label} 경로가 안전한 파일이 아닙니다.")
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise HwpxArtifactError(f"{label} 파일을 읽을 수 없습니다.") from exc
        if current != data:
            raise HwpxArtifactError(f"기존 {label} 파일이 승인 기본값과 다릅니다.")
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if _is_link(path.parent):
            raise HwpxArtifactError(f"{label} 폴더에 링크를 사용할 수 없습니다.")
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        _write_once(path, data, label=label)
    except OSError as exc:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HwpxArtifactError(f"{label} 파일을 만들 수 없습니다.") from exc


def _default_profile(kind: str) -> dict[str, Any]:
    return {
        "schema_version": "0.1.0",
        "profile_id": f"neutral-{kind}-v1",
        "document_kind": kind,
        "approval": {
            "status": "approved",
            "reviewer": "user-approved-default",
            "approved_at": "2026-08-25T00:00:00Z",
        },
        "template": {
            "source": "user_approved_clean_hwpx",
            "path": "templates/neutral-a4-v1.hwpx",
            "sha256": DEFAULT_TEMPLATE_SHA256,
        },
        "design": {
            "page": {
                "width_mm": 210,
                "height_mm": 297,
                "orientation": "portrait",
                "margins_mm": {"top": 18, "right": 20, "bottom": 18, "left": 20},
            },
            "columns": {"count": 1, "gap_mm": 0, "mixed_columns": False},
            "flow": {
                "section_count": 1,
                "mode": "natural",
                "figure_placement": "inline",
                "choice_layout": "source",
                "unsupported_table": "error",
                "page_break_between_items": False,
            },
            "styles": {
                "paragraph": "neutral-paragraph",
                "equation": "neutral-equation",
                "choice": "neutral-choice",
                "figure_caption": "neutral-figure-caption",
            },
        },
    }


def _provision_defaults(project_dir: Path) -> bool:
    """Fill only null HWPX settings and leave every non-null custom value untouched."""

    project_path = _safe_file(project_dir, "project.json")
    for _ in range(3):
        project = load_json_object(project_path)
        rendering = project.get("rendering")
        export = project.get("export")
        workflow = project.get("workflow")
        if not isinstance(rendering, dict) or not isinstance(export, dict):
            raise HwpxArtifactError("project rendering/export 설정이 올바르지 않습니다.")
        revision = workflow.get("revision") if isinstance(workflow, Mapping) else None
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise HwpxArtifactError("project workflow revision이 올바르지 않습니다.")

        missing_kinds = [
            kind
            for kind, key in (
                ("question", "question_design_profile_ref"),
                ("explanation", "explanation_design_profile_ref"),
            )
            if rendering.get(key) is None
        ]
        engine_contract = (
            export.get("engine_id"),
            export.get("engine_fingerprint_sha256"),
        )
        needs_engine = (
            engine_contract[0] is None
            or engine_contract[1] is None
            or engine_contract in LEGACY_DEFAULT_ENGINE_CONTRACTS
        )
        if not missing_kinds and not needs_engine:
            return False

        if missing_kinds:
            try:
                template_data = DEFAULT_TEMPLATE.read_bytes()
            except OSError as exc:
                raise HwpxArtifactError("승인된 중립 HWPX 템플릿을 읽을 수 없습니다.") from exc
            if _sha256_bytes(template_data) != DEFAULT_TEMPLATE_SHA256:
                raise HwpxArtifactError("승인된 중립 HWPX 템플릿의 SHA-256이 일치하지 않습니다.")
            profiles = _safe_directory(project_dir, "profiles", create=True)
            templates = _safe_directory(profiles, "templates", create=True)
            _write_once(
                templates / "neutral-a4-v1.hwpx",
                template_data,
                label="중립 HWPX 템플릿",
            )
            for kind in missing_kinds:
                _write_once(
                    profiles / f"neutral-{kind}-v1.json",
                    _canonical_json(_default_profile(kind)),
                    label=f"{kind} 중립 디자인 profile",
                )

        candidate = copy.deepcopy(project)
        candidate_rendering = candidate["rendering"]
        for kind, key in (
            ("question", "question_design_profile_ref"),
            ("explanation", "explanation_design_profile_ref"),
        ):
            if candidate_rendering.get(key) is None:
                candidate_rendering[key] = DEFAULT_PROFILE_REFS[kind]
        candidate_export = candidate["export"]
        candidate_engine_contract = (
            candidate_export.get("engine_id"),
            candidate_export.get("engine_fingerprint_sha256"),
        )
        if (
            candidate_export.get("engine_id") is None
            or candidate_engine_contract in LEGACY_DEFAULT_ENGINE_CONTRACTS
        ):
            candidate_export["engine_id"] = NATIVE_RENDERER_ENGINE_ID
        if (
            candidate_export.get("engine_fingerprint_sha256") is None
            or candidate_engine_contract in LEGACY_DEFAULT_ENGINE_CONTRACTS
        ):
            candidate_export["engine_fingerprint_sha256"] = (
                NATIVE_RENDERER_ENGINE_FINGERPRINT_SHA256
            )
        try:
            save_json_cas(project_path, candidate, revision)
            return True
        except RevisionConflict:
            continue
    raise HwpxArtifactError("프로젝트 설정이 동시에 변경되어 기본 HWPX 설정을 저장하지 못했습니다.")


def _receipt(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HwpxArtifactError("HWPX build receipt를 읽을 수 없습니다.") from exc
    if not isinstance(value, dict) or raw != _canonical_json(value):
        raise HwpxArtifactError("HWPX build receipt 형식이 올바르지 않습니다.")
    return value, raw


def _output_entry(receipt: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    outputs = receipt.get("outputs")
    matches = [
        entry
        for entry in outputs
        if isinstance(entry, Mapping) and entry.get("document_kind") == kind
    ] if isinstance(outputs, list) else []
    if len(matches) != 1:
        raise HwpxArtifactError(f"receipt에 {kind} HWPX가 정확히 하나 있어야 합니다.")
    return matches[0]


def _output_artifact(
    build_dir: Path,
    receipt: Mapping[str, Any],
    project_id: str,
    kind: str,
) -> HwpxArtifact:
    entry = _output_entry(receipt, kind)
    name = entry.get("path")
    expected_sha = entry.get("sha256")
    expected_size = entry.get("size_bytes")
    if not isinstance(name, str) or not name.casefold().endswith(".hwpx"):
        raise HwpxArtifactError(f"receipt의 {kind} HWPX 파일명이 올바르지 않습니다.")
    if not isinstance(expected_sha, str) or not SHA256_RE.fullmatch(expected_sha):
        raise HwpxArtifactError(f"receipt의 {kind} HWPX SHA-256이 올바르지 않습니다.")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 1:
        raise HwpxArtifactError(f"receipt의 {kind} HWPX 크기가 올바르지 않습니다.")
    path = _safe_file(build_dir, name)
    size, digest = _measure(path)
    if size != expected_size or digest != expected_sha:
        raise HwpxArtifactError(f"{kind} HWPX가 build receipt와 일치하지 않습니다.")
    attachment = f"{project_id}-{kind}-{digest[:16]}.hwpx"
    return HwpxArtifact(
        artifact_kind=kind,
        path=path,
        name=name,
        attachment_name=attachment,
        media_type="application/hwp+zip",
        size=size,
        sha256=digest,
    )


def _load_build(
    build_dir: Path,
    fingerprint: str,
    project_id: str,
    *,
    cached: bool,
) -> HwpxBuild:
    if _is_link(build_dir) or not build_dir.is_dir():
        raise HwpxArtifactError("HWPX build 폴더가 안전하지 않습니다.")
    receipt_path = _safe_file(build_dir, "receipt.json")
    receipt, receipt_bytes = _receipt(receipt_path)
    if (
        receipt.get("status") != "built"
        or receipt.get("build_fingerprint_sha256") != fingerprint
        or not isinstance(receipt.get("project"), Mapping)
        or receipt["project"].get("project_id") != project_id
    ):
        raise HwpxArtifactError("HWPX build receipt가 요청한 프로젝트·입력과 일치하지 않습니다.")
    outputs = tuple(
        _output_artifact(build_dir, receipt, project_id, kind)
        for kind in ("question", "explanation")
    )
    expected_names = {"receipt.json", *(artifact.name for artifact in outputs)}
    try:
        actual_names = {path.name for path in build_dir.iterdir()}
    except OSError as exc:
        raise HwpxArtifactError("HWPX build 폴더를 읽을 수 없습니다.") from exc
    if actual_names != expected_names:
        raise HwpxArtifactError("HWPX build 폴더에 선언되지 않은 파일이 있습니다.")
    receipt_sha = _sha256_bytes(receipt_bytes)
    receipt_artifact = HwpxArtifact(
        artifact_kind="receipt",
        path=receipt_path,
        name="receipt.json",
        attachment_name=f"{project_id}-receipt-{receipt_sha[:16]}.json",
        media_type="application/json; charset=utf-8",
        size=len(receipt_bytes),
        sha256=receipt_sha,
    )
    return HwpxBuild(
        build_id=str(receipt.get("build_id")),
        build_fingerprint_sha256=fingerprint,
        receipt=receipt,
        receipt_artifact=receipt_artifact,
        outputs=(outputs[0], outputs[1]),
        cached=cached,
    )


def _remove_stage(stage: Path, root: Path) -> None:
    if stage.parent != root or not stage.name.startswith(".build-"):
        return
    try:
        if _is_link(stage):
            stage.unlink(missing_ok=True)
        elif stage.exists():
            shutil.rmtree(stage)
    except OSError:
        pass


def create_hwpx_build(project_dir: Path | str) -> HwpxBuild:
    """Provision null defaults, run safety preflight, and publish an immutable pair."""

    with _BUILD_LOCK:
        project_dir = _safe_root(project_dir)
        _provision_defaults(project_dir)
        provisional = preflight_export(project_dir)
        fingerprint = str(provisional.receipt["build_fingerprint_sha256"])
        if not SHA256_RE.fullmatch(fingerprint):
            raise HwpxArtifactError("HWPX build fingerprint가 올바르지 않습니다.")
        project_id = provisional.project_id
        if not ID_RE.fullmatch(project_id):
            raise HwpxArtifactError("project_id가 HWPX 다운로드 규칙과 맞지 않습니다.")
        root = _safe_directory(project_dir, "outputs", "hwpx", create=True)
        build_name = f"build-{fingerprint}"
        build_dir = root / build_name
        if build_dir.exists():
            return _load_build(build_dir, fingerprint, project_id, cached=True)
        if _is_link(build_dir):
            raise HwpxArtifactError("HWPX build 경로에 링크를 사용할 수 없습니다.")

        stage = root / f".build-{uuid.uuid4().hex[:12]}.tmp"
        try:
            plan = preflight_export(project_dir, stage)
            if plan.receipt.get("build_fingerprint_sha256") != fingerprint:
                raise HwpxArtifactError("HWPX 입력이 build 계획 중 변경되었습니다.")
            receipt = render_export_plan(plan)
            if receipt.get("build_fingerprint_sha256") != fingerprint:
                raise HwpxArtifactError("렌더러 receipt가 build 계획과 일치하지 않습니다.")
            _write_once(stage / "receipt.json", _canonical_json(receipt), label="HWPX build receipt")
            if build_dir.exists() or _is_link(build_dir):
                raise HwpxArtifactError("동일 fingerprint의 HWPX build가 이미 존재합니다.")
            try:
                stage.rename(build_dir)
            except OSError as exc:
                raise HwpxArtifactError("HWPX build를 원자적으로 게시할 수 없습니다.") from exc
            return _load_build(build_dir, fingerprint, project_id, cached=False)
        finally:
            _remove_stage(stage, root)


def resolve_hwpx_artifact(
    project_dir: Path | str,
    project_id: str,
    fingerprint: str,
    artifact_kind: str,
    expected_sha256: str,
) -> HwpxArtifact:
    """Resolve one content-bound artifact without accepting a filesystem path."""

    if not isinstance(project_id, str) or not ID_RE.fullmatch(project_id):
        raise HwpxArtifactError("project_id가 올바르지 않습니다.")
    if not isinstance(fingerprint, str) or not SHA256_RE.fullmatch(fingerprint):
        raise HwpxArtifactError("HWPX build fingerprint가 올바르지 않습니다.")
    if artifact_kind not in {"question", "explanation", "receipt"}:
        raise HwpxArtifactError("HWPX artifact 종류가 올바르지 않습니다.")
    if not isinstance(expected_sha256, str) or not SHA256_RE.fullmatch(expected_sha256):
        raise HwpxArtifactError("HWPX artifact SHA-256이 올바르지 않습니다.")
    project_dir = _safe_root(project_dir)
    root = _safe_directory(project_dir, "outputs", "hwpx")
    build_dir = _safe_directory(root, f"build-{fingerprint}")
    build = _load_build(build_dir, fingerprint, project_id, cached=True)
    artifact = (
        build.receipt_artifact
        if artifact_kind == "receipt"
        else next(value for value in build.outputs if value.artifact_kind == artifact_kind)
    )
    if artifact.sha256 != expected_sha256:
        raise HwpxArtifactError("HWPX artifact의 content-bound SHA-256이 일치하지 않습니다.")
    return artifact


__all__ = [
    "ExportPreflightError",
    "HwpxArtifact",
    "HwpxArtifactError",
    "HwpxBuild",
    "HwpxRenderError",
    "create_hwpx_build",
    "resolve_hwpx_artifact",
]
