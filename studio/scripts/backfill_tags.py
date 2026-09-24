from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.model import load_json_object, update_tags_cas
from app.tagging import auto_tag_document, rule_tags
from astra_runtime import inside


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill question tags in one project")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rules-only", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)

    project_dir = inside(args.project_dir, "project_dir")
    project = load_json_object(project_dir / "project.json")
    item_ids = list(project.get("item_order", []))
    if args.limit is not None:
        item_ids = item_ids[: max(0, args.limit)]
    warnings = 0
    for index, item_id in enumerate(item_ids, start=1):
        item_path = project_dir / "items" / str(item_id) / "question.json"
        document = load_json_object(item_path)
        if args.rules_only:
            tags, warning = rule_tags(project, document), None
        else:
            tags, warning = auto_tag_document(project, document)
        if warning:
            warnings += 1
        if not args.dry_run:
            update_tags_cas(item_path, document["workflow"]["revision"], tags)
        number = document.get("identity", {}).get("printed_number", item_id)
        print(
            f"[{index}/{len(item_ids)}] {number}: {tags['unit']} · {tags['difficulty']}"
            f" · {tags['source']}{' · 확인 필요' if warning else ''}",
            flush=True,
        )
    print(json.dumps({"items": len(item_ids), "warnings": warnings, "dry_run": args.dry_run}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
