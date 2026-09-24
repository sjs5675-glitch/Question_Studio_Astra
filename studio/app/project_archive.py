"""Deterministic, integrity-checked project backup archives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ARCHIVE_ID_RE = re.compile(
    r"^r([1-9][0-9]*)-([0-9a-f]{64})-([0-9a-f]{64})$"
)
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
CHUNK_SIZE = 1024 * 1024


class ProjectArchiveError(ValueError):
    """Raised when canonical backup inputs cannot be trusted."""


@dataclass(frozen=True)
class ProjectArchive:
    archive_id: str
    name: str
    path: Path
    size: int
    sha256: str
    cached: bool


@dataclass(frozen=True)
class ProjectSnapshot:
    project_id: str
    input_fingerprint_sha256: str
    file_count: int


@dataclass(frozen=True)
class _InputFile:
    member: str
    path: Path
    size: int
    sha256: str


# ponytail: one process-local lock is enough for this single-user desktop app.
# Use cross-process file locks only if multiple servers share a workspace.
_ARCHIVE_LOCK = threading.Lock()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _relative(value: Any, *, label: str, prefix: str | None = None) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ProjectArchiveError(f"{label} 경로가 올바르지 않습니다.")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or str(relative) != value
        or any(part in {"", ".", ".."} for part in relative.parts)
        or (prefix is not None and (not relative.parts or relative.parts[0] != prefix))
    ):
        raise ProjectArchiveError(f"{label} 경로가 올바르지 않습니다.")
    return relative


def _safe_file(root: Path, relative: PurePosixPath, *, label: str) -> Path:
    candidate = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current /= part
        if _is_link(current):
            raise ProjectArchiveError(f"{label}에 심볼릭 링크를 사용할 수 없습니다.")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ProjectArchiveError(f"{label} 파일을 찾을 수 없습니다.") from exc
    if not candidate.is_file():
        raise ProjectArchiveError(f"{label} 파일을 찾을 수 없습니다.")
    return candidate


def _measure(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise ProjectArchiveError("백업 입력 파일을 읽을 수 없습니다.") from exc
    return size, digest.hexdigest()


def _json_file(path: Path, *, label: str) -> tuple[dict[str, Any], int, str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectArchiveError(f"{label} JSON을 읽을 수 없습니다.") from exc
    if not isinstance(value, dict):
        raise ProjectArchiveError(f"{label} JSON 루트는 객체여야 합니다.")
    return value, len(raw), hashlib.sha256(raw).hexdigest()


def _revision(document: Mapping[str, Any], *, label: str) -> int:
    workflow = document.get("workflow")
    revision = workflow.get("revision") if isinstance(workflow, Mapping) else None
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ProjectArchiveError(f"{label} revision이 올바르지 않습니다.")
    return revision


def _add(
    files: list[_InputFile],
    members: set[str],
    *,
    member: str,
    path: Path,
    declared_sha256: Any = None,
    declared_size: Any = None,
    require_declared_sha256: bool = False,
    allow_identical_duplicate: bool = False,
) -> None:
    normalized = str(_relative(member, label="ZIP member"))
    duplicate_key = normalized.casefold()
    size, digest = _measure(path)
    if not isinstance(declared_sha256, str) or not SHA256_RE.fullmatch(declared_sha256):
        if require_declared_sha256 or declared_sha256 is not None:
            raise ProjectArchiveError(f"{normalized}의 SHA-256 선언이 올바르지 않습니다.")
    elif digest != declared_sha256.lower():
        raise ProjectArchiveError(f"{normalized}의 SHA-256이 선언값과 일치하지 않습니다.")
    if declared_size is not None and (
        isinstance(declared_size, bool)
        or not isinstance(declared_size, int)
        or declared_size < 0
        or declared_size != size
    ):
        raise ProjectArchiveError(f"{normalized}의 파일 크기가 선언값과 일치하지 않습니다.")
    if duplicate_key in members:
        existing = next(
            (value for value in files if value.member.casefold() == duplicate_key), None
        )
        if (
            allow_identical_duplicate
            and existing is not None
            and existing.path.resolve() == path.resolve()
            and existing.size == size
            and existing.sha256 == digest
        ):
            return
        raise ProjectArchiveError("중복된 ZIP member 경로가 있습니다.")
    members.add(duplicate_key)
    files.append(_InputFile(normalized, path, size, digest))


def _collect(project_dir: Path) -> tuple[dict[str, Any], bytes, list[_InputFile]]:
    if _is_link(project_dir):
        raise ProjectArchiveError("프로젝트 폴더에 심볼릭 링크를 사용할 수 없습니다.")
    try:
        project_dir = project_dir.resolve(strict=True)
    except OSError as exc:
        raise ProjectArchiveError("프로젝트 폴더를 찾을 수 없습니다.") from exc
    if not project_dir.is_dir():
        raise ProjectArchiveError("프로젝트 폴더를 찾을 수 없습니다.")

    files: list[_InputFile] = []
    members: set[str] = set()
    project_path = _safe_file(project_dir, PurePosixPath("project.json"), label="project.json")
    project, project_size, project_sha = _json_file(project_path, label="project.json")
    project_id = project.get("project_id")
    if not isinstance(project_id, str) or not ID_RE.fullmatch(project_id):
        raise ProjectArchiveError("project_id가 올바르지 않습니다.")
    if project_dir.name != project_id:
        raise ProjectArchiveError("프로젝트 폴더와 project_id가 일치하지 않습니다.")
    project_revision = _revision(project, label="project")
    _add(
        files,
        members,
        member="project.json",
        path=project_path,
        declared_sha256=project_sha,
        declared_size=project_size,
    )

    item_order = project.get("item_order")
    if not isinstance(item_order, list):
        raise ProjectArchiveError("item_order가 배열이 아닙니다.")
    item_revisions: list[dict[str, Any]] = []
    seen_items: set[str] = set()
    for item_id in item_order:
        if not isinstance(item_id, str) or not ID_RE.fullmatch(item_id):
            raise ProjectArchiveError("item_order의 item_id가 올바르지 않습니다.")
        if item_id.casefold() in seen_items:
            raise ProjectArchiveError("item_order에 중복 문항이 있습니다.")
        seen_items.add(item_id.casefold())
        item_relative = PurePosixPath("items", item_id, "question.json")
        item_path = _safe_file(project_dir, item_relative, label=f"문항 {item_id}")
        package, item_size, item_sha = _json_file(item_path, label=f"문항 {item_id}")
        if package.get("item_id") != item_id:
            raise ProjectArchiveError(f"문항 {item_id}의 식별자가 일치하지 않습니다.")
        item_revisions.append(
            {"item_id": item_id, "revision": _revision(package, label=f"문항 {item_id}")}
        )
        _add(
            files,
            members,
            member=str(item_relative),
            path=item_path,
            declared_sha256=item_sha,
            declared_size=item_size,
        )

        resources = package.get("resources")
        assets = resources.get("assets") if isinstance(resources, Mapping) else []
        if not isinstance(assets, list):
            raise ProjectArchiveError(f"문항 {item_id}의 assets가 배열이 아닙니다.")
        item_dir = item_path.parent
        for index, asset in enumerate(assets):
            if not isinstance(asset, Mapping):
                raise ProjectArchiveError(f"문항 {item_id}의 asset[{index}]가 객체가 아닙니다.")
            asset_relative = _relative(asset.get("path"), label=f"asset[{index}]")
            if not asset_relative.parts or asset_relative.parts[0] not in {"source", "assets"}:
                raise ProjectArchiveError(f"문항 {item_id}의 asset[{index}] 경로가 올바르지 않습니다.")
            asset_path = _safe_file(item_dir, asset_relative, label=f"문항 {item_id} asset[{index}]")
            member = str(PurePosixPath("items", item_id, *asset_relative.parts))
            _add(
                files,
                members,
                member=member,
                path=asset_path,
                declared_sha256=asset.get("sha256"),
                declared_size=asset.get("size_bytes"),
                require_declared_sha256=True,
            )

    rendering = project.get("rendering")
    if rendering is not None and not isinstance(rendering, Mapping):
        raise ProjectArchiveError("rendering이 객체가 아닙니다.")
    for key in ("question_design_profile_ref", "explanation_design_profile_ref"):
        reference = rendering.get(key) if isinstance(rendering, Mapping) else None
        if reference is None:
            continue
        profile_relative = _relative(reference, label=key, prefix="profiles")
        if profile_relative.suffix.casefold() != ".json":
            raise ProjectArchiveError(f"{key}는 JSON profile이어야 합니다.")
        profile_path = _safe_file(project_dir, profile_relative, label=key)
        profile, profile_size, profile_sha = _json_file(profile_path, label=key)
        _add(
            files,
            members,
            member=str(profile_relative),
            path=profile_path,
            declared_sha256=profile_sha,
            declared_size=profile_size,
        )
        approval = profile.get("approval")
        if not isinstance(approval, Mapping) or approval.get("status") != "approved":
            continue
        template = profile.get("template")
        if not isinstance(template, Mapping) or template.get("source") != "user_approved_clean_hwpx":
            raise ProjectArchiveError(f"{key}의 승인 template 선언이 올바르지 않습니다.")
        template_from_profile = _relative(template.get("path"), label=f"{key}.template")
        profile_parent = PurePosixPath(*profile_relative.parts[:-1])
        combined = profile_parent.joinpath(template_from_profile)
        template_relative = _relative(str(combined), label=f"{key}.template", prefix="profiles")
        template_path = _safe_file(project_dir, template_relative, label=f"{key}.template")
        _add(
            files,
            members,
            member=str(template_relative),
            path=template_path,
            declared_sha256=template.get("sha256"),
            require_declared_sha256=True,
            allow_identical_duplicate=True,
        )

    files.sort(key=lambda value: value.member)
    manifest_files = [
        {"path": value.member, "size": value.size, "sha256": value.sha256}
        for value in files
    ]
    fingerprint_payload = {
        "project_id": project_id,
        "project_revision": project_revision,
        "items": item_revisions,
        "files": manifest_files,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schema_version": "1.0",
        **fingerprint_payload,
        "input_fingerprint_sha256": fingerprint,
    }
    manifest_bytes = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    return project, manifest_bytes, files


def _zip_info(member: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(member, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _cached_archive_is_valid(
    path: Path, manifest_bytes: bytes, files: list[_InputFile]
) -> bool:
    if _is_link(path) or not path.is_file():
        return False
    expected = {value.member: value for value in files}
    expected["archive-manifest.json"] = _InputFile(
        "archive-manifest.json",
        path,
        len(manifest_bytes),
        hashlib.sha256(manifest_bytes).hexdigest(),
    )
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            if (
                archive.comment
                or not infos
                or infos[0].header_offset != 0
                or any(info.extra or info.comment for info in infos)
            ):
                return False
            with path.open("rb") as raw_stream:
                raw_stream.seek(-22, os.SEEK_END)
                end_record = raw_stream.read(22)
            if len(end_record) != 22 or end_record[:4] != b"PK\x05\x06" or end_record[20:] != b"\x00\x00":
                return False
            names = [info.filename for info in infos]
            expected_names = ["archive-manifest.json", *(value.member for value in files)]
            if names != expected_names or len(names) != len(set(names)):
                return False
            for info in infos:
                record = expected[info.filename]
                if (
                    info.is_dir()
                    or info.file_size != record.size
                    or info.date_time != ZIP_TIMESTAMP
                    or info.compress_type != zipfile.ZIP_STORED
                ):
                    return False
                digest = hashlib.sha256()
                with archive.open(info, "r") as stream:
                    for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                        digest.update(chunk)
                if digest.hexdigest() != record.sha256:
                    return False
    except (OSError, RuntimeError, zipfile.BadZipFile):
        return False
    return True


def _write_archive(
    path: Path, manifest_bytes: bytes, files: list[_InputFile], project_dir: Path
) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(_zip_info("archive-manifest.json"), manifest_bytes)
            for record in files:
                digest = hashlib.sha256()
                size = 0
                source_path = _safe_file(
                    project_dir,
                    _relative(record.member, label="백업 입력 member"),
                    label=record.member,
                )
                if source_path.resolve(strict=True) != record.path.resolve(strict=True):
                    raise ProjectArchiveError(f"{record.member}의 입력 경로가 변경되었습니다.")
                with source_path.open("rb") as source, archive.open(
                    _zip_info(record.member), "w", force_zip64=True
                ) as destination:
                    opened = os.fstat(source.fileno())
                    current = os.stat(source_path, follow_symlinks=False)
                    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                        raise ProjectArchiveError(f"{record.member}가 백업 전에 변경되었습니다.")
                    for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                        size += len(chunk)
                        digest.update(chunk)
                        destination.write(chunk)
                if size != record.size or digest.hexdigest() != record.sha256:
                    raise ProjectArchiveError(
                        f"{record.member}가 백업 도중 변경되었습니다."
                    )
                _safe_file(
                    project_dir,
                    _relative(record.member, label="백업 입력 member"),
                    label=record.member,
                )
        with temporary.open("rb+") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProjectArchiveError("ZIP 백업 파일을 만들 수 없습니다.") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def create_project_archive(project_dir: Path | str) -> ProjectArchive:
    """Create or reuse the deterministic archive for the current canonical inputs."""

    project_dir = Path(project_dir)
    with _ARCHIVE_LOCK:
        project, manifest_bytes, files = _collect(project_dir)
        project_id = str(project["project_id"])
        revision = _revision(project, label="project")
        exports = project_dir.resolve(strict=True) / "exports"
        if exports.exists() and (_is_link(exports) or not exports.is_dir()):
            raise ProjectArchiveError("exports 경로가 안전한 폴더가 아닙니다.")
        exports.mkdir(parents=True, exist_ok=True)
        fingerprint = json.loads(manifest_bytes)["input_fingerprint_sha256"]
        name = f"{project_id}-r{revision}-{fingerprint}.zip"
        output = exports / name
        if output.exists() and _is_link(output):
            raise ProjectArchiveError("기존 백업 경로에 심볼릭 링크를 사용할 수 없습니다.")
        cached = _cached_archive_is_valid(output, manifest_bytes, files)
        if not cached:
            _write_archive(output, manifest_bytes, files, project_dir.resolve(strict=True))
        size, digest = _measure(output)
        return ProjectArchive(
            archive_id=f"r{revision}-{fingerprint}-{digest}",
            name=name,
            path=output,
            size=size,
            sha256=digest,
            cached=cached,
        )


def materialize_project_snapshot(
    project_dir: Path | str, destination: Path | str
) -> ProjectSnapshot:
    """Copy the validated canonical file set into a new hidden staging directory."""

    project_dir = Path(project_dir).expanduser().resolve(strict=True)
    destination = Path(destination).expanduser().resolve(strict=False)
    if destination.exists() or _is_link(destination):
        raise ProjectArchiveError("snapshot 대상 경로가 이미 존재합니다.")
    try:
        destination.relative_to(project_dir)
    except ValueError:
        pass
    else:
        raise ProjectArchiveError("snapshot 대상은 원본 프로젝트 밖이어야 합니다.")

    project, manifest_bytes, files = _collect(project_dir)
    manifest = json.loads(manifest_bytes)
    try:
        destination.mkdir(parents=True, exist_ok=False)
        for record in files:
            target = destination.joinpath(*PurePosixPath(record.member).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(record.path, target)
            size, digest = _measure(target)
            if size != record.size or digest != record.sha256:
                raise ProjectArchiveError("snapshot 복사 파일의 무결성이 일치하지 않습니다.")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return ProjectSnapshot(
        project_id=str(project["project_id"]),
        input_fingerprint_sha256=str(manifest["input_fingerprint_sha256"]),
        file_count=len(files),
    )


def project_input_fingerprint(project_dir: Path | str) -> str:
    """Return the canonical input fingerprint without creating an archive."""

    _, manifest_bytes, _ = _collect(Path(project_dir))
    return str(json.loads(manifest_bytes)["input_fingerprint_sha256"])


def resolve_project_archive(
    project_dir: Path | str, project_id: str, archive_id: str
) -> tuple[Path, str, str]:
    """Resolve a content-bound download id without accepting caller-supplied paths."""

    if not isinstance(project_id, str) or not ID_RE.fullmatch(project_id):
        raise ProjectArchiveError("project_id가 올바르지 않습니다.")
    match = ARCHIVE_ID_RE.fullmatch(archive_id or "")
    if match is None:
        raise ProjectArchiveError("archive_id가 올바르지 않습니다.")
    revision, fingerprint, expected_sha = match.groups()
    project_dir = Path(project_dir).resolve(strict=True)
    relative = PurePosixPath(
        "exports", f"{project_id}-r{revision}-{fingerprint}.zip"
    )
    path = _safe_file(project_dir, relative, label="백업 파일")
    _, actual_sha = _measure(path)
    if actual_sha != expected_sha:
        raise ProjectArchiveError("백업 파일 무결성이 일치하지 않습니다.")
    return path, path.name, expected_sha
