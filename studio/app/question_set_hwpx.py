"""Immutable HWPX builds for ordered question-set references.

Question packages remain owned by their source projects.  A build takes a
private snapshot, changes only the snapshot's visible number, and reuses the
approved native renderer with the clean problem-bank two-column template.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .hwpx_artifacts import (
    DEFAULT_TEMPLATE,
    DEFAULT_TEMPLATE_SHA256,
    _default_profile,
)
from .hwpx_export import (
    NATIVE_RENDERER_ENGINE_FINGERPRINT_SHA256,
    NATIVE_RENDERER_ENGINE_ID,
    ExportPlan,
    ExportPreflightError,
    preflight_export,
)
from .hwpx_renderer import (
    HwpxRenderError,
    _LoadedItem,
    _Renderer,
    _fresh_plan,
    _inspect_package,
    _json_bytes,
    _load_template_context,
    _package_bytes,
    _read_exact,
    render_export_plan,
)
from .hwpx_validation import validate_hwpx_output
from .model import ModelError, validate_id


RECEIPT_VERSION = "0.2.0"
QUESTION_SET_RENDER_CONTRACT = "question-set-native-ordered.v0.4.0"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SUBQUESTION_RE = re.compile(r"^([^()]+?)\s*\(\s*(\d+)\s*\)\s*$")
CHUNK_SIZE = 1024 * 1024
PROBLEM_BANK_TEMPLATE_NAME = "problem-bank-a4-2col-v1.hwpx"
PROBLEM_BANK_TEMPLATE_SHA256 = "57765699a46164e8fc2b65a4cf9b30080ca7adacded106630808cdb9e54dfccb"
PROBLEM_BANK_PAGE = {
    "width_mm": 210,
    "height_mm": 297,
    "orientation": "portrait",
    "margins_mm": {"top": 8, "right": 13, "bottom": 8, "left": 13},
}
PROBLEM_BANK_COLUMNS = {"count": 2, "gap_mm": 8, "mixed_columns": False}


class QuestionSetHwpxError(ValueError):
    """A question-set reference or immutable artifact was not trustworthy."""


def _problem_bank_template_bytes() -> bytes:
    """Derive the clean two-column bank template from the approved neutral base."""

    source = DEFAULT_TEMPLATE.read_bytes()
    if _sha256_bytes(source) != DEFAULT_TEMPLATE_SHA256:
        raise QuestionSetHwpxError("기본 중립 HWPX 템플릿의 SHA-256이 일치하지 않습니다.")
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        infos = archive.infolist()
        members = {info.filename: archive.read(info.filename) for info in infos}
    section = members["Contents/section0.xml"].decode("utf-8")
    replacements = (
        ('spaceColumns="0"', 'spaceColumns="1134"'),
        (
            '<hp:pagePr landscape="WIDELY" width="59528" height="84189" gutterType="LEFT_ONLY">',
            '<hp:pagePr landscape="WIDELY" width="59528" height="84189" gutterType="LEFT_ONLY">',
        ),
        (
            '<hp:margin header="0" footer="0" gutter="0" left="5669" right="5669" top="5102" bottom="5102"/>',
            '<hp:margin header="2551" footer="3685" gutter="0" left="3685" right="3685" top="2268" bottom="2268"/>',
        ),
        (
            '<hp:colPr id="" type="NEWSPAPER" layout="LEFT" colCount="1" sameSz="1" sameGap="0"/>',
            '<hp:colPr id="" type="NEWSPAPER" layout="LEFT" colCount="2" sameSz="1" sameGap="2268"><hp:colLine type="SOLID" width="0.12 mm" color="#000000"/></hp:colPr>',
        ),
        ('horzsize="48190"', 'horzsize="24945"'),
    )
    for old, new in replacements:
        if section.count(old) != 1:
            raise QuestionSetHwpxError("기본 HWPX 템플릿의 2단 변환 경계가 올바르지 않습니다.")
        section = section.replace(old, new, 1)
    members["Contents/section0.xml"] = section.encode("utf-8")

    output = io.BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for source_info in infos:
            info = zipfile.ZipInfo(source_info.filename, (1980, 1, 1, 0, 0, 0))
            info.compress_type = source_info.compress_type
            info.create_system = source_info.create_system
            info.external_attr = source_info.external_attr
            archive.writestr(info, members[source_info.filename])
    return output.getvalue()


def _problem_bank_profile(kind: str) -> dict[str, Any]:
    profile = _default_profile(kind)
    profile["profile_id"] = f"problem-bank-{kind}-v1"
    profile["template"].update(
        path=f"templates/{PROBLEM_BANK_TEMPLATE_NAME}",
        sha256=PROBLEM_BANK_TEMPLATE_SHA256,
    )
    profile["design"]["page"] = copy.deepcopy(PROBLEM_BANK_PAGE)
    profile["design"]["columns"] = copy.deepcopy(PROBLEM_BANK_COLUMNS)
    return profile


@dataclass(frozen=True)
class QuestionSetHwpxArtifact:
    artifact_kind: str
    path: Path
    name: str
    attachment_name: str
    media_type: str
    size: int
    sha256: str


@dataclass(frozen=True)
class QuestionSetHwpxBuild:
    set_id: str
    build_id: str
    build_fingerprint_sha256: str
    receipt: Mapping[str, Any]
    receipt_artifact: QuestionSetHwpxArtifact
    outputs: tuple[QuestionSetHwpxArtifact, ...]
    cached: bool


@dataclass(frozen=True)
class _SourceItem:
    ordinal: int
    project_id: str
    item_id: str
    synthetic_item_id: str
    requested_revision: int | None
    actual_revision: int
    path: Path
    sha256: str
    value: Mapping[str, Any]


# ponytail: the desktop server has one process; an OS lock is unnecessary until
# multiple server processes are supported.
_BUILD_LOCK = threading.Lock()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _validated_id(value: Any, *, field: str) -> str:
    try:
        return validate_id(value, field=field)
    except ModelError as exc:
        raise QuestionSetHwpxError(str(exc)) from exc


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
                raise QuestionSetHwpxError("HWPX 산출물 경로가 읽기 전에 변경되었습니다.")
            for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise QuestionSetHwpxError("HWPX 산출물을 읽을 수 없습니다.") from exc
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
        raise QuestionSetHwpxError("HWPX build receipt를 직렬화할 수 없습니다.") from exc


def _workspace(path: Path | str) -> Path:
    candidate = Path(path)
    if _is_link(candidate):
        raise QuestionSetHwpxError("워크스페이스 폴더에 링크를 사용할 수 없습니다.")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise QuestionSetHwpxError("워크스페이스 폴더를 찾을 수 없습니다.") from exc
    if not resolved.is_dir():
        raise QuestionSetHwpxError("워크스페이스 폴더를 찾을 수 없습니다.")
    return resolved


def _direct_directory(root: Path, name: str, *, create: bool = False) -> Path:
    _validated_id(name, field="directory id")
    path = root / name
    if path.exists():
        if _is_link(path) or not path.is_dir():
            raise QuestionSetHwpxError("안전하지 않은 폴더 경로입니다.")
    elif create:
        try:
            path.mkdir()
        except OSError as exc:
            raise QuestionSetHwpxError("산출물 폴더를 만들 수 없습니다.") from exc
    else:
        raise QuestionSetHwpxError("폴더를 찾을 수 없습니다.")
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise QuestionSetHwpxError("폴더가 워크스페이스 밖을 가리킵니다.") from exc
    return path


def _regular_file(path: Path, *, label: str) -> Path:
    if _is_link(path) or not path.is_file():
        raise QuestionSetHwpxError(f"{label} 파일을 찾을 수 없거나 안전하지 않습니다.")
    return path


def _load_exact_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes, str]:
    path = _regular_file(path, label=label)
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QuestionSetHwpxError(f"{label} JSON을 읽을 수 없습니다.") from exc
    if not isinstance(value, dict):
        raise QuestionSetHwpxError(f"{label} JSON 루트는 객체여야 합니다.")
    return value, raw, _sha256_bytes(raw)


def _revision(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise QuestionSetHwpxError(f"{label} revision이 올바르지 않습니다.")
    return value


def _set_revision(document: Mapping[str, Any]) -> int:
    value = document.get("revision")
    if value is None and isinstance(document.get("workflow"), Mapping):
        value = document["workflow"].get("revision")
    return _revision(value, label="문제지")


def _output_mode(document: Mapping[str, Any]) -> str:
    value = document.get("output_mode")
    if value is None and isinstance(document.get("rendering"), Mapping):
        value = document["rendering"].get("output_mode")
    if value not in {"separate", "combined_end_section"}:
        raise QuestionSetHwpxError("output_mode은 separate 또는 combined_end_section이어야 합니다.")
    return str(value)


def _template_boundary(document: Mapping[str, Any]) -> dict[str, Any]:
    rendering = document.get("rendering")
    rendering = rendering if isinstance(rendering, Mapping) else {}
    template_ref = rendering.get("template_ref", document.get("template_ref"))
    profile_ref = rendering.get("design_profile_ref", document.get("design_profile_ref"))
    if template_ref is not None or profile_ref is not None:
        raise QuestionSetHwpxError(
            "사용자 HWPX 템플릿 연결은 아직 설정되지 않았습니다. 현재는 기본 문제은행 2단 템플릿만 사용할 수 있습니다."
        )
    return {
        "selection": "problem_bank_two_column_default",
        "requested_template_ref": None,
        "requested_design_profile_ref": None,
        "template_sha256": PROBLEM_BANK_TEMPLATE_SHA256,
    }


def _safe_title(value: Any, fallback: str) -> str:
    text = str(value).strip() if value is not None else ""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", text).strip(" .-")
    return text or fallback


def _assert_plain_tree(root: Path) -> None:
    if _is_link(root):
        raise QuestionSetHwpxError("문항 폴더에 링크를 사용할 수 없습니다.")
    try:
        for path in root.rglob("*"):
            if _is_link(path):
                raise QuestionSetHwpxError("문항 폴더 안에 링크를 사용할 수 없습니다.")
    except OSError as exc:
        raise QuestionSetHwpxError("문항 폴더를 검사할 수 없습니다.") from exc


def _resolve_sources(
    workspace: Path,
    question_set: Mapping[str, Any],
) -> tuple[_SourceItem, ...]:
    refs = question_set.get("items")
    if not isinstance(refs, list) or not refs:
        raise QuestionSetHwpxError("문제지에는 문항이 한 개 이상 있어야 합니다.")
    seen: set[tuple[str, str]] = set()
    result: list[_SourceItem] = []
    for ordinal, ref in enumerate(refs, 1):
        if not isinstance(ref, Mapping):
            raise QuestionSetHwpxError(f"문제지 {ordinal}번 참조가 올바르지 않습니다.")
        project_id = _validated_id(ref.get("project_id"), field="project_id")
        item_id = _validated_id(ref.get("item_id"), field="item_id")
        key = (project_id, item_id)
        if key in seen:
            raise QuestionSetHwpxError("같은 문항은 한 문제지에 두 번 넣을 수 없습니다.")
        seen.add(key)

        project_dir = _direct_directory(workspace, project_id)
        project, _, _ = _load_exact_json(project_dir / "project.json", label="프로젝트")
        if project.get("project_id") != project_id:
            raise QuestionSetHwpxError(f"프로젝트 경로와 ID가 일치하지 않습니다: {project_id}")
        items_dir = _direct_directory(project_dir, "items")
        item_dir = _direct_directory(items_dir, item_id)
        _assert_plain_tree(item_dir)
        item_path = item_dir / "question.json"
        item, _, item_sha = _load_exact_json(item_path, label="문항")
        if item.get("item_id") != item_id:
            raise QuestionSetHwpxError(f"문항 경로와 ID가 일치하지 않습니다: {project_id}/{item_id}")
        workflow = item.get("workflow")
        actual_revision = _revision(
            workflow.get("revision") if isinstance(workflow, Mapping) else None,
            label=f"문항 {project_id}/{item_id}",
        )
        requested = ref.get("source_revision")
        if requested is not None:
            requested = _revision(requested, label=f"문항 참조 {project_id}/{item_id}")
        identity_hash = hashlib.sha256(f"{project_id}\0{item_id}".encode()).hexdigest()[:16]
        result.append(
            _SourceItem(
                ordinal=ordinal,
                project_id=project_id,
                item_id=item_id,
                synthetic_item_id=f"selected-{ordinal:04d}-{identity_hash}",
                requested_revision=requested,
                actual_revision=actual_revision,
                path=item_path,
                sha256=item_sha,
                value=item,
            )
        )
    return tuple(result)


def _subquestion_parts(value: Mapping[str, Any]) -> tuple[str, str] | None:
    identity = value.get("identity")
    if not isinstance(identity, Mapping) or identity.get("unit_kind") != "subquestion":
        return None
    printed_number = identity.get("printed_number")
    match = SUBQUESTION_RE.fullmatch(str(printed_number).strip()) if printed_number is not None else None
    if not match:
        return None
    return match.group(1).strip(), f"({match.group(2)})"


def _block_visible_text(block: Mapping[str, Any]) -> str:
    if block.get("type") == "paragraph":
        inlines = block.get("inlines")
        if not isinstance(inlines, list):
            return ""
        parts: list[str] = []
        for inline in inlines:
            if not isinstance(inline, Mapping):
                continue
            text = inline.get("text")
            if not isinstance(text, str):
                text = inline.get("source_text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    return ""


def _shared_context_plan(
    sources: tuple[_SourceItem, ...],
) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    previous_group: tuple[str, str] | None = None
    for source in sources:
        parts = _subquestion_parts(source.value)
        decision: dict[str, Any] = {"render": "full"}
        if parts is None:
            previous_group = None
            result.append(decision)
            continue

        base, label = parts
        group_key = (source.project_id, re.sub(r"\s+", "", base).casefold())
        group_seed = f"{group_key[0]}\0{group_key[1]}".encode()
        decision.update(
            group_id=f"group-{_sha256_bytes(group_seed)[:16]}",
            base_printed_number=base,
            subquestion_label=label,
        )
        if group_key == previous_group:
            content = source.value.get("content")
            question = content.get("question") if isinstance(content, Mapping) else None
            blocks = question.get("blocks") if isinstance(question, Mapping) else None
            if isinstance(blocks, list):
                start = next(
                    (
                        index
                        for index, block in enumerate(blocks)
                        if isinstance(block, Mapping)
                        and _block_visible_text(block).lstrip().startswith(label)
                    ),
                    None,
                )
                if start is not None:
                    decision["render"] = "target_suffix"
                    decision["start_block_index"] = start
                    block_id = blocks[start].get("block_id")
                    if isinstance(block_id, str):
                        decision["start_block_id"] = block_id
                else:
                    decision["fallback"] = "target_marker_not_found_preserved_full"
        previous_group = group_key
        result.append(decision)
    return tuple(result)


def _fingerprint(
    *,
    set_id: str,
    set_sha256: str,
    revision: int,
    mode: str,
    sources: tuple[_SourceItem, ...],
    template_boundary: Mapping[str, Any],
) -> str:
    payload = {
        "contract": QUESTION_SET_RENDER_CONTRACT,
        "set_id": set_id,
        "set_sha256": set_sha256,
        "revision": revision,
        "output_mode": mode,
        "items": [
            {
                "ordinal": source.ordinal,
                "project_id": source.project_id,
                "item_id": source.item_id,
                "revision": source.actual_revision,
                "sha256": source.sha256,
            }
            for source in sources
        ],
        "template": dict(template_boundary),
        "engine_id": NATIVE_RENDERER_ENGINE_ID,
        "engine_fingerprint_sha256": NATIVE_RENDERER_ENGINE_FINGERPRINT_SHA256,
    }
    return _sha256_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise QuestionSetHwpxError(f"파일을 만들 수 없습니다: {path.name}") from exc


def _snapshot_project(
    root: Path,
    *,
    set_id: str,
    title: str,
    revision: int,
    mode: str,
    sources: tuple[_SourceItem, ...],
    shared_context: tuple[Mapping[str, Any], ...],
) -> Path:
    project_dir = root / "project"
    project_dir.mkdir()
    profiles = project_dir / "profiles"
    templates = profiles / "templates"
    templates.mkdir(parents=True)
    template = _problem_bank_template_bytes()
    if _sha256_bytes(template) != PROBLEM_BANK_TEMPLATE_SHA256:
        raise QuestionSetHwpxError("문제은행 2단 HWPX 템플릿의 SHA-256이 일치하지 않습니다.")
    _write_bytes(templates / PROBLEM_BANK_TEMPLATE_NAME, template)
    for kind in ("question", "explanation"):
        _write_bytes(
            profiles / f"problem-bank-{kind}-v1.json",
            _canonical_json(_problem_bank_profile(kind)),
        )

    for source, context_decision in zip(sources, shared_context, strict=True):
        target = project_dir / "items" / source.synthetic_item_id
        resources = source.value.get("resources")
        assets = resources.get("assets", []) if isinstance(resources, Mapping) else []
        asset_paths = {
            Path(asset["path"]) for asset in assets
            if isinstance(asset, Mapping) and isinstance(asset.get("path"), str)
        }

        def ignore_unused_jobs(folder: str, names: list[str]) -> set[str]:
            relative = Path(folder).relative_to(source.path.parent)
            return {
                name for name in names
                if (relative / name).parts[0] == "jobs"
                and not any(relative / name == asset or relative / name in asset.parents for asset in asset_paths)
            }

        try:
            shutil.copytree(source.path.parent, target, ignore=ignore_unused_jobs)
        except OSError as exc:
            raise QuestionSetHwpxError(
                f"문항 스냅샷을 만들 수 없습니다: {source.project_id}/{source.item_id}"
            ) from exc
        snapshot = copy.deepcopy(dict(source.value))
        snapshot["item_id"] = source.synthetic_item_id
        identity = snapshot.get("identity")
        if not isinstance(identity, dict):
            raise QuestionSetHwpxError(
                f"문항 identity가 올바르지 않습니다: {source.project_id}/{source.item_id}"
            )
        identity["printed_number"] = str(source.ordinal)
        if context_decision.get("render") == "target_suffix":
            content = snapshot.get("content")
            question = content.get("question") if isinstance(content, dict) else None
            blocks = question.get("blocks") if isinstance(question, dict) else None
            start = context_decision.get("start_block_index")
            if (
                not isinstance(blocks, list)
                or isinstance(start, bool)
                or not isinstance(start, int)
                or not 0 <= start < len(blocks)
            ):
                raise QuestionSetHwpxError("공유 문항 스냅샷의 대상 블록이 올바르지 않습니다.")
            question["blocks"] = blocks[start:]
        if mode == "combined_end_section":
            resources = snapshot.get("resources")
            assets = resources.get("assets") if isinstance(resources, Mapping) else None
            if isinstance(assets, list):
                for asset in assets:
                    if isinstance(asset, dict):
                        asset["section"] = "shared"
        (target / "question.json").write_bytes(_canonical_json(snapshot))

    synthetic_project_id = f"question-set-{hashlib.sha256(set_id.encode()).hexdigest()[:24]}"
    project = {
        "schema_version": "0.1.0",
        "project_id": synthetic_project_id,
        "language": "ko-KR",
        "workflow": {"status": "content_ready", "revision": revision},
        "metadata": {
            "filename_base": title,
            "exam_type": mode,
            "range": set_id,
        },
        "item_order": [source.synthetic_item_id for source in sources],
        "groups": [],
        "rendering": {
            "question_design_profile_ref": "profiles/problem-bank-question-v1.json",
            "explanation_design_profile_ref": "profiles/problem-bank-explanation-v1.json",
        },
        "export": {
            "renderer": "native_ordered",
            "output_mode": "separate_hwpx",
            "shared_item_policy": "explicit_item_order",
            "include_answers": True,
            "include_explanations": True,
            "engine_id": NATIVE_RENDERER_ENGINE_ID,
            "engine_fingerprint_sha256": NATIVE_RENDERER_ENGINE_FINGERPRINT_SHA256,
        },
    }
    (project_dir / "project.json").write_bytes(_canonical_json(project))
    return project_dir


def _load_render_items(plan: ExportPlan) -> tuple[Mapping[str, Any], tuple[_LoadedItem, ...]]:
    plan = _fresh_plan(plan)
    receipt = plan.receipt
    project = _json_bytes(
        _read_exact(plan.project_path, str(receipt["project"]["sha256"]), "project"),
        "project",
    )
    entries = {
        entry["item_id"]: entry
        for entry in receipt["inputs"]["items"]
        if isinstance(entry, Mapping) and isinstance(entry.get("item_id"), str)
    }
    loaded: list[_LoadedItem] = []
    items_root = (plan.project_path.parent / "items").resolve()
    for item_id in plan.question.item_ids:
        if (
            not item_id
            or "\\" in item_id
            or ":" in item_id
            or PurePosixPath(item_id).name != item_id
        ):
            raise QuestionSetHwpxError("렌더링 스냅샷의 문항 ID가 안전하지 않습니다.")
        entry = entries.get(item_id)
        if not isinstance(entry, Mapping):
            raise QuestionSetHwpxError(f"렌더링 계획에 문항이 없습니다: {item_id}")
        item_path = (items_root / item_id / "question.json").resolve()
        if not item_path.is_relative_to(items_root):
            raise QuestionSetHwpxError("렌더링 스냅샷 문항이 폴더 밖을 가리킵니다.")
        value = _json_bytes(
            _read_exact(item_path, str(entry.get("sha256")), f"item {item_id}"),
            f"item {item_id}",
        )
        mode = entry.get("question_render_mode")
        source_asset_id = entry.get("source_question_asset_id")
        answer_mode = entry.get("answer_render_mode")
        explanation_mode = entry.get("explanation_render_mode")
        if mode not in {"canonical", "source_crop"} or (
            mode == "source_crop" and not isinstance(source_asset_id, str)
        ) or answer_mode not in {"saved", "omitted"} or explanation_mode not in {
            "saved",
            "omitted",
        }:
            raise QuestionSetHwpxError(f"렌더링 모드가 올바르지 않습니다: {item_id}")
        loaded.append(
            _LoadedItem(
                item_id,
                item_path,
                value,
                str(mode),
                source_asset_id if isinstance(source_asset_id, str) else None,
                str(answer_mode),
                str(explanation_mode),
            )
        )
    return project, tuple(loaded)


def _render_combined(plan: ExportPlan, output_path: Path) -> dict[str, Any]:
    project, items = _load_render_items(plan)
    context = _load_template_context(plan.question)
    renderer = _Renderer("question", context)
    export = project.get("export")
    if not isinstance(export, Mapping):
        raise QuestionSetHwpxError("렌더링 스냅샷의 export 계약이 없습니다.")

    markers = [
        renderer.question_item(item, ordinal)
        for ordinal, item in enumerate(items, 1)
    ]
    for ordinal, (item, marker) in enumerate(zip(items, markers, strict=True), 1):
        renderer.endnote_item(
            item,
            ordinal,
            marker,
            include_answer=export.get("include_answers") is True,
            include_explanation=export.get("include_explanations") is True,
        )
    section, hpf, binaries = renderer.finish()
    data = _package_bytes(
        context,
        section,
        hpf,
        "\n".join(renderer.preview).encode("utf-8"),
        binaries,
    )
    canonical_counts = dict(sorted(renderer.canonical.items()))
    renderer_counts = _inspect_package(
        data,
        canonical_counts,
        context.writable_width,
        allow_native_endnotes=True,
    )
    _write_bytes(output_path, data)
    digest = _sha256_bytes(data)
    output = {
        "document_kind": "combined",
        "path": output_path.name,
        "sha256": digest,
        "size_bytes": len(data),
        "expected_counts": canonical_counts,
        "actual_counts": canonical_counts,
        "renderer_counts": renderer_counts,
        "validation": {
            "package": "passed",
            "native_endnote_contract": "passed",
            "hancom_visual": "pending",
        },
    }
    report = validate_hwpx_output(
        output_path,
        "combined",
        build_receipt={"outputs": [output]},
        run_external_tools=False,
    )
    if not report["ok"]:
        codes = ", ".join(issue["code"] for issue in report["errors"])
        raise HwpxRenderError(f"strict combined HWPX validation failed: {codes}")
    output["validation"]["strict_report_sha256"] = report["report_sha256"]
    return output


def _source_receipts(
    sources: tuple[_SourceItem, ...],
    shared_context: tuple[Mapping[str, Any], ...],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for source, context_decision in zip(sources, shared_context, strict=True):
        entry: dict[str, Any] = {
            "ordinal": source.ordinal,
            "project_id": source.project_id,
            "item_id": source.item_id,
            "source_revision": source.requested_revision,
            "actual_revision": source.actual_revision,
            "sha256": source.sha256,
            "shared_context": context_decision.get("render", "full"),
        }
        group_id = context_decision.get("group_id")
        if isinstance(group_id, str):
            entry["group"] = {
                "group_id": group_id,
                "base_printed_number": context_decision["base_printed_number"],
                "subquestion_label": context_decision["subquestion_label"],
            }
        start_block_id = context_decision.get("start_block_id")
        if isinstance(start_block_id, str):
            entry["shared_context_start_block_id"] = start_block_id
        fallback = context_decision.get("fallback")
        if isinstance(fallback, str):
            entry["shared_context_fallback"] = fallback
        result.append(entry)
    return result


def _warnings(
    sources: tuple[_SourceItem, ...],
    renderer_warnings: Any,
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    synthetic = {source.synthetic_item_id: source for source in sources}
    for source in sources:
        if (
            source.requested_revision is not None
            and source.requested_revision != source.actual_revision
        ):
            warnings.append(
                {
                    "code": "source_revision_changed",
                    "project_id": source.project_id,
                    "item_id": source.item_id,
                    "expected_revision": source.requested_revision,
                    "actual_revision": source.actual_revision,
                }
            )
    if isinstance(renderer_warnings, list):
        for warning in renderer_warnings:
            if not isinstance(warning, Mapping):
                continue
            value = copy.deepcopy(dict(warning))
            source = synthetic.get(value.get("item_id"))
            if source:
                value["item_id"] = source.item_id
                value["project_id"] = source.project_id
            warnings.append(value)
    return warnings


def _output_artifact(
    build_dir: Path,
    entry: Mapping[str, Any],
    set_id: str,
) -> QuestionSetHwpxArtifact:
    kind = entry.get("document_kind")
    name = entry.get("path")
    expected_sha = entry.get("sha256")
    expected_size = entry.get("size_bytes")
    if kind not in {"question", "explanation", "combined"}:
        raise QuestionSetHwpxError("receipt의 HWPX 종류가 올바르지 않습니다.")
    if (
        not isinstance(name, str)
        or not name.casefold().endswith(".hwpx")
        or Path(name).name != name
    ):
        raise QuestionSetHwpxError("receipt의 HWPX 파일명이 올바르지 않습니다.")
    if not isinstance(expected_sha, str) or not SHA256_RE.fullmatch(expected_sha):
        raise QuestionSetHwpxError("receipt의 HWPX SHA-256이 올바르지 않습니다.")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 1:
        raise QuestionSetHwpxError("receipt의 HWPX 크기가 올바르지 않습니다.")
    path = _regular_file(build_dir / name, label="HWPX 산출물")
    try:
        path.resolve(strict=True).relative_to(build_dir.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise QuestionSetHwpxError("HWPX 산출물이 build 폴더 밖을 가리킵니다.") from exc
    size, digest = _measure(path)
    if (size, digest) != (expected_size, expected_sha):
        raise QuestionSetHwpxError("HWPX 산출물이 build receipt와 일치하지 않습니다.")
    return QuestionSetHwpxArtifact(
        artifact_kind=str(kind),
        path=path,
        name=name,
        attachment_name=f"{set_id}-{kind}-{digest[:16]}.hwpx",
        media_type="application/hwp+zip",
        size=size,
        sha256=digest,
    )


def _load_build(
    build_dir: Path,
    *,
    set_id: str,
    fingerprint: str,
    cached: bool,
) -> QuestionSetHwpxBuild:
    if _is_link(build_dir) or not build_dir.is_dir():
        raise QuestionSetHwpxError("HWPX build 폴더가 안전하지 않습니다.")
    receipt_path = _regular_file(build_dir / "receipt.json", label="HWPX receipt")
    receipt, raw, _ = _load_exact_json(receipt_path, label="HWPX receipt")
    if raw != _canonical_json(receipt):
        raise QuestionSetHwpxError("HWPX receipt 형식이 올바르지 않습니다.")
    question_set = receipt.get("question_set")
    if (
        receipt.get("status") != "built"
        or receipt.get("build_fingerprint_sha256") != fingerprint
        or not isinstance(question_set, Mapping)
        or question_set.get("set_id") != set_id
    ):
        raise QuestionSetHwpxError("HWPX receipt가 요청한 문제지와 일치하지 않습니다.")
    mode = question_set.get("output_mode")
    expected_kinds = (
        ("question", "explanation") if mode == "separate" else ("combined",)
    )
    entries = receipt.get("outputs")
    if not isinstance(entries, list) or tuple(
        entry.get("document_kind") for entry in entries if isinstance(entry, Mapping)
    ) != expected_kinds:
        raise QuestionSetHwpxError("HWPX receipt의 산출물 구성이 올바르지 않습니다.")
    outputs = tuple(_output_artifact(build_dir, entry, set_id) for entry in entries)
    expected_names = {"receipt.json", *(artifact.name for artifact in outputs)}
    try:
        actual_names = {path.name for path in build_dir.iterdir()}
    except OSError as exc:
        raise QuestionSetHwpxError("HWPX build 폴더를 읽을 수 없습니다.") from exc
    if actual_names != expected_names:
        raise QuestionSetHwpxError("HWPX build 폴더에 선언되지 않은 파일이 있습니다.")
    receipt_sha = _sha256_bytes(raw)
    receipt_artifact = QuestionSetHwpxArtifact(
        artifact_kind="receipt",
        path=receipt_path,
        name="receipt.json",
        attachment_name=f"{set_id}-receipt-{receipt_sha[:16]}.json",
        media_type="application/json; charset=utf-8",
        size=len(raw),
        sha256=receipt_sha,
    )
    return QuestionSetHwpxBuild(
        set_id=set_id,
        build_id=str(receipt.get("build_id")),
        build_fingerprint_sha256=fingerprint,
        receipt=receipt,
        receipt_artifact=receipt_artifact,
        outputs=outputs,
        cached=cached,
    )


def _output_root(workspace: Path, set_id: str, *, create: bool) -> Path:
    sets = workspace / "question_sets"
    if not sets.exists() or _is_link(sets) or not sets.is_dir():
        raise QuestionSetHwpxError("문제지 저장 폴더를 찾을 수 없습니다.")
    outputs = sets / "outputs"
    if not outputs.exists() and create:
        outputs.mkdir()
    if _is_link(outputs) or not outputs.is_dir():
        raise QuestionSetHwpxError("문제지 HWPX 산출물 폴더가 안전하지 않습니다.")
    set_outputs = outputs / set_id
    if not set_outputs.exists() and create:
        set_outputs.mkdir()
    if _is_link(set_outputs) or not set_outputs.is_dir():
        raise QuestionSetHwpxError("문제지별 HWPX 폴더가 안전하지 않습니다.")
    hwpx = set_outputs / "hwpx"
    if not hwpx.exists() and create:
        hwpx.mkdir()
    if _is_link(hwpx) or not hwpx.is_dir():
        raise QuestionSetHwpxError("문제지 HWPX build 폴더가 안전하지 않습니다.")
    return hwpx


def _create_question_set_hwpx_build(
    workspace_root: Path | str,
    set_id: str,
) -> QuestionSetHwpxBuild:
    """Build immutable HWPX output(s) from one saved question set."""

    with _BUILD_LOCK:
        workspace = _workspace(workspace_root)
        set_id = _validated_id(set_id, field="set_id")
        if not set_id.startswith("set-"):
            raise QuestionSetHwpxError("set_id는 'set-'으로 시작해야 합니다.")
        sets_dir = workspace / "question_sets"
        if _is_link(sets_dir) or not sets_dir.is_dir():
            raise QuestionSetHwpxError("문제지 저장 폴더를 찾을 수 없습니다.")
        question_set, set_raw, set_sha = _load_exact_json(
            sets_dir / f"{set_id}.json", label="문제지"
        )
        if question_set.get("set_id") != set_id:
            raise QuestionSetHwpxError("문제지 경로와 set_id가 일치하지 않습니다.")
        revision = _set_revision(question_set)
        mode = _output_mode(question_set)
        template_boundary = _template_boundary(question_set)
        sources = _resolve_sources(workspace, question_set)
        shared_context = _shared_context_plan(sources)
        title = _safe_title(question_set.get("title"), set_id)
        fingerprint = _fingerprint(
            set_id=set_id,
            set_sha256=set_sha,
            revision=revision,
            mode=mode,
            sources=sources,
            template_boundary=template_boundary,
        )
        root = _output_root(workspace, set_id, create=True)
        build_dir = root / f"build-{fingerprint}"
        if build_dir.exists():
            return _load_build(
                build_dir,
                set_id=set_id,
                fingerprint=fingerprint,
                cached=True,
            )
        if _is_link(build_dir):
            raise QuestionSetHwpxError("동일 fingerprint의 HWPX build 경로가 안전하지 않습니다.")

        stage = root / f".build-{uuid.uuid4().hex[:12]}.tmp"
        stage.mkdir()
        renderer_receipt: Mapping[str, Any] = {}
        try:
            with tempfile.TemporaryDirectory(prefix=".question-set-input-", dir=root) as temp:
                snapshot = _snapshot_project(
                    Path(temp),
                    set_id=set_id,
                    title=title,
                    revision=revision,
                    mode=mode,
                    sources=sources,
                    shared_context=shared_context,
                )
                plan = preflight_export(snapshot, stage)
                if mode == "separate":
                    renderer_receipt = render_export_plan(plan)
                    outputs = [
                        copy.deepcopy(entry)
                        for entry in renderer_receipt["outputs"]
                        if isinstance(entry, Mapping)
                    ]
                else:
                    combined_path = stage / f"{title}-문제와해설.hwpx"
                    outputs = [_render_combined(plan, combined_path)]
                    renderer_receipt = {
                        "warnings": copy.deepcopy(plan.receipt.get("warnings", []))
                    }

            current_set_raw = _regular_file(
                sets_dir / f"{set_id}.json", label="문제지"
            ).read_bytes()
            if current_set_raw != set_raw:
                raise QuestionSetHwpxError("문제지가 HWPX build 중 변경되었습니다.")
            for source in sources:
                if _measure(source.path)[1] != source.sha256:
                    raise QuestionSetHwpxError(
                        f"원본 문항이 HWPX build 중 변경되었습니다: {source.project_id}/{source.item_id}"
                    )

            receipt = {
                "schema_version": RECEIPT_VERSION,
                "receipt_type": "question_json_studio.question_set_hwpx_build",
                "status": "built",
                "build_id": f"build-{fingerprint[:24]}",
                "build_fingerprint_sha256": fingerprint,
                "question_set": {
                    "set_id": set_id,
                    "title": question_set.get("title"),
                    "revision": revision,
                    "sha256": set_sha,
                    "output_mode": mode,
                },
                "inputs": {
                    "ordered_items": _source_receipts(sources, shared_context),
                    "template": template_boundary,
                    "engine": {
                        "engine_id": NATIVE_RENDERER_ENGINE_ID,
                        "engine_fingerprint_sha256": NATIVE_RENDERER_ENGINE_FINGERPRINT_SHA256,
                    },
                },
                "outputs": outputs,
                "policy": {
                    "renderer": "native_ordered",
                    "render_contract": QUESTION_SET_RENDER_CONTRACT,
                    "output_mode": mode,
                    "renumbering": "snapshot_only_1_to_n",
                    "source_items_mutated": False,
                    "shared_context": {
                        "grouping": "same_project_and_printed_number_base",
                        "adjacency": "selected_order_consecutive_only",
                        "single_or_first": "full",
                        "following_sibling": "target_suffix",
                        "missing_target_marker": "preserve_full",
                    },
                    "endnotes_used": mode == "combined_end_section",
                    "endnotes": {
                        "enabled": mode == "combined_end_section",
                        "placement": (
                            "end_of_document" if mode == "combined_end_section" else None
                        ),
                        "numbering": (
                            "continuous_1_to_n" if mode == "combined_end_section" else None
                        ),
                        "labels": (
                            ["[답]", "[풀이]"] if mode == "combined_end_section" else []
                        ),
                        "missing_content": (
                            "empty_label_with_warning"
                            if mode == "combined_end_section"
                            else None
                        ),
                    },
                    "combined_explanation": (
                        "native_endnotes" if mode == "combined_end_section" else None
                    ),
                    "template_selection": template_boundary["selection"],
                },
                "warnings": _warnings(sources, renderer_receipt.get("warnings")),
            }
            _write_bytes(stage / "receipt.json", _canonical_json(receipt))
            if build_dir.exists() or _is_link(build_dir):
                raise QuestionSetHwpxError("동일 fingerprint의 HWPX build가 이미 존재합니다.")
            try:
                stage.rename(build_dir)
            except OSError as exc:
                raise QuestionSetHwpxError("HWPX build를 원자적으로 게시할 수 없습니다.") from exc
            return _load_build(
                build_dir,
                set_id=set_id,
                fingerprint=fingerprint,
                cached=False,
            )
        finally:
            if stage.exists() and stage.parent == root and stage.name.startswith(".build-"):
                try:
                    shutil.rmtree(stage)
                except OSError:
                    pass


def create_question_set_hwpx_build(
    workspace_root: Path | str,
    set_id: str,
) -> QuestionSetHwpxBuild:
    """Build immutable HWPX output(s) from one saved question set."""

    try:
        return _create_question_set_hwpx_build(workspace_root, set_id)
    except QuestionSetHwpxError:
        raise
    except (ExportPreflightError, HwpxRenderError, ModelError, OSError) as exc:
        raise QuestionSetHwpxError(str(exc)) from exc


def resolve_question_set_hwpx_artifact(
    workspace_root: Path | str,
    set_id: str,
    fingerprint: str,
    artifact_kind: str,
    expected_sha256: str,
) -> QuestionSetHwpxArtifact:
    """Resolve one content-bound question-set artifact without accepting a path."""

    workspace = _workspace(workspace_root)
    set_id = _validated_id(set_id, field="set_id")
    if not set_id.startswith("set-"):
        raise QuestionSetHwpxError("set_id는 'set-'으로 시작해야 합니다.")
    if not isinstance(fingerprint, str) or not SHA256_RE.fullmatch(fingerprint):
        raise QuestionSetHwpxError("HWPX build fingerprint가 올바르지 않습니다.")
    if artifact_kind not in {"question", "explanation", "combined", "receipt"}:
        raise QuestionSetHwpxError("HWPX artifact 종류가 올바르지 않습니다.")
    if not isinstance(expected_sha256, str) or not SHA256_RE.fullmatch(expected_sha256):
        raise QuestionSetHwpxError("HWPX artifact SHA-256이 올바르지 않습니다.")
    root = _output_root(workspace, set_id, create=False)
    build_dir = root / f"build-{fingerprint}"
    build = _load_build(
        build_dir,
        set_id=set_id,
        fingerprint=fingerprint,
        cached=True,
    )
    artifact = (
        build.receipt_artifact
        if artifact_kind == "receipt"
        else next(
            (value for value in build.outputs if value.artifact_kind == artifact_kind),
            None,
        )
    )
    if artifact is None or artifact.sha256 != expected_sha256:
        raise QuestionSetHwpxError("HWPX artifact의 content-bound SHA-256이 일치하지 않습니다.")
    return artifact


__all__ = [
    "QuestionSetHwpxArtifact",
    "QuestionSetHwpxBuild",
    "QuestionSetHwpxError",
    "create_question_set_hwpx_build",
    "resolve_question_set_hwpx_artifact",
]
