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


class LabFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
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
        self.assertEqual(ig.codex_effort_for_scope("고2-역학과 에너지"), "xhigh")
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
    def __init__(self, png: Path, *, verifier_answers: dict[int, str] | None = None) -> None:
        self.png = png
        self.answers = verifier_answers or {}
        self.calls: list[tuple] = []

    def claude(self, prompt, *, effort, add_dirs):
        self.calls.append(("claude", effort))
        if "그림 파일을 Read 도구로 열어" in prompt:
            nos = [int(n) for n in re.findall(r"### 문항 (\d+)", prompt)]
            return json.dumps({"results": [{"no": n, "ok": True, "issues": []} for n in nos]}), {}
        if "[현재 초안]" in prompt:
            return json.dumps({"items": [draft(stem="수정된 자료 설명이다.")]}, ensure_ascii=False), {}
        if "변형 문항을 정확히" in prompt:
            figure = {"method": "code", "alt_text": "질량 그래프", "spec": {"type": "graph", "description": "시간-질량"}}
            return json.dumps({"items": [draft("variant", figure=figure)]}, ensure_ascii=False), {}
        return json.dumps({"items": [draft("new")]}, ensure_ascii=False), {}

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


class GuideAndApiTests(LabFixture):
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

    def test_http_routes_require_session_and_run_job_flow(self) -> None:
        import http.client
        import threading

        from app.server import create_server

        allowed = Path(self.temporary.name) / "allowed"
        allowed.mkdir()
        server = create_server(workspace_root=self.workspace, import_roots=[allowed], port=0, token="itemgen-token")
        server.app.item_generation_worker.runners = pipeline.Runners(*(lambda m: (m.claude, m.codex))(FakeModels(self.figure_png)))
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
