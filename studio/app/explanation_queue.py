"""Sequential AI explanation queue.

The question bank adds one item at a time ("AI 해설" on a card). A single
background worker processes the queue in order so only one AI explanation
runs at a time. The queue is persisted in the workspace so it survives a
restart; an entry that was running when the server stopped is put back in
line.

Mode is decided when the entry runs, from the item's current content: no
explanation -> write, existing explanation -> supplement. The worker reads
the current revision and saves through the normal CAS path, so an edit made
while the AI is writing makes the save fail instead of being overwritten.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .model import atomic_write_json, load_json_object

QUEUE_FILE = ".ai_explanation_queue.json"
ACTIVE_STATES = {"queued", "running"}
FINAL_STATES = {"saved", "sufficient", "held", "failed", "cancelled"}
KEEP_FINISHED = 200

Runner = Callable[[str, str], Mapping[str, Any]]
"""(project_id, item_id) -> outcome {state, mode, message}."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class QueueError(Exception):
    pass


class ExplanationQueue:
    def __init__(self, workspace_root: Path, runner: Runner, *, autostart: bool = True) -> None:
        self.path = Path(workspace_root) / QUEUE_FILE
        self.runner = runner
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._entries: list[dict[str, Any]] = self._load()
        self._thread: threading.Thread | None = None
        if autostart:
            self.start()

    # ---- persistence -------------------------------------------------
    def _load(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            raw = load_json_object(self.path)
        except Exception:
            return []
        entries = [dict(e) for e in raw.get("entries", []) if isinstance(e, Mapping)]
        for entry in entries:
            if entry.get("state") == "running":  # interrupted by a restart -> back in line
                entry["state"] = "queued"
                entry["message"] = "서버 재시작으로 다시 대기"
        return entries

    def _save(self) -> None:
        finished = [e for e in self._entries if e.get("state") in FINAL_STATES]
        if len(finished) > KEEP_FINISHED:
            drop = {id(e) for e in finished[: len(finished) - KEEP_FINISHED]}
            self._entries = [e for e in self._entries if id(e) not in drop]
        atomic_write_json(self.path, {"schema_version": "0.1.0", "entries": self._entries})

    # ---- public API --------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            entries = [dict(e) for e in self._entries]
        return {
            "entries": entries,
            "queued": sum(e.get("state") == "queued" for e in entries),
            "running": sum(e.get("state") == "running" for e in entries),
        }

    def enqueue(self, project_id: str, item_id: str, label: str = "") -> dict[str, Any]:
        with self._lock:
            for entry in self._entries:
                if (entry.get("project_id"), entry.get("item_id")) == (project_id, item_id) and entry.get("state") in ACTIVE_STATES:
                    raise QueueError("이미 대기열에 있는 문항입니다.")
            entry = {
                "id": "aiq-" + uuid.uuid4().hex[:12],
                "project_id": project_id,
                "item_id": item_id,
                "label": str(label or "")[:120],
                "state": "queued",
                "mode": None,
                "message": "",
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
            }
            self._entries.append(entry)
            self._save()
        self._wake.set()
        return dict(entry)

    def cancel(self, entry_id: str) -> dict[str, Any]:
        with self._lock:
            for entry in self._entries:
                if entry.get("id") == entry_id:
                    if entry.get("state") != "queued":
                        raise QueueError("대기 중인 문항만 취소할 수 있습니다.")
                    entry.update(state="cancelled", finished_at=_now(), message="사용자가 취소")
                    self._save()
                    return dict(entry)
        raise KeyError(entry_id)

    def clear_finished(self) -> int:
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.get("state") in ACTIVE_STATES]
            self._save()
            return before - len(self._entries)

    # ---- worker ------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="ai-explanation-queue", daemon=True)
        self._thread.start()
        self._wake.set()

    def _next(self) -> dict[str, Any] | None:
        with self._lock:
            for entry in self._entries:
                if entry.get("state") == "queued":
                    entry.update(state="running", started_at=_now(), message="")
                    self._save()
                    return entry
        return None

    def run_once(self) -> bool:
        """Process one queued entry. Returns False when nothing was queued."""
        entry = self._next()
        if entry is None:
            return False
        try:
            outcome = dict(self.runner(str(entry["project_id"]), str(entry["item_id"])))
        except Exception as exc:  # noqa: BLE001 - recorded on the entry
            outcome = {"state": "failed", "message": str(exc) or type(exc).__name__}
        state = outcome.get("state") if outcome.get("state") in FINAL_STATES else "failed"
        with self._lock:
            entry.update(
                state=state,
                mode=outcome.get("mode", entry.get("mode")),
                message=str(outcome.get("message") or "")[:300],
                finished_at=_now(),
            )
            self._save()
        return True

    def _loop(self) -> None:
        while True:
            self._wake.wait(timeout=30)
            self._wake.clear()
            while self.run_once():
                pass
