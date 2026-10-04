from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from app.model import RevisionConflict, atomic_write_json, figure_generation_summary, load_json_object
from app.question_bank import (
    QUESTION_SET_SCHEMA,
    QuestionBankError,
    create_question_set,
    get_question_set,
    list_bank_items,
    list_question_sets,
    patch_question_set,
    search_bank_items,
)


def make_project(workspace: Path, project_id: str, items: list[tuple[str, int, str]]) -> None:
    project_dir = workspace / project_id
    atomic_write_json(
        project_dir / "project.json",
        {
            "project_id": project_id,
            "metadata": {"book_title": f"교재 {project_id}"},
            "item_order": [item_id for item_id, _, _ in items[:-1]],
        },
    )
    for item_id, revision, text in items:
        atomic_write_json(
            project_dir / "items" / item_id / "question.json",
            {
                "schema_version": "0.1.0",
                "item_id": item_id,
                "workflow": {"status": "content_ready", "revision": revision},
                "identity": {"printed_number": item_id.upper()},
                "tags": {
                    "subject": "물리학",
                    "unit": "빛과 물질" if item_id.endswith("2") else "힘과 에너지",
                    "difficulty": "standard",
                    "problem_types": ["calculation"],
                    "keywords": [text],
                    "features": {"has_figure": item_id.endswith("2"), "has_graph": False},
                },
                "provenance": {
                    "problem": {
                        "original_filename": "physics.pdf",
                        "source_pages": [{"pdf_page_index": revision}],
                    },
                    "solution_links": [],
                },
                "content": {
                    "question": {
                        "blocks": [
                            {
                                "type": "paragraph",
                                "inlines": [{"type": "text", "text": text}],
                            }
                        ]
                    },
                    "answer": {"kind": "text", "text": "정답"},
                    "explanation": {"blocks": [{"type": "paragraph", "inlines": []}]},
                },
                "resources": {"assets": []},
                "review": {"issues": []},
            },
        )


class QuestionBankTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "workspace"
        make_project(
            self.workspace,
            "project-one",
            [("item-1", 3, "등가속도 운동"), ("item-2", 7, "광전 효과 그래프")],
        )
        make_project(self.workspace, "project-two", [("item-3", 2, "전자기 유도")])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_bank_scans_every_item_and_searches_summary(self) -> None:
        items = list_bank_items(self.workspace)

        self.assertEqual(3, len(items))
        extra = next(item for item in items if item["item_id"] == "item-2")
        self.assertEqual({"project_id": "project-one", "item_id": "item-2"}, extra["ref"])
        self.assertEqual("광전 효과 그래프", extra["preview"]["text"])
        self.assertEqual("physics.pdf", extra["source"]["original_filename"])
        self.assertIsNone(extra["updated_at"])
        self.assertEqual("빛과 물질", extra["tags"]["unit"])
        self.assertTrue(extra["status"]["answer_saved"])
        self.assertTrue(extra["status"]["explanation_saved"])
        self.assertEqual(64, len(extra["content_fingerprint_sha256"]))
        self.assertIsNone(extra["duplicate_warning"])
        package = load_json_object(
            self.workspace / "project-one" / "items" / "item-2" / "question.json"
        )
        self.assertEqual(
            figure_generation_summary(package), extra["figure_generation"]
        )
        self.assertEqual(
            ["item-2"],
            [
                item["item_id"]
                for item in search_bank_items(
                    self.workspace,
                    query="광전 그래프",
                    filters={"unit": "빛과 물질", "has_figure": True},
                )
            ],
        )

    def test_bank_preview_uses_only_ready_question_crops_in_order(self) -> None:
        path = self.workspace / "project-one" / "items" / "item-2" / "question.json"
        package = load_json_object(path)
        package["resources"]["assets"] = [
            {"asset_id": "first", "role": "source_crop", "section": "question", "state": "ready", "width_px": 100, "height_px": 400},
            {"asset_id": "explanation", "role": "source_crop", "section": "explanation", "state": "ready"},
            {"asset_id": "pending", "role": "source_crop", "section": "question", "state": "missing"},
            {"asset_id": "second", "role": "source_crop", "section": "question", "state": "ready", "width_px": 200, "height_px": 300},
        ]
        package["workflow"]["updated_at"] = "2026-09-24T00:00:00Z"
        atomic_write_json(path, package)
        item = next(value for value in list_bank_items(self.workspace, lightweight=True) if value["item_id"] == "item-2")
        self.assertEqual(["first", "second"], [image["asset_id"] for image in item["preview"]["images"]])
        self.assertEqual({"asset_id": "first", "width_px": 100, "height_px": 400}, item["preview"]["images"][0])
        self.assertEqual("not_checked", item["duplicate_similarity_status"])
        self.assertEqual("not_checked", item["status"]["solution_link"])
        self.assertEqual("2026-09-24T00:00:00Z", item["updated_at"])

    def test_lightweight_bank_updates_after_source_change_without_sharing_results(self) -> None:
        first = list_bank_items(self.workspace, lightweight=True)
        before = next(item for item in first if item["item_id"] == "item-1")
        before["preview"]["text"] = "caller mutation"
        repeated = next(item for item in list_bank_items(self.workspace, lightweight=True) if item["item_id"] == "item-1")
        self.assertNotEqual("caller mutation", repeated["preview"]["text"])

        path = self.workspace / "project-one" / "items" / "item-1" / "question.json"
        package = load_json_object(path)
        package["workflow"]["revision"] += 1
        package["content"]["question"]["blocks"][0]["inlines"][0]["text"] = "새 문제 본문"
        atomic_write_json(path, package)
        updated = next(item for item in list_bank_items(self.workspace, lightweight=True) if item["item_id"] == "item-1")
        self.assertEqual(4, updated["revision"])
        self.assertEqual("새 문제 본문", updated["preview"]["text"])

        project_path = self.workspace / "project-one" / "project.json"
        project = load_json_object(project_path)
        project["metadata"]["book_title"] = "새 출처 이름"
        atomic_write_json(project_path, project)
        renamed = next(item for item in list_bank_items(self.workspace, lightweight=True) if item["item_id"] == "item-1")
        self.assertEqual("새 출처 이름", renamed["source"]["project_title"])

        path.unlink()
        self.assertNotIn("item-1", {item["item_id"] for item in list_bank_items(self.workspace, lightweight=True)})

    def test_create_patch_reorder_and_resolve_revisions(self) -> None:
        created = create_question_set(self.workspace, title="복습", set_id="set-review")
        self.assertEqual("separate", created["output_mode"])
        self.assertEqual(
            {"template_ref": None, "design_profile_ref": None}, created["rendering"]
        )
        saved = patch_question_set(
            self.workspace,
            "set-review",
            1,
            {
                "items": [
                    {"project_id": "project-one", "item_id": "item-1"},
                    {"project_id": "project-two", "item_id": "item-3"},
                ],
                "output_mode": "combined_end_section",
                "rendering": {"template_ref": "templates/later.hwpx"},
            },
        )
        self.assertEqual([3, 2], [item["source_revision"] for item in saved["items"]])
        self.assertEqual("templates/later.hwpx", saved["rendering"]["template_ref"])

        item_path = self.workspace / "project-one" / "items" / "item-1" / "question.json"
        changed = json.loads(item_path.read_text(encoding="utf-8"))
        changed["workflow"]["revision"] = 4
        atomic_write_json(item_path, changed)

        resolved = get_question_set(self.workspace, "set-review")
        self.assertEqual(["updated", "current"], [item["revision_status"] for item in resolved["resolved_items"]])
        self.assertEqual([4, 2], [item["current_revision"] for item in resolved["resolved_items"]])

        reordered = patch_question_set(
            self.workspace,
            "set-review",
            2,
            {
                "items": [
                    {"project_id": "project-two", "item_id": "item-3"},
                    {"project_id": "project-one", "item_id": "item-1"},
                ]
            },
        )
        self.assertEqual(["item-3", "item-1"], [item["item_id"] for item in reordered["items"]])
        self.assertEqual([2, 3], [item["source_revision"] for item in reordered["items"]])
        self.assertEqual(1, len(list_question_sets(self.workspace)))

        with self.assertRaises(RevisionConflict):
            patch_question_set(self.workspace, "set-review", 2, {"title": "오래된 저장"})

    def test_duplicate_missing_and_unsafe_values_are_explicit(self) -> None:
        create_question_set(self.workspace, set_id="set-safe")
        duplicate = [
            {"project_id": "project-one", "item_id": "item-1"},
            {"project_id": "project-one", "item_id": "item-1"},
        ]
        with self.assertRaisesRegex(QuestionBankError, "duplicate"):
            patch_question_set(self.workspace, "set-safe", 1, {"items": duplicate})
        self.assertEqual(1, get_question_set(self.workspace, "set-safe", resolve_items=False)["revision"])
        with self.assertRaises(QuestionBankError):
            create_question_set(self.workspace, set_id="../escape")
        with self.assertRaisesRegex(QuestionBankError, "safe relative path"):
            patch_question_set(
                self.workspace,
                "set-safe",
                1,
                {"rendering": {"template_ref": "../outside.hwpx"}},
            )

        saved = patch_question_set(
            self.workspace,
            "set-safe",
            1,
            {"items": [{"project_id": "project-one", "item_id": "item-1"}]},
        )
        (self.workspace / "project-one" / "items" / "item-1" / "question.json").unlink()
        missing = get_question_set(self.workspace, "set-safe")["resolved_items"][0]
        self.assertEqual("missing", missing["revision_status"])
        self.assertIsNone(missing["current_revision"])
        self.assertEqual(2, saved["revision"])

    def test_subquestions_are_grouped_and_saved_as_one_ordered_unit(self) -> None:
        for item_id, printed in (("item-1", "H19(1)"), ("item-2", "H19(2)")):
            path = self.workspace / "project-one" / "items" / item_id / "question.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["identity"].update(
                printed_number=printed,
                unit_kind="subquestion",
                parent_item_id=None,
                group_id=None,
            )
            atomic_write_json(path, document)

        summaries = {item["item_id"]: item for item in list_bank_items(self.workspace)}
        first_dependency = summaries["item-1"]["dependency"]
        self.assertEqual("H19", first_dependency["group_label"])
        self.assertEqual(1, first_dependency["sub_index"])
        self.assertEqual(2, first_dependency["group_size"])
        self.assertTrue(first_dependency["shared_context"])
        self.assertEqual(
            first_dependency["group_key"], summaries["item-2"]["dependency"]["group_key"]
        )

        create_question_set(self.workspace, set_id="set-h19")
        single = patch_question_set(
            self.workspace,
            "set-h19",
            1,
            {"items": [{"project_id": "project-one", "item_id": "item-2"}]},
        )
        self.assertEqual(["item-2"], [item["item_id"] for item in single["items"]])

        both = patch_question_set(
            self.workspace,
            "set-h19",
            2,
            {
                "items": [
                    {"project_id": "project-one", "item_id": "item-2"},
                    {"project_id": "project-two", "item_id": "item-3"},
                    {"project_id": "project-one", "item_id": "item-1"},
                ]
            },
        )
        self.assertEqual(
            ["item-1", "item-2", "item-3"], [item["item_id"] for item in both["items"]]
        )
        resolved = get_question_set(self.workspace, "set-h19")["resolved_items"]
        self.assertEqual([1, 2], [item["summary"]["dependency"]["sub_index"] for item in resolved[:2]])
        self.assertEqual([2, 2], [item["summary"]["dependency"]["group_size"] for item in resolved[:2]])
        self.assertNotIn("_duplicate_text", resolved[0]["summary"])

        group_after_ordinary = patch_question_set(
            self.workspace,
            "set-h19",
            3,
            {
                "items": [
                    {"project_id": "project-two", "item_id": "item-3"},
                    {"project_id": "project-one", "item_id": "item-2"},
                    {"project_id": "project-one", "item_id": "item-1"},
                ]
            },
        )
        self.assertEqual(
            ["item-3", "item-1", "item-2"],
            [item["item_id"] for item in group_after_ordinary["items"]],
        )

    def test_same_printed_subnumber_with_different_source_owners_is_not_one_group(self) -> None:
        for item_id, owner in (("item-1", "p0001-q1"), ("item-2", "p0001-q5")):
            path = self.workspace / "project-one" / "items" / item_id / "question.json"
            document = load_json_object(path)
            document["identity"].update(printed_number="01(1)", unit_kind="subquestion")
            document["provenance"]["problem"].update(
                owner_question_id=owner,
                unit_id=f"{owner}-sub-1",
                source_pages=[{"pdf_page_index": 0}],
            )
            atomic_write_json(path, document)
        summaries = {item["item_id"]: item for item in list_bank_items(self.workspace, lightweight=True)}
        self.assertNotEqual(summaries["item-1"]["dependency"]["group_key"], summaries["item-2"]["dependency"]["group_key"])
        self.assertEqual([1, 1], [summaries[item_id]["dependency"]["group_size"] for item_id in ("item-1", "item-2")])

    def test_duplicate_warnings_are_advisory_and_skip_siblings(self) -> None:
        exact_text = "마찰이 없는 수평면에서 물체의 가속도와 힘의 관계를 설명한 것으로 옳은 것은 무엇인가"
        similar_text = exact_text + "?"
        make_project(self.workspace, "project-three", [("item-4", 1, exact_text)])
        make_project(self.workspace, "project-four", [("item-5", 1, exact_text)])
        make_project(self.workspace, "project-five", [("item-6", 1, similar_text)])

        summaries = {item["item_id"]: item for item in list_bank_items(self.workspace)}
        warning = summaries["item-4"]["duplicate_warning"]
        self.assertEqual("exact", warning["kind"])
        self.assertEqual(
            {"item-5", "item-6"}, {match["ref"]["item_id"] for match in warning["matches"]}
        )
        self.assertEqual("similar", summaries["item-6"]["duplicate_warning"]["matches"][0]["kind"])
        self.assertEqual(6, len(summaries))

        for item_id, printed in (("item-4", "H19(1)"), ("item-5", "H19(2)")):
            project_id = "project-three" if item_id == "item-4" else "project-four"
            path = self.workspace / project_id / "items" / item_id / "question.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["identity"].update(printed_number=printed, unit_kind="subquestion")
            atomic_write_json(path, document)
        # Printed-number fallback is intentionally project-scoped; equal labels
        # from different books remain duplicate candidates, not siblings.
        summaries = {item["item_id"]: item for item in list_bank_items(self.workspace)}
        self.assertIsNotNone(summaries["item-4"]["duplicate_warning"])

        sibling_text = "공통 그림을 보고 자석 사이에 작용하는 힘을 서술하시오"
        make_project(
            self.workspace,
            "project-siblings",
            [("item-7", 1, sibling_text), ("item-8", 1, sibling_text)],
        )
        for item_id, printed in (("item-7", "H19(1)"), ("item-8", "H19(2)")):
            path = self.workspace / "project-siblings" / "items" / item_id / "question.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["identity"].update(printed_number=printed, unit_kind="subquestion")
            atomic_write_json(path, document)
        summaries = {item["item_id"]: item for item in list_bank_items(self.workspace)}
        self.assertIsNone(summaries["item-7"]["duplicate_warning"])
        self.assertIsNone(summaries["item-8"]["duplicate_warning"])

    def test_written_document_matches_schema(self) -> None:
        document = create_question_set(self.workspace, set_id="set-schema")
        try:
            from jsonschema import Draft202012Validator, FormatChecker
        except ImportError:  # pragma: no cover - project requirements install jsonschema
            self.skipTest("jsonschema is unavailable")
        schema = json.loads(QUESTION_SET_SCHEMA.read_text(encoding="utf-8"))
        errors = list(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document)
        )
        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
