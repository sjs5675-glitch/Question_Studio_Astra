"""Asynchronous PDF -> crop -> import -> OCR batch coordination.

The crop application remains a separate local process.  This module only
submits a bounded request to its fixed loopback API, imports the completed
leaf run, and applies transcription candidates to verified items.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, assert_never
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from astra_runtime import inside
from astra_page_failure import parse_page_failure, page_failure_message
from . import ASTRA
from .importer import import_leaf_run
from .figure_generation import (
    FIGURE_PROFILES,
    IMAGE_SIZE,
    PRICING_AS_OF,
    FigureGenerationAdapter,
    FigureGenerationError,
    FigureProviderNotConfigured,
    FigureProviderRequestError,
    GeminiFigureAdapter,
    GeneratedImage,
    get_or_create_figure_artifact,
    inspect_figure_generation_inputs,
    materialize_figure_artifact_cas,
    regenerate_figure_cas,
)
from .model import (
    ModelError,
    RevisionConflict,
    atomic_write_json,
    figure_generation_summary,
    inspect_figure_regeneration_target,
    load_json_object,
    save_json_cas,
    sha256_file,
    solution_content_hash,
    validate_id,
)
from .question_bank import QuestionBankError, resolve_bank_item_refs
from .pdf_uploads import PdfUploadError, delete_pdf_upload, resolve_pdf_upload
from .solution_reconciliation import (
    SolutionIntegrityError,
    enrich_project_from_leaf,
    record_source_reconciliation,
)
from .transcription import (
    AstraTranscriptionError,
    CandidateJob,
    CandidateIntegrityError,
    CandidateValidationError,
    CodexRateLimitError,
    PROVIDER_DEFAULTS,
    ProviderConfig,
    ProviderNotConfigured,
    SHARED_PARENT_PROMPT_VERSION,
    apply_candidate_job,
    clone_candidate_job_for_item,
    clone_explanation_content_cas,
    create_transcription_job,
    effective_provider_config,
    load_candidate_job,
    parse_provider_profile,
    shared_analysis_key,
    analysis_attempts,
    load_analysis_request,
    load_analysis_response,
)


CROP_API = ASTRA.crop_api
ACTIVE_STATES = {
    "queued",
    "starting_crop",
    "polling_crop",
    "importing",
    "ocr",
    "figure_generation",
}
TERMINAL_CROP_FAILURES = {"failed", "interrupted"}
TERMINAL_STATES = {"completed", "failed", "interrupted", "cancelled"}
MAX_POLL_SECONDS = 6 * 60 * 60
FIGURE_PLAN_RETENTION = timedelta(minutes=30)
SOLUTION_RELINK_PLAN_RETENTION = timedelta(minutes=30)
SOLUTION_CONFIRMATION_PLAN_RETENTION = timedelta(minutes=30)
SOLUTION_CONFIRMATION_COUNT_KEYS = (
    "manual_verified",
    "auto_verified",
    "review",
)
SOLUTION_CONFIRMATION_EFFECT_FIELDS = {
    "unit_id",
    "role",
    "result",
    "reason",
    "asset_id",
    "before_sha256",
    "after_sha256",
}
SOLUTION_CONFIRMATION_PREVIEW_FIELDS = {
    "plan_id",
    "plan_sha256",
    "source_link_manifest_sha256",
    "snapshot_sha256",
    "scope",
    "manual_item",
    "effects",
    "counts",
    "provider_calls",
    "expires_at",
    "bindings",
    "response_sha256",
    "effects_sha256",
    "counts_sha256",
}
SOLUTION_CONFIRMATION_BINDING_FIELDS = {
    "job_sha256",
    "document_sha256",
    "run_sha256",
    "unit_sha256",
    "selection_sha256",
    "link_manifest_sha256",
    "cache_key_sha256",
    "snapshot_file_sha256",
    "snapshot_payload_sha256",
    "problem_source_sha256",
    "solution_source_sha256",
    "alignment_index_sha256",
    "versions_sha256",
    "selected_assets",
}
SOLUTION_CONFIRMATION_RECEIPT_FIELDS = {
    "plan_id",
    "applied",
    "idempotent",
    "confirmation_ids",
    "source_link_manifest_sha256",
    "link_manifest_sha256",
    "receipt_sha256",
    "effects",
    "counts",
    "effects_sha256",
    "counts_sha256",
    "response_sha256",
    "provider_calls",
}
RESUME_RETENTION = timedelta(days=7)
REQUEST_FIELDS = {
    "source_path",
    "solution_path",
    "pages",
    "workers",
    "crop_dpi",
    "ocr_profile",
    "problem_pdf",
    "solution_pdf",
    "source",
    "solution_source",
    "regenerate_figures",
    "figure_profile",
    "figure_budget_usd",
    "remove_blue_text",
    "provider_profile",
}


class _FigureBudgetExceeded(RuntimeError):
    pass


class _BatchCancelled(RuntimeError):
    pass


class _CropTerminalFailure(RuntimeError):
    pass


class _BudgetedFigureAdapter:
    """Reserve estimated output cost only when a new provider call is required."""

    def __init__(
        self,
        adapter: FigureGenerationAdapter,
        per_call_usd: Decimal,
        budget_usd: Decimal,
    ) -> None:
        self.adapter = adapter
        self.provider_name = getattr(adapter, "provider_name", "")
        self.per_call_usd = per_call_usd
        self.budget_usd = budget_usd
        self.calls = 0
        self.estimated_output_usd = Decimal("0")

    def generate(self, **request):  # noqa: ANN003, ANN201
        if self.estimated_output_usd + self.per_call_usd > self.budget_usd:
            raise _FigureBudgetExceeded
        self.calls += 1
        self.estimated_output_usd += self.per_call_usd
        try:
            generated = self.adapter.generate(**request)
        except FigureGenerationError:
            # Local validation and credential failures happen before a remote call.
            self.calls -= 1
            self.estimated_output_usd -= self.per_call_usd
            raise
        except FigureProviderRequestError:
            # A remote attempt with no usable image has no output-image charge.
            self.estimated_output_usd -= self.per_call_usd
            raise
        if not isinstance(generated, GeneratedImage):
            # The provider returned no generated image; the caller will reject it.
            self.estimated_output_usd -= self.per_call_usd
        return generated


def validate_figure_options(
    payload: Mapping[str, Any],
) -> tuple[bool, str, Decimal, bool]:
    """Validate the public, billable batch options without accepting coercions."""

    try:
        provider_profile = parse_provider_profile(payload.get("provider_profile"))
    except CandidateValidationError as exc:
        raise BatchError("Unsupported provider profile") from exc
    regenerate = payload.get("regenerate_figures", False)
    if not isinstance(regenerate, bool):
        raise BatchError("Invalid regenerate_figures")
    if provider_profile == "codex_only" and regenerate:
        raise BatchError("Codex-only batches cannot regenerate figures")
    profile = payload.get("figure_profile", "balanced")
    if not isinstance(profile, str) or profile not in FIGURE_PROFILES:
        raise BatchError("Unsupported figure profile")
    remove_blue_text = payload.get("remove_blue_text", False)
    if not isinstance(remove_blue_text, bool):
        raise BatchError("Invalid remove_blue_text")
    raw_budget = payload.get("figure_budget_usd", 0)
    if type(raw_budget) not in {int, float}:
        raise BatchError("Invalid figure_budget_usd")
    try:
        budget = Decimal(str(raw_budget))
    except InvalidOperation as exc:
        raise BatchError("Invalid figure_budget_usd") from exc
    if (
        not budget.is_finite()
        or budget < 0
        or budget > 100
        or budget != budget.quantize(Decimal("0.01"))
    ):
        raise BatchError("Invalid figure_budget_usd")
    return regenerate, profile, budget, remove_blue_text


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_URL_OPENER = build_opener(_NoRedirect)


class BatchError(RuntimeError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _local_ocr_error_code(exc: RevisionConflict | OSError) -> str:
    match exc:
        case RevisionConflict():
            return "revision_conflict"
        case PermissionError():
            return "storage_permission_denied"
        case OSError():
            return "storage_io_failed"
        case unreachable:
            assert_never(unreachable)


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def _ocr_error_code(exc: Exception, fallback: str = "ocr_failed") -> str:
    if isinstance(exc, (RevisionConflict, OSError)):
        return _local_ocr_error_code(exc)
    if isinstance(exc, ModelError) and isinstance(exc.__cause__, OSError):
        return _local_ocr_error_code(exc.__cause__)
    if isinstance(exc, ProviderNotConfigured):
        return "provider_not_configured"
    if isinstance(exc, CodexRateLimitError):
        return getattr(exc, "code", "provider_rate_limited")
    if isinstance(exc, ModelError):
        return getattr(exc, "code", "cache_invalid")
    return getattr(exc, "code", None) or fallback


class BatchCoordinator:
    """One-process, one-worker coordinator for expensive desktop batches."""

    def __init__(
        self,
        workspace_root: Path | str,
        pdf_roots: Sequence[Path | str],
        import_roots: Sequence[Path | str],
        crop_root: Path | str,
        *,
        poll_seconds: float = 1.0,
        figure_adapter: FigureGenerationAdapter | None = None,
        recover_active: bool = True,
        is_deleted: Callable[[str, str | None], bool] | None = None,
    ) -> None:
        self.workspace_root = inside(workspace_root, "workspace_root")
        self.pdf_roots = tuple(Path(root).expanduser().resolve() for root in pdf_roots)
        self.import_roots = tuple(
            Path(root).expanduser().resolve() for root in import_roots
        )
        self.crop_root = inside(crop_root, "crop_root")
        if not self.pdf_roots or not self.import_roots:
            raise BatchError("PDF and import allowlists must not be empty")
        if not self.crop_root.is_dir():
            raise BatchError("Crop application root is unavailable")
        if poll_seconds <= 0:
            raise BatchError("poll_seconds must be positive")
        self.poll_seconds = float(poll_seconds)
        self.figure_adapter = figure_adapter
        self.is_deleted = is_deleted or (lambda _project_id, _item_id: False)
        self.jobs_root = self.workspace_root / ".batch_jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.figure_plans_root = self.jobs_root / ".figure_plans"
        self.solution_relink_plans_root = self.jobs_root / ".solution_relink_plans"
        self.solution_confirmation_plans_root = (
            self.jobs_root / ".solution_confirmation_plans"
        )
        self.figure_cache_root = self.workspace_root / ".figure_generation_cache"
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="batch")
        self._load_existing(recover_active=recover_active)
        if recover_active:
            for job in self._jobs.values():
                if (job.get("kind") in {"solution_operation", "solution_pipeline"}
                        and job.get("state") == "interrupted" and not job.get("cancel_requested")
                        and not self.is_deleted(str(job.get("project_id") or ""), job.get("item_id"))):
                    job.update(state="queued", stage="solution_transcription", event="restart_queued",
                               revision=int(job.get("revision") or 0) + 1, updated_at=_now(), finished_at=None)
                    self._persist(job)
                    self._executor.submit(self._run_solution_operation, job["id"])
            for operation_path in self.workspace_root.glob("*/items/*/jobs/solution_operations/*/operation.json"):
                try:
                    if operation_path.is_symlink() or not operation_path.resolve().is_relative_to(self.workspace_root):
                        continue
                    operation = load_json_object(operation_path)
                    if (operation.get("source_event") or {}).get("operation") == "auto_crop_repair":
                        continue
                    project_id, item_id = operation_path.parts[-7], operation_path.parts[-5]
                    if self.is_deleted(project_id, item_id):
                        continue
                    operation_id = operation_path.parent.name
                    question_path = operation_path.parents[3] / "question.json"
                    document = load_json_object(question_path)
                    selection = document.get("provenance", {}).get("solution_selection")
                    if (operation.get("operation_id") == operation_id and isinstance(selection, Mapping)
                        and selection.get("revision") == operation.get("selection_revision")
                        and selection.get("source_asset_id") == operation.get("source_asset_id")
                        and selection.get("source_sha256") == operation.get("source_sha256")):
                        self.enqueue_solution_operation(project_id, item_id, operation_id)
                except (OSError, KeyError, ModelError):
                    continue

    @staticmethod
    def _question_figure_blocks(package: Mapping[str, Any]):
        content = package.get("content")
        question = content.get("question") if isinstance(content, Mapping) else None

        def walk(value: Any):
            if isinstance(value, Mapping):
                if value.get("type") == "figure":
                    yield value
                for child in value.values():
                    yield from walk(child)
            elif isinstance(value, list):
                for child in value:
                    yield from walk(child)

        yield from walk(question)

    @staticmethod
    def _selected_figure_is_protected(
        package: Mapping[str, Any], block: Mapping[str, Any]
    ) -> bool:
        assets = package.get("resources", {}).get("assets", [])
        selected = next(
            (
                asset
                for asset in (assets if isinstance(assets, list) else [])
                if isinstance(asset, Mapping)
                and asset.get("asset_id") == block.get("asset_id")
            ),
            None,
        )
        review = selected.get("review") if isinstance(selected, Mapping) else None
        return bool(
            isinstance(selected, Mapping)
            and selected.get("role") == "figure_regenerated"
            and isinstance(review, Mapping)
            and review.get("status") == "approved"
        )

    def _build_bank_figure_plan(self, items: Any) -> dict[str, Any]:
        try:
            resolved = resolve_bank_item_refs(self.workspace_root, items)
        except (ModelError, OSError, QuestionBankError) as exc:
            raise BatchError(str(exc), status=409) from exc
        if not resolved or len(resolved) > 1000:
            raise BatchError("Select between 1 and 1000 bank items")
        targets: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        figure_count = 0
        existing_regenerated = 0
        for entry in resolved:
            if self.project_provider_profile(str(entry["project_id"])) == "codex_only":
                raise BatchError(
                    "Codex-only projects cannot regenerate figures", status=409
                )
            package = entry["package"]
            summary = figure_generation_summary(package)
            figure_count += int(summary.get("figure_count") or 0)
            protected = package.get("workflow", {}).get("status") == "content_ready"
            for block in self._question_figure_blocks(package):
                block_id = block.get("block_id")
                if not isinstance(block_id, str):
                    continue
                inspection = inspect_figure_regeneration_target(package, block_id)
                block_protected = protected or self._selected_figure_is_protected(
                    package, block
                )
                reason = (
                    "protected" if block_protected else str(inspection["reason"])
                )
                public_target = {
                    "project_id": entry["project_id"],
                    "item_id": entry["item_id"],
                    "block_id": block_id,
                    "reason": reason,
                    "visual_kind": inspection["visual_kind"],
                    "placement_context": inspection["placement_context"],
                }
                if inspection.get("regenerated") is True:
                    existing_regenerated += 1
                    blocked.append({**public_target, "reason": "already_regenerated"})
                    continue
                if block_protected or inspection.get("eligible") is not True:
                    blocked.append(public_target)
                    continue
                try:
                    inputs = inspect_figure_generation_inputs(
                        entry["item_path"],
                        entry["source_revision"],
                        block_id,
                        self.figure_cache_root,
                    )
                except (ModelError, OSError):
                    blocked.append({**public_target, "reason": "invalid_source"})
                    continue
                targets.append(
                    {
                        "project_id": entry["project_id"],
                        "item_id": entry["item_id"],
                        "source_revision": entry["source_revision"],
                        "block_id": block_id,
                        **inputs,
                    }
                )
        keys = {str(target["artifact_key"]) for target in targets}
        cached_keys = {
            str(target["artifact_key"])
            for target in targets
            if target.get("cached") is True
        }
        provider_calls = len(keys - cached_keys)
        blocked_counts: dict[str, int] = {}
        for target in blocked:
            reason = str(target["reason"])
            blocked_counts[reason] = blocked_counts.get(reason, 0) + 1
        refs = [
            {
                key: entry[key]
                for key in ("project_id", "item_id", "source_revision")
            }
            for entry in resolved
        ]
        return {
            "selected_item_count": len(resolved),
            "figure_count": figure_count,
            "eligible_targets": len(targets),
            "cached_targets": existing_regenerated
            + sum(target.get("cached") is True for target in targets),
            "unique_generation_keys": len(keys),
            "provider_calls_max": provider_calls,
            "estimated_output_usd_max": float(
                Decimal(str(FIGURE_PROFILES["quality"].estimated_output_usd))
                * provider_calls
            ),
            "pricing_as_of": PRICING_AS_OF,
            "input_and_token_costs_included": False,
            "blocked_counts": blocked_counts,
            "blocked_targets": blocked,
            "items": refs,
            "targets": targets,
            "profile": "quality",
            "image_size": IMAGE_SIZE,
            "remove_blue_text": False,
        }

    @staticmethod
    def _bank_figure_plan_sha(plan: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(
                plan,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _public_bank_figure_plan(record: Mapping[str, Any]) -> dict[str, Any]:
        result = {
            key: copy.deepcopy(record[key])
            for key in (
                "plan_id",
                "plan_sha256",
                "expires_at",
            )
        }
        core = record["plan"]
        for key in (
            "selected_item_count",
            "figure_count",
            "eligible_targets",
            "cached_targets",
            "unique_generation_keys",
            "provider_calls_max",
            "estimated_output_usd_max",
            "pricing_as_of",
            "input_and_token_costs_included",
            "blocked_counts",
            "blocked_targets",
        ):
            result[key] = copy.deepcopy(core[key])
        return result

    def preflight_bank_figures(self, items: Any) -> dict[str, Any]:
        plan = self._build_bank_figure_plan(items)
        plan_sha256 = self._bank_figure_plan_sha(plan)
        now = datetime.now(timezone.utc)
        plan_id = f"figure-plan-{uuid.uuid4()}"
        record = {
            "schema_version": "0.1.0",
            "plan_id": plan_id,
            "plan_sha256": plan_sha256,
            "created_at": now.isoformat(),
            "expires_at": (now + FIGURE_PLAN_RETENTION).isoformat(),
            "consumed_at": None,
            "plan": plan,
        }
        if self.figure_plans_root.is_symlink():
            raise BatchError("Figure plan storage is unsafe", status=409)
        self.figure_plans_root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.figure_plans_root / plan_id / "plan.json", record)
        return self._public_bank_figure_plan(record)

    def confirm_bank_figure_plan(
        self, plan_id: Any, plan_sha256: Any, confirmed: Any
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise BatchError("Figure generation requires explicit confirmation")
        try:
            validate_id(plan_id, field="plan_id")
        except ModelError as exc:
            raise BatchError("Figure plan was not found", status=404) from exc
        if not isinstance(plan_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", plan_sha256
        ):
            raise BatchError("Figure plan hash is invalid")
        plan_path = self.figure_plans_root / str(plan_id) / "plan.json"
        with self._lock:
            if plan_path.is_symlink() or plan_path.parent.is_symlink() or not plan_path.is_file():
                raise BatchError("Figure plan was not found", status=404)
            record = load_json_object(plan_path)
            if (
                record.get("plan_id") != plan_id
                or record.get("plan_sha256") != plan_sha256
            ):
                raise BatchError("Figure plan no longer matches", status=409)
            if record.get("consumed_at") is not None:
                raise BatchError("Figure plan was already used", status=409)
            try:
                expires = datetime.fromisoformat(str(record.get("expires_at")))
            except ValueError as exc:
                raise BatchError("Figure plan is invalid", status=409) from exc
            if expires.tzinfo is None or datetime.now(timezone.utc) >= expires:
                raise BatchError("Figure plan expired", status=409)
            current = self._build_bank_figure_plan(record.get("plan", {}).get("items"))
            if self._bank_figure_plan_sha(current) != plan_sha256:
                raise BatchError(
                    "Figure sources or pricing changed; run preflight again",
                    status=409,
                )
            if int(current.get("eligible_targets") or 0) < 1:
                raise BatchError("Figure plan has no eligible targets", status=409)
            timestamp = _now()
            job_id = f"bank-figures-{uuid.uuid4()}"
            job = {
                "schema_version": "0.1.0",
                "id": job_id,
                "kind": "bank_figure_generation",
                "attempt": 1,
                "resume_root": job_id,
                "revision": 1,
                "event": "queued",
                "state": "queued",
                "stage": "figure_generation_queued",
                "progress": 0,
                "message": "Selected question figures are queued.",
                "created_at": timestamp,
                "updated_at": timestamp,
                "finished_at": None,
                "request": {
                    "plan_id": plan_id,
                    "plan_sha256": plan_sha256,
                    "profile": "quality",
                    "remove_blue_text": False,
                    "items": current["items"],
                    "targets": current["targets"],
                    "estimated_output_usd_max": current[
                        "estimated_output_usd_max"
                    ],
                },
                "project_id": None,
                "summary": {
                    "crop": {"total": 0, "passed": 0, "review": 0, "failed": 0, "registered": 0, "skipped": 0},
                    "ocr": {"total": 0, "attempted": 0, "succeeded": 0, "failed": 0, "question_succeeded": 0, "question_failed": 0, "explanation_succeeded": 0, "explanation_failed": 0, "failures": []},
                    "figures": {
                        "eligible": current["eligible_targets"],
                        "attempted": 0,
                        "succeeded": 0,
                        "failed": 0,
                        "skipped_budget": 0,
                        "cached": current["cached_targets"],
                        "provider_calls_max": current["provider_calls_max"],
                        "materialized": 0,
                        "estimated_output_usd": 0.0,
                        "blocked_counts": current["blocked_counts"],
                        "failures": [],
                    },
                },
                "error_code": None,
                "cancel_requested": False,
                "cancel_requested_at": None,
            }
            self._jobs[job_id] = job
            self._persist(job)
            record["consumed_at"] = timestamp
            record["batch_id"] = job_id
            atomic_write_json(plan_path, record)
        self._executor.submit(self._run_bank_figure_batch, job_id)
        return self._public(job)

    @staticmethod
    def _solution_relink_plan_sha(plan: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(
                plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _source_file_sha(project: Mapping[str, Any], role: str) -> str:
        source_import = project.get("source_import")
        files = source_import.get("source_files") if isinstance(source_import, Mapping) else None
        matches = [
            record.get("sha256")
            for record in (files if isinstance(files, list) else [])
            if isinstance(record, Mapping) and record.get("role") == role
        ]
        if (
            len(matches) != 1
            or not isinstance(matches[0], str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", matches[0]) is None
        ):
            raise BatchError(f"Project {role} identity is unavailable", status=409)
        return matches[0].casefold()

    @staticmethod
    def _manual_solution_count(manifest: Mapping[str, Any]) -> int:
        manual_assets = {
            str(asset_id)
            for asset_id, asset in (
                manifest.get("assets", {}).items()
                if isinstance(manifest.get("assets"), Mapping)
                else []
            )
            if isinstance(asset, Mapping)
            and asset.get("selection_status") == "manual_verified"
        }
        manual_items = {
            str(item_id)
            for item_id, item in (
                manifest.get("items", {}).items()
                if isinstance(manifest.get("items"), Mapping)
                else []
            )
            if isinstance(item, Mapping)
            and (
                (
                    isinstance(item.get("confirmation"), Mapping)
                    and str(item["confirmation"].get("method") or "").casefold()
                    == "manual"
                )
                or "manual_user_confirmation" in (item.get("evidence") or [])
            )
        }
        return len(manual_assets) + len(manual_items)

    @staticmethod
    def _confirmation_digest(value: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()

    def _solution_confirmation_context(
        self,
        project_id: Any,
        item_id: Any,
        *,
        expected_item_revision: Any | None = None,
        expected_link_sha256: str | None = None,
        allow_rotated_anchor: bool = False,
    ) -> dict[str, Any]:
        try:
            project_id = validate_id(project_id, field="project_id")
            item_id = validate_id(item_id, field="item_id")
        except ModelError as exc:
            raise BatchError("Project or item was not found", status=404) from exc
        project_dir = (self.workspace_root / project_id).resolve()
        project_path = project_dir / "project.json"
        if not project_dir.is_relative_to(self.workspace_root) or not project_path.is_file():
            raise BatchError("Project was not found", status=404)
        project = load_json_object(project_path)
        if project.get("project_id") != project_id or item_id not in (
            project.get("item_order") or []
        ):
            raise BatchError("Project or item was not found", status=404)
        workflow = project.get("workflow")
        project_revision = workflow.get("revision") if isinstance(workflow, Mapping) else None
        if not isinstance(project_revision, int) or isinstance(project_revision, bool):
            raise BatchError("Project revision is unavailable", status=409)
        item_path = (project_dir / "items" / item_id / "question.json").resolve()
        if not item_path.is_relative_to(project_dir) or not item_path.is_file():
            raise BatchError("Item was not found", status=404)
        item = load_json_object(item_path)
        if item.get("item_id") != item_id:
            raise BatchError("Item identity does not match", status=409)
        item_workflow = item.get("workflow")
        item_revision = (
            item_workflow.get("revision") if isinstance(item_workflow, Mapping) else None
        )
        if not isinstance(item_revision, int) or isinstance(item_revision, bool):
            raise BatchError("Item revision is unavailable", status=409)
        if expected_item_revision is not None and (
            isinstance(expected_item_revision, bool)
            or not isinstance(expected_item_revision, int)
            or item_revision != expected_item_revision
        ):
            raise BatchError("Item changed; preview again", status=409)

        source_import = project.get("source_import")
        reconciliation = project.get("source_reconciliation")
        if not isinstance(source_import, Mapping) or not isinstance(
            reconciliation, Mapping
        ):
            raise BatchError("Upstream solution identity is unavailable", status=409)
        leaf_run_id = source_import.get("leaf_run_id")
        crop_job_id = reconciliation.get("crop_job_id")
        anchor_leaf = reconciliation.get("leaf_run_id")
        anchor_manifest_sha = reconciliation.get("manifest_sha256")
        anchor_link_sha = reconciliation.get("current_link_manifest_sha256")
        anchor_revision = reconciliation.get("revision")
        if (
            not isinstance(leaf_run_id, str)
            or Path(leaf_run_id).name != leaf_run_id
            or not isinstance(crop_job_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", crop_job_id) is None
            or anchor_leaf != leaf_run_id
            or not isinstance(anchor_manifest_sha, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", anchor_manifest_sha) is None
            or not isinstance(anchor_link_sha, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", anchor_link_sha) is None
            or not isinstance(anchor_revision, int)
            or isinstance(anchor_revision, bool)
        ):
            raise BatchError("Upstream solution identity is invalid", status=409)
        manifest_records = [
            record
            for record in source_import.get("source_files", [])
            if isinstance(record, Mapping) and record.get("role") == "manifest"
        ]
        if (
            len(manifest_records) != 1
            or manifest_records[0].get("relative_path") != "manifest.json"
            or not isinstance(manifest_records[0].get("sha256"), str)
            or str(manifest_records[0]["sha256"]).casefold()
            != anchor_manifest_sha.casefold()
        ):
            raise BatchError("Problem manifest identity is invalid", status=409)

        crop_record = self._crop_job_snapshot(crop_job_id)
        documents = crop_record.get("documents")
        if (
            crop_record.get("state") != "completed"
            or not isinstance(documents, list)
            or len(documents) != 1
            or not isinstance(documents[0], Mapping)
        ):
            raise BatchError("Question crop result is unavailable", status=409)
        try:
            run_dir = self._allowed_run_dir(documents[0])
        except RuntimeError as exc:
            raise BatchError("Question crop result is unavailable", status=409) from exc
        manifest_path = run_dir / "manifest.json"
        if (
            run_dir.name != leaf_run_id
            or run_dir.parent.name != "output"
            or run_dir.parent.parent.name != crop_job_id
            or sha256_file(manifest_path) != anchor_manifest_sha.casefold()
        ):
            raise BatchError("Project and crop result do not match", status=409)
        link_path = run_dir / "solutions" / "link_manifest.json"
        if not link_path.is_file():
            raise BatchError("Solution link manifest is unavailable", status=409)
        current_link_sha = sha256_file(link_path)
        anchor_link_sha = anchor_link_sha.casefold()
        if expected_link_sha256 is None:
            if current_link_sha != anchor_link_sha:
                raise BatchError("Solution links changed; preview again", status=409)
        elif current_link_sha != expected_link_sha256:
            raise BatchError("Solution links changed; preview again", status=409)
        if not allow_rotated_anchor and anchor_link_sha != current_link_sha:
            raise BatchError("Solution reconciliation anchor is stale", status=409)

        link_manifest = load_json_object(link_path)
        if "propagation_unavailable" in link_manifest:
            raise BatchError("Fixed-edge propagation is unavailable", status=409)
        if link_manifest.get("manual_propagation_contract") != "fixed-edge-v1":
            raise BatchError(
                "This result has no fixed-edge propagation contract", status=409
            )
        problem = link_manifest.get("problem")
        solution = link_manifest.get("solution")
        problem_source_sha = (
            problem.get("source_sha256") if isinstance(problem, Mapping) else None
        )
        solution_source_sha = (
            solution.get("source_sha256") if isinstance(solution, Mapping) else None
        )
        if (
            not isinstance(problem_source_sha, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", problem_source_sha) is None
            or not isinstance(solution_source_sha, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", solution_source_sha) is None
        ):
            raise BatchError("Solution source identity is invalid", status=409)
        snapshot_ref = link_manifest.get("alignment_snapshot")
        if not isinstance(snapshot_ref, Mapping):
            raise BatchError("Alignment snapshot is unavailable", status=409)
        relative_path = snapshot_ref.get("relative_path")
        snapshot_sha = snapshot_ref.get("sha256")
        if (
            snapshot_ref.get("schema_version") != 1
            or not isinstance(relative_path, str)
            or not relative_path
            or Path(relative_path).is_absolute()
            or ".." in Path(relative_path).parts
            or not isinstance(snapshot_sha, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", snapshot_sha) is None
        ):
            raise BatchError("Alignment snapshot reference is invalid", status=409)
        snapshot_sha = snapshot_sha.casefold()
        if relative_path != f"solutions/alignment_snapshot-{snapshot_sha}.json":
            raise BatchError("Alignment snapshot is not content-addressed", status=409)
        snapshot_path = (run_dir / Path(relative_path)).resolve()
        if (
            not snapshot_path.is_relative_to(run_dir)
            or snapshot_path.is_symlink()
            or not snapshot_path.is_file()
            or sha256_file(snapshot_path) != snapshot_sha.casefold()
        ):
            raise BatchError("Alignment snapshot integrity check failed", status=409)
        cache_key = link_manifest.get("cache_key")
        if not isinstance(cache_key, str) or not cache_key:
            raise BatchError("Solution link cache identity is unavailable", status=409)
        return {
            "project_id": project_id,
            "project_dir": project_dir,
            "project_revision": project_revision,
            "item_id": item_id,
            "item_path": item_path,
            "item": item,
            "item_revision": item_revision,
            "crop_job_id": crop_job_id,
            "run_dir": run_dir,
            "manifest_sha256": anchor_manifest_sha.casefold(),
            "link_path": link_path,
            "link_manifest": link_manifest,
            "link_manifest_sha256": current_link_sha,
            "anchor_link_manifest_sha256": anchor_link_sha,
            "source_reconciliation_revision": anchor_revision,
            "cache_key": cache_key,
            "snapshot_sha256": snapshot_sha,
            "problem_source_sha256": problem_source_sha.casefold(),
            "solution_source_sha256": solution_source_sha.casefold(),
        }

    @staticmethod
    def _solution_confirmation_selection(
        item: Mapping[str, Any], source_asset_id: Any
    ) -> dict[str, str]:
        if not isinstance(source_asset_id, str) or not source_asset_id:
            raise BatchError("Select one local solution source asset")
        assets = item.get("resources", {}).get("assets", [])
        asset_matches = [
            asset
            for asset in (assets if isinstance(assets, list) else [])
            if isinstance(asset, Mapping) and asset.get("asset_id") == source_asset_id
        ]
        if len(asset_matches) != 1:
            raise BatchError("Selected local solution source asset is unavailable", status=409)
        links = item.get("provenance", {}).get("solution_links", [])
        matches = [
            link
            for link in (links if isinstance(links, list) else [])
            if isinstance(link, Mapping) and link.get("source_asset_id") == source_asset_id
        ]
        if len(matches) != 1:
            raise BatchError(
                "Selected source does not map to exactly one upstream asset", status=409
            )
        link = matches[0]
        kind = link.get("kind")
        if kind == "mixed":
            raise BatchError("Mixed answer/explanation assets cannot be propagated", status=409)
        role = (
            "answer"
            if kind == "quick_answer"
            else "explanation"
            if kind == "detailed_solution"
            else None
        )
        external_asset_id = link.get("external_asset_id")
        if role is None or not isinstance(external_asset_id, str) or not external_asset_id:
            raise BatchError("Selected source has no usable upstream asset identity", status=409)
        provenance = item.get("provenance")
        problem = provenance.get("problem") if isinstance(provenance, Mapping) else None
        raw_snapshot = (
            provenance.get("raw_snapshot") if isinstance(provenance, Mapping) else None
        )
        manifest_item = (
            raw_snapshot.get("manifest_item") if isinstance(raw_snapshot, Mapping) else None
        )
        unit_id = (
            problem.get("unit_id")
            if isinstance(problem, Mapping) and isinstance(problem.get("unit_id"), str)
            else manifest_item.get("unit_id")
            if isinstance(manifest_item, Mapping)
            and isinstance(manifest_item.get("unit_id"), str)
            else None
        )
        if not isinstance(unit_id, str) or not unit_id or len(unit_id) > 300:
            raise BatchError("Item upstream identity is unavailable", status=409)
        return {
            "source_asset_id": source_asset_id,
            "external_asset_id": external_asset_id,
            "role": role,
            "unit_id": unit_id,
        }

    def solution_confirmation_capability(
        self, project_id: Any, item_id: Any
    ) -> dict[str, Any]:
        try:
            context = self._solution_confirmation_context(project_id, item_id)
        except BatchError as exc:
            return {
                "available": False,
                "disabled_reason": str(exc),
                "eligible_source_asset_ids": [],
                "source_asset_reasons": {},
            }
        assets = context["item"].get("resources", {}).get("assets", [])
        source_ids = [
            asset.get("asset_id")
            for asset in (assets if isinstance(assets, list) else [])
            if isinstance(asset, Mapping)
            and asset.get("section") in {"explanation", "shared"}
            and isinstance(asset.get("asset_id"), str)
        ]
        eligible: list[str] = []
        reasons: dict[str, str] = {}
        for source_id in source_ids:
            try:
                self._solution_confirmation_selection(context["item"], source_id)
                eligible.append(source_id)
            except BatchError as exc:
                reasons[source_id] = str(exc)
        return {
            "available": bool(eligible),
            "disabled_reason": None if eligible else "No eligible solution source asset",
            "eligible_source_asset_ids": eligible,
            "source_asset_reasons": reasons,
        }

    @staticmethod
    def _confirmation_counts(value: Any) -> dict[str, int]:
        if not isinstance(value, Mapping) or set(value) != set(
            SOLUTION_CONFIRMATION_COUNT_KEYS
        ):
            raise BatchError("Question Crop confirmation counts are invalid", status=409)
        result: dict[str, int] = {}
        for key in SOLUTION_CONFIRMATION_COUNT_KEYS:
            count = value[key]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise BatchError("Question Crop confirmation counts are invalid", status=409)
            result[key] = count
        return result

    @staticmethod
    def _confirmation_effects(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise BatchError("Question Crop confirmation effects are invalid", status=409)
        effects: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw in value:
            if not isinstance(raw, Mapping) or set(raw) != SOLUTION_CONFIRMATION_EFFECT_FIELDS:
                raise BatchError("Question Crop confirmation effects are invalid", status=409)
            unit_id = raw.get("unit_id")
            role = raw.get("role")
            result = raw.get("result")
            asset_id = raw.get("asset_id")
            if (
                not isinstance(unit_id, str)
                or not unit_id
                or len(unit_id) > 300
                or role not in {"quick_answer", "detailed_solution"}
                or result not in SOLUTION_CONFIRMATION_COUNT_KEYS
                or not isinstance(raw.get("reason"), str)
                or not raw["reason"]
                or (
                    asset_id is not None
                    and (not isinstance(asset_id, str) or not asset_id)
                )
                or any(
                    not isinstance(raw.get(field), str)
                    or re.fullmatch(r"[0-9a-f]{64}", raw[field]) is None
                    for field in ("before_sha256", "after_sha256")
                )
                or (unit_id, role) in seen
            ):
                raise BatchError("Question Crop confirmation effects are invalid", status=409)
            seen.add((unit_id, role))
            effects.append(copy.deepcopy(dict(raw)))
        return effects

    @classmethod
    def _confirmation_delta(
        cls,
        *,
        effects: Any,
        counts: Any,
        effects_sha256: Any,
        counts_sha256: Any,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        normalized_effects = cls._confirmation_effects(effects)
        normalized_counts = cls._confirmation_counts(counts)
        expected_counts = {
            key: sum(effect["result"] == key for effect in normalized_effects)
            for key in SOLUTION_CONFIRMATION_COUNT_KEYS
        }
        if (
            normalized_counts != expected_counts
            or effects_sha256 != cls._confirmation_digest(normalized_effects)
            or counts_sha256 != cls._confirmation_digest(normalized_counts)
        ):
            raise BatchError("Question Crop confirmation delta is invalid", status=409)
        return normalized_effects, normalized_counts

    @staticmethod
    def _confirmation_role_binding(
        upstream_plan: Mapping[str, Any], binding: Mapping[str, Any]
    ) -> list[str]:
        manual_item = upstream_plan.get("manual_item")
        if not isinstance(manual_item, Mapping) or set(manual_item) != {
            "unit_id",
            "before",
            "after",
        }:
            raise BatchError("Question Crop manual item is invalid", status=409)
        before = manual_item.get("before")
        after = manual_item.get("after")
        role = binding.get("role")
        ids_field = "answer_ids" if role == "answer" else "explanation_ids"
        confirmation_field = (
            "answer_confirmation" if role == "answer" else "explanation_confirmation"
        )
        confirmation = (
            after.get(confirmation_field) if isinstance(after, Mapping) else None
        )
        confirmation_ids = (
            confirmation.get("confirmation_ids")
            if isinstance(confirmation, Mapping)
            else None
        )
        if (
            manual_item.get("unit_id") != binding.get("unit_id")
            or not isinstance(before, Mapping)
            or not isinstance(after, Mapping)
            or after.get(ids_field) != [binding.get("external_asset_id")]
            or not isinstance(confirmation, Mapping)
            or confirmation.get("method") != "manual"
            or not isinstance(confirmation_ids, list)
            or len(confirmation_ids) != 1
            or not isinstance(confirmation_ids[0], str)
            or re.fullmatch(r"mc-[0-9a-f]{32}", confirmation_ids[0]) is None
        ):
            raise BatchError("Question Crop manual edge binding is invalid", status=409)
        return list(confirmation_ids)

    @classmethod
    def _validate_confirmation_preview(
        cls,
        upstream: Mapping[str, Any],
        context: Mapping[str, Any],
        selection: Mapping[str, str],
    ) -> datetime:
        if set(upstream) != SOLUTION_CONFIRMATION_PREVIEW_FIELDS:
            raise BatchError("Question Crop returned an invalid confirmation preview", status=409)
        plan_id = upstream.get("plan_id")
        sha_fields = (
            "plan_sha256",
            "source_link_manifest_sha256",
            "snapshot_sha256",
            "response_sha256",
            "effects_sha256",
            "counts_sha256",
        )
        if (
            upstream.get("provider_calls") != 0
            or not isinstance(plan_id, str)
            or re.fullmatch(r"mcp-[0-9a-f]{32}", plan_id) is None
            or any(
                not isinstance(upstream.get(field), str)
                or re.fullmatch(r"[0-9a-f]{64}", upstream[field]) is None
                for field in sha_fields
            )
            or upstream.get("source_link_manifest_sha256")
            != context["link_manifest_sha256"]
            or upstream.get("snapshot_sha256") != context["snapshot_sha256"]
        ):
            raise BatchError("Question Crop returned an invalid confirmation preview", status=409)
        scope = upstream.get("scope")
        if not isinstance(scope, Mapping) or set(scope) != {
            "scope_id",
            "segment_id",
            "label",
            "item_count",
        }:
            raise BatchError("Question Crop confirmation scope is invalid", status=409)
        scope_item_count = scope.get("item_count")
        if (
            not isinstance(scope.get("scope_id"), str)
            or not scope["scope_id"]
            or not isinstance(scope.get("segment_id"), str)
            or not scope["segment_id"]
            or not isinstance(scope.get("label"), str)
            or not scope["label"]
            or isinstance(scope_item_count, bool)
            or not isinstance(scope_item_count, int)
            or scope_item_count < 1
        ):
            raise BatchError("Question Crop confirmation scope is invalid", status=409)
        effects, counts = cls._confirmation_delta(
            effects=upstream.get("effects"),
            counts=upstream.get("counts"),
            effects_sha256=upstream.get("effects_sha256"),
            counts_sha256=upstream.get("counts_sha256"),
        )
        binding = {**selection}
        cls._confirmation_role_binding(upstream, binding)
        expected_role = "quick_answer" if selection["role"] == "answer" else "detailed_solution"
        if sum(
            effect["unit_id"] == selection["unit_id"]
            and effect["role"] == expected_role
            and effect["result"] == "manual_verified"
            and effect["asset_id"] == selection["external_asset_id"]
            for effect in effects
        ) != 1:
            raise BatchError("Question Crop manual effect binding is invalid", status=409)
        response_payload = {
            "scope": dict(scope),
            "manual_item": upstream["manual_item"],
            "effects": effects,
            "counts": counts,
            "provider_calls": 0,
        }
        if upstream.get("response_sha256") != cls._confirmation_digest(response_payload):
            raise BatchError("Question Crop confirmation response digest is invalid", status=409)
        bindings = upstream.get("bindings")
        selected_assets = (
            bindings.get("selected_assets") if isinstance(bindings, Mapping) else None
        )
        if (
            not isinstance(bindings, Mapping)
            or set(bindings) != SOLUTION_CONFIRMATION_BINDING_FIELDS
            or any(
                not isinstance(bindings.get(field), str)
                or re.fullmatch(r"[0-9a-f]{64}", bindings[field]) is None
                for field in SOLUTION_CONFIRMATION_BINDING_FIELDS - {"selected_assets"}
            )
            or bindings.get("selection_sha256")
            != cls._confirmation_digest(
                {
                    "unit_id": selection["unit_id"],
                    "answer_ids": [selection["external_asset_id"]]
                    if selection["role"] == "answer"
                    else [],
                    "explanation_ids": [selection["external_asset_id"]]
                    if selection["role"] == "explanation"
                    else [],
                    "propagate_scope": True,
                }
            )
            or bindings.get("link_manifest_sha256")
            != context["link_manifest_sha256"]
            or bindings.get("cache_key_sha256")
            != cls._confirmation_digest(context["cache_key"])
            or bindings.get("snapshot_file_sha256") != context["snapshot_sha256"]
            or bindings.get("problem_source_sha256")
            != context["problem_source_sha256"]
            or bindings.get("solution_source_sha256")
            != context["solution_source_sha256"]
            or not isinstance(selected_assets, list)
            or len(selected_assets) != 1
            or not isinstance(selected_assets[0], Mapping)
            or set(selected_assets[0]) != {
                "asset_id",
                "identity_sha256",
                "output_sha256",
            }
            or selected_assets[0].get("asset_id") != selection["external_asset_id"]
            or any(
                not isinstance(selected_assets[0].get(field), str)
                or re.fullmatch(r"[0-9a-f]{64}", selected_assets[0][field]) is None
                for field in ("identity_sha256", "output_sha256")
            )
        ):
            raise BatchError("Question Crop confirmation bindings are invalid", status=409)
        try:
            expires = datetime.fromisoformat(str(upstream.get("expires_at")))
        except ValueError as exc:
            raise BatchError("Question Crop confirmation preview expiry is invalid", status=409) from exc
        if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
            raise BatchError("Question Crop confirmation preview already expired", status=409)
        return expires

    @staticmethod
    def _public_confirmation_preview(record: Mapping[str, Any]) -> dict[str, Any]:
        upstream = record["upstream_plan"]
        scope = upstream.get("scope") if isinstance(upstream, Mapping) else None
        counts = upstream.get("counts") if isinstance(upstream, Mapping) else None
        return {
            "preview_id": record["preview_id"],
            "preview_sha256": record["preview_sha256"],
            "expires_at": record["expires_at"],
            "selected_source_asset_id": record["binding"]["source_asset_id"],
            "scope_label": str(scope.get("label") or "연결 범위")
            if isinstance(scope, Mapping)
            else "연결 범위",
            "scope_item_count": int(scope.get("item_count") or 0)
            if isinstance(scope, Mapping)
            else 0,
            "counts": BatchCoordinator._confirmation_counts(counts),
            "provider_calls": 0,
        }

    def preview_solution_confirmation(
        self,
        project_id: Any,
        item_id: Any,
        *,
        expected_revision: Any,
        source_asset_id: Any,
        propagate_scope: Any,
    ) -> dict[str, Any]:
        if propagate_scope is not True:
            raise BatchError("Scope propagation must be explicitly enabled")
        context = self._solution_confirmation_context(
            project_id, item_id, expected_item_revision=expected_revision
        )
        selection = self._solution_confirmation_selection(
            context["item"], source_asset_id
        )
        payload = {
            "doc": 0,
            "unit_id": selection["unit_id"],
            "answer_ids": [selection["external_asset_id"]]
            if selection["role"] == "answer"
            else [],
            "explanation_ids": [selection["external_asset_id"]]
            if selection["role"] == "explanation"
            else [],
            "propagate_scope": True,
            "expected_link_manifest_sha256": context["link_manifest_sha256"],
            "expected_cache_key": context["cache_key"],
        }
        try:
            upstream = self._request_json(
                "POST",
                (
                    f"/api/jobs/{quote(context['crop_job_id'], safe='')}"
                    "/solutions/confirmation-plans"
                ),
                payload,
            )
        except RuntimeError as exc:
            raise BatchError("Question Crop confirmation preview is unavailable", status=409) from exc
        now = datetime.now(timezone.utc)
        upstream_expires = self._validate_confirmation_preview(
            upstream, context, selection
        )
        binding = {
            "project_id": context["project_id"],
            "project_revision": context["project_revision"],
            "item_id": context["item_id"],
            "item_revision": context["item_revision"],
            "source_reconciliation_revision": context[
                "source_reconciliation_revision"
            ],
            "source_link_manifest_sha256": context["link_manifest_sha256"],
            "manifest_sha256": context["manifest_sha256"],
            "cache_key": context["cache_key"],
            "snapshot_sha256": context["snapshot_sha256"],
            "problem_source_sha256": context["problem_source_sha256"],
            "solution_source_sha256": context["solution_source_sha256"],
            "crop_job_id": context["crop_job_id"],
            "leaf_run_id": context["run_dir"].name,
            **selection,
        }
        preview_id = f"solution-confirmation-{uuid.uuid4()}"
        expires_at = min(
            now + SOLUTION_CONFIRMATION_PLAN_RETENTION, upstream_expires
        ).isoformat()
        signed = {
            "binding": binding,
            "upstream_plan": upstream,
            "expires_at": expires_at,
        }
        record = {
            "schema_version": "fixed-edge-studio-v1",
            "preview_id": preview_id,
            "preview_sha256": self._confirmation_digest(signed),
            "created_at": now.isoformat(),
            "expires_at": expires_at,
            "binding": binding,
            "upstream_plan": copy.deepcopy(upstream),
            "upstream_result": None,
            "upstream_result_sha256": None,
            "completed_result": None,
            "completed_result_sha256": None,
        }
        root = self.solution_confirmation_plans_root
        if root.is_symlink():
            raise BatchError("Solution confirmation plan storage is unsafe", status=409)
        root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(root / preview_id / "plan.json", record)
        return self._public_confirmation_preview(record)

    def _load_solution_confirmation_record(
        self, preview_id: Any, preview_sha256: Any
    ) -> tuple[Path, dict[str, Any]]:
        try:
            preview_id = validate_id(preview_id, field="preview_id")
        except ModelError as exc:
            raise BatchError("Solution confirmation preview was not found", status=404) from exc
        if (
            not isinstance(preview_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", preview_sha256) is None
        ):
            raise BatchError("Solution confirmation preview hash is invalid")
        path = self.solution_confirmation_plans_root / preview_id / "plan.json"
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
            raise BatchError("Solution confirmation preview was not found", status=404)
        record = load_json_object(path)
        if (
            record.get("preview_id") != preview_id
            or record.get("preview_sha256") != preview_sha256
            or self._confirmation_digest(
                {
                    "binding": record.get("binding"),
                    "upstream_plan": record.get("upstream_plan"),
                    "expires_at": record.get("expires_at"),
                }
            )
            != preview_sha256
        ):
            raise BatchError("Solution confirmation preview no longer matches", status=409)
        return path, record

    def _revalidate_solution_confirmation_binding(
        self,
        project_id: Any,
        item_id: Any,
        binding: Mapping[str, Any],
        *,
        expected_link_sha256: str,
    ) -> dict[str, Any]:
        source_link_sha = binding.get("source_link_manifest_sha256")
        context = self._solution_confirmation_context(
            project_id,
            item_id,
            expected_item_revision=binding.get("item_revision"),
            expected_link_sha256=expected_link_sha256,
            allow_rotated_anchor=expected_link_sha256 != source_link_sha,
        )
        selection = self._solution_confirmation_selection(
            context["item"], binding.get("source_asset_id")
        )
        current_binding = {
            "project_revision": context["project_revision"],
            "item_revision": context["item_revision"],
            "source_reconciliation_revision": context[
                "source_reconciliation_revision"
            ],
            "source_link_manifest_sha256": context[
                "anchor_link_manifest_sha256"
            ],
            "manifest_sha256": context["manifest_sha256"],
            "cache_key": context["cache_key"],
            "snapshot_sha256": context["snapshot_sha256"],
            "problem_source_sha256": context["problem_source_sha256"],
            "solution_source_sha256": context["solution_source_sha256"],
            "crop_job_id": context["crop_job_id"],
            "leaf_run_id": context["run_dir"].name,
            **selection,
        }
        if any(binding.get(key) != value for key, value in current_binding.items()):
            raise BatchError("Project, item, or selection changed; preview again", status=409)
        return context

    @classmethod
    def _solution_confirmation_enrichment_progress(
        cls, record: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        progress = record.get("studio_reconciliation")
        if progress is None:
            return None
        required = {
            "state",
            "project_revision",
            "source_reconciliation_revision",
            "source_link_manifest_sha256",
            "link_manifest_sha256",
            "item_revision",
            "item_sha256",
            "source_asset_id",
            "external_asset_id",
            "role",
            "unit_id",
        }
        if (
            not isinstance(progress, Mapping)
            or set(progress) != required
            or progress.get("state") != "enriched"
            or record.get("studio_reconciliation_sha256")
            != cls._confirmation_digest(progress)
            or any(
                not isinstance(progress.get(field), str)
                or re.fullmatch(r"[0-9a-f]{64}", progress[field]) is None
                for field in (
                    "source_link_manifest_sha256",
                    "link_manifest_sha256",
                    "item_sha256",
                )
            )
            or any(
                isinstance(progress.get(field), bool)
                or not isinstance(progress.get(field), int)
                for field in (
                    "project_revision",
                    "source_reconciliation_revision",
                    "item_revision",
                )
            )
            or any(
                not isinstance(progress.get(field), str) or not progress[field]
                for field in (
                    "source_asset_id",
                    "external_asset_id",
                    "role",
                    "unit_id",
                )
            )
        ):
            raise BatchError("Studio reconciliation checkpoint is invalid", status=409)
        return progress

    def _capture_solution_confirmation_enrichment(
        self,
        plan_path: Path,
        record: dict[str, Any],
        binding: Mapping[str, Any],
        *,
        expected_link_sha256: str,
    ) -> dict[str, Any]:
        context = self._solution_confirmation_context(
            binding["project_id"],
            binding["item_id"],
            expected_link_sha256=expected_link_sha256,
            allow_rotated_anchor=True,
        )
        selection = self._solution_confirmation_selection(
            context["item"], binding.get("source_asset_id")
        )
        fixed = {
            "project_revision": context["project_revision"],
            "source_reconciliation_revision": context[
                "source_reconciliation_revision"
            ],
            "source_link_manifest_sha256": context[
                "anchor_link_manifest_sha256"
            ],
            "link_manifest_sha256": context["link_manifest_sha256"],
            **selection,
        }
        expected = {
            "project_revision": binding["project_revision"],
            "source_reconciliation_revision": binding[
                "source_reconciliation_revision"
            ],
            "source_link_manifest_sha256": binding[
                "source_link_manifest_sha256"
            ],
            "link_manifest_sha256": expected_link_sha256,
            "source_asset_id": binding["source_asset_id"],
            "external_asset_id": binding["external_asset_id"],
            "role": binding["role"],
            "unit_id": binding["unit_id"],
        }
        if fixed != expected:
            raise BatchError("Studio project changed during reconciliation", status=409)
        progress = {
            "state": "enriched",
            **fixed,
            "item_revision": context["item_revision"],
            "item_sha256": sha256_file(context["item_path"]),
        }
        record["studio_reconciliation"] = progress
        record["studio_reconciliation_sha256"] = self._confirmation_digest(progress)
        atomic_write_json(plan_path, record)
        return context

    def _revalidate_solution_confirmation_enrichment(
        self,
        binding: Mapping[str, Any],
        progress: Mapping[str, Any],
        *,
        anchor_intent: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = self._solution_confirmation_context(
            binding["project_id"],
            binding["item_id"],
            expected_item_revision=progress["item_revision"],
            expected_link_sha256=str(progress["link_manifest_sha256"]),
            allow_rotated_anchor=True,
        )
        selection = self._solution_confirmation_selection(
            context["item"], binding.get("source_asset_id")
        )
        current = {
            "project_revision": context["project_revision"],
            "source_reconciliation_revision": context[
                "source_reconciliation_revision"
            ],
            "source_link_manifest_sha256": context[
                "anchor_link_manifest_sha256"
            ],
            "link_manifest_sha256": context["link_manifest_sha256"],
            "item_revision": context["item_revision"],
            "item_sha256": sha256_file(context["item_path"]),
            **selection,
        }
        expected = {key: value for key, value in progress.items() if key != "state"}
        if anchor_intent is not None:
            expected.update(
                project_revision=anchor_intent["project_revision_after"],
                source_reconciliation_revision=anchor_intent[
                    "source_reconciliation_revision_after"
                ],
                source_link_manifest_sha256=anchor_intent[
                    "link_manifest_sha256"
                ],
            )
        if current != expected:
            raise BatchError("Studio item changed after reconciliation", status=409)
        return context

    @classmethod
    def _solution_confirmation_transaction(
        cls, record: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        transaction = record.get("studio_reconciliation_transaction")
        if transaction is None:
            return None
        required = {
            "state",
            "stage_relative_path",
            "project_revision",
            "source_reconciliation_revision",
            "source_link_manifest_sha256",
            "link_manifest_sha256",
            "protected_files",
            "changes",
        }
        protected = transaction.get("protected_files") if isinstance(transaction, Mapping) else None
        changes = transaction.get("changes") if isinstance(transaction, Mapping) else None
        if (
            not isinstance(transaction, Mapping)
            or set(transaction) != required
            or transaction.get("state") not in {"preparing", "staged"}
            or transaction.get("stage_relative_path") != "studio-reconciliation-stage"
            or record.get("studio_reconciliation_transaction_sha256")
            != cls._confirmation_digest(transaction)
            or any(
                isinstance(transaction.get(field), bool)
                or not isinstance(transaction.get(field), int)
                for field in ("project_revision", "source_reconciliation_revision")
            )
            or any(
                not isinstance(transaction.get(field), str)
                or re.fullmatch(r"[0-9a-f]{64}", transaction[field]) is None
                for field in (
                    "source_link_manifest_sha256",
                    "link_manifest_sha256",
                )
            )
            or not isinstance(protected, list)
            or not isinstance(changes, list)
        ):
            raise BatchError("Studio reconciliation transaction is invalid", status=409)
        seen: set[str] = set()
        for value in protected:
            if (
                not isinstance(value, Mapping)
                or set(value) != {"relative_path", "sha256"}
                or not isinstance(value.get("relative_path"), str)
                or value["relative_path"] in seen
                or Path(value["relative_path"]).is_absolute()
                or ".." in Path(value["relative_path"]).parts
                or not isinstance(value.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None
            ):
                raise BatchError("Studio reconciliation baseline is invalid", status=409)
            seen.add(value["relative_path"])
        change_paths: set[str] = set()
        for value in changes:
            if (
                not isinstance(value, Mapping)
                or set(value) != {"relative_path", "before_sha256", "after_sha256"}
                or not isinstance(value.get("relative_path"), str)
                or value["relative_path"] in change_paths
                or Path(value["relative_path"]).is_absolute()
                or ".." in Path(value["relative_path"]).parts
                or (
                    value.get("before_sha256") is not None
                    and (
                        not isinstance(value["before_sha256"], str)
                        or re.fullmatch(r"[0-9a-f]{64}", value["before_sha256"])
                        is None
                    )
                )
                or not isinstance(value.get("after_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", value["after_sha256"]) is None
            ):
                raise BatchError("Studio reconciliation change set is invalid", status=409)
            change_paths.add(value["relative_path"])
        if transaction["state"] == "preparing" and changes:
            raise BatchError("Studio reconciliation intent is invalid", status=409)
        return transaction

    @staticmethod
    def _solution_confirmation_protected_files(project_dir: Path) -> list[dict[str, str]]:
        project_path = project_dir / "project.json"
        project = load_json_object(project_path)
        item_order = project.get("item_order")
        if not isinstance(item_order, list) or any(
            not isinstance(item_id, str) or Path(item_id).name != item_id
            for item_id in item_order
        ):
            raise BatchError("Studio project item identity is invalid", status=409)
        required = [project_path] + [
            project_dir / "items" / item_id / "question.json" for item_id in item_order
        ]
        if any(not path.is_file() for path in required):
            raise BatchError("Studio project baseline is unavailable", status=409)
        paths = sorted(path for path in project_dir.rglob("*") if path.is_file())
        protected: list[dict[str, str]] = []
        for path in paths:
            resolved = path.resolve()
            if (
                not resolved.is_relative_to(project_dir)
                or path.is_symlink()
                or not path.is_file()
            ):
                raise BatchError("Studio project baseline is unavailable", status=409)
            protected.append(
                {
                    "relative_path": resolved.relative_to(project_dir).as_posix(),
                    "sha256": sha256_file(resolved),
                }
            )
        return protected

    def _validate_solution_confirmation_transaction_identity(
        self,
        binding: Mapping[str, Any],
        transaction: Mapping[str, Any],
    ) -> dict[str, Any]:
        context = self._solution_confirmation_context(
            binding["project_id"],
            binding["item_id"],
            expected_link_sha256=str(transaction["link_manifest_sha256"]),
            allow_rotated_anchor=True,
        )
        selection = self._solution_confirmation_selection(
            context["item"], binding.get("source_asset_id")
        )
        current = {
            "project_revision": context["project_revision"],
            "source_reconciliation_revision": context[
                "source_reconciliation_revision"
            ],
            "source_link_manifest_sha256": context[
                "anchor_link_manifest_sha256"
            ],
            "link_manifest_sha256": context["link_manifest_sha256"],
            **selection,
        }
        expected = {
            "project_revision": transaction["project_revision"],
            "source_reconciliation_revision": transaction[
                "source_reconciliation_revision"
            ],
            "source_link_manifest_sha256": transaction[
                "source_link_manifest_sha256"
            ],
            "link_manifest_sha256": transaction["link_manifest_sha256"],
            "source_asset_id": binding["source_asset_id"],
            "external_asset_id": binding["external_asset_id"],
            "role": binding["role"],
            "unit_id": binding["unit_id"],
        }
        if current != expected:
            raise BatchError("Studio project changed during reconciliation", status=409)
        return context

    @staticmethod
    def _write_staged_file(source: Path, destination: Path, expected_sha256: str) -> None:
        payload = source.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise BatchError("Studio reconciliation stage changed", status=409)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _prepare_solution_confirmation_transaction(
        self,
        plan_path: Path,
        record: dict[str, Any],
        binding: Mapping[str, Any],
        *,
        project_dir: Path,
        run_dir: Path,
        expected_link_sha256: str,
    ) -> Mapping[str, Any]:
        transaction = self._solution_confirmation_transaction(record)
        if transaction is None:
            transaction = {
                "state": "preparing",
                "stage_relative_path": "studio-reconciliation-stage",
                "project_revision": binding["project_revision"],
                "source_reconciliation_revision": binding[
                    "source_reconciliation_revision"
                ],
                "source_link_manifest_sha256": binding[
                    "source_link_manifest_sha256"
                ],
                "link_manifest_sha256": expected_link_sha256,
                "protected_files": self._solution_confirmation_protected_files(
                    project_dir
                ),
                "changes": [],
            }
            record["studio_reconciliation_transaction"] = transaction
            record["studio_reconciliation_transaction_sha256"] = (
                self._confirmation_digest(transaction)
            )
            atomic_write_json(plan_path, record)
        self._validate_solution_confirmation_transaction_identity(binding, transaction)
        if transaction["state"] == "staged":
            return transaction
        current_protected = self._solution_confirmation_protected_files(project_dir)
        if current_protected != transaction["protected_files"]:
            raise BatchError("Studio project changed before reconciliation", status=409)
        stage_dir = plan_path.parent / str(transaction["stage_relative_path"])
        if not stage_dir.resolve().is_relative_to(plan_path.parent.resolve()):
            raise BatchError("Studio reconciliation stage is unsafe", status=409)
        if stage_dir.exists():
            if stage_dir.is_symlink():
                raise BatchError("Studio reconciliation stage is unsafe", status=409)
            shutil.rmtree(stage_dir)
        if any(path.is_symlink() for path in project_dir.rglob("*")):
            raise BatchError("Studio project contains unsafe links", status=409)
        shutil.copytree(project_dir, stage_dir)
        if self._solution_confirmation_protected_files(stage_dir) != transaction[
            "protected_files"
        ]:
            raise BatchError("Studio project changed while staging reconciliation", status=409)
        enrich_project_from_leaf(
            stage_dir,
            run_dir,
            preserve_approved=True,
            expected_link_manifest_sha256=expected_link_sha256,
            expected_project_revision=binding.get("project_revision"),
        )
        if any(
            not (stage_dir / Path(value["relative_path"])).is_file()
            for value in transaction["protected_files"]
        ):
            raise BatchError("Studio reconciliation removed protected content", status=409)
        item_ids = {
            Path(value["relative_path"]).parts[1]
            for value in transaction["protected_files"]
            if Path(value["relative_path"]).parts[0] == "items"
        }
        changes: list[dict[str, Any]] = []
        for staged in sorted(path for path in stage_dir.rglob("*") if path.is_file()):
            relative = staged.relative_to(stage_dir)
            parts = relative.parts
            allowed = (
                len(parts) == 3
                and parts[0] == "items"
                and parts[1] in item_ids
                and parts[2] == "question.json"
            ) or (
                len(parts) == 4
                and parts[0] == "items"
                and parts[1] in item_ids
                and parts[2] == "source"
            )
            live = project_dir / relative
            before_sha = sha256_file(live) if live.is_file() else None
            after_sha = sha256_file(staged)
            if before_sha == after_sha:
                continue
            if len(parts) == 4 and parts[2] == "source" and before_sha is not None:
                allowed = False
            if not allowed or live.is_symlink():
                raise BatchError("Studio reconciliation staged an unsafe change", status=409)
            changes.append(
                {
                    "relative_path": relative.as_posix(),
                    "before_sha256": before_sha,
                    "after_sha256": after_sha,
                }
            )
        transaction = {**transaction, "state": "staged", "changes": changes}
        record["studio_reconciliation_transaction"] = transaction
        record["studio_reconciliation_transaction_sha256"] = self._confirmation_digest(
            transaction
        )
        atomic_write_json(plan_path, record)
        return transaction

    def _apply_solution_confirmation_transaction(
        self,
        plan_path: Path,
        record: dict[str, Any],
        binding: Mapping[str, Any],
        transaction: Mapping[str, Any],
        *,
        project_dir: Path,
    ) -> None:
        self._validate_solution_confirmation_transaction_identity(binding, transaction)
        stage_dir = plan_path.parent / str(transaction["stage_relative_path"])
        changes = {value["relative_path"]: value for value in transaction["changes"]}
        for protected in transaction["protected_files"]:
            relative = protected["relative_path"]
            path = project_dir / Path(relative)
            current_sha = sha256_file(path) if path.is_file() else None
            allowed = {protected["sha256"]}
            if relative in changes:
                allowed.add(changes[relative]["after_sha256"])
            if path.is_symlink() or current_sha not in allowed:
                raise BatchError("Studio project changed during reconciliation", status=409)
        ordered = sorted(
            transaction["changes"],
            key=lambda value: (
                Path(value["relative_path"]).name == "question.json",
                value["relative_path"],
            ),
        )
        for change in ordered:
            relative = Path(change["relative_path"])
            live = project_dir / relative
            current_sha = sha256_file(live) if live.is_file() else None
            if current_sha == change["after_sha256"]:
                continue
            if live.is_symlink() or current_sha != change["before_sha256"]:
                raise BatchError("Studio project changed during reconciliation", status=409)
            staged = stage_dir / relative
            if not staged.is_file() or staged.is_symlink():
                raise BatchError("Studio reconciliation stage is unavailable", status=409)
            self._write_staged_file(staged, live, change["after_sha256"])

    @classmethod
    def _solution_confirmation_anchor_intent(
        cls, record: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        intent = record.get("studio_anchor_intent")
        if intent is None:
            return None
        required = {
            "state",
            "project_revision_before",
            "project_revision_after",
            "source_reconciliation_revision_before",
            "source_reconciliation_revision_after",
            "source_link_manifest_sha256",
            "link_manifest_sha256",
            "project_before_payload_sha256",
            "workflow_before",
            "source_reconciliation_before",
            "completed_result",
            "completed_result_sha256",
        }
        if (
            not isinstance(intent, Mapping)
            or set(intent) != required
            or intent.get("state") not in {"pending", "applied"}
            or record.get("studio_anchor_intent_sha256")
            != cls._confirmation_digest(intent)
            or any(
                isinstance(intent.get(field), bool)
                or not isinstance(intent.get(field), int)
                for field in (
                    "project_revision_before",
                    "project_revision_after",
                    "source_reconciliation_revision_before",
                    "source_reconciliation_revision_after",
                )
            )
            or intent.get("project_revision_after")
            != intent.get("project_revision_before") + 1
            or intent.get("source_reconciliation_revision_after")
            != intent.get("source_reconciliation_revision_before") + 1
            or any(
                not isinstance(intent.get(field), str)
                or re.fullmatch(r"[0-9a-f]{64}", intent[field]) is None
                for field in (
                    "source_link_manifest_sha256",
                    "link_manifest_sha256",
                    "project_before_payload_sha256",
                    "completed_result_sha256",
                )
            )
            or not isinstance(intent.get("workflow_before"), Mapping)
            or not isinstance(intent.get("source_reconciliation_before"), Mapping)
            or not isinstance(intent.get("completed_result"), Mapping)
            or intent.get("completed_result_sha256")
            != cls._confirmation_digest(intent["completed_result"])
        ):
            raise BatchError("Studio anchor intent is invalid", status=409)
        return intent

    @classmethod
    def _solution_confirmation_anchor_state(
        cls, project_path: Path, intent: Mapping[str, Any]
    ) -> str:
        project = load_json_object(project_path)
        if cls._confirmation_digest(project) == intent["project_before_payload_sha256"]:
            return "before"
        workflow = project.get("workflow")
        reconciliation = project.get("source_reconciliation")
        if not isinstance(workflow, Mapping) or not isinstance(reconciliation, Mapping):
            raise BatchError("Studio project changed during anchor rotation", status=409)
        if (
            workflow.get("revision") != intent["project_revision_after"]
            or reconciliation.get("revision")
            != intent["source_reconciliation_revision_after"]
            or reconciliation.get("current_link_manifest_sha256")
            != intent["link_manifest_sha256"]
        ):
            raise BatchError("Studio project changed during anchor rotation", status=409)
        try:
            updated_at = datetime.fromisoformat(str(workflow.get("updated_at")))
            applied_at = datetime.fromisoformat(str(reconciliation.get("applied_at")))
        except ValueError as exc:
            raise BatchError("Studio anchor timestamp is invalid", status=409) from exc
        if updated_at.tzinfo is None or applied_at.tzinfo is None:
            raise BatchError("Studio anchor timestamp is invalid", status=409)
        workflow_before = intent["workflow_before"]
        reconciliation_before = intent["source_reconciliation_before"]
        if (
            set(workflow) != set(workflow_before)
            or set(reconciliation) != set(reconciliation_before)
            or any(
                workflow.get(key) != value
                for key, value in workflow_before.items()
                if key not in {"revision", "updated_at"}
            )
            or any(
                reconciliation.get(key) != value
                for key, value in reconciliation_before.items()
                if key
                not in {"revision", "current_link_manifest_sha256", "applied_at"}
            )
        ):
            raise BatchError("Studio project changed during anchor rotation", status=409)
        normalized = copy.deepcopy(project)
        normalized["workflow"] = copy.deepcopy(dict(workflow_before))
        normalized["source_reconciliation"] = copy.deepcopy(
            dict(reconciliation_before)
        )
        if cls._confirmation_digest(normalized) != intent["project_before_payload_sha256"]:
            raise BatchError("Studio project changed during anchor rotation", status=409)
        return "after"

    def _persist_solution_confirmation_anchor_intent(
        self,
        plan_path: Path,
        record: dict[str, Any],
        binding: Mapping[str, Any],
        *,
        project_path: Path,
        new_link_sha256: str,
        completed_result: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        existing = self._solution_confirmation_anchor_intent(record)
        if existing is not None:
            return existing
        project = load_json_object(project_path)
        workflow = project.get("workflow")
        reconciliation = project.get("source_reconciliation")
        if (
            not isinstance(workflow, Mapping)
            or workflow.get("revision") != binding.get("project_revision")
            or not isinstance(reconciliation, Mapping)
            or reconciliation.get("revision")
            != binding.get("source_reconciliation_revision")
            or reconciliation.get("current_link_manifest_sha256")
            != binding.get("source_link_manifest_sha256")
        ):
            raise BatchError("Studio project changed before anchor rotation", status=409)
        intent = {
            "state": "pending",
            "project_revision_before": binding["project_revision"],
            "project_revision_after": binding["project_revision"] + 1,
            "source_reconciliation_revision_before": binding[
                "source_reconciliation_revision"
            ],
            "source_reconciliation_revision_after": binding[
                "source_reconciliation_revision"
            ]
            + 1,
            "source_link_manifest_sha256": binding[
                "source_link_manifest_sha256"
            ],
            "link_manifest_sha256": new_link_sha256,
            "project_before_payload_sha256": self._confirmation_digest(project),
            "workflow_before": copy.deepcopy(dict(workflow)),
            "source_reconciliation_before": copy.deepcopy(dict(reconciliation)),
            "completed_result": copy.deepcopy(dict(completed_result)),
            "completed_result_sha256": self._confirmation_digest(completed_result),
        }
        record["studio_anchor_intent"] = intent
        record["studio_anchor_intent_sha256"] = self._confirmation_digest(intent)
        atomic_write_json(plan_path, record)
        return intent

    @classmethod
    def _validate_confirmation_receipt(
        cls,
        upstream_result: Mapping[str, Any],
        upstream_plan: Mapping[str, Any],
        binding: Mapping[str, Any],
    ) -> str:
        if set(upstream_result) != SOLUTION_CONFIRMATION_RECEIPT_FIELDS:
            raise BatchError("Question Crop returned an invalid apply receipt", status=409)
        new_link_sha = upstream_result.get("link_manifest_sha256")
        confirmation_ids = upstream_result.get("confirmation_ids")
        expected_confirmation_ids = cls._confirmation_role_binding(
            upstream_plan, binding
        )
        effects, counts = cls._confirmation_delta(
            effects=upstream_result.get("effects"),
            counts=upstream_result.get("counts"),
            effects_sha256=upstream_result.get("effects_sha256"),
            counts_sha256=upstream_result.get("counts_sha256"),
        )
        if (
            upstream_result.get("applied") is not True
            or not isinstance(upstream_result.get("idempotent"), bool)
            or upstream_result.get("provider_calls") != 0
            or upstream_result.get("plan_id") != upstream_plan.get("plan_id")
            or upstream_result.get("source_link_manifest_sha256")
            != binding.get("source_link_manifest_sha256")
            or not isinstance(new_link_sha, str)
            or re.fullmatch(r"[0-9a-f]{64}", new_link_sha) is None
            or not isinstance(upstream_result.get("receipt_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", upstream_result["receipt_sha256"])
            is None
            or confirmation_ids != expected_confirmation_ids
            or effects != upstream_plan.get("effects")
            or counts != upstream_plan.get("counts")
            or upstream_result.get("effects_sha256")
            != upstream_plan.get("effects_sha256")
            or upstream_result.get("counts_sha256")
            != upstream_plan.get("counts_sha256")
            or upstream_result.get("response_sha256")
            != upstream_plan.get("response_sha256")
        ):
            raise BatchError("Question Crop returned an invalid apply receipt", status=409)
        return new_link_sha

    @classmethod
    def _validate_confirmation_receipt_manifest(
        cls,
        link_manifest: Mapping[str, Any],
        upstream_result: Mapping[str, Any],
        binding: Mapping[str, Any],
    ) -> None:
        confirmation_ids = upstream_result["confirmation_ids"]
        constraints = link_manifest.get("manual_constraints")
        events = link_manifest.get("manual_confirmation_events")
        role = "quick_answer" if binding.get("role") == "answer" else "detailed_solution"
        matching_constraints = [
            constraint
            for constraint in (constraints if isinstance(constraints, list) else [])
            if isinstance(constraint, Mapping)
            and constraint.get("confirmation_id") in confirmation_ids
        ]
        matching_events = [
            event
            for event in (events if isinstance(events, list) else [])
            if isinstance(event, Mapping)
            and event.get("plan_id") == upstream_result.get("plan_id")
        ]
        if (
            len(matching_constraints) != len(confirmation_ids)
            or {value.get("confirmation_id") for value in matching_constraints}
            != set(confirmation_ids)
            or any(
                value.get("unit_id") != binding.get("unit_id")
                or value.get("role") != role
                or value.get("asset_id") != binding.get("external_asset_id")
                for value in matching_constraints
            )
            or len(matching_events) != 1
        ):
            raise BatchError("Applied manual edge does not match the preview", status=409)
        event = matching_events[0]
        for field in (
            "confirmation_ids",
            "source_link_manifest_sha256",
            "receipt_sha256",
            "effects",
            "counts",
            "effects_sha256",
            "counts_sha256",
            "response_sha256",
            "provider_calls",
        ):
            if event.get(field) != upstream_result.get(field):
                raise BatchError("Applied confirmation receipt is detached", status=409)

    def apply_solution_confirmation(
        self,
        project_id: Any,
        item_id: Any,
        *,
        preview_id: Any,
        preview_sha256: Any,
        confirmed: Any,
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise BatchError("Solution propagation requires explicit confirmation")
        with self._lock:
            plan_path, record = self._load_solution_confirmation_record(
                preview_id, preview_sha256
            )
            binding = record.get("binding")
            if (
                not isinstance(binding, Mapping)
                or binding.get("project_id") != project_id
                or binding.get("item_id") != item_id
            ):
                raise BatchError("Solution confirmation preview no longer matches", status=409)
            try:
                expires = datetime.fromisoformat(str(record.get("expires_at")))
            except ValueError as exc:
                raise BatchError("Solution confirmation preview is invalid", status=409) from exc
            if expires.tzinfo is None or datetime.now(timezone.utc) >= expires:
                raise BatchError("Solution confirmation preview expired", status=409)
            completed = record.get("completed_result")
            if isinstance(completed, Mapping):
                if record.get("completed_result_sha256") != self._confirmation_digest(
                    completed
                ):
                    raise BatchError("Solution confirmation receipt is invalid", status=409)
                raise BatchError("Solution confirmation preview was already used", status=409)

            upstream_plan = record.get("upstream_plan")
            if not isinstance(upstream_plan, Mapping):
                raise BatchError("Solution confirmation preview is invalid", status=409)
            upstream_result = record.get("upstream_result")
            if isinstance(upstream_result, Mapping) and record.get(
                "upstream_result_sha256"
            ) != self._confirmation_digest(upstream_result):
                raise BatchError("Upstream confirmation receipt is invalid", status=409)
            link_path = (
                self.crop_root
                / "runs"
                / "app"
                / str(binding.get("crop_job_id") or "")
                / "output"
                / str(binding.get("leaf_run_id") or "")
                / "solutions"
                / "link_manifest.json"
            )
            if not link_path.is_file():
                raise BatchError("Solution link manifest is unavailable", status=409)
            if not isinstance(upstream_result, Mapping):
                context = self._revalidate_solution_confirmation_binding(
                    project_id,
                    item_id,
                    binding,
                    expected_link_sha256=sha256_file(link_path),
                )
                try:
                    upstream_result = self._request_json(
                        "POST",
                        (
                            f"/api/jobs/{quote(context['crop_job_id'], safe='')}"
                            "/solutions/confirmation-plans/"
                            f"{quote(str(upstream_plan['plan_id']), safe='')}/apply"
                        ),
                        {
                            "expected_link_manifest_sha256": binding[
                                "source_link_manifest_sha256"
                            ]
                        },
                    )
                except RuntimeError as exc:
                    raise BatchError("Question Crop confirmation apply is unavailable", status=409) from exc
                new_link_sha = self._validate_confirmation_receipt(
                    upstream_result, upstream_plan, binding
                )
                context = self._revalidate_solution_confirmation_binding(
                    project_id,
                    item_id,
                    binding,
                    expected_link_sha256=new_link_sha,
                )
                self._validate_confirmation_receipt_manifest(
                    context["link_manifest"], upstream_result, binding
                )
                record["upstream_result"] = copy.deepcopy(dict(upstream_result))
                record["upstream_result_sha256"] = self._confirmation_digest(
                    upstream_result
                )
                atomic_write_json(plan_path, record)
            else:
                new_link_sha = self._validate_confirmation_receipt(
                    upstream_result, upstream_plan, binding
                )
            # A durable Crop receipt is not authority to overwrite newer Studio
            # state. A retry may resume only the exact append-only state that this
            # private plan checkpointed after enrichment.
            project_dir = self.workspace_root / str(project_id)
            progress = self._solution_confirmation_enrichment_progress(record)
            transaction = self._solution_confirmation_transaction(record)
            anchor_intent = self._solution_confirmation_anchor_intent(record)
            anchor_state = (
                self._solution_confirmation_anchor_state(
                    project_dir / "project.json", anchor_intent
                )
                if anchor_intent is not None
                else None
            )
            if anchor_intent is not None and progress is None:
                raise BatchError("Studio anchor intent lacks item checkpoint", status=409)
            context = (
                self._revalidate_solution_confirmation_enrichment(
                    binding,
                    progress,
                    anchor_intent=anchor_intent if anchor_state == "after" else None,
                )
                if progress is not None
                else self._validate_solution_confirmation_transaction_identity(
                    binding, transaction
                )
                if transaction is not None
                else self._revalidate_solution_confirmation_binding(
                    project_id,
                    item_id,
                    binding,
                    expected_link_sha256=new_link_sha,
                )
            )
            self._validate_confirmation_receipt_manifest(
                context["link_manifest"], upstream_result, binding
            )
            if not link_path.is_file() or sha256_file(link_path) != new_link_sha:
                raise BatchError("Applied solution manifest is not available yet", status=409)
            project = load_json_object(project_dir / "project.json")
            reconciliation = project.get("source_reconciliation")
            anchor_sha = (
                reconciliation.get("current_link_manifest_sha256")
                if isinstance(reconciliation, Mapping)
                else None
            )
            workflow = project.get("workflow")
            if anchor_state != "after" and (
                not isinstance(workflow, Mapping)
                or workflow.get("revision") != binding.get("project_revision")
                or not isinstance(reconciliation, Mapping)
                or reconciliation.get("revision")
                != binding.get("source_reconciliation_revision")
                or anchor_sha != binding.get("source_link_manifest_sha256")
            ):
                raise BatchError("Studio project changed after upstream apply", status=409)
            run_dir = link_path.parent.parent
            if sha256_file(run_dir / "manifest.json") != binding.get(
                "manifest_sha256"
            ):
                raise BatchError("Problem manifest changed after preview", status=409)
            if progress is None:
                try:
                    transaction = self._prepare_solution_confirmation_transaction(
                        plan_path,
                        record,
                        binding,
                        project_dir=project_dir,
                        run_dir=run_dir,
                        expected_link_sha256=new_link_sha,
                    )
                    self._apply_solution_confirmation_transaction(
                        plan_path,
                        record,
                        binding,
                        transaction,
                        project_dir=project_dir,
                    )
                except (SolutionIntegrityError, ModelError, OSError, shutil.Error) as exc:
                    raise BatchError(
                        "Studio reconciliation could not be completed", status=409
                    ) from exc
                self._capture_solution_confirmation_enrichment(
                    plan_path,
                    record,
                    binding,
                    expected_link_sha256=new_link_sha,
                )
                progress = self._solution_confirmation_enrichment_progress(record)
            assert progress is not None
            context = self._revalidate_solution_confirmation_enrichment(
                binding,
                progress,
                anchor_intent=anchor_intent if anchor_state == "after" else None,
            )
            preview = self._public_confirmation_preview(record)
            counts = upstream_result.get("counts")
            result = {
                "preview_id": preview["preview_id"],
                "applied": True,
                "idempotent": bool(upstream_result.get("idempotent")),
                "scope_label": preview["scope_label"],
                "scope_item_count": preview["scope_item_count"],
                "counts": self._confirmation_counts(counts),
                "provider_calls": 0,
            }
            if anchor_intent is None:
                anchor_intent = self._persist_solution_confirmation_anchor_intent(
                    plan_path,
                    record,
                    binding,
                    project_path=project_dir / "project.json",
                    new_link_sha256=new_link_sha,
                    completed_result=result,
                )
                anchor_state = "before"
            elif (
                anchor_intent["completed_result_sha256"]
                != self._confirmation_digest(result)
                or anchor_intent["completed_result"] != result
            ):
                raise BatchError("Studio completion response changed", status=409)
            if anchor_intent["state"] == "applied" and anchor_state != "after":
                raise BatchError("Studio anchor state regressed", status=409)
            if anchor_state == "before":
                try:
                    record_source_reconciliation(
                        project_dir,
                        run_dir,
                        str(binding["crop_job_id"]),
                        expected_link_manifest_sha256=new_link_sha,
                        expected_project_revision=binding.get("project_revision"),
                    )
                except (SolutionIntegrityError, ModelError, OSError) as exc:
                    raise BatchError(
                        "Studio reconciliation could not be completed", status=409
                    ) from exc
                anchor_state = self._solution_confirmation_anchor_state(
                    project_dir / "project.json", anchor_intent
                )
            if anchor_state != "after":
                raise BatchError("Studio anchor rotation did not complete", status=409)
            self._revalidate_solution_confirmation_enrichment(
                binding, progress, anchor_intent=anchor_intent
            )
            if anchor_intent["state"] != "applied":
                anchor_intent = {**anchor_intent, "state": "applied"}
                record["studio_anchor_intent"] = anchor_intent
                record["studio_anchor_intent_sha256"] = self._confirmation_digest(
                    anchor_intent
                )
                atomic_write_json(plan_path, record)
            result = copy.deepcopy(dict(anchor_intent["completed_result"]))
            record["completed_result"] = copy.deepcopy(result)
            record["completed_result_sha256"] = self._confirmation_digest(result)
            atomic_write_json(plan_path, record)
            return result

    def _crop_job_snapshot(self, crop_job_id: str) -> dict[str, Any]:
        path = self.crop_root / "runs" / "app" / crop_job_id / "job.json"
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise BatchError("Question crop job is unavailable", status=409) from exc
        if (
            not resolved.is_relative_to(self.crop_root)
            or path.is_symlink()
            or path.parent.is_symlink()
        ):
            raise BatchError("Question crop job identity is unsafe", status=409)
        record = load_json_object(resolved)
        if record.get("id") != crop_job_id:
            raise BatchError("Question crop job identity does not match", status=409)
        return record

    @staticmethod
    def _crop_provider_profile(record: Mapping[str, Any]) -> str:
        options = record.get("options")
        if options is not None and not isinstance(options, Mapping):
            raise BatchError("Question crop provider profile is invalid", status=409)
        try:
            return parse_provider_profile(
                options.get("provider_profile")
                if isinstance(options, Mapping)
                else None
            )
        except CandidateValidationError as exc:
            raise BatchError(
                "Question crop provider profile is invalid", status=409
            ) from exc

    @classmethod
    def _require_crop_provider_profile(
        cls, record: Mapping[str, Any], expected: str
    ) -> None:
        if cls._crop_provider_profile(record) != expected:
            raise RuntimeError("question crop provider profile changed")

    def _build_solution_relink_plan(self, project_id: Any) -> dict[str, Any]:
        try:
            project_id = validate_id(project_id, field="project_id")
        except ModelError as exc:
            raise BatchError("Project was not found", status=404) from exc
        project_dir = (self.workspace_root / project_id).resolve()
        project_path = project_dir / "project.json"
        if not project_dir.is_relative_to(self.workspace_root) or not project_path.is_file():
            raise BatchError("Project was not found", status=404)
        project = load_json_object(project_path)
        if project.get("project_id") != project_id:
            raise BatchError("Project was not found", status=404)
        workflow = project.get("workflow")
        project_revision = workflow.get("revision") if isinstance(workflow, Mapping) else None
        source_import = project.get("source_import")
        leaf_run_id = source_import.get("leaf_run_id") if isinstance(source_import, Mapping) else None
        if (
            not isinstance(project_revision, int)
            or isinstance(project_revision, bool)
            or not isinstance(leaf_run_id, str)
            or not leaf_run_id
            or Path(leaf_run_id).name != leaf_run_id
        ):
            raise BatchError("Project source identity is unavailable", status=409)
        manifest_sha = self._source_file_sha(project, "manifest")
        source_files = source_import.get("source_files") if isinstance(source_import, Mapping) else None
        linked_at_import = any(
            isinstance(entry, Mapping) and entry.get("role") == "link_manifest"
            for entry in (source_files if isinstance(source_files, list) else [])
        )
        initial_link_sha = self._source_file_sha(project, "link_manifest") if linked_at_import else None

        reconciliation = project.get("source_reconciliation")
        reconciled_crop_job = (
            reconciliation.get("crop_job_id")
            if isinstance(reconciliation, Mapping)
            else None
        )
        reconciled_link_sha = (
            reconciliation.get("current_link_manifest_sha256")
            if isinstance(reconciliation, Mapping)
            else None
        )
        with self._lock:
            project_records = [
                copy.deepcopy(record)
                for record in self._jobs.values()
                if record.get("project_id") == project_id
                and isinstance(record.get("crop_job_id"), str)
            ]
        crop_job_ids = {
            str(record["crop_job_id"])
            for record in project_records
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(record["crop_job_id"]))
        }
        if isinstance(reconciled_crop_job, str):
            crop_job_ids.add(reconciled_crop_job)
        if len(crop_job_ids) != 1:
            raise BatchError("Project crop job identity is ambiguous", status=409)
        crop_job_id = next(iter(crop_job_ids))
        crop_record = self._crop_job_snapshot(crop_job_id)
        provider_profile = self._crop_provider_profile(crop_record)
        if crop_record.get("state") != "completed":
            raise BatchError("Question crop job is not completed", status=409)
        documents = crop_record.get("documents")
        if (
            not isinstance(documents, list)
            or len(documents) != 1
            or not isinstance(documents[0], Mapping)
        ):
            raise BatchError("Only one-document crop jobs can be relinked", status=409)
        run_dir = self._allowed_run_dir(documents[0])
        if (
            run_dir.name != leaf_run_id
            or run_dir.parent.name != "output"
            or run_dir.parent.parent.name != crop_job_id
            or sha256_file(run_dir / "manifest.json") != manifest_sha
        ):
            raise BatchError("Project and crop run identity do not match", status=409)

        link_path = run_dir / "solutions" / "link_manifest.json"
        if not link_path.is_file() and initial_link_sha is not None:
            raise BatchError("Solution link manifest is unavailable", status=409)
        current_link_sha = sha256_file(link_path) if link_path.is_file() else None
        expected_link_sha = (
            str(reconciled_link_sha).casefold()
            if isinstance(reconciled_link_sha, str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", reconciled_link_sha)
            else initial_link_sha
        )
        recoverable_hashes: dict[
            str, list[tuple[str, str, str | None]]
        ] = {}
        pending_recoveries: list[tuple[str, str, str | None]] = []
        for record in project_records:
            request = record.get("request")
            if (
                record.get("kind") != "solution_relink"
                or record.get("state") not in {"failed", "interrupted"}
                or record.get("recovery_consumed_by") is not None
                or record.get("superseded_by") is not None
                or not isinstance(request, Mapping)
                or request.get("crop_job_id") != crop_job_id
                or request.get("leaf_run_id") != leaf_run_id
                or request.get("manifest_sha256") != manifest_sha
            ):
                continue
            try:
                stored_provider_profile = parse_provider_profile(
                    request.get("provider_profile")
                )
            except CandidateValidationError:
                continue
            if stored_provider_profile != provider_profile:
                continue
            requested_solution_sha = request.get("solution_sha256")
            if (
                not isinstance(requested_solution_sha, str)
                or re.fullmatch(r"[0-9a-f]{64}", requested_solution_sha) is None
            ):
                continue
            result = record.get("upstream_result")
            if (
                isinstance(result, Mapping)
                and result.get("crop_job_id") == crop_job_id
                and result.get("leaf_run_id") == leaf_run_id
                and result.get("manifest_sha256") == manifest_sha
                and result.get("solution_sha256") == requested_solution_sha
                and parse_provider_profile(result.get("provider_profile"))
                == provider_profile
                and isinstance(result.get("link_manifest_sha256"), str)
                and re.fullmatch(
                    r"[0-9a-f]{64}", str(result.get("link_manifest_sha256"))
                )
                and (
                    request.get("link_manifest_sha256") == expected_link_sha
                    or (
                        isinstance(reconciliation, Mapping)
                        and result.get("link_manifest_sha256") == expected_link_sha
                        and current_link_sha == expected_link_sha
                    )
                )
            ):
                recoverable_hashes.setdefault(
                    str(result["link_manifest_sha256"]), []
                ).append(
                    (
                        requested_solution_sha,
                        str(record["id"]),
                        str(record.get("recovery_of"))
                        if isinstance(record.get("recovery_of"), str)
                        else None,
                    )
                )
            upstream_requested = record.get("upstream_requested")
            if (
                not isinstance(result, Mapping)
                and request.get("link_manifest_sha256") == expected_link_sha
                and isinstance(upstream_requested, Mapping)
                and upstream_requested.get("source_link_manifest_sha256")
                == expected_link_sha
                and upstream_requested.get("manifest_sha256") == manifest_sha
                and upstream_requested.get("solution_sha256")
                == requested_solution_sha
                and parse_provider_profile(
                    upstream_requested.get("provider_profile")
                )
                == provider_profile
            ):
                pending_recoveries.append(
                    (
                        requested_solution_sha,
                        str(record["id"]),
                        str(record.get("recovery_of"))
                        if isinstance(record.get("recovery_of"), str)
                        else None,
                    )
                )

        record_created_at = {
            str(record["id"]): str(record.get("created_at") or "")
            for record in project_records
        }

        def select_recovery(
            matches: Sequence[tuple[str, str, str | None]],
        ) -> tuple[str, str]:
            parent_ids = {
                parent_id for _, _, parent_id in matches if parent_id is not None
            }
            descendants = [entry for entry in matches if entry[1] not in parent_ids]
            candidates = descendants if descendants else list(matches)
            if len({solution_sha for solution_sha, _, _ in candidates}) != 1:
                raise BatchError("Solution relink recovery is ambiguous", status=409)
            latest = max(
                candidates,
                key=lambda entry: (record_created_at[entry[1]], entry[1]),
            )
            return latest[0], latest[1]

        crop_solution = crop_record.get("solution")
        crop_solution_state = (
            crop_solution.get("state") if isinstance(crop_solution, Mapping) else None
        )
        if pending_recoveries and crop_solution_state in {
            "queued",
            "indexing",
            "linking",
        }:
            raise BatchError("Prior solution relinking is still running", status=409)
        upstream_already_completed = False
        recovery_solution_shas: set[str] = set()
        recovery_source_job_id: str | None = None
        superseded_pending_job_ids: list[str] = []
        if current_link_sha != expected_link_sha:
            if current_link_sha in recoverable_hashes:
                matches = recoverable_hashes[current_link_sha]
                selected_sha, selected_job = select_recovery(matches)
                upstream_already_completed = True
                recovery_solution_shas.add(selected_sha)
                recovery_source_job_id = selected_job
            elif (
                pending_recoveries
                and crop_solution_state == "completed"
            ):
                selected_sha, selected_job = select_recovery(pending_recoveries)
                upstream_already_completed = True
                recovery_solution_shas.add(selected_sha)
                recovery_source_job_id = selected_job
                superseded_pending_job_ids = sorted(
                    {job_id for _, job_id, _ in pending_recoveries if job_id != selected_job}
                )
            else:
                raise BatchError("Solution links changed; run preflight again", status=409)
        elif current_link_sha in recoverable_hashes:
            matches = recoverable_hashes[current_link_sha]
            selected_sha, selected_job = select_recovery(matches)
            upstream_already_completed = True
            recovery_solution_shas.add(selected_sha)
            recovery_source_job_id = selected_job
        elif pending_recoveries and crop_solution_state == "completed":
            superseded_pending_job_ids = sorted(
                {job_id for _, job_id, _ in pending_recoveries}
            )

        link_manifest = load_json_object(link_path) if link_path.is_file() else {"items": {}}
        manual_count = self._manual_solution_count(link_manifest)
        if manual_count:
            raise BatchError(
                "Manual solution selections require the Phase 3 preservation contract",
                status=409,
            )
        source_records = sorted(
            project_records,
            key=lambda record: str(record.get("updated_at") or record.get("created_at") or ""),
            reverse=True,
        )
        source_record = next(
            (
                record for record in source_records
                if isinstance(record.get("request"), Mapping)
                and str(record["request"].get("solution_path") or "")
            ),
            None,
        )
        request = source_record.get("request") if isinstance(source_record, Mapping) else {}
        solution_meta = link_manifest.get("solution")
        solution_path_value = (
            solution_meta.get("source_pdf") if isinstance(solution_meta, Mapping) else None
        )
        solution_sha = (
            solution_meta.get("source_sha256") if isinstance(solution_meta, Mapping) else None
        )
        if current_link_sha is None:
            solution_path_value = request.get("solution_path") if isinstance(request, Mapping) else None
            upload = request.get("solution_upload") if isinstance(request, Mapping) else None
            solution_sha = upload.get("sha256") if isinstance(upload, Mapping) else None
            crop_source = crop_solution.get("source") if isinstance(crop_solution, Mapping) else None
            if crop_source and crop_source != solution_path_value:
                raise BatchError("Solution PDF source changed", status=409)
        solution_path = self._pdf(solution_path_value, "solution_path", required=True)
        assert solution_path is not None
        if current_link_sha is None and solution_sha is None:
            solution_sha = sha256_file(solution_path)
        if not isinstance(solution_sha, str) or re.fullmatch(r"[0-9a-fA-F]{64}", solution_sha) is None:
            raise BatchError("Solution PDF identity is unavailable", status=409)
        if sha256_file(solution_path) != solution_sha.casefold():
            raise BatchError("Solution PDF changed", status=409)
        if upstream_already_completed and solution_sha.casefold() not in recovery_solution_shas:
            raise BatchError("Recovered solution PDF identity does not match", status=409)

        crop_dpi = request.get("crop_dpi") if isinstance(request, Mapping) else None
        crop_dpi = crop_dpi if crop_dpi in {300, 450} else 300
        metadata = project.get("metadata")
        source_name = (
            metadata.get("filename_base") if isinstance(metadata, Mapping) else project_id
        )
        items = link_manifest.get("items")
        studio_manual_items: set[str] = set()
        studio_approved_items: set[str] = set()
        for item_id in project.get("item_order") or []:
            if not isinstance(item_id, str):
                continue
            item_path = (project_dir / "items" / item_id / "question.json").resolve()
            if not item_path.is_relative_to(project_dir) or not item_path.is_file():
                continue
            item = load_json_object(item_path)
            links = item.get("provenance", {}).get("solution_links", [])
            if any(
                isinstance(link, Mapping)
                and "manual_user_confirmation" in (link.get("evidence") or [])
                for link in (links if isinstance(links, list) else [])
            ):
                studio_manual_items.add(item_id)
            item_workflow = item.get("workflow")
            item_review = item.get("review")
            if (
                isinstance(item_workflow, Mapping)
                and item_workflow.get("status") in {"content_ready", "export_ready"}
            ) or (
                isinstance(item_review, Mapping)
                and (
                    (
                        isinstance(item_review.get("approved_revision"), int)
                        and not isinstance(item_review.get("approved_revision"), bool)
                    )
                    or isinstance(item_review.get("approved_sha256"), str)
                    or isinstance(item_review.get("reviewer"), str)
                )
            ):
                studio_approved_items.add(item_id)
        counts = {key: 0 for key in ("complete", "partial", "review", "unmatched", "conflict")}
        for item in items.values() if isinstance(items, Mapping) else []:
            if not isinstance(item, Mapping):
                continue
            status = str(item.get("overall_status") or "")
            if not status:
                legacy = str(item.get("status") or "unmatched")
                linked_roles = int(bool(item.get("answer_ids"))) + int(
                    bool(item.get("explanation_ids"))
                )
                status = (
                    "conflict"
                    if legacy == "conflict"
                    else "complete"
                    if legacy == "verified" and linked_roles == 2
                    else "partial"
                    if legacy == "verified" and linked_roles == 1
                    else "review"
                    if legacy == "review"
                    else "unmatched"
                )
            status = status if status in counts else "review"
            counts[status] += 1
        return {
            "project_id": project_id,
            "project_revision": project_revision,
            "crop_job_id": crop_job_id,
            "provider_profile": provider_profile,
            "leaf_run_id": leaf_run_id,
            "manifest_sha256": manifest_sha,
            "link_manifest_sha256": current_link_sha,
            "solution_sha256": solution_sha.casefold(),
            "solution_path": str(solution_path),
            "solution_name": solution_path.name,
            "source_name": str(source_name or project_id),
            "crop_dpi": crop_dpi,
            "item_count": len(project.get("item_order") or []),
            "manual_selection_count": manual_count,
            "studio_manual_selection_count": len(studio_manual_items),
            "studio_approved_item_count": len(studio_approved_items),
            "current_status_counts": counts,
            "problem_crops_reused": True,
            "cache_status": "recovery" if upstream_already_completed else "unknown",
            "provider_calls_possible": not upstream_already_completed,
            "provider_calls_max": 0 if upstream_already_completed else None,
            "estimated_cost_usd_max": 0.0 if upstream_already_completed else None,
            "upstream_already_completed": upstream_already_completed,
            "recovery_source_job_id": recovery_source_job_id,
            "superseded_pending_job_ids": superseded_pending_job_ids,
        }

    @staticmethod
    def _public_solution_relink_plan(record: Mapping[str, Any]) -> dict[str, Any]:
        result = {
            key: copy.deepcopy(record[key])
            for key in ("plan_id", "plan_sha256", "expires_at")
        }
        for key, value in record["plan"].items():
            if key != "solution_path":
                result[key] = copy.deepcopy(value)
        return result

    def preflight_solution_relink(self, project_id: Any) -> dict[str, Any]:
        plan = self._build_solution_relink_plan(project_id)
        plan_sha256 = self._solution_relink_plan_sha(plan)
        now = datetime.now(timezone.utc)
        plan_id = f"solution-relink-plan-{uuid.uuid4()}"
        record = {
            "schema_version": "0.1.0",
            "plan_id": plan_id,
            "plan_sha256": plan_sha256,
            "created_at": now.isoformat(),
            "expires_at": (now + SOLUTION_RELINK_PLAN_RETENTION).isoformat(),
            "consumed_at": None,
            "plan": plan,
        }
        if self.solution_relink_plans_root.is_symlink():
            raise BatchError("Solution relink plan storage is unsafe", status=409)
        self.solution_relink_plans_root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.solution_relink_plans_root / plan_id / "plan.json", record
        )
        return self._public_solution_relink_plan(record)

    def confirm_solution_relink_plan(
        self,
        project_id: Any,
        plan_id: Any,
        plan_sha256: Any,
        confirmed: Any,
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise BatchError("Solution relinking requires explicit confirmation")
        try:
            project_id = validate_id(project_id, field="project_id")
            plan_id = validate_id(plan_id, field="plan_id")
        except ModelError as exc:
            raise BatchError("Solution relink plan was not found", status=404) from exc
        if not isinstance(plan_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", plan_sha256) is None:
            raise BatchError("Solution relink plan hash is invalid")
        plan_path = self.solution_relink_plans_root / plan_id / "plan.json"
        with self._lock:
            if plan_path.is_symlink() or plan_path.parent.is_symlink() or not plan_path.is_file():
                raise BatchError("Solution relink plan was not found", status=404)
            record = load_json_object(plan_path)
            if (
                record.get("plan_id") != plan_id
                or record.get("plan_sha256") != plan_sha256
                or record.get("plan", {}).get("project_id") != project_id
            ):
                raise BatchError("Solution relink plan no longer matches", status=409)
            if record.get("consumed_at") is not None:
                raise BatchError("Solution relink plan was already used", status=409)
            try:
                expires = datetime.fromisoformat(str(record.get("expires_at")))
            except ValueError as exc:
                raise BatchError("Solution relink plan is invalid", status=409) from exc
            if expires.tzinfo is None or datetime.now(timezone.utc) >= expires:
                raise BatchError("Solution relink plan expired", status=409)
            current = self._build_solution_relink_plan(project_id)
            if self._solution_relink_plan_sha(current) != plan_sha256:
                raise BatchError(
                    "Project or solution links changed; run preflight again", status=409
                )
            if any(
                job.get("kind") == "solution_relink"
                and (
                    job.get("project_id") == project_id
                    or job.get("crop_job_id") == current["crop_job_id"]
                )
                and job.get("state") in ACTIVE_STATES
                for job in self._jobs.values()
            ):
                raise BatchError("Solution relinking is already running", status=409)
            timestamp = _now()
            job_id = f"solution-relink-{uuid.uuid4()}"
            job = {
                "schema_version": "0.1.0",
                "id": job_id,
                "kind": "solution_relink",
                "revision": 1,
                "event": "queued",
                "state": "queued",
                "stage": "solution_relink_queued",
                "progress": 0,
                "message": "Answer and explanation relinking is queued.",
                "created_at": timestamp,
                "updated_at": timestamp,
                "finished_at": None,
                "request": copy.deepcopy(current),
                "crop_job_id": current["crop_job_id"],
                "project_id": project_id,
                "recovery_of": current.get("recovery_source_job_id"),
                "supersedes_relink_ids": copy.deepcopy(
                    current.get("superseded_pending_job_ids") or []
                ),
                "upstream_requested": None,
                "upstream_result": None,
                "studio_reconciliation": {"state": "pending"},
                "summary": {
                    "crop": {
                        "total": current["item_count"],
                        "passed": current["item_count"],
                        "review": 0,
                        "failed": 0,
                        "registered": current["item_count"],
                        "skipped": 0,
                    },
                    "ocr": {"succeeded": 0, "failed": 0, "failures": []},
                    "figures": {"failed": 0, "skipped_budget": 0, "failures": []},
                    "solution": {
                        "state": "queued",
                        "affected_items": current["item_count"],
                    },
                },
                "error_code": None,
                "cancel_requested": False,
                "cancel_requested_at": None,
            }
            self._jobs[job_id] = job
            self._persist(job)
            for superseded_id in current.get("superseded_pending_job_ids") or []:
                superseded = self._jobs.get(str(superseded_id))
                if superseded is None:
                    raise BatchError(
                        "Superseded solution relink job disappeared", status=409
                    )
                superseded["superseded_by"] = job_id
                superseded["revision"] = int(superseded.get("revision") or 0) + 1
                superseded["updated_at"] = timestamp
                self._persist(superseded)
            record["consumed_at"] = timestamp
            record["batch_id"] = job_id
            atomic_write_json(plan_path, record)
        self._executor.submit(self._run_solution_relink, job_id)
        return self._public(job)

    def _load_existing(self, *, recover_active: bool = True) -> None:
        for path in sorted(self.jobs_root.glob("*/job.json")):
            try:
                record = load_json_object(path)
                job_id = record["id"]
                if not isinstance(job_id, str) or path.parent.name != job_id:
                    continue
                if recover_active and record.get("state") in ACTIVE_STATES:
                    cancelled = record.get("cancel_requested") is True and not record.get("crop_job_id")
                    timestamp = _now()
                    record.update(
                        state="cancelled" if cancelled else "interrupted",
                        stage="cancelled" if cancelled else "interrupted",
                        message=(
                            "Batch cancellation completed when the application stopped."
                            if cancelled
                            else "The application restarted before this batch completed."
                        ),
                        event="cancelled" if cancelled else "app_restarted",
                        error_code="cancelled_by_user" if cancelled else "app_restarted",
                        revision=int(record.get("revision") or 0) + 1,
                        updated_at=timestamp,
                        finished_at=timestamp,
                    )
                    atomic_write_json(path, record)
                self._jobs[job_id] = record
            except (OSError, KeyError, ModelError):
                continue

    def create(
        self,
        payload: Mapping[str, Any],
        *,
        source_name: str | None = None,
        solution_name: str | None = None,
        source_upload: Mapping[str, Any] | None = None,
        solution_upload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise BatchError("Request body must be an object")
        if any(not isinstance(key, str) or key not in REQUEST_FIELDS for key in payload):
            raise BatchError("Request contains unsupported fields")
        source = self._pdf(
            self._alias(payload, ("source_path", "problem_pdf", "source")),
            "source_path",
            required=True,
        )
        solution = self._pdf(
            self._alias(
                payload, ("solution_path", "solution_pdf", "solution_source")
            ),
            "solution_path",
            required=False,
        )
        source_upload_identity = self._upload_identity(source_upload, source)
        solution_upload_identity = self._upload_identity(solution_upload, solution)
        pages = self._pages(payload.get("pages", "all"))
        workers = self._choice_int(payload.get("workers", 2), "workers", {1, 2, 3, 4})
        crop_dpi = self._choice_int(
            payload.get("crop_dpi", 300), "crop_dpi", {300, 450}
        )
        profile = str(payload.get("ocr_profile") or "balanced")
        if profile not in PROVIDER_DEFAULTS:
            raise BatchError("Unsupported OCR profile")
        try:
            provider_profile = parse_provider_profile(payload.get("provider_profile"))
        except CandidateValidationError as exc:
            raise BatchError("Unsupported provider profile") from exc
        regenerate_figures, figure_profile, figure_budget, remove_blue_text = (
            validate_figure_options(payload)
        )

        job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        timestamp = _now()
        record: dict[str, Any] = {
            "schema_version": "0.1.0",
            "id": job_id,
            "attempt": 1,
            "resume_root": job_id,
            "revision": 1,
            "event": "queued",
            "state": "queued",
            "stage": "queued",
            "progress": 0,
            "message": "Batch is queued.",
            "created_at": timestamp,
            "updated_at": timestamp,
            "finished_at": None,
            # Private persistence fields are removed from list/get/create views.
            "request": {
                "source_path": str(source),
                "solution_path": str(solution) if solution else None,
                "source_name": self._display_name(source_name, source.name),
                "solution_name": (
                    self._display_name(solution_name, solution.name)
                    if solution
                    else None
                ),
                "source_upload": source_upload_identity,
                "solution_upload": solution_upload_identity,
                "pages": pages,
                "workers": workers,
                "crop_dpi": crop_dpi,
                "ocr_profile": profile,
                "regenerate_figures": regenerate_figures,
                "figure_profile": figure_profile,
                "figure_budget_usd": float(figure_budget),
                "remove_blue_text": remove_blue_text,
                "provider_profile": provider_profile,
            },
            "crop_job_id": None,
            "project_id": None,
            "summary": {
                "crop": {"total": 0, "passed": 0, "skipped": 0},
                "ocr": {
                    "attempted": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "question_succeeded": 0,
                    "question_failed": 0,
                    "explanation_succeeded": 0,
                    "explanation_failed": 0,
                    "failures": [],
                },
                "figures": {
                    "eligible": 0,
                    "attempted": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "skipped_budget": 0,
                    "cached": 0,
                    "estimated_output_usd": 0.0,
                    "failures": [],
                },
            },
            "error_code": None,
            "cancel_requested": False,
            "cancel_requested_at": None,
        }
        with self._lock:
            self._jobs[job_id] = record
            self._persist(record)
        self._executor.submit(self._run, job_id)
        return self._public(record)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            records = sorted(
                self._jobs.values(), key=lambda item: str(item["created_at"]), reverse=True
            )
            visible = [self._public(record) for record in records]
        inspected: set[str] = set()
        for record, item in zip(records, visible):
            if (record.get("kind") != "solution_relink" or record.get("state") != "interrupted"
                    or record.get("recovery_consumed_by") or record.get("superseded_by")):
                continue
            request = record.get("request")
            upstream = record.get("upstream_requested")
            crop_id = record.get("crop_job_id")
            if (not isinstance(request, Mapping) or not isinstance(upstream, Mapping)
                    or not isinstance(crop_id, str) or crop_id in inspected):
                continue
            inspected.add(crop_id)
            if (upstream.get("crop_job_id") != crop_id
                    or upstream.get("solution_sha256") != request.get("solution_sha256")
                    or upstream.get("provider_profile") != request.get("provider_profile")):
                continue
            try:
                crop = self._crop_job_snapshot(crop_id)
            except (BatchError, ModelError, OSError):
                continue
            solution = crop.get("solution")
            options = crop.get("options")
            if (crop.get("state") != "completed" or not isinstance(solution, Mapping)
                    or not isinstance(options, Mapping)
                    or options.get("provider_profile") != request.get("provider_profile")
                    or solution.get("source") != request.get("solution_path")
                    or solution.get("state") not in {"queued", "indexing", "linking", "completed"}
                    or not isinstance(solution.get("updated_at"), str)
                    or not isinstance(upstream.get("requested_at"), str)
                    or solution["updated_at"] < upstream["requested_at"]):
                continue
            item["upstream_solution_state"] = solution["state"]
        return visible

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise BatchError("Batch job was not found", status=404)
            return self._public(record)

    def project_provider_profile(self, project_id: str) -> str:
        try:
            project_id = validate_id(project_id, field="project_id")
        except ModelError as exc:
            raise BatchError("Project was not found", status=404) from exc
        project_path = self.workspace_root / project_id / "project.json"
        if not project_path.is_file():
            raise BatchError("Project was not found", status=404)
        project = load_json_object(project_path)
        profiles: set[str] = set()
        if "provider_profile" in project:
            try:
                profiles.add(parse_provider_profile(project.get("provider_profile")))
            except CandidateValidationError as exc:
                raise BatchError("Project provider profile is invalid", status=409) from exc
        source_import = project.get("source_import")
        if isinstance(source_import, Mapping) and "provider_profile" in source_import:
            try:
                profiles.add(
                    parse_provider_profile(source_import.get("provider_profile"))
                )
            except CandidateValidationError as exc:
                raise BatchError("Project provider profile is invalid", status=409) from exc
        with self._lock:
            records = [
                copy.deepcopy(record)
                for record in self._jobs.values()
                if record.get("project_id") == project_id
            ]
        for item in records:
            request = item.get("request")
            if not isinstance(request, Mapping) or "provider_profile" not in request:
                continue
            try:
                profiles.add(parse_provider_profile(request.get("provider_profile")))
            except CandidateValidationError as exc:
                raise BatchError("Project provider profile is invalid", status=409) from exc
        if len(profiles) > 1:
            raise BatchError("Project provider profile is ambiguous", status=409)
        return next(iter(profiles), "full")

    @staticmethod
    def _bind_project_provider_profile(project_dir: Path, expected: str) -> None:
        expected = parse_provider_profile(expected)
        project_path = project_dir / "project.json"
        project = load_json_object(project_path)
        recorded = project.get("provider_profile")
        if recorded is not None:
            try:
                recorded = parse_provider_profile(recorded)
            except CandidateValidationError as exc:
                raise BatchError("Project provider profile is invalid", status=409) from exc
            if recorded != expected:
                raise BatchError("Project provider profile cannot change", status=409)
            return
        project["provider_profile"] = expected
        atomic_write_json(project_path, project)

    def retained_upload_ids(self) -> set[str]:
        """Uploads still required to start or resume work before project import."""

        with self._lock:
            records = [copy.deepcopy(record) for record in self._jobs.values()]
        retained: set[str] = set()
        for record in records:
            if record.get("state") == "completed" or self._stored_project_dir(record):
                continue
            if record.get("state") not in ACTIVE_STATES and not self._resume_not_expired(record):
                continue
            retained.update(self._record_upload_ids(record))
        return retained

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Request cooperative cancellation, or cancel a queued job immediately."""

        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise BatchError("Batch job was not found", status=404)
            state = str(record.get("state") or "")
            if state == "cancelled":
                return self._public(record)
            if state not in ACTIVE_STATES:
                raise BatchError("Only an active batch can be cancelled", status=409)
            timestamp = _now()
            if state == "queued":
                record.update(
                    state="cancelled",
                    stage="cancelled",
                    message="Batch was cancelled before processing started.",
                    event="cancelled",
                    error_code="cancelled_by_user",
                    cancel_requested=True,
                    cancel_requested_at=timestamp,
                    finished_at=timestamp,
                    updated_at=timestamp,
                    revision=int(record.get("revision") or 0) + 1,
                )
            elif record.get("cancel_requested") is not True:
                record.update(
                    message=(
                        "Cancellation requested; stopping the active crop process."
                    ),
                    event="cancel_requested",
                    error_code="cancel_requested",
                    cancel_requested=True,
                    cancel_requested_at=timestamp,
                    updated_at=timestamp,
                    revision=int(record.get("revision") or 0) + 1,
                )
            self._persist(record)
            result = self._public(record)
        if result.get("crop_job_id") and result.get("state") == "polling_crop":
            try:
                self._checkpoint(job_id)
            except _BatchCancelled:
                return self.get(job_id)
        return result

    def resume(self, job_id: str) -> dict[str, Any]:
        """Queue a safe new attempt using the original input and durable outputs."""

        with self._lock:
            source = self._jobs.get(job_id)
            if source is None:
                raise BatchError("Batch job was not found", status=404)
            if source.get("state") not in {"failed", "interrupted", "cancelled"}:
                raise BatchError(
                    "Only a failed, interrupted, or cancelled batch can be resumed",
                    status=409,
                )
            if not self._resume_not_expired(source):
                raise BatchError("This batch resume window has expired", status=409)
            root_id = str(source.get("resume_root") or source.get("id") or "")
            if any(
                record.get("state") in ACTIVE_STATES
                and str(record.get("resume_root") or record.get("id") or "") == root_id
                for record in self._jobs.values()
            ):
                raise BatchError("This batch already has an active attempt", status=409)
            if any(record.get("resume_of") == job_id for record in self._jobs.values()):
                raise BatchError("This batch already has a newer resume", status=409)
            source_copy = copy.deepcopy(source)

        request_value = source_copy.get("request")
        request = request_value if isinstance(request_value, Mapping) else {}
        retry_style = bool(
            source_copy.get("retry_of")
            or source_copy.get("kind") in {"missing_question_ocr", "missing_content_ocr"}
        )
        if retry_style:
            targets = self._retry_targets(source_copy, allow_empty=True)
            profile = str(request.get("ocr_profile") or "balanced")
            if profile not in PROVIDER_DEFAULTS:
                raise BatchError("The batch OCR profile is unavailable", status=409)
            try:
                provider_profile = parse_provider_profile(
                    request.get("provider_profile")
                )
            except CandidateValidationError as exc:
                raise BatchError(
                    "The batch provider profile is unavailable", status=409
                ) from exc
        else:
            if self._stored_project_dir(source_copy) is None:
                self._request_pdf(request, "source", required=True)
                self._request_pdf(request, "solution", required=False)
            targets = []

        timestamp = _now()
        resume_id = (
            "resume-"
            + datetime.now().strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        record = copy.deepcopy(source_copy)
        record.update(
            id=resume_id,
            resume_of=job_id,
            resume_root=root_id,
            attempt=int(source_copy.get("attempt") or 1) + 1,
            revision=1,
            event="resume_queued",
            state="queued",
            stage="resume_queued",
            progress=0,
            message="Batch resume is queued.",
            created_at=timestamp,
            updated_at=timestamp,
            finished_at=None,
            error_code=None,
            cancel_requested=False,
            cancel_requested_at=None,
        )
        with self._lock:
            if any(
                item.get("state") in ACTIVE_STATES
                and str(item.get("resume_root") or item.get("id") or "") == root_id
                for item in self._jobs.values()
            ) or any(item.get("resume_of") == job_id for item in self._jobs.values()):
                raise BatchError("This batch already has a newer attempt", status=409)
            self._jobs[resume_id] = record
            self._persist(record)
        if source_copy.get("kind") == "solution_relink":
            self._executor.submit(self._run_solution_relink, resume_id)
        elif source_copy.get("kind") in {"solution_pipeline", "solution_operation"}:
            self._executor.submit(self._run_solution_operation, resume_id)
        elif retry_style:
            self._executor.submit(
                self._run_retry, resume_id, targets, profile, provider_profile
            )
        else:
            self._executor.submit(self._run, resume_id)
        return self._public(record)

    def retry(self, job_id: str) -> dict[str, Any]:
        """Queue only the OCR sections still recorded as failed."""

        with self._lock:
            source = self._jobs.get(job_id)
            if source is None:
                raise BatchError("Batch job was not found", status=404)
            if source.get("state") in ACTIVE_STATES:
                raise BatchError("An active batch cannot be retried", status=409)
            if source.get("state") != "completed" and not source.get("retry_of"):
                raise BatchError("Only a completed OCR batch can be retried", status=409)
            if any(record.get("retry_of") == job_id for record in self._jobs.values()):
                raise BatchError("This batch already has a newer retry", status=409)
            source_copy = copy.deepcopy(source)

        targets = self._retry_targets(source_copy)
        request_value = source_copy.get("request", {})
        request = request_value if isinstance(request_value, Mapping) else {}
        profile = str(request.get("ocr_profile") or "balanced")
        if profile not in PROVIDER_DEFAULTS:
            raise BatchError("The batch OCR profile is unavailable", status=409)
        try:
            provider_profile = parse_provider_profile(request.get("provider_profile"))
        except CandidateValidationError as exc:
            raise BatchError(
                "The batch provider profile is unavailable", status=409
            ) from exc

        timestamp = _now()
        retry_id = (
            "retry-"
            + datetime.now().strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        failures = [
            {"unit_id": unit_id, "section": section, "error_code": error_code}
            for _, _, section, unit_id, error_code in targets
        ]
        record: dict[str, Any] = {
            "schema_version": "0.1.0",
            "id": retry_id,
            "retry_of": job_id,
            "attempt": 1,
            "resume_root": retry_id,
            "revision": 1,
            "event": "queued",
            "state": "queued",
            "stage": "retry_queued",
            "progress": 0,
            "message": "Failed OCR sections are queued for retry.",
            "created_at": timestamp,
            "updated_at": timestamp,
            "finished_at": None,
            "request": {
                key: request.get(key)
                for key in (
                    "source_name",
                    "solution_name",
                    "pages",
                    "workers",
                    "crop_dpi",
                    "ocr_profile",
                    "provider_profile",
                )
            },
            "crop_job_id": source_copy.get("crop_job_id"),
            "project_id": source_copy.get("project_id"),
            "summary": {
                "crop": copy.deepcopy(
                    source_copy.get("summary", {}).get("crop", {})
                    if isinstance(source_copy.get("summary"), Mapping)
                    else {}
                ),
                "ocr": {
                    "total": len(targets),
                    "attempted": 0,
                    "succeeded": 0,
                    "failed": len(targets),
                    "question_succeeded": 0,
                    "question_failed": sum(
                        section == "question" for _, _, section, _, _ in targets
                    ),
                    "explanation_succeeded": 0,
                    "explanation_failed": sum(
                        section == "explanation" for _, _, section, _, _ in targets
                    ),
                    "failures": failures,
                },
            },
            "error_code": None,
            "cancel_requested": False,
            "cancel_requested_at": None,
        }
        with self._lock:
            if any(item.get("retry_of") == job_id for item in self._jobs.values()):
                raise BatchError("This batch already has a newer retry", status=409)
            self._jobs[retry_id] = record
            self._persist(record)
        self._executor.submit(
            self._run_retry, retry_id, targets, profile, provider_profile
        )
        return self._public(record)

    def retry_figures(self, job_id: str) -> dict[str, Any]:
        """Retry only unresolved failed figure blocks with the original budget."""

        with self._lock:
            source = self._jobs.get(job_id)
            if source is None:
                raise BatchError("Batch job was not found", status=404)
            if source.get("state") not in TERMINAL_STATES:
                raise BatchError("Only a terminal batch can retry figures", status=409)
            project_id = source.get("project_id")
            if not isinstance(project_id, str) or self._stored_project_dir(source) is None:
                raise BatchError("The figure retry project is unavailable", status=409)
            if any(record.get("figure_retry_of") == job_id for record in self._jobs.values()):
                raise BatchError("This batch already has a newer figure retry", status=409)
            if any(
                record.get("kind") == "figure_retry"
                and record.get("project_id") == project_id
                and record.get("state") in ACTIVE_STATES
                for record in self._jobs.values()
            ):
                raise BatchError("A figure retry is already active for this project", status=409)
            source_copy = copy.deepcopy(source)

        request_value = source_copy.get("request")
        request = request_value if isinstance(request_value, Mapping) else {}
        try:
            provider_profile = parse_provider_profile(request.get("provider_profile"))
        except CandidateValidationError as exc:
            raise BatchError(
                "The batch provider profile is unavailable", status=409
            ) from exc
        if provider_profile == "codex_only":
            raise BatchError("Codex-only batches cannot retry figures", status=409)
        targets = self._figure_retry_targets(source_copy)
        profile = request.get("figure_profile")
        remove_blue_text = request.get("remove_blue_text")
        try:
            budget_usd = Decimal(str(request.get("figure_budget_usd")))
        except (AttributeError, InvalidOperation) as exc:
            raise BatchError("The stored figure budget is invalid", status=409) from exc
        if (
            profile not in FIGURE_PROFILES
            or not isinstance(remove_blue_text, bool)
            or not budget_usd.is_finite()
            or budget_usd < 0
        ):
            raise BatchError("The stored figure options are invalid", status=409)
        prior_spend_usd = self._successful_figure_spend(source_copy, str(profile))

        source_failures = {
            (str(failure.get("item_id")), str(failure.get("block_id"))): str(
                failure.get("error_code") or "figure_generation_failed"
            )
            for failure in source_copy.get("summary", {}).get("figures", {}).get(
                "failures", []
            )
            if isinstance(failure, Mapping)
        }
        failures = [
            {
                "item_id": item_id,
                "unit_id": unit_id,
                "section": section,
                "block_id": block_id,
                "error_code": source_failures.get(
                    (item_id, block_id), "figure_generation_failed"
                ),
            }
            for _, item_id, unit_id, section, block_id in targets
        ]
        timestamp = _now()
        retry_id = (
            "figure-retry-"
            + datetime.now().strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        record: dict[str, Any] = {
            "schema_version": "0.1.0",
            "id": retry_id,
            "kind": "figure_retry",
            "figure_retry_of": job_id,
            "figure_spend_before_usd": float(prior_spend_usd),
            "attempt": 1,
            "resume_root": retry_id,
            "revision": 1,
            "event": "figure_retry_queued",
            "state": "queued",
            "stage": "figure_retry_queued",
            "progress": 0,
            "message": f"Failed figures are queued for retry (0/{len(targets)}).",
            "created_at": timestamp,
            "updated_at": timestamp,
            "finished_at": None,
            "request": {
                "source_name": request.get("source_name"),
                "solution_name": request.get("solution_name"),
                "regenerate_figures": True,
                "figure_profile": profile,
                "figure_budget_usd": float(budget_usd),
                "remove_blue_text": remove_blue_text,
            },
            "crop_job_id": source_copy.get("crop_job_id"),
            "project_id": project_id,
            "summary": {
                "crop": copy.deepcopy(
                    source_copy.get("summary", {}).get("crop", {})
                ),
                "ocr": {
                    "total": 0,
                    "attempted": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "question_succeeded": 0,
                    "question_failed": 0,
                    "explanation_succeeded": 0,
                    "explanation_failed": 0,
                    "failures": [],
                },
                "figures": {
                    "eligible": len(targets),
                    "attempted": 0,
                    "succeeded": 0,
                    "failed": len(targets),
                    "skipped_budget": 0,
                    "cached": 0,
                    "estimated_output_usd": float(prior_spend_usd),
                    "failures": failures,
                },
            },
            "error_code": None,
            "cancel_requested": False,
            "cancel_requested_at": None,
        }
        with self._lock:
            if any(item.get("figure_retry_of") == job_id for item in self._jobs.values()):
                raise BatchError("This batch already has a newer figure retry", status=409)
            if any(
                item.get("kind") == "figure_retry"
                and item.get("project_id") == project_id
                and item.get("state") in ACTIVE_STATES
                for item in self._jobs.values()
            ):
                raise BatchError("A figure retry is already active for this project", status=409)
            self._jobs[retry_id] = record
            self._persist(record)
        self._executor.submit(
            self._run_figure_retry,
            retry_id,
            targets,
            str(profile),
            budget_usd,
            bool(remove_blue_text),
            prior_spend_usd,
        )
        return self._public(record)

    def transcribe_missing(
        self,
        project_id: str,
        profile: str = "balanced",
        provider_profile: Any = None,
    ) -> dict[str, Any]:
        """Queue missing question and registered solution-candidate OCR."""

        try:
            project_id = validate_id(project_id, field="project_id")
        except ModelError as exc:
            raise BatchError("Project was not found", status=404) from exc
        if profile not in PROVIDER_DEFAULTS:
            raise BatchError("Unsupported OCR profile")
        effective_provider_profile = self.project_provider_profile(project_id)
        if provider_profile is None:
            provider_profile = effective_provider_profile
        else:
            try:
                provider_profile = parse_provider_profile(provider_profile)
            except CandidateValidationError as exc:
                raise BatchError("Unsupported provider profile") from exc
            if provider_profile != effective_provider_profile:
                raise BatchError(
                    "Project provider profile cannot be overridden", status=409
                )
        project_dir = (self.workspace_root / project_id).resolve()
        project_path = project_dir / "project.json"
        if not project_dir.is_relative_to(self.workspace_root) or not project_path.is_file():
            raise BatchError("Project was not found", status=404)
        project = load_json_object(project_path)
        if project.get("project_id") != project_id:
            raise BatchError("Project was not found", status=404)

        targets: list[tuple[Path, str, str, str, str]] = []
        item_order = project.get("item_order")
        if not isinstance(item_order, list):
            raise BatchError("Stored project is invalid", status=409)
        for item_id in item_order:
            if not isinstance(item_id, str):
                continue
            question_path = project_dir / "items" / item_id / "question.json"
            document = load_json_object(question_path)
            content = document.get("content")
            question = content.get("question") if isinstance(content, Mapping) else None
            blocks = question.get("blocks") if isinstance(question, Mapping) else None
            problem = document.get("provenance", {}).get("problem", {})
            unit_id = problem.get("unit_id") if isinstance(problem, Mapping) else None
            target_unit = str(unit_id or item_id)
            if not (isinstance(blocks, list) and blocks):
                resources = document.get("resources")
                assets = resources.get("assets") if isinstance(resources, Mapping) else None
                source = next(
                    (
                        asset
                        for asset in (assets if isinstance(assets, list) else [])
                        if isinstance(asset, Mapping)
                        and asset.get("role") == "source_crop"
                        and asset.get("section") in {"question", "shared"}
                        and asset.get("state") == "ready"
                        and isinstance(asset.get("asset_id"), str)
                    ),
                    None,
                )
                if source is not None:
                    targets.append(
                        (
                            question_path,
                            str(source["asset_id"]),
                            "question",
                            target_unit,
                            "missing_transcription",
                        )
                    )
            explanation = content.get("explanation") if isinstance(content, Mapping) else None
            explanation_blocks = (
                explanation.get("blocks") if isinstance(explanation, Mapping) else None
            )
            selected_link = self._registered_explanation_link(document)
            explanation_asset = (
                str(selected_link["source_asset_id"])
                if selected_link is not None
                else None
            )
            invalid_bogi_labels = self._has_invalid_bogi_labels(explanation)
            stale_explanation_source = self._has_stale_explanation_source(
                document, selected_link
            )
            if (
                not (isinstance(explanation_blocks, list) and explanation_blocks)
                or invalid_bogi_labels
                or stale_explanation_source
            ):
                if explanation_asset:
                    targets.append(
                        (
                            question_path,
                            explanation_asset,
                            "explanation",
                            target_unit,
                            (
                                "invalid_bogi_labels"
                                if invalid_bogi_labels
                                else "stale_explanation_source"
                                if stale_explanation_source
                                else "missing_transcription"
                            ),
                        )
                    )
        if not targets:
            raise BatchError("This project has no missing question or solution OCR", status=409)

        timestamp = _now()
        job_id = (
            "missing-ocr-"
            + datetime.now().strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        failures = [
            {"unit_id": unit_id, "section": section, "error_code": error_code}
            for _, _, section, unit_id, error_code in targets
        ]
        metadata = project.get("metadata")
        source_name = (
            metadata.get("filename_base") if isinstance(metadata, Mapping) else project_id
        )
        record: dict[str, Any] = {
            "schema_version": "0.1.0",
            "id": job_id,
            "kind": "missing_content_ocr",
            "attempt": 1,
            "resume_root": job_id,
            "revision": 1,
            "event": "queued",
            "state": "queued",
            "stage": "missing_ocr_queued",
            "progress": 0,
            "message": f"미전사 문제·해설 {len(targets)}개를 OCR 대기열에 등록했습니다.",
            "created_at": timestamp,
            "updated_at": timestamp,
            "finished_at": None,
            "request": {
                "source_name": str(source_name or project_id),
                "solution_name": None,
                "ocr_profile": profile,
                "provider_profile": provider_profile,
            },
            "crop_job_id": None,
            "project_id": project_id,
            "summary": {
                "crop": {
                    "total": len(item_order),
                    "passed": len(item_order),
                    "review": 0,
                    "failed": 0,
                    "registered": len(item_order),
                    "skipped": 0,
                },
                "ocr": {
                    "total": len(targets),
                    "attempted": 0,
                    "succeeded": 0,
                    "failed": len(targets),
                    "question_succeeded": 0,
                    "question_failed": sum(
                        section == "question" for _, _, section, _, _ in targets
                    ),
                    "explanation_succeeded": 0,
                    "explanation_failed": sum(
                        section == "explanation" for _, _, section, _, _ in targets
                    ),
                    "failures": failures,
                },
            },
            "error_code": None,
            "cancel_requested": False,
            "cancel_requested_at": None,
        }
        with self._lock:
            if any(
                job.get("kind") in {"missing_question_ocr", "missing_content_ocr"}
                and job.get("project_id") == project_id
                and job.get("state") in ACTIVE_STATES
                for job in self._jobs.values()
            ):
                raise BatchError("Missing question OCR is already running", status=409)
            self._jobs[job_id] = record
            self._persist(record)
        self._executor.submit(
            self._run_retry, job_id, targets, profile, provider_profile
        )
        return self._public(record)

    def close(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _persist(self, record: Mapping[str, Any]) -> None:
        atomic_write_json(self.jobs_root / str(record["id"]) / "job.json", record)

    def _update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            record = self._jobs[job_id]
            if record.get("state") == "cancelled":
                return copy.deepcopy(record)
            if "event" not in changes:
                if "state" in changes:
                    changes["event"] = f"state:{changes['state']}"
                elif "stage" in changes:
                    changes["event"] = f"stage:{changes['stage']}"
            record.update(
                changes,
                revision=int(record.get("revision") or 0) + 1,
                updated_at=_now(),
            )
            self._persist(record)
            return copy.deepcopy(record)

    def _confirm_crop_cancel(self, job_id: str) -> bool:
        """Leave cancellation pending unless the crop server confirms tree exit."""
        with self._lock:
            record = self._jobs[job_id]
            if not record.get("cancel_requested") or record.get("state") == "cancelled":
                return True
            crop_job_id = record.get("crop_job_id")
        if not crop_job_id:
            return True
        try:
            crop = self._request_json("POST", f"/api/jobs/{quote(str(crop_job_id), safe='')}/cancel", {})
        except RuntimeError:
            crop = {}
        solution = crop.get("solution") or {}
        stopped = (
            crop.get("error_code") != "cancel_failed"
            and crop.get("state") in {"cancelled", "failed", "interrupted", "completed"}
            and solution.get("state") not in {"queued", "indexing", "linking"}
        )
        if not stopped:
            self._update(job_id, error_code="cancel_failed", event="cancel_requested", message="작업 종료를 확인하지 못했습니다. 취소를 다시 요청해 주세요.")
        return stopped

    def _checkpoint(self, job_id: str) -> None:
        """Finish a requested cancellation only between durable work units."""

        with self._lock:
            if not self._confirm_crop_cancel(job_id):
                raise _BatchCancelled
            record = self._jobs[job_id]
            if record.get("state") == "cancelled":
                raise _BatchCancelled
            if record.get("cancel_requested") is not True:
                return
            timestamp = _now()
            record.update(
                state="cancelled",
                stage="cancelled",
                message="Batch cancellation completed at a safe step boundary.",
                event="cancelled",
                error_code="cancelled_by_user",
                revision=int(record.get("revision") or 0) + 1,
                updated_at=timestamp,
                finished_at=timestamp,
            )
            self._persist(record)
        self._cleanup_uploads_if_safe(job_id)
        raise _BatchCancelled

    def _finish(
        self,
        job_id: str,
        *,
        state: str,
        stage: str,
        progress: int,
        message: str,
        error_code: str | None = None,
    ) -> None:
        """Commit one terminal transition without racing a successful cancel request."""

        with self._lock:
            if not self._confirm_crop_cancel(job_id):
                return
            record = self._jobs[job_id]
            timestamp = _now()
            if record.get("cancel_requested") is True:
                record.update(
                    state="cancelled",
                    stage="cancelled",
                    message="Batch cancellation completed at a safe step boundary.",
                    event="cancelled",
                    error_code="cancelled_by_user",
                )
            else:
                record.update(
                    state=state,
                    stage=stage,
                    progress=progress,
                    message=message,
                    event=f"state:{state}",
                    error_code=error_code,
                )
            record.update(
                revision=int(record.get("revision") or 0) + 1,
                updated_at=timestamp,
                finished_at=timestamp,
            )
            self._persist(record)
        self._cleanup_uploads_if_safe(job_id)

    @staticmethod
    def _record_upload_ids(record: Mapping[str, Any]) -> set[str]:
        request = record.get("request")
        if not isinstance(request, Mapping):
            return set()
        result: set[str] = set()
        for field in ("source_upload", "solution_upload"):
            identity = request.get(field)
            upload_id = identity.get("upload_id") if isinstance(identity, Mapping) else None
            if isinstance(upload_id, str):
                result.add(upload_id)
        return result

    def _cleanup_uploads_if_safe(self, job_id: str) -> None:
        try:
            with self._lock:
                record = copy.deepcopy(self._jobs[job_id])
                if record.get("state") not in TERMINAL_STATES:
                    return
                if record.get("state") != "completed" and self._stored_project_dir(record) is None:
                    return
                upload_ids = self._record_upload_ids(record)
                retained_elsewhere = set()
                for other_id, other in self._jobs.items():
                    if other_id == job_id:
                        continue
                    if (
                        other.get("state") != "completed"
                        and self._stored_project_dir(other) is None
                        and (
                            other.get("state") in ACTIVE_STATES
                            or self._resume_not_expired(other)
                        )
                    ):
                        retained_elsewhere.update(self._record_upload_ids(other))
            for upload_id in upload_ids - retained_elsewhere:
                try:
                    delete_pdf_upload(self.workspace_root, upload_id)
                except (PdfUploadError, OSError):
                    continue
        except (KeyError, ModelError, OSError, TypeError, ValueError):
            return

    @staticmethod
    def _resume_not_expired(record: Mapping[str, Any]) -> bool:
        if record.get("state") not in {"failed", "interrupted", "cancelled"}:
            return False
        updated_at = record.get("updated_at")
        if not isinstance(updated_at, str):
            return False
        try:
            updated = datetime.fromisoformat(updated_at)
            if updated.tzinfo is None:
                return False
            return datetime.now(timezone.utc) - updated.astimezone(timezone.utc) <= RESUME_RETENTION
        except (OverflowError, ValueError):
            return False

    @staticmethod
    def _public(record: Mapping[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(dict(record))
        request = result.pop("request", {})
        state = str(result.get("state") or "queued")
        result["status"] = (
            state if state in {"queued", *TERMINAL_STATES} else "running"
        )
        result["can_cancel"] = (
            result.get("kind") != "solution_relink"
            and state in ACTIVE_STATES
            and (result.get("cancel_requested") is not True or result.get("error_code") == "cancel_failed")
        )
        result["can_resume"] = (
            result.get("kind")
            not in {"figure_retry", "bank_figure_generation", "solution_relink"}
            and BatchCoordinator._resume_not_expired(result)
        )
        result["phase"] = {
            "queued": "preparing",
            "starting_crop": "preparing",
            "polling_crop": "cropping",
            "importing": "validating",
            "ocr": "transcribing",
            "figure_generation": "figure_generation",
            "completed": "finalizing",
            "failed": "failed",
            "interrupted": "interrupted",
            "cancelled": "cancelled",
        }.get(state, "preparing")
        if state == "polling_crop" and result.get("stage") in {
            "solution",
            "solution_retry",
        }:
            result["phase"] = "matching"
        failure = parse_page_failure(result.get("detection_failure"))
        if state == "failed" and failure is not None:
            result["phase"] = failure["stage"]
        result["source_name"] = request.get("source_name")
        result["solution_name"] = request.get("solution_name")
        result["options"] = {
            key: request.get(key)
            for key in (
                "pages",
                "workers",
                "crop_dpi",
                "ocr_profile",
                "provider_profile",
                "regenerate_figures",
                "figure_profile",
                "figure_budget_usd",
                "remove_blue_text",
            )
        }
        if result["options"].get("regenerate_figures") is True:
            result["options"].update(
                figure_budget_scope="estimated_image_output_only",
                figure_input_and_token_costs_included=False,
                figure_cache_scope="same_project_item_block",
            )
        if result.get("kind") == "bank_figure_generation":
            result["options"] = {
                "figure_profile": "quality",
                "figure_budget_usd": request.get("estimated_output_usd_max"),
                "remove_blue_text": False,
                "figure_budget_scope": "estimated_image_output_only",
                "figure_input_and_token_costs_included": False,
                "figure_cache_scope": "cross_project_semantic_artifact",
            }
        summary = result.get("summary")
        if isinstance(summary, dict):
            crop = summary.get("crop") if isinstance(summary.get("crop"), dict) else {}
            ocr = summary.get("ocr") if isinstance(summary.get("ocr"), dict) else {}
            figures = (
                summary.get("figures")
                if isinstance(summary.get("figures"), dict)
                else {}
            )
            solution = (
                summary.get("solution")
                if isinstance(summary.get("solution"), dict)
                else {}
            )
            solution_affected = (
                max(0, int(solution.get("affected_items") or 0))
                if solution.get("state") == "failed"
                else 0
            )
            warning_count = (
                int(crop.get("review") or 0)
                + int(crop.get("failed") or 0)
                + int(ocr.get("failed") or 0)
                + int(figures.get("failed") or 0)
                + int(figures.get("skipped_budget") or 0)
                + solution_affected
            )
            summary.update(
                registered=int(crop.get("registered") or crop.get("total") or 0),
                warning_count=warning_count,
                source_fallback_sections=(
                    int(crop.get("failed") or 0) + int(ocr.get("failed") or 0)
                ),
                items=int(crop.get("total") or 0),
                crops=(
                    int(crop.get("passed") or 0) + int(crop.get("review") or 0)
                ),
                transcribed=int(ocr.get("succeeded") or 0),
                failed=int(ocr.get("failed") or 0) + int(figures.get("failed") or 0),
                skipped=int(crop.get("skipped") or 0),
            )
            result["errors"] = copy.deepcopy(
                (ocr.get("failures") or []) + (figures.get("failures") or [])
            )
            result["warnings"] = copy.deepcopy(result["errors"])
            if solution_affected:
                relink_failure = result.get("kind") == "solution_relink"
                result["warnings"].append(
                    {
                        "stage": "solution",
                        "section": "explanation",
                        "error_code": (
                            "solution_relink_failed"
                            if relink_failure
                            else "solution_link_failed"
                        ),
                        "affected_items": solution_affected,
                        "recovery_available": bool(
                            relink_failure
                            and isinstance(result.get("upstream_result"), Mapping)
                        ),
                        "message": (
                            (
                                "답·해설 재연결을 완료하지 못했습니다. 기존 저장 내용은 보존되며 같은 upstream 결과로 복구할 수 있습니다."
                                if isinstance(result.get("upstream_result"), Mapping)
                                else "답·해설 재연결을 시작하지 못했습니다. 기존 저장 내용은 변경되지 않았습니다."
                            )
                            if relink_failure
                            else "해설 연결을 완료하지 못해 문제만 저장했습니다. 각 문항은 해설 검토 필요로 표시했습니다."
                        ),
                    }
                )
            if result.get("project_id"):
                result["registration"] = {
                    "status": "registered",
                    "item_count": summary["registered"],
                    "warning_count": warning_count,
                    "source_fallback_sections": summary["source_fallback_sections"],
                }
        else:
            result["errors"] = []
            result["warnings"] = []
        return result

    def _retry_targets(
        self, record: Mapping[str, Any], *, allow_empty: bool = False
    ) -> list[tuple[Path, str, str, str, str]]:
        summary = record.get("summary", {})
        ocr = summary.get("ocr", {}) if isinstance(summary, Mapping) else {}
        failures = ocr.get("failures") if isinstance(ocr, Mapping) else None
        if not isinstance(failures, list):
            raise BatchError("The stored OCR failure list is invalid", status=409)
        if not failures and allow_empty:
            return []
        if not failures:
            raise BatchError("This batch has no failed OCR sections", status=409)

        selected: dict[tuple[str, str], str] = {}
        for failure in failures:
            if not isinstance(failure, Mapping):
                raise BatchError("The stored OCR failure list is invalid", status=409)
            unit_id = failure.get("unit_id")
            section = failure.get("section")
            if not isinstance(unit_id, str) or section not in {"question", "explanation"}:
                raise BatchError("The stored OCR failure list is invalid", status=409)
            selected[(unit_id, str(section))] = str(
                failure.get("error_code") or "ocr_failed"
            )

        try:
            project_id = validate_id(str(record.get("project_id") or ""), field="project_id")
            project_dir = (self.workspace_root / project_id).resolve()
            if not _inside(project_dir, (self.workspace_root,)):
                raise ModelError("project path is outside workspace")
            project = load_json_object(project_dir / "project.json")
            item_order = project.get("item_order")
            if not isinstance(item_order, list):
                raise ModelError("project item_order is invalid")

            by_unit: dict[str, tuple[Path, dict[str, Any]]] = {}
            wanted_units = {unit_id for unit_id, _ in selected}
            for item_id in item_order:
                if not isinstance(item_id, str):
                    continue
                validate_id(item_id, field="item_id")
                question_path = (project_dir / "items" / item_id / "question.json").resolve()
                if not _inside(question_path, (project_dir,)) or not question_path.is_file():
                    continue
                document = load_json_object(question_path)
                problem = document.get("provenance", {}).get("problem", {})
                unit_id = problem.get("unit_id") if isinstance(problem, Mapping) else None
                unit_id = str(unit_id or item_id)
                if unit_id not in wanted_units:
                    continue
                if unit_id in by_unit:
                    raise ModelError("duplicate unit_id in project")
                by_unit[str(unit_id)] = (question_path, document)
        except (ModelError, OSError) as exc:
            raise BatchError("The retry project is unavailable", status=409) from exc

        targets: list[tuple[Path, str, str, str, str]] = []
        for (unit_id, section), error_code in selected.items():
            found = by_unit.get(unit_id)
            if found is None:
                raise BatchError("A failed OCR target is unavailable", status=409)
            question_path, document = found
            content = document.get("content")
            if isinstance(content, Mapping) and content.get(section) is not None:
                continue
            asset_id = (
                "source-question"
                if section == "question"
                else self._registered_explanation_asset(document)
            )
            if not asset_id:
                raise BatchError("A failed OCR target is unavailable", status=409)
            targets.append((question_path, asset_id, section, unit_id, error_code))
        if not targets and not allow_empty:
            raise BatchError("This batch has no unresolved failed OCR sections", status=409)
        return targets

    def _figure_retry_targets(
        self, record: Mapping[str, Any]
    ) -> list[tuple[Path, str, str, str, str]]:
        summary = record.get("summary")
        figures = summary.get("figures") if isinstance(summary, Mapping) else None
        failures = figures.get("failures") if isinstance(figures, Mapping) else None
        if not isinstance(failures, list) or not failures:
            raise BatchError("This batch has no failed figure blocks", status=409)

        wanted: dict[tuple[str, str], None] = {}
        for failure in failures:
            if not isinstance(failure, Mapping):
                raise BatchError("The stored figure failure list is invalid", status=409)
            item_id = failure.get("item_id")
            block_id = failure.get("block_id")
            try:
                item_id = validate_id(item_id, field="item_id")
                block_id = validate_id(block_id, field="block_id")
            except ModelError as exc:
                raise BatchError(
                    "The stored figure failure list is invalid", status=409
                ) from exc
            wanted[(item_id, block_id)] = None

        project_dir = self._stored_project_dir(record)
        if project_dir is None:
            raise BatchError("The figure retry project is unavailable", status=409)
        available = {
            (item_id, block_id): target
            for target in self._figure_targets(
                project_dir, self._project_units(project_dir), set()
            )
            for _, item_id, _, _, block_id in (target,)
        }
        targets = [
            available[key]
            for key in wanted
            if key in available
            and not self._has_successful_figure_candidate(
                available[key][0], available[key][4]
            )
        ]
        if not targets:
            raise BatchError("This batch has no unresolved failed figure blocks", status=409)
        return targets

    @staticmethod
    def _has_successful_figure_candidate(
        question_path: Path, block_id: str
    ) -> bool:
        document = load_json_object(question_path)
        resources = document.get("resources")
        assets = resources.get("assets") if isinstance(resources, Mapping) else None
        for asset in assets if isinstance(assets, list) else []:
            generation = asset.get("generation") if isinstance(asset, Mapping) else None
            job_id = generation.get("job_id") if isinstance(generation, Mapping) else None
            if (
                not isinstance(asset, Mapping)
                or asset.get("role") != "figure_regenerated"
                or asset.get("state") != "ready"
                or not isinstance(generation, Mapping)
                or generation.get("status") != "succeeded"
                or not isinstance(job_id, str)
            ):
                continue
            try:
                validate_id(job_id, field="figure_job_id")
                metadata = load_json_object(
                    question_path.parent
                    / "jobs"
                    / "figure_generation"
                    / job_id
                    / "job.json"
                )
            except (ModelError, OSError):
                continue
            if metadata.get("job_id") == job_id and metadata.get("block_id") == block_id:
                return True
        return False

    def _pdf(self, value: Any, field: str, *, required: bool) -> Path | None:
        text = str(value or "").strip()
        if not text:
            if required:
                raise BatchError(f"{field} is required")
            return None
        try:
            path = Path(text).expanduser().resolve(strict=True)
        except OSError as exc:
            raise BatchError(f"{field} is unavailable") from exc
        if not path.is_file() or path.suffix.casefold() != ".pdf":
            raise BatchError(f"{field} must be a PDF file")
        if not _inside(path, self.pdf_roots):
            raise BatchError(f"{field} is outside the configured PDF folders", status=403)
        return path

    def _upload_identity(
        self, value: Mapping[str, Any] | None, path: Path | None
    ) -> dict[str, str] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping) or path is None:
            raise BatchError("Invalid uploaded PDF identity")
        upload_id = value.get("upload_id")
        expected_sha = value.get("sha256")
        try:
            upload = resolve_pdf_upload(self.workspace_root, upload_id)
        except PdfUploadError as exc:
            raise BatchError("Uploaded PDF is unavailable or changed", status=409) from exc
        if upload.path != path or upload.sha256 != expected_sha:
            raise BatchError("Uploaded PDF identity does not match", status=409)
        return {"upload_id": upload.upload_id, "sha256": upload.sha256}

    def _request_pdf(
        self, request: Mapping[str, Any], prefix: str, *, required: bool
    ) -> Path | None:
        path = self._pdf(
            request.get(f"{prefix}_path"), f"{prefix}_path", required=required
        )
        identity = request.get(f"{prefix}_upload")
        if identity is not None:
            self._upload_identity(identity, path)
        return path

    @staticmethod
    def _alias(payload: Mapping[str, Any], names: Sequence[str]) -> Any:
        supplied = [payload[name] for name in names if str(payload.get(name) or "").strip()]
        if len({str(value).strip() for value in supplied}) > 1:
            raise BatchError(f"Conflicting aliases for {names[0]}")
        return supplied[0] if supplied else None

    @staticmethod
    def _display_name(value: str | None, fallback: str) -> str:
        name = str(value or fallback).strip()
        if not name or len(name) > 255 or Path(name).name != name:
            raise BatchError("Invalid uploaded PDF name")
        return name

    @staticmethod
    def _pages(value: Any) -> str:
        pages = str(value or "").strip().casefold()
        if pages == "all":
            return pages
        if not pages or len(pages) > 512:
            raise BatchError("Invalid page selection")
        tokens = pages.split(",")
        for token in tokens:
            parts = token.strip().split("-")
            if len(parts) not in {1, 2} or any(
                not part.isascii() or not part.isdecimal() or int(part) < 1
                for part in parts
            ):
                raise BatchError("Invalid page selection")
            if len(parts) == 2 and int(parts[0]) > int(parts[1]):
                raise BatchError("Invalid page selection")
        return pages

    @staticmethod
    def _choice_int(value: Any, name: str, allowed: set[int]) -> int:
        if type(value) is int:
            result = value
        elif isinstance(value, str) and value.isascii() and value.isdecimal():
            result = int(value)
        else:
            raise BatchError(f"Invalid {name}")
        if result not in allowed:
            raise BatchError(f"Invalid {name}")
        return result

    def _run_solution_relink(self, job_id: str) -> None:
        try:
            self._checkpoint(job_id)
            with self._lock:
                record = copy.deepcopy(self._jobs[job_id])
            request = record.get("request")
            if not isinstance(request, Mapping):
                raise RuntimeError("solution relink request is invalid")
            project_id = str(record.get("project_id") or "")
            crop_job_id = str(record.get("crop_job_id") or "")
            project_dir = (self.workspace_root / project_id).resolve()
            project_path = project_dir / "project.json"
            if not project_dir.is_relative_to(self.workspace_root) or not project_path.is_file():
                raise RuntimeError("solution relink project is unavailable")
            if (record.get("studio_reconciliation") or {}).get("state") == "completed":
                self._run_solution_pipeline_stage(job_id)
                self._finish(job_id, state="completed", stage="completed", progress=100,
                    message=self.get(job_id)["message"])
                return
            self._ensure_crop_engine()
            project = load_json_object(project_path)
            workflow = project.get("workflow")
            if (
                project.get("project_id") != project_id
                or not isinstance(workflow, Mapping)
                or workflow.get("revision") != request.get("project_revision")
            ):
                raise RuntimeError("project changed after solution relink preflight")
            solution_path = self._pdf(
                request.get("solution_path"), "solution_path", required=True
            )
            assert solution_path is not None
            if sha256_file(solution_path) != request.get("solution_sha256"):
                raise RuntimeError("solution PDF changed after preflight")

            current_record: Mapping[str, Any] = self._request_json(
                "GET", f"/api/jobs/{quote(crop_job_id, safe='')}"
            )
            provider_profile = parse_provider_profile(request.get("provider_profile"))
            self._require_crop_provider_profile(current_record, provider_profile)
            upstream_requested = record.get("upstream_requested")
            current_solution = current_record.get("solution")
            reattach = (
                isinstance(record.get("resume_of"), str)
                and not isinstance(record.get("upstream_result"), Mapping)
                and isinstance(upstream_requested, Mapping)
                and upstream_requested.get("crop_job_id") == crop_job_id
                and upstream_requested.get("leaf_run_id") == request.get("leaf_run_id")
                and upstream_requested.get("manifest_sha256") == request.get("manifest_sha256")
                and upstream_requested.get("source_link_manifest_sha256") == request.get("link_manifest_sha256")
                and upstream_requested.get("solution_sha256") == request.get("solution_sha256")
                and upstream_requested.get("provider_profile") == provider_profile
                and isinstance(current_solution, Mapping)
                and current_solution.get("source") == str(solution_path)
                and current_solution.get("state") in {"queued", "indexing", "linking", "completed"}
                and isinstance(current_solution.get("updated_at"), str)
                and isinstance(upstream_requested.get("requested_at"), str)
                and current_solution["updated_at"] >= upstream_requested["requested_at"]
            )
            if current_record.get("state") != "completed":
                raise RuntimeError("question crop job is not completed")
            current_documents = current_record.get("documents")
            if (
                not isinstance(current_documents, list)
                or len(current_documents) != 1
                or not isinstance(current_documents[0], Mapping)
            ):
                raise RuntimeError("solution relink requires exactly one crop document")
            current_document = current_documents[0]
            current_run_dir = self._allowed_run_dir(current_document)
            if (
                current_run_dir.name != request.get("leaf_run_id")
                or current_run_dir.parent.name != "output"
                or current_run_dir.parent.parent.name != crop_job_id
                or sha256_file(current_run_dir / "manifest.json")
                != request.get("manifest_sha256")
            ):
                raise RuntimeError("solution relink source identity changed")
            current_link_path = current_run_dir / "solutions" / "link_manifest.json"
            expected_link_sha = request.get("link_manifest_sha256")
            if not reattach and (
                (expected_link_sha is None and current_link_path.exists())
                or (
                    expected_link_sha is not None
                    and (
                        not current_link_path.is_file()
                        or sha256_file(current_link_path) != expected_link_sha
                    )
                )
            ):
                raise RuntimeError("solution links changed after preflight")
            if reattach:
                self._update(
                    job_id, state="polling_crop", stage="solution", progress=71,
                    message="Waiting for the existing Crop solution run.",
                )
                if current_solution.get("state") != "completed":
                    current_record = self._wait_for_crop(
                        job_id, crop_job_id, True, initial_record=current_record
                    )
            elif request.get("upstream_already_completed") is not True:
                self._update(
                    job_id,
                    upstream_requested={
                        "crop_job_id": crop_job_id,
                        "leaf_run_id": request.get("leaf_run_id"),
                        "manifest_sha256": request.get("manifest_sha256"),
                        "source_link_manifest_sha256": request.get(
                            "link_manifest_sha256"
                        ),
                        "solution_sha256": request.get("solution_sha256"),
                        "provider_profile": provider_profile,
                        "requested_at": _now(),
                    },
                    state="polling_crop",
                    stage="solution",
                    progress=5,
                    message="Recomputing answer and explanation links.",
                )
                self._request_json(
                    "POST",
                    f"/api/jobs/{quote(crop_job_id, safe='')}/solutions",
                    {
                        "source": str(solution_path),
                        "crop_dpi": int(request.get("crop_dpi") or 300),
                        "force": False,
                    },
                )
                current_record = self._wait_for_crop(
                    job_id, crop_job_id, True
                )
            solution = current_record.get("solution")
            if not isinstance(solution, Mapping) or solution.get("state") != "completed":
                raise RuntimeError("solution relinking did not complete")
            documents = current_record.get("documents")
            if (
                not isinstance(documents, list)
                or len(documents) != 1
                or not isinstance(documents[0], Mapping)
            ):
                raise RuntimeError("solution relink result changed document scope")
            document = documents[0]
            run_dir = self._allowed_run_dir(document)
            if (
                run_dir.name != request.get("leaf_run_id")
                or run_dir.parent.name != "output"
                or run_dir.parent.parent.name != crop_job_id
                or sha256_file(run_dir / "manifest.json")
                != request.get("manifest_sha256")
            ):
                raise RuntimeError("solution relink result identity changed")
            link_path = run_dir / "solutions" / "link_manifest.json"
            final_manifest = load_json_object(link_path)
            if self._manual_solution_count(final_manifest):
                raise RuntimeError("manual solution selections appeared during relinking")
            final_solution = final_manifest.get("solution")
            if (
                not isinstance(final_solution, Mapping)
                or str(final_solution.get("source_sha256") or "").casefold()
                != request.get("solution_sha256")
            ):
                raise RuntimeError("solution relink output used a different PDF")
            final_solution_path = self._pdf(
                final_solution.get("source_pdf"), "solution_path", required=True
            )
            assert final_solution_path is not None
            if sha256_file(final_solution_path) != request.get("solution_sha256"):
                raise RuntimeError("solution relink output PDF changed")
            new_link_sha = sha256_file(link_path)
            upstream_result = {
                "crop_job_id": crop_job_id,
                "leaf_run_id": run_dir.name,
                "manifest_sha256": request.get("manifest_sha256"),
                "link_manifest_sha256": new_link_sha,
                "solution_sha256": request.get("solution_sha256"),
                "provider_profile": provider_profile,
            }
            self._update(
                job_id,
                upstream_result=upstream_result,
                studio_reconciliation={
                    "state": "running",
                    "link_manifest_sha256": new_link_sha,
                },
                state="importing",
                stage="solution_reconciliation",
                progress=85,
                message="Applying the verified link result without replacing saved content.",
            )

            project = load_json_object(project_path)
            workflow = project.get("workflow")
            if (
                not isinstance(workflow, Mapping)
                or workflow.get("revision") != request.get("project_revision")
            ):
                raise RuntimeError("project changed while solution relinking was running")
            result = enrich_project_from_leaf(
                project_dir,
                run_dir,
                preserve_approved=True,
                expected_link_manifest_sha256=new_link_sha,
                expected_project_revision=request.get("project_revision"),
            )
            record_source_reconciliation(
                project_dir,
                run_dir,
                crop_job_id,
                expected_link_manifest_sha256=new_link_sha,
                expected_project_revision=request.get("project_revision"),
            )
            self._update(
                job_id,
                studio_reconciliation={
                    "state": "completed",
                    "link_manifest_sha256": new_link_sha,
                },
            )
            recovery_of = request.get("recovery_source_job_id")
            if isinstance(recovery_of, str):
                with self._lock:
                    seen: set[str] = set()
                    source_id: str | None = recovery_of
                    while source_id is not None:
                        if source_id in seen:
                            raise RuntimeError("solution relink recovery lineage is cyclic")
                        seen.add(source_id)
                        source_job = self._jobs.get(source_id)
                        if source_job is None:
                            raise RuntimeError("solution relink recovery source disappeared")
                        source_job["recovery_consumed_by"] = job_id
                        source_job["revision"] = int(source_job.get("revision") or 0) + 1
                        source_job["updated_at"] = _now()
                        self._persist(source_job)
                        parent_id = source_job.get("recovery_of")
                        source_id = parent_id if isinstance(parent_id, str) else None
            with self._lock:
                summary = copy.deepcopy(self._jobs[job_id]["summary"])
            summary["solution"] = {
                "state": "completed",
                "affected_items": int(request.get("item_count") or 0),
                "updated_item_count": len(result.updated_item_ids),
                "preserved_verified_count": len(result.preserved_verified_item_ids),
                "missing_detailed_solution_count": len(result.missing_item_ids),
                "added_candidate_count": result.added_candidate_count,
                "link_manifest_sha256": new_link_sha,
            }
            self._update(job_id, summary=summary)
            self._run_solution_pipeline_stage(job_id)
            self._finish(
                job_id,
                state="completed",
                stage="completed",
                progress=100,
                message=self.get(job_id)["message"],
            )
        except _BatchCancelled:
            return
        except Exception:
            try:
                with self._lock:
                    live = copy.deepcopy(self._jobs[job_id])
                    summary = copy.deepcopy(live["summary"])
                if isinstance(live.get("upstream_result"), Mapping):
                    self._update(
                        job_id,
                        studio_reconciliation={
                            "state": "recovery_required",
                            "link_manifest_sha256": live["upstream_result"].get(
                                "link_manifest_sha256"
                            ),
                        },
                    )
                summary["solution"] = {
                    "state": "failed",
                    "affected_items": int(
                        summary.get("crop", {}).get("total") or 0
                    ),
                    "error_code": "solution_relink_failed",
                }
                self._update(job_id, summary=summary)
                self._finish(
                    job_id,
                    state="failed",
                    stage="failed",
                    progress=100,
                    message="Answer and explanation relinking failed. Existing saved content was preserved.",
                    error_code="solution_relink_failed",
                )
            except Exception:
                return

    def _collect_solution_targets(
        self, project_dir: Path, item_ids: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:

        project = load_json_object(project_dir / "project.json")
        selected_ids = item_ids if item_ids is not None else project.get("item_order", [])
        targets: list[dict[str, Any]] = []
        for item_id in selected_ids:
            if not isinstance(item_id, str):
                continue
            question_path = project_dir / "items" / item_id / "question.json"
            if not question_path.is_file():
                continue
            document = load_json_object(question_path)
            link = self._registered_explanation_link(document)
            selection = document.get("provenance", {}).get("solution_selection")
            asset_id = str(link["source_asset_id"]) if link else ""
            source_sha = self._asset_sha256(document, asset_id) if asset_id else None
            selection_revision = selection.get("revision") if isinstance(selection, Mapping) else None
            key = f"{item_id}:{asset_id}:{source_sha or 'none'}:{selection_revision or 0}"
            targets.append({
                "key": key, "item_id": item_id, "asset_id": asset_id,
                "source_sha256": source_sha, "selection_revision": selection_revision,
                "input_revision": document.get("workflow", {}).get("revision"),
                "input_content_hash": solution_content_hash(document),
                "state": "pending" if source_sha else "no_source",
            })
        return targets

    def enqueue_solution_operation(self, project_id: str, item_id: str, operation_id: str) -> str:
        job_id = f"solution-operation-{operation_id}"
        profile = self.project_provider_profile(project_id) if (self.workspace_root / project_id / "project.json").is_file() else "full"
        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is not None:
                if existing.get("project_id") != project_id or existing.get("item_id") != item_id:
                    raise BatchError("Solution operation ID belongs to another item", status=409)
                return job_id
            timestamp = _now()
            record = {
                "schema_version": "0.1.0", "id": job_id, "kind": "solution_operation",
                "revision": 1, "event": "queued", "state": "queued", "stage": "solution_transcription",
                "progress": 0, "message": "Solution image transcription is queued.",
                "created_at": timestamp, "updated_at": timestamp, "finished_at": None,
                "request": {"ocr_profile": "balanced", "provider_profile": profile}, "project_id": project_id, "item_id": item_id,
                "operation_id": operation_id, "summary": {}, "error_code": None,
                "cancel_requested": False, "cancel_requested_at": None,
            }
            self._jobs[job_id] = record
            self._persist(record)
        self._executor.submit(self._run_solution_operation, job_id)
        return job_id

    def transcribe_project_solutions(self, project_id: str, item_ids: list[str] | None = None) -> dict[str, Any]:
        try:
            project_id = validate_id(project_id, field="project_id")
        except ModelError as exc:
            raise BatchError("Project was not found", status=404) from exc
        project_dir = (self.workspace_root / project_id).resolve()
        if not project_dir.is_relative_to(self.workspace_root) or not (project_dir / "project.json").is_file():
            raise BatchError("Project was not found", status=404)
        if item_ids is not None:
            order = load_json_object(project_dir / "project.json").get("item_order")
            if not isinstance(item_ids, list) or not item_ids or not isinstance(order, list):
                raise BatchError("Solution item selection is invalid", status=400)
            try:
                item_ids = [validate_id(item_id, field="item_id") for item_id in item_ids]
            except ModelError as exc:
                raise BatchError("Solution item selection is invalid", status=400) from exc
            if any(item_id not in order for item_id in item_ids) or len(set(item_ids)) != len(item_ids):
                raise BatchError("Solution item selection is invalid", status=400)
        with self._lock:
            if any(job.get("project_id") == project_id and job.get("state") in ACTIVE_STATES for job in self._jobs.values()):
                raise BatchError("A project batch is already active", status=409)
            timestamp = _now()
            job_id = f"solution-pipeline-{uuid.uuid4()}"
            record = {
                "schema_version": "0.1.0", "id": job_id, "kind": "solution_pipeline",
                "revision": 1, "event": "queued", "state": "queued", "stage": "solution_transcription",
                "progress": 0, "message": "Project solution transcription is queued.",
                "created_at": timestamp, "updated_at": timestamp, "finished_at": None,
                "request": {"ocr_profile": "balanced", "provider_profile": self.project_provider_profile(project_id),
                            "item_ids": item_ids},
                "project_id": project_id, "summary": {}, "error_code": None,
                "cancel_requested": False, "cancel_requested_at": None,
            }
            self._jobs[job_id] = record
            self._persist(record)
        self._executor.submit(self._run_solution_operation, job_id)
        return self._public(record)

    def _run_solution_operation(self, job_id: str) -> None:
        try:
            self._run_solution_pipeline_stage(job_id)
            self._finish(job_id, state="completed", stage="completed", progress=100,
                         message=self.get(job_id)["message"])
        except Exception as exc:
            self._finish(job_id, state="failed", stage="failed", progress=100,
                         message="Solution image transcription failed.", error_code=_ocr_error_code(exc))

    def _run_solution_pipeline_stage(self, job_id: str) -> None:

        with self._lock:
            record = copy.deepcopy(self._jobs[job_id])
        project_dir = self.workspace_root / str(record["project_id"])
        request = record.get("request") or {}
        provider_profile = parse_provider_profile(request.get("provider_profile"))
        config = effective_provider_config(provider_profile, str(request.get("ocr_profile") or "balanced"))
        previous = {
            target["key"]: target
            for target in record.get("solution_targets", [])
            if isinstance(target, Mapping) and isinstance(target.get("key"), str)
        }
        item_ids = [record["item_id"]] if record.get("kind") == "solution_operation" else request.get("item_ids")
        targets = self._collect_solution_targets(project_dir, item_ids=item_ids)
        for target in targets:
            prior = previous.get(target["key"])
            if prior:
                target.update({name: prior[name] for name in ("attempts", "ocr_calls", "ocr_candidates", "candidate_job_id", "verification_receipt", "input_content_hash") if name in prior})
                if prior.get("state") in {"failed", "cancelled", "generated_pending_apply"}:
                    target["state"] = prior["state"]
            question_path = project_dir / "items" / target["item_id"] / "question.json"
            if target["source_sha256"]:
                document = load_json_object(question_path)
                transcriptions = document.get("provenance", {}).get("transcriptions", [])
                selection = document.get("provenance", {}).get("solution_selection")
                current_transcription = any(
                    isinstance(entry, Mapping)
                    and entry.get("section") == "explanation"
                    and entry.get("status") == "succeeded"
                    and entry.get("shared_source_sha256") == target["source_sha256"]
                    and target["asset_id"] in entry.get("input_asset_ids", [])
                    and (entry.get("selection_revision") == target["selection_revision"]
                         or (entry.get("selection_revision") is None and target["selection_revision"] == 1
                             and isinstance(selection, Mapping) and selection.get("origin") == "auto_candidate"))
                    for entry in (transcriptions if isinstance(transcriptions, list) else [])
                )
                if current_transcription and self._has_explanation(document):
                    target["state"] = "applied" if prior and prior.get("state") == "applied" else "skipped_current"
        for target in targets:
            if target["state"] != "pending" or target["selection_revision"] is not None:
                continue
            question_path = project_dir / "items" / target["item_id"] / "question.json"
            document = load_json_object(question_path)
            document["provenance"]["solution_selection"] = {
                "source_asset_id": target["asset_id"], "source_sha256": target["source_sha256"],
                "revision": 1, "origin": "auto_candidate", "selected_at": _now(),
            }
            saved = save_json_cas(question_path, document, expected_revision=document["workflow"]["revision"])
            target["selection_revision"] = 1
            target["input_revision"] = saved["workflow"]["revision"]
            target["key"] = f'{target["item_id"]}:{target["asset_id"]}:{target["source_sha256"]}:1'
        with self._lock:
            live = self._jobs[job_id]
            live["solution_targets"] = targets
            live["state"] = "ocr"
            live["stage"] = "solution_transcription"
            live["progress"] = 86
            live["message"] = f"Solution transcription: {sum(target['state'] == 'no_source' for target in targets)}/{len(targets)} terminal."
            live["revision"] = int(live.get("revision") or 0) + 1
            live["updated_at"] = _now()
            self._persist(live)

        if not targets:
            self._update(job_id, summary={**record["summary"], "solution_transcription": {"total": 0}})
            return

        candidate_cache = self._shared_candidate_cache(project_dir, config, provider_profile=provider_profile)
        content_cache = self._shared_content_cache(project_dir, provider_profile=provider_profile)
        analysis_owners = self._shared_analysis_owners(project_dir, provider_profile)
        groups: dict[str, list[dict[str, Any]]] = {}
        for target in targets:
            if target["state"] == "pending":
                groups.setdefault(target["source_sha256"], []).append(target)
        terminal_states = {"applied", "skipped_current", "generated_pending_apply", "failed", "no_source", "cancelled"}

        def report_targets() -> None:
            done = sum(target["state"] in terminal_states for target in targets)
            self._update(job_id, solution_targets=targets, progress=86 + 8 * done // len(targets),
                         message=f"Solution transcription: {done}/{len(targets)} terminal.")

        provider_groups = []
        for source_sha, group in groups.items():
            if candidate_cache.get(source_sha) is not None:
                continue
            if source_sha in content_cache and not any(
                self._has_explanation(load_json_object(project_dir / "items" / target["item_id"] / "question.json"))
                for target in group
            ):
                continue
            provider_groups.append((source_sha, group[0]))

        def create_candidate(source_sha, leader):
            for attempt in range(2):
                try:
                    owner = analysis_owners.get(source_sha)
                    candidate = create_transcription_job(
                        owner[0] if owner else project_dir / "items" / leader["item_id"] / "question.json",
                        owner[1] if owner else leader["asset_id"],
                        "explanation", config=config, shared_parent_mode=True,
                        provider_profile=provider_profile,
                    )
                    return candidate, attempt + 1, None
                except (CodexRateLimitError, OSError) as exc:
                    if attempt == 0:
                        continue
                    return None, attempt + 1, exc
                except AstraTranscriptionError as exc:
                    if attempt == 0 and exc.code == "timeout":
                        continue
                    return None, attempt + 1, exc
                except Exception as exc:
                    return None, attempt + 1, exc
            raise AssertionError("unreachable provider retry state")

        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="solution-provider")
        provider_queue = iter(provider_groups)
        provider_futures = {}

        def prefetch() -> None:
            while len(provider_futures) < 2:
                try:
                    source_sha, leader = next(provider_queue)
                except StopIteration:
                    break
                provider_futures[source_sha] = pool.submit(create_candidate, source_sha, leader)

        try:
            self._checkpoint(job_id)
            prefetch()
            for source_sha, group in groups.items():
                self._checkpoint(job_id)
                pending = [target for target in group if target["state"] == "pending"]
                if not pending:
                    continue
                leader = pending[0]
                candidate = candidate_cache.get(source_sha)
                if source_sha in provider_futures:
                    candidate, calls, error = provider_futures.pop(source_sha).result()
                    for target in pending:
                        target["ocr_calls"] = int(target.get("ocr_calls") or 0) + calls
                    prefetch()
                    if error is not None:
                        for target in pending:
                            target.update(state="failed", error_code=_ocr_error_code(error), attempts=calls)
                        report_targets()
                        continue
                    candidate_cache[source_sha] = candidate
                for target in pending:
                    self._checkpoint(job_id)
                    question_path = project_dir / "items" / target["item_id"] / "question.json"
                    current = load_json_object(question_path)
                    if candidate is not None:
                        target["candidate_job_id"] = candidate.path.name
                    selection = current.get("provenance", {}).get("solution_selection")
                    if (self._asset_sha256(current, target["asset_id"]) != source_sha
                        or self._registered_explanation_asset(current) != target["asset_id"]
                        or (isinstance(selection, Mapping) and selection.get("revision") != target["selection_revision"])):
                        target["state"] = "cancelled"
                        report_targets()
                        continue
                    current_hash = solution_content_hash(current)
                    auto_hash = current.get("provenance", {}).get("last_auto_applied_hash")
                    manual_content = self._has_explanation(current) and (not isinstance(auto_hash, str) or auto_hash != current_hash)
                    if (current_hash != target.get("input_content_hash") or manual_content):
                        target["state"] = "generated_pending_apply"
                        report_targets()
                        continue
                    try:
                        revision = current["workflow"]["revision"]
                        if candidate is not None:
                            local = clone_candidate_job_for_item(question_path, candidate, target["asset_id"], expected_base_revision=revision)
                            target["candidate_job_id"] = local.path.name
                            self._update(job_id, solution_targets=targets)
                            apply_candidate_job(question_path, local.path, expected_base_revision=revision,
                                preserve_existing_answer=True, discard_candidate_answer=True)
                        else:
                            source_document, source_item_id = content_cache[source_sha]
                            clone_explanation_content_cas(source_document, question_path, target["asset_id"], source_sha,
                                source_item_id=source_item_id, expected_revision=revision)
                    except (RevisionConflict, CandidateValidationError):
                        target["state"] = "generated_pending_apply"
                    except Exception as exc:
                        target.update(state="failed", error_code=_ocr_error_code(exc))
                    else:
                        target["state"] = "applied"
                    target["attempts"] = int(target.get("attempts") or 0) + 1
                    report_targets()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        counts = {state: sum(target["state"] == state for target in targets)
            for state in ("applied", "skipped_current", "generated_pending_apply", "failed", "no_source", "cancelled")}
        with self._lock:
            summary = copy.deepcopy(self._jobs[job_id]["summary"])
        summary["solution_transcription"] = {"total": len(targets), **counts}
        self._update(job_id, summary=summary, solution_targets=targets, progress=95,
                     message=f"Solution transcription ended: {len(targets)} targets.")

        from .solution_verification import alternative_assets, can_try_alternative, mark_pair_mismatch, repair_selected_crop, verify_selected
        verified = reviewed = failed = 0
        verification_total = sum(target["state"] in {"applied", "skipped_current"} for target in targets)
        self._update(job_id, stage="solution_verification", message=f"Checking solution pairs: 0/{verification_total}.")
        for target in targets:
            if target["state"] not in {"applied", "skipped_current"}:
                continue
            self._checkpoint(job_id)
            question_path = project_dir / "items" / target["item_id"] / "question.json"
            try:
                current = load_json_object(question_path)
                selection = current.get("provenance", {}).get("solution_selection")
                if not isinstance(selection, dict):
                    current["provenance"]["solution_selection"] = {
                        "source_asset_id": target["asset_id"],
                        "source_sha256": target["source_sha256"],
                        "revision": 1, "origin": "auto_candidate", "selected_at": _now(),
                    }
                    save_json_cas(question_path, current, expected_revision=current["workflow"]["revision"])
                outcome = verify_selected(question_path)
                if (outcome["state"] == "review" and int(target.get("crop_repairs") or 0) < 1
                    and can_try_alternative(target)):
                    current = load_json_object(question_path)
                    last_auto = current.get("provenance", {}).get("last_auto_applied_hash")
                    if not self._has_explanation(current) or (isinstance(last_auto, str) and last_auto == solution_content_hash(current)):
                        repaired = repair_selected_crop(question_path, outcome, self.import_roots)
                        if repaired is not None:
                            current = load_json_object(question_path)
                            selection = current["provenance"]["solution_selection"]
                            target.update(asset_id=selection["source_asset_id"], source_sha256=selection["source_sha256"],
                                selection_revision=selection["revision"], crop_repairs=1,
                                ocr_candidates=int(target.get("ocr_candidates") or 1) + 1,
                                ocr_calls=int(target.get("ocr_calls") or 0) + 1,
                                key=f"{target['item_id']}:{selection['source_asset_id']}:{selection['source_sha256']}:{selection['revision']}")
                            self._update(job_id, solution_targets=targets)
                            try:
                                candidate = create_transcription_job(question_path, target["asset_id"], "explanation",
                                    config=config, shared_parent_mode=True, provider_profile=provider_profile)
                                local = clone_candidate_job_for_item(question_path, candidate, target["asset_id"],
                                    expected_base_revision=current["workflow"]["revision"])
                                target["candidate_job_id"] = local.path.name
                                self._update(job_id, solution_targets=targets)
                                apply_candidate_job(question_path, local.path,
                                    expected_base_revision=current["workflow"]["revision"],
                                    preserve_existing_answer=True, discard_candidate_answer=True)
                            except Exception as exc:
                                target.update(state="failed", error_code=_ocr_error_code(exc))
                                raise
                            outcome = verify_selected(question_path)
                if outcome["state"] == "mismatch":
                    mark_pair_mismatch(question_path)
                    current = load_json_object(question_path)
                    alternatives = alternative_assets(current, target["asset_id"])
                    for alternative_id in alternatives:
                        if not can_try_alternative(target):
                            break
                        alternative_sha = self._asset_sha256(current, alternative_id)
                        if not alternative_sha:
                            continue
                        prior = current["provenance"]["solution_selection"]
                        current["provenance"]["solution_selection"] = {
                            "source_asset_id": alternative_id,
                            "source_sha256": alternative_sha,
                            "revision": int(prior["revision"]) + 1,
                            "origin": "auto_candidate", "selected_at": _now(),
                        }
                        current = save_json_cas(question_path, current, expected_revision=current["workflow"]["revision"])
                        target["ocr_candidates"] = int(target.get("ocr_candidates") or 1) + 1
                        target["ocr_calls"] = int(target.get("ocr_calls") or 0) + 1
                        candidate = create_transcription_job(question_path, alternative_id, "explanation",
                            config=config, shared_parent_mode=True, provider_profile=provider_profile)
                        local = clone_candidate_job_for_item(question_path, candidate, alternative_id,
                            expected_base_revision=current["workflow"]["revision"])
                        apply_candidate_job(question_path, local.path,
                            expected_base_revision=current["workflow"]["revision"],
                            preserve_existing_answer=True, discard_candidate_answer=True)
                        outcome = verify_selected(question_path)
                        if outcome["state"] == "verified":
                            break
                        if outcome["state"] == "mismatch":
                            mark_pair_mismatch(question_path)
                        current = load_json_object(question_path)
                target["verification_receipt"] = outcome.get("receipt")
                target["verification_state"] = outcome["state"]
                if outcome["state"] == "verified":
                    verified += 1
                else:
                    reviewed += 1
            except Exception as exc:
                target["verification_state"] = "failed"
                target["verification_error"] = _ocr_error_code(exc)
                failed += 1
            checked = verified + reviewed + failed
            self._update(job_id, solution_targets=targets,
                         progress=95 + (4 * checked // verification_total if verification_total else 4),
                         message=f"Checking solution pairs: {checked}/{verification_total}.")
        counts = {state: sum(target["state"] == state for target in targets)
            for state in ("applied", "skipped_current", "generated_pending_apply", "failed", "no_source", "cancelled")}
        summary["solution_transcription"] = {"total": len(targets), **counts}
        summary["solution_verification"] = {"verified": verified, "review": reviewed, "failed": failed}
        self._update(job_id, summary=summary, solution_targets=targets, progress=99,
                     message=(f"Solution processing ended: {counts['applied']} typed, {counts['failed']} OCR failed, "
                              f"{counts['no_source']} no source; {verified} verified, {reviewed} review, {failed} check failed."))

    def _run(self, job_id: str) -> None:
        try:
            self._checkpoint(job_id)
            with self._lock:
                record = copy.deepcopy(self._jobs[job_id])
            request = copy.deepcopy(record["request"])
            resumed = isinstance(record.get("resume_of"), str)
            project_dir = self._stored_project_dir(record)
            stored_solution = record.get("summary", {}).get("solution", {})
            solution_warning = (
                isinstance(stored_solution, Mapping)
                and stored_solution.get("state") == "failed"
            )
            if project_dir is None:
                source_path = self._request_pdf(request, "source", required=True)
                solution_path = self._request_pdf(request, "solution", required=False)
                request["source_path"] = str(source_path)
                request["solution_path"] = str(solution_path) if solution_path else None
                completed: Mapping[str, Any] | None = None
                crop_job_id = record.get("crop_job_id") if resumed else None
                if isinstance(crop_job_id, str) and re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", crop_job_id
                ):
                    self._update(
                        job_id,
                        state="polling_crop",
                        stage="crop_resume",
                        message="Waiting for the existing crop job.",
                    )
                    try:
                        initial_crop_record: Mapping[str, Any] | None = None
                        if resumed and request.get("solution_path"):
                            initial_crop_record = self._request_json(
                                "GET", f"/api/jobs/{quote(crop_job_id, safe='')}"
                            )
                            current_solution = initial_crop_record.get("solution")
                            if (
                                initial_crop_record.get("state") == "completed"
                                and isinstance(current_solution, Mapping)
                                and current_solution.get("state")
                                in TERMINAL_CROP_FAILURES
                            ):
                                self._update(
                                    job_id,
                                    stage="solution_retry",
                                    message="Retrying solution linking with existing question crops.",
                                )
                                try:
                                    self._request_json(
                                        "POST",
                                        f"/api/jobs/{quote(crop_job_id, safe='')}/solutions",
                                        {
                                            "source": request["solution_path"],
                                            "crop_dpi": request.get("crop_dpi", 300),
                                            "force": False,
                                        },
                                    )
                                except RuntimeError:
                                    # The completed question result is still safe
                                    # to import as an explanation-review fallback.
                                    pass
                                else:
                                    initial_crop_record = None
                        completed = self._wait_for_crop(
                            job_id,
                            crop_job_id,
                            bool(request.get("solution_path")),
                            initial_record=initial_crop_record,
                        )
                    except _CropTerminalFailure:
                        if not resumed:
                            raise
                        self._update(
                            job_id,
                            state="polling_crop",
                            stage="crop_resume",
                            message="Resuming the existing crop output from its pending step.",
                        )
                        self._request_json(
                            "POST",
                            f"/api/jobs/{quote(crop_job_id, safe='')}/resume",
                            {},
                        )
                        completed = self._wait_for_crop(
                            job_id,
                            crop_job_id,
                            bool(request.get("solution_path")),
                        )
                if completed is None:
                    self._checkpoint(job_id)
                    # A resumed imported project/crop can survive source cleanup;
                    # only revalidate paths if they will cross the crop API boundary.
                    self._pdf(request.get("source_path"), "source_path", required=True)
                    self._pdf(
                        request.get("solution_path"), "solution_path", required=False
                    )
                    self._update(
                        job_id,
                        state="starting_crop",
                        stage="crop_start",
                        progress=2,
                        message="Starting the crop engine.",
                    )
                    self._ensure_crop_engine()
                    self._checkpoint(job_id)
                    crop = self._request_json(
                        "POST",
                        "/api/jobs",
                        {
                            "source": request["source_path"],
                            "solution_source": request.get("solution_path"),
                            "pages": request.get("pages", "all"),
                            "workers": request.get("workers", 2),
                            "crop_dpi": request.get("crop_dpi", 300),
                            "unit": "atomic",
                            "answer_space": "exclude",
                            "sidecars": "exclude",
                            "verify": "all",
                            "provider_profile": parse_provider_profile(
                                request.get("provider_profile")
                            ),
                        },
                    )
                    self._require_crop_provider_profile(
                        crop,
                        parse_provider_profile(request.get("provider_profile")),
                    )
                    crop_job_id = crop.get("id") if isinstance(crop, Mapping) else None
                    if not isinstance(crop_job_id, str) or not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", crop_job_id
                    ):
                        raise RuntimeError("invalid crop response")
                    self._update(
                        job_id,
                        state="polling_crop",
                        stage="crop",
                        progress=5,
                        message="Cropping and solution linking are running.",
                        crop_job_id=crop_job_id,
                    )
                    completed = self._wait_for_crop(
                        job_id, crop_job_id, bool(request.get("solution_path"))
                    )
                self._checkpoint(job_id)
                document = self._completed_document(completed)
                solution = completed.get("solution")
                solution_warning = bool(
                    request.get("solution_path")
                    and isinstance(solution, Mapping)
                    and solution.get("state") in TERMINAL_CROP_FAILURES
                )
                run_dir = self._allowed_run_dir(document)
                ocr_units = self._ocr_units(document)
                total = len(document["items"])
                crop_review = sum(
                    item.get("verdict") == "review"
                    for item in document["items"]
                    if isinstance(item, Mapping)
                )
                crop_failed = sum(
                    item.get("verdict") == "fail"
                    for item in document["items"]
                    if isinstance(item, Mapping)
                )
                crop_passed = total - crop_review - crop_failed
                with self._lock:
                    summary = copy.deepcopy(self._jobs[job_id]["summary"])
                summary["crop"].update(
                    total=total,
                    passed=crop_passed,
                    review=crop_review,
                    failed=crop_failed,
                    registered=total,
                    skipped=0,
                )
                if solution_warning:
                    summary["solution"] = {
                        "state": "failed",
                        "affected_items": total,
                        "error_code": "solution_link_failed",
                    }
                self._update(
                    job_id,
                    state="importing",
                    stage="import",
                    progress=72,
                    message="Importing registered crops.",
                    summary=summary,
                )
                self._checkpoint(job_id)
                imported = import_leaf_run(run_dir, self.workspace_root)
                self._update(job_id, project_id=imported.project_id)
                project_dir = imported.project_dir
            else:
                ocr_units = self._project_units(project_dir)
            self._bind_project_provider_profile(
                project_dir, parse_provider_profile(request.get("provider_profile"))
            )
            if solution_warning:
                affected_items = self._mark_solution_review_needed(project_dir)
                with self._lock:
                    summary = copy.deepcopy(self._jobs[job_id]["summary"])
                summary["solution"] = {
                    "state": "failed",
                    "affected_items": affected_items,
                    "error_code": "solution_link_failed",
                }
                self._update(job_id, summary=summary)
            self._checkpoint(job_id)
            self._run_ocr(
                job_id,
                project_dir,
                ocr_units,
                str(request.get("ocr_profile") or "balanced"),
                provider_profile=parse_provider_profile(
                    request.get("provider_profile")
                ),
                resume=resumed,
            )
            if (
                parse_provider_profile(request.get("provider_profile")) == "full"
                and request.get("regenerate_figures") is True
            ):
                prior_figure_spend = Decimal("0")
                if resumed:
                    prior_figure_spend = self._reset_figure_summary(job_id)
                self._run_figures(
                    job_id,
                    project_dir,
                    ocr_units,
                    str(request.get("figure_profile") or "balanced"),
                    Decimal(str(request.get("figure_budget_usd") or 0)),
                    request.get("remove_blue_text") is True,
                    prior_spend_usd=prior_figure_spend,
                )
            self._checkpoint(job_id)
            with self._lock:
                summary = self._jobs[job_id]["summary"]
                failures = int(summary["ocr"]["failed"]) + int(
                    summary["figures"]["failed"]
                )
                skipped_figures = int(summary["figures"]["skipped_budget"])
            self._finish(
                job_id,
                state="completed",
                stage="complete",
                progress=100,
                message=(
                    "문제는 저장했습니다. 해설 연결을 완료하지 못한 문항은 검토 필요로 표시했습니다."
                    if solution_warning
                    else "일괄 등록을 완료했습니다. OCR 또는 그림 경고 항목은 저장된 원본으로 사용할 수 있습니다."
                    if failures
                    else "일괄 등록을 완료했습니다. 비용 상한을 넘긴 그림은 원본으로 사용할 수 있습니다."
                    if skipped_figures
                    else "일괄 등록을 완료했습니다."
                ),
            )
        except _BatchCancelled:
            return
        except Exception as exc:
            try:
                self._checkpoint(job_id)
            except _BatchCancelled:
                return
            # External HTTP bodies, provider output, paths, and secrets never enter
            # the durable/public failure record.
            with self._lock:
                failed_stage = str(self._jobs[job_id].get("stage") or "batch")
                failure_detail = self._jobs[job_id].get("detection_failure")
                failed_progress = self._jobs[job_id].get("progress")
            self._finish(
                job_id,
                state="failed",
                stage="failed",
                progress=(int(failed_progress) if isinstance(failure_detail, Mapping) and type(failed_progress) is int else 100),
                message=(str(exc) if isinstance(exc, _CropTerminalFailure) and self._jobs[job_id].get("detection_failure") else "Batch processing failed."),
                error_code=(
                    "provider_rate_limited"
                    if isinstance(exc, CodexRateLimitError)
                    else "crop_failed" if isinstance(exc, _CropTerminalFailure)
                    else f"{failed_stage}_failed"
                ),
            )

    def _run_retry(
        self,
        job_id: str,
        targets: Sequence[tuple[Path, str, str, str, str]],
        profile: str,
        provider_profile: Any = None,
    ) -> None:
        try:
            self._checkpoint(job_id)
            self._update(
                job_id,
                state="ocr",
                stage="retry_ocr",
                progress=78,
                message=f"Retrying failed OCR sections (0/{len(targets)}).",
            )
            parsed_profile = parse_provider_profile(provider_profile)
            config = effective_provider_config(parsed_profile, profile)
            self._run_retry_targets(
                job_id, targets, config, provider_profile=parsed_profile
            )
            with self._lock:
                failures = int(self._jobs[job_id]["summary"]["ocr"]["failed"])
                succeeded = int(self._jobs[job_id]["summary"]["ocr"]["succeeded"])
                project_id = self._jobs[job_id].get("project_id")
            if succeeded and isinstance(project_id, str):
                self._mark_project_content_working(self.workspace_root / project_id)
            self._finish(
                job_id,
                state="completed",
                stage="complete",
                progress=100,
                message=(
                    "OCR 재시도를 완료했습니다. 남은 경고 항목은 저장된 원본으로 사용할 수 있습니다."
                    if failures
                    else "OCR 재시도를 완료했습니다."
                ),
            )
        except _BatchCancelled:
            return
        except Exception as exc:
            try:
                self._checkpoint(job_id)
            except _BatchCancelled:
                return
            self._finish(
                job_id,
                state="failed",
                stage="failed",
                progress=100,
                message="OCR retry failed.",
                error_code=_ocr_error_code(exc, "retry_ocr_failed"),
            )

    def _run_retry_targets(
        self,
        job_id: str,
        targets: Sequence[tuple[Path, str, str, str, str]],
        config: ProviderConfig,
        *,
        provider_profile: Any = None,
    ) -> None:
        """Deduplicate explanation OCR while keeping legacy targets compatible."""

        groups: dict[str, list[tuple[Path, str, str, str, str]]] = {}
        legacy: list[tuple[Path, str, str, str, str]] = []
        for target in targets:
            question_path, asset_id, section, _, _ = target
            if section != "explanation":
                legacy.append(target)
                continue
            source_sha256 = self._asset_sha256(load_json_object(question_path), asset_id)
            if source_sha256 is None:
                legacy.append(target)
            else:
                groups.setdefault(source_sha256, []).append(target)

        if not groups:
            for question_path, asset_id, section, unit_id, _ in legacy:
                self._checkpoint(job_id)
                self._try_transcription(
                    job_id,
                    question_path,
                    asset_id,
                    section,
                    unit_id,
                    config,
                    replacing_failure=True,
                    provider_profile=parse_provider_profile(provider_profile),
                )
                self._checkpoint(job_id)
            return

        project_dir = next(iter(groups.values()))[0][0].parents[2]
        analysis_owners = self._shared_analysis_owners(
            project_dir, parse_provider_profile(provider_profile)
        )
        candidate_cache = self._shared_candidate_cache(project_dir, config,
            provider_profile=parse_provider_profile(provider_profile))
        content_cache = self._shared_content_cache(
            project_dir, provider_profile=parse_provider_profile(provider_profile)
        )
        stale_source_shas = {
            source_sha256
            for source_sha256, group in groups.items()
            if any(target[4] == "stale_explanation_source" for target in group)
        }
        provider_groups = sum(
            source_sha256 not in candidate_cache
            and (
                source_sha256 in stale_source_shas
                or source_sha256 not in content_cache
            )
            for source_sha256 in groups
        )
        with self._lock:
            record = self._jobs[job_id]
            ocr = record["summary"]["ocr"]
            ocr.update(
                provider_total=provider_groups + len(legacy),
                provider_attempted=0,
                provider_failed=0,
                provider_cached=0,
                application_total=len(targets),
                application_attempted=0,
                content_reused=0,
                work_total=provider_groups + len(legacy) + len(targets),
                work_attempted=0,
            )
            record["revision"] = int(record.get("revision") or 0) + 1
            record["event"] = "ocr_deduplicated_plan"
            record["updated_at"] = _now()
            self._persist(record)

        for question_path, asset_id, section, unit_id, _ in legacy:
            self._checkpoint(job_id)
            self._try_transcription(
                job_id,
                question_path,
                asset_id,
                section,
                unit_id,
                config,
                replacing_failure=True,
                track_work=True,
                provider_profile=parse_provider_profile(provider_profile),
            )
            self._checkpoint(job_id)

        for source_sha256, group in groups.items():
            self._checkpoint(job_id)
            self._try_shared_explanation_group(
                job_id,
                source_sha256,
                group,
                config,
                candidate_cache.get(source_sha256),
                (
                    None
                    if source_sha256 in stale_source_shas
                    else content_cache.get(source_sha256)
                ),
                provider_profile=parse_provider_profile(provider_profile),
                analysis_owner=analysis_owners.get(source_sha256),
            )
            self._checkpoint(job_id)

    def _run_bank_figure_batch(self, job_id: str) -> None:
        try:
            self._checkpoint(job_id)
            with self._lock:
                request = copy.deepcopy(self._jobs[job_id]["request"])
            targets = request.get("targets")
            if not isinstance(targets, list):
                raise BatchError("Stored figure targets are invalid", status=409)
            groups: dict[str, list[dict[str, Any]]] = {}
            for target in targets:
                if isinstance(target, dict) and isinstance(
                    target.get("artifact_key"), str
                ):
                    groups.setdefault(str(target["artifact_key"]), []).append(target)
            item_revisions = {
                (str(item["project_id"]), str(item["item_id"])): int(
                    item["source_revision"]
                )
                for item in request.get("items", [])
                if isinstance(item, Mapping)
            }
            adapter = _BudgetedFigureAdapter(
                self.figure_adapter or GeminiFigureAdapter(),
                Decimal(str(FIGURE_PROFILES["quality"].estimated_output_usd)),
                Decimal(str(request.get("estimated_output_usd_max") or 0)),
            )
            work_total = len(targets) + int(
                self._jobs[job_id]["summary"]["figures"].get(
                    "provider_calls_max", 0
                )
            )
            work_done = 0
            self._update(
                job_id,
                state="figure_generation",
                stage="figure_generation",
                progress=1,
                message=f"Regenerating selected figures (0/{work_total}).",
            )

            def outcome(
                target: Mapping[str, Any], error_code: str | None, *, materialized: bool
            ) -> None:
                nonlocal work_done
                work_done += 1
                with self._lock:
                    record = self._jobs[job_id]
                    figures = record["summary"]["figures"]
                    if error_code is None:
                        figures["succeeded"] += 1
                        if materialized:
                            figures["materialized"] += 1
                    else:
                        figures["failed"] += 1
                        figures["failures"].append(
                            {
                                "project_id": target.get("project_id"),
                                "item_id": target.get("item_id"),
                                "block_id": target.get("block_id"),
                                "error_code": error_code,
                            }
                        )
                    figures["attempted"] = adapter.calls
                    figures["estimated_output_usd"] = float(
                        adapter.estimated_output_usd
                    )
                    record["progress"] = (
                        round(work_done / work_total * 99) if work_total else 99
                    )
                    record["message"] = (
                        f"Regenerating selected figures ({work_done}/{work_total})."
                    )
                    record["revision"] = int(record.get("revision") or 0) + 1
                    record["event"] = "figure_progress"
                    record["updated_at"] = _now()
                    self._persist(record)

            for _, group in groups.items():
                self._checkpoint(job_id)
                valid: list[tuple[dict[str, Any], Path, int]] = []
                for target in group:
                    try:
                        item_key = (
                            str(target["project_id"]),
                            str(target["item_id"]),
                        )
                        expected_revision = item_revisions[item_key]
                        resolved = resolve_bank_item_refs(
                            self.workspace_root,
                            [
                                {
                                    key: target[key]
                                    for key in ("project_id", "item_id")
                                }
                                | {"source_revision": expected_revision}
                            ],
                        )[0]
                        inputs = inspect_figure_generation_inputs(
                            resolved["item_path"],
                            expected_revision,
                            target["block_id"],
                            self.figure_cache_root,
                        )
                        if any(
                            inputs.get(field) != target.get(field)
                            for field in (
                                "source_asset_id",
                                "source_sha256",
                                "prompt_sha256",
                                "artifact_key",
                                "model",
                                "image_size",
                                "prompt_version",
                            )
                        ):
                            raise RevisionConflict(
                                int(target["source_revision"]),
                                int(target["source_revision"]),
                            )
                    except (RevisionConflict, QuestionBankError):
                        outcome(target, "revision_conflict", materialized=False)
                    except Exception:
                        outcome(target, "figure_generation_failed", materialized=False)
                    else:
                        valid.append(
                            (target, resolved["item_path"], expected_revision)
                        )
                if not valid:
                    if not all(target.get("cached") is True for target in group):
                        work_total = max(len(targets), work_total - 1)
                        with self._lock:
                            figures = self._jobs[job_id]["summary"]["figures"]
                            figures["provider_calls_max"] = max(
                                0, int(figures["provider_calls_max"]) - 1
                            )
                    continue
                calls_before = adapter.calls
                try:
                    representative, representative_path, representative_revision = valid[0]
                    artifact = get_or_create_figure_artifact(
                        self.figure_cache_root,
                        representative_path,
                        representative_revision,
                        str(representative["block_id"]),
                        adapter=adapter,
                    )
                except _FigureBudgetExceeded:
                    error_code = "figure_budget_exceeded"
                except FigureProviderNotConfigured:
                    error_code = "provider_not_configured"
                except FigureProviderRequestError:
                    error_code = "provider_request_failed"
                except Exception:
                    error_code = "figure_generation_failed"
                else:
                    error_code = None
                provider_delta = adapter.calls - calls_before
                if provider_delta:
                    work_done += provider_delta
                elif not all(target.get("cached") is True for target, _, _ in valid):
                    work_total = max(len(targets), work_total - 1)
                    with self._lock:
                        figures = self._jobs[job_id]["summary"]["figures"]
                        figures["provider_calls_max"] = max(
                            0, int(figures["provider_calls_max"]) - 1
                        )
                with self._lock:
                    record = self._jobs[job_id]
                    figures = record["summary"]["figures"]
                    figures["attempted"] = adapter.calls
                    figures["estimated_output_usd"] = float(
                        adapter.estimated_output_usd
                    )
                    record["progress"] = (
                        round(work_done / work_total * 99) if work_total else 99
                    )
                    record["message"] = (
                        f"Regenerating selected figures ({work_done}/{work_total})."
                    )
                    record["revision"] = int(record.get("revision") or 0) + 1
                    record["event"] = "figure_provider_progress"
                    record["updated_at"] = _now()
                    self._persist(record)
                if error_code is not None:
                    for target, _, _ in valid:
                        outcome(target, error_code, materialized=False)
                    continue
                if artifact.cached and not all(
                    target.get("cached") is True for target, _, _ in valid
                ):
                    with self._lock:
                        self._jobs[job_id]["summary"]["figures"]["cached"] += len(
                            valid
                        )
                self._checkpoint(job_id)
                for target, item_path, expected_revision in valid:
                    self._checkpoint(job_id)
                    item_key = (
                        str(target["project_id"]),
                        str(target["item_id"]),
                    )
                    expected_revision = item_revisions[item_key]
                    try:
                        saved, _, created = materialize_figure_artifact_cas(
                            item_path,
                            expected_revision,
                            str(target["block_id"]),
                            artifact,
                        )
                    except RevisionConflict:
                        outcome(target, "revision_conflict", materialized=False)
                    except Exception:
                        outcome(target, "figure_generation_failed", materialized=False)
                    else:
                        item_revisions[item_key] = int(saved["workflow"]["revision"])
                        outcome(target, None, materialized=created)
                    self._checkpoint(job_id)
            with self._lock:
                failures = int(
                    self._jobs[job_id]["summary"]["figures"].get("failed") or 0
                )
            self._finish(
                job_id,
                state="completed",
                stage="complete",
                progress=100,
                message=(
                    f"Selected figure generation completed with {failures} warning(s)."
                    if failures
                    else "Selected figure generation completed."
                ),
            )
        except _BatchCancelled:
            return
        except Exception:
            try:
                self._checkpoint(job_id)
            except _BatchCancelled:
                return
            self._finish(
                job_id,
                state="failed",
                stage="failed",
                progress=100,
                message="Selected figure generation failed.",
                error_code="figure_generation_failed",
            )

    def _run_figure_retry(
        self,
        job_id: str,
        targets: Sequence[tuple[Path, str, str, str, str]],
        profile: str,
        budget_usd: Decimal,
        remove_blue_text: bool,
        prior_spend_usd: Decimal,
    ) -> None:
        try:
            self._checkpoint(job_id)
            with self._lock:
                project_id = self._jobs[job_id].get("project_id")
            if not isinstance(project_id, str):
                raise BatchError("The figure retry project is unavailable", status=409)
            project_dir = self._stored_project_dir({"project_id": project_id})
            if project_dir is None:
                raise BatchError("The figure retry project is unavailable", status=409)
            self._run_figures(
                job_id,
                project_dir,
                self._project_units(project_dir),
                profile,
                budget_usd,
                remove_blue_text,
                prior_spend_usd=prior_spend_usd,
                targets_override=targets,
                replacing_failures=True,
            )
            self._checkpoint(job_id)
            with self._lock:
                failures = int(
                    self._jobs[job_id]["summary"]["figures"].get("failed") or 0
                )
            self._finish(
                job_id,
                state="completed",
                stage="complete",
                progress=100,
                message=(
                    f"그림 재시도를 완료했습니다. 남은 실패 {failures}개는 원본 그림을 유지합니다."
                    if failures
                    else "실패한 그림 재생성을 완료했습니다."
                ),
            )
        except _BatchCancelled:
            return
        except Exception:
            try:
                self._checkpoint(job_id)
            except _BatchCancelled:
                return
            self._finish(
                job_id,
                state="failed",
                stage="failed",
                progress=100,
                message="Figure retry failed.",
                error_code="figure_retry_failed",
            )

    def _run_ocr(
        self,
        job_id: str,
        project_dir: Path,
        ocr_units: set[str],
        profile: str,
        *,
        provider_profile: Any = None,
        resume: bool = False,
    ) -> None:
        project = load_json_object(project_dir / "project.json")
        item_order = project.get("item_order")
        if not isinstance(item_order, list):
            raise RuntimeError("invalid imported project")
        parsed_profile = parse_provider_profile(provider_profile)
        config = effective_provider_config(parsed_profile, profile)
        targets: list[tuple[Path, str, str, str]] = []
        for item_id in item_order:
            if not isinstance(item_id, str):
                continue
            question_path = project_dir / "items" / item_id / "question.json"
            document = load_json_object(question_path)
            problem = document.get("provenance", {}).get("problem", {})
            unit_id = problem.get("unit_id") if isinstance(problem, Mapping) else None
            if unit_id not in ocr_units:
                continue
            targets.append((question_path, "source-question", "question", str(unit_id)))
            explanation_asset = self._registered_explanation_asset(document)
            if explanation_asset:
                targets.append(
                    (question_path, explanation_asset, "explanation", str(unit_id))
                )
        pending = targets
        failure_keys: set[tuple[str, str]] = set()
        with self._lock:
            ocr = self._jobs[job_id]["summary"]["ocr"]
            if resume:
                existing_failures = {
                    (str(item.get("unit_id")), str(item.get("section"))): item
                    for item in ocr.get("failures", [])
                    if isinstance(item, Mapping)
                }
                pending = []
                completed: list[tuple[str, str]] = []
                for question_path, asset_id, section, unit_id in targets:
                    document = load_json_object(question_path)
                    content = document.get("content")
                    if isinstance(content, Mapping) and content.get(section) is not None:
                        completed.append((unit_id, section))
                    else:
                        pending.append((question_path, asset_id, section, unit_id))
                pending_keys = {(unit_id, section) for _, _, section, unit_id in pending}
                retained = [
                    copy.deepcopy(failure)
                    for key, failure in existing_failures.items()
                    if key in pending_keys
                ]
                failure_keys = {
                    (str(item["unit_id"]), str(item["section"])) for item in retained
                }
                ocr.update(
                    total=len(targets),
                    attempted=len(completed) + len(retained),
                    succeeded=len(completed),
                    failed=len(retained),
                    question_succeeded=sum(s == "question" for _, s in completed),
                    question_failed=sum(s == "question" for _, s in failure_keys),
                    explanation_succeeded=sum(
                        s == "explanation" for _, s in completed
                    ),
                    explanation_failed=sum(
                        s == "explanation" for _, s in failure_keys
                    ),
                    failures=retained,
                )
            else:
                ocr["total"] = len(targets)
            record = self._jobs[job_id]
            record["revision"] = int(record.get("revision") or 0) + 1
            record["event"] = "ocr_plan_updated"
            record["updated_at"] = _now()
            self._persist(self._jobs[job_id])
        self._checkpoint(job_id)
        self._update(
            job_id,
            state="ocr",
            stage="ocr",
            progress=78,
            message=f"Transcribing registered crops ({len(targets) - len(pending)}/{len(targets)}).",
        )
        for question_path, asset_id, section, unit_id in pending:
            self._checkpoint(job_id)
            self._try_transcription(
                job_id,
                question_path,
                asset_id,
                section,
                unit_id,
                config,
                replacing_failure=(unit_id, section) in failure_keys,
                provider_profile=parsed_profile,
            )
            self._checkpoint(job_id)
        if targets:
            self._mark_project_content_working(project_dir)

    def _stored_project_dir(self, record: Mapping[str, Any]) -> Path | None:
        project_id = record.get("project_id")
        if not isinstance(project_id, str):
            return None
        try:
            validate_id(project_id, field="project_id")
        except ModelError:
            return None
        project_dir = (self.workspace_root / project_id).resolve()
        if (
            project_dir.is_relative_to(self.workspace_root)
            and (project_dir / "project.json").is_file()
        ):
            return project_dir
        return None

    @staticmethod
    def _project_units(project_dir: Path) -> set[str]:
        project = load_json_object(project_dir / "project.json")
        item_order = project.get("item_order")
        if not isinstance(item_order, list):
            raise RuntimeError("invalid imported project")
        units: set[str] = set()
        for item_id in item_order:
            if not isinstance(item_id, str):
                continue
            document = load_json_object(
                project_dir / "items" / item_id / "question.json"
            )
            problem = document.get("provenance", {}).get("problem", {})
            unit_id = problem.get("unit_id") if isinstance(problem, Mapping) else None
            if isinstance(unit_id, str):
                units.add(unit_id)
        return units

    def _reset_figure_summary(self, job_id: str) -> Decimal:
        with self._lock:
            record = self._jobs[job_id]
            request = record.get("request")
            profile = (
                request.get("figure_profile")
                if isinstance(request, Mapping)
                else None
            )
            prior_spend = self._successful_figure_spend(record, str(profile or ""))
            record["figure_spend_before_usd"] = float(prior_spend)
            self._jobs[job_id]["summary"]["figures"] = {
                "eligible": 0,
                "attempted": 0,
                "succeeded": 0,
                "failed": 0,
                "skipped_budget": 0,
                "cached": 0,
                "estimated_output_usd": float(prior_spend),
                "failures": [],
            }
            self._jobs[job_id]["revision"] = (
                int(self._jobs[job_id].get("revision") or 0) + 1
            )
            self._jobs[job_id]["event"] = "figure_resume_reset"
            self._jobs[job_id]["updated_at"] = _now()
            self._persist(self._jobs[job_id])
            return prior_spend

    @staticmethod
    def _successful_figure_spend(
        record: Mapping[str, Any], profile: str
    ) -> Decimal:
        try:
            base = max(
                Decimal("0"), Decimal(str(record.get("figure_spend_before_usd") or 0))
            )
        except InvalidOperation:
            base = Decimal("0")
        selected_profile = FIGURE_PROFILES.get(profile)
        if selected_profile is None:
            return base
        summary = record.get("summary")
        figures = summary.get("figures") if isinstance(summary, Mapping) else None
        if not isinstance(figures, Mapping):
            return base
        try:
            succeeded = max(0, int(figures.get("succeeded") or 0))
            cached = max(0, int(figures.get("cached") or 0))
        except (TypeError, ValueError):
            return base
        noncached = max(0, succeeded - cached)
        return base + Decimal(str(selected_profile.estimated_output_usd)) * noncached

    @staticmethod
    def _mark_project_content_working(project_dir: Path) -> None:
        try:
            project_path = project_dir / "project.json"
            project = load_json_object(project_path)
            if project.get("workflow", {}).get("status") != "source_only":
                return
            project["workflow"]["status"] = "content_working"
            save_json_cas(
                project_path,
                project,
                project["workflow"]["revision"],
            )
        except (OSError, ModelError, RevisionConflict):
            # Item OCR is already durable; a derived project-status race is harmless.
            return

    @staticmethod
    def _mark_solution_review_needed(project_dir: Path) -> int:
        """Keep question data while recording that no explanation was invented."""

        project = load_json_object(project_dir / "project.json")
        item_order = project.get("item_order")
        if not isinstance(item_order, list):
            raise ModelError("project.item_order must be an array")
        affected = 0
        for raw_item_id in item_order:
            item_id = validate_id(raw_item_id, field="item_id")
            question_path = project_dir / "items" / item_id / "question.json"
            document = load_json_object(question_path)
            provenance = document.get("provenance")
            links = (
                provenance.get("solution_links")
                if isinstance(provenance, Mapping)
                else None
            )
            if any(
                isinstance(link, Mapping)
                and link.get("kind") in {"detailed_solution", "mixed"}
                for link in (links if isinstance(links, list) else [])
            ):
                continue
            affected += 1
            candidate = copy.deepcopy(document)
            raw_snapshot = candidate["provenance"]["raw_snapshot"]
            link_item = raw_snapshot.get("link_item")
            if not isinstance(link_item, dict):
                link_item = {}
                raw_snapshot["link_item"] = link_item
            reasons = link_item.get("review_reasons")
            if not isinstance(reasons, list):
                reasons = []
            link_item["review_reasons"] = list(
                dict.fromkeys([*reasons, "solution_pipeline_failed"])
            )
            link_item.setdefault("first_blocker", "solution_pipeline")
            review = candidate["review"]
            review["explanation"] = "unreviewed"
            review["coverage"]["explanation"] = "incomplete"
            issues = review.get("issues")
            if not isinstance(issues, list):
                issues = []
            review["issues"] = list(
                dict.fromkeys([*issues, "solution_link:solution_pipeline_failed"])
            )
            if candidate != document:
                save_json_cas(
                    question_path,
                    candidate,
                    document["workflow"]["revision"],
                    preserve_approval=True,
                )
        return affected

    def _run_figures(
        self,
        job_id: str,
        project_dir: Path,
        ocr_units: set[str],
        profile: str,
        budget_usd: Decimal,
        remove_blue_text: bool,
        *,
        prior_spend_usd: Decimal = Decimal("0"),
        targets_override: Sequence[tuple[Path, str, str, str, str]] | None = None,
        replacing_failures: bool = False,
    ) -> None:
        self._checkpoint(job_id)
        with self._lock:
            failures = self._jobs[job_id]["summary"]["ocr"].get("failures", [])
            failed_sections = {
                (failure.get("unit_id"), failure.get("section"))
                for failure in failures
                if isinstance(failure, Mapping)
            }
        targets = (
            list(targets_override)
            if targets_override is not None
            else self._figure_targets(project_dir, ocr_units, failed_sections)
        )
        remaining_budget = max(Decimal("0"), budget_usd - prior_spend_usd)
        adapter = _BudgetedFigureAdapter(
            self.figure_adapter or GeminiFigureAdapter(),
            Decimal(str(FIGURE_PROFILES[profile].estimated_output_usd)),
            remaining_budget,
        )
        with self._lock:
            record = self._jobs[job_id]
            figures = record["summary"]["figures"]
            figures["eligible"] = len(targets)
            record["revision"] = int(record.get("revision") or 0) + 1
            record["event"] = "figure_plan_updated"
            record["updated_at"] = _now()
            self._persist(record)
        self._update(
            job_id,
            state="figure_generation",
            stage="figure_generation",
            progress=90,
            message=(
                "Regenerating figures (0/"
                f"{len(targets)}); remaining image-output limit ${remaining_budget:.2f}."
            ),
        )
        for index, (question_path, item_id, unit_id, section, block_id) in enumerate(
            targets, start=1
        ):
            self._checkpoint(job_id)
            failure_code: str | None = None
            try:
                document = load_json_object(question_path)
                workflow = document.get("workflow")
                revision = workflow.get("revision") if isinstance(workflow, Mapping) else None
                if isinstance(revision, bool) or not isinstance(revision, int):
                    raise ModelError("invalid figure item revision")
                _, _, figure_job, _ = regenerate_figure_cas(
                    question_path,
                    revision,
                    block_id,
                    profile,
                    remove_blue_text,
                    adapter=adapter,
                )
            except _FigureBudgetExceeded:
                outcome = "skipped_budget"
                if replacing_failures:
                    failure_code = "figure_budget_exceeded"
            except FigureProviderNotConfigured:
                outcome = "failed"
                failure_code = "provider_not_configured"
            except FigureProviderRequestError:
                outcome = "failed"
                failure_code = "provider_request_failed"
            except RevisionConflict:
                outcome = "failed"
                failure_code = "revision_conflict"
            except Exception:
                outcome = "failed"
                failure_code = "figure_generation_failed"
            else:
                outcome = "succeeded"

            with self._lock:
                record = self._jobs[job_id]
                figures = record["summary"]["figures"]
                failure = {
                    "item_id": item_id,
                    "unit_id": unit_id,
                    "section": section,
                    "block_id": block_id,
                    "error_code": failure_code or "figure_generation_failed",
                }
                if replacing_failures:
                    matched = next(
                        (
                            failure_index
                            for failure_index, previous in enumerate(
                                figures.get("failures", [])
                            )
                            if previous.get("item_id") == item_id
                            and previous.get("block_id") == block_id
                        ),
                        None,
                    )
                    if outcome == "succeeded":
                        figures["succeeded"] += 1
                        if matched is not None:
                            figures["failures"].pop(matched)
                            figures["failed"] = max(0, int(figures["failed"]) - 1)
                    else:
                        if outcome == "skipped_budget":
                            figures["skipped_budget"] += 1
                        if matched is None:
                            figures["failures"].append(failure)
                            figures["failed"] += 1
                        else:
                            figures["failures"][matched] = failure
                else:
                    figures[outcome] += 1
                figures["attempted"] = adapter.calls
                figures["estimated_output_usd"] = float(
                    prior_spend_usd + adapter.estimated_output_usd
                )
                if outcome == "succeeded" and figure_job.cached:
                    figures["cached"] += 1
                if failure_code and not replacing_failures:
                    figures["failures"].append(failure)
                record["progress"] = 90 + round(index / len(targets) * 9)
                record["message"] = (
                    f"Regenerating figures ({index}/{len(targets)}); "
                    "estimated image output "
                    f"${prior_spend_usd + adapter.estimated_output_usd:.3f} "
                    f"of ${budget_usd:.2f}."
                )
                record["revision"] = int(record.get("revision") or 0) + 1
                record["event"] = "figure_progress"
                record["updated_at"] = _now()
                self._persist(record)
            self._checkpoint(job_id)

    @staticmethod
    def _figure_targets(
        project_dir: Path,
        pass_units: set[str],
        failed_sections: set[tuple[Any, Any]],
    ) -> list[tuple[Path, str, str, str, str]]:
        project = load_json_object(project_dir / "project.json")
        item_order = project.get("item_order")
        if not isinstance(item_order, list):
            raise RuntimeError("invalid imported project")
        targets: list[tuple[Path, str, str, str, str]] = []

        def figures(value: Any):
            if isinstance(value, Mapping):
                if value.get("type") == "figure":
                    yield value
                for child in value.values():
                    yield from figures(child)
            elif isinstance(value, list):
                for child in value:
                    yield from figures(child)

        for item_id in item_order:
            if not isinstance(item_id, str):
                continue
            validate_id(item_id, field="item_id")
            question_path = (project_dir / "items" / item_id / "question.json").resolve()
            if not question_path.is_relative_to(project_dir.resolve()):
                raise RuntimeError("invalid imported item path")
            document = load_json_object(question_path)
            problem = document.get("provenance", {}).get("problem", {})
            unit_id = problem.get("unit_id") if isinstance(problem, Mapping) else None
            if not isinstance(unit_id, str) or unit_id not in pass_units:
                continue
            resources = document.get("resources")
            assets = resources.get("assets") if isinstance(resources, Mapping) else None
            if not isinstance(assets, list):
                continue
            originals: dict[str, Mapping[str, Any]] = {}
            for asset in assets:
                if not isinstance(asset, Mapping) or asset.get("role") != "figure_original":
                    continue
                asset_id = asset.get("asset_id")
                try:
                    validate_id(asset_id, field="asset_id")
                except ModelError:
                    continue
                originals[str(asset_id)] = asset
            content = document.get("content")
            if not isinstance(content, Mapping):
                continue
            seen: set[str] = set()
            for section in ("question", "explanation"):
                if (unit_id, section) in failed_sections:
                    continue
                for block in figures(content.get(section)):
                    block_id = block.get("block_id")
                    original_id = block.get("original_asset_id")
                    try:
                        validate_id(block_id, field="block_id")
                        validate_id(original_id, field="original_asset_id")
                    except ModelError:
                        continue
                    original = originals.get(str(original_id))
                    if (
                        original is None
                        or original.get("state") != "ready"
                        or original.get("section") not in {section, "shared"}
                        or str(block_id) in seen
                    ):
                        continue
                    seen.add(str(block_id))
                    targets.append(
                        (question_path, item_id, unit_id, section, str(block_id))
                    )
        return targets

    @staticmethod
    def _record_ocr_progress(record: dict[str, Any]) -> None:
        ocr = record["summary"]["ocr"]
        has_work_plan = isinstance(ocr.get("work_total"), int)
        attempted = int(
            ocr.get("work_attempted") if has_work_plan else ocr.get("attempted") or 0
        )
        total = int(ocr.get("work_total") if has_work_plan else ocr.get("total") or 0)
        request = record.get("request")
        figure_stage = isinstance(request, Mapping) and request.get(
            "regenerate_figures"
        ) is True
        span = 12 if figure_stage else 20
        record["progress"] = 78 + (
            round(min(attempted, total) / total * span) if total else 0
        )
        action = (
            "Retrying failed OCR sections"
            if record.get("retry_of")
            else "Transcribing registered crops"
        )
        if has_work_plan:
            record["message"] = (
                f"{action} ({attempted}/{total} work; "
                f"{int(ocr.get('succeeded') or 0)}/{int(ocr.get('total') or 0)} items)."
            )
        else:
            record["message"] = f"{action} ({attempted}/{total})."

    @staticmethod
    def _is_protected_solution_link(link: Mapping[str, Any]) -> bool:
        return link.get("status") == "verified" or "manual_user_confirmation" in (
            link.get("evidence") or []
        )

    @staticmethod
    def _is_boundary_seam_repaired(link: Mapping[str, Any]) -> bool:
        return any(
            "boundary_seam_repaired" in (link.get(field) or [])
            for field in ("evidence", "review_reasons")
        )

    @classmethod
    def _registered_explanation_link(
        cls, document: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        assets = document.get("resources", {}).get("assets", [])
        asset_ids = {
            asset.get("asset_id")
            for asset in assets
            if isinstance(asset, Mapping)
            and asset.get("section") in {"answer", "explanation"}
            and asset.get("role") == "source_crop"
            and asset.get("state", "ready") == "ready"
        }
        links = document.get("provenance", {}).get("solution_links", [])
        eligible = [
            link
            for link in (links if isinstance(links, list) else [])
            if isinstance(link, Mapping)
            and link.get("status") in {"verified", "review"}
            and link.get("source_asset_id") in asset_ids
            and not any("rejected" in str(reason) or "excluded" in str(reason) for reason in (link.get("review_reasons") or []))
        ]
        selection = document.get("provenance", {}).get("solution_selection")
        if isinstance(selection, Mapping):
            selected_id = selection.get("source_asset_id")
            selected_sha = selection.get("source_sha256")
            if selected_id in asset_ids and selected_sha == cls._asset_sha256(document, selected_id):
                chosen = next((link for link in eligible if link.get("source_asset_id") == selected_id), None)
                return chosen
        detailed = [
            link
            for link in eligible
            if link.get("kind") in {"detailed_solution", "mixed"}
        ]
        candidates = detailed or [
            link for link in eligible if link.get("kind") == "quick_answer"
        ]
        protected = [
            link for link in candidates if cls._is_protected_solution_link(link)
        ]
        selected_pool = protected or candidates
        selected_pool.sort(
            key=lambda link: (
                0 if link.get("status") == "verified" else 1,
                (
                    0
                    if not protected and cls._is_boundary_seam_repaired(link)
                    else 1
                ),
                -float(link.get("confidence") or 0),
                str(link.get("source_asset_id")),
            )
        )
        return selected_pool[0] if selected_pool else None

    @classmethod
    def _registered_explanation_asset(cls, document: Mapping[str, Any]) -> str | None:
        selected = cls._registered_explanation_link(document)
        return str(selected["source_asset_id"]) if selected is not None else None

    @classmethod
    def _has_stale_explanation_source(
        cls,
        document: Mapping[str, Any],
        selected_link: Mapping[str, Any] | None = None,
    ) -> bool:
        selected = selected_link or cls._registered_explanation_link(document)
        if selected is None or selected.get("kind") not in {
            "detailed_solution",
            "mixed",
        }:
            return False
        explanation = document.get("content", {}).get("explanation")
        blocks = explanation.get("blocks") if isinstance(explanation, Mapping) else None
        if not isinstance(blocks, list) or not blocks:
            return False
        transcriptions = document.get("provenance", {}).get("transcriptions", [])
        successful = [
            (index, record)
            for index, record in enumerate(
                transcriptions if isinstance(transcriptions, list) else []
            )
            if isinstance(record, Mapping)
            and record.get("section") == "explanation"
            and record.get("status") == "succeeded"
        ]
        if not successful:
            return False
        _, latest = max(
            successful,
            key=lambda pair: (
                pair[1].get("attempt")
                if isinstance(pair[1].get("attempt"), int)
                else -1,
                pair[0],
            ),
        )
        input_asset_ids = latest.get("input_asset_ids")
        asset_id = selected.get("source_asset_id")
        source_sha = cls._asset_sha256(document, asset_id)
        selection = document.get("provenance", {}).get("solution_selection")
        return not (
            isinstance(input_asset_ids, list)
            and asset_id in input_asset_ids
            and source_sha is not None
            and latest.get("shared_source_sha256") == source_sha
            and (
                not isinstance(selection, Mapping)
                or latest.get("selection_revision") == selection.get("revision")
            )
        )

    _verified_explanation_asset = _registered_explanation_asset

    @staticmethod
    def _asset_sha256(document: Mapping[str, Any], asset_id: str) -> str | None:
        assets = document.get("resources", {}).get("assets", [])
        for asset in assets if isinstance(assets, list) else []:
            if not isinstance(asset, Mapping) or asset.get("asset_id") != asset_id:
                continue
            value = asset.get("sha256")
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                return value
            return None
        return None

    @classmethod
    def _shared_analysis_owners(
        cls, project_dir: Path, provider_profile: str = "full"
    ) -> dict[str, tuple[Path, str, Path]]:
        latest: dict[str, tuple[int, Path, str, Path]] = {}
        for item_dir in (project_dir / "items").iterdir():
            document = load_json_object(item_dir / "question.json")
            asset_id = cls._registered_explanation_asset(document)
            source_sha = cls._asset_sha256(document, asset_id) if asset_id else None
            if source_sha is None:
                continue
            expected_key = shared_analysis_key(source_sha, provider_profile=parse_provider_profile(provider_profile))
            try:
                families = list((item_dir / "jobs" / ".analysis").iterdir())
            except FileNotFoundError:
                continue
            for family in families:
                if family.name != expected_key[:24]:
                    continue
                for attempt in analysis_attempts(family):
                    request = load_analysis_request(attempt, expected_key, section="explanation",
                        shared_parent_mode=True, provider_profile=parse_provider_profile(provider_profile), source_asset_id=asset_id)
                    if request.source_asset_id != asset_id:
                        raise CandidateIntegrityError("shared analysis source asset differs")
                    order = int(attempt.name, 16)
                    if source_sha not in latest or order > latest[source_sha][0]:
                        latest[source_sha] = (order, item_dir / "question.json", asset_id, attempt)
        return {source_sha: (path, asset_id, attempt) for source_sha, (_, path, asset_id, attempt) in latest.items()}

    @classmethod
    def _shared_candidate_cache(
        cls, project_dir: Path, config: ProviderConfig, *, provider_profile: str = "full"
    ) -> dict[str, CandidateJob]:
        cached: dict[str, CandidateJob] = {}
        analysis_owners = cls._shared_analysis_owners(project_dir, provider_profile)
        for item_dir in (project_dir / "items").iterdir():
            try:
                job_paths = list((item_dir / "jobs" / "transcription").iterdir())
            except FileNotFoundError:
                continue
            for job_path in job_paths:
                if job_path.name.startswith("."):
                    continue
                metadata = load_json_object(job_path / "job.json")
                source_sha256 = metadata.get("input_sha256")
                if (
                    metadata.get("section") != "explanation"
                    or metadata.get("shared_parent_mode") is not True
                    or metadata.get("provider") != config.provider
                    or metadata.get("model") != config.model
                    or parse_provider_profile(metadata.get("provider_profile")) != provider_profile
                    or not isinstance(source_sha256, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)
                ):
                    continue
                job = load_candidate_job(job_path)
                if source_sha256 in analysis_owners:
                    latest_attempt = analysis_owners[source_sha256][2]
                    expected_key = shared_analysis_key(source_sha256, provider_profile=parse_provider_profile(provider_profile))
                    request = load_analysis_request(latest_attempt, expected_key, section="explanation",
                        shared_parent_mode=True, provider_profile=parse_provider_profile(provider_profile), source_asset_id=analysis_owners[source_sha256][1])
                    if metadata.get("idempotency_key") != request.idempotency_key:
                        continue
                    load_analysis_response(latest_attempt, expected_key, section="explanation",
                        shared_parent_mode=True, provider_profile=parse_provider_profile(provider_profile), source_asset_id=analysis_owners[source_sha256][1], published_job=job)
                elif metadata.get("analysis_response_path"):
                    raise CandidateIntegrityError("published shared candidate checkpoint is missing")
                if metadata.get("analysis_key") != shared_analysis_key(source_sha256,
                    provider_profile=parse_provider_profile(metadata.get("provider_profile"))):
                    continue
                cached.setdefault(source_sha256, job)
        return cached

    @classmethod
    def _shared_content_cache(
        cls, project_dir: Path, *, provider_profile: str = "full"
    ) -> dict[str, tuple[dict[str, Any], str]]:
        cached: dict[str, tuple[dict[str, Any], str]] = {}
        analysis_owners = cls._shared_analysis_owners(project_dir, provider_profile)
        for item_dir in (project_dir / "items").iterdir():
            document = load_json_object(item_dir / "question.json")
            asset_id = cls._registered_explanation_asset(document)
            source_sha256 = cls._asset_sha256(document, asset_id) if asset_id else None
            explanation = document.get("content", {}).get("explanation")
            blocks = explanation.get("blocks") if isinstance(explanation, Mapping) else None
            item_id = document.get("item_id")
            if (
                source_sha256 is None or not isinstance(blocks, list) or not blocks
                or source_sha256 in analysis_owners
                or not isinstance(item_id, str) or cls._has_invalid_bogi_labels(explanation)
                or not cls._has_shared_parent_provenance(document, asset_id, source_sha256, provider_profile=provider_profile)
            ):
                continue
            cached.setdefault(source_sha256, (document, item_id))
        return cached

    @classmethod
    def _has_invalid_bogi_labels(cls, value: Any) -> bool:
        if isinstance(value, Mapping):
            if value.get("type") == "bogi":
                items = value.get("items")
                if isinstance(items, list) and len(items) > 1:
                    labels = [
                        str(item.get("label") or "")
                        for item in items
                        if isinstance(item, Mapping)
                    ]
                    if len(labels) != len(items):
                        return True
                    if any(not label.strip() for label in labels):
                        return True
                    if len(set(labels)) != len(labels):
                        return True
            return any(cls._has_invalid_bogi_labels(child) for child in value.values())
        if isinstance(value, list):
            return any(cls._has_invalid_bogi_labels(child) for child in value)
        return False

    @staticmethod
    def _has_shared_parent_provenance(
        document: Mapping[str, Any], asset_id: str, source_sha256: str, *, provider_profile: str = "full"
    ) -> bool:
        transcriptions = document.get("provenance", {}).get("transcriptions", [])
        for record in transcriptions if isinstance(transcriptions, list) else []:
            if (
                not isinstance(record, Mapping)
                or record.get("section") != "explanation"
                or record.get("shared_source_sha256") != source_sha256
            ):
                continue
            input_asset_ids = record.get("input_asset_ids")
            if not isinstance(input_asset_ids, list) or asset_id not in input_asset_ids:
                continue
            if (
                record.get("prompt_version") == SHARED_PARENT_PROMPT_VERSION
                or record.get("provider") == "shared-content-cache"
            ):
                return record.get("analysis_key") == shared_analysis_key(source_sha256, provider_profile=parse_provider_profile(provider_profile))
        return False

    @staticmethod
    def _has_explanation(document: Mapping[str, Any]) -> bool:
        explanation = document.get("content", {}).get("explanation")
        blocks = explanation.get("blocks") if isinstance(explanation, Mapping) else None
        return isinstance(blocks, list) and bool(blocks)

    def _record_provider_result(
        self,
        job_id: str,
        *,
        failed: bool = False,
        cached: bool = False,
        remove_planned_call: bool = False,
    ) -> None:
        with self._lock:
            record = self._jobs[job_id]
            ocr = record["summary"]["ocr"]
            if cached:
                ocr["provider_cached"] = int(ocr.get("provider_cached") or 0) + 1
                if remove_planned_call:
                    ocr["provider_total"] = max(
                        0, int(ocr.get("provider_total") or 0) - 1
                    )
                    ocr["work_total"] = max(
                        0, int(ocr.get("work_total") or 0) - 1
                    )
            else:
                ocr["provider_attempted"] = int(ocr.get("provider_attempted") or 0) + 1
                ocr["work_attempted"] = int(ocr.get("work_attempted") or 0) + 1
                if failed:
                    ocr["provider_failed"] = int(ocr.get("provider_failed") or 0) + 1
            self._record_ocr_progress(record)
            record["revision"] = int(record.get("revision") or 0) + 1
            record["event"] = "ocr_provider_cached" if cached else "ocr_provider_finished"
            record["updated_at"] = _now()
            self._persist(record)

    def _record_shared_outcome(
        self,
        job_id: str,
        target: tuple[Path, str, str, str, str],
        *,
        succeeded: bool,
        error_code: str = "ocr_failed",
        application_attempted: bool,
        content_reused: bool = False,
    ) -> None:
        question_path, _, section, unit_id, _ = target
        item_id = question_path.parent.name
        with self._lock:
            record = self._jobs[job_id]
            ocr = record["summary"]["ocr"]
            ocr["attempted"] = int(ocr.get("attempted") or 0) + 1
            if application_attempted:
                ocr["application_attempted"] = (
                    int(ocr.get("application_attempted") or 0) + 1
                )
                ocr["work_attempted"] = int(ocr.get("work_attempted") or 0) + 1
            if content_reused:
                ocr["content_reused"] = int(ocr.get("content_reused") or 0) + 1
            failure = {
                "item_id": item_id,
                "unit_id": unit_id,
                "section": section,
            "error_code": error_code,
            }
            matched = next(
                (
                    index
                    for index, previous in enumerate(ocr.get("failures", []))
                    if (
                        previous.get("item_id") == item_id
                        or (
                            previous.get("item_id") is None
                            and previous.get("unit_id") == unit_id
                            and previous.get("section") == section
                        )
                    )
                ),
                None,
            )
            if succeeded:
                if matched is not None:
                    ocr["failures"].pop(matched)
                    ocr["failed"] = max(0, int(ocr.get("failed") or 0) - 1)
                    ocr[f"{section}_failed"] = max(
                        0, int(ocr.get(f"{section}_failed") or 0) - 1
                    )
                ocr["succeeded"] = int(ocr.get("succeeded") or 0) + 1
                ocr[f"{section}_succeeded"] = (
                    int(ocr.get(f"{section}_succeeded") or 0) + 1
                )
            elif matched is not None:
                ocr["failures"][matched] = failure
            else:
                ocr["failures"].append(failure)
                ocr["failed"] = int(ocr.get("failed") or 0) + 1
                ocr[f"{section}_failed"] = int(ocr.get(f"{section}_failed") or 0) + 1
            self._record_ocr_progress(record)
            record["revision"] = int(record.get("revision") or 0) + 1
            record["event"] = (
                "ocr_attempt_succeeded" if succeeded else "ocr_attempt_failed"
            )
            record["updated_at"] = _now()
            self._persist(record)

    def _record_application_started(self, job_id: str) -> None:
        with self._lock:
            record = self._jobs[job_id]
            ocr = record["summary"]["ocr"]
            ocr["application_attempted"] = (
                int(ocr.get("application_attempted") or 0) + 1
            )
            ocr["work_attempted"] = int(ocr.get("work_attempted") or 0) + 1
            self._record_ocr_progress(record)
            record["revision"] = int(record.get("revision") or 0) + 1
            record["event"] = "ocr_application_started"
            record["updated_at"] = _now()
            self._persist(record)

    def _apply_shared_candidate(
        self,
        source_job: CandidateJob,
        target: tuple[Path, str, str, str, str],
    ) -> None:
        question_path, asset_id, _, _, _ = target
        current = load_json_object(question_path)
        revision = current.get("workflow", {}).get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ModelError("invalid shared explanation target revision")
        local_job = clone_candidate_job_for_item(
            question_path,
            source_job,
            asset_id,
            expected_base_revision=revision,
        )
        apply_candidate_job(
            question_path,
            local_job.path,
            expected_base_revision=revision,
            preserve_existing_answer=True,
            discard_candidate_answer=True,
        )

    def _try_shared_explanation_group(
        self,
        job_id: str,
        source_sha256: str,
        group: Sequence[tuple[Path, str, str, str, str]],
        config: ProviderConfig,
        cached_job: CandidateJob | None,
        cached_content: tuple[dict[str, Any], str] | None,
        *,
        provider_profile: Any = None,
        analysis_owner: tuple[Path, str, Path] | None = None,
    ) -> None:
        completed: list[tuple[Path, str, str, str, str]] = []
        pending: list[tuple[Path, str, str, str, str]] = []
        for target in group:
            needs_repair = target[4] in {
                "invalid_bogi_labels",
                "stale_explanation_source",
            }
            has_explanation = self._has_explanation(load_json_object(target[0]))
            (completed if has_explanation and not needs_repair else pending).append(target)
        if completed:
            with self._lock:
                record = self._jobs[job_id]
                ocr = record["summary"]["ocr"]
                ocr["application_total"] = max(
                    0, int(ocr.get("application_total") or 0) - len(completed)
                )
                ocr["work_total"] = max(
                    0, int(ocr.get("work_total") or 0) - len(completed)
                )
                if not pending and cached_job is None and cached_content is None:
                    ocr["provider_total"] = max(
                        0, int(ocr.get("provider_total") or 0) - 1
                    )
                    ocr["work_total"] = max(
                        0, int(ocr.get("work_total") or 0) - 1
                    )
                record["revision"] = int(record.get("revision") or 0) + 1
                record["event"] = "ocr_completed_items_skipped"
                record["updated_at"] = _now()
                self._persist(record)
            for target in completed:
                self._record_shared_outcome(
                    job_id,
                    target,
                    succeeded=True,
                    application_attempted=False,
                )
        if not pending:
            return

        if cached_job is not None:
            self._record_provider_result(job_id, cached=True)
        elif cached_content is not None:
            source_document, source_item_id = cached_content
            for target in pending:
                question_path, asset_id, _, _, _ = target
                current = load_json_object(question_path)
                revision = current.get("workflow", {}).get("revision")
                try:
                    if isinstance(revision, bool) or not isinstance(revision, int):
                        raise ModelError("invalid shared explanation target revision")
                    clone_explanation_content_cas(
                        source_document,
                        question_path,
                        asset_id,
                        source_sha256,
                        source_item_id=source_item_id,
                        expected_revision=revision,
                    )
                except (RevisionConflict, OSError) as exc:
                    self._record_shared_outcome(
                        job_id,
                        target,
                        succeeded=False,
                        error_code=_local_ocr_error_code(exc),
                        application_attempted=True,
                    )
                except (ModelError, OSError) as exc:
                    self._record_shared_outcome(job_id, target, succeeded=False,
                        error_code=_ocr_error_code(exc), application_attempted=True)
                else:
                    self._record_shared_outcome(
                        job_id,
                        target,
                        succeeded=True,
                        application_attempted=True,
                        content_reused=True,
                    )
            return

        if cached_job is None:
            leader = pending[0]
            try:
                cached_job = create_transcription_job(
                    analysis_owner[0] if analysis_owner else leader[0],
                    analysis_owner[1] if analysis_owner else leader[1],
                    "explanation",
                    config=config,
                    shared_parent_mode=True,
                    provider_profile=parse_provider_profile(provider_profile),
                )
            except Exception as exc:
                self._record_provider_result(job_id, failed=True)
                error_code = _ocr_error_code(exc)
                for target in pending:
                    self._record_shared_outcome(
                        job_id,
                        target,
                        succeeded=False,
                        error_code=error_code,
                        application_attempted=False,
                    )
                if isinstance(exc, CodexRateLimitError):
                    raise
                return
            self._record_provider_result(
                job_id,
                cached=cached_job.cached,
                remove_planned_call=cached_job.cached,
            )

        for target in pending:
            try:
                self._apply_shared_candidate(cached_job, target)
            except (RevisionConflict, OSError) as exc:
                self._record_shared_outcome(
                    job_id,
                    target,
                    succeeded=False,
                    error_code=_local_ocr_error_code(exc),
                    application_attempted=True,
                )
            except Exception:
                self._record_shared_outcome(
                    job_id,
                    target,
                    succeeded=False,
                    application_attempted=True,
                )
            else:
                self._record_shared_outcome(
                    job_id,
                    target,
                    succeeded=True,
                    application_attempted=True,
                )

    def _try_transcription(
        self,
        job_id: str,
        question_path: Path,
        asset_id: str,
        section: str,
        unit_id: str,
        config: ProviderConfig,
        *,
        replacing_failure: bool = False,
        track_work: bool = False,
        provider_profile: Any = None,
    ) -> None:
        with self._lock:
            record = self._jobs[job_id]
            ocr = record["summary"]["ocr"]
            ocr["attempted"] += 1
            record["revision"] = int(record.get("revision") or 0) + 1
            record["event"] = "ocr_attempt_started"
            record["updated_at"] = _now()
            self._persist(record)
        candidate_job: CandidateJob | None = None
        try:
            candidate_job = create_transcription_job(
                question_path,
                asset_id,
                section,
                config=config,
                provider_profile=parse_provider_profile(provider_profile),
            )
            if track_work:
                self._record_provider_result(
                    job_id,
                    cached=bool(getattr(candidate_job, "cached", False)),
                    remove_planned_call=bool(
                        getattr(candidate_job, "cached", False)
                    ),
                )
            apply_options: dict[str, Any] = {}
            if section == "explanation":
                current = load_json_object(question_path)
                content = current.get("content")
                if isinstance(content, Mapping) and content.get("answer") is not None:
                    apply_options["preserve_existing_answer"] = True
            if track_work:
                self._record_application_started(job_id)
            try:
                apply_candidate_job(
                    question_path,
                    candidate_job.path,
                    expected_base_revision=int(
                        candidate_job.metadata["base_revision"]
                    ),
                    **apply_options,
                )
            except CandidateValidationError as exc:
                if section != "explanation" or (
                    "cannot be linked without inference" not in str(exc)
                ):
                    raise
                apply_candidate_job(
                    question_path,
                    candidate_job.path,
                    expected_base_revision=int(
                        candidate_job.metadata["base_revision"]
                    ),
                    preserve_existing_answer=True,
                    discard_candidate_answer=True,
                )
        except Exception as exc:
            if track_work and candidate_job is None:
                self._record_provider_result(job_id, failed=True)
            error_code = _ocr_error_code(exc)
            with self._lock:
                record = self._jobs[job_id]
                ocr = record["summary"]["ocr"]
                failure = {
                    "unit_id": unit_id,
                    "section": section,
                    "error_code": error_code,
                    "phase": "application" if candidate_job is not None else "analysis",
                }
                if replacing_failure:
                    for index, previous in enumerate(ocr["failures"]):
                        if (
                            previous.get("unit_id") == unit_id
                            and previous.get("section") == section
                        ):
                            ocr["failures"][index] = failure
                            break
                else:
                    ocr["failed"] += 1
                    ocr[f"{section}_failed"] += 1
                    ocr["failures"].append(failure)
                self._record_ocr_progress(record)
                record["revision"] = int(record.get("revision") or 0) + 1
                record["event"] = "ocr_attempt_failed"
                record["updated_at"] = _now()
                self._persist(record)
            if isinstance(exc, CodexRateLimitError):
                raise
            return
        with self._lock:
            record = self._jobs[job_id]
            ocr = record["summary"]["ocr"]
            if replacing_failure:
                for index, previous in enumerate(ocr["failures"]):
                    if (
                        previous.get("unit_id") == unit_id
                        and previous.get("section") == section
                    ):
                        ocr["failures"].pop(index)
                        ocr["failed"] -= 1
                        ocr[f"{section}_failed"] -= 1
                        break
            ocr["succeeded"] += 1
            ocr[f"{section}_succeeded"] += 1
            self._record_ocr_progress(record)
            record["revision"] = int(record.get("revision") or 0) + 1
            record["event"] = "ocr_attempt_succeeded"
            record["updated_at"] = _now()
            self._persist(record)

    def _ensure_crop_engine(self) -> None:
        try:
            health = self._request_json("GET", "/api/health")
            if health.get("status") == "ok":
                return
        except Exception:
            pass
        python = ASTRA.python
        server = self.crop_root / "app_server.py"
        if not python.is_file() or not server.is_file():
            raise RuntimeError("crop backend unavailable")
        engine_logs = self.jobs_root / "engine"
        engine_logs.mkdir(parents=True, exist_ok=True)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with (engine_logs / "stdout.log").open("ab") as stdout, (
            engine_logs / "stderr.log"
        ).open("ab") as stderr:
            process = subprocess.Popen(
                [str(python), "-B", "-u", str(server), "--port", str(ASTRA.crop_port)],
                cwd=self.crop_root,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                creationflags=flags,
            )
        atomic_write_json(
            engine_logs / "engine.json",
            {"pid": process.pid, "started_at": _now()},
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            time.sleep(min(self.poll_seconds, 1.0))
            try:
                health = self._request_json("GET", "/api/health")
                if health.get("status") == "ok":
                    return
            except Exception:
                continue
        raise RuntimeError("crop engine did not start")

    def _wait_for_crop(
        self,
        job_id: str,
        crop_job_id: str,
        require_solution: bool,
        *,
        initial_record: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        with self._lock:
            request = self._jobs[job_id].get("request")
        if not isinstance(request, Mapping):
            raise RuntimeError("batch request is invalid")
        expected_provider_profile = parse_provider_profile(
            request.get("provider_profile")
        )
        deadline = None if require_solution else time.monotonic() + MAX_POLL_SECONDS
        pending_record = initial_record
        while deadline is None or time.monotonic() < deadline:
            self._checkpoint(job_id)
            if pending_record is not None:
                record = pending_record
                pending_record = None
            else:
                record = self._request_json(
                    "GET", f"/api/jobs/{quote(crop_job_id, safe='')}"
                )
            self._require_crop_provider_profile(record, expected_provider_profile)
            self._checkpoint(job_id)
            state = record.get("state")
            if state in TERMINAL_CROP_FAILURES:
                failure = parse_page_failure(record.get("detection_failure"))
                if failure is not None:
                    self._update(
                        job_id,
                        stage=failure["stage"],
                        progress=round(failure["completed"] / failure["total"] * 100),
                        message=page_failure_message(failure),
                        detection_failure=failure,
                    )
                    raise _CropTerminalFailure(page_failure_message(failure))
                raise _CropTerminalFailure("crop failed")
            if state not in {"queued", "running", "completed"}:
                raise RuntimeError("invalid crop state")
            if state == "completed":
                if not require_solution:
                    return record
                solution = record.get("solution")
                if not isinstance(solution, Mapping):
                    raise RuntimeError("solution result missing")
                solution_state = solution.get("state")
                if solution_state == "completed":
                    return record
                if solution_state in TERMINAL_CROP_FAILURES:
                    # Preserve the completed question crops. The importer will
                    # register them with explicit explanation-review metadata.
                    return record
                if solution_state not in {"queued", "indexing", "linking"}:
                    raise RuntimeError("invalid solution state")
                solution_progress = solution.get("progress")
                projected = (
                    68 + min(3, max(0, solution_progress))
                    if type(solution_progress) is int
                    else 68
                )
                self._update(
                    job_id,
                    stage="solution",
                    progress=projected,
                    message="Linking problems to detailed solutions.",
                )
            else:
                crop_progress = record.get("progress")
                projected = (
                    5 + round(min(100, max(0, crop_progress)) * 0.62)
                    if type(crop_progress) is int
                    else 5
                )
                self._update(
                    job_id,
                    stage="crop",
                    progress=projected,
                    message="Cropping and verifying problem pages.",
                )
            time.sleep(self.poll_seconds)
        raise RuntimeError("crop timed out")

    @staticmethod
    def _completed_document(record: Mapping[str, Any]) -> Mapping[str, Any]:
        documents = record.get("documents")
        if not isinstance(documents, list) or not documents or not isinstance(documents[0], Mapping):
            raise RuntimeError("crop document missing")
        document = documents[0]
        if not isinstance(document.get("items"), list):
            raise RuntimeError("crop items missing")
        return document

    def _allowed_run_dir(self, document: Mapping[str, Any]) -> Path:
        value = document.get("run_dir")
        if not isinstance(value, str) or not value:
            raise RuntimeError("crop run missing")
        try:
            run_dir = Path(value).expanduser().resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("crop run unavailable") from exc
        if (
            not run_dir.is_dir()
            or not run_dir.is_relative_to(self.crop_root)
            or not _inside(run_dir, self.import_roots)
            or not (run_dir / "manifest.json").is_file()
        ):
            raise RuntimeError("crop run is outside the import allowlist")
        return run_dir

    @staticmethod
    def _ocr_units(document: Mapping[str, Any]) -> set[str]:
        result: set[str] = set()
        seen: set[str] = set()
        for item in document["items"]:
            if not isinstance(item, Mapping):
                raise RuntimeError("invalid crop item")
            unit_id = item.get("id") or item.get("unit_id")
            verdict = item.get("verdict")
            if not isinstance(unit_id, str) or not unit_id or not isinstance(verdict, str):
                raise RuntimeError("invalid crop item fields")
            if unit_id in seen or verdict not in {"pass", "review", "fail"}:
                raise RuntimeError("invalid crop item fields")
            seen.add(unit_id)
            result.add(unit_id)
        return result

    @staticmethod
    def _request_json(
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(CROP_API + path, data=data, headers=headers, method=method)
        try:
            with _URL_OPENER.open(request, timeout=15) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("crop API unavailable") from exc
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError("crop API response too large")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("invalid crop API response") from exc
        if not isinstance(result, dict):
            raise RuntimeError("invalid crop API response")
        return result
