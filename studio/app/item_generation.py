"""AI item generation: Lab learning data, drafts, automatic checks and bank storage.

Generated items are stored as ordinary canonical question packages inside one
hidden internal project per subject (``project-itemgen-<taxonomy>``).  They
carry ``provenance.source_system.name == "item_generation"`` and
``provenance.generation`` instead of a PDF ``problem`` record.  Original
projects are never modified.
"""
from __future__ import annotations

import ast
import copy
import datetime as dt
import hashlib
import json
import math
import operator
import os
import re
import secrets
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from .model import (
    ModelError,
    atomic_write_json,
    load_json_object,
    make_source_only_project,
    save_json_cas,
    sha256_file,
    utc_now,
    validate_id,
)

PIPELINE_VERSION = "item-generation-v1"
INTERNAL_PROJECT_PREFIX = "project-itemgen-"
JOBS_DIRNAME = ".item_generation"
MAX_ITEMS_PER_JOB = 50
NEW_SIMILARITY_LIMIT = 0.45
VARIANT_COPY_LIMIT = 0.8
CHOICE_LABELS = ("①", "②", "③", "④", "⑤")
BOGI_LABELS = ("ㄱ", "ㄴ", "ㄷ", "ㄹ", "ㅁ")
SUBJECT_TAXONOMY = {"통합과학2": "integrated-2", "통합과학1": "integrated-1", "역학과 에너지": "mechanics-energy"}
SIMPLE_UNIT_BY_ROMAN = {
    "통합과학2": {"Ⅰ": "변화와 다양성", "Ⅱ": "환경과 에너지", "Ⅲ": "과학과 미래 사회"},
}
_LOCK = threading.RLock()


class ItemGenerationError(ValueError):
    """Invalid generation request, draft or stored job."""


class ItemGenerationConflict(ItemGenerationError):
    """The job changed while it was being updated."""


# --------------------------------------------------------------------------- Lab data

def lab_root() -> Path:
    return Path(os.environ.get("ASTRA_ITEM_LAB_ROOT") or "C:/Question_Item_Lab").resolve()


_CACHE: dict[str, tuple[int, Any]] = {}


def _cached_json(path: Path, *, lines: bool = False) -> Any:
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return [] if lines else None
    key = str(path)
    hit = _CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    text = path.read_text(encoding="utf-8")
    value = [json.loads(line) for line in text.splitlines() if line.strip()] if lines else json.loads(text)
    _CACHE[key] = (stamp, value)
    return value


def list_scopes(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or lab_root()
    scopes = []
    for path in sorted((root / "scope").glob("*.json")):
        grade, _, subject = path.stem.partition("-")
        scopes.append({"scope": path.stem, "grade": grade, "subject": subject})
    return scopes


def load_scope(scope: str, root: Path | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[^/\\.]{1,40}-[^/\\.]{1,40}", scope or ""):
        raise ItemGenerationError("학년 범위 이름이 올바르지 않습니다.")
    path = (root or lab_root()) / "scope" / f"{scope}.json"
    data = _cached_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("units"), list):
        raise ItemGenerationError(f"학년 범위 파일을 찾을 수 없습니다: {scope}")
    return data


def codex_effort_for_scope(scope: str) -> str:
    match = re.match(r"(중|고)(\d)", scope or "")
    if not match:
        return "xhigh"
    return "xhigh" if match.group(1) == "고" and int(match.group(2)) >= 2 else "high"


def _lab_projects(root: Path) -> list[Path]:
    return sorted(path for path in (root / "projects").glob("project-*") if (path / "items.jsonl").is_file())


def analyses_for_unit(major: str, middle: str, root: Path | None = None) -> list[dict[str, Any]]:
    root = root or lab_root()
    result = []
    for project in _lab_projects(root):
        for row in _cached_json(project / "items.jsonl", lines=True):
            unit = row.get("unit") or {}
            if unit.get("major") == major and unit.get("middle") == middle:
                result.append(row)
    return result


def lab_inputs(root: Path | None = None) -> dict[str, dict[str, Any]]:
    root = root or lab_root()
    inputs: dict[str, dict[str, Any]] = {}
    for project in _lab_projects(root):
        for row in _cached_json(project / "_input.jsonl", lines=True):
            if isinstance(row.get("item_id"), str):
                inputs[row["item_id"]] = row
    return inputs


def item_analysis(item_id: str, root: Path | None = None) -> dict[str, Any] | None:
    root = root or lab_root()
    for project in _lab_projects(root):
        for row in _cached_json(project / "items.jsonl", lines=True):
            if row.get("item_id") == item_id:
                return row
    return None


def topic_tree(scope: str, root: Path | None = None) -> dict[str, Any]:
    """Major → middle → detail topics (from analyses) → allowed scope concepts."""

    root = root or lab_root()
    data = load_scope(scope, root)
    grade, _, subject = scope.partition("-")
    majors: dict[str, dict[str, Any]] = {}
    for unit in data["units"]:
        analyses = analyses_for_unit(unit["major"], unit["middle"], root)
        details: dict[str, int] = {}
        for row in analyses:
            detail = (row.get("unit") or {}).get("detail_topic")
            if isinstance(detail, str) and detail.strip():
                details[detail.strip()] = details.get(detail.strip(), 0) + 1
        pattern = _pattern_path(root, subject, unit)
        majors.setdefault(unit["major"], {"major": unit["major"], "middles": []})["middles"].append({
            "middle": unit["middle"],
            "concepts": list(unit.get("concepts") or []),
            "analysis_count": len(analyses),
            "pattern_available": pattern.is_file(),
            "details": [{"name": name, "count": count} for name, count in sorted(details.items(), key=lambda kv: -kv[1])],
        })
    return {"scope": scope, "grade": grade, "subject": subject, "curriculum": data.get("curriculum"),
            "codex_effort": codex_effort_for_scope(scope), "majors": list(majors.values())}


def search_topics(scope: str, query: str, root: Path | None = None) -> list[dict[str, Any]]:
    terms = [term for term in (query or "").casefold().split() if term]
    if not terms:
        return []
    hits = []
    for major in topic_tree(scope, root)["majors"]:
        for middle in major["middles"]:
            candidates = [("middle", middle["middle"])] + [("concept", c) for c in middle["concepts"]] + [
                ("detail", d["name"]) for d in middle["details"]]
            for kind, text in candidates:
                haystack = f"{major['major']} {middle['middle']} {text}".casefold()
                if all(term in haystack for term in terms):
                    hits.append({"major": major["major"], "middle": middle["middle"], "kind": kind, "text": text})
    return hits[:50]


def _pattern_path(root: Path, subject: str, unit: Mapping[str, Any]) -> Path:
    return root / "patterns" / subject / f"{str(unit['major']).split('.')[0]}_{unit['middle']}.md"


def resolve_unit(scope: str, major: str, middle: str, root: Path | None = None) -> dict[str, Any]:
    for unit in load_scope(scope, root)["units"]:
        if unit.get("major") == major and unit.get("middle") == middle:
            return unit
    raise ItemGenerationError("선택한 단원이 학년 범위 파일에 없습니다.")


def guides_text(subject: str, root: Path | None = None) -> tuple[str, str | None]:
    """Shared guide documents read by both the bot and the app; returns (text, sha256)."""

    root = root or lab_root()
    parts = []
    for path in (root / "guides" / "출제지침.md", root / "guides" / "과목" / f"{subject}.md"):
        if path.is_file():
            parts.append(f"## {path.name}\n" + path.read_text(encoding="utf-8"))
    if not parts:
        return "", None
    text = "\n\n".join(parts)
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def append_guide_rule(rule: str, root: Path | None = None) -> dict[str, Any]:
    """'지침에 추가': append one dated line to the common guide (atomic replace)."""

    rule = " ".join(str(rule or "").split())
    if not rule:
        raise ItemGenerationError("추가할 지침을 입력하세요.")
    if len(rule) > 500:
        raise ItemGenerationError("지침은 500자 이내로 입력하세요.")
    path = (root or lab_root()) / "guides" / "출제지침.md"
    with _LOCK:
        if not path.is_file() or path.is_symlink():
            raise ItemGenerationError("공통 지침서(guides/출제지침.md)가 없습니다.")
        text = path.read_text(encoding="utf-8").rstrip("\n")
        line = f"- ({dt.date.today().isoformat()}) {rule}"
        temp = path.with_name(path.name + f".{secrets.token_hex(4)}.tmp")
        temp.write_text(text + "\n" + line + "\n", encoding="utf-8")
        os.replace(temp, path)
    return {"added": line, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


# --------------------------------------------------------------------------- similarity

def _blocks_text(blocks: Any) -> str:
    out: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "text" and isinstance(value.get("text"), str):
                out.append(value["text"])
            elif value.get("type") == "equation":
                out.append(str(value.get("source_text") or value.get("latex") or ""))
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(blocks)
    return " ".join(out)


def _inline_text(inlines: Any) -> str:
    out = []
    for inline in inlines or []:
        if not isinstance(inline, Mapping):
            continue
        if inline.get("type") == "equation":
            out.append("$" + str(inline.get("latex") or inline.get("source_text") or "") + "$")
        elif inline.get("type") == "line_break":
            out.append("\n")
        else:
            out.append(str(inline.get("text") or ""))
    return "".join(out)


def blocks_text_for_prompt(blocks: Any, indent: str = "") -> str:
    """Readable text of canonical blocks for model prompts (mirrors the Lab render_item)."""

    lines: list[str] = []
    for block in blocks or []:
        if not isinstance(block, Mapping):
            continue
        kind, role = block.get("type"), block.get("role")
        if kind == "paragraph":
            tag = f"[{role}] " if role and role != "body" else ""
            lines.append(indent + tag + _inline_text(block.get("inlines")))
        elif kind == "figure":
            lines.append(indent + f"[그림] {block.get('alt_text') or '(설명 없음)'}")
        elif kind == "table":
            lines.append(indent + "[표]")
            for row in block.get("rows") or []:
                cells = [" ".join(blocks_text_for_prompt(c.get("blocks")).split()) for c in row.get("cells") or []]
                lines.append(indent + "| " + " | ".join(cells) + " |")
        elif kind in {"bogi", "choices"}:
            lines.append(indent + ("[보기]" if kind == "bogi" else "[선지]"))
            for item in block.get("items") or []:
                lines.append(indent + f"  {item.get('label', '')} " + " ".join(blocks_text_for_prompt(item.get("blocks")).split()))
        else:
            if block.get("inlines"):
                lines.append(indent + _inline_text(block.get("inlines")))
            inner = block.get("blocks")
            if inner:
                lines.append(blocks_text_for_prompt(inner, indent + "  "))
    return "\n".join(lines)


def grams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text or "")
    return {compact[i:i + 3] for i in range(max(0, len(compact) - 2))}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def draft_text(draft: Mapping[str, Any]) -> str:
    parts = [str(draft.get("stem") or ""), str(draft.get("instruction") or "")]
    parts += [str(v) for v in (draft.get("bogi") or {}).values()]
    parts += [str(v) for v in (draft.get("choices") or {}).values()]
    table = draft.get("table")
    if isinstance(table, Mapping):
        parts.append(" ".join(map(str, table.get("columns") or [])))
        parts += [" ".join(map(str, row)) for row in table.get("rows") or []]
    return " ".join(parts)


# --------------------------------------------------------------------------- safe arithmetic

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Mod: operator.mod}


def safe_eval(expression: str) -> float:
    """Evaluate a plain arithmetic expression (numbers, + - * / ** %, parentheses) only."""

    if not isinstance(expression, str) or len(expression) > 200:
        raise ItemGenerationError("계산식이 올바르지 않습니다.")
    text = expression.replace("×", "*").replace("÷", "/").replace("^", "**").replace("−", "-")

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 12:
                raise ItemGenerationError("지수가 너무 큽니다.")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ItemGenerationError("허용되지 않은 계산식입니다.")

    try:
        return ev(ast.parse(text, mode="eval"))
    except (SyntaxError, ZeroDivisionError, OverflowError) as exc:
        raise ItemGenerationError(f"계산식을 계산할 수 없습니다: {expression}") from exc


def check_calculations(draft: Mapping[str, Any]) -> dict[str, Any]:
    checks = draft.get("calc_checks")
    if not checks:
        return {"calc_ok": None, "calc_results": []}
    results, ok = [], True
    for index, check in enumerate(checks if isinstance(checks, list) else []):
        expr = check.get("expression") if isinstance(check, Mapping) else None
        expected = check.get("expected") if isinstance(check, Mapping) else None
        try:
            value = safe_eval(str(expr))
            expected_value = float(expected)
            good = math.isclose(value, expected_value, rel_tol=1e-6, abs_tol=1e-9)
            results.append({"index": index, "expression": expr, "computed": value, "expected": expected_value, "ok": good})
        except (ItemGenerationError, TypeError, ValueError) as exc:
            good = False
            results.append({"index": index, "expression": expr, "error": str(exc), "ok": False})
        ok = ok and good
    return {"calc_ok": ok, "calc_results": results}


# --------------------------------------------------------------------------- draft validation

DRAFT_REQUIRED = ("kind", "difficulty", "format", "scope_concepts", "stem", "instruction", "answer", "solution",
                  "design_intent", "distractor_intents", "unique_answer_rationale")


def draft_format_errors(draft: Any) -> list[str]:
    if not isinstance(draft, Mapping):
        return ["초안은 객체여야 합니다."]
    errors = [f"필수 항목 없음: {key}" for key in DRAFT_REQUIRED if not draft.get(key)]
    if draft.get("kind") not in {"variant", "new"}:
        errors.append("kind는 variant 또는 new여야 합니다.")
    if draft.get("kind") == "variant" and not draft.get("source_item_id"):
        errors.append("변형 문항에는 source_item_id가 필요합니다.")
    choices = draft.get("choices")
    if choices is not None:
        if not isinstance(choices, Mapping) or not choices or any(k not in CHOICE_LABELS for k in choices):
            errors.append("선지는 ①~⑤ 기호를 키로 하는 객체여야 합니다.")
        elif str(draft.get("answer") or "").strip() not in choices:
            errors.append("선택형 정답은 선지 기호 하나여야 합니다.")
    bogi = draft.get("bogi")
    if bogi is not None and (not isinstance(bogi, Mapping) or any(k not in BOGI_LABELS for k in bogi)):
        errors.append("보기는 ㄱ~ㅁ을 키로 하는 객체여야 합니다.")
    table = draft.get("table")
    if table is not None:
        columns = table.get("columns") if isinstance(table, Mapping) else None
        rows = table.get("rows") if isinstance(table, Mapping) else None
        if not isinstance(columns, list) or not columns or not isinstance(rows, list) or not rows or any(
                not isinstance(r, list) or len(r) != len(columns) for r in rows):
            errors.append("표는 columns와 같은 길이의 rows가 필요합니다.")
    figure = draft.get("figure")
    if figure is not None and (not isinstance(figure, Mapping) or not isinstance(figure.get("spec"), Mapping)):
        errors.append("그림은 spec(그림 명세) 객체가 필요합니다.")
    if not isinstance(draft.get("design_intent"), Mapping):
        errors.append("design_intent는 객체여야 합니다.")
    return errors


def auto_checks(draft: Mapping[str, Any], *, allowed_concepts: Iterable[str],
                corpus: Mapping[str, set[str]], printed: Mapping[str, Any]) -> dict[str, Any]:
    allowed = set(allowed_concepts)
    checks: dict[str, Any] = {}
    errors = draft_format_errors(draft)
    if errors:
        checks["format_errors"] = errors
    bad = [c for c in draft.get("scope_concepts") or [] if c not in allowed]
    checks["scope_ok"] = not bad and bool(draft.get("scope_concepts"))
    if bad:
        checks["scope_unlisted"] = bad
    g = grams(draft_text(draft))
    best = max(((jaccard(g, v), k) for k, v in corpus.items()), default=(0.0, None))
    checks["max_similarity"] = {"score": round(best[0], 3), "item_id": best[1], "printed_number": printed.get(best[1])}
    if draft.get("kind") == "new" and best[0] >= NEW_SIMILARITY_LIMIT:
        checks["too_similar_for_new"] = True
    source = draft.get("source_item_id")
    if draft.get("kind") == "variant" and source in corpus:
        score = round(jaccard(g, corpus[source]), 3)
        checks["similarity_to_source"] = score
        if score >= VARIANT_COPY_LIMIT:
            checks["near_copy"] = True
    checks.update(check_calculations(draft))
    checks["passed"] = bool(checks["scope_ok"] and not errors and not checks.get("too_similar_for_new")
                            and not checks.get("near_copy") and checks.get("calc_ok") is not False)
    return checks


# --------------------------------------------------------------------------- jobs

def jobs_root(workspace: Path | str) -> Path:
    return Path(workspace).resolve() / JOBS_DIRNAME


def new_job_id() -> str:
    return "gen-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def job_dir(workspace: Path | str, job_id: str) -> Path:
    validate_id(job_id, field="job_id")
    if not job_id.startswith("gen-"):
        raise ItemGenerationError("생성 작업 번호가 올바르지 않습니다.")
    path = jobs_root(workspace) / job_id
    if path.is_symlink():
        raise ItemGenerationError("생성 작업 경로가 링크입니다.")
    return path


def load_job(workspace: Path | str, job_id: str) -> dict[str, Any]:
    path = job_dir(workspace, job_id) / "job.json"
    if not path.is_file():
        raise ItemGenerationError("생성 작업을 찾을 수 없습니다.")
    return load_json_object(path)


def save_job(workspace: Path | str, job: dict[str, Any], *, expected_revision: int | None = None) -> dict[str, Any]:
    with _LOCK:
        path = job_dir(workspace, job["job_id"]) / "job.json"
        current = load_json_object(path) if path.is_file() else None
        current_revision = int(current.get("revision", 0)) if current else 0
        if expected_revision is not None and current_revision != expected_revision:
            raise ItemGenerationConflict("생성 작업이 다른 곳에서 바뀌었습니다. 새로 고친 뒤 다시 시도하세요.")
        job["revision"] = current_revision + 1
        job["updated_at"] = utc_now()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, job)
        return job


def update_job(workspace: Path | str, job_id: str, change) -> dict[str, Any]:
    with _LOCK:
        job = load_job(workspace, job_id)
        revision = job.get("revision")
        change(job)
        return save_job(workspace, job, expected_revision=revision)


def list_jobs(workspace: Path | str, *, limit: int = 50) -> list[dict[str, Any]]:
    root = jobs_root(workspace)
    jobs = []
    for path in sorted(root.glob("gen-*/job.json"), reverse=True)[:limit]:
        try:
            job = load_json_object(path)
        except (ModelError, OSError):
            continue
        drafts = job.get("drafts") or []
        jobs.append({key: job.get(key) for key in ("job_id", "status", "route", "scope", "unit", "request", "created_at",
                                                    "updated_at", "error", "progress")}
                    | {"draft_count": len(drafts),
                       "verified_count": sum((d.get("verification") or {}).get("status") == "verified" for d in drafts),
                       "confirmed_count": sum(bool(d.get("confirmed_item_id")) for d in drafts)})
    return jobs


def validate_request(body: Mapping[str, Any], root: Path | None = None) -> dict[str, Any]:
    scope = str(body.get("scope") or "")
    unit = resolve_unit(scope, str(body.get("major") or ""), str(body.get("middle") or ""), root)
    variants, new = body.get("variants", 0), body.get("new", 0)
    if type(variants) is not int or type(new) is not int or variants < 0 or new < 0:
        raise ItemGenerationError("문항 수가 올바르지 않습니다.")
    if variants + new < 1 or variants + new > MAX_ITEMS_PER_JOB:
        raise ItemGenerationError(f"한 번에 1~{MAX_ITEMS_PER_JOB}문항까지 만들 수 있습니다.")
    source_ids = [str(x) for x in body.get("source_ids") or [] if isinstance(x, str) and x]
    for source in source_ids:
        validate_id(source, field="source_ids")
    figure = body.get("figure", "auto")
    if figure not in {"auto", "use", "none"}:
        raise ItemGenerationError("그림 옵션이 올바르지 않습니다.")
    details = [str(x)[:120] for x in body.get("details") or [] if isinstance(x, str) and x.strip()][:10]
    return {
        "scope": scope, "major": unit["major"], "middle": unit["middle"], "details": details,
        "variants": variants, "new": new,
        "difficulty": str(body.get("difficulty") or "중,상")[:20],
        "format": str(body.get("format") or "")[:40] or None,
        "purpose": str(body.get("purpose") or "")[:20] or None,
        "figure": figure,
        "free_request": str(body.get("free_request") or "")[:2000] or None,
        "source_ids": source_ids,
    }


def estimate_calls(request: Mapping[str, Any]) -> dict[str, int]:
    total = int(request["variants"]) + int(request["new"])
    batches = math.ceil(int(request["variants"]) / 5) + math.ceil(int(request["new"]) / 5)
    figures = total if request.get("figure") == "use" else (0 if request.get("figure") == "none" else math.ceil(total / 2))
    return {"claude_generate": batches, "figure_codex_max": figures, "figure_check_claude_max": figures,
            "codex_verify": math.ceil(total / 5)}


def create_job(workspace: Path | str, request: Mapping[str, Any], *, route: str = "quick",
               bot_session_id: str | None = None) -> dict[str, Any]:
    job_id = new_job_id()
    job_dir(workspace, job_id).mkdir(parents=True, exist_ok=False)
    job = {
        "schema": "item-generation-job-v1", "job_id": job_id, "route": route, "status": "queued",
        "scope": request["scope"], "unit": {"major": request["major"], "middle": request["middle"]},
        "request": dict(request), "estimate": estimate_calls(request) if route == "quick" else None,
        "created_at": utc_now(), "drafts": [], "usage": [], "error": None,
        "progress": {"step": "queued", "message": "대기 중"}, "bot_session_id": bot_session_id,
    }
    return save_job(workspace, job)


# --------------------------------------------------------------------------- canonical conversion

def _hid(*parts: Any) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:24]


def _text_run(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text, "source_text": None}


def _paragraph(seed: str, role: str, text: str) -> dict[str, Any]:
    return {"block_id": f"block-{_hid(seed, role, text)}", "type": "paragraph", "role": role,
            "inlines": [_text_run(text)], "review_status": "unreviewed"}


def _paragraphs(seed: str, role: str, text: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return [_paragraph(f"{seed}:{index}", role, line) for index, line in enumerate(lines)]


def _table_block(seed: str, table: Mapping[str, Any]) -> dict[str, Any]:
    columns = [str(c) for c in table["columns"]]

    def cell(role: str, text: Any, index: str) -> dict[str, Any]:
        return {"role": role, "row_span": 1, "col_span": 1, "blocks": [_paragraph(f"{seed}:{index}", "body", str(text))]}

    rows = [{"cells": [cell("header", c, f"h{i}") for i, c in enumerate(columns)]}]
    rows += [{"cells": [cell("body", v, f"r{r}c{c}") for c, v in enumerate(row)]} for r, row in enumerate(table["rows"])]
    return {"block_id": f"block-{_hid(seed, 'table')}", "type": "table", "semantic_type": "generic",
            "column_count": len(columns), "rows": rows, "review_status": "unreviewed"}


def _intent_lines(draft: Mapping[str, Any]) -> list[str]:
    intent = draft.get("design_intent") if isinstance(draft.get("design_intent"), Mapping) else {}
    labels = (("purpose", "평가 목적"), ("assessed_ability", "측정 능력"), ("structure_rationale", "구성 이유"),
              ("difficulty_design", "난이도 설계"))
    lines = ["[출제 의도]"] + [f"{label}: {intent[key]}" for key, label in labels if intent.get(key)]
    targeted = intent.get("misconceptions_targeted")
    if isinstance(targeted, list) and targeted:
        lines.append("겨냥한 오개념: " + " / ".join(map(str, targeted)))
    distractors = draft.get("distractor_intents")
    if isinstance(distractors, Mapping) and distractors:
        lines.append("오답 설계: " + " / ".join(f"{k} {v}" for k, v in distractors.items()))
    return lines if len(lines) > 1 else []


def answer_type_for(draft: Mapping[str, Any]) -> str:
    if draft.get("choices"):
        return "multiple_choice"
    text = str(draft.get("format") or "")
    return "descriptive" if "서술" in text else "short_answer"


def draft_to_content(draft: Mapping[str, Any], seed: str, *, figure_asset_id: str | None = None,
                     include_intent: bool = True) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = _paragraphs(f"{seed}:stem", "stem", str(draft["stem"]))
    if figure_asset_id:
        figure = draft.get("figure") if isinstance(draft.get("figure"), Mapping) else {}
        blocks.append({
            "block_id": f"block-{_hid(seed, 'figure')}", "type": "figure", "role": "main",
            "asset_id": figure_asset_id, "original_asset_id": figure_asset_id,
            "alt_text": str(figure.get("alt_text") or (figure.get("spec") or {}).get("description") or "생성 그림")[:500],
            "visual_kind": "pure_figure", "placement_context": "body",
            "regeneration_policy": "original_only", "regeneration_note": None,
            "review_status": "unreviewed",
        })
    if isinstance(draft.get("table"), Mapping):
        blocks.append(_table_block(seed, draft["table"]))
    blocks += _paragraphs(f"{seed}:instruction", "instruction", str(draft["instruction"]))
    bogi = draft.get("bogi")
    if isinstance(bogi, Mapping) and bogi:
        blocks.append({
            "block_id": f"block-{_hid(seed, 'bogi')}", "type": "bogi",
            "title": {"visible": True, "text": "[ 보기 ]"},
            "items": [{"item_id": f"bogi-item-{_hid(seed, 'bogi', key)}", "label": f"{key}.",
                       "blocks": [_paragraph(f"{seed}:bogi:{key}", "body", str(value))]}
                      for key, value in bogi.items()],
            "review_status": "unreviewed",
        })
    answer: dict[str, Any]
    choices = draft.get("choices")
    if isinstance(choices, Mapping) and choices:
        choice_ids = {key: f"choice-{_hid(seed, 'choice', key)}" for key in choices}
        # Short choices (ㄱ, ㄴ, ㄷ combinations) use the textbook grid like the source items; long ones stack.
        layout = "grid" if max(len(str(value)) for value in choices.values()) <= 12 else "vertical"
        blocks.append({
            "block_id": f"block-{_hid(seed, 'choices')}", "type": "choices", "source_layout": layout,
            "items": [{"choice_id": choice_ids[key], "label": key,
                       "blocks": [_paragraph(f"{seed}:choice:{key}", "body", str(value))]}
                      for key, value in choices.items()],
            "review_status": "unreviewed",
        })
        answer = {"kind": "choice", "choice_ids": [choice_ids[str(draft["answer"]).strip()]]}
    else:
        answer = {"kind": "content", "blocks": _paragraphs(f"{seed}:answer", "answer", str(draft["answer"]))}
    explanation_blocks = _paragraphs(f"{seed}:solution", "explanation", str(draft["solution"]))
    if include_intent:
        explanation_blocks += [_paragraph(f"{seed}:intent:{i}", "note", line) for i, line in enumerate(_intent_lines(draft))]
    return {"question": {"blocks": blocks}, "answer": answer,
            "explanation": {"blocks": explanation_blocks} if explanation_blocks else None}


def _simple_unit(subject: str, major: str) -> str:
    roman = re.match(r"\s*([ⅠⅡⅢⅣⅤ])", major or "")
    return SIMPLE_UNIT_BY_ROMAN.get(subject, {}).get(roman.group(1) if roman else "", "미분류")


def _tags(document: Mapping[str, Any], *, subject: str, grade: int | None, major: str,
          draft: Mapping[str, Any]) -> dict[str, Any]:
    from .tagging import _features, tag_source_sha256

    features = _features(document)
    if features["has_figure"]:
        features["visual_dependency"] = "required"
    level = {"상": "advanced", "중": "standard", "하": "basic"}.get(str(draft.get("difficulty") or ""), "unknown")
    keywords = [str(k)[:40] for k in (draft.get("concepts") or draft.get("scope_concepts") or []) if str(k).strip()][:8]
    return {
        "curriculum": "2022 개정", "school_level": "high", "grade": grade, "subject": subject,
        "unit": _simple_unit(subject, major), "difficulty": level,
        "problem_types": (["condition_judgment"] if draft.get("bogi") else ["concept"]),
        "keywords": list(dict.fromkeys(keywords)), "features": features, "source": "ai", "confidence": None,
        "review_status": "auto", "source_sha256": tag_source_sha256(document), "updated_at": utc_now(),
    }


def internal_project_id(subject: str) -> str:
    key = SUBJECT_TAXONOMY.get(subject)
    if not key:
        raise ItemGenerationError(f"지원하지 않는 생성 과목입니다: {subject}")
    return INTERNAL_PROJECT_PREFIX + key


def is_internal_project(project_id: Any) -> bool:
    return isinstance(project_id, str) and project_id.startswith(INTERNAL_PROJECT_PREFIX)


def _source_item_record(workspace: Path, source_id: str | None, inputs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    if not source_id:
        return None
    row = inputs.get(source_id) or {}
    project_id = row.get("project_id")
    if not isinstance(project_id, str):
        analysis = item_analysis(source_id)
        project_id = (analysis or {}).get("project_id")
    if not isinstance(project_id, str):
        return None
    return {"project_id": project_id, "item_id": source_id,
            "printed_number": str(row.get("printed_number")) if row.get("printed_number") is not None else None,
            "content_hash": row.get("content_hash") if re.fullmatch(r"[0-9a-f]{64}", str(row.get("content_hash") or "")) else None}


def build_package(workspace: Path | str, job: Mapping[str, Any], draft: Mapping[str, Any], *, item_id: str,
                  include_intent: bool = True) -> tuple[dict[str, Any], Path | None]:
    """Return (canonical package, generated figure file or None)."""

    workspace = Path(workspace).resolve()
    scope = str(job["scope"])
    grade_text, _, subject = scope.partition("-")
    grade_match = re.search(r"([1-3])", grade_text)
    grade = int(grade_match.group(1)) if grade_match else None
    figure_file: Path | None = None
    figure_asset_id = None
    assets: list[dict[str, Any]] = []
    figure_record = None
    figure = draft.get("figure") if isinstance(draft.get("figure"), Mapping) else None
    if figure and figure.get("file"):
        figure_file = (job_dir(workspace, str(job["job_id"])) / str(figure["file"])).resolve()
        if not figure_file.is_file() or job_dir(workspace, str(job["job_id"])).resolve() not in figure_file.parents:
            raise ItemGenerationError("초안 그림 파일을 찾을 수 없습니다.")
        from PIL import Image

        with Image.open(figure_file) as opened:
            width, height = opened.size
        figure_asset_id = f"figure-{_hid(item_id, 'generated')}"
        digest = sha256_file(figure_file)
        assets.append({
            "asset_id": figure_asset_id, "section": "question", "role": "figure_generated", "state": "ready",
            "path": f"assets/{figure_asset_id}.png", "media_type": "image/png", "sha256": digest,
            "width_px": width, "height_px": height, "generation": None,
            "review": {"status": "unreviewed", "boundary_uncertain": False, "notes": []},
        })
        figure_record = {"method": figure.get("method") if figure.get("method") in {"code", "codex_image"} else "code",
                         "spec": dict(figure.get("spec") or {}), "check": dict(figure.get("check") or {}),
                         "asset_id": figure_asset_id, "sha256": digest}
    content = draft_to_content(draft, item_id, figure_asset_id=figure_asset_id, include_intent=include_intent)
    now = utc_now()
    verification = dict(draft.get("verification") or {"status": "pending"})
    request = job.get("request") if isinstance(job.get("request"), Mapping) else {}
    generation = {
        "kind": draft["kind"], "route": job.get("route", "quick"), "job_id": job["job_id"],
        "draft_no": int(draft["no"]), "variant_type": [str(v) for v in draft.get("variant_type") or []],
        "source_item": _source_item_record(workspace, draft.get("source_item_id"), lab_inputs()),
        "reference_item_ids": [str(v) for v in draft.get("reference_item_ids") or []],
        "topic": {"subject": subject, "major": job["unit"]["major"], "middle": job["unit"]["middle"],
                  "detail": (request.get("details") or [None])[0]},
        "scope_file": f"scope/{scope}.json", "scope_concepts": [str(v) for v in draft.get("scope_concepts") or []],
        "concepts": [str(v) for v in draft.get("concepts") or []],
        "difficulty": draft.get("difficulty"), "format": draft.get("format"),
        "design_intent": dict(draft.get("design_intent") or {}),
        "distractor_intents": dict(draft.get("distractor_intents") or {}),
        "unique_answer_rationale": draft.get("unique_answer_rationale"),
        "generator": {"model": str(draft.get("generator_model") or job.get("generator_model") or "claude-opus-5-5"),
                      "effort": draft.get("generator_effort") or job.get("generator_effort") or "high",
                      "created_at": job.get("created_at") or now,
                      "guide_sha256": job.get("guide_sha256"), "request": dict(request),
                      "bot_session_id": job.get("bot_session_id")},
        "figure": figure_record,
        "verification": {"status": verification.get("status", "pending"), "model": verification.get("model"),
                         "effort": verification.get("effort"), "at": verification.get("at"),
                         "verifier_answer": verification.get("verifier_answer"),
                         "verifier_reasoning": verification.get("verifier_reasoning"),
                         "verifier_issue": verification.get("verifier_issue"),
                         "verifier_bogi": verification.get("verifier_bogi")},
        "auto_checks": dict(draft.get("auto_checks") or {}),
        "confirmed_at": now,
    }
    source = generation["source_item"]
    label = ("변형" if draft["kind"] == "variant" else "신규")
    printed = f"AI{label}-{job['job_id'][-6:]}-{int(draft['no'])}"
    if source and source.get("printed_number"):
        # No parentheses: "N(M)" is the bank's sub-question pattern and would group these items.
        printed += f" · 원{source['printed_number']}"
    package: dict[str, Any] = {
        "$schema": "../../../../schemas/question-package.schema.json",
        "schema_version": "0.6.0",
        "item_id": item_id, "language": "ko-KR",
        "workflow": {"status": "transcribed", "revision": 1, "created_at": now, "updated_at": now},
        "identity": {"printed_number": printed, "unit_kind": "question", "answer_type": answer_type_for(draft),
                     "score": None, "parent_item_id": None, "group_id": None},
        "provenance": {"imported_at": now,
                       "source_system": {"name": "item_generation", "pipeline_version": PIPELINE_VERSION},
                       "generation": generation, "solution_links": [], "transcriptions": [], "raw_snapshot": None},
        "content": content,
        "resources": {"assets": assets},
        "review": {"question": "unreviewed", "answer": "unreviewed",
                   "explanation": "unreviewed" if content["explanation"] else "not_applicable",
                   "assets": "unreviewed" if assets else "not_applicable",
                   "coverage": {"question": "not_applicable", "explanation": "not_applicable"},
                   "approved_revision": None, "approved_sha256": None, "reviewer": None, "reviewed_at": None,
                   "issues": []},
    }
    from .model import SCHEMA_VERSION

    package["schema_version"] = SCHEMA_VERSION
    package["tags"] = _tags(package, subject=subject, grade=grade, major=job["unit"]["major"], draft=draft)
    return package, figure_file


def validate_package(package: Mapping[str, Any], package_path: Path) -> list[str]:
    from scripts.validate_package import DEFAULT_SCHEMA, Validator, json_schema_errors

    return json_schema_errors(package, DEFAULT_SCHEMA) + Validator(package_path, check_files=True).validate(dict(package))


def _ensure_internal_project(workspace: Path, subject: str, item_id: str, grade: int | None) -> None:
    project_id = internal_project_id(subject)
    project_dir = workspace / project_id
    if project_dir.is_symlink():
        raise ItemGenerationError("내부 보관 프로젝트 경로가 링크입니다.")
    project_path = project_dir / "project.json"
    if project_path.exists():
        project = load_json_object(project_path)
        if item_id not in project["item_order"]:
            project["item_order"].append(item_id)
            save_json_cas(project_path, project, project["workflow"]["revision"])
        return
    project_dir.mkdir(parents=True, exist_ok=True)
    project = make_source_only_project(project_id=project_id, item_ids=[item_id], groups=[], source_import={})
    project["source_import"] = None
    project["metadata"].update({"subject": subject, "school_level": "high", "grade": str(grade) if grade else None,
                                "filename_base": f"AI 생성 문항 {subject}", "book_title": f"AI 생성 문항 · {subject}",
                                "book_type": "AI 생성"})
    atomic_write_json(project_path, project)


def confirm_drafts(workspace: Path | str, job_id: str, numbers: Iterable[int], *,
                   include_intent: bool = True) -> dict[str, Any]:
    """Store selected drafts in the bank (internal project). Already stored drafts are skipped."""

    workspace = Path(workspace).resolve()
    wanted = sorted({int(n) for n in numbers})
    if not wanted:
        raise ItemGenerationError("확정할 문항을 고르세요.")
    results = []
    with _LOCK:
        job = load_job(workspace, job_id)
        if job.get("status") not in {"ready", "partial"}:
            raise ItemGenerationError("생성·검증이 끝난 작업만 확정할 수 있습니다.")
        drafts = {int(d["no"]): d for d in job.get("drafts") or []}
        grade_match = re.search(r"([1-3])", str(job["scope"]).partition("-")[0])
        grade = int(grade_match.group(1)) if grade_match else None
        subject = str(job["scope"]).partition("-")[2]
        project_id = internal_project_id(subject)
        for no in wanted:
            draft = drafts.get(no)
            if draft is None:
                raise ItemGenerationError(f"{no}번 초안이 없습니다.")
            if draft.get("discarded"):
                raise ItemGenerationError(f"{no}번 초안은 버린 초안입니다.")
            if draft.get("confirmed_item_id"):
                results.append({"no": no, "item_id": draft["confirmed_item_id"], "already": True})
                continue
            if draft_format_errors(draft):
                raise ItemGenerationError(f"{no}번 초안 형식 오류: {draft_format_errors(draft)[0]}")
            if (draft.get("verification") or {}).get("status") in (None, "pending", "error", "not_solved"):
                raise ItemGenerationError(f"{no}번은 아직 Codex 검증을 받지 않았습니다. 검증 후 확정하세요.")
            item_id = f"gen-{_hid(job_id, no, uuid.uuid4().hex)}"
            stage = jobs_root(workspace) / ".stage" / uuid.uuid4().hex[:12]
            item_stage = stage / "item"
            (item_stage / "assets").mkdir(parents=True)
            try:
                package, figure_file = build_package(workspace, job, draft, item_id=item_id, include_intent=include_intent)
                if figure_file is not None:
                    asset = package["resources"]["assets"][0]
                    shutil.copyfile(figure_file, item_stage / asset["path"])
                question_path = item_stage / "question.json"
                atomic_write_json(question_path, package)
                errors = validate_package(package, question_path)
                if errors:
                    raise ItemGenerationError(f"{no}번 문항 저장 검증 실패: {errors[0]}")
                item_dir = workspace / project_id / "items" / item_id
                item_dir.parent.mkdir(parents=True, exist_ok=True)
                item_stage.rename(item_dir)
                _ensure_internal_project(workspace, subject, item_id, grade)
            finally:
                shutil.rmtree(stage, ignore_errors=True)
            draft["confirmed_item_id"] = item_id
            draft["confirmed_project_id"] = project_id
            draft["confirmed_at"] = utc_now()
            results.append({"no": no, "item_id": item_id, "already": False})
        save_job(workspace, job, expected_revision=job["revision"])
    return {"job_id": job_id, "project_id": project_id, "results": results}


def generation_summary(package: Mapping[str, Any]) -> dict[str, Any] | None:
    """Small bank-card summary for generated items (None for source items)."""

    provenance = package.get("provenance") if isinstance(package.get("provenance"), Mapping) else {}
    generation = provenance.get("generation")
    if not isinstance(generation, Mapping):
        return None
    source = generation.get("source_item") if isinstance(generation.get("source_item"), Mapping) else None
    return {"kind": generation.get("kind"), "job_id": generation.get("job_id"),
            "source_printed_number": source.get("printed_number") if source else None,
            "source_item_id": source.get("item_id") if source else None,
            "verification": (generation.get("verification") or {}).get("status"),
            "route": generation.get("route")}
