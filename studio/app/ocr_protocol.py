"""Vision-model OCR output parsing and normalization.

Adapted from PNKmath/exam-studio commit
bd97d15ed67e325692455b60c483d5e592d0d1c4:
`studio/server/stages/equationSentinel.ts`,
`studio/server/stages/modelHarness.ts`, and
`studio/server/stages/extractor.ts#sanitizeExtractedChoices`.

The solver and generated-explanation pipeline are intentionally excluded.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any


EQ_SENTINEL_OPEN = "<<EQ>>"
EQ_SENTINEL_CLOSE = "<</EQ>>"
_EQ_SPAN = re.compile(r"<<EQ>>([\s\S]*?)<</EQ>>")
_EQ_TOKEN = re.compile(r"<<EQ>>|<</EQ>>")
_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_CHOICE_PREFIX = re.compile(r"^[①②③④⑤]\s*")


class ModelJsonError(ValueError):
    pass


def strip_equation_sentinels(raw: str) -> str:
    return _EQ_TOKEN.sub("", raw)


def escape_equation_sentinels(raw: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return json.dumps(match.group(1), ensure_ascii=False)[1:-1]

    return _EQ_SPAN.sub(replace, raw)


def sentinel_candidates(raw: str) -> list[str]:
    has_odd_command_slash = False
    for span in _EQ_SPAN.finditer(raw):
        for match in re.finditer(r"(\\+)[A-Za-z]", span.group(1)):
            if len(match.group(1)) % 2 == 1:
                has_odd_command_slash = True
                break
        if has_odd_command_slash:
            break
    stripped = strip_equation_sentinels(raw)
    escaped = escape_equation_sentinels(raw)
    return [escaped, stripped] if has_odd_command_slash else [stripped, escaped]


def _balanced_json(value: str) -> str | None:
    starts = [position for position in (value.find("{"), value.find("[")) if position >= 0]
    if not starts:
        return None
    start = min(starts)
    opening = value[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(value)):
        char = value[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return value[start : index + 1]
    return None


def _json_candidates(raw: str) -> Iterator[str]:
    trimmed = raw.strip()
    if trimmed.startswith(("{", "[")):
        yield trimmed
    for match in _FENCE.finditer(raw):
        candidate = match.group(1).strip()
        if candidate:
            yield candidate
    balanced = _balanced_json(trimmed)
    if balanced:
        yield balanced


def parse_model_json(raw: str) -> Any:
    for transformed in sentinel_candidates(raw):
        for candidate in _json_candidates(transformed):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    raise ModelJsonError("model output did not contain valid JSON")


def sanitize_choice_prefixes(candidate: dict[str, Any]) -> dict[str, Any]:
    """Remove a duplicated ①..⑤ prefix from candidate choice text runs."""
    blocks = candidate.get("blocks")
    if not isinstance(blocks, list):
        return candidate
    changed = False
    normalized_blocks: list[Any] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "choices":
            normalized_blocks.append(block)
            continue
        block_copy = dict(block)
        items = block.get("items")
        if not isinstance(items, list):
            normalized_blocks.append(block)
            continue
        new_items = []
        for item in items:
            if not isinstance(item, dict):
                new_items.append(item)
                continue
            item_copy = dict(item)
            runs = item.get("runs")
            if isinstance(runs, list) and runs and isinstance(runs[0], dict):
                first = runs[0]
                if first.get("type") == "text" and isinstance(first.get("text"), str):
                    stripped = _CHOICE_PREFIX.sub("", first["text"])
                    if stripped != first["text"]:
                        new_runs = [dict(first, text=stripped), *runs[1:]]
                        if stripped == "":
                            new_runs = new_runs[1:]
                        item_copy["runs"] = new_runs
                        changed = True
            new_items.append(item_copy)
        block_copy["items"] = new_items
        normalized_blocks.append(block_copy)
    return dict(candidate, blocks=normalized_blocks) if changed else candidate

