from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from astra_runtime import inside
from .model import atomic_write_json, load_json_object
from .question_bank import _normalised_question_text


VERSION = 1
SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


class ProvenanceError(ValueError):
    pass


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _private_root(workspace: Path) -> Path:
    root = workspace.resolve()
    candidate = (root / "_internal" / "provenance").resolve()
    if not _inside(candidate, root):
        raise ProvenanceError("private index path escapes workspace")
    return candidate


def _report_path(workspace: Path, path: Path) -> Path:
    target = path.resolve()
    allowed = (
        (_private_root(workspace) / "reports").resolve(),
        (Path(__file__).resolve().parents[2] / ".omo" / "evidence" / "search-ui-provenance-build").resolve(),
    )
    if not any(_inside(target, root) for root in allowed) or target.suffix.lower() != ".json":
        raise ProvenanceError("report must be a JSON file in a private evidence directory")
    if _inside(target, (Path(__file__).resolve().parents[1] / "web").resolve()):
        raise ProvenanceError("report cannot be stored in the web root")
    return target


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _code(document_sha: Any, pages: Any, number: Any, page_count: Any) -> tuple[str | None, list[int], list[str]]:
    reasons: list[str] = []
    if not isinstance(document_sha, str) or not SHA256.fullmatch(document_sha):
        reasons.append("missing_document_sha256")
    if not isinstance(number, str) or not number.strip():
        reasons.append("missing_printed_number")
    indexes: list[int] = []
    if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
        reasons.append("invalid_document_page_count")
    if not isinstance(pages, list) or not pages:
        reasons.append("missing_source_pages")
    else:
        for page in pages:
            index = page.get("pdf_page_index") if isinstance(page, Mapping) else None
            if (isinstance(index, bool) or not isinstance(index, int)
                    or index < 0 or not isinstance(page_count, int)
                    or isinstance(page_count, bool) or index >= page_count):
                reasons.append("invalid_pdf_page_index")
            else:
                indexes.append(index)
    indexes = sorted(set(indexes))
    if not indexes:
        reasons.append("missing_valid_pdf_page_index")
    if reasons:
        return None, indexes, sorted(set(reasons))
    canonical = {
        "version": VERSION,
        "document_sha256": document_sha.lower(),
        "pdf_page_indexes": indexes,
        "printed_number": unicodedata.normalize("NFC", number.strip()),
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "AS1-" + hashlib.sha256(raw).hexdigest(), indexes, []


def _overrides(workspace: Path) -> dict[tuple[str, str], Mapping[str, Any]]:
    path = _private_root(workspace) / "overrides.json"
    if path.is_symlink():
        raise ProvenanceError("override path must be a private file")
    if not path.is_file():
        return {}
    payload = load_json_object(path)
    entries = payload.get("entries")
    if payload.get("schema_version") != 1 or not isinstance(entries, list):
        raise ProvenanceError("invalid overrides header")
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ProvenanceError("invalid override entry")
        key = (entry.get("project_id"), entry.get("item_id"))
        if not all(isinstance(value, str) and value for value in key) or key in result:
            raise ProvenanceError("duplicate or invalid override reference")
        pages = entry.get("pages")
        if (not isinstance(pages, list)
                or any(not isinstance(page, Mapping) or type(page.get("pdf_page_index")) is not int for page in pages)
                or len({page["pdf_page_index"] for page in pages}) != len(pages)):
            raise ProvenanceError("duplicate or invalid override pages")
        if entry.get("confirmed_conflict", False) and not str(entry.get("evidence_note") or "").strip():
            raise ProvenanceError("confirmed conflict requires evidence_note")
        reviewed_at = entry.get("reviewed_at")
        try:
            reviewed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00")) if isinstance(reviewed_at, str) and reviewed_at.endswith("Z") else None
        except ValueError:
            reviewed = None
        if reviewed is None or reviewed.tzinfo != timezone.utc:
            raise ProvenanceError("override requires UTC reviewed_at")
        result[key] = entry
    return result


def _record(project_id: str, item_path: Path, project: Mapping[str, Any], override: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = item_path.read_bytes()
    package = json.loads(raw.decode("utf-8"))
    if not isinstance(package, dict):
        raise ProvenanceError("question JSON root must be an object")
    item_id = item_path.parent.name
    if package.get("item_id") != item_id:
        raise ProvenanceError("stored item identity mismatch")
    provenance = package.get("provenance") or {}
    problem = provenance.get("problem") or {}
    identity = package.get("identity") or {}
    code, indexes, reasons = _code(
        problem.get("document_sha256"), problem.get("source_pages"),
        identity.get("printed_number"), problem.get("document_page_count"),
    )
    source_sha = hashlib.sha256(raw).hexdigest()
    labels: dict[int, str] = {}
    confirmed = False
    if override:
        if override.get("source_file_sha256") != source_sha:
            reasons.append("override_stale")
        else:
            item_root = item_path.parent.resolve()
            assets = {
                asset.get("asset_id") for asset in (package.get("resources") or {}).get("assets", [])
                if isinstance(asset, Mapping)
                and isinstance(asset.get("asset_id"), str)
                and asset.get("role") == "source_crop"
                and asset.get("section") == "question"
                and asset.get("state") == "ready"
                and isinstance(asset.get("path"), str)
                and _inside((item_root / asset["path"]).resolve(), item_root)
                and (item_root / asset["path"]).is_file()
            }
            for page in override["pages"]:
                if (not isinstance(page, Mapping) or isinstance(page.get("pdf_page_index"), bool)
                        or page.get("pdf_page_index") not in indexes
                        or not isinstance(page.get("printed_page_label"), str)
                        or not page["printed_page_label"].strip()
                        or page.get("evidence_asset_id") not in assets):
                    raise ProvenanceError("override page or source asset is invalid")
                labels[page["pdf_page_index"]] = page["printed_page_label"]
            confirmed = override.get("confirmed_conflict") is True
    metadata = project.get("metadata") or {}
    text = _normalised_question_text((package.get("content") or {}).get("question"))
    return {
        "project_id": project_id,
        "item_id": item_id,
        "provenance_code": code,
        "source_file_sha256": source_sha,
        "source_revision": (package.get("workflow") or {}).get("revision"),
        "rule_version": VERSION,
        "document_sha256": problem.get("document_sha256"),
        "printed_number": identity.get("printed_number"),
        "pdf_page_indexes": indexes,
        "printed_page_labels": [labels.get(index) for index in indexes],
        "source_name": metadata.get("book_title") or metadata.get("filename_base"),
        "content_fingerprint_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
        "status": "needs_review" if reasons else "complete",
        "reasons": sorted(set(reasons)),
        "confirmed_conflict": confirmed,
    }


def build_index(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    overrides = _overrides(workspace)
    records: list[dict[str, Any]] = []
    for project_path in sorted(workspace.glob("project-*/project.json")):
        if not _inside(project_path.resolve(), workspace):
            continue
        project = load_json_object(project_path)
        project_id = project_path.parent.name
        if project.get("project_id") != project_id:
            raise ProvenanceError("stored project identity mismatch")
        for item_path in sorted(project_path.parent.glob("items/*/question.json")):
            if _inside(item_path.resolve(), project_path.parent.resolve()):
                records.append(_record(project_id, item_path, project, overrides.get((project_id, item_path.parent.name))))
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record["provenance_code"]:
            groups.setdefault(record["provenance_code"], []).append(record)
    for group in groups.values():
        fingerprints = {record["content_fingerprint_sha256"] for record in group if record["content_fingerprint_sha256"]}
        if len(group) > 1 and any(record["content_fingerprint_sha256"] is None for record in group):
            for record in group:
                if record["content_fingerprint_sha256"] is None:
                    record["reasons"].append("duplicate_content_unchecked")
                    record["status"] = "needs_review"
        if len(fingerprints) > 1:
            for record in group:
                record["reasons"].append("duplicate_content_mismatch")
                record["status"] = "needs_review"
        for record in group:
            if record.pop("confirmed_conflict"):
                record["status"] = "conflict"
    for record in records:
        record.pop("confirmed_conflict", None)
        record["reasons"] = sorted(set(record["reasons"]))
    return {"schema_version": VERSION, "records": records}


@contextmanager
def _locked(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".apply.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ProvenanceError("index apply already in progress") from exc
    try:
        os.close(descriptor)
        yield
    finally:
        path.unlink(missing_ok=True)


def _report(index: Mapping[str, Any], mode: str) -> dict[str, Any]:
    records = index["records"]
    return {
        "schema_version": VERSION,
        "mode": mode,
        "scanned": len(records),
        **{status: sum(record["status"] == status for record in records) for status in ("complete", "needs_review", "conflict")},
        "stale": sum("override_stale" in record["reasons"] for record in records),
        "issues": [
            {"project_id": record["project_id"], "item_id": record["item_id"], "reasons": record["reasons"]}
            for record in records if record["reasons"]
        ],
    }


def run(workspace: Path, mode: str, report: Path) -> dict[str, Any]:
    workspace = inside(workspace, "workspace_root")
    if not workspace.is_dir():
        raise ProvenanceError("workspace does not exist")
    report = _report_path(workspace, report)
    if mode == "apply":
        with _locked(_private_root(workspace)):
            index_path = _private_root(workspace) / "index.json"
            if index_path.is_symlink():
                raise ProvenanceError("index path must be a private file")
            index = build_index(workspace)
            for record in index["records"]:
                path = workspace / record["project_id"] / "items" / record["item_id"] / "question.json"
                if _digest(path) != record["source_file_sha256"]:
                    raise ProvenanceError("source changed during index build")
            atomic_write_json(index_path, index)
    elif mode == "dry-run":
        index = build_index(workspace)
    else:
        raise ProvenanceError("mode must be dry-run or apply")
    result = _report(index, mode)
    atomic_write_json(report, result)
    return result


def lookup(workspace: Path, project_id: str, item_id: str) -> dict[str, Any] | None:
    path = _private_root(workspace) / "index.json"
    if not path.is_file():
        return None
    index = load_json_object(path)
    for record in index.get("records", []):
        if record.get("project_id") == project_id and record.get("item_id") == item_id:
            source = workspace / project_id / "items" / item_id / "question.json"
            return record if source.is_file() and _digest(source) == record.get("source_file_sha256") else None
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the private question source index")
    parser.add_argument("--workspace", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.workspace, "apply" if args.apply else "dry-run", args.report), ensure_ascii=False))


if __name__ == "__main__":
    main()
