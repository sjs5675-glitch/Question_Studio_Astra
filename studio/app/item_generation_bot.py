"""'봇과 상의하기': talk to the item-analyst Hermes bot and import the drafts it writes.

The bot runs as ``hermes -p item-analyst chat -t file`` (file tools only, no
terminal), with ``HERMES_WRITE_SAFE_ROOT`` pinned to one per-conversation
folder under ``C:/Question_Item_Lab/app_drafts``. The app never trusts the
bot's file as a finished item: imported drafts go through the same format
checks, figure check and Codex verification as quick-generated drafts.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .item_generation import (
    ItemGenerationError,
    _LOCK,
    jobs_root,
    lab_root,
    resolve_unit,
)
from .model import atomic_write_json, utc_now


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data)

HERMES_BIN = os.environ.get("ASTRA_ITEMGEN_HERMES_BIN",
                            "C:/Users/ExampleUser/AppData/Local/hermes/hermes-agent/venv/Scripts/hermes.exe")
BOT_PROFILE = "item-analyst"
DRAFT_FILE = "drafts.json"
_CHAT_ID = re.compile(r"^chat-[0-9a-f]{20}$")

FIRST_TURN = """[Forest of Q 앱 '문항 생성' 메뉴에서 온 대화]
교사가 문항 출제를 상의하려고 한다. 대상: {scope} / {major} > {middle}

작업 규칙:
- 너의 SOUL 지침(학년 범위, 정답 유일성, 출제 의도, 그림 규칙)을 그대로 따른다.
- 학습 자료는 C:/Question_Item_Lab 아래(scope/, patterns/, projects/, guides/)에서 Read로 읽을 수 있다.
- 파일 쓰기는 이 폴더만 가능하다: {draft_dir}
- 교사가 '초안 저장'을 요청하면 {draft_dir}/drafts.json 에 {{"items": [...]}} 형식으로 저장한다.
  각 item 형식은 C:/Question_Item_Lab/tools/generate_items.py 의 출력 형식과 같다
  (kind, source_item_id, difficulty, format, scope_concepts, stem, table, figure, instruction, bogi, choices,
   answer, solution, design_intent, distractor_intents, unique_answer_rationale, calc_checks).
  그림이 필요하면 figure에 spec만 쓰고 그림 파일은 만들지 않는다(앱이 작도한다).
- 정답 검증·그림 작도·문제은행 저장은 앱이 한다. 너는 초안까지만 만든다.
- 답은 짧게, 한국어로.

교사 메시지:
{message}"""


def chats_root(workspace: Path | str) -> Path:
    return jobs_root(workspace) / "chats"


def drafts_root(root: Path | None = None) -> Path:
    return (root or lab_root()) / "app_drafts"


def _chat_path(workspace: Path | str, chat_id: str) -> Path:
    if not _CHAT_ID.fullmatch(str(chat_id)):
        raise ItemGenerationError("대화 ID가 올바르지 않습니다.")
    return chats_root(workspace) / f"{chat_id}.json"


def load_chat(workspace: Path | str, chat_id: str) -> dict[str, Any]:
    path = _chat_path(workspace, chat_id)
    if not path.is_file():
        raise ItemGenerationError("대화를 찾을 수 없습니다.")
    return json.loads(path.read_text(encoding="utf-8"))


def list_chats(workspace: Path | str, limit: int = 30) -> list[dict[str, Any]]:
    base = chats_root(workspace)
    if not base.is_dir():
        return []
    rows = []
    for path in sorted(base.glob("chat-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            chat = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows.append({k: chat.get(k) for k in ("chat_id", "created_at", "updated_at", "request", "status", "turn_count")}
                    | {"last": (chat.get("turns") or [{}])[-1].get("text", "")[:120]})
    return rows


def run_hermes(query: str, *, resume: str | None, write_root: Path, timeout: int = 900) -> dict[str, Any]:
    """Run one bot turn; return {"text", "session_id", "usage"}."""

    cmd = [HERMES_BIN, "-p", BOT_PROFILE, "chat", "-t", "file", "-Q", "--format", "stream-json", "--query-file", "-"]
    if resume:
        cmd += ["--resume", resume]
    env = dict(os.environ, HERMES_WRITE_SAFE_ROOT=str(write_root), PYTHONIOENCODING="utf-8")
    started = time.time()
    proc = subprocess.run(cmd, input=query, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          cwd=str(write_root), env=env, timeout=timeout)
    text_parts, session_id, result = [], None, None
    tools = []
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("session_id"):
            session_id = event["session_id"]
        elif kind == "text":
            text_parts.append(str(event.get("text") or ""))
        elif kind == "tool_use":
            tools.append(str(event.get("name") or ""))
        elif kind == "result":
            result = event
            session_id = event.get("session_id") or session_id
    if result is None and proc.returncode != 0:
        raise ItemGenerationError(f"봇 실행 실패(rc={proc.returncode}): {(proc.stderr or proc.stdout)[-400:]}")
    text = "".join(text_parts).strip() or str((result or {}).get("result") or "").strip()
    return {"text": text, "session_id": session_id, "tools": tools,
            "usage": (result or {}).get("usage"), "duration_s": round(time.time() - started, 1)}


class BotChats:
    """Per-chat locks plus one background thread per in-flight turn."""

    def __init__(self, workspace_root: Path, *, runner: Callable[..., dict[str, Any]] = run_hermes,
                 lab: Path | None = None) -> None:
        self.workspace = Path(workspace_root).resolve()
        self.runner = runner
        self.lab = lab
        self._busy: set[str] = set()
        self._guard = threading.Lock()

    def draft_dir(self, chat_id: str) -> Path:
        return drafts_root(self.lab) / chat_id

    def create(self, request: Mapping[str, Any], message: str, *, user: str = "") -> dict[str, Any]:
        resolve_unit(request["scope"], request["major"], request["middle"], self.lab)
        message = str(message or "").strip()
        if not message:
            raise ItemGenerationError("메시지를 입력하세요.")
        chat_id = "chat-" + os.urandom(10).hex()
        draft_dir = self.draft_dir(chat_id)
        draft_dir.mkdir(parents=True, exist_ok=False)
        chat = {"chat_id": chat_id, "created_at": utc_now(), "updated_at": utc_now(), "request": dict(request),
                "status": "idle", "session_id": None, "turns": [], "turn_count": 0, "usage": [],
                "draft_dir": str(draft_dir)}
        with _LOCK:
            _atomic_write_json(_chat_path(self.workspace, chat_id), chat)
        return self.send(chat_id, message, first=True, user=user)

    def send(self, chat_id: str, message: str, *, first: bool = False, user: str = "") -> dict[str, Any]:
        message = str(message or "").strip()[:4000]
        if not message:
            raise ItemGenerationError("메시지를 입력하세요.")
        with self._guard:
            if chat_id in self._busy:
                raise ItemGenerationError("봇이 아직 답하는 중입니다.")
            self._busy.add(chat_id)
        try:
            with _LOCK:
                chat = load_chat(self.workspace, chat_id)
                chat["turns"].append({"role": "user", "text": message, "at": utc_now(), "user": user})
                chat["status"] = "thinking"
                chat["updated_at"] = utc_now()
                _atomic_write_json(_chat_path(self.workspace, chat_id), chat)
        except Exception:
            with self._guard:
                self._busy.discard(chat_id)
            raise
        request = chat["request"]
        query = FIRST_TURN.format(scope=request["scope"], major=request["major"], middle=request["middle"],
                                  draft_dir=chat["draft_dir"].replace("\\", "/"), message=message) if first else message
        threading.Thread(target=self._turn, args=(chat_id, query, chat.get("session_id")), daemon=True,
                         name=f"bot-{chat_id}").start()
        return chat

    def _turn(self, chat_id: str, query: str, resume: str | None) -> None:
        try:
            try:
                reply = self.runner(query, resume=resume, write_root=self.draft_dir(chat_id))
                error = None
            except Exception as exc:  # noqa: BLE001 - shown in the chat
                reply, error = None, str(exc)[:600]
            with _LOCK:
                chat = load_chat(self.workspace, chat_id)
                if reply:
                    chat["session_id"] = reply.get("session_id") or chat.get("session_id")
                    chat["turns"].append({"role": "bot", "text": reply.get("text") or "(빈 답)", "at": utc_now(),
                                          "tools": reply.get("tools") or []})
                    chat["usage"].append({"at": utc_now(), "usage": reply.get("usage"), "duration_s": reply.get("duration_s")})
                    chat["status"] = "idle"
                else:
                    chat["turns"].append({"role": "error", "text": error, "at": utc_now()})
                    chat["status"] = "error"
                chat["turn_count"] = sum(1 for t in chat["turns"] if t["role"] == "user")
                chat["has_drafts"] = (self.draft_dir(chat_id) / DRAFT_FILE).is_file()
                chat["updated_at"] = utc_now()
                _atomic_write_json(_chat_path(self.workspace, chat_id), chat)
        finally:
            with self._guard:
                self._busy.discard(chat_id)

    def read_drafts(self, chat_id: str) -> list[Any]:
        load_chat(self.workspace, chat_id)
        path = self.draft_dir(chat_id) / DRAFT_FILE
        if path.is_symlink() or not path.is_file():
            raise ItemGenerationError("봇이 아직 초안 파일을 저장하지 않았습니다. 봇에게 '초안 저장'을 요청하세요.")
        if path.stat().st_size > 2_000_000:
            raise ItemGenerationError("초안 파일이 너무 큽니다.")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ItemGenerationError(f"초안 파일 JSON 오류: {exc}") from exc
        items = data.get("items") if isinstance(data, Mapping) else data
        if not isinstance(items, list):
            raise ItemGenerationError("초안 파일에 items 목록이 없습니다.")
        return items
