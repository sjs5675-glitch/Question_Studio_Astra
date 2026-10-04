from __future__ import annotations

import http.client
import hashlib
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from app.server import create_server
from tests.test_question_bank import make_project


TOKEN = "question-bank-server-token"


class QuestionBankServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        allowed = self.root / "allowed"
        allowed.mkdir()
        make_project(self.workspace, "project-one", [("item-1", 2, "첫 문제")])
        make_project(self.workspace, "project-two", [("item-2", 3, "둘째 문제")])
        self.server = create_server(
            workspace_root=self.workspace,
            import_roots=[allowed],
            port=0,
            token=TOKEN,
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, method: str, path: str, body: object | None = None) -> tuple[int, dict]:
        headers = {"X-Session-Token": TOKEN, "Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8")) if raw else {}
        finally:
            connection.close()

    def test_bank_create_patch_and_get_routes(self) -> None:
        status, bank = self.request("GET", "/api/bank/items")
        self.assertEqual(200, status, bank)
        self.assertEqual(2, len(bank["items"]))

        status, created = self.request(
            "POST",
            "/api/question-sets",
            {"title": "혼합 문제지", "output_mode": "separate"},
        )
        self.assertEqual(201, status, created)
        question_set = created["question_set"]
        set_id = question_set["set_id"]
        status, saved = self.request(
            "PATCH",
            f"/api/question-sets/{set_id}",
            {
                "expected_revision": 1,
                "output_mode": "combined_end_section",
                "title": "순서 변경",
                "items": [
                    {"project_id": "project-two", "item_id": "item-2"},
                    {"project_id": "project-one", "item_id": "item-1"},
                ],
            },
        )
        self.assertEqual(200, status, saved)
        self.assertEqual(
            ["item-2", "item-1"],
            [item["item_id"] for item in saved["question_set"]["items"]],
        )
        self.assertEqual(
            ["current", "current"],
            [item["revision_status"] for item in saved["question_set"]["resolved_items"]],
        )

        status, listed = self.request("GET", "/api/question-sets")
        self.assertEqual(200, status, listed)
        self.assertEqual(1, len(listed["question_sets"]))

    def test_bank_items_expose_project_provider_profile(self) -> None:
        with self.server.app.batches._lock:
            self.server.app.batches._jobs["project-one-profile"] = {
                "id": "project-one-profile",
                "project_id": "project-one",
                "request": {"provider_profile": "codex_only"},
            }

        status, bank = self.request("GET", "/api/bank/items")

        self.assertEqual(200, status, bank)
        profiles = {
            item["ref"]["project_id"]: item["provider_profile"]
            for item in bank["items"]
        }
        self.assertEqual("codex_only", profiles["project-one"])
        self.assertEqual("full", profiles["project-two"])

    def test_delete_archives_only_set_definition_and_rejects_stale_revision(self) -> None:
        _, created = self.request("POST", "/api/question-sets", {"title": "삭제 검사"})
        set_id = created["question_set"]["set_id"]
        before = hashlib.sha256((self.workspace / "project-one/items/item-1/question.json").read_bytes()).hexdigest()
        active = self.workspace / "question_sets" / f"{set_id}.json"
        status, _ = self.request("DELETE", f"/api/question-sets/{set_id}", {"expected_revision": 2})
        self.assertEqual(409, status)
        self.assertTrue(active.is_file())
        status, deleted = self.request("DELETE", f"/api/question-sets/{set_id}", {"expected_revision": 1})
        self.assertEqual(200, status, deleted)
        self.assertFalse(active.exists())
        self.assertTrue((self.workspace / "question_sets/.deleted" / f"{set_id}.json").is_file())
        self.assertEqual(before, hashlib.sha256((self.workspace / "project-one/items/item-1/question.json").read_bytes()).hexdigest())
        status, listed = self.request("GET", "/api/question-sets")
        self.assertEqual(200, status)
        self.assertFalse(listed["question_sets"])

    def test_bad_patch_and_stale_revision_do_not_write(self) -> None:
        _, created = self.request("POST", "/api/question-sets", {})
        set_id = created["question_set"]["set_id"]
        body = {
            "expected_revision": 1,
            "items": [{"project_id": "project-one", "item_id": "item-1"}],
        }
        status, _ = self.request("PATCH", f"/api/question-sets/{set_id}", body)
        self.assertEqual(200, status)
        status, _ = self.request("PATCH", f"/api/question-sets/{set_id}", body)
        self.assertEqual(409, status)

    def test_private_provenance_never_enters_web_responses(self) -> None:
        known_code = "AS1-" + "b" * 64
        project_path = self.workspace / "project-one" / "project.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project["item_order"] = ["item-1"]
        project_path.write_text(json.dumps(project), encoding="utf-8")
        private = self.workspace / "_internal" / "provenance" / "index.json"
        private.parent.mkdir(parents=True)
        private.write_text(json.dumps({"records": [{"provenance_code": known_code}]}), encoding="utf-8")
        routes = [
            ("GET", "/api/bank/items", None),
            ("GET", "/api/projects", None),
            ("GET", "/api/projects/project-one/items/item-1", None),
            ("POST", "/api/question-sets", {"title": "비노출 검사"}),
            ("GET", "/api/not-found", None),
            ("GET", "/_internal/provenance/index.json", None),
            ("GET", "/api/_internal/provenance/index.json", None),
            ("GET", "/%2e%2e/_internal/provenance/index.json", None),
        ]
        for method, route, body in routes:
            with self.subTest(route=route):
                status, response = self.request(method, route, body)
                self.assertNotIn(known_code, json.dumps(response))
                if "_internal" in route:
                    self.assertGreaterEqual(status, 400)
                elif route != "/api/not-found":
                    self.assertLess(status, 400)
        original_config = self.server.app.config
        try:
            self.server.app.config = replace(original_config, web_root=self.workspace)
            status, response = self.request("GET", "/_internal/provenance/index.json")
            self.assertEqual(404, status)
            self.assertNotIn(known_code, json.dumps(response))
        finally:
            self.server.app.config = original_config


if __name__ == "__main__":
    unittest.main()
