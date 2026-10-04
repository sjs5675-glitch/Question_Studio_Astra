"""Read-only global question bank and ordered, file-backed question sets."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import unicodedata
import uuid
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Mapping, Sequence

from .trash import Trash
from .model import (
    ModelError,
    ModelNotFound,
    RevisionConflict,
    atomic_write_json,
    figure_generation_summary,
    load_json_object,
    registration_summary,
    solution_link_summary,
    utc_now,
    validate_id,
)


SCHEMA_VERSION = "0.1.0"
OUTPUT_MODES = frozenset({"separate", "combined_end_section"})
QUESTION_SET_SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "question-set.schema.json"
_FILTERS = frozenset(
    {
        "project_id",
        "subject",
        "unit",
        "difficulty",
        "problem_type",
        "has_figure",
        "has_graph",
    }
)
_SUBQUESTION_NUMBER = re.compile(
    r"^\s*(?P<base>.+?)\s*\(\s*(?P<sub>[0-9A-Za-zㄱ-ㆎ가-힣]+)\s*\)\s*$"
)
_SIMILAR_DUPLICATE_THRESHOLD = 0.94


class QuestionBankError(ModelError):
    """Raised when a bank query or question-set mutation is invalid."""


class QuestionSetNotFound(ModelNotFound):
    """Raised when a requested question set or referenced item is absent."""


class QuestionSetArchiveConflict(QuestionBankError):
    pass


# ponytail: one process-local lock per set fits the single desktop server; add an
# OS lock only if multiple server processes may edit the same workspace.
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_BANK_CACHE_LOCK = threading.Lock()
_BANK_CACHE: tuple[Path, tuple[tuple[str, int, int, int], ...], list[dict[str, Any]]] | None = None


def _lock_for(path: Path) -> threading.Lock:
    path = path.resolve()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.Lock())


def _workspace(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sets_root(workspace_root: Path | str, *, create: bool = False) -> Path:
    workspace = _workspace(workspace_root)
    candidate = workspace / "question_sets"
    if candidate.exists() and not _is_within(candidate.resolve(), workspace):
        raise QuestionBankError("question_sets must stay inside the workspace")
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    resolved = candidate.resolve()
    if not _is_within(resolved, workspace):
        raise QuestionBankError("question_sets must stay inside the workspace")
    return resolved


def _set_path(workspace_root: Path | str, set_id: str, *, create_root: bool = False) -> Path:
    try:
        validate_id(set_id, field="set_id")
    except ModelError as exc:
        raise QuestionBankError(str(exc)) from exc
    if not set_id.startswith("set-"):
        raise QuestionBankError("set_id must start with 'set-'")
    root = _sets_root(workspace_root, create=create_root)
    path = (root / f"{set_id}.json").resolve()
    if not _is_within(path, root):
        raise QuestionBankError("unsafe question-set path")
    return path


def _positive_revision(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise QuestionBankError(f"{field} must be a positive integer")
    return value


def _title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
        raise QuestionBankError("title must be between 1 and 200 characters")
    return value.strip()


def _output_mode(value: Any) -> str:
    if value not in OUTPUT_MODES:
        raise QuestionBankError("output_mode must be separate or combined_end_section")
    return str(value)


def _safe_relative_path(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 512:
        raise QuestionBankError(f"{field} must be a safe relative path or null")
    if value.startswith("/") or "\\" in value or ":" in value:
        raise QuestionBankError(f"{field} must be a safe relative path or null")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise QuestionBankError(f"{field} must be a safe relative path or null")
    return value


def _rendering(value: Any, current: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise QuestionBankError("rendering must be an object")
    unexpected = set(value) - {"template_ref", "design_profile_ref"}
    if unexpected:
        raise QuestionBankError(f"unknown rendering fields: {', '.join(sorted(unexpected))}")
    merged = {
        "template_ref": None,
        "design_profile_ref": None,
        **(dict(current) if isinstance(current, Mapping) else {}),
        **dict(value),
    }
    return {
        key: _safe_relative_path(merged[key], field=f"rendering.{key}")
        for key in ("template_ref", "design_profile_ref")
    }


def _workflow_revision(package: Mapping[str, Any]) -> int:
    workflow = package.get("workflow")
    value = workflow.get("revision") if isinstance(workflow, Mapping) else None
    return _positive_revision(value, field="workflow.revision")


def _project_and_item(
    workspace_root: Path | str, project_id: str, item_id: str
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    workspace = _workspace(workspace_root)
    validate_id(project_id, field="project_id")
    validate_id(item_id, field="item_id")
    project_dir = (workspace / project_id).resolve()
    if not project_id.startswith("project-") or not _is_within(project_dir, workspace):
        raise QuestionSetNotFound(f"project not found: {project_id}")
    project_path = project_dir / "project.json"
    item_path = (project_dir / "items" / item_id / "question.json").resolve()
    if not project_path.is_file() or not _is_within(item_path, project_dir) or not item_path.is_file():
        raise QuestionSetNotFound(f"item not found: {project_id}/{item_id}")
    project = load_json_object(project_path)
    package = load_json_object(item_path)
    if project.get("project_id") != project_id or package.get("item_id") != item_id:
        raise QuestionBankError(f"stored identity mismatch: {project_id}/{item_id}")
    _workflow_revision(package)
    return project, package, item_path


def _walk(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _preview(section: Any, *, limit: int | None = 420) -> str:
    parts: list[str] = []
    for node in _walk(section):
        if node.get("type") == "paragraph":
            text = "".join(
                "\n"
                if inline.get("type") == "line_break"
                else str(
                    inline.get("text")
                    or inline.get("source_text")
                    or inline.get("latex")
                    or inline.get("hwp_script")
                    or inline.get("plain_text")
                    or ""
                )
                for inline in (node.get("inlines") or [])
                if isinstance(inline, Mapping)
            ).strip()
            if text:
                parts.append(text)
        elif (
            node.get("type") == "figure"
            and isinstance(node.get("alt_text"), str)
            and node["alt_text"].strip()
        ):
            parts.append("[그림] " + node["alt_text"].strip())
    result = "\n".join(parts)
    return result if limit is None else result[:limit]


def _normalised_question_text(section: Any) -> str:
    """Return stable OCR content for fingerprints and conservative duplicate hints."""

    return "\n".join(
        " ".join(unicodedata.normalize("NFKC", part).casefold().split())
        for part in _preview(section, limit=None).splitlines()
        if part.strip()
    )


def _dependency_seed(
    project_id: str,
    project: Mapping[str, Any],
    item_id: str,
    package: Mapping[str, Any],
) -> dict[str, Any]:
    identity = package.get("identity") if isinstance(package.get("identity"), Mapping) else {}
    printed = str(identity.get("printed_number") or "").strip()
    unit_kind = identity.get("unit_kind")
    group_id = identity.get("group_id")
    parent_id = identity.get("parent_item_id")
    group_label: str | None = None
    member_index: int | None = None

    if isinstance(group_id, str) and group_id:
        group_key = f"{project_id}:group:{group_id}"
        for group in project.get("groups", []) if isinstance(project.get("groups"), list) else []:
            if isinstance(group, Mapping) and group.get("group_id") == group_id:
                group_label = group.get("label") if isinstance(group.get("label"), str) else None
                members = group.get("member_item_ids")
                if isinstance(members, list) and item_id in members:
                    member_index = members.index(item_id) + 1
                break
    elif isinstance(parent_id, str) and parent_id:
        group_key = f"{project_id}:parent:{parent_id}"
    else:
        match = _SUBQUESTION_NUMBER.fullmatch(printed)
        if match and (unit_kind == "subquestion" or match.group("sub")):
            group_label = " ".join(match.group("base").split())
            provenance = package.get("provenance")
            problem = provenance.get("problem") if isinstance(provenance, Mapping) else None
            owner = problem.get("owner_question_id") if isinstance(problem, Mapping) else None
            group_key = (
                f"{project_id}:owner:{owner}"
                if isinstance(owner, str) and owner
                else f"{project_id}:printed:{group_label.casefold()}"
            )
        else:
            return {
                "group_key": None,
                "group_label": None,
                "sub_index": None,
                "group_size": 1,
                "shared_context": False,
            }

    match = _SUBQUESTION_NUMBER.fullmatch(printed)
    sub_index: int | str | None = member_index
    if match:
        raw_sub = match.group("sub")
        sub_index = int(raw_sub) if raw_sub.isdecimal() else raw_sub
        group_label = group_label or " ".join(match.group("base").split())
    return {
        "group_key": group_key,
        "group_label": group_label or printed or group_key.rsplit(":", 1)[-1],
        "sub_index": sub_index,
        "group_size": 1,
        "shared_context": unit_kind == "subquestion" or bool(group_id or parent_id),
    }


def _source_summary(project: Mapping[str, Any], package: Mapping[str, Any]) -> dict[str, Any]:
    metadata = project.get("metadata") if isinstance(project.get("metadata"), Mapping) else {}
    provenance = package.get("provenance")
    problem = provenance.get("problem") if isinstance(provenance, Mapping) else {}
    pages = problem.get("source_pages") if isinstance(problem, Mapping) else []
    return {
        "project_title": (
            metadata.get("book_title")
            or metadata.get("filename_base")
        ),
        "original_filename": problem.get("original_filename") if isinstance(problem, Mapping) else None,
        "pdf_page_indexes": [
            page.get("pdf_page_index")
            for page in (pages if isinstance(pages, list) else [])
            if isinstance(page, Mapping) and isinstance(page.get("pdf_page_index"), int)
        ],
    }


def _bank_summary(
    project_id: str, project: Mapping[str, Any], item_id: str, package: Mapping[str, Any],
    *, item_root: Path | None = None, lightweight: bool = False,
) -> dict[str, Any]:
    workflow = package.get("workflow") if isinstance(package.get("workflow"), Mapping) else {}
    content = package.get("content") if isinstance(package.get("content"), Mapping) else {}
    review = package.get("review") if isinstance(package.get("review"), Mapping) else {}
    resources = package.get("resources") if isinstance(package.get("resources"), Mapping) else {}
    assets = resources.get("assets") if isinstance(resources.get("assets"), list) else []
    question = content.get("question")
    registration = registration_summary(package)
    revision = _workflow_revision(package)
    identity = package.get("identity") if isinstance(package.get("identity"), Mapping) else {}
    duplicate_text = _normalised_question_text(question)
    return {
        "project_id": project_id,
        "item_id": item_id,
        "ref": {"project_id": project_id, "item_id": item_id},
        "revision": revision,
        "updated_at": workflow.get("updated_at") if isinstance(workflow.get("updated_at"), str) else None,
        "identity": copy.deepcopy(dict(identity)),
        "dependency": _dependency_seed(project_id, project, item_id, package),
        "content_fingerprint_sha256": hashlib.sha256(duplicate_text.encode("utf-8")).hexdigest(),
        "duplicate_warning": None,
        "duplicate_similarity_status": "not_checked" if lightweight else "checked",
        "_duplicate_text": duplicate_text,
        "tags": copy.deepcopy(package.get("tags")),
        "figure_generation": figure_generation_summary(package),
        "source": _source_summary(project, package),
        "preview": {
            "printed_number": identity.get("printed_number"),
            "text": _preview(question),
            "images": [
                {
                    "asset_id": asset["asset_id"],
                    "width_px": asset.get("width_px"),
                    "height_px": asset.get("height_px"),
                }
                for asset in assets
                if isinstance(asset, Mapping)
                and isinstance(asset.get("asset_id"), str)
                and asset.get("role") == "source_crop"
                and asset.get("section") == "question"
                and asset.get("state") == "ready"
            ],
        },
        "status": {
            "workflow": workflow.get("status", "unknown"),
            "registration": registration["status"],
            "usable": registration["usable"],
            "answer_saved": content.get("answer") is not None,
            "explanation_saved": content.get("explanation") is not None,
            "review_issue_count": len(review.get("issues", []))
            if isinstance(review.get("issues"), list)
            else 0,
            "solution_link": "not_checked" if lightweight else solution_link_summary(package, item_root=item_root)["status"],
        },
    }


def _postprocess_bank_summaries(summaries: list[dict[str, Any]], *, lightweight: bool = False) -> None:
    group_sizes: dict[str, int] = {}
    for summary in summaries:
        group_key = summary["dependency"]["group_key"]
        if group_key:
            group_sizes[group_key] = group_sizes.get(group_key, 0) + 1
    for summary in summaries:
        dependency = summary["dependency"]
        if dependency["group_key"]:
            dependency["group_size"] = group_sizes[dependency["group_key"]]
            dependency["shared_context"] = True

    matches: list[list[dict[str, Any]]] = [[] for _ in summaries]
    if lightweight:
        by_fingerprint: dict[str, list[int]] = {}
        for index, summary in enumerate(summaries):
            if summary["_duplicate_text"]:
                by_fingerprint.setdefault(summary["content_fingerprint_sha256"], []).append(index)
        for indexes in by_fingerprint.values():
            for offset, left_index in enumerate(indexes):
                for right_index in indexes[offset + 1:]:
                    left, right = summaries[left_index], summaries[right_index]
                    if left["dependency"]["group_key"] and left["dependency"]["group_key"] == right["dependency"]["group_key"]:
                        continue
                    matches[left_index].append({"ref": copy.deepcopy(right["ref"]), "kind": "exact", "similarity": 1.0})
                    matches[right_index].append({"ref": copy.deepcopy(left["ref"]), "kind": "exact", "similarity": 1.0})
    else:
        for left_index, left in enumerate(summaries):
            left_text = left["_duplicate_text"]
            if not left_text:
                continue
            for right_index in range(left_index + 1, len(summaries)):
                right = summaries[right_index]
                if (
                    left["dependency"]["group_key"]
                    and left["dependency"]["group_key"] == right["dependency"]["group_key"]
                ):
                    continue
                right_text = right["_duplicate_text"]
                if not right_text:
                    continue
                if left["content_fingerprint_sha256"] == right["content_fingerprint_sha256"]:
                    kind, similarity = "exact", 1.0
                elif min(len(left_text), len(right_text)) >= 40:
                    matcher = SequenceMatcher(None, left_text, right_text)
                    if (
                        matcher.real_quick_ratio() < _SIMILAR_DUPLICATE_THRESHOLD
                        or matcher.quick_ratio() < _SIMILAR_DUPLICATE_THRESHOLD
                    ):
                        continue
                    similarity = round(matcher.ratio(), 3)
                    if similarity < _SIMILAR_DUPLICATE_THRESHOLD:
                        continue
                    kind = "similar"
                else:
                    continue
                matches[left_index].append({"ref": copy.deepcopy(right["ref"]), "kind": kind, "similarity": similarity})
                matches[right_index].append({"ref": copy.deepcopy(left["ref"]), "kind": kind, "similarity": similarity})
    for summary, item_matches in zip(summaries, matches):
        summary.pop("_duplicate_text", None)
        if item_matches:
            item_matches.sort(key=lambda item: (item["ref"]["project_id"], item["ref"]["item_id"]))
            summary["duplicate_warning"] = {
                "kind": "exact" if any(item["kind"] == "exact" for item in item_matches) else "similar",
                "matches": item_matches,
            }


def _bank_signature(workspace: Path) -> tuple[tuple[str, int, int, int], ...] | None:
    try:
        paths = []
        for project in sorted(workspace.glob("project-*/project.json")):
            paths.append(project)
            paths.extend(sorted(project.parent.glob("items/*/question.json")))
        result = []
        trash_path = workspace / "_internal" / "trash.json"
        if trash_path.is_file():
            paths.append(trash_path)
        for path in paths:
            state = path.stat()
            result.append((str(path.relative_to(workspace)), state.st_ino, state.st_mtime_ns, state.st_size))
        return tuple(result)
    except OSError:
        return None


def list_bank_items(workspace_root: Path | str, *, lightweight: bool = False, include_deleted: bool = False) -> list[dict[str, Any]]:
    """Scan every safe ``project-*/items/*/question.json`` without copying it."""

    global _BANK_CACHE
    workspace = _workspace(workspace_root)
    signature = _bank_signature(workspace) if lightweight and not include_deleted else None
    if signature is not None:
        with _BANK_CACHE_LOCK:
            if _BANK_CACHE is not None and _BANK_CACHE[0] == workspace and _BANK_CACHE[1] == signature:
                return copy.deepcopy(_BANK_CACHE[2])
    summaries: list[dict[str, Any]] = []
    trash = Trash(workspace)
    for project_path in sorted(workspace.glob("project-*/project.json")):
        project_path = project_path.resolve()
        if not _is_within(project_path, workspace):
            continue
        try:
            project = load_json_object(project_path)
            project_id = validate_id(project.get("project_id"), field="project_id")
        except (ModelError, OSError):
            continue
        if not include_deleted and trash.is_deleted(project_id):
            continue
        project_dir = project_path.parent
        if project_id != project_dir.name:
            continue
        items_root = project_dir / "items"
        if not items_root.is_dir():
            continue
        paths: dict[str, Path] = {}
        for item_path in items_root.glob("*/question.json"):
            resolved = item_path.resolve()
            if not _is_within(resolved, items_root.resolve()):
                continue
            item_id = item_path.parent.name
            try:
                validate_id(item_id, field="item_id")
            except ModelError:
                continue
            paths[item_id] = resolved
        configured = project.get("item_order") if isinstance(project.get("item_order"), list) else []
        ordered: list[str] = []
        for item_id in configured:
            if isinstance(item_id, str) and item_id in paths and item_id not in ordered:
                ordered.append(item_id)
        ordered.extend(sorted(set(paths) - set(ordered)))
        for item_id in ordered:
            if not include_deleted and trash.is_deleted(project_id, item_id):
                continue
            try:
                package = load_json_object(paths[item_id])
                if package.get("item_id") != item_id:
                    continue
                summaries.append(_bank_summary(project_id, project, item_id, package, item_root=paths[item_id].parent, lightweight=lightweight))
            except (ModelError, OSError):
                continue
    _postprocess_bank_summaries(summaries, lightweight=lightweight)
    if signature is not None and signature == _bank_signature(workspace):
        with _BANK_CACHE_LOCK:
            _BANK_CACHE = (workspace, signature, copy.deepcopy(summaries))
    return summaries


def search_bank_items(
    workspace_root: Path | str,
    *,
    query: str = "",
    filters: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return bank summaries matching text and the small, current tag taxonomy."""

    if not isinstance(query, str):
        raise QuestionBankError("query must be a string")
    if filters is not None and not isinstance(filters, Mapping):
        raise QuestionBankError("filters must be an object")
    filters = dict(filters or {})
    unexpected = set(filters) - _FILTERS
    if unexpected:
        raise QuestionBankError(f"unknown filters: {', '.join(sorted(unexpected))}")
    for boolean_filter in ("has_figure", "has_graph"):
        if boolean_filter in filters and not isinstance(filters[boolean_filter], bool):
            raise QuestionBankError(f"{boolean_filter} must be a boolean")
    terms = query.casefold().split()
    matches: list[dict[str, Any]] = []
    for summary in list_bank_items(workspace_root, lightweight=True):
        tags = summary.get("tags") if isinstance(summary.get("tags"), Mapping) else {}
        features = tags.get("features") if isinstance(tags.get("features"), Mapping) else {}
        searchable = json.dumps(
            {
                "preview": summary["preview"],
                "tags": tags,
                "source": summary["source"],
                "identity": summary["identity"],
            },
            ensure_ascii=False,
        ).casefold()
        if any(term not in searchable for term in terms):
            continue
        if "project_id" in filters and summary["project_id"] != filters["project_id"]:
            continue
        if any(tags.get(key) != filters[key] for key in ("subject", "unit", "difficulty") if key in filters):
            continue
        if "problem_type" in filters and filters["problem_type"] not in (tags.get("problem_types") or []):
            continue
        if any(features.get(key) is not filters[key] for key in ("has_figure", "has_graph") if key in filters):
            continue
        matches.append(summary)
    return matches


def _validate_item_refs(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        raise QuestionBankError("items must be an array")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise QuestionBankError(f"items[{index}] must be an object")
        unexpected = set(item) - {"project_id", "item_id", "source_revision"}
        if unexpected:
            raise QuestionBankError(f"items[{index}] contains unknown fields")
        project_id = validate_id(item.get("project_id"), field=f"items[{index}].project_id")
        item_id = validate_id(item.get("item_id"), field=f"items[{index}].item_id")
        source_revision = _positive_revision(
            item.get("source_revision"), field=f"items[{index}].source_revision"
        )
        ref = (project_id, item_id)
        if ref in seen:
            raise QuestionBankError(f"duplicate item reference: {project_id}/{item_id}")
        seen.add(ref)
        result.append(
            {
                "project_id": project_id,
                "item_id": item_id,
                "source_revision": source_revision,
            }
        )
    return result


def resolve_bank_item_refs(
    workspace_root: Path | str, items: Any
) -> list[dict[str, Any]]:
    """Resolve explicit cross-project refs and bind each to its current revision."""

    resolved: list[dict[str, Any]] = []
    for ref in _validate_item_refs(items):
        project, package, item_path = _project_and_item(
            workspace_root, ref["project_id"], ref["item_id"]
        )
        current_revision = _workflow_revision(package)
        if current_revision != ref["source_revision"]:
            raise QuestionBankError(
                "source revision changed: "
                f"{ref['project_id']}/{ref['item_id']}"
            )
        resolved.append(
            {
                **ref,
                "project": project,
                "package": package,
                "item_path": item_path,
            }
        )
    return resolved


def _validate_question_set(document: Mapping[str, Any], *, path: Path | None = None) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise QuestionBankError("question set must be an object")
    allowed = {
        "$schema",
        "schema_version",
        "set_id",
        "title",
        "revision",
        "output_mode",
        "items",
        "rendering",
        "created_at",
        "updated_at",
    }
    required = allowed - {"$schema", "rendering"}
    if set(document) - allowed or not required.issubset(document):
        raise QuestionBankError("question set fields do not match the storage contract")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise QuestionBankError(f"unsupported question-set schema_version: {document.get('schema_version')}")
    set_id = validate_id(document.get("set_id"), field="set_id")
    if not set_id.startswith("set-") or (path is not None and path.stem != set_id):
        raise QuestionBankError("question-set identity does not match its path")
    if not isinstance(document.get("created_at"), str) or not isinstance(document.get("updated_at"), str):
        raise QuestionBankError("question-set timestamps must be strings")
    candidate = copy.deepcopy(dict(document))
    candidate["title"] = _title(document.get("title"))
    candidate["revision"] = _positive_revision(document.get("revision"), field="revision")
    candidate["output_mode"] = _output_mode(document.get("output_mode"))
    candidate["items"] = _validate_item_refs(document.get("items"))
    if "rendering" in document:
        candidate["rendering"] = _rendering(document.get("rendering"))
    return candidate


def create_question_set(
    workspace_root: Path | str,
    *,
    title: str = "새 문제지",
    output_mode: str = "separate",
    set_id: str | None = None,
    rendering: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an empty set. Items are added later through the CAS patch."""

    title = _title(title)
    output_mode = _output_mode(output_mode)
    rendering_value = _rendering(rendering or {})
    for _ in range(10):
        candidate_id = set_id or f"set-{uuid.uuid4()}"
        path = _set_path(workspace_root, candidate_id, create_root=True)
        with _lock_for(path):
            if path.exists():
                if set_id is not None:
                    raise QuestionBankError(f"question set already exists: {candidate_id}")
                continue
            timestamp = utc_now()
            document = {
                "$schema": "../../schemas/question-set.schema.json",
                "schema_version": SCHEMA_VERSION,
                "set_id": candidate_id,
                "title": title,
                "revision": 1,
                "output_mode": output_mode,
                "items": [],
                "rendering": rendering_value,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            document = _validate_question_set(document, path=path)
            atomic_write_json(path, document)
            return document
    raise QuestionBankError("could not allocate a unique question-set id")


def _load_question_set(workspace_root: Path | str, set_id: str) -> tuple[Path, dict[str, Any]]:
    path = _set_path(workspace_root, set_id)
    if not path.is_file():
        raise QuestionSetNotFound(f"question set not found: {set_id}")
    return path, _validate_question_set(load_json_object(path), path=path)


def list_question_sets(workspace_root: Path | str) -> list[dict[str, Any]]:
    root = _sets_root(workspace_root)
    if not root.is_dir():
        return []
    summaries: list[dict[str, Any]] = []
    for path in sorted(root.glob("set-*.json")):
        if not _is_within(path.resolve(), root):
            continue
        try:
            document = _validate_question_set(load_json_object(path), path=path)
        except (ModelError, OSError):
            continue
        summaries.append(
            {
                key: copy.deepcopy(document[key])
                for key in (
                    "set_id",
                    "title",
                    "revision",
                    "output_mode",
                    "rendering",
                    "created_at",
                    "updated_at",
                )
                if key in document
            }
            | {"item_count": len(document["items"])}
        )
    return sorted(summaries, key=lambda value: value["updated_at"], reverse=True)


def resolve_question_set(
    workspace_root: Path | str, document: Mapping[str, Any]
) -> dict[str, Any]:
    """Add current/missing revision state without changing persisted references."""

    stored = _validate_question_set(document)
    bank = {
        (summary["project_id"], summary["item_id"]): summary
        for summary in list_bank_items(workspace_root, lightweight=True, include_deleted=True)
    }
    resolved_items: list[dict[str, Any]] = []
    for position, item in enumerate(stored["items"], start=1):
        summary = bank.get((item["project_id"], item["item_id"]))
        if summary is not None:
            current_revision = summary["revision"]
            revision_status = (
                "current" if current_revision == item["source_revision"] else "updated"
            )
        else:
            current_revision = None
            revision_status = "missing"
        resolved_items.append(
            {
                **copy.deepcopy(item),
                "position": position,
                "revision_status": revision_status,
                "current_revision": current_revision,
                "summary": summary,
            }
        )
    return {**stored, "resolved_items": resolved_items}


def get_question_set(
    workspace_root: Path | str, set_id: str, *, resolve_items: bool = True
) -> dict[str, Any]:
    _, document = _load_question_set(workspace_root, set_id)
    return resolve_question_set(workspace_root, document) if resolve_items else document


def _normalise_patch_items(
    workspace_root: Path | str,
    current: Sequence[Mapping[str, Any]],
    requested: Any,
) -> list[dict[str, Any]]:
    if not isinstance(requested, Sequence) or isinstance(requested, (str, bytes, bytearray)):
        raise QuestionBankError("items must be an array")
    existing = {
        (item["project_id"], item["item_id"]): item["source_revision"] for item in current
    }
    normalised: list[tuple[dict[str, Any], dict[str, Any] | None, int]] = []
    seen: set[tuple[str, str]] = set()
    for index, value in enumerate(requested):
        if not isinstance(value, Mapping):
            raise QuestionBankError(f"items[{index}] must be an object")
        unexpected = set(value) - {"project_id", "item_id", "source_revision"}
        if unexpected:
            raise QuestionBankError(f"items[{index}] contains unknown fields")
        project_id = validate_id(value.get("project_id"), field=f"items[{index}].project_id")
        item_id = validate_id(value.get("item_id"), field=f"items[{index}].item_id")
        ref = (project_id, item_id)
        if ref in seen:
            raise QuestionBankError(f"duplicate item reference: {project_id}/{item_id}")
        seen.add(ref)
        source_revision = existing.get(ref)
        if source_revision is None and Trash(workspace_root).is_deleted(project_id, item_id):
            raise QuestionBankError("휴지통 문항을 새로 추가할 수 없습니다.")
        project: dict[str, Any] | None = None
        package: dict[str, Any] | None = None
        try:
            project, package, _ = _project_and_item(workspace_root, project_id, item_id)
        except (ModelError, OSError):
            if source_revision is None:
                raise
        if source_revision is None and package is not None:
            source_revision = _workflow_revision(package)
        item = {
            "project_id": project_id,
            "item_id": item_id,
            "source_revision": source_revision,
        }
        dependency = (
            _dependency_seed(project_id, project, item_id, package)
            if project is not None and package is not None
            else None
        )
        normalised.append((item, dependency, index))

    # A selected shared-question family behaves as one movable unit. Its first
    # requested position stays fixed while siblings are placed next to it.
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any] | None, int]]] = {}
    for entry in normalised:
        group_key = entry[1].get("group_key") if entry[1] else None
        if group_key:
            grouped.setdefault(group_key, []).append(entry)
    for siblings in grouped.values():
        siblings.sort(
            key=lambda entry: (
                entry[1].get("sub_index") is None,
                0 if isinstance(entry[1].get("sub_index"), int) else 1,
                entry[1].get("sub_index") if isinstance(entry[1].get("sub_index"), int) else str(entry[1].get("sub_index") or "").casefold(),
                entry[2],
            )
        )

    result: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for item, dependency, _ in normalised:
        group_key = dependency.get("group_key") if dependency else None
        if not group_key:
            result.append(item)
        elif group_key not in emitted:
            result.extend(entry[0] for entry in grouped[group_key])
            emitted.add(group_key)
    return result


def patch_question_set(
    workspace_root: Path | str,
    set_id: str,
    expected_revision: int,
    patch: Mapping[str, Any],
) -> dict[str, Any]:
    """CAS-update title, output mode, rendering, or ordered item references."""

    expected_revision = _positive_revision(expected_revision, field="expected_revision")
    if not isinstance(patch, Mapping):
        raise QuestionBankError("patch must be an object")
    unexpected = set(patch) - {"title", "output_mode", "items", "rendering"}
    if unexpected:
        raise QuestionBankError(f"unknown patch fields: {', '.join(sorted(unexpected))}")
    if not patch:
        raise QuestionBankError("patch cannot be empty")
    path = _set_path(workspace_root, set_id)
    with _lock_for(path):
        if not path.is_file():
            raise QuestionSetNotFound(f"question set not found: {set_id}")
        current = _validate_question_set(load_json_object(path), path=path)
        if current["revision"] != expected_revision:
            raise RevisionConflict(expected_revision, current["revision"])
        candidate = copy.deepcopy(current)
        if "title" in patch:
            candidate["title"] = _title(patch["title"])
        if "output_mode" in patch:
            candidate["output_mode"] = _output_mode(patch["output_mode"])
        if "items" in patch:
            candidate["items"] = _normalise_patch_items(
                workspace_root, current["items"], patch["items"]
            )
        if "rendering" in patch:
            candidate["rendering"] = _rendering(patch["rendering"], current.get("rendering"))
        candidate["revision"] = expected_revision + 1
        candidate["updated_at"] = utc_now()
        candidate = _validate_question_set(candidate, path=path)
        atomic_write_json(path, candidate)
        return candidate


def delete_question_set(
    workspace_root: Path | str, set_id: str, expected_revision: int
) -> dict[str, Any]:
    expected_revision = _positive_revision(expected_revision, field="expected_revision")
    path = _set_path(workspace_root, set_id)
    with _lock_for(path):
        if not path.is_file():
            raise QuestionSetNotFound(f"question set not found: {set_id}")
        current = _validate_question_set(load_json_object(path), path=path)
        if current["revision"] != expected_revision:
            raise RevisionConflict(expected_revision, current["revision"])
        archive_root = path.parent / ".deleted"
        if archive_root.exists() and not _is_within(archive_root.resolve(), _workspace(workspace_root)):
            raise QuestionBankError("unsafe question-set archive")
        archive_root.mkdir(exist_ok=True)
        archive = archive_root / path.name
        if archive.exists() or archive.is_symlink():
            raise QuestionSetArchiveConflict(f"question set archive already exists: {set_id}")
        path.replace(archive)
        return current


__all__ = [
    "OUTPUT_MODES",
    "QUESTION_SET_SCHEMA",
    "QuestionBankError",
    "QuestionSetNotFound",
    "QuestionSetArchiveConflict",
    "create_question_set",
    "delete_question_set",
    "get_question_set",
    "list_bank_items",
    "list_question_sets",
    "patch_question_set",
    "resolve_question_set",
    "search_bank_items",
]
