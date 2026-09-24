"""Read the newest safe recovery progress record from one configured root."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


MAX_PROGRESS_BYTES = 64 * 1024
PROGRESS_RELATIVE = Path("solutions") / "recovery-progress.json"


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _safe_record(root: Path, path: Path) -> dict[str, Any] | None:
    current = root
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    for part in relative.parts:
        current /= part
        if _is_link(current):
            return None
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
        if path.stat().st_size > MAX_PROGRESS_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != "0.1.0"
        or value.get("status") not in {
        "running",
        "completed",
        "failed",
        }
    ):
        return None
    current_item = value.get("current_item")
    stage = value.get("stage")
    stage_text = value.get("stage_text")
    latest = value.get("latest_verification")
    updated_at = value.get("updated_at")
    verification_count = value.get("verification_file_count")
    crop_count = value.get("crop_count")
    if (
        current_item is not None
        and (not isinstance(current_item, str) or len(current_item) > 256)
    ):
        return None
    if not isinstance(stage, str) or not stage or len(stage) > 128:
        return None
    if not isinstance(stage_text, str) or not stage_text or len(stage_text) > 500:
        return None
    if latest is not None and (
        not isinstance(latest, str)
        or len(latest) > 255
        or Path(latest).name != latest
        or "\\" in latest
    ):
        return None
    if (
        type(verification_count) is not int
        or verification_count < 0
        or type(crop_count) is not int
        or crop_count < 0
        or not isinstance(updated_at, str)
    ):
        return None
    try:
        datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return {
        "status": value["status"],
        "current_item": current_item,
        "stage": stage,
        "stage_text": stage_text,
        "verification_file_count": verification_count,
        "crop_count": crop_count,
        "latest_verification": latest,
        "updated_at": updated_at,
    }


def newest_recovery_progress(recovery_root: Path | str) -> dict[str, Any] | None:
    """Return only whitelisted fields; caller-supplied paths never reach this API."""

    root = Path(recovery_root).expanduser().resolve(strict=False)
    if not root.is_dir() or _is_link(root):
        return None
    candidates = []
    for path in root.rglob(PROGRESS_RELATIVE.as_posix()):
        try:
            candidates.append((path.stat().st_mtime_ns, path.as_posix().casefold(), path))
        except OSError:
            continue
    for _, _, path in sorted(candidates, reverse=True):
        record = _safe_record(root, path)
        if record is not None:
            return record
    return None
