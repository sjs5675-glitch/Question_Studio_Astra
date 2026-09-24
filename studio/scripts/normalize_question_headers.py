#!/usr/bin/env python3
"""Safely remove audited source headers from stored question JSON packages."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.model import (  # noqa: E402
    ModelError,
    approval_digest,
    atomic_write_json,
    load_json_object,
    save_json_cas,
)
from app.transcription import normalize_canonical_question_headers  # noqa: E402
from scripts.validate_package import (  # noqa: E402
    DEFAULT_SCHEMA,
    Validator as PackageValidator,
    json_schema_errors,
)


DEFAULT_WORKSPACE = ROOT / "workspace"
BACKUP_ROOT_NAME = ".question-header-backups"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _revision(document: Mapping[str, Any]) -> int:
    workflow = document.get("workflow")
    value = workflow.get("revision") if isinstance(workflow, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ModelError("workflow.revision must be a positive integer")
    return value


def _question_blocks(document: Mapping[str, Any]) -> list[Any] | None:
    content = document.get("content")
    question = content.get("question") if isinstance(content, Mapping) else None
    blocks = question.get("blocks") if isinstance(question, Mapping) else None
    return blocks if isinstance(blocks, list) else None


def _question_has_content(blocks: list[Any] | None) -> bool:
    if not blocks:
        return False
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") != "paragraph":
            return True
        inlines = block.get("inlines")
        if not isinstance(inlines, list):
            continue
        for inline in inlines:
            if not isinstance(inline, Mapping):
                continue
            if inline.get("type") == "text" and str(inline.get("text") or "").strip():
                return True
            if inline.get("type") == "equation":
                return True
    return False


def _has_valid_content_ready_approval(document: Mapping[str, Any]) -> bool:
    workflow = document.get("workflow")
    review = document.get("review")
    if not isinstance(workflow, Mapping) or not isinstance(review, Mapping):
        return False
    revision = workflow.get("revision")
    return (
        workflow.get("status") == "content_ready"
        and review.get("approved_revision") == revision
        and review.get("approved_sha256") == approval_digest(document)
    )


def _validation_errors(path: Path, candidate: Mapping[str, Any]) -> list[str]:
    errors = json_schema_errors(candidate, DEFAULT_SCHEMA)
    errors.extend(PackageValidator(path, check_files=False).validate(candidate))
    return errors


def _validation_signature(error: str) -> str:
    """Ignore only the top-level question index shifted by header removal."""

    error = re.sub(
        r"content\.question\.blocks\[\d+\]",
        "content.question.blocks[*]",
        error,
        count=1,
    )
    return re.sub(
        r"schema\.content\.question\.blocks\.\d+",
        "schema.content.question.blocks.*",
        error,
        count=1,
    )


def _prepare_item(path: Path, workspace: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    raw = path.read_bytes()
    document = load_json_object(path)
    original_errors = _validation_errors(path, document)
    candidate, removals, before, after = normalize_canonical_question_headers(
        document
    )
    if not removals:
        return {"status": "unchanged"}, None

    revision = _revision(document)
    project_id = path.parents[2].name
    item_id = str(document.get("item_id") or path.parent.name)
    record: dict[str, Any] = {
        "status": "ready",
        "project_id": project_id,
        "item_id": item_id,
        "path": path.relative_to(workspace).as_posix(),
        "revision_before": revision,
        "revision_after": revision + 1,
        "sha256_before": _sha256_bytes(raw),
        "removals": removals,
        "before": before,
        "after": after,
    }
    blocks = _question_blocks(candidate)
    if not _question_has_content(blocks):
        record.update(
            status="rejected",
            reason="cleanup_would_leave_question_empty",
        )
        return record, None

    validation_candidate = copy.deepcopy(candidate)
    validation_candidate["workflow"]["revision"] = revision + 1
    if _has_valid_content_ready_approval(document):
        review = validation_candidate.get("review")
        if not isinstance(review, dict):
            record.update(status="rejected", reason="invalid_content_ready_review")
            return record, None
        review["approved_revision"] = revision + 1
        review["approved_sha256"] = approval_digest(validation_candidate)
        record["approval_preserved"] = True
    else:
        record["approval_preserved"] = False

    errors = _validation_errors(path, validation_candidate)
    original_signatures = {_validation_signature(error) for error in original_errors}
    new_errors = [
        error
        for error in errors
        if _validation_signature(error) not in original_signatures
    ]
    if new_errors:
        record.update(
            status="rejected",
            reason="package_validation_failed",
            validation_errors=new_errors,
        )
        return record, None
    if errors:
        record["preexisting_validation_errors"] = errors
    return record, {
        "path": path,
        "document": validation_candidate,
        "expected_revision": revision,
        "raw": raw,
        "record": record,
    }


def _question_set_consequences(
    workspace: Path, plans: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    changed = {
        (plan["record"]["project_id"], plan["record"]["item_id"]): plan["record"]
        for plan in plans
    }
    consequences: list[dict[str, Any]] = []
    sets_root = workspace / "question_sets"
    if not sets_root.is_dir():
        return consequences
    for path in sorted(sets_root.glob("set-*.json")):
        try:
            document = load_json_object(path)
        except ModelError:
            continue
        refs = []
        for ref in document.get("items", []):
            if not isinstance(ref, Mapping):
                continue
            key = (ref.get("project_id"), ref.get("item_id"))
            record = changed.get(key)
            if record is None:
                continue
            refs.append(
                {
                    "project_id": key[0],
                    "item_id": key[1],
                    "source_revision": ref.get("source_revision"),
                    "revision_before": record["revision_before"],
                    "revision_after": record["revision_after"],
                    "warning_code_after_apply": "source_revision_changed",
                }
            )
        if refs:
            consequences.append(
                {
                    "set_id": document.get("set_id") or path.stem,
                    "references": refs,
                }
            )
    return consequences


def _backup_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _create_backup(workspace: Path, plans: list[dict[str, Any]]) -> Path:
    backup = workspace / BACKUP_ROOT_NAME / _backup_stamp()
    files_root = backup / "files"
    files_root.mkdir(parents=True, exist_ok=False)
    entries = []
    for plan in plans:
        record = plan["record"]
        destination = files_root / Path(record["path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(plan["raw"])
        if _sha256_bytes(destination.read_bytes()) != record["sha256_before"]:
            raise ModelError(f"backup SHA-256 mismatch: {record['path']}")
        entries.append(
            {
                key: copy.deepcopy(record[key])
                for key in (
                    "project_id",
                    "item_id",
                    "path",
                    "revision_before",
                    "revision_after",
                    "sha256_before",
                    "removals",
                    "before",
                    "after",
                )
            }
        )
    atomic_write_json(
        backup / "manifest.json",
        {
            "manifest_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "workspace": str(workspace),
            "entries": entries,
        },
    )
    return backup


def run(workspace: Path | str, *, apply: bool = False) -> dict[str, Any]:
    from astra_runtime import inside

    workspace = inside(workspace, "workspace") if apply else Path(workspace).resolve()
    if not workspace.is_dir():
        raise ModelError(f"workspace directory does not exist: {workspace}")

    records: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []
    scanned = 0
    for path in sorted(workspace.glob("project-*/items/*/question.json")):
        scanned += 1
        try:
            record, plan = _prepare_item(path, workspace)
        except (OSError, ValueError, RuntimeError) as exc:
            records.append(
                {
                    "status": "rejected",
                    "path": path.relative_to(workspace).as_posix(),
                    "reason": "inspection_failed",
                    "error": str(exc),
                }
            )
            continue
        if record["status"] != "unchanged":
            records.append(record)
        if plan is not None:
            plans.append(plan)

    consequences = _question_set_consequences(workspace, plans)
    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "workspace": str(workspace),
        "scanned_items": scanned,
        "changed_items": len(records),
        "ready_items": len(plans),
        "rejected_items": sum(record.get("status") == "rejected" for record in records),
        "applied_items": 0,
        "backup_directory": None,
        "question_set_warning": (
            "Applying increments item revisions. Stored question-set references then "
            "show as updated and HWPX builds emit source_revision_changed until the "
            "set is saved again."
        ),
        "affected_question_sets": consequences,
        "items": records,
    }
    if not apply or not plans:
        return report

    backup = _create_backup(workspace, plans)
    report["backup_directory"] = str(backup)

    changed_during_backup = [
        plan["record"]["path"]
        for plan in plans
        if _sha256_bytes(plan["path"].read_bytes()) != plan["record"]["sha256_before"]
    ]
    if changed_during_backup:
        report["apply_aborted"] = True
        report["conflicts"] = changed_during_backup
        return report

    for plan in plans:
        try:
            saved = save_json_cas(
                plan["path"],
                plan["document"],
                plan["expected_revision"],
                preserve_approval=True,
            )
        except (OSError, ModelError) as exc:
            plan["record"].update(status="write_failed", error=str(exc))
            report["apply_aborted"] = True
            break
        plan["record"]["status"] = "applied"
        plan["record"]["revision_after"] = saved["workflow"]["revision"]
        report["applied_items"] += 1
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write validated changes after creating a complete timestamped backup",
    )
    args = parser.parse_args(argv)
    try:
        report = run(args.workspace, apply=args.apply)
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"fatal_error": str(exc)}, ensure_ascii=True))
        return 2
    # ASCII escaping keeps CLI JSON lossless on Windows CP949 consoles too.
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 2 if report.get("apply_aborted") else 0


if __name__ == "__main__":
    raise SystemExit(main())
