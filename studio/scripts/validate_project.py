#!/usr/bin/env python3
"""Question JSON Studio project와 item/group/export 교차 계약을 검사한다."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from validate_package import Validator as PackageValidator
from validate_package import json_schema_errors


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT_SCHEMA = ROOT / "schemas" / "project.schema.json"
DEFAULT_ITEM_SCHEMA = ROOT / "schemas" / "question-package.schema.json"
SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")


class ProjectValidator:
    def __init__(
        self,
        project_path: Path,
        items_root: Path | None = None,
        *,
        check_items: bool = False,
        check_item_schemas: bool = False,
        item_schema: Path = DEFAULT_ITEM_SCHEMA,
    ) -> None:
        self.project_path = project_path.resolve()
        self.project_root = self.project_path.parent
        self.items_root = (items_root or self.project_root / "items").resolve()
        self.check_items = check_items
        self.check_item_schemas = check_item_schemas
        self.item_schema = item_schema
        self.errors: list[str] = []

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def load_items(self, item_order: list[str], required: bool) -> dict[str, dict[str, Any]]:
        loaded: dict[str, dict[str, Any]] = {}
        if not required and not self.items_root.is_dir():
            return loaded
        for item_id in item_order:
            item_path = self.items_root / item_id / "question.json"
            if not item_path.is_file():
                self.error(f"item_order[{item_id}]", f"item 파일 없음: {item_path}")
                continue
            try:
                item = json.loads(item_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self.error(f"item_order[{item_id}]", f"item JSON 읽기 실패: {exc}")
                continue
            if not isinstance(item, dict):
                self.error(f"item_order[{item_id}]", "question.json 최상위는 객체여야 함")
                continue
            if item.get("item_id") != item_id:
                self.error(f"item_order[{item_id}]", "폴더 ID와 question.item_id가 다름")
            if self.check_item_schemas:
                item_schema_errors = json_schema_errors(item, self.item_schema)
                for error in item_schema_errors:
                    self.error(f"item[{item_id}]", error)
                if item_schema_errors:
                    continue
            loaded[item_id] = item
            package_errors = PackageValidator(item_path, check_files=False).validate(item)
            for error in package_errors:
                self.error(f"item[{item_id}]", error)
        return loaded

    def validate(self, data: Any) -> list[str]:
        self.errors = []
        if not isinstance(data, dict):
            return ["root: JSON 객체가 필요함"]
        workflow = data.get("workflow")
        status = workflow.get("status") if isinstance(workflow, dict) else None
        item_order_value = data.get("item_order")
        if not isinstance(item_order_value, list):
            self.error("item_order", "배열이 필요함")
            item_order: list[str] = []
        else:
            item_order = [item for item in item_order_value if isinstance(item, str)]
            if len(item_order) != len(item_order_value):
                self.error("item_order", "모든 값이 ID 문자열이어야 함")
        item_ids = set(item_order)

        groups = data.get("groups")
        seen_groups: set[str] = set()
        member_owner: dict[str, str] = {}
        group_records: list[dict[str, Any]] = []
        if not isinstance(groups, list):
            self.error("groups", "배열이 필요함")
            groups = []
        for index, group in enumerate(groups):
            where = f"groups[{index}]"
            if not isinstance(group, dict):
                self.error(where, "group 객체가 필요함")
                continue
            group_records.append(group)
            group_id = group.get("group_id")
            if isinstance(group_id, str):
                if group_id in seen_groups:
                    self.error(f"{where}.group_id", "group ID 중복")
                seen_groups.add(group_id)
            shared = group.get("shared_item_id")
            if shared is not None and shared not in item_ids:
                self.error(f"{where}.shared_item_id", "item_order에 없는 item")
            members = group.get("member_item_ids")
            if not isinstance(members, list):
                self.error(f"{where}.member_item_ids", "배열이 필요함")
                continue
            if shared in members:
                self.error(where, "shared item은 member 목록에 동시에 있을 수 없음")
            for member in members:
                if member not in item_ids:
                    self.error(f"{where}.member_item_ids", f"item_order에 없는 item={member!r}")
                    continue
                previous = member_owner.get(member)
                if previous is not None and previous != group_id:
                    self.error(f"{where}.member_item_ids", f"item={member}이 여러 group에 속함")
                elif isinstance(group_id, str):
                    member_owner[member] = group_id

        need_items = self.check_items or status in {"content_ready", "export_ready"}
        loaded = self.load_items(item_order, need_items)

        if status == "content_ready":
            for item_id in item_order:
                item = loaded.get(item_id)
                item_workflow = item.get("workflow") if isinstance(item, dict) else None
                if not isinstance(item_workflow, dict) or item_workflow.get("status") != "content_ready":
                    self.error(
                        f"item[{item_id}].workflow.status",
                        "project content_ready에는 content_ready item만 허용",
                    )

        if status == "export_ready":
            metadata = data.get("metadata")
            required_metadata = (
                "year",
                "semester",
                "exam_type",
                "school",
                "school_level",
                "grade",
                "subject",
                "filename_base",
            )
            if not isinstance(metadata, dict):
                self.error("metadata", "export_ready project에는 metadata 객체가 필요함")
            else:
                for key in required_metadata:
                    if not isinstance(metadata.get(key), str) or not metadata[key].strip():
                        self.error(f"metadata.{key}", "ready/export 전에 값을 확정해야 함")

            rendering = data.get("rendering")
            for key in ("question_design_profile_ref", "explanation_design_profile_ref"):
                design_ref = rendering.get(key) if isinstance(rendering, dict) else None
                if not isinstance(design_ref, str) or not design_ref:
                    self.error(f"rendering.{key}", "export_ready에는 승인된 design profile이 필요함")
                    continue
                pure = PurePosixPath(design_ref)
                design_path = (self.project_root / Path(*pure.parts)).resolve()
                try:
                    design_path.relative_to(self.project_root)
                except ValueError:
                    self.error(f"rendering.{key}", "project 밖 경로")
                else:
                    if not design_path.is_file():
                        self.error(f"rendering.{key}", f"파일 없음: {design_path}")

            export = data.get("export")
            if not isinstance(export, dict):
                self.error("export", "export_ready project에는 export 객체가 필요함")
                export = {}
            if not isinstance(export.get("engine_id"), str) or not export["engine_id"].strip():
                self.error("export.engine_id", "고정된 local engine ID가 필요함")
            fingerprint = export.get("engine_fingerprint_sha256")
            if not isinstance(fingerprint, str) or not SHA256_RE.fullmatch(fingerprint):
                self.error("export.engine_fingerprint_sha256", "고정된 engine fingerprint가 필요함")

            for item_id in item_order:
                item = loaded.get(item_id)
                if not item:
                    continue
                if item.get("workflow", {}).get("status") != "content_ready":
                    self.error(f"item[{item_id}].workflow.status", "project export_ready에는 content_ready item만 허용")
                identity = item.get("identity") if isinstance(item.get("identity"), dict) else {}
                parent_id = identity.get("parent_item_id")
                if parent_id == item_id:
                    self.error(f"item[{item_id}].identity.parent_item_id", "자기 자신을 parent로 참조할 수 없음")
                elif parent_id is not None and parent_id not in item_ids:
                    self.error(f"item[{item_id}].identity.parent_item_id", "item_order에 없는 parent")
                group_id = identity.get("group_id")
                if group_id is not None:
                    if group_id not in seen_groups:
                        self.error(f"item[{item_id}].identity.group_id", "project.groups에 없는 group")
                    elif member_owner.get(item_id) != group_id and not any(
                        group.get("group_id") == group_id
                        and group.get("shared_item_id") == item_id
                        for group in group_records
                    ):
                        self.error(f"item[{item_id}].identity.group_id", "해당 group의 member/shared 목록에 없음")
                content = item.get("content") if isinstance(item.get("content"), dict) else {}
                review = item.get("review") if isinstance(item.get("review"), dict) else {}
                unit_kind = identity.get("unit_kind")
                content_bearing = unit_kind not in {"group", "shared_group_bundle"}
                if export.get("include_answers") and content_bearing:
                    answer = content.get("answer")
                    if answer is None or (isinstance(answer, dict) and answer.get("kind") == "unknown"):
                        self.error(f"item[{item_id}].content.answer", "답 포함 export에는 확정 answer가 필요함")
                    if review.get("answer") != "approved":
                        self.error(f"item[{item_id}].review.answer", "답 포함 export에는 승인이 필요함")
                if export.get("include_explanations") and content_bearing:
                    if content.get("explanation") is None:
                        self.error(f"item[{item_id}].content.explanation", "해설 포함 export에는 explanation이 필요함")
                    if review.get("explanation") != "approved":
                        self.error(f"item[{item_id}].review.explanation", "해설 포함 export에는 승인이 필요함")

            for index, group in enumerate(group_records):
                group_id = group.get("group_id")
                related = list(group.get("member_item_ids") or [])
                if group.get("shared_item_id") is not None:
                    related.append(group["shared_item_id"])
                for item_id in related:
                    item = loaded.get(item_id)
                    identity = item.get("identity") if isinstance(item, dict) else None
                    if isinstance(identity, dict) and identity.get("group_id") != group_id:
                        self.error(f"groups[{index}]", f"item={item_id}의 identity.group_id가 다름")
        return self.errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--schema", type=Path, default=DEFAULT_PROJECT_SCHEMA)
    parser.add_argument("--item-schema", type=Path, default=DEFAULT_ITEM_SCHEMA)
    parser.add_argument("--items-root", type=Path)
    parser.add_argument("--check-items", action="store_true")
    parser.add_argument("--semantic-only", action="store_true")
    args = parser.parse_args()

    try:
        data = json.loads(args.project.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    if not args.semantic_only:
        try:
            schema_errors = json_schema_errors(data, args.schema)
        except (OSError, json.JSONDecodeError, RuntimeError) as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 2
        if schema_errors:
            print(f"FAIL: {len(schema_errors)}개 오류", file=sys.stderr)
            for error in schema_errors:
                print(f"- {error}", file=sys.stderr)
            return 1

    validator = ProjectValidator(
        args.project,
        args.items_root,
        check_items=args.check_items,
        check_item_schemas=not args.semantic_only,
        item_schema=args.item_schema,
    )
    errors = validator.validate(data)
    if errors:
        print(f"FAIL: {len(errors)}개 오류", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"OK: {args.project.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
