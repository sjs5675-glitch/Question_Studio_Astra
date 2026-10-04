from __future__ import annotations

import astra_cli

import copy
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from app.model import RevisionConflict, atomic_write_json, make_source_only_question
from app.transcription import (
    DEFAULT_PROVIDER_CONFIG,
    PROVIDER_DEFAULTS,
    CandidateBaseRevisionConflict,
    CandidateIntegrityError,
    CandidateValidationError,
    CodexCliAdapter,
    CodexRateLimitError,
    GeminiAdapter,
    ImageValidationError,
    OpenAIAdapter,
    ProviderNotConfigured,
    ProviderConfig,
    ProviderRequestError,
    ValidatedImage,
    _collect_choice_ids,
    _collect_statement_ids,
    apply_candidate_job,
    clone_candidate_job_for_item,
    create_transcription_job,
    inspect_image,
    load_candidate_job,
    make_idempotency_key,
    normalize_canonical_question_headers,
    normalize_candidate_for_apply,
    parse_provider_profile,
)
from scripts.validate_package import Validator as PackageValidator


def candidate(*, section: str = "question", coverage: bool = True) -> dict:
    return {
        "schema_version": "0.1.0",
        "section": section,
        "blocks": [
            {
                "type": "paragraph",
                "role": "stem" if section == "question" else "explanation",
                "runs": [
                    {"type": "text", "text": "물체의 속력은 "},
                    {
                        "type": "equation",
                        "source_text": "v=d/t",
                        "latex": "v=\\frac{d}{t}",
                    },
                ],
                "bbox": {"left": 0.1, "top": 0.1, "right": 0.9, "bottom": 0.3},
                "confidence": 0.98,
            },
            {
                "type": "choices",
                "source_layout": "vertical",
                "items": [
                    {
                        "label": "①",
                        "runs": [{"type": "text", "text": "① 1 m/s"}],
                        "figure_bbox": None,
                    },
                    {
                        "label": "②",
                        "runs": [{"type": "text", "text": "2 m/s"}],
                        "figure_bbox": None,
                    },
                ],
                "bbox": {"left": 0.1, "top": 0.4, "right": 0.8, "bottom": 0.8},
                "confidence": 0.95,
            },
            {
                "type": "figure",
                "role": "main",
                "figure_kind": "diagram",
                "regeneration_policy": "original_only",
                "bbox": {"left": 0.2, "top": 0.2, "right": 0.5, "bottom": 0.6},
                "labels": [],
                "alt_text": "속력 모식도",
                "regeneration_note": None,
                "confidence": 0.9,
            },
        ],
        "answer": {
            "kind": "choice",
            "choice_labels": ["②"],
            "runs": [],
            "true_false": [],
        },
        "coverage_complete": coverage,
        "ignored_regions": [],
        "warnings": [],
    }


class FakeAdapter:
    provider_name = "codex"

    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls = 0
        self.last_prompt = ""

    def transcribe(self, **request) -> str:
        self.calls += 1
        self.last_prompt = request["prompt"]
        body = json.dumps(self.response, ensure_ascii=False)
        return f"```json\n{body}\n```"


@unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow required")
class TranscriptionTest(unittest.TestCase):
    def make_item(self, root: Path, *, section: str = "question") -> tuple[Path, str]:
        from PIL import Image

        item_dir = root / "item"
        source_dir = item_dir / "source"
        source_dir.mkdir(parents=True)
        image_path = source_dir / f"{section}.png"
        Image.new("RGB", (40, 30), "white").save(image_path)
        source_asset_id = f"source-{section}"
        asset = {
            "asset_id": source_asset_id,
            "section": section,
            "role": "source_crop",
            "state": "ready",
            "path": f"source/{section}.png",
            "media_type": "image/png",
            "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
            "width_px": 40,
            "height_px": 30,
            "generation": None,
            "review": {
                "status": "unreviewed",
                "boundary_uncertain": False,
                "notes": [],
            },
        }
        package = make_source_only_question(
            item_id="item-1",
            identity={
                "printed_number": "1",
                "unit_kind": "question",
                "answer_type": "multiple_choice",
            },
            provenance={"transcriptions": []},
            assets=[asset],
            has_answer_source=True,
            has_explanation_source=section == "explanation",
        )
        question_path = item_dir / "question.json"
        atomic_write_json(question_path, package)
        return question_path, source_asset_id

    def make_item_with_explanation_job(
        self, root: Path, *, existing_answer: dict | None
    ):
        from PIL import Image

        question_path, question_asset_id = self.make_item(root)
        question_job = create_transcription_job(
            question_path,
            question_asset_id,
            "question",
            adapter=FakeAdapter(candidate()),
        )
        document = apply_candidate_job(
            question_path, question_job.path, expected_base_revision=1
        )
        explanation_path = question_path.parent / "source" / "explanation.png"
        Image.new("RGB", (40, 30), "white").save(explanation_path)
        explanation_asset_id = "source-explanation"
        document["resources"]["assets"].append(
            {
                "asset_id": explanation_asset_id,
                "section": "explanation",
                "role": "source_crop",
                "state": "ready",
                "path": "source/explanation.png",
                "media_type": "image/png",
                "sha256": hashlib.sha256(explanation_path.read_bytes()).hexdigest(),
                "width_px": 40,
                "height_px": 30,
                "generation": None,
                "review": {
                    "status": "unreviewed",
                    "boundary_uncertain": False,
                    "notes": [],
                },
            }
        )
        document["content"]["answer"] = copy.deepcopy(existing_answer)
        document["workflow"]["revision"] = 3
        atomic_write_json(question_path, document)
        explanation_job = create_transcription_job(
            question_path,
            explanation_asset_id,
            "explanation",
            adapter=FakeAdapter(candidate(section="explanation")),
        )
        return question_path, explanation_job

    def test_provider_defaults_and_unconfigured_adapters(self) -> None:
        self.assertEqual(("codex", "gpt-6-astra"), (DEFAULT_PROVIDER_CONFIG.provider, DEFAULT_PROVIDER_CONFIG.model))
        self.assertTrue(all(value == DEFAULT_PROVIDER_CONFIG for value in PROVIDER_DEFAULTS.values()))
        with patch.dict(os.environ, {"GEMINI_API_KEY": "dummy", "OPENAI_API_KEY": "dummy"}):
            for adapter in (GeminiAdapter(), OpenAIAdapter()):
                with self.assertRaises(ProviderRequestError):
                    adapter.transcribe()

    def test_provider_profile_is_strict_and_legacy_defaults_to_full(self) -> None:
        self.assertEqual("full", parse_provider_profile(None))
        self.assertEqual("full", parse_provider_profile("full"))
        self.assertEqual("codex_only", parse_provider_profile("codex_only"))
        for value in (True, "codex", "", 1):
            with self.subTest(value=value), self.assertRaises(CandidateValidationError):
                parse_provider_profile(value)

    def test_codex_cli_uses_read_only_structured_image_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "source.png"
            image.write_bytes(b"image")
            fixture = astra_cli.Result(json.dumps(candidate()), candidate(), {"selected_model": astra_cli.MODEL, "observed_model": None})
            with patch("astra_cli.run", return_value=fixture) as run:
                adapter = CodexCliAdapter()
                raw = adapter.transcribe(image=ValidatedImage(image, "image/png", hashlib.sha256(b"image").hexdigest(), 1, 1, 5), section="question", prompt="transcribe only", schema={"type": "object"}, model=astra_cli.MODEL)
            self.assertEqual(candidate(), json.loads(raw))
            self.assertEqual((image,), run.call_args.kwargs["images"])
            self.assertEqual("ocr-question", run.call_args.kwargs["stage"])
            self.assertEqual(astra_cli.MODEL, run.call_args.kwargs["model"])
            self.assertIn("untrusted", run.call_args.kwargs["prompt"])
            self.assertIsNone(adapter.last_receipt["observed_model"])

    def test_codex_cli_rate_limit_stops_without_hidden_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "source.png"
            image.write_bytes(b"image")
            with patch("astra_cli.run", side_effect=astra_cli.AstraError("quota_exhausted")) as run, self.assertRaises(CodexRateLimitError) as caught:
                CodexCliAdapter().transcribe(image=ValidatedImage(image, "image/png", hashlib.sha256(b"image").hexdigest(), 1, 1, 5), section="question", prompt="source", schema={"type": "object"}, model=astra_cli.MODEL)
            self.assertEqual(1, run.call_count)
            self.assertEqual(1, caught.exception.attempts)
            self.assertEqual("quota_exhausted", caught.exception.code)

    def test_codex_cli_never_accepts_stale_output_from_a_failed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            question, asset = self.make_item(Path(temporary))
            with patch("astra_cli.run", side_effect=astra_cli.AstraError("terminal_missing")) as run, self.assertRaises(ProviderRequestError):
                create_transcription_job(question, asset, "question")
            self.assertEqual(1, run.call_count)
            self.assertFalse((question.parent / "jobs/transcription").exists())

    def test_codex_only_candidate_identity_is_separate_with_astra_text_in_both(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            question_path, asset_id = self.make_item(Path(temporary))
            full_adapter = FakeAdapter(candidate())
            full = create_transcription_job(
                question_path, asset_id, "question", adapter=full_adapter
            )
            codex_adapter = FakeAdapter(candidate())
            codex_adapter.provider_name = "codex"
            codex = create_transcription_job(
                question_path,
                asset_id,
                "question",
                adapter=codex_adapter,
                config=ProviderConfig("codex", "gpt-6-astra"),
                provider_profile="codex_only",
            )
            full_again = create_transcription_job(
                question_path, asset_id, "question", adapter=full_adapter
            )
        self.assertNotEqual(full.path, codex.path)
        self.assertNotIn("provider_profile", full.metadata)
        self.assertEqual("codex_only", codex.metadata["provider_profile"])
        self.assertTrue(full_again.cached)
        self.assertEqual(1, full_adapter.calls)

    def test_profileless_full_candidate_keeps_legacy_provider_model_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            question_path, asset_id = self.make_item(Path(temporary))
            adapter = FakeAdapter(candidate())
            adapter.provider_name = "gemini"
            created = create_transcription_job(
                question_path,
                asset_id,
                "question",
                adapter=adapter,
                config=ProviderConfig("gemini", "gemini-legacy-model"),
            )

            loaded = load_candidate_job(created.path)

        self.assertEqual("gemini-legacy-model", loaded.metadata["model"])
        self.assertNotIn("provider_profile", loaded.metadata)

    def test_candidate_boundary_rejects_profile_provider_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            question_path, asset_id = self.make_item(Path(temporary))
            paid = FakeAdapter(candidate())
            with self.assertRaises(CandidateValidationError):
                create_transcription_job(
                    question_path,
                    asset_id,
                    "question",
                    adapter=paid,
                    config=ProviderConfig("gemini", "legacy"),
                    provider_profile="codex_only",
                )
            codex = FakeAdapter(candidate())
            codex.provider_name = "codex"
            with self.assertRaises(CandidateValidationError):
                create_transcription_job(
                    question_path,
                    asset_id,
                    "question",
                    adapter=codex,
                    config=ProviderConfig("codex", "gpt-5.6-sol"),
                    provider_profile="full",
                )
            with self.assertRaises(CandidateValidationError):
                create_transcription_job(
                    question_path,
                    asset_id,
                    "question",
                    adapter=codex,
                    config=ProviderConfig("codex", "unapproved-model"),
                    provider_profile="codex_only",
                )
        self.assertEqual(0, paid.calls)
        self.assertEqual(0, codex.calls)

    def test_candidate_loader_rejects_rehashed_profile_provider_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            question_path, asset_id = self.make_item(root)
            full = create_transcription_job(
                question_path,
                asset_id,
                "question",
                adapter=FakeAdapter(candidate()),
            )
            full_metadata = json.loads((full.path / "job.json").read_text("utf-8"))
            full_metadata["provider"] = "codex"
            full_metadata["model"] = "gpt-5.6-sol"
            full_key = make_idempotency_key(
                full_metadata["input_sha256"],
                full_metadata["prompt_sha256"],
                full_metadata["schema_sha256"],
                full_metadata["model"],
            )
            full_metadata["idempotency_key"] = full_key
            full_metadata["job_id"] = f"question-{full_key[:32]}-r1"
            atomic_write_json(full.path / "job.json", full_metadata)
            tampered_full = full.path.parent / full_metadata["job_id"]
            full.path.rename(tampered_full)
            with self.assertRaises(CandidateIntegrityError):
                load_candidate_job(tampered_full)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            question_path, asset_id = self.make_item(root)
            codex_adapter = FakeAdapter(candidate())
            codex_adapter.provider_name = "codex"
            codex = create_transcription_job(
                question_path,
                asset_id,
                "question",
                adapter=codex_adapter,
                config=ProviderConfig("codex", "gpt-6-astra"),
                provider_profile="codex_only",
            )
            codex_metadata = json.loads((codex.path / "job.json").read_text("utf-8"))
            codex_metadata["provider"] = "gemini"
            codex_metadata["model"] = "gemini-3.7-flash"
            codex_key = make_idempotency_key(
                codex_metadata["input_sha256"],
                codex_metadata["prompt_sha256"],
                codex_metadata["schema_sha256"],
                codex_metadata["model"],
                provider_profile="codex_only",
            )
            codex_metadata["idempotency_key"] = codex_key
            codex_metadata["job_id"] = f"question-{codex_key[:32]}-r1"
            atomic_write_json(codex.path / "job.json", codex_metadata)
            tampered_codex = codex.path.parent / codex_metadata["job_id"]
            codex.path.rename(tampered_codex)
            with self.assertRaises(CandidateIntegrityError):
                load_candidate_job(tampered_codex)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            question_path, asset_id = self.make_item(root)
            codex_adapter = FakeAdapter(candidate())
            codex_adapter.provider_name = "codex"
            codex = create_transcription_job(
                question_path,
                asset_id,
                "question",
                adapter=codex_adapter,
                config=ProviderConfig("codex", "gpt-6-astra"),
                provider_profile="codex_only",
            )
            metadata = json.loads((codex.path / "job.json").read_text("utf-8"))
            metadata["model"] = "unapproved-model"
            key = make_idempotency_key(
                metadata["input_sha256"],
                metadata["prompt_sha256"],
                metadata["schema_sha256"],
                metadata["model"],
                provider_profile="codex_only",
            )
            metadata["idempotency_key"] = key
            metadata["job_id"] = f"question-{key[:32]}-r1"
            atomic_write_json(codex.path / "job.json", metadata)
            tampered = codex.path.parent / metadata["job_id"]
            codex.path.rename(tampered)
            with self.assertRaises(CandidateIntegrityError):
                load_candidate_job(tampered)

    def test_image_validation_is_fail_closed(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ImageValidationError):
                inspect_image(root / "missing.png")
            corrupt = root / "corrupt.png"
            corrupt.write_bytes(b"not an image")
            with self.assertRaises(ImageValidationError):
                inspect_image(corrupt)
            image_path = root / "valid.png"
            Image.new("RGB", (3, 2), "white").save(image_path)
            with self.assertRaises(ImageValidationError):
                inspect_image(image_path, expected_media_type="image/jpeg")
            with self.assertRaises(ImageValidationError):
                inspect_image(image_path, max_bytes=1)

    def test_job_is_parsed_validated_immutable_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            question_path, asset_id = self.make_item(Path(temporary))
            adapter = FakeAdapter(candidate())
            first = create_transcription_job(
                question_path, asset_id, "question", adapter=adapter
            )
            first_raw = (first.path / "raw.txt").read_bytes()
            second = create_transcription_job(
                question_path, asset_id, "question", adapter=adapter
            )
            self.assertEqual(1, adapter.calls)
            self.assertTrue(second.cached)
            self.assertEqual(first.path, second.path)
            self.assertEqual(first_raw, (second.path / "raw.txt").read_bytes())
            self.assertEqual("1 m/s", second.candidate["blocks"][1]["items"][0]["runs"][0]["text"])
            self.assertIn("never solve or generate an explanation", adapter.last_prompt)
            self.assertEqual(
                second.metadata["idempotency_key"][:32], second.path.name.split("-")[-2]
            )

    def test_question_source_header_is_removed_and_underline_marks_survive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            question_path, asset_id = self.make_item(Path(temporary))
            marked = candidate()
            marked["blocks"][0]["runs"] = [
                {"type": "text", "text": "1 수능 예시 문제\n"},
                {"type": "text", "text": "㉠", "marks": ["underline"]},
                {"type": "text", "text": "의 속력은 "},
                {
                    "type": "equation",
                    "source_text": "v=d/t",
                    "latex": "v=\\frac{d}{t}",
                },
            ]
            adapter = FakeAdapter(marked)
            job = create_transcription_job(
                question_path,
                asset_id,
                "question",
                adapter=adapter,
            )
            saved = apply_candidate_job(
                question_path,
                job.path,
                expected_base_revision=1,
            )

            first = saved["content"]["question"]["blocks"][0]["inlines"]
            self.assertEqual("㉠", first[0]["text"])
            self.assertEqual(["underline"], first[0]["marks"])
            self.assertNotIn("marks", first[1])
            self.assertEqual(
                "1 수능 예시 문제\n",
                job.candidate["blocks"][0]["runs"][0]["text"],
            )
            self.assertFalse(
                any(
                    "source header" in issue.lower()
                    for issue in saved["review"]["issues"]
                )
            )
            self.assertIn("metadata only", adapter.last_prompt)
            self.assertIn('marks: ["underline"]', adapter.last_prompt)
            self.assertIn("never correctness or the answer", adapter.last_prompt)

    def test_candidate_schema_rejects_non_underline_ocr_marks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            question_path, asset_id = self.make_item(Path(temporary))
            invalid = candidate()
            invalid["blocks"][0]["runs"][0]["marks"] = ["bold"]
            with self.assertRaises(CandidateValidationError):
                create_transcription_job(
                    question_path,
                    asset_id,
                    "question",
                    adapter=FakeAdapter(invalid),
                )

    def test_leading_artifact_normalization_is_conservative(self) -> None:
        cases = (
            (
                "compact-badge",
                "15",
                "15 수능 예시 문제\n물체 15개의 운동을 설명한 것은?",
                "물체 15개의 운동을 설명한 것은?",
            ),
            (
                "grade-badge",
                "12",
                "12 등급UP\n그림에서 12 m를 이동하였다.",
                "그림에서 12 m를 이동하였다.",
            ),
            (
                "identifier-only",
                "A05",
                "A05: 다음 설명으로 옳은 것은?",
                "다음 설명으로 옳은 것은?",
            ),
        )
        for name, printed_number, source, expected in cases:
            with self.subTest(name=name):
                value = candidate()
                value["blocks"][0]["runs"] = [{"type": "text", "text": source}]
                normalized = normalize_candidate_for_apply(
                    value,
                    item_id=f"item-{name}",
                    section="question",
                    source_asset_id="source-question",
                    id_seed=name,
                    printed_number=printed_number,
                )
                self.assertEqual(
                    expected,
                    normalized["section"]["blocks"][0]["inlines"][0]["text"],
                )

        semantic_heading = candidate()
        semantic_heading["blocks"][0]["runs"] = [
            {"type": "text", "text": "핵심 자료 1 태양 에너지의 생성"}
        ]
        retained = normalize_candidate_for_apply(
            semantic_heading,
            item_id="item-semantic-heading",
            section="question",
            source_asset_id="source-question",
            id_seed="semantic-heading",
            printed_number="01",
        )
        self.assertEqual(
            "핵심 자료 1 태양 에너지의 생성",
            retained["section"]["blocks"][0]["inlines"][0]["text"],
        )
        self.assertEqual(
            "① 1 m/s",
            retained["section"]["blocks"][1]["items"][0]["blocks"][0][
                "inlines"
            ][0]["text"],
        )

    def test_standalone_source_metadata_heading_is_removed(self) -> None:
        value = candidate()
        value["blocks"].insert(
            0,
            {
                "type": "paragraph",
                "role": "heading",
                "runs": [
                    {
                        "type": "text",
                        "text": "05 기출 문제 2025학년도 10월 고1 학평",
                    }
                ],
                "bbox": {
                    "left": 0.1,
                    "top": 0.01,
                    "right": 0.9,
                    "bottom": 0.08,
                },
                "confidence": 0.99,
            },
        )
        normalized = normalize_candidate_for_apply(
            value,
            item_id="item-source-heading",
            section="question",
            source_asset_id="source-question",
            id_seed="source-heading",
            printed_number="05",
        )
        self.assertEqual("stem", normalized["section"]["blocks"][0]["role"])
        self.assertFalse(
            any("source header" in issue.lower() for issue in normalized["issues"])
        )

    def test_audited_exam_source_lines_are_removed_only_as_headings(self) -> None:
        cases = (
            ("M04", "M04 2023 대비 수능 6 / 물Ⅱ 변형"),
            ("B05", "B05 2023 실시 6월 학평 2 / 물 I (고2)"),
            ("B39", "B39 *** 2028 대비 수능 예시 20(2차)"),
            ("Ⅱ01", "Ⅱ 01 수능 대비 기출"),
        )
        for index, (printed_number, source_line) in enumerate(cases, 1):
            with self.subTest(source_line=source_line):
                value = candidate()
                value["blocks"].insert(
                    0,
                    {
                        "type": "paragraph",
                        "role": "heading",
                        "runs": [{"type": "text", "text": source_line}],
                        "bbox": {
                            "left": 0.1,
                            "top": 0.01,
                            "right": 0.9,
                            "bottom": 0.08,
                        },
                        "confidence": 0.99,
                    },
                )
                normalized = normalize_candidate_for_apply(
                    value,
                    item_id=f"item-source-{index}",
                    section="question",
                    source_asset_id="source-question",
                    id_seed=printed_number,
                    printed_number=printed_number,
                )
                self.assertEqual(
                    "물체의 속력은 ",
                    normalized["section"]["blocks"][0]["inlines"][0]["text"],
                )

        sentence = candidate()
        sentence["blocks"][0]["role"] = "stem"
        sentence["blocks"][0]["runs"] = [
            {
                "type": "text",
                "text": "2023년 수능 대비 학습 방법을 설명한 문장이다.",
            }
        ]
        retained = normalize_candidate_for_apply(
            sentence,
            item_id="item-source-sentence",
            section="question",
            source_asset_id="source-question",
            id_seed="source-sentence",
            printed_number="M04",
        )
        self.assertEqual(
            "2023년 수능 대비 학습 방법을 설명한 문장이다.",
            retained["section"]["blocks"][0]["inlines"][0]["text"],
        )

    def test_canonical_header_cleanup_is_pure_and_keeps_source_text(self) -> None:
        document = {
            "identity": {"printed_number": "M04"},
            "content": {
                "question": {
                    "blocks": [
                        {
                            "type": "paragraph",
                            "role": "heading",
                            "inlines": [
                                {
                                    "type": "text",
                                    "text": "M04 2023 대비 수능 6 / 물Ⅱ 변형",
                                    "source_text": "M04 2023 대비 수능 6 / 물Ⅱ 변형",
                                }
                            ],
                        },
                        {
                            "type": "paragraph",
                            "role": "stem",
                            "inlines": [
                                {
                                    "type": "text",
                                    "text": "다음 설명으로 옳은 것은?",
                                    "source_text": "다음 설명으로 옳은 것은?",
                                }
                            ],
                        },
                    ]
                }
            },
        }
        untouched = copy.deepcopy(document)
        cleaned, removals, before, after = normalize_canonical_question_headers(
            document
        )
        self.assertEqual(untouched, document)
        self.assertEqual("M04 2023 대비 수능 6 / 물Ⅱ 변형", before)
        self.assertEqual("다음 설명으로 옳은 것은?", after)
        self.assertEqual(["printed_number", "출처 메타데이터"], removals)
        self.assertEqual(
            "다음 설명으로 옳은 것은?",
            cleaned["content"]["question"]["blocks"][0]["inlines"][0][
                "source_text"
            ],
        )

        inline_document = {
            "identity": {"printed_number": "03"},
            "content": {
                "question": {
                    "blocks": [
                        {
                            "type": "paragraph",
                            "role": "stem",
                            "inlines": [
                                {
                                    "type": "text",
                                    "text": "03 수능 예시 문제\n다음은 실제 문제이다.",
                                    "source_text": "03 수능 예시 문제\n다음은 실제 문제이다.",
                                }
                            ],
                        }
                    ]
                }
            },
        }
        inline_cleaned, _, _, _ = normalize_canonical_question_headers(
            inline_document
        )
        inline = inline_cleaned["content"]["question"]["blocks"][0]["inlines"][0]
        self.assertEqual("다음은 실제 문제이다.", inline["text"])
        self.assertEqual("다음은 실제 문제이다.", inline["source_text"])

    def test_canonical_number_after_leading_shared_blocks_is_removed(self) -> None:
        document = {
            "identity": {"printed_number": "03"},
            "content": {
                "question": {
                    "blocks": [
                        {
                            "type": "box",
                            "role": "passage",
                            "blocks": [
                                {
                                    "type": "paragraph",
                                    "role": "body",
                                    "inlines": [
                                        {
                                            "type": "text",
                                            "text": "공통 자료 03은 그대로 둔다.",
                                            "source_text": "공통 자료 03은 그대로 둔다.",
                                        }
                                    ],
                                }
                            ],
                        },
                        {"type": "figure", "role": "main"},
                        {
                            "type": "paragraph",
                            "role": "stem",
                            "inlines": [
                                {
                                    "type": "text",
                                    "text": "03 환경 변화 과정을 설명한 것이다.",
                                    "source_text": "03 환경 변화 과정을 설명한 것이다.",
                                }
                            ],
                        },
                    ]
                }
            },
        }

        cleaned, removals, before, after = normalize_canonical_question_headers(
            document
        )

        self.assertEqual(["printed_number"], removals)
        self.assertEqual("03 환경 변화 과정을 설명한 것이다.", before)
        self.assertEqual("환경 변화 과정을 설명한 것이다.", after)
        nested = cleaned["content"]["question"]["blocks"][0]["blocks"][0]
        self.assertEqual("공통 자료 03은 그대로 둔다.", nested["inlines"][0]["text"])
        stem = cleaned["content"]["question"]["blocks"][2]["inlines"][0]
        self.assertEqual("환경 변화 과정을 설명한 것이다.", stem["text"])
        self.assertEqual("환경 변화 과정을 설명한 것이다.", stem["source_text"])

    def test_subquestion_explanation_prompt_excludes_sibling_answers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            question_path, asset_id = self.make_item(
                Path(temporary), section="explanation"
            )
            document = json.loads(question_path.read_text(encoding="utf-8"))
            document["identity"]["printed_number"] = "H19(1)"
            atomic_write_json(question_path, document)
            adapter = FakeAdapter(candidate(section="explanation"))
            job = create_transcription_job(
                question_path,
                asset_id,
                "explanation",
                adapter=adapter,
            )
            self.assertIn('Canonical target identifier: "H19(1)"', adapter.last_prompt)
            self.assertIn("reason sibling_subquestion", adapter.last_prompt)
            self.assertIn("Never merge a sibling's answer", adapter.last_prompt)
            self.assertEqual("transcribe-exam-crop-v5", job.metadata["prompt_version"])

    def test_shared_parent_prompt_has_distinct_identity_and_no_child_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            question_path, asset_id = self.make_item(
                Path(temporary), section="explanation"
            )
            document = json.loads(question_path.read_text(encoding="utf-8"))
            document["identity"]["printed_number"] = "H19(1)"
            atomic_write_json(question_path, document)
            adapter = FakeAdapter(candidate(section="explanation"))
            job = create_transcription_job(
                question_path,
                asset_id,
                "explanation",
                adapter=adapter,
                shared_parent_mode=True,
            )
            self.assertTrue(
                adapter.last_prompt.startswith(
                    "# Shared-parent explanation transcription — constraints "
                    "(highest priority)"
                )
            )
            self.assertIn("Set the top-level answer to null", adapter.last_prompt)
            self.assertIn("Never solve, infer, complete", adapter.last_prompt)
            self.assertNotIn("Canonical target identifier", adapter.last_prompt)
            self.assertNotIn("sibling_subquestion", adapter.last_prompt)
            self.assertNotIn("only that target's labeled answer", adapter.last_prompt)
            self.assertTrue(job.metadata["shared_parent_mode"])
            self.assertEqual(
                "transcribe-exam-crop-v5-shared-parent-v1",
                job.metadata["prompt_version"],
            )

            normal = create_transcription_job(
                question_path,
                asset_id,
                "explanation",
                adapter=adapter,
            )
            self.assertNotEqual(
                normal.metadata["idempotency_key"], job.metadata["idempotency_key"]
            )
            saved = apply_candidate_job(
                question_path, job.path, expected_base_revision=1
            )
            self.assertIsNone(saved["content"]["answer"])

    def test_cross_item_clone_rejects_nonshared_candidate_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path, source_asset_id = self.make_item(
                root / "source", section="explanation"
            )
            target_path, target_asset_id = self.make_item(
                root / "target", section="explanation"
            )
            normal_job = create_transcription_job(
                source_path,
                source_asset_id,
                "explanation",
                adapter=FakeAdapter(candidate(section="explanation")),
            )
            with self.assertRaisesRegex(
                CandidateIntegrityError, "only shared-parent candidate jobs"
            ):
                clone_candidate_job_for_item(
                    target_path,
                    normal_job,
                    target_asset_id,
                    expected_base_revision=1,
                )

    def test_answer_only_crop_can_fill_answer_without_promoting_solution_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            question_path, asset_id = self.make_item(
                Path(temporary), section="answer"
            )
            document = json.loads(question_path.read_text(encoding="utf-8"))
            document["provenance"]["solution_links"] = [
                {
                    "kind": "quick_answer",
                    "status": "review",
                    "source_asset_id": asset_id,
                }
            ]
            atomic_write_json(question_path, document)
            answer_only = {
                "schema_version": "0.1.0",
                "section": "explanation",
                "blocks": [
                    {
                        "type": "paragraph",
                        "role": "answer",
                        "runs": [{"type": "text", "text": "정답: ⑤"}],
                        "bbox": {
                            "left": 0.1,
                            "top": 0.1,
                            "right": 0.9,
                            "bottom": 0.5,
                        },
                        "confidence": 0.99,
                    }
                ],
                "answer": {
                    "kind": "content",
                    "choice_labels": [],
                    "runs": [{"type": "text", "text": "⑤"}],
                    "true_false": [],
                },
                "coverage_complete": True,
                "ignored_regions": [],
                "warnings": [],
            }
            adapter = FakeAdapter(answer_only)
            job = create_transcription_job(
                question_path,
                asset_id,
                "explanation",
                adapter=adapter,
            )
            saved = apply_candidate_job(
                question_path, job.path, expected_base_revision=1
            )

            self.assertIn("store the answer and actual explanation only", adapter.last_prompt)
            self.assertEqual("content", saved["content"]["answer"]["kind"])
            self.assertEqual(
                "⑤",
                saved["content"]["answer"]["blocks"][0]["inlines"][0]["text"],
            )
            self.assertEqual(
                "review", saved["provenance"]["solution_links"][0]["status"]
            )

    def test_structured_answer_only_candidate_can_have_no_explanation_blocks(self) -> None:
        answer_only = candidate(section="explanation")
        answer_only["blocks"] = []
        answer_only["answer"] = {
            "kind": "choice",
            "choice_labels": ["②"],
            "runs": [],
            "true_false": [],
        }
        normalized = normalize_candidate_for_apply(
            answer_only,
            item_id="item-1",
            section="explanation",
            source_asset_id="source-answer",
            id_seed="structured-answer-only",
            existing_choice_ids={"②": "choice-2"},
        )
        self.assertEqual([], normalized["section"]["blocks"])
        self.assertEqual(["choice-2"], normalized["answer"]["choice_ids"])
        self.assertTrue(any("정답만 저장" in issue for issue in normalized["issues"]))

        answer_only["answer"] = {
            "kind": "unknown",
            "choice_labels": [],
            "runs": [],
            "true_false": [],
        }
        with self.assertRaises(CandidateValidationError):
            normalize_candidate_for_apply(
                answer_only,
                item_id="item-1",
                section="explanation",
                source_asset_id="source-answer",
                id_seed="empty-unknown-answer",
                existing_choice_ids={"②": "choice-2"},
            )

    def test_invalid_candidate_creates_no_published_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            question_path, asset_id = self.make_item(Path(temporary))
            invalid = candidate()
            invalid["blocks"][0]["bbox"]["right"] = 1.05
            with self.assertRaises(CandidateValidationError):
                create_transcription_job(
                    question_path, asset_id, "question", adapter=FakeAdapter(invalid)
                )
            jobs = question_path.parent / "jobs" / "transcription"
            self.assertFalse(jobs.exists())

    def test_zero_size_bbox_is_minimally_expanded_and_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            question_path, asset_id = self.make_item(Path(temporary))
            degenerate = candidate()
            bbox = degenerate["blocks"][0]["bbox"]
            bbox["right"] = bbox["left"]
            bbox["bottom"] = bbox["top"]
            job = create_transcription_job(
                question_path,
                asset_id,
                "question",
                adapter=FakeAdapter(degenerate),
            )
            repaired = job.candidate["blocks"][0]["bbox"]
            self.assertGreater(repaired["right"], repaired["left"])
            self.assertGreater(repaired["bottom"], repaired["top"])
            self.assertTrue(
                any("zero-size bbox" in warning for warning in job.candidate["warnings"])
            )

    def test_inverted_bbox_endpoints_are_reordered_and_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            question_path, asset_id = self.make_item(Path(temporary))
            inverted = candidate()
            bbox = inverted["blocks"][0]["bbox"]
            bbox["left"], bbox["right"] = bbox["right"], bbox["left"]
            bbox["top"], bbox["bottom"] = bbox["bottom"], bbox["top"]
            job = create_transcription_job(
                question_path,
                asset_id,
                "question",
                adapter=FakeAdapter(inverted),
            )
            repaired = job.candidate["blocks"][0]["bbox"]
            self.assertLess(repaired["left"], repaired["right"])
            self.assertLess(repaired["top"], repaired["bottom"])
            self.assertTrue(
                any("inverted bbox" in warning for warning in job.candidate["warnings"])
            )

    def test_normalize_is_stable_and_apply_is_revision_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            question_path, asset_id = self.make_item(Path(temporary))
            original_candidate = candidate(coverage=False)
            untouched = copy.deepcopy(original_candidate)
            normalized_a = normalize_candidate_for_apply(
                original_candidate,
                item_id="item-1",
                section="question",
                source_asset_id=asset_id,
                id_seed="same-job",
            )
            normalized_b = normalize_candidate_for_apply(
                original_candidate,
                item_id="item-1",
                section="question",
                source_asset_id=asset_id,
                id_seed="same-job",
            )
            self.assertEqual(untouched, original_candidate)
            self.assertEqual(normalized_a, normalized_b)
            first_block = normalized_a["section"]["blocks"][0]
            self.assertTrue(first_block["block_id"].startswith("block-"))
            self.assertEqual(asset_id, first_block["source_region"]["source_asset_id"])
            self.assertEqual("ratio", first_block["source_region"]["bbox"]["unit"])
            self.assertTrue(first_block["inlines"][1]["hwp_script"])

            job = create_transcription_job(
                question_path,
                asset_id,
                "question",
                adapter=FakeAdapter(original_candidate),
            )
            saved = apply_candidate_job(
                question_path, job.path, expected_base_revision=1
            )
            self.assertEqual(2, saved["workflow"]["revision"])
            self.assertEqual("needs_review", saved["workflow"]["status"])
            self.assertEqual("incomplete", saved["review"]["coverage"]["question"])
            self.assertEqual(
                saved["content"]["question"]["blocks"][1]["items"][1]["choice_id"],
                saved["content"]["answer"]["choice_ids"][0],
            )
            self.assertEqual(1, len(saved["provenance"]["transcriptions"]))
            figure = saved["content"]["question"]["blocks"][2]
            self.assertEqual("pure_figure", figure["visual_kind"])
            self.assertEqual("body", figure["placement_context"])
            self.assertEqual([], figure["embedded_text_runs"])
            self.assertEqual("original_only", figure["regeneration_policy"])
            self.assertIsNone(figure["regeneration_note"])
            figure_asset = next(
                asset
                for asset in saved["resources"]["assets"]
                if asset["asset_id"] == figure["asset_id"]
            )
            self.assertEqual("figure_original", figure_asset["role"])
            self.assertEqual(asset_id, figure_asset["derived_from_asset_id"])
            self.assertTrue((question_path.parent / figure_asset["path"]).is_file())
            with self.assertRaises(RevisionConflict):
                apply_candidate_job(
                    question_path, job.path, expected_base_revision=1
                )
            with self.assertRaises(CandidateBaseRevisionConflict):
                apply_candidate_job(
                    question_path, job.path, expected_base_revision=2
                )

    def test_figure_candidate_metadata_is_preserved_canonically(self) -> None:
        value = candidate()
        figure = value["blocks"][2]
        figure.update(
            figure_kind="graph",
            regeneration_policy="ai_allowed",
            labels=[
                {
                    "text": "속력",
                    "bbox": {"left": 0.22, "top": 0.24, "right": 0.3, "bottom": 0.29},
                }
            ],
            regeneration_note="축과 단위를 그대로 보존",
        )

        normalized = normalize_candidate_for_apply(
            value,
            item_id="item-metadata",
            section="question",
            source_asset_id="source-question",
            id_seed="metadata",
        )
        canonical = normalized["section"]["blocks"][2]

        self.assertEqual("pure_figure", canonical["visual_kind"])
        self.assertEqual("body", canonical["placement_context"])
        self.assertEqual("ai_allowed", canonical["regeneration_policy"])
        self.assertEqual("축과 단위를 그대로 보존", canonical["regeneration_note"])
        self.assertEqual("속력", canonical["embedded_text_runs"][0]["text"])
        self.assertEqual(
            "ratio", canonical["embedded_text_runs"][0]["bbox"]["unit"]
        )

        figure.update(figure_kind="table_image", regeneration_policy="ai_allowed")
        table = normalize_candidate_for_apply(
            value,
            item_id="item-table-metadata",
            section="question",
            source_asset_id="source-question",
            id_seed="table-metadata",
        )["section"]["blocks"][2]
        self.assertEqual("table_snapshot", table["visual_kind"])
        self.assertEqual("original_only", table["regeneration_policy"])

        choice_value = candidate()
        choice_value["blocks"][1]["items"][0]["figure_bbox"] = {
            "left": 0.2,
            "top": 0.45,
            "right": 0.4,
            "bottom": 0.7,
        }
        choice = normalize_candidate_for_apply(
            choice_value,
            item_id="item-choice-metadata",
            section="question",
            source_asset_id="source-question",
            id_seed="choice-metadata",
        )["section"]["blocks"][1]["items"][0]["blocks"][1]
        self.assertEqual("choice", choice["placement_context"])
        self.assertEqual("unknown", choice["visual_kind"])
        self.assertEqual("original_only", choice["regeneration_policy"])

    def test_automatic_explanation_apply_can_preserve_an_existing_answer(self) -> None:
        cases = (
            ("preserve", {"kind": "unknown"}, True, "unknown"),
            ("fill-null", None, True, "choice"),
            ("manual-default", {"kind": "unknown"}, False, "choice"),
        )
        for name, existing_answer, preserve, expected_kind in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                question_path, job = self.make_item_with_explanation_job(
                    Path(temporary), existing_answer=existing_answer
                )
                saved = apply_candidate_job(
                    question_path,
                    job.path,
                    expected_base_revision=3,
                    preserve_existing_answer=preserve,
                )
                self.assertEqual(expected_kind, saved["content"]["answer"]["kind"])

    def test_explanation_allows_repeated_statement_labels_in_separate_groups(self) -> None:
        explanation = candidate(section="explanation")
        group = {
            "type": "bogi",
            "title": "보기",
            "title_visible": True,
            "items": [
                {
                    "label": "ㄱ",
                    "runs": [{"type": "text", "text": "첫 설명"}],
                    "figure_bbox": None,
                }
            ],
            "bbox": {"left": 0.1, "top": 0.1, "right": 0.9, "bottom": 0.3},
            "confidence": 0.95,
        }
        explanation["blocks"] = [copy.deepcopy(group), copy.deepcopy(group)]
        choices = {
            "type": "choices",
            "source_layout": "vertical",
            "items": [
                {
                    "label": "①",
                    "runs": [{"type": "text", "text": "선택지 설명"}],
                    "figure_bbox": None,
                }
            ],
            "bbox": {"left": 0.1, "top": 0.4, "right": 0.9, "bottom": 0.6},
            "confidence": 0.95,
        }
        explanation["blocks"].extend([copy.deepcopy(choices), copy.deepcopy(choices)])
        explanation["answer"] = {
            "kind": "choice",
            "choice_labels": ["①"],
            "runs": [],
            "true_false": [],
        }
        normalized = normalize_candidate_for_apply(
            explanation,
            item_id="item-1",
            section="explanation",
            source_asset_id="source-explanation",
            id_seed="duplicate-labels",
            existing_choice_ids={"①": "question-choice"},
            existing_statement_ids={"ㄱ": "question-statement"},
        )
        ids = [
            block["items"][0]["item_id"]
            for block in normalized["section"]["blocks"]
            if block["type"] == "bogi"
        ]
        self.assertEqual(2, len(set(ids)))
        self.assertEqual(["question-choice"], normalized["answer"]["choice_ids"])

    def test_all_unlabeled_bogi_items_flatten_to_box_and_preserve_figures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            question_path, asset_id = self.make_item(
                Path(temporary), section="explanation"
            )
            explanation = candidate(section="explanation")
            explanation["blocks"] = [
                {
                    "type": "box",
                    "role": "data",
                    "title": "자료 분석",
                    "runs": [],
                    "items": [
                        {
                            "label": "",
                            "runs": [{"type": "text", "text": "첫 번째 자료"}],
                            "figure_bbox": {
                                "left": 0.1,
                                "top": 0.2,
                                "right": 0.45,
                                "bottom": 0.8,
                            },
                        },
                        {
                            "label": "",
                            "runs": [],
                            "figure_bbox": {
                                "left": 0.55,
                                "top": 0.2,
                                "right": 0.9,
                                "bottom": 0.8,
                            },
                        },
                    ],
                    "bbox": {
                        "left": 0.05,
                        "top": 0.1,
                        "right": 0.95,
                        "bottom": 0.9,
                    },
                    "confidence": 0.96,
                }
            ]
            explanation["answer"] = None
            job = create_transcription_job(
                question_path,
                asset_id,
                "explanation",
                adapter=FakeAdapter(explanation),
            )
            saved = apply_candidate_job(
                question_path, job.path, expected_base_revision=1
            )

            outer = saved["content"]["explanation"]["blocks"][0]
            self.assertEqual("box", outer["type"])
            self.assertEqual(["paragraph", "figure", "figure"], [
                block["type"] for block in outer["blocks"]
            ])
            self.assertFalse(
                any(block["type"] == "bogi" for block in outer["blocks"])
            )
            figures = [block for block in outer["blocks"] if block["type"] == "figure"]
            self.assertEqual(2, len(figures))
            self.assertEqual(2, len({block["asset_id"] for block in figures}))
            self.assertTrue(
                all(block["placement_context"] == "shared_box" for block in figures)
            )
            self.assertTrue(
                any("라벨 없는 보기" in issue for issue in saved["review"]["issues"])
            )
            strict_errors = PackageValidator(
                question_path, check_files=True
            ).validate(saved)
            self.assertFalse(
                any("content.explanation" in error for error in strict_errors),
                strict_errors,
            )

    def test_mixed_empty_and_visible_bogi_labels_fail_without_inference(self) -> None:
        explanation = candidate(section="explanation")
        bogi = {
            "type": "bogi",
            "title": "",
            "title_visible": False,
            "items": [
                {
                    "label": "",
                    "runs": [{"type": "text", "text": "무라벨 자료"}],
                    "figure_bbox": None,
                },
                {
                    "label": "(2)",
                    "runs": [{"type": "text", "text": "라벨 자료"}],
                    "figure_bbox": None,
                },
            ],
            "bbox": {"left": 0.1, "top": 0.1, "right": 0.9, "bottom": 0.9},
            "confidence": 0.9,
        }
        explanation["blocks"] = [bogi]
        explanation["answer"] = None
        with self.assertRaisesRegex(
            CandidateValidationError, "mixes labeled and unlabeled"
        ):
            normalize_candidate_for_apply(
                explanation,
                item_id="item-1",
                section="explanation",
                source_asset_id="source-explanation",
                id_seed="mixed-empty-labels",
            )

    def test_ascii_choice_label_maps_only_to_unique_existing_circled_label(self) -> None:
        explanation = candidate(section="explanation")
        explanation["answer"]["choice_labels"] = ["5"]
        normalized = normalize_candidate_for_apply(
            explanation,
            item_id="item-1",
            section="explanation",
            source_asset_id="source-explanation",
            id_seed="ascii-choice-label",
            existing_choice_ids={"⑤": "choice-5"},
        )
        self.assertEqual(["choice-5"], normalized["answer"]["choice_ids"])

        with self.assertRaises(CandidateValidationError):
            normalize_candidate_for_apply(
                explanation,
                item_id="item-1",
                section="explanation",
                source_asset_id="source-explanation",
                id_seed="missing-circled-label",
                existing_choice_ids={"④": "choice-4"},
            )

        ambiguous = _collect_choice_ids(
            {
                "blocks": [
                    {
                        "type": "choices",
                        "items": [{"label": "⑤", "choice_id": "choice-5-a"}],
                    },
                    {
                        "type": "choices",
                        "items": [{"label": "⑤", "choice_id": "choice-5-b"}],
                    },
                ]
            }
        )
        self.assertNotIn("⑤", ambiguous)
        with self.assertRaises(CandidateValidationError):
            normalize_candidate_for_apply(
                explanation,
                item_id="item-1",
                section="explanation",
                source_asset_id="source-explanation",
                id_seed="ambiguous-circled-label",
                existing_choice_ids=ambiguous,
            )

    def test_visible_unlinked_explanation_choice_is_preserved_as_content(self) -> None:
        explanation = candidate(section="explanation")
        explanation["answer"] = {
            "kind": "choice",
            "choice_labels": ["ㄴ", "ㄹ"],
            "runs": [{"type": "text", "text": "ㄴ, ㄹ"}],
            "true_false": [],
        }
        normalized = normalize_candidate_for_apply(
            explanation,
            item_id="item-1",
            section="explanation",
            source_asset_id="source-explanation",
            id_seed="visible-short-answer",
            existing_statement_ids={"ㄱ": "statement-1", "ㄴ": "statement-2"},
        )
        self.assertEqual("content", normalized["answer"]["kind"])
        self.assertEqual(
            "ㄴ, ㄹ", normalized["answer"]["blocks"][0]["inlines"][0]["text"]
        )
        self.assertTrue(
                any("보이는 정답 글자를 그대로 저장" in issue for issue in normalized["issues"])
        )

        explanation["answer"]["runs"] = []
        with self.assertRaises(CandidateValidationError):
            normalize_candidate_for_apply(
                explanation,
                item_id="item-1",
                section="explanation",
                source_asset_id="source-explanation",
                id_seed="missing-visible-short-answer",
            )

        question = copy.deepcopy(explanation)
        question["section"] = "question"
        question["answer"]["runs"] = [{"type": "text", "text": "ㄴ, ㄹ"}]
        with self.assertRaises(CandidateValidationError):
            normalize_candidate_for_apply(
                question,
                item_id="item-1",
                section="question",
                source_asset_id="source-question",
                id_seed="question-choice-remains-strict",
            )

    def test_non_statement_bogi_labels_can_repeat_without_blocking_answers(self) -> None:
        question = candidate()
        bullet = {
            "type": "bogi",
            "title": "",
            "title_visible": False,
            "items": [
                {
                    "label": "•",
                    "runs": [{"type": "text", "text": "장식 글머리 내용"}],
                    "figure_bbox": None,
                }
            ],
            "bbox": {"left": 0.1, "top": 0.1, "right": 0.9, "bottom": 0.3},
            "confidence": 0.95,
        }
        choices = copy.deepcopy(question["blocks"][1])
        choices["items"] = [
            {
                "label": "④",
                "runs": [{"type": "text", "text": "④ 네 번째 선택지"}],
                "figure_bbox": None,
            }
        ]
        speaker = copy.deepcopy(bullet)
        speaker["items"][0].update(label="교사")
        question["blocks"] = [
            copy.deepcopy(bullet),
            copy.deepcopy(bullet),
            copy.deepcopy(speaker),
            copy.deepcopy(speaker),
            choices,
        ]
        question["answer"]["choice_labels"] = ["④"]
        normalized_question = normalize_candidate_for_apply(
            question,
            item_id="item-1",
            section="question",
            source_asset_id="source-question",
            id_seed="decorative-bullets",
        )
        bogi_labels = [
            block["items"][0]["label"]
            for block in normalized_question["section"]["blocks"]
            if block["type"] == "bogi"
        ]
        self.assertEqual(["•", "•", "교사", "교사"], bogi_labels)
        self.assertEqual({}, _collect_statement_ids(normalized_question["section"]))
        for marker in ("ㄱ", "(ㄴ)", "ㄷ.", "㉠", "(㉡)."):
            with self.subTest(marker=marker):
                self.assertEqual(
                    {marker: "statement"},
                    _collect_statement_ids(
                        {
                            "type": "bogi",
                            "items": [{"label": marker, "item_id": "statement"}],
                        }
                    ),
                )

        choice_ids = _collect_choice_ids(normalized_question["section"])
        explanation = candidate(section="explanation")
        explanation["answer"]["choice_labels"] = ["4"]
        normalized_explanation = normalize_candidate_for_apply(
            explanation,
            item_id="item-1",
            section="explanation",
            source_asset_id="source-explanation",
            id_seed="answer-after-decorative-bullets",
            existing_choice_ids=choice_ids,
        )
        self.assertEqual(
            normalized_question["answer"]["choice_ids"],
            normalized_explanation["answer"]["choice_ids"],
        )

        duplicate_real_label = copy.deepcopy(question)
        for block in duplicate_real_label["blocks"][:2]:
            block["items"][0]["label"] = "ㄱ"
        with self.assertRaisesRegex(
            CandidateValidationError, "duplicate statement label"
        ):
            normalize_candidate_for_apply(
                duplicate_real_label,
                item_id="item-1",
                section="question",
                source_asset_id="source-question",
                id_seed="duplicate-real-label",
            )

    def test_empty_explanation_box_is_ignored_with_review_issue(self) -> None:
        explanation = candidate(section="explanation")
        explanation["blocks"].append(
            {
                "type": "box",
                "role": "data",
                "title": None,
                "runs": [],
                "items": [],
                "bbox": {"left": 0.1, "top": 0.8, "right": 0.9, "bottom": 0.9},
                "confidence": 0.8,
            }
        )
        titled = copy.deepcopy(explanation["blocks"][-1])
        titled["title"] = "[실험 결과]"
        explanation["blocks"].append(titled)
        normalized = normalize_candidate_for_apply(
            explanation,
            item_id="item-1",
            section="explanation",
            source_asset_id="source-explanation",
            id_seed="empty-box",
            existing_choice_ids={"①": "choice-1", "②": "choice-2"},
        )
        self.assertNotIn("box", [block["type"] for block in normalized["section"]["blocks"]])
        self.assertTrue(any("Ignored empty OCR box" in issue for issue in normalized["issues"]))
        self.assertTrue(any("Flattened title-only OCR box" in issue for issue in normalized["issues"]))
        self.assertTrue(
            any(
                block.get("role") == "heading"
                and block["inlines"][0].get("text") == "[실험 결과]"
                for block in normalized["section"]["blocks"]
            )
        )


if __name__ == "__main__":
    unittest.main()
