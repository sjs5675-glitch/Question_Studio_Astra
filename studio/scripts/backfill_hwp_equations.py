#!/usr/bin/env python3
"""Add HWP equation scripts to already-transcribed project items."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from typing import Any

from app.equation_bridge import EquationBridgeError, convert_latex, science_warnings
from app.model import load_json_object, save_json_cas
from astra_runtime import inside


def fill(value: Any, issues: list[str], location: str = "content") -> tuple[int, int]:
    converted_count = 0
    error_count = 0
    if isinstance(value, dict):
        if value.get("type") == "equation":
            latex = value.get("latex")
            if not value.get("hwp_script") and isinstance(latex, str) and latex.strip():
                try:
                    converted = convert_latex(latex)
                except EquationBridgeError as exc:
                    issues.append(f"HWP equation conversion failed at {location}: {exc}")
                    error_count += 1
                else:
                    value["hwp_script"] = converted.hwp_script
                    converted_count += 1
            if isinstance(latex, str):
                issues.extend(
                    f"HWP equation review at {location}: {warning}"
                    for warning in science_warnings(latex)
                )
        for key, child in value.items():
            if key != "hwp_script":
                added, failed = fill(child, issues, f"{location}.{key}")
                converted_count += added
                error_count += failed
    elif isinstance(value, list):
        for index, child in enumerate(value):
            added, failed = fill(child, issues, f"{location}[{index}]")
            converted_count += added
            error_count += failed
    return converted_count, error_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    args = parser.parse_args(argv)
    project_path = inside(args.project, "project")
    project = load_json_object(project_path)
    total_converted = 0
    total_errors = 0
    changed_items = 0
    for item_id in project.get("item_order", []):
        item_path = project_path.parent / "items" / str(item_id) / "question.json"
        document = load_json_object(item_path)
        issues = document.get("review", {}).get("issues")
        if not isinstance(issues, list):
            continue
        issue_count_before = len(issues)
        converted, errors = fill(document.get("content"), issues)
        total_converted += converted
        total_errors += errors
        if converted or errors or len(issues) != issue_count_before:
            document["review"]["issues"] = list(dict.fromkeys(issues))
            save_json_cas(item_path, document, document["workflow"]["revision"])
            changed_items += 1
    print(
        json.dumps(
            {
                "changed_items": changed_items,
                "converted_equations": total_converted,
                "conversion_errors": total_errors,
            },
            ensure_ascii=False,
        )
    )
    return 1 if total_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
