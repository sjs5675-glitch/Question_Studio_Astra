"""Registry of AI-generated items: stable item codes, fingerprints and duplicate checks.

Every generated draft gets a stable code (``AIQ-<yymmdd>-<job6>-<no>``) derived from
its job and draft number, so items created before this module existed also get a
code without rewriting stored files.  The index is rebuilt from the durable
records on each check: confirmed bank items (``project-itemgen-*``) and the drafts
of every generation job.  Nothing here calls a model.

Duplicate levels
- ``duplicate``: same normalised text, same text apart from numbers, or very high
  text overlap.  Blocks figures, verification and confirmation.
- ``similar``: notable text overlap or the same design structure (unit, format,
  concepts, figure type and purpose).  Shown as a warning only.
- ``unique``: nothing close was found.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .model import ModelError, load_json_object

DUPLICATE_TEXT = 0.6
SIMILAR_TEXT = 0.4
SIMILAR_CONCEPTS = 0.8
SIMILAR_PURPOSE = 0.5
_JOBS_DIRNAME = ".item_generation"
_INTERNAL_PREFIX = "project-itemgen-"


def item_code(job_id: str, no: int) -> str:
    match = re.fullmatch(r"gen-\d{2}(\d{6})-\d{6}-([0-9a-f]+)", str(job_id))
    if match:
        return f"AIQ-{match.group(1)}-{match.group(2)[-6:]}-{int(no):02d}"
    digest = hashlib.sha256(str(job_id).encode("utf-8")).hexdigest()[:6]
    return f"AIQ-000000-{digest}-{int(no):02d}"


def _norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(text or "")).lower()


def _skeleton(text: str) -> str:
    return re.sub(r"\d+(?:\.\d+)?", "#", _norm(text))


def _grams(text: str) -> set[str]:
    compact = _norm(text)
    return {compact[i:i + 3] for i in range(max(0, len(compact) - 2))}


def _jaccard(a: set[Any], b: set[Any]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_COMBO_CHOICE = re.compile(r"^[\sㄱ-ㅎ,·및와과]*$")


def draft_text(draft: Mapping[str, Any]) -> str:
    """Content used for comparison. The instruction sentence and pure ㄱㄴㄷ combination choices are
    shared boilerplate across 보기형 items and would make unrelated items look alike."""

    parts = [str(draft.get("stem") or "")]
    parts += [str(v) for v in (draft.get("bogi") or {}).values()] if isinstance(draft.get("bogi"), Mapping) else []
    if isinstance(draft.get("choices"), Mapping):
        parts += [str(v) for v in draft["choices"].values() if not _COMBO_CHOICE.match(str(v))]
    table = draft.get("table")
    if isinstance(table, Mapping):
        parts.append(" ".join(map(str, table.get("columns") or [])))
        parts += [" ".join(map(str, row)) for row in table.get("rows") or []]
    return " ".join(parts)


def _figure_type(figure: Any) -> str | None:
    if not isinstance(figure, Mapping):
        return None
    spec = figure.get("spec") if isinstance(figure.get("spec"), Mapping) else {}
    value = spec.get("type") or figure.get("method")
    return str(value) if value else None


def fingerprint(text: str, *, concepts: Iterable[Any] = (), fmt: Any = None, figure: Any = None,
                purpose: Any = None) -> dict[str, Any]:
    """Durable, model-free summary used to find duplicates later."""

    return {
        "text_sha256": _sha(_norm(text)),
        "skeleton_sha256": _sha(_skeleton(text)),
        "concepts": sorted({str(c) for c in concepts if str(c).strip()}),
        "format": str(fmt) if fmt else None,
        "figure_type": _figure_type(figure),
        "purpose": str(purpose or "")[:300] or None,
    }


def draft_fingerprint(draft: Mapping[str, Any]) -> dict[str, Any]:
    intent = draft.get("design_intent") if isinstance(draft.get("design_intent"), Mapping) else {}
    return fingerprint(draft_text(draft), concepts=list(draft.get("scope_concepts") or []) + list(draft.get("concepts") or []),
                       fmt=draft.get("format"), figure=draft.get("figure"), purpose=intent.get("purpose"))


def _entry(*, key: str, code: str, origin: str, scope: Any, unit: Mapping[str, Any], text: str,
           fp: Mapping[str, Any], item_id: str | None = None, kind: Any = None,
           source_item_id: Any = None) -> dict[str, Any]:
    return {"key": key, "code": code, "origin": origin, "item_id": item_id, "scope": scope,
            "major": unit.get("major"), "middle": unit.get("middle"), "kind": kind,
            "source_item_id": source_item_id, "text": text, "grams": _grams(text), "fingerprint": dict(fp),
            "purpose_grams": _grams(fp.get("purpose") or "")}


def draft_entry(job: Mapping[str, Any], draft: Mapping[str, Any]) -> dict[str, Any]:
    text = draft_text(draft)
    return _entry(key=f"{job['job_id']}#{int(draft['no'])}", code=draft.get("item_code") or item_code(job["job_id"], draft["no"]),
                  origin="draft", scope=job.get("scope"), unit=job.get("unit") or {}, text=text,
                  fp=draft_fingerprint(draft), kind=draft.get("kind"), source_item_id=draft.get("source_item_id"))


def _bank_text(package: Mapping[str, Any]) -> str:
    from .item_generation import _blocks_text
    stem, bogi, choices, tables = [], [], [], []
    def walk(blocks):
        for block in blocks or []:
            if not isinstance(block, Mapping):
                continue
            kind = block.get('type')
            if kind == 'figure' or block.get('role') == 'instruction':
                continue
            if kind in {'bogi', 'choices'}:
                for row in block.get('items') or []:
                    text = _blocks_text(row.get('blocks'))
                    if kind == 'bogi':
                        bogi.append(text)
                    elif not _COMBO_CHOICE.match(text):
                        choices.append(text)
            elif kind == 'table':
                for row in block.get('rows') or []:
                    tables.append(' '.join(_blocks_text(c.get('blocks')) for c in row.get('cells') or []))
            elif kind in {'paragraph', 'equation'}:
                stem.append(_blocks_text([block]))
            elif block.get('blocks'):
                walk(block['blocks'])
    walk(((package.get('content') or {}).get('question') or {}).get('blocks'))
    # Same comparison order as draft_text, independent of canonical layout order.
    return ' '.join(stem + bogi + choices + tables)


def bank_entries(workspace: Path) -> list[dict[str, Any]]:
    from .trash import Trash
    from .item_generation_identity import safe_path
    deleted = Trash(workspace).deletion_snapshot()
    out = []
    for path in sorted(Path(workspace).glob(f"{_INTERNAL_PREFIX}*/items/*/question.json")):
        try:
            if deleted(path.parents[2].name, path.parent.name):
                continue
            path = safe_path(Path(workspace), str(path.relative_to(workspace)))
            package = load_json_object(path)
        except (ModelError, OSError, ValueError):
            continue
        generation = ((package.get("provenance") or {}).get("generation") or {})
        if not isinstance(generation, Mapping):
            continue
        job_id, no = generation.get("job_id"), generation.get("draft_no")
        code = generation.get("item_code") or (item_code(job_id, no) if job_id and no else str(package.get("item_id")))
        text = _bank_text(package)
        intent = generation.get("design_intent") if isinstance(generation.get("design_intent"), Mapping) else {}
        # Saved content can be edited after confirmation. Stored generation-time
        # fingerprints are historical metadata, never the current duplicate gate.
        fp = fingerprint(
            text, concepts=list(generation.get("scope_concepts") or []) + list(generation.get("concepts") or []),
            fmt=generation.get("format"), figure=generation.get("figure"), purpose=intent.get("purpose"))
        topic = generation.get("topic") or {}
        scope = str(generation.get("scope_file") or "").removeprefix("scope/").removesuffix(".json") or None
        source = generation.get("source_item") if isinstance(generation.get("source_item"), Mapping) else {}
        out.append(_entry(key=f"{job_id}#{no}" if job_id and no else f"bank:{package.get('item_id')}", code=code,
                          origin="bank", scope=scope, unit=topic, text=text, fp=fp, item_id=package.get("item_id"),
                          kind=generation.get("kind"), source_item_id=source.get("item_id")))
    return out


def job_entries(workspace: Path, *, exclude_job: str | None = None) -> list[dict[str, Any]]:
    out = []
    for path in sorted((Path(workspace) / _JOBS_DIRNAME).glob("gen-*/job.json")):
        try:
            job = load_json_object(path)
        except (ModelError, OSError, ValueError):
            continue
        if job.get("job_id") == exclude_job or job.get("status") == "cancelled":
            continue
        for draft in job.get("drafts") or []:
            if draft.get("discarded") or draft.get('confirmed_item_id') or (draft.get("duplicate_check") or {}).get("status") == "duplicate":
                continue
            out.append(draft_entry(job, draft))
    return out


def build_index(workspace: Path | str, *, exclude_job: str | None = None) -> list[dict[str, Any]]:
    """Bank items first; a draft already stored in the bank is represented once."""

    bank = bank_entries(Path(workspace))
    seen = {e["key"] for e in bank}
    return bank + [e for e in job_entries(Path(workspace), exclude_job=exclude_job) if e["key"] not in seen]


def check(candidate: Mapping[str, Any], index: Iterable[Mapping[str, Any]], *, limit: int = 3) -> dict[str, Any]:
    """Compare one entry with the index; returns a JSON-safe result."""

    fp = candidate["fingerprint"]
    matches = []
    for other in index:
        if other["key"] == candidate["key"]:
            continue
        ofp = other["fingerprint"]
        text_score = _jaccard(candidate["grams"], other["grams"])
        reasons, level = [], None
        if fp["text_sha256"] == ofp.get("text_sha256"):
            reasons.append("본문이 같음")
            level = "duplicate"
        elif fp["skeleton_sha256"] == ofp.get("skeleton_sha256"):
            reasons.append("숫자만 다르고 본문이 같음")
            level = "duplicate"
        elif text_score >= DUPLICATE_TEXT:
            reasons.append(f"본문 겹침 {text_score:.2f}")
            level = "duplicate"
        elif text_score >= SIMILAR_TEXT:
            reasons.append(f"본문 겹침 {text_score:.2f}")
            level = "similar"
        same_unit = candidate.get("middle") and candidate.get("middle") == other.get("middle")
        if level is None and same_unit and fp.get("format") == ofp.get("format") \
                and fp.get("figure_type") == ofp.get("figure_type") \
                and _jaccard(set(fp.get("concepts") or []), set(ofp.get("concepts") or [])) >= SIMILAR_CONCEPTS \
                and _jaccard(candidate["purpose_grams"], other["purpose_grams"]) >= SIMILAR_PURPOSE:
            reasons.append("같은 단원·형식·개념·그림 유형·출제 목표")
            level = "similar"
        if level:
            matches.append({"code": other["code"], "origin": other["origin"], "item_id": other.get("item_id"),
                            "level": level, "text_score": round(text_score, 3), "reasons": reasons})
    matches.sort(key=lambda m: (m["level"] != "duplicate", -m["text_score"]))
    status = "duplicate" if any(m["level"] == "duplicate" for m in matches) else "similar" if matches else "unique"
    return {"status": status, "matches": matches[:limit], "compared": sum(1 for o in index if o["key"] != candidate["key"])}


def avoid_section(index: Iterable[Mapping[str, Any]], *, scope: Any, middle: Any, limit: int = 40) -> str:
    """Prompt text listing designs already used in the same unit, newest last."""

    rows = [e for e in index if e.get("scope") == scope and e.get("middle") == middle][-limit:]
    if not rows:
        return ""
    lines = ["", "[이미 만든 문항 — 아래와 같은 상황·자료·풀이 구조를 다시 쓰지 말 것. 숫자·이름만 바꾼 것도 중복이다]"]
    for e in rows:
        fp = e["fingerprint"]
        lines.append(f"- {e['code']} | {fp.get('format') or '-'} | 그림 {fp.get('figure_type') or '없음'} | "
                     f"개념 {', '.join(fp.get('concepts') or [])[:80]} | 목표 {str(fp.get('purpose') or '')[:80]} | "
                     f"본문 {' '.join(e['text'].split())[:90]}")
    return "\n".join(lines) + "\n"
