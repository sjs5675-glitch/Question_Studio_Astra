#!/usr/bin/env python3
"""Remove reprinted problem blocks from two-column explanation OCR sections."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from typing import Any, Mapping

from app.model import load_json_object, save_json_cas
from astra_runtime import inside


MARKERS = ("단서+발상", "문제+자료 분석", "보기 분석")


def block_text(value: Any) -> str:
    parts: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"text", "source_text"} and isinstance(child, str):
                parts.append(child)
            elif key not in {"block_id", "hwp_script"}:
                parts.append(block_text(child))
    elif isinstance(value, list):
        parts.extend(block_text(child) for child in value)
    return " ".join(part for part in parts if part)


def source_left(block: Mapping[str, Any]) -> float | None:
    bbox = (block.get("source_region") or {}).get("bbox")
    value = bbox.get("left") if isinstance(bbox, Mapping) else None
    return float(value) if isinstance(value, (int, float)) else None


def boundary_index(blocks: list[Mapping[str, Any]]) -> int | None:
    for index, block in enumerate(blocks):
        text = re.sub(r"\s+", "", block_text(block))
        if any(marker in text for marker in MARKERS):
            return index
    last_choices = max(
        (index for index, block in enumerate(blocks) if block.get("type") == "choices"),
        default=-1,
    )
    for index in range(last_choices + 1, len(blocks)):
        if blocks[index].get("role") in {"explanation", "answer", "note", "heading"}:
            return index
    return None


def filter_blocks(blocks: list[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    boundary = boundary_index(blocks)
    if boundary is None:
        return [dict(block) for block in blocks], []
    kept: list[dict[str, Any]] = []
    removed: list[str] = []
    for index, block in enumerate(blocks):
        keep = index >= boundary
        if index < boundary and block.get("type") == "paragraph":
            role = block.get("role")
            left = source_left(block)
            keep = role == "heading" or (
                left is not None
                and left >= 0.48
                and role in {"note", "explanation", "answer", "caption", "body"}
            )
        if keep:
            kept.append(dict(block))
        else:
            removed.append(str(block.get("block_id")))
    return kept, removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    project_path = inside(args.project, "project") if args.apply else args.project.resolve()
    project = load_json_object(project_path)
    report = []
    for item_id in project.get("item_order", []):
        item_path = project_path.parent / "items" / str(item_id) / "question.json"
        document = load_json_object(item_path)
        explanation = document.get("content", {}).get("explanation")
        if not isinstance(explanation, dict) or not isinstance(explanation.get("blocks"), list):
            continue
        filtered, removed = filter_blocks(explanation["blocks"])
        if not removed or not filtered:
            continue
        report.append(
            {
                "number": document.get("identity", {}).get("printed_number"),
                "before": len(explanation["blocks"]),
                "after": len(filtered),
                "removed": removed,
            }
        )
        if args.apply:
            explanation["blocks"] = filtered
            document["review"]["issues"].append(
                f"Filtered {len(removed)} likely reprinted-problem blocks from explanation; review source layout"
            )
            document["review"]["issues"] = list(dict.fromkeys(document["review"]["issues"]))
            save_json_cas(item_path, document, document["workflow"]["revision"])
    print(json.dumps({"apply": args.apply, "items": report}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
