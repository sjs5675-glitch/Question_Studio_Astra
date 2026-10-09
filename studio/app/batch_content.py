from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import stat
from threading import Lock
from typing import Final, TypedDict

from .content_status import CONTENT_STATUS_LABELS, ContentState, stored_content_status
from .model import ModelError, load_json_object, validate_id


class ContentProjection(TypedDict, total=False):
    content_counts: dict[str, int]
    content_error_count: int


@dataclass(frozen=True, slots=True)
class _ItemState:
    signature: tuple[int, int]
    status: ContentState | None


@dataclass(frozen=True, slots=True)
class _ProjectState:
    signature: tuple[int, int]
    item_order: tuple[str, ...]
    items: Mapping[str, _ItemState]


_CACHE: Final[dict[Path, _ProjectState]] = {}
_CACHE_LOCK: Final = Lock()


def _signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def project_content_projection(
    project_dir: Path, *, is_deleted: Callable[[str], bool] | None = None,
) -> ContentProjection:
    """Reuse saved states, refreshing changed files outside the cache/job locks."""
    project_dir = project_dir.resolve()
    project_path = project_dir / "project.json"
    signature = _signature(project_path)
    with _CACHE_LOCK:
        previous = _CACHE.get(project_dir)
    if previous is not None and previous.signature == signature:
        item_order = previous.item_order
    else:
        item_order = tuple(load_json_object(project_path).get("item_order") or [])
    item_root = (project_dir / "items").resolve()
    links: set[str] | None = None
    if item_root.is_relative_to(project_dir):
        try:
            with os.scandir(item_root) as entries:
                links = {entry.name for entry in entries if _is_link(entry.stat(follow_symlinks=False))}
        except OSError:
            links = None
    items: dict[str, _ItemState] = {}
    counts: dict[str, int] = dict.fromkeys(CONTENT_STATUS_LABELS, 0)
    errors = 0
    for item_id in item_order:
        if is_deleted is not None and is_deleted(item_id):
            if previous is not None and item_id in previous.items:
                items[item_id] = previous.items[item_id]
            continue
        try:
            validate_id(item_id, field="item_id")
            if not item_root.is_relative_to(project_dir):
                errors += 1
                continue
            path = item_root / item_id / "question.json"
            info = path.stat(follow_symlinks=False)
            if links is None or item_id in links or _is_link(info):
                path = path.resolve()
                if not path.is_relative_to(project_dir):
                    errors += 1
                    continue
                info = path.stat()
            item_signature = (info.st_mtime_ns, info.st_size)
            cached = items.get(item_id) or (previous.items.get(item_id) if previous else None)
            if cached is None or cached.signature != item_signature:
                try:
                    status = stored_content_status(load_json_object(path))["status"]
                except (ModelError, OSError):
                    status = None
                cached = _ItemState(item_signature, status)
                if _signature(path) == item_signature:
                    items[item_id] = cached
            else:
                items[item_id] = cached
            if cached.status is None:
                errors += 1
            else:
                counts[cached.status] += 1
        except (ModelError, OSError):
            errors += 1
    if _signature(project_path) == signature:
        snapshot = _ProjectState(signature, item_order, items)
        with _CACHE_LOCK:
            # A slower request must not replace a snapshot published while it read.
            if _CACHE.get(project_dir) is previous:
                _CACHE[project_dir] = snapshot
    return {"content_counts": counts, "content_error_count": errors}
