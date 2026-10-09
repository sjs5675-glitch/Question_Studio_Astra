"""Copy explicitly stored answer-role text without solving or changing its source."""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Mapping

from .model import stored_content_status

_GLYPHS = re.compile(r"[①-⑳](?:\s*,\s*[①-⑳])*")
_PREFIX = re.compile(r"^\s*(?:(?:정답|답)\s*:?\s*)?")


def explanation_scope_reason(document: Mapping[str, Any]) -> str | None:
    """Explicit cross-item reuse is unsafe even when its blocks say 'answer'."""
    identity = document.get("identity") or {}
    if identity.get("parent_item_id"):
        return "shared_explanation"
    provenance = document.get("provenance") or {}
    if provenance.get("shared_parent_mode") is True:
        return "shared_explanation"
    problem = provenance.get("problem") or {}
    if (problem.get("owner_question_id") and problem.get("unit_id")
            and problem["owner_question_id"] != problem["unit_id"]):
        return "other_item_scope"
    attempts = [record for record in provenance.get("transcriptions", [])
                if isinstance(record, Mapping) and record.get("section") == "explanation"
                and record.get("status") == "succeeded"]
    if attempts:
        latest = attempts[-1]
        if (latest.get("shared_parent_mode") is True or latest.get("reused_from_job_id")
                or latest.get("provider") == "shared-content-cache"):
            return "shared_explanation"
    return None


def _paragraph(document: Mapping[str, Any], source: Mapping[str, Any],
               inlines: list[dict[str, Any]]) -> dict[str, Any]:
    block = copy.deepcopy(dict(source))
    # Like normalizer.answer(), canonical answer paragraphs have no source_region;
    # the unchanged explanation paragraph retains the source asset and crop bounds.
    block.pop("source_region", None)
    seed = str(source.get("block_id", "")) + repr(inlines)
    base = "answer-promotion-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    # Block IDs are unique across question, answer, and explanation.
    existing: set[str] = set()
    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            if isinstance(value.get("block_id"), str):
                existing.add(value["block_id"])
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    collect(document.get("content"))
    block_id = base
    index = 1
    while block_id in existing:
        block_id = f"{base}-{index}"
        index += 1
    block.update(block_id=block_id, type="paragraph", role="answer",
                 inlines=copy.deepcopy(inlines), review_status="unreviewed")
    return block


def promote_answer_from_explanation(document: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Return a new canonical answer and reason; never mutate the document."""
    if stored_content_status(document)["has_answer"]:
        return None, "existing_answer"
    scope_reason = explanation_scope_reason(document)
    if scope_reason:
        return None, scope_reason
    content = document.get("content") or {}
    explanation = content.get("explanation") or {}
    blocks = [block for block in explanation.get("blocks", [])
              if isinstance(block, Mapping) and block.get("role") == "answer"]
    if not blocks:
        return None, "no_answer_block"
    source = blocks[0]
    if source.get("type") != "paragraph" or not isinstance(source.get("inlines"), list):
        return None, "unsupported_answer_block"
    inlines = source["inlines"]
    text = "".join(run.get("text", "") if run.get("type") == "text"
                   else "\n" if run.get("type") == "line_break" else "\ufffc"
                   for run in inlines)
    start = _PREFIX.match(text).end()
    glyphs = _GLYPHS.match(text, start)
    if glyphs:
        suffix = text[glyphs.end():]
        if suffix and not (suffix[0].isspace() or suffix[0] in "•·*|"):
            return None, "ambiguous_glyph"
        # Share the canonical normalizer's duplicate-label and ID rules.
        from .transcription import _collect_choice_ids
        choices = _collect_choice_ids(content.get("question"))
        labels = re.findall(r"[①-⑳]", glyphs.group())
        if all(label in choices and choices[label] not in {"None", ""} for label in labels):
            return {"kind": "choice", "choice_ids": list(dict.fromkeys(choices[label] for label in labels))}, "promoted_choice"
        literal = glyphs.group()
        return {"kind": "content", "blocks": [_paragraph(document, source, [
            {"type": "text", "text": literal, "source_text": literal}])]}, "promoted_literal"
    if not text.strip():
        return None, "empty_answer_block"
    return {"kind": "content", "blocks": [_paragraph(document, source, inlines)]}, "promoted_content"
