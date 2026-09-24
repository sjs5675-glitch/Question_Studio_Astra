from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astra_runtime import bootstrap, inside, validate_parsed_paths

ASTRA = bootstrap()
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "0.1.0"
STATUSES = frozenset({"running", "completed", "failed"})
PROGRESS_FIELDS = (
    "schema_version",
    "status",
    "current_item",
    "stage",
    "stage_text",
    "verification_file_count",
    "crop_count",
    "latest_verification",
    "updated_at",
)
CROP_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"})


class ProgressError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _run_directory(run_dir: str | Path) -> Path:
    try:
        resolved = Path(run_dir).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ProgressError(f"run directory not found: {run_dir}") from exc
    if not resolved.is_dir():
        raise ProgressError(f"run directory not found: {resolved}")
    return resolved


def _safe_directory(root: Path, *parts: str, create: bool = False) -> Path:
    candidate = root.joinpath(*parts)
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ProgressError(f"artifact directory escapes run directory: {candidate}")
    if create:
        inside(resolved, "recovery output")
        resolved.mkdir(parents=True, exist_ok=True)
    if resolved.exists() and not resolved.is_dir():
        raise ProgressError(f"artifact directory is not a directory: {resolved}")
    return resolved


def recovery_progress_path(run_dir: str | Path, *, create: bool = False) -> Path:
    root = _run_directory(run_dir)
    return _safe_directory(root, "solutions", create=create) / "recovery-progress.json"


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProgressError(f"{name} must be a non-negative integer")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ProgressError(f"{name} must be a string")
    return value


def _verification_name(value: object) -> str | None:
    if value is None:
        return None
    name = _text(value, "latest_verification")
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ProgressError("latest_verification must be a basename or null")
    return name


def build_recovery_progress(
    *,
    status: str,
    current_item: str = "",
    stage: str = "",
    stage_text: str = "",
    verification_file_count: int = 0,
    crop_count: int = 0,
    latest_verification: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    if status not in STATUSES:
        raise ProgressError(f"invalid recovery status: {status}")
    timestamp = utc_now() if updated_at is None else _text(updated_at, "updated_at")
    if not timestamp:
        raise ProgressError("updated_at must not be empty")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "current_item": _text(current_item, "current_item"),
        "stage": _text(stage, "stage"),
        "stage_text": _text(stage_text, "stage_text"),
        "verification_file_count": _count(
            verification_file_count, "verification_file_count"
        ),
        "crop_count": _count(crop_count, "crop_count"),
        "latest_verification": _verification_name(latest_verification),
        "updated_at": timestamp,
    }


def _validate_progress(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(PROGRESS_FIELDS):
        raise ProgressError("invalid recovery progress fields")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ProgressError("unsupported recovery progress schema")
    return build_recovery_progress(
        status=value.get("status"),
        current_item=value.get("current_item"),
        stage=value.get("stage"),
        stage_text=value.get("stage_text"),
        verification_file_count=value.get("verification_file_count"),
        crop_count=value.get("crop_count"),
        latest_verification=value.get("latest_verification"),
        updated_at=value.get("updated_at"),
    )


def write_recovery_progress(
    run_dir: str | Path,
    *,
    status: str,
    current_item: str = "",
    stage: str = "",
    stage_text: str = "",
    verification_file_count: int = 0,
    crop_count: int = 0,
    latest_verification: str | None = None,
    updated_at: str | None = None,
) -> Path:
    progress = build_recovery_progress(
        status=status,
        current_item=current_item,
        stage=stage,
        stage_text=stage_text,
        verification_file_count=verification_file_count,
        crop_count=crop_count,
        latest_verification=latest_verification,
        updated_at=updated_at,
    )
    output = recovery_progress_path(run_dir, create=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(progress, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output


def read_recovery_progress(run_dir: str | Path) -> dict[str, Any] | None:
    path = recovery_progress_path(run_dir)
    if path.is_symlink():
        raise ProgressError(f"recovery progress must not be a symlink: {path}")
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProgressError(f"cannot read recovery progress: {path}") from exc
    return _validate_progress(value)


def _artifact_files(directory: Path, suffixes: frozenset[str]) -> list[tuple[int, str]]:
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ProgressError(f"artifact path is not a directory: {directory}")
    files: list[tuple[int, str]] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if (
                        entry.is_file(follow_symlinks=False)
                        and Path(entry.name).suffix.casefold() in suffixes
                    ):
                        files.append((entry.stat(follow_symlinks=False).st_mtime_ns, entry.name))
                except FileNotFoundError:
                    continue
    except OSError as exc:
        raise ProgressError(f"cannot scan artifact directory: {directory}") from exc
    return files


def current_item_from_verification(name: str | None) -> str:
    if not name:
        return ""
    match = re.match(r"^(p\d+-q[^-]+(?:-sub-[^-]+)?)", name, flags=re.IGNORECASE)
    return match.group(1) if match else Path(name).stem


def scan_recovery_artifacts(run_dir: str | Path) -> dict[str, Any]:
    root = _run_directory(run_dir)
    solutions = _safe_directory(root, "solutions")
    verifications = _artifact_files(
        _safe_directory(solutions, "detections", "verifications"),
        frozenset({".json"}),
    )
    crops = _artifact_files(_safe_directory(solutions, "crops"), CROP_SUFFIXES)
    latest = max(verifications, default=None)
    latest_name = latest[1] if latest is not None else None
    return {
        "verification_file_count": len(verifications),
        "crop_count": len(crops),
        "latest_verification": latest_name,
        "current_item": current_item_from_verification(latest_name),
    }


def refresh_recovery_progress(
    run_dir: str | Path,
    *,
    status: str | None = None,
    current_item: str | None = None,
    stage: str | None = None,
    stage_text: str | None = None,
) -> dict[str, Any]:
    previous = read_recovery_progress(run_dir)
    artifacts = scan_recovery_artifacts(run_dir)
    selected_status = (
        status if status is not None else (previous or {}).get("status") or "running"
    )
    latest_changed = artifacts["latest_verification"] != (previous or {}).get(
        "latest_verification"
    )
    selected_item = current_item
    if selected_item is None:
        selected_item = (
            artifacts["current_item"]
            if latest_changed or previous is None
            else previous["current_item"]
        )
    if stage is None:
        stage = (
            selected_status
            if selected_status in {"completed", "failed"}
            else (previous or {}).get("stage") or "monitoring"
        )
    if stage_text is None:
        terminal_text = {
            "completed": "Recovery completed",
            "failed": "Recovery failed",
        }
        stage_text = (
            terminal_text[selected_status]
            if status in terminal_text
            else (previous or {}).get("stage_text")
            or terminal_text.get(selected_status, "Watching recovery artifacts")
        )
    output = write_recovery_progress(
        run_dir,
        status=selected_status,
        current_item=selected_item,
        stage=stage,
        stage_text=stage_text,
        verification_file_count=artifacts["verification_file_count"],
        crop_count=artifacts["crop_count"],
        latest_verification=artifacts["latest_verification"],
    )
    return read_recovery_progress(output.parent.parent) or {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="원자적으로 recovery 진행 상태를 기록합니다.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--status", required=True, choices=sorted(STATUSES))
    parser.add_argument("--current-item", default=None)
    parser.add_argument("--stage", default=None)
    parser.add_argument("--stage-text", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_parsed_paths(args)
        progress = refresh_recovery_progress(
            args.run_dir,
            status=args.status,
            current_item=args.current_item,
            stage=args.stage,
            stage_text=args.stage_text,
        )
        print(json.dumps(progress, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as exc:
        print(f"recovery progress write failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
