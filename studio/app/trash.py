from __future__ import annotations

import copy
import hashlib
import json
import shutil
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from .model import ModelError, atomic_write_json, load_json_object, sha256_file, utc_now, validate_id


class TrashError(ModelError):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


_LOCKS: dict[Path, threading.RLock] = {}
_GUARD = threading.Lock()


def _lock(root: Path) -> threading.RLock:
    with _GUARD:
        return _LOCKS.setdefault(root, threading.RLock())


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class Trash:
    def __init__(self, workspace: Path | str, busy: Callable[[str], bool] | None = None) -> None:
        self.root = Path(workspace).resolve()
        self.path = self.root / "_internal" / "trash.json"
        self.journals = self.root / "_internal" / "purge"
        self.lock = _lock(self.root)
        self.busy = busy or (lambda _project_id: False)

    def _safe(self, path: Path, owner: Path | None = None) -> Path:
        base = self.root if owner is None else owner
        if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
            raise TrashError("삭제 경로가 소유 폴더 밖입니다.", 422)
        return path

    def _state(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "revision": 0, "projects": {}, "items": {}}
        value = load_json_object(self._safe(self.path))
        if (value.get("schema_version") != 1 or type(value.get("revision")) is not int
                or value["revision"] < 0 or not isinstance(value.get("projects"), dict)
                or not isinstance(value.get("items"), dict)
                or any(not isinstance(pid, str) or not isinstance(stamp, str)
                       for pid, stamp in value.get("projects", {}).items())
                or any(not isinstance(pid, str) or not isinstance(items, dict)
                       or any(not isinstance(iid, str) or not isinstance(stamp, str)
                              for iid, stamp in items.items())
                       for pid, items in value.get("items", {}).items())):
            raise TrashError("휴지통 파일이 손상되었습니다.", 500)
        return value

    def pending(self, project_id: str, item_id: str | None = None) -> bool:
        for path in self.journals.glob("purge-*.json"):
            record = load_json_object(self._safe(path))
            plan = record.get("preview", {})
            if record.get("stage") != "done" and plan.get("project_id") == project_id:
                if item_id is None or plan.get("kind") == "project" or plan.get("item_id") == item_id:
                    return True
        return False

    def require_active(self, project_id: str, item_id: str | None = None) -> None:
        if self.pending(project_id, item_id):
            raise TrashError("완전 삭제 작업을 먼저 재개해야 합니다.")
        if self.is_deleted(project_id, item_id):
            raise TrashError("휴지통에 있는 대상입니다. 먼저 복원하세요.", 410)

    def deletion_snapshot(self) -> Callable[[str, str | None], bool]:
        """Fresh per-list predicate; never retained across requests or writes."""
        with self.lock:
            state = self._state()
        def deleted(project_id: str, item_id: str | None = None) -> bool:
            return project_id in state["projects"] or (item_id is not None and item_id in state["items"].get(project_id, {}))
        return deleted

    def is_deleted(self, project_id: str, item_id: str | None = None) -> bool:
        state = self._state()
        return project_id in state["projects"] or (item_id is not None and item_id in state["items"].get(project_id, {}))

    def _target(self, kind: str, project_id: str, item_id: str | None) -> tuple[Path, dict[str, Any], list[str]]:
        if kind not in {"project", "item"}:
            raise TrashError("삭제 대상 종류가 올바르지 않습니다.", 400)
        validate_id(project_id, field="project_id")
        if not project_id.startswith("project-"):
            raise TrashError("프로젝트 ID가 올바르지 않습니다.", 400)
        project_dir = self._safe(self.root / project_id)
        project_path = self._safe(project_dir / "project.json", project_dir)
        if not project_path.is_file():
            raise TrashError("프로젝트를 찾을 수 없습니다.", 404)
        project = load_json_object(project_path)
        if project.get("project_id") != project_id:
            raise TrashError("프로젝트 식별자가 일치하지 않습니다.")
        ids = [validate_id(value, field="item_id") for value in project.get("item_order", [])]
        ids.extend(path.parent.name for path in sorted((project_dir / "items").glob("*/question.json"))
                   if path.parent.name not in ids and self._safe(path, project_dir).is_file())
        if kind == "project":
            if item_id is not None:
                raise TrashError("프로젝트 요청에 문항 ID를 넣을 수 없습니다.", 400)
            return project_dir, project, ids
        validate_id(item_id, field="item_id")
        if item_id not in ids:
            raise TrashError("문항을 찾을 수 없습니다.", 404)
        item_dir = self._safe(project_dir / "items" / item_id, project_dir)
        question = self._safe(item_dir / "question.json", item_dir)
        if not question.is_file() or load_json_object(question).get("item_id") != item_id:
            raise TrashError("문항 저장 파일을 찾을 수 없습니다.", 404)
        return item_dir, project, [item_id]

    def _fingerprint(self, kind: str, target: Path, project: Mapping[str, Any], ids: list[str]) -> str:
        files = [target / "project.json"] if kind == "project" else [target / "question.json"]
        if kind == "project":
            files += [target / "items" / item_id / "question.json" for item_id in ids]
        return _digest([(str(path.relative_to(self.root)), sha256_file(self._safe(path))) for path in files])

    def _sets(self, project_id: str, ids: list[str]) -> list[dict[str, Any]]:
        result = []
        for path in sorted((self.root / "question_sets").glob("set-*.json")):
            doc = load_json_object(self._safe(path))
            hits = [ref for ref in doc.get("items", []) if ref.get("project_id") == project_id and ref.get("item_id") in ids]
            if hits:
                result.append({"set_id": doc["set_id"], "title": doc["title"], "revision": doc["revision"], "remove_count": len(hits)})
        return result

    def preview(self, kind: str, project_id: str, item_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            target, project, ids = self._target(kind, project_id, item_id)
            package = load_json_object(target / "question.json") if kind == "item" else None
            metadata = project.get("metadata") or {}
            name = (metadata.get("book_title") or metadata.get("filename_base") or project_id) if kind == "project" else (package.get("identity") or {}).get("printed_number") or item_id
            source = ((package.get("provenance") or {}).get("problem") or {}).get("original_filename") if package else None
            return {"kind": kind, "project_id": project_id, "item_id": item_id,
                    "name": name, "source_filename": source,
                    "item_count": len(ids), "question_sets": self._sets(project_id, ids),
                    "busy": self.busy(project_id), "trash_revision": self._state()["revision"],
                    "target_fingerprint": self._fingerprint(kind, target, project, ids)}

    def list(self) -> dict[str, Any]:
        with self.lock:
            state = self._state()
            entries = [{"kind": "project", "project_id": pid, "item_id": None, "deleted_at": stamp}
                       for pid, stamp in state["projects"].items()]
            entries += [{"kind": "item", "project_id": pid, "item_id": iid, "deleted_at": stamp}
                        for pid, items in state["items"].items() if pid not in state["projects"]
                        for iid, stamp in items.items()]
            for entry in entries:
                entry["status"] = "trashed"
                try:
                    target, project, ids = self._target(entry["kind"], entry["project_id"], entry["item_id"])
                    package = load_json_object(target / "question.json") if entry["kind"] == "item" else None
                    metadata = project.get("metadata") or {}
                    entry["name"] = (metadata.get("book_title") or metadata.get("filename_base") or entry["project_id"]) if package is None else (package.get("identity") or {}).get("printed_number") or entry["item_id"]
                    entry["item_count"] = len(ids)
                except TrashError:
                    entry["name"] = entry["item_id"] or entry["project_id"]
            for journal_path in self.journals.glob("purge-*.json"):
                journal = load_json_object(self._safe(journal_path))
                if journal.get("stage") == "done":
                    continue
                plan = journal.get("preview", {})
                entry = next((entry for entry in entries if entry["kind"] == plan.get("kind")
                              and entry["project_id"] == plan.get("project_id")
                              and entry["item_id"] == plan.get("item_id")), None)
                if entry is None:
                    entry = {"kind": plan.get("kind"), "project_id": plan.get("project_id"),
                             "item_id": plan.get("item_id"), "deleted_at": None}
                    entries.append(entry)
                entry.update(status="deleting", operation_id=journal_path.stem,
                             manifest_digest=journal.get("digest"),
                             expected_trash_revision=plan.get("trash_revision"),
                             expected_revisions={value["set_id"]: value["revision"]
                                                 for value in plan.get("question_sets", [])})
            return {"trash_revision": state["revision"], "entries": entries}

    def move_restore(self, action: str, body: Mapping[str, Any]) -> dict[str, Any]:
        kind, pid, iid = body.get("kind"), body.get("project_id"), body.get("item_id")
        with self.lock:
            target, project, ids = self._target(kind, pid, iid)
            state = self._state()
            if self.pending(pid, iid):
                raise TrashError("완전 삭제 작업을 먼저 재개해야 합니다.")
            if body.get("expected_trash_revision") != state["revision"]:
                raise TrashError("휴지통이 변경되었습니다. 다시 확인하세요.")
            if self.busy(pid):
                raise TrashError("진행 중인 작업이 있어 삭제할 수 없습니다.")
            if action == "move":
                if body.get("target_fingerprint") != self._fingerprint(kind, target, project, ids):
                    raise TrashError("대상 자료가 변경되었습니다. 다시 확인하세요.")
                selected = state["projects"] if kind == "project" else state["items"].setdefault(pid, {})
                key = pid if kind == "project" else iid
                if key not in selected:
                    selected[key] = utc_now()
                    state["revision"] += 1
                    atomic_write_json(self.path, state)
            elif action == "restore":
                if kind == "item" and pid in state["projects"]:
                    raise TrashError("프로젝트를 먼저 복원하세요.")
                selected = state["projects"] if kind == "project" else state["items"].get(pid, {})
                key = pid if kind == "project" else iid
                if key in selected:
                    del selected[key]
                    state["revision"] += 1
                    atomic_write_json(self.path, state)
            else:
                raise TrashError("휴지통 작업이 올바르지 않습니다.", 400)
            return {"status": "deleted" if action == "move" else "restored", "trash_revision": state["revision"]}

    def preview_items(self, refs: list[dict[str, str]]) -> dict[str, Any]:
        if not isinstance(refs, list) or not refs or len(refs) > 5000 or any(not isinstance(r, dict) for r in refs):
            raise TrashError("삭제할 문항 목록이 올바르지 않습니다.", 400)
        keys = [(validate_id(r.get("project_id"), field="project_id"),
                 validate_id(r.get("item_id"), field="item_id")) for r in refs]
        if len(set(keys)) != len(keys):
            raise TrashError("중복된 삭제 문항이 있습니다.", 400)
        with self.lock:
            entries: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            for pid, iid in keys:
                try:
                    self.require_active(pid, iid)
                    entries.append(self.preview("item", pid, iid))
                except TrashError as exc:
                    failed.append({"project_id": pid, "item_id": iid, "status": "failed", "reason": str(exc)})
            return {"items": entries, "failed": failed, "trash_revision": self._state()["revision"]}

    def move_items(self, body: Mapping[str, Any]) -> dict[str, Any]:
        operation_id = validate_id(body.get("operation_id"), field="operation_id")
        if not operation_id.startswith("move-"):
            raise TrashError("삭제 작업 ID가 올바르지 않습니다.", 400)
        path = self._safe(self.root / "_internal" / "trash-moves" / f"{operation_id}.json")
        with self.lock:
            if path.exists():
                record = load_json_object(path)
                if record.get("items") != body.get("items") or body.get("expected_trash_revision") != record["items"][0]["trash_revision"]:
                    raise TrashError("삭제 작업 ID가 다른 요청에 사용되었습니다.")
            else:
                items = body.get("items")
                if not isinstance(items, list) or not items or len(items) > 5000:
                    raise TrashError("삭제할 문항 목록이 올바르지 않습니다.", 400)
                refs = [(validate_id(entry.get("project_id"), field="project_id"),
                         validate_id(entry.get("item_id"), field="item_id")) for entry in items if isinstance(entry, dict)]
                if len(refs) != len(items) or len(set(refs)) != len(items):
                    raise TrashError("삭제할 문항 목록이 올바르지 않습니다.", 400)
                expected = body.get("expected_trash_revision")
                if type(expected) is not int or any(entry.get("trash_revision") != expected or not isinstance(entry.get("target_fingerprint"), str) for entry in items):
                    raise TrashError("삭제 대상 확인 정보가 올바르지 않습니다.", 400)
                record = {"operation_id": operation_id, "items": items, "results": [], "status": "running",
                          "stale": self._state()["revision"] != expected}
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(path, record)
            done = {(entry["project_id"], entry["item_id"]) for entry in record["results"]}
            for entry in record["items"]:
                pid, iid = entry["project_id"], entry["item_id"]
                if (pid, iid) in done:
                    continue
                try:
                    state = self._state()
                    if record["stale"]:
                        raise TrashError("휴지통 버전이 변경되었습니다.")
                    if self.is_deleted(pid, iid):
                        result = {"project_id": pid, "item_id": iid, "status": "deleted"}
                    else:
                        self.move_restore("move", {"kind": "item", "project_id": pid, "item_id": iid,
                            "expected_trash_revision": state["revision"], "target_fingerprint": entry.get("target_fingerprint")})
                        result = {"project_id": pid, "item_id": iid, "status": "deleted"}
                except TrashError as exc:
                    result = {"project_id": pid, "item_id": iid, "status": "failed", "reason": str(exc)}
                record["results"].append(result)
                atomic_write_json(path, record)
            record["status"] = "done"
            atomic_write_json(path, record)
            return record

    def move_items_status(self, operation_id: str) -> dict[str, Any]:
        validate_id(operation_id, field="operation_id")
        if not operation_id.startswith("move-"):
            raise TrashError("삭제 작업 ID가 올바르지 않습니다.", 400)
        path = self._safe(self.root / "_internal" / "trash-moves" / f"{operation_id}.json")
        with self.lock:
            if not path.is_file():
                raise TrashError("삭제 작업을 찾을 수 없습니다.", 404)
            record = load_json_object(path)
            if record.get("status") == "running":
                return self.move_items({"operation_id": operation_id, "items": record["items"],
                                        "expected_trash_revision": record["items"][0]["trash_revision"]})
            return record

    def _files(self, target: Path) -> list[dict[str, Any]]:
        files = []
        for path in sorted(target.rglob("*")):
            if path.is_symlink():
                raise TrashError("삭제 폴더 안에 링크가 있습니다.", 422)
            if path.is_file():
                self._safe(path, target)
                files.append({"path": str(path.relative_to(self.root)).replace("\\", "/"), "sha256": sha256_file(path), "bytes": path.stat().st_size})
        return files

    def _shared(self, target: Path, excluded: set[tuple[str, str]]) -> list[dict[str, str]]:
        shared = []
        for question in self.root.glob("project-*/items/*/question.json"):
            pid, iid = question.parents[2].name, question.parent.name
            if (pid, iid) in excluded:
                continue
            doc = load_json_object(self._safe(question))
            assets = doc.get("resources", {}).get("assets", [])
            for asset in assets:
                raw = asset.get("path") if isinstance(asset, dict) else None
                if not isinstance(raw, str):
                    continue
                source = (question.parent / raw).resolve()
                if source.is_relative_to(target.resolve()) and source.is_file():
                    shared.append({"project_id": pid, "item_id": iid, "asset_id": asset.get("asset_id", ""),
                                   "old_path": raw, "source": str(source.relative_to(self.root)).replace("\\", "/"),
                                   "question_sha256": sha256_file(question)})
        return shared

    def purge_preview(self, kind: str, project_id: str, item_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            target, project, ids = self._target(kind, project_id, item_id)
            state = self._state()
            selected = state["projects"] if kind == "project" else state["items"].get(project_id, {})
            if (project_id if kind == "project" else item_id) not in selected:
                raise TrashError("휴지통에 있는 대상만 완전 삭제할 수 있습니다.")
            if self.busy(project_id):
                raise TrashError("진행 중인 작업이 있습니다.")
            if self.pending(project_id, item_id):
                raise TrashError("완전 삭제 작업을 먼저 재개해야 합니다.")
            files = self._files(target)
            excluded = {(project_id, iid) for iid in ids}
            shared = self._shared(target, excluded)
            if kind == "item":
                package = load_json_object(target / "question.json")
                for group in project.get("groups", []):
                    if group.get("shared_item_id") != item_id:
                        continue
                    survivor_id = next((member for member in group.get("member_item_ids", [])
                                        if member != item_id and (self.root / project_id / "items" / member / "question.json").is_file()), None)
                    if survivor_id is None:
                        continue
                    for asset in package.get("resources", {}).get("assets", []):
                        if asset.get("role") != "source_crop" or asset.get("section") != "question":
                            continue
                        source = (target / asset.get("path", "")).resolve()
                        if not source.is_file() or not source.is_relative_to(target):
                            continue
                        shared.append({"project_id": project_id, "item_id": survivor_id,
                                       "asset_id": asset["asset_id"], "old_path": None,
                                       "source": str(source.relative_to(self.root)).replace("\\", "/"),
                                       "owner_asset": copy.deepcopy(asset),
                                       "question_sha256": sha256_file(self.root / project_id / "items" / survivor_id / "question.json")})
            metadata = project.get("metadata") or {}
            package = load_json_object(target / "question.json") if kind == "item" else None
            name = (metadata.get("book_title") or metadata.get("filename_base") or project_id) if package is None else (package.get("identity") or {}).get("printed_number") or item_id
            source_filename = ((package.get("provenance") or {}).get("problem") or {}).get("original_filename") if package else None
            plan = {"kind": kind, "project_id": project_id, "item_id": item_id,
                    "name": name, "source_filename": source_filename,
                    "trash_revision": state["revision"], "target_fingerprint": self._fingerprint(kind, target, project, ids),
                    "item_ids": ids, "files": files, "question_sets": self._sets(project_id, ids), "shared": shared}
            return {**plan, "manifest_digest": _digest(plan), "delete_bytes": sum(file["bytes"] for file in files),
                    "notice": "기존 출력 파일과 외부 백업은 별도 사본으로 남습니다."}

    def purge(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if body.get("confirmed") is not True:
            raise TrashError("완전 삭제 확인이 필요합니다.", 400)
        operation_id = validate_id(body.get("operation_id"), field="operation_id")
        if not operation_id.startswith("purge-"):
            raise TrashError("작업 ID가 올바르지 않습니다.", 400)
        journal_path = self._safe(self.journals / f"{operation_id}.json")
        with self.lock:
            if journal_path.exists():
                journal = load_json_object(journal_path)
                if journal.get("digest") != body.get("manifest_digest"):
                    raise TrashError("완전 삭제 작업 ID가 다른 대상에 사용되었습니다.")
                if journal.get("stage") == "done":
                    return {"status": "purged", "operation_id": operation_id}
            else:
                preview = self.purge_preview(body.get("kind"), body.get("project_id"), body.get("item_id"))
                if (preview["manifest_digest"] != body.get("manifest_digest")
                        or preview["trash_revision"] != body.get("expected_trash_revision")):
                    raise TrashError("삭제 자료가 변경되었습니다. 다시 확인하세요.")
                expected = body.get("expected_revisions")
                revisions = {entry["set_id"]: entry["revision"] for entry in preview["question_sets"]}
                if expected != revisions:
                    raise TrashError("문제지가 변경되었습니다. 다시 확인하세요.")
                journal = {"stage": "prepared", "digest": preview["manifest_digest"], "preview": preview,
                           "operation_id": operation_id}
                atomic_write_json(journal_path, journal)
            self._finish(journal_path, journal)
            return {"status": "purged", "operation_id": operation_id}

    def _finish(self, path: Path, journal: dict[str, Any]) -> None:
        plan = journal["preview"]
        pid, iid, kind = plan["project_id"], plan["item_id"], plan["kind"]
        target = self._safe(self.root / pid if kind == "project" else self.root / pid / "items" / iid)
        doomed = set(plan["item_ids"])
        stage_dir = self._safe(self.journals / f"{path.stem}-stage")
        if journal["stage"] == "prepared":
            stage_dir.mkdir(parents=True, exist_ok=True)
            for index, entry in enumerate(plan["shared"]):
                source = self._safe(self.root / entry["source"], target)
                staged = self._safe(stage_dir / str(index), stage_dir)
                shutil.copy2(source, staged)
                if sha256_file(source) != sha256_file(staged):
                    raise TrashError("공유 자료 준비 확인에 실패했습니다.", 500)
            journal["stage"] = "commit"
            atomic_write_json(path, journal)
        if journal["stage"] == "commit":
            for index, entry in enumerate(plan["shared"]):
                survivor = self._safe(self.root / entry["project_id"] / "items" / entry["item_id"] / "question.json")
                doc = load_json_object(survivor)
                staged = self._safe(stage_dir / str(index), stage_dir)
                destination = survivor.parent / "assets" / f"preserved-{hashlib.sha256(entry['source'].encode()).hexdigest()[:16]}{Path(entry['source']).suffix}"
                self._safe(destination, survivor.parent)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.copy2(staged, destination)
                if sha256_file(staged) != sha256_file(destination):
                    raise TrashError("공유 자료 보존 확인에 실패했습니다.", 500)
                changed = False
                for asset in doc.get("resources", {}).get("assets", []):
                    if asset.get("asset_id") == entry["asset_id"] and asset.get("path") == entry["old_path"]:
                        asset["path"] = str(destination.relative_to(survivor.parent)).replace("\\", "/")
                        changed = True
                if entry.get("owner_asset"):
                    new_asset = copy.deepcopy(entry["owner_asset"])
                    new_asset["asset_id"] = f"preserved-{entry['asset_id']}"
                    new_asset["path"] = str(destination.relative_to(survivor.parent)).replace("\\", "/")
                    assets = doc.setdefault("resources", {}).setdefault("assets", [])
                    if not any(asset.get("asset_id") == new_asset["asset_id"] for asset in assets):
                        assets.append(new_asset)
                        changed = True
                if changed:
                    atomic_write_json(survivor, doc)
            journal["stage"] = "shared_updated"
            atomic_write_json(path, journal)
        if journal["stage"] == "shared_updated":
            for entry in plan["question_sets"]:
                set_path = self._safe(self.root / "question_sets" / f"{entry['set_id']}.json")
                doc = load_json_object(set_path)
                remaining = [ref for ref in doc["items"] if not (ref["project_id"] == pid and ref["item_id"] in doomed)]
                if len(remaining) != len(doc["items"]):
                    doc["items"] = remaining
                    doc["revision"] += 1
                    doc["updated_at"] = utc_now()
                    atomic_write_json(set_path, doc)
            journal["stage"] = "sets_updated"
            atomic_write_json(path, journal)
        if journal["stage"] == "sets_updated":
            for name, field in (("index.json", "records"), ("overrides.json", "entries")):
                index_path = self.root / "_internal" / "provenance" / name
                if index_path.is_file():
                    doc = load_json_object(self._safe(index_path))
                    entries = doc.get(field)
                    if not isinstance(entries, list):
                        raise TrashError("출처 색인이 손상되었습니다.", 500)
                    remaining = [entry for entry in entries if not (
                        entry.get("project_id") == pid and entry.get("item_id") in doomed)]
                    if len(remaining) != len(entries):
                        doc[field] = remaining
                        atomic_write_json(index_path, doc)
            journal["stage"] = "provenance_updated"
            atomic_write_json(path, journal)
        if journal["stage"] == "provenance_updated":
            if kind == "item":
                for question in (self.root / pid / "items").glob("*/question.json"):
                    if question.parent.name == iid:
                        continue
                    survivor = load_json_object(self._safe(question))
                    identity = survivor.get("identity")
                    if isinstance(identity, dict) and identity.get("parent_item_id") == iid:
                        identity["parent_item_id"] = None
                        atomic_write_json(question, survivor)
                project_path = self._safe(self.root / pid / "project.json")
                project = load_json_object(project_path)
                if iid in project["item_order"]:
                    project["item_order"] = [value for value in project["item_order"] if value != iid]
                    groups = []
                    for group in project.get("groups", []):
                        members = [value for value in group["member_item_ids"] if value != iid]
                        if members:
                            group["member_item_ids"] = members
                            if group.get("shared_item_id") == iid:
                                group["shared_item_id"] = members[0]
                            groups.append(group)
                    project["groups"] = groups
                    project["workflow"]["revision"] += 1
                    atomic_write_json(project_path, project)
            journal["stage"] = "owner_updated"
            atomic_write_json(path, journal)
        if journal["stage"] == "owner_updated":
            expected_files = {entry["path"]: entry["sha256"] for entry in plan["files"]}
            for existing in target.rglob("*") if target.exists() else []:
                if existing.is_symlink():
                    raise TrashError("삭제 폴더 안에 링크가 생겼습니다.", 422)
                if existing.is_file():
                    relative = str(existing.relative_to(self.root)).replace("\\", "/")
                    if expected_files.get(relative) != sha256_file(existing):
                        raise TrashError("삭제 자료가 변경되었습니다. 작업을 중단했습니다.")
            for file in plan["files"]:
                candidate = self._safe(self.root / file["path"], target)
                if candidate.is_file():
                    candidate.unlink()
            if target.exists():
                shutil.rmtree(target)
            journal["stage"] = "files_removed"
            atomic_write_json(path, journal)
        if journal["stage"] == "files_removed":
            if target.exists() or self._sets(pid, list(doomed)):
                raise TrashError("삭제 완료 확인에 실패했습니다.", 500)
            for entry in plan["shared"]:
                survivor = self._safe(self.root / entry["project_id"] / "items" / entry["item_id"] / "question.json")
                doc = load_json_object(survivor)
                expected_id = f"preserved-{entry['asset_id']}" if entry.get("owner_asset") else entry["asset_id"]
                asset = next((asset for asset in doc.get("resources", {}).get("assets", [])
                              if asset.get("asset_id") == expected_id), None)
                if not asset or not (survivor.parent / asset["path"]).is_file():
                    raise TrashError("공유 자료 확인에 실패했습니다.", 500)
            state = self._state()
            if kind == "project":
                state["projects"].pop(pid, None)
                state["items"].pop(pid, None)
            else:
                state["items"].get(pid, {}).pop(iid, None)
            state["revision"] += 1
            atomic_write_json(self.path, state)
            if stage_dir.exists():
                shutil.rmtree(stage_dir)
            journal.clear()
            journal.update({"stage": "done", "digest": plan["manifest_digest"], "operation_id": path.stem})
            atomic_write_json(path, journal)
