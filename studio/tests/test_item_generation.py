"""AI item generation: draft checks, pipeline (fake models), bank storage and HWPX export."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image

from app import item_generation as ig
from app import item_generation_pipeline as pipeline
from app.item_generation_bot import BotChats
from app.question_bank import list_bank_items, search_bank_items

MAJOR = "Ⅰ-2. 화학 변화"
MIDDLE = "1. 산화와 환원"
CONCEPTS = ["산소의 이동과 산화·환원", "전자의 이동과 산화·환원"]
SOURCE_ID = "11111111-1111-5111-8111-111111111111"
SOURCE_PROJECT = "project-11111111-1111-5111-8111-111111111111"


def source_blocks(text: str) -> list[dict]:
    return [{"type": "paragraph", "block_id": "p1", "role": "body", "inlines": [{"type": "text", "text": text}]}]


def draft(kind: str = "new", **overrides) -> dict:
    base = {
        "kind": kind,
        "source_item_id": SOURCE_ID if kind == "variant" else None,
        "source_printed_number": "3" if kind == "variant" else None,
        "difficulty": "중",
        "format": "보기(ㄱㄴㄷ)형",
        "scope_concepts": [CONCEPTS[1]],
        "concepts": ["전자 이동"],
        "stem": "구리판을 질산 은 수용액에 넣었더니 구리판 표면에 은이 석출되었다.",
        "table": {"columns": ["시간(분)", "질량(g)"], "rows": [["0", "10.0"], ["5", "11.5"]]},
        "figure": None,
        "instruction": "이에 대한 설명으로 옳은 것만을 <보기>에서 있는 대로 고른 것은?",
        "bogi": {"ㄱ": "구리는 산화된다.", "ㄴ": "은 이온은 전자를 잃는다.", "ㄷ": "용액은 점점 푸른색을 띤다."},
        "choices": {"①": "ㄱ", "②": "ㄴ", "③": "ㄱ, ㄷ", "④": "ㄴ, ㄷ", "⑤": "ㄱ, ㄴ, ㄷ"},
        "answer": "③",
        "solution": "구리는 전자를 잃어 산화되고 은 이온은 전자를 얻어 환원된다.",
        "design_intent": {"purpose": "전자 이동으로 산화·환원을 판단한다.", "assessed_ability": "자료 해석",
                          "structure_rationale": "보기형", "misconceptions_targeted": ["전자를 잃는 쪽 혼동"],
                          "difficulty_design": "중", "basis": "패턴"},
        "distractor_intents": {"ㄴ": "전자 이동 방향 혼동"},
        "unique_answer_rationale": "ㄱ, ㄷ만 옳다.",
        "calc_checks": [{"expression": "11.5-10.0", "expected": 1.5, "meaning": "질량 증가"}],
    }
    base.update(overrides)
    return base


class EffortPolicyTests(unittest.TestCase):
    def test_exact_high_school_subjects_use_max_verification(self) -> None:
        for scope in ("고2-물리학", "고2-역학과 에너지", "고3-물리학"):
            with self.subTest(scope=scope):
                self.assertEqual(ig.codex_effort_for_scope(scope), "max")
                self.assertEqual(ig.claude_effort_for_scope(scope), "max")

    def test_non_target_scopes_keep_existing_efforts(self) -> None:
        cases = {
            "고2-전자기와 양자": "xhigh", "고2-통합과학2": "xhigh",
            "고3-화학": "xhigh", "고2-물리학 심화": "xhigh",
            "고2-응용 물리학": "xhigh", "고2-역학과 에너지 확장": "xhigh",
            "고2-물리학-추가": "xhigh", "고2-물리학\n": "xhigh",
            "고1-물리학": "high", "고1-역학과 에너지": "high",
            "중3-물리학": "high", "중2-과학": "high", "고1-통합과학2": "high",
            "물리학": "xhigh", "미상-물리학": "xhigh", "": "xhigh",
            "고2물리학": "xhigh",
        }
        for scope, codex_effort in cases.items():
            with self.subTest(scope=scope):
                self.assertEqual(ig.claude_effort_for_scope(scope), "high")
                self.assertEqual(ig.codex_effort_for_scope(scope), codex_effort)
        self.assertEqual(pipeline.GENERATE_EFFORT, "high")

    def test_ui_estimate_uses_returned_efforts(self) -> None:
        text = (Path(__file__).parents[1] / "web" / "item-generation.mjs").read_text(encoding="utf-8")
        estimate_line = next(line for line in text.splitlines() if "예상 호출: 생성" in line)
        self.assertIn("Claude Opus ${e.claude_effort}", estimate_line)
        self.assertIn("Codex 검증(${e.codex_effort})", estimate_line)
        self.assertNotIn("Claude Opus high", estimate_line)


class LabFixture(unittest.TestCase):
    def setUp(self) -> None:
        # Python 3.13's Windows mode 0o700 prevents sandbox access to disposable
        # fixtures, including the HWPX export's nested TemporaryDirectory.
        if os.name == "nt":
            mkdtemp, mkdir = tempfile.mkdtemp, os.mkdir

            def inherited_tempdir(*args, **kwargs):
                with mock.patch("tempfile._os.mkdir", side_effect=lambda path, mode=0o777: mkdir(path)):
                    path = mkdtemp(*args, **kwargs)
                # Nested export paths exceed MAX_PATH in this isolated dev checkout.
                return path if path.startswith("\\\\?\\") else "\\\\?\\" + path

            patcher = mock.patch("tempfile.mkdtemp", side_effect=inherited_tempdir)
            patcher.start()
            self.addCleanup(patcher.stop)
        synthetic_root = Path(__file__).resolve().parents[2] / ".test-tmp"
        synthetic_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=synthetic_root)
        base = Path(self.temporary.name)
        self.lab = base / "lab"
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        (self.lab / "scope").mkdir(parents=True)
        (self.lab / "scope" / "고1-통합과학2.json").write_text(json.dumps({
            "curriculum": "2022 개정", "excluded_notes": ["산화수 계산"],
            "units": [{"major": MAJOR, "middle": MIDDLE, "concepts": CONCEPTS}]}, ensure_ascii=False), encoding="utf-8")
        project = self.lab / "projects" / SOURCE_PROJECT
        project.mkdir(parents=True)
        (project / "items.jsonl").write_text(json.dumps({
            "item_id": SOURCE_ID, "project_id": SOURCE_PROJECT, "printed_number": "3",
            "unit": {"major": MAJOR, "middle": MIDDLE, "detail_topic": "금속과 이온의 반응"},
            "difficulty": {"level": "중"}, "structure": {"format": "보기(ㄱㄴㄷ)형"}}, ensure_ascii=False) + "\n", encoding="utf-8")
        (project / "_input.jsonl").write_text(json.dumps({
            "item_id": SOURCE_ID, "printed_number": "3", "content_hash": "a" * 64,
            "question": {"blocks": source_blocks("아연판을 황산 구리 수용액에 넣었더니 붉은색 구리가 석출되었다. 이에 대한 설명으로 옳은 것은?")},
            "answer": {"kind": "choice", "value": "②"}, "assets": []}, ensure_ascii=False) + "\n", encoding="utf-8")
        (self.lab / "patterns" / "통합과학2").mkdir(parents=True)
        (self.lab / "patterns" / "통합과학2" / f"Ⅰ-2_{MIDDLE}.md").write_text("# 패턴\n보기형 위주", encoding="utf-8")
        self.figure_png = base / "figure.png"
        Image.new("RGB", (320, 200), "white").save(self.figure_png)
        patcher = mock.patch.dict(os.environ, {"ASTRA_ITEM_LAB_ROOT": str(self.lab)})
        patcher.start()
        self.addCleanup(patcher.stop)
        ig._CACHE.clear()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, **overrides) -> dict:
        body = {"scope": "고1-통합과학2", "major": MAJOR, "middle": MIDDLE, "variants": 1, "new": 1, "difficulty": "중"}
        body.update(overrides)
        return ig.validate_request(body)

    def scoped_request(self, scope: str, **overrides) -> dict:
        source = self.lab / "scope" / "고1-통합과학2.json"
        target = self.lab / "scope" / f"{scope}.json"
        if not target.exists():
            target.write_bytes(source.read_bytes())
        return self.request(scope=scope, **overrides)


class DraftCheckTests(LabFixture):
    def test_auto_checks_flag_scope_calculation_and_similarity(self) -> None:
        corpus = {SOURCE_ID: ig.grams(ig._blocks_text(source_blocks("아연판을 황산 구리 수용액에 넣었다")))}
        good = ig.auto_checks(draft(), allowed_concepts=CONCEPTS, corpus=corpus, printed={})
        self.assertTrue(good["passed"])
        self.assertTrue(good["calc_ok"])
        bad = ig.auto_checks(draft(scope_concepts=["산화수"], calc_checks=[{"expression": "2*3", "expected": 7}]),
                             allowed_concepts=CONCEPTS, corpus=corpus, printed={})
        self.assertFalse(bad["passed"])
        self.assertEqual(bad["scope_unlisted"], ["산화수"])
        self.assertFalse(bad["calc_ok"])

    def test_calculator_rejects_non_arithmetic(self) -> None:
        with self.assertRaises(ig.ItemGenerationError):
            ig.safe_eval("__import__('os').system('x')")
        self.assertEqual(ig.safe_eval("(6+6)/4"), 3.0)

    def test_format_errors_require_single_choice_answer_and_intent(self) -> None:
        self.assertIn("선택형 정답은 선지 기호 하나여야 합니다.", ig.draft_format_errors(draft(answer="ㄱ, ㄷ")))
        self.assertTrue(ig.draft_format_errors(draft(design_intent="문장")))
        self.assertTrue(ig.draft_format_errors(draft(kind="variant", source_item_id=None)))
        self.assertEqual(ig.draft_format_errors(draft()), [])

    def test_fifty_items_are_split_into_five_item_prompts(self) -> None:
        request = self.request(variants=0, new=50)
        prompts = pipeline.build_prompts({"job_id": "large-job", "request": request})
        self.assertEqual([5] * 10, [count for _, _, count in prompts])
        estimate = ig.estimate_calls(request)
        self.assertEqual(10, estimate["claude_generate"])
        self.assertEqual(10, estimate["codex_verify"])
        self.assertEqual(50, sum(self.request(variants=25, new=25)[k] for k in ("variants", "new")))

    def test_request_limits_and_codex_effort_by_grade(self) -> None:
        with self.assertRaises(ig.ItemGenerationError):
            self.request(variants=26, new=25)
        with self.assertRaises(ig.ItemGenerationError):
            self.request(middle="없는 단원")
        self.assertEqual(ig.codex_effort_for_scope("고1-통합과학2"), "high")
        self.assertEqual(ig.codex_effort_for_scope("고2-역학과 에너지"), "max")
        self.assertEqual(ig.codex_effort_for_scope("고2-전자기와 양자"), "xhigh")
        self.assertEqual(ig.codex_effort_for_scope("고2-통합과학2"), "xhigh")
        self.assertEqual(ig.codex_effort_for_scope("중3-과학"), "high")

    def test_topic_tree_and_search_use_scope_and_analyses(self) -> None:
        tree = ig.topic_tree("고1-통합과학2")
        middle = tree["majors"][0]["middles"][0]
        self.assertEqual(middle["analysis_count"], 1)
        self.assertEqual(middle["details"], [{"name": "금속과 이온의 반응", "count": 1}])
        self.assertTrue(middle["pattern_available"])
        hits = ig.search_topics("고1-통합과학2", "이온 반응")
        self.assertEqual(hits[0]["kind"], "detail")


class FakeModels:
    STEMS = ["마그네슘 리본을 묽은 염산에 넣었더니 기체가 발생하였다.",
             "철 못을 황산 구리 수용액에 담가 두었더니 표면이 붉게 변하였다.",
             "아연 조각을 질산 납 수용액에 넣었더니 회색 고체가 생겼다.",
             "알루미늄 포일을 염화 구리 수용액에 넣었더니 용액의 색이 옅어졌다."]

    def __init__(self, png: Path, *, verifier_answers: dict[int, str] | None = None, repeat_new: bool = False) -> None:
        self.png = png
        self.answers = verifier_answers or {}
        self.calls: list[tuple] = []
        self.prompts: list[str] = []
        self.repeat_new = repeat_new
        self.new_count = 0

    def claude(self, prompt, *, effort, add_dirs):
        self.calls.append(("claude", effort))
        self.prompts.append(prompt)
        if "그림 파일을 Read 도구로 열어" in prompt:
            nos = [int(n) for n in re.findall(r"### 문항 (\d+)", prompt)]
            return json.dumps({"results": [{"no": n, "ok": True, "issues": []} for n in nos]}), {}
        if "[현재 초안]" in prompt:
            return json.dumps({"items": [draft(stem="수정된 자료 설명이다.")]}, ensure_ascii=False), {}
        if "변형 문항을 정확히" in prompt:
            figure = {"method": "code", "alt_text": "질량 그래프", "spec": {"type": "graph", "description": "시간-질량"}}
            return json.dumps({"items": [draft("variant", figure=figure)]}, ensure_ascii=False), {}
        stem = self.STEMS[0] if self.repeat_new and "[대체 생성]" not in prompt else self.STEMS[self.new_count % len(self.STEMS)]
        self.new_count += 1
        k = self.STEMS.index(stem)
        bogi = {"ㄱ": f"{k}번 금속은 산화된다.", "ㄴ": f"{k}번 용액의 양이온 수는 {k + 2}배가 된다.",
                "ㄷ": ["기체가 발생한다.", "용액의 색이 변한다.", "고체가 석출된다.", "전자는 금속에서 이온으로 이동한다."][k]}
        one = draft("new", stem=stem, bogi=bogi, table=None, calc_checks=[])
        wanted = re.search(r"새 문항 (\d+)개", prompt)
        n = int(wanted.group(1)) if wanted and self.repeat_new and "[대체 생성]" not in prompt else 1
        return json.dumps({"items": [one] * n}, ensure_ascii=False), {}

    def codex(self, prompt, *, effort, images, write=False, workdir=None, timeout=0):
        self.calls.append(("codex", effort, write, len(images)))
        if write:
            shutil.copyfile(self.png, workdir / "output.png")
            return "DONE", {}
        nos = [int(n) for n in re.findall(r"### 문항 (\d+)", prompt)]
        return json.dumps({"results": [{"no": n, "answer": self.answers.get(n, "③"), "reasoning": "풀이", "issue": None}
                                       for n in nos]}), {}


class PipelineTests(LabFixture):
    def run_job(self, models: FakeModels, **request) -> dict:
        job = ig.create_job(self.workspace, self.request(**request))
        worker = pipeline.GenerationWorker(self.workspace, runners=pipeline.Runners(models.claude, models.codex), autostart=False)
        self.assertTrue(worker.run_once())
        return ig.load_job(self.workspace, job["job_id"])

    def test_generate_figure_check_and_blind_verify(self) -> None:
        models = FakeModels(self.figure_png, verifier_answers={2: "①"})
        job = self.run_job(models)
        self.assertEqual(job["status"], "ready")
        variant, new = job["drafts"]
        self.assertEqual((variant["kind"], new["kind"]), ("variant", "new"))
        self.assertEqual(variant["figure"]["file"], "figures/d1-code.png")
        self.assertEqual(variant["figure"]["check"]["status"], "ok")
        self.assertEqual(variant["verification"]["status"], "verified")
        self.assertEqual(new["verification"]["status"], "answer_conflict")
        self.assertIn(("claude", "high"), models.calls)
        self.assertIn(("codex", "high", False, 1), models.calls)
        verify_prompts = [c for c in models.calls if c[0] == "codex" and not c[2]]
        self.assertEqual(len(verify_prompts), 1)

    def test_verifier_prompt_hides_answer_solution_and_intent(self) -> None:
        text = pipeline.problem_text({**draft(), "no": 1})
        self.assertNotIn("구리는 전자를 잃어 산화되고", text)
        self.assertNotIn("전자 이동으로 산화·환원을 판단한다", text)
        self.assertNotIn("정답", text)

    def test_revise_and_edit_requeue_verification(self) -> None:
        models = FakeModels(self.figure_png)
        job = self.run_job(models)
        worker = pipeline.GenerationWorker(self.workspace, runners=pipeline.Runners(models.claude, models.codex), autostart=False)
        pipeline.request_revision(self.workspace, job["job_id"], 2, "자료를 바꿔줘")
        worker.run_once()
        job = ig.load_job(self.workspace, job["job_id"])
        self.assertEqual(job["drafts"][1]["stem"], "수정된 자료 설명이다.")
        self.assertEqual(job["drafts"][1]["history"][0]["request"], "자료를 바꿔줘")
        self.assertEqual(job["drafts"][1]["verification"]["status"], "verified")
        with self.assertRaises(ig.ItemGenerationError):
            pipeline.edit_draft(self.workspace, job["job_id"], 2, {"answer": "ㄱ"})
        with self.assertRaises(ig.ItemGenerationError):
            pipeline.edit_draft(self.workspace, job["job_id"], 2, {"kind": "variant"})
        pipeline.edit_draft(self.workspace, job["job_id"], 2, {"answer": "①"})
        self.assertEqual(ig.load_job(self.workspace, job["job_id"])["drafts"][1]["verification"]["status"], "pending")

    def test_model_failure_marks_job_failed_without_drafts(self) -> None:
        def broken(*args, **kwargs):
            raise RuntimeError("quota")
        job = ig.create_job(self.workspace, self.request(variants=0, new=1))
        pipeline.run_job(self.workspace, job["job_id"], pipeline.Runners(broken, broken))
        job = ig.load_job(self.workspace, job["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("quota", job["error"])

    def test_bot_import_rejects_malformed_and_queues_valid_drafts(self) -> None:
        with self.assertRaises(ig.ItemGenerationError):
            pipeline.import_bot_drafts(self.workspace, self.request(), [{"kind": "new"}], bot_session_id="s")
        job = pipeline.import_bot_drafts(self.workspace, self.request(), [draft("new")], bot_session_id="s")
        self.assertEqual((job["route"], job["status"], len(job["drafts"])), ("bot", "queued", 1))
        self.assertEqual(job["drafts"][0]["verification"]["status"], "pending")


class ScopedEffortPipelineTests(LabFixture):
    def test_generation_revision_metadata_and_figure_efforts(self) -> None:
        cases = (("고2-물리학", "max", "max"), ("고2-역학과 에너지", "max", "max"),
                 ("고3-물리학", "max", "max"), ("고2-전자기와 양자", "high", "xhigh"),
                 ("고2-통합과학2", "high", "xhigh"), ("고1-물리학", "high", "high"))
        for scope, author_effort, verify_effort in cases:
            with self.subTest(scope=scope):
                workspace = self.workspace / scope
                workspace.mkdir()
                models = FakeModels(self.figure_png)
                runners = pipeline.Runners(models.claude, models.codex)
                job = ig.create_job(workspace, self.scoped_request(scope))
                job = pipeline.run_job(workspace, job["job_id"], runners)
                self.assertEqual(job["status"], "ready")
                self.assertEqual(job["generator_model"], "claude-opus-5-5")
                self.assertEqual(job["generator_effort"], author_effort)
                self.assertEqual(models.calls.count(("claude", author_effort)), 2)
                self.assertIn(("codex", verify_effort, False, 1), models.calls)
                self.assertIn(("codex", "medium", True, 0), models.calls)
                self.assertIn(("claude", "medium"), models.calls)
                self.assertEqual(job["drafts"][0]["figure"]["check"]["effort"], "medium")
                for item in job["drafts"]:
                    self.assertEqual(item["generator_model"], "claude-opus-5-5")
                    self.assertEqual(item["generator_effort"], author_effort)
                    self.assertEqual(item["verification"]["model"], "gpt-6-astra")
                    self.assertEqual(item["verification"]["effort"], verify_effort)
                for usage in (u for u in job["usage"] if u["step"] == "generate"):
                    self.assertEqual((usage["model"], usage["effort"]), ("claude-opus-5-5", author_effort))
                models.calls.clear()
                pipeline.request_revision(workspace, job["job_id"], 2, "자료를 바꿔줘")
                revised = pipeline.run_job(workspace, job["job_id"], runners)
                self.assertEqual(revised["drafts"][1]["stem"], "수정된 자료 설명이다.")
                self.assertEqual(revised["drafts"][1]["generator_effort"], author_effort)
                self.assertEqual(revised["drafts"][1]["verification"]["effort"], verify_effort)
                self.assertEqual(models.calls, [("claude", author_effort), ("codex", verify_effort, False, 0)])
                usage = next(u for u in revised["usage"] if u["step"] == "revise")
                self.assertEqual((usage["model"], usage["effort"]), ("claude-opus-5-5", author_effort))

    def test_revision_preserves_old_and_unrevised_metadata_and_legacy_fallback(self) -> None:
        job = ig.create_job(self.workspace, self.scoped_request("고2-역학과 에너지"), route="bot")
        old = draft(no=1, generator_model="claude-opus-5-5", generator_effort="high",
                    verification={"status": "verified", "model": "gpt-6-astra", "effort": "xhigh"})
        legacy = draft(no=2, stem=FakeModels.STEMS[1], verification={"status": "verified"})
        job = ig.update_job(self.workspace, job["job_id"], lambda j: j.update(
            status="ready", drafts=[old, legacy], generator_model="claude-opus-5-5", generator_effort="high"))
        pipeline.request_revision(self.workspace, job["job_id"], 1, "자료를 바꿔줘")
        models = FakeModels(self.figure_png)
        revised = pipeline.run_job(self.workspace, job["job_id"], pipeline.Runners(models.claude, models.codex))
        self.assertEqual(revised["drafts"][0]["generator_effort"], "max")
        self.assertEqual(revised["drafts"][0]["verification"]["effort"], "max")
        self.assertEqual(revised["drafts"][0]["history"][0]["previous"], old)
        self.assertEqual(revised["drafts"][1], legacy)
        self.assertEqual(revised["generator_effort"], "high")  # original job authoring metadata
        package, _ = ig.build_package(self.workspace, revised, legacy, item_id=SOURCE_ID)
        self.assertEqual(package["provenance"]["generation"]["generator"]["effort"], "high")
        # Older jobs without either metadata field retain the original fallback too.
        revised.pop("generator_effort")
        revised.pop("generator_model")
        package, _ = ig.build_package(self.workspace, revised, legacy, item_id=SOURCE_ID)
        self.assertEqual(package["provenance"]["generation"]["generator"]["effort"], "high")

    def test_failed_generation_and_revision_preserve_author_metadata(self) -> None:
        calls = []

        def broken(prompt, *, effort, **kwargs):
            calls.append(effort)
            raise RuntimeError("synthetic quota")

        job = ig.create_job(self.workspace, self.scoped_request("고2-물리학", variants=0, new=1))
        ig.update_job(self.workspace, job["job_id"], lambda j: j.update(
            generator_model="claude-opus-5-5", generator_effort="high"))
        failed = pipeline.run_job(self.workspace, job["job_id"], pipeline.Runners(broken, broken))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["drafts"], [])
        self.assertEqual(failed["generator_effort"], "high")
        self.assertIn("synthetic quota", failed["error"])
        old = draft(no=1, generator_model="item-analyst (hermes bot)", generator_effort=None,
                    verification={"status": "verified"}, history=[{"request": "old history"}])
        job = ig.update_job(self.workspace, job["job_id"], lambda j: j.update(status="ready", drafts=[old]))
        pipeline.request_revision(self.workspace, job["job_id"], 1, "자료를 바꿔줘")
        failed = pipeline.run_job(self.workspace, job["job_id"], pipeline.Runners(broken, broken))
        self.assertEqual(failed["drafts"][0], {**old, "revise_request": "자료를 바꿔줘", "revise_error": "synthetic quota"})
        self.assertEqual(failed["generator_effort"], "high")
        self.assertEqual(calls, ["max", "max"])  # genuine failures are not automatically retried

    def test_bot_verification_keeps_bot_author_metadata(self) -> None:
        job = pipeline.import_bot_drafts(self.workspace, self.scoped_request("고2-물리학"),
                                         [draft()], bot_session_id="synthetic-bot")
        models = FakeModels(self.figure_png)
        job = pipeline.run_job(self.workspace, job["job_id"], pipeline.Runners(models.claude, models.codex))
        self.assertEqual(models.calls, [("codex", "max", False, 0)])
        self.assertEqual(job["generator_model"], "item-analyst (hermes bot)")
        self.assertNotIn("generator_effort", job)
        self.assertEqual(job["drafts"][0]["generator_model"], "item-analyst (hermes bot)")
        self.assertIsNone(job["drafts"][0]["generator_effort"])
        package, _ = ig.build_package(self.workspace, job, job["drafts"][0], item_id=SOURCE_ID)
        self.assertEqual(package["provenance"]["generation"]["generator"]["model"], "item-analyst (hermes bot)")


class BankStorageTests(LabFixture):
    def ready_job(self) -> dict:
        models = FakeModels(self.figure_png, verifier_answers={2: "①"})
        job = ig.create_job(self.workspace, self.request())
        pipeline.run_job(self.workspace, job["job_id"], pipeline.Runners(models.claude, models.codex))
        return ig.load_job(self.workspace, job["job_id"])

    def test_confirm_stores_valid_hidden_items_with_generation_provenance(self) -> None:
        job = self.ready_job()
        result = ig.confirm_drafts(self.workspace, job["job_id"], [1, 2])
        self.assertEqual(result["project_id"], "project-itemgen-integrated-2")
        project_dir = self.workspace / result["project_id"]
        variant_id = result["results"][0]["item_id"]
        package = json.loads((project_dir / "items" / variant_id / "question.json").read_text(encoding="utf-8"))
        generation = package["provenance"]["generation"]
        self.assertEqual(package["provenance"]["source_system"]["name"], "item_generation")
        self.assertEqual(generation["source_item"]["item_id"], SOURCE_ID)
        self.assertEqual(generation["source_item"]["project_id"], SOURCE_PROJECT)
        self.assertEqual(generation["verification"]["status"], "verified")
        asset = package["resources"]["assets"][0]
        self.assertEqual(asset["role"], "figure_generated")
        self.assertTrue((project_dir / "items" / variant_id / asset["path"]).is_file())
        figures = [b for b in package["content"]["question"]["blocks"] if b["type"] == "figure"]
        self.assertEqual(figures[0]["asset_id"], figures[0]["original_asset_id"])
        explanation_text = json.dumps(package["content"]["explanation"], ensure_ascii=False)
        self.assertIn("출제 의도", explanation_text)
        self.assertEqual(ig.validate_package(package, project_dir / "items" / variant_id / "question.json"), [])
        again = ig.confirm_drafts(self.workspace, job["job_id"], [1])
        self.assertTrue(again["results"][0]["already"])

    def test_unverified_drafts_cannot_be_confirmed(self) -> None:
        job = pipeline.import_bot_drafts(self.workspace, self.request(), [draft("new")], bot_session_id=None)
        ig.update_job(self.workspace, job["job_id"], lambda j: j.update(status="ready"))
        with self.assertRaises(ig.ItemGenerationError):
            ig.confirm_drafts(self.workspace, job["job_id"], [1])
        # Codex returned no answer at all: still unverified.
        ig.update_job(self.workspace, job["job_id"], lambda j: j["drafts"][0].update(verification={"status": "not_solved"}))
        with self.assertRaises(ig.ItemGenerationError):
            ig.confirm_drafts(self.workspace, job["job_id"], [1])

    def test_bank_lists_generated_items_with_origin_filter(self) -> None:
        job = self.ready_job()
        ig.confirm_drafts(self.workspace, job["job_id"], [1, 2])
        items = list_bank_items(self.workspace)
        kinds = sorted(item["generation"]["kind"] for item in items)
        self.assertEqual(kinds, ["new", "variant"])
        for item in items:
            package = json.loads((self.workspace / item["project_id"] / "items" / item["item_id"] / "question.json").read_text(encoding="utf-8"))
            layouts = [b.get("source_layout") for b in package["content"]["question"]["blocks"] if b["type"] == "choices"]
            self.assertEqual(layouts, ["grid"])
        # Variant labels carry the source number but must not look like sub-questions "N(M)".
        self.assertTrue(all(not item["dependency"]["shared_context"] for item in items))
        variants = search_bank_items(self.workspace, filters={"origin": "variant"})
        self.assertEqual(len(variants["items"] if isinstance(variants, dict) else variants), 1)
        with self.assertRaises(Exception):
            search_bank_items(self.workspace, filters={"origin": "bad"})

    def test_question_set_hwpx_exports_generated_figure_item(self) -> None:
        from app.question_bank import create_question_set, patch_question_set
        from app.question_set_hwpx import create_question_set_hwpx_build

        job = self.ready_job()
        result = ig.confirm_drafts(self.workspace, job["job_id"], [1, 2])
        refs = [{"project_id": result["project_id"], "item_id": r["item_id"], "source_revision": 1} for r in result["results"]]
        question_set = create_question_set(self.workspace, title="AI 생성 시험")
        question_set = patch_question_set(self.workspace, question_set["set_id"], question_set["revision"], {"items": refs})
        build = create_question_set_hwpx_build(self.workspace, question_set["set_id"])
        self.assertTrue(build.outputs)
        with zipfile.ZipFile(build.outputs[0].path) as archive:
            names = archive.namelist()
            sections = "".join(archive.read(n).decode("utf-8", "ignore") for n in names if n.startswith("Contents/section"))
        self.assertTrue(any(n.startswith("BinData/") for n in names))
        self.assertIn("구리판을 질산 은 수용액에 넣었더니", sections)


class RegistryTests(LabFixture):
    """Item codes, fingerprints and duplicate checks across drafts, jobs and the bank."""

    def run_new(self, models: FakeModels, n: int = 1) -> dict:
        job = ig.create_job(self.workspace, self.request(variants=0, new=n))
        pipeline.run_job(self.workspace, job["job_id"], pipeline.Runners(models.claude, models.codex))
        return ig.load_job(self.workspace, job["job_id"])

    def test_item_code_is_stable_and_readable(self) -> None:
        from app import item_registry as reg

        self.assertEqual(reg.item_code("gen-20261007-110619-597c6a", 3), "AIQ-261007-597c6a-03")
        self.assertEqual(reg.item_code("gen-20261007-110619-597c6a", 3), reg.item_code("gen-20261007-110619-597c6a", 3))
        self.assertRegex(reg.item_code("odd-id", 1), r"^AIQ-000000-[0-9a-f]{6}-01$")

    def test_number_only_change_is_duplicate_and_new_situation_is_unique(self) -> None:
        from app import item_registry as reg

        base = {"job_id": "gen-20261007-110619-aaaaaa", "scope": "고1-통합과학2", "unit": {"major": MAJOR, "middle": MIDDLE}}
        first = reg.draft_entry(base, {**draft(stem="질량이 10 g인 구리판을 넣었다."), "no": 1})
        numbers = reg.draft_entry(base, {**draft(stem="질량이 25 g인 구리판을 넣었다."), "no": 2})
        self.assertEqual(reg.check(numbers, [first])["status"], "duplicate")
        self.assertIn("숫자만 다르고 본문이 같음", reg.check(numbers, [first])["matches"][0]["reasons"])
        other = reg.draft_entry(base, {**draft(stem="전지의 두 전극에서 일어나는 반응을 비교하였다.",
                                               bogi={"ㄱ": "가", "ㄴ": "나", "ㄷ": "다"},
                                               choices={"①": "ㄱ", "②": "ㄴ"}, instruction="옳은 것은?",
                                               table=None, design_intent={"purpose": "전지 반응 비교"}), "no": 3})
        self.assertEqual(reg.check(other, [first])["status"], "unique")

    def test_drafts_get_codes_and_a_repeat_in_the_same_job_is_excluded_then_replaced(self) -> None:
        models = FakeModels(self.figure_png, repeat_new=True)
        job = self.run_new(models, n=2)
        # One prompt asks for 2; the fake repeats the same item → 2nd is a duplicate, then one replacement round.
        codes = [d["item_code"] for d in job["drafts"]]
        self.assertEqual(len(set(codes)), len(codes))
        self.assertTrue(all(c.startswith("AIQ-") for c in codes))
        statuses = [d["duplicate_check"]["status"] for d in job["drafts"]]
        self.assertEqual(statuses.count("duplicate"), 1)
        dup = next(d for d in job["drafts"] if d["duplicate_check"]["status"] == "duplicate")
        self.assertEqual(dup["duplicate_check"]["matches"][0]["code"], job["drafts"][0]["item_code"])
        self.assertEqual(dup["verification"]["status"], "pending")  # not sent to Codex
        kept = [d for d in job["drafts"] if d["duplicate_check"]["status"] != "duplicate"]
        self.assertEqual(len(kept), 2)
        self.assertTrue(all(d["verification"]["status"] == "verified" for d in kept))
        self.assertTrue(any("[대체 생성]" in p for p in models.prompts))
        self.assertIn("중복 제외 1", job["progress"]["message"])
        with self.assertRaises(ig.ItemGenerationError):
            ig.confirm_drafts(self.workspace, job["job_id"], [dup["no"]])

    def test_later_job_sees_earlier_items_in_prompt_and_registry(self) -> None:
        first = self.run_new(FakeModels(self.figure_png))
        code = first["drafts"][0]["item_code"]
        models = FakeModels(self.figure_png, repeat_new=True)
        second = self.run_new(models)
        generate_prompt = next(p for p in models.prompts if "새 문항" in p and "[대체 생성]" not in p)
        self.assertIn("[이미 만든 문항", generate_prompt)
        self.assertIn(code, generate_prompt)
        self.assertEqual(second["drafts"][0]["duplicate_check"]["status"], "duplicate")
        self.assertEqual(second["drafts"][0]["duplicate_check"]["matches"][0]["code"], code)

    def test_confirm_stores_code_and_fingerprint_and_blocks_bank_duplicates(self) -> None:
        first = self.run_new(FakeModels(self.figure_png))
        stored = ig.confirm_drafts(self.workspace, first["job_id"], [1])
        code = stored["results"][0]["item_code"]
        item_dir = self.workspace / stored["project_id"] / "items" / stored["results"][0]["item_id"]
        package = json.loads((item_dir / "question.json").read_text(encoding="utf-8"))
        generation = package["provenance"]["generation"]
        self.assertEqual(generation["item_code"], code)
        self.assertEqual(len(generation["fingerprint"]["text_sha256"]), 64)
        self.assertEqual(ig.validate_package(package, item_dir / "question.json"), [])
        bank = list_bank_items(self.workspace)
        self.assertEqual(bank[0]["generation"]["item_code"], code)
        # A second job whose draft slipped past generation (e.g. created before the bank save) is blocked at confirm.
        second = self.run_new(FakeModels(self.figure_png))
        ig.update_job(self.workspace, second["job_id"], lambda j: j["drafts"][0].update(
            stem=first["drafts"][0]["stem"], duplicate_check={"status": "unique", "matches": []},
            verification={"status": "verified"}))
        with self.assertRaises(ig.ItemGenerationError) as caught:
            ig.confirm_drafts(self.workspace, second["job_id"], [1])
        self.assertIn(code, str(caught.exception))
        self.assertEqual(ig.load_job(self.workspace, second["job_id"])["drafts"][0]["duplicate_check"]["status"], "duplicate")

    def test_items_saved_before_registry_get_a_code_from_job_and_draft_number(self) -> None:
        from app import item_registry as reg

        first = self.run_new(FakeModels(self.figure_png))
        stored = ig.confirm_drafts(self.workspace, first["job_id"], [1])
        path = self.workspace / stored["project_id"] / "items" / stored["results"][0]["item_id"] / "question.json"
        package = json.loads(path.read_text(encoding="utf-8"))
        for key in ("item_code", "fingerprint", "duplicate_check"):
            package["provenance"]["generation"].pop(key)
        path.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
        entries = reg.bank_entries(self.workspace)
        self.assertEqual(entries[0]["code"], reg.item_code(first["job_id"], 1))
        self.assertEqual(ig.generation_summary(package)["item_code"], reg.item_code(first["job_id"], 1))

    def test_bank_comparison_uses_current_content_and_excludes_shared_boilerplate(self):
        from app import item_registry as reg
        first = self.run_new(FakeModels(self.figure_png))
        saved = ig.confirm_drafts(self.workspace, first['job_id'], [1])
        path = self.workspace / saved['project_id'] / 'items' / saved['results'][0]['item_id'] / 'question.json'
        package = json.loads(path.read_text('utf-8'))
        original = reg.draft_entry(first, first['drafts'][0])
        entries = reg.bank_entries(self.workspace)
        self.assertEqual(entries[0]['fingerprint']['text_sha256'], original['fingerprint']['text_sha256'])
        self.assertNotIn(first['drafts'][0]['instruction'], entries[0]['text'])
        changed = draft(stem='아주 다른 새로운 별의 생성 과정', bogi=None, table=None, choices=None, format='단답형', answer='3')
        package['content'] = ig.draft_to_content(changed, package['item_id'])
        ig.atomic_write_json(path, package)
        now = reg.bank_entries(self.workspace)[0]
        self.assertEqual(now['fingerprint']['text_sha256'], reg.draft_fingerprint(changed)['text_sha256'])
        self.assertNotEqual(now['fingerprint']['text_sha256'], original['fingerprint']['text_sha256'])


class GuideAndApiTests(LabFixture):
    def test_topics_and_estimate_routes_return_effective_efforts_without_server(self) -> None:
        from app.item_generation_routes import handle_item_generation_api

        for scope, author, verify in (("고2-物理学", "high", "xhigh"), ("고2-물리학", "max", "max"),
                                      ("고2-역학과 에너지", "max", "max"),
                                      ("고2-전자기와 양자", "high", "xhigh"), ("고1-통합과학2", "high", "high")):
            with self.subTest(scope=scope):
                request = self.scoped_request(scope)
                handler = mock.Mock()
                handler.app.config.workspace_root = self.workspace
                handler._read_json.return_value = request
                handle_item_generation_api(handler, "GET", ["api", "item-generation", "topics"], {"scope": scope})
                tree = handler._send_json.call_args.args[1]
                self.assertEqual((tree["claude_effort"], tree["codex_effort"]), (author, verify))
                handle_item_generation_api(handler, "POST", ["api", "item-generation", "estimate"], {})
                estimate = handler._send_json.call_args.args[1]["estimate"]
                self.assertEqual((estimate["claude_effort"], estimate["codex_effort"]), (author, verify))
                self.assertEqual((estimate["claude_generate"], estimate["codex_verify"]), (2, 1))
                job = ig.create_job(self.workspace, request)
                self.assertEqual(job["estimate"], estimate)

    def test_guide_rule_is_appended_and_changes_hash(self) -> None:
        (self.lab / "guides").mkdir()
        (self.lab / "guides" / "출제지침.md").write_text("# 지침" + chr(10), encoding="utf-8")
        _, before = ig.guides_text("통합과학2")
        added = ig.append_guide_rule("  보기 ㄷ은   그래프 해석으로 만든다 ")
        self.assertTrue(added["added"].endswith("보기 ㄷ은 그래프 해석으로 만든다"))
        text, after = ig.guides_text("통합과학2")
        self.assertIn("보기 ㄷ은 그래프 해석으로 만든다", text)
        self.assertNotEqual(before, after)
        with self.assertRaises(ig.ItemGenerationError):
            ig.append_guide_rule("   ")

    @unittest.skipIf(os.environ.get("ASTRA_ITEMGEN_NO_SERVICE_TESTS") == "1", "service starts excluded by scoped test run")
    def test_http_routes_require_session_and_run_job_flow(self) -> None:
        import http.client
        import threading

        from app.server import create_server

        allowed = Path(self.temporary.name) / "allowed"
        allowed.mkdir()
        models = FakeModels(self.figure_png)
        worker_class = pipeline.GenerationWorker
        with mock.patch('app.item_generation_pipeline.GenerationWorker', side_effect=lambda workspace, **kw: worker_class(
                workspace, runners=pipeline.Runners(models.claude, models.codex), autostart=False)):
            server = create_server(workspace_root=self.workspace, import_roots=[allowed], port=0, token="itemgen-token")
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(method, path, body=None, token="itemgen-token"):
            headers = {"Accept": "application/json"}
            if token:
                headers["X-Session-Token"] = token
            payload = None
            if body is not None:
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json"
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                connection.request(method, path, body=payload, headers=headers)
                response = connection.getresponse()
                raw = response.read()
                return response.status, json.loads(raw.decode("utf-8")) if raw else {}
            finally:
                connection.close()

        try:
            status, _ = request("GET", "/api/item-generation/scopes", token=None)
            self.assertEqual(status, 401)
            status, scopes = request("GET", "/api/item-generation/scopes")
            self.assertEqual((status, scopes["scopes"][0]["scope"]), (200, "고1-통합과학2"))
            status, created = request("POST", "/api/item-generation/jobs",
                                      {"scope": "고1-통합과학2", "major": MAJOR, "middle": MIDDLE, "variants": 1, "new": 1})
            self.assertEqual(status, 201, created)
            server.app.item_generation_worker.run_once()
            for _ in range(100):
                status, job = request("GET", f"/api/item-generation/jobs/{created['job_id']}")
                if job["status"] in {"ready", "failed", "partial"}:
                    break
                import time
                time.sleep(0.1)
            self.assertEqual(job["status"], "ready", job)
            status, confirmed = request("POST", f"/api/item-generation/jobs/{created['job_id']}/confirm", {"numbers": [1, 2]})
            self.assertEqual(status, 200, confirmed)
            status, bank = request("GET", "/api/bank/items")
            self.assertEqual(sorted(i["generation"]["kind"] for i in bank["items"]), ["new", "variant"])
            status, projects = request("GET", "/api/projects")
            listed = projects.get("projects", projects) if isinstance(projects, dict) else projects
            self.assertFalse(any(str(p.get("project_id", "")).startswith("project-itemgen-") for p in listed))
            status, bad = request("POST", "/api/item-generation/jobs", {"scope": "고1-통합과학2", "major": MAJOR, "middle": "없음", "variants": 1})
            self.assertEqual(status, 400, bad)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class BotChatTests(LabFixture):
    def test_bot_turn_is_recorded_and_drafts_read_from_own_folder(self) -> None:
        seen = {}

        def runner(query, *, resume, write_root):
            seen.update(query=query, resume=resume, write_root=write_root)
            (write_root / "drafts.json").write_text(json.dumps({"items": [draft("new")]}, ensure_ascii=False), encoding="utf-8")
            return {"text": "초안을 저장했습니다.", "session_id": "sess-1", "usage": {}, "duration_s": 1.0}

        bots = BotChats(self.workspace, runner=runner, lab=self.lab)
        chat = bots.create({"scope": "고1-통합과학2", "major": MAJOR, "middle": MIDDLE}, "보기형 2문항 상의하자")
        for _ in range(200):
            if chat["chat_id"] not in bots._busy:
                break
            import time
            time.sleep(0.01)
        from app.item_generation_bot import load_chat
        stored = load_chat(self.workspace, chat["chat_id"])
        self.assertEqual([t["role"] for t in stored["turns"]], ["user", "bot"])
        self.assertEqual(stored["session_id"], "sess-1")
        self.assertEqual(seen["write_root"], self.lab / "app_drafts" / chat["chat_id"])
        self.assertIn("파일 쓰기는 이 폴더만 가능하다", seen["query"])
        self.assertEqual(len(bots.read_drafts(chat["chat_id"])), 1)
        with self.assertRaises(ig.ItemGenerationError):
            bots.read_drafts("chat-../../x")


if __name__ == "__main__":
    unittest.main()
