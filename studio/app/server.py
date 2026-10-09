"""Loopback-only HTTP server for Question JSON Studio."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import mimetypes
import os
import re
import secrets
import gzip
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from astra_runtime import inside, validate_parsed_paths, port_number
from solution_learning.contracts import ActorKind
from . import ASTRA
from .batch import BatchCoordinator, BatchError, validate_figure_options
from .batch_content import project_content_projection
from .answer_promotion import promote_answer_from_explanation
from .document_routes import DocumentRequest, document_response
from .document_types import DocumentError
from .equation_bridge import equation_source_changed
from .figure_generation import (
    FIGURE_PROFILES,
    GEMINI_FIGURE_GENERATION_DISABLED_MESSAGE,
    active_figure_profile,
    figure_profile_available,
    gemini_figure_generation_enabled,
    IMAGE_SIZE as FIGURE_IMAGE_SIZE,
    PRICING_AS_OF as FIGURE_PRICING_AS_OF,
    FigureGenerationAdapter,
    FigureProviderNotConfigured,
    FigureProviderRequestError,
    regenerate_figure_cas,
)
from .figure_recrop import recrop_figure_cas
from .hwpx_artifacts import (
    HwpxArtifactError,
    create_hwpx_build,
    resolve_hwpx_artifact,
)
from .hwpx_export import ExportPreflightError
from .hwpx_renderer import HwpxRenderError
from .importer import ImportConflictError, import_leaf_run
from astra_cli.quality_reads import quality_read_scope

from .model import (
    ModelError,
    ModelNotFound,
    RevisionConflict,
    approve_question_cas,
    figure_generation_summary,
    is_advisory_review_issue,
    load_json_object,
    registration_summary,
    stored_content_status,
    solution_link_summary,
    update_tags_cas,
    review_figure_asset_cas,
    save_json_cas,
    select_figure_asset_cas,
    validate_id,
)
from .pdf_uploads import (
    MAX_PDF_UPLOAD_BYTES,
    PdfUploadError,
    PdfUploadNotFound,
    PdfUploadTimeout,
    cleanup_pdf_uploads,
    resolve_pdf_upload,
    store_pdf_upload,
)
from .project_archive import (
    ProjectArchiveError,
    create_project_archive,
    resolve_project_archive,
)
from .question_bank import (
    create_question_set,
    delete_question_set,
    QuestionSetArchiveConflict,
    get_question_set,
    list_bank_items,
    list_question_sets,
    patch_question_set,
)
from .question_set_hwpx import (
    QuestionSetHwpxError,
    create_question_set_hwpx_build,
    resolve_question_set_hwpx_artifact,
)
from .recovery_progress import newest_recovery_progress
from .review_gates import evaluate_package
from .solution_reconciliation import (
    SolutionIntegrityError,
    enrich_project_from_leaf,
    locate_leaf_run,
)
from .solution_feedback import (
    ACTOR_KINDS as FEEDBACK_ACTOR_KINDS,
    FeedbackRequestError,
    FeedbackSourceConflict,
    FeedbackSyncError,
    apply_feedback,
    preflight_feedback,
)
from .solution_learning_sync import FeedbackSyncRequest, StudioLearningSync
from .solution_sources import MAX_UPLOAD_BYTES, SolutionSourceError, select_source, source_pages, stage_image
from .learning_library import LearningLibrary, LearningLibraryError
from .transcription import (
    PROVIDER_DEFAULTS,
    CandidateValidationError,
    ProviderNotConfigured,
    ProviderRequestError,
    apply_candidate_job,
    create_transcription_job,
    effective_provider_config,
    load_candidate_job,
    parse_provider_profile,
)
from . import ai_explanation
from .explanation_queue import ExplanationQueue, QueueError
from .trash import Trash, TrashError
from .tagging import (
    TaggingAdapter,
    TaggingError,
    auto_tag_document,
    normalize_manual_tags,
)
from scripts.validate_package import (
    DEFAULT_SCHEMA as PACKAGE_SCHEMA,
    Validator as PackageValidator,
    json_schema_errors,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = ASTRA.studio_port
MAX_JSON_BYTES = 8 * 1024 * 1024
SESSION_COOKIE = "astra_studio_session"
PDF_UPLOAD_IDLE_TIMEOUT_SECONDS = 60.0


def _normalize_origin(
    value: str,
    *,
    allow_tailnet_http: bool = False,
) -> str:
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("trusted origin 포트가 올바르지 않습니다.") from exc
    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname.casefold() if parsed.hostname else ""
    local_http = scheme == "http" and hostname in {"127.0.0.1", "localhost"}
    tailnet_http = (
        scheme == "http"
        and allow_tailnet_http
        and bool(hostname)
        and ("." not in hostname or hostname.endswith(".ts.net"))
        and port not in {None, 80}
    )
    if (
        scheme not in {"http", "https"}
        or (scheme != "https" and not local_http and not tailnet_http)
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "trusted origin은 경로 없는 HTTPS 또는 tailnet HTTP origin이어야 합니다."
        )
    default_port = 443 if scheme == "https" else 80
    port = default_port if port is None else port
    if not 1 <= port <= 65535:
        raise ValueError("trusted origin 포트가 올바르지 않습니다.")
    host = f"[{hostname}]" if ":" in hostname else hostname
    suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{host}{suffix}"


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class AppConfig:
    workspace_root: Path
    web_root: Path
    import_roots: tuple[Path, ...]
    pdf_roots: tuple[Path, ...]
    crop_root: Path
    recovery_root: Path
    token: str
    host: str
    port: int
    trusted_origins: tuple[str, ...]
    tailnet_owner_login: str | None
    review_actor_kind: str = "human"


class StudioApplication:
    def __init__(
        self,
        config: AppConfig,
        *,
        figure_adapter: FigureGenerationAdapter | None = None,
        tagging_adapter: TaggingAdapter | None = None,
        explanation_runner: "ai_explanation.Runner | None" = None,
        feedback_sync: Callable[[FeedbackSyncRequest], None] | None = None,
        learning_library: LearningLibrary | None = None,
        explanation_queue_autostart: bool = True,
    ) -> None:
        self.config = config
        self.figure_adapter = figure_adapter
        self.tagging_adapter = tagging_adapter
        self.explanation_runner = explanation_runner or ai_explanation.cli_runner
        self.feedback_sync = feedback_sync
        self.learning_library = learning_library
        self._import_lock = threading.Lock()
        self._project_status_lock = threading.Lock()
        self._projects_listed = threading.Event()
        self.config.workspace_root.mkdir(parents=True, exist_ok=True)
        self.trash = Trash(self.config.workspace_root, busy=lambda pid: any(
            job.get("project_id") == pid and job.get("state") not in {"completed", "failed", "interrupted", "cancelled"}
            for job in self.batches.list()
        ))
        self.batches = BatchCoordinator(
            workspace_root=self.config.workspace_root,
            pdf_roots=(*self.config.pdf_roots, self.config.workspace_root / ".uploads"),
            import_roots=self.config.import_roots,
            crop_root=self.config.crop_root,
            figure_adapter=self.figure_adapter,
            is_deleted=self.trash.is_deleted,
        )
        cleanup_pdf_uploads(
            self.config.workspace_root,
            retained_ids=self.batches.retained_upload_ids(),
        )
        # Warm the question-bank cache off the startup path so readiness is not
        # delayed by a full workspace scan; requests that arrive first build it themselves.
        threading.Thread(target=self._warm_bank_cache, name="bank-cache-warmup", daemon=True).start()
        self.explanation_queue = ExplanationQueue(
            self.config.workspace_root, self._run_queued_explanation, autostart=explanation_queue_autostart
        )
        from .item_generation_bot import BotChats
        from .item_generation_pipeline import GenerationWorker

        # Same switch as the explanation queue: tests construct the app without background AI work.
        self.item_generation_worker = GenerationWorker(self.config.workspace_root, autostart=explanation_queue_autostart)
        self.item_generation_bots = BotChats(self.config.workspace_root)

    def _warm_bank_cache(self) -> None:
        # Let the first project list (the app's opening screen) finish first; running the
        # full bank scan concurrently starves it of the GIL and disk (measured 2.8s -> 72s).
        self._projects_listed.wait(timeout=60)
        try:
            list_bank_items(self.config.workspace_root, lightweight=True)
        except Exception:  # best effort; a real request rebuilds and reports errors
            pass

    def recovery_progress(self) -> dict[str, Any] | None:
        return newest_recovery_progress(self.config.recovery_root)

    def store_pdf_upload(
        self, stream: Any, *, content_length: int, original_name: str
    ) -> dict[str, Any]:
        try:
            cleanup_pdf_uploads(
                self.config.workspace_root,
                retained_ids=self.batches.retained_upload_ids(),
            )
            upload = store_pdf_upload(
                self.config.workspace_root,
                stream,
                content_length=content_length,
                original_name=original_name,
            )
        except PdfUploadTimeout as exc:
            raise ApiError(HTTPStatus.REQUEST_TIMEOUT, str(exc)) from exc
        except PdfUploadError as exc:
            status = (
                HTTPStatus.INSUFFICIENT_STORAGE
                if "quota" in str(exc).casefold() or "disk space" in str(exc).casefold()
                else HTTPStatus.BAD_REQUEST
            )
            raise ApiError(status, str(exc)) from exc
        return {
            "upload_id": upload.upload_id,
            "name": upload.original_name,
            "size_bytes": upload.size,
            "sha256": upload.sha256,
        }

    def require_batch_active(self, job_id: str) -> None:
        job = self.batches.get(job_id)
        project_id = job.get("project_id")
        if isinstance(project_id, str):
            self.trash.require_active(project_id)

    def create_batch(self, body: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(body)
        create_options: dict[str, Any] = {}
        for upload_field, path_field, name_field, identity_field in (
            ("source_upload_id", "source_path", "source_name", "source_upload"),
            ("solution_upload_id", "solution_path", "solution_name", "solution_upload"),
        ):
            upload_id = payload.pop(upload_field, None)
            if upload_id is None:
                continue
            if payload.get(path_field):
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    f"{upload_field}와 {path_field}는 동시에 사용할 수 없습니다.",
                )
            try:
                upload = resolve_pdf_upload(self.config.workspace_root, upload_id)
            except PdfUploadNotFound as exc:
                raise ApiError(HTTPStatus.NOT_FOUND, "업로드한 PDF를 찾을 수 없습니다.") from exc
            except PdfUploadError as exc:
                raise ApiError(HTTPStatus.CONFLICT, "업로드한 PDF 무결성이 일치하지 않습니다.") from exc
            payload[path_field] = str(upload.path)
            create_options[name_field] = upload.original_name
            create_options[identity_field] = {
                "upload_id": upload.upload_id,
                "sha256": upload.sha256,
            }
        return self.batches.create(payload, **create_options)

    def _project_provider_profile(
        self, project_id: str, supplied: Any = None
    ) -> str:
        effective = self.batches.project_provider_profile(project_id)
        if supplied is None:
            return effective
        try:
            requested = parse_provider_profile(supplied)
        except CandidateValidationError as exc:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "provider_profile must be full or codex_only."
            ) from exc
        if requested != effective:
            raise ApiError(
                HTTPStatus.CONFLICT, "provider_profile cannot change after project creation."
            )
        return effective

    def reconcile_project_solutions(
        self, project_id: str, profile: str
    ) -> dict[str, Any]:
        project_dir, project = self._project(project_id)
        try:
            leaf = locate_leaf_run(project, self.config.import_roots)
            if leaf is None:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "원본 Question_Crop 결과를 찾을 수 없어 해설을 재판정할 수 없습니다.",
                )
            result = enrich_project_from_leaf(project_dir, leaf)
        except SolutionIntegrityError as exc:
            raise ApiError(
                HTTPStatus.CONFLICT, "해설 재판정 원본의 무결성이 일치하지 않습니다."
            ) from exc
        self._refresh_project_status(project_id)
        ocr_job = None
        try:
            ocr_job = self.transcribe_project_missing(project_id, profile)
        except BatchError as exc:
            if exc.status != HTTPStatus.CONFLICT or "no missing" not in str(exc).casefold():
                raise
        return {
            "updated_item_count": len(result.updated_item_ids),
            "preserved_verified_count": len(result.preserved_verified_item_ids),
            "missing_detailed_solution_count": len(result.missing_item_ids),
            "added_candidate_count": result.added_candidate_count,
            "ocr_job": ocr_job,
        }

    def transcribe_project_missing(
        self, project_id: str, profile: str, provider_profile: Any = None
    ) -> dict[str, Any]:
        effective = self._project_provider_profile(project_id, provider_profile)
        return self.batches.transcribe_missing(project_id, profile, effective)

    def transcribe_project_solutions(
        self, project_id: str, item_ids: list[str] | None = None, *, answer_backfill_only: bool = False
    ) -> dict[str, Any]:
        return self.batches.transcribe_project_solutions(
            project_id, item_ids, answer_backfill_only=answer_backfill_only)

    def upload_solution_image(self, project_id: str, item_id: str, body: bytes, media_type: str, filename: str) -> dict[str, Any]:
        return stage_image(self._item_path(project_id, item_id), body, media_type, filename)

    def select_item_solution_source(self, project_id: str, item_id: str, body: Mapping[str, Any], *, revert: bool = False) -> dict[str, Any]:
        item_path = self._item_path(project_id, item_id)
        result = select_source(item_path, body, revert=revert,
                               import_roots=tuple(self.config.import_roots))
        job_id = self.batches.enqueue_solution_operation(project_id, item_id, result["operation_id"])
        result["job_id"] = job_id
        if isinstance(self.feedback_sync, StudioLearningSync):
            try:
                self.feedback_sync.sync_source(item_path, result["operation_id"])
            except FeedbackSyncError as exc:
                result["learning_sync"] = {"status": "pending", "retryable": True, "reason": str(exc)}
            else:
                result["learning_sync"] = {"status": "synced", "retryable": False}
        return result

    def item_solution_source_pages(self, project_id: str, item_id: str) -> dict[str, Any]:
        return source_pages(self._item_path(project_id, item_id), project_id, item_id,
                            tuple(self.config.import_roots))

    def preflight_project_solution_relink(self, project_id: str) -> dict[str, Any]:
        return self.batches.preflight_solution_relink(project_id)

    def start_project_solution_relink(
        self, project_id: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self.batches.confirm_solution_relink_plan(
            project_id,
            body.get("plan_id"),
            body.get("plan_sha256"),
            body.get("confirmed"),
        )

    def preview_item_solution_propagation(
        self, project_id: str, item_id: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self.batches.preview_solution_confirmation(
            project_id,
            item_id,
            expected_revision=body.get("expected_revision"),
            source_asset_id=body.get("source_asset_id"),
            propagate_scope=body.get("propagate_scope"),
        )

    def apply_item_solution_propagation(
        self, project_id: str, item_id: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        result = self.batches.apply_solution_confirmation(
            project_id,
            item_id,
            preview_id=body.get("preview_id"),
            preview_sha256=body.get("preview_sha256"),
            confirmed=body.get("confirmed"),
        )
        self._refresh_project_status(project_id)
        return result

    def preflight_solution_feedback(
        self,
        project_id: str,
        item_id: str,
        body: Mapping[str, Any],
        reviewer_id: str,
    ) -> dict[str, Any]:
        try:
            return preflight_feedback(
                self._item_path(project_id, item_id), body,
                actor_kind=self.config.review_actor_kind, reviewer_id=reviewer_id,
            )
        except FeedbackRequestError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        except FeedbackSourceConflict as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc

    def apply_solution_feedback(
        self,
        project_id: str,
        item_id: str,
        body: Mapping[str, Any],
        reviewer_id: str,
    ) -> dict[str, Any]:
        item_path = self._item_path(project_id, item_id)
        try:
            result = apply_feedback(
                item_path, body,
                actor_kind=self.config.review_actor_kind, reviewer_id=reviewer_id,
            )
        except FeedbackRequestError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        except FeedbackSourceConflict as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
        learning_sync: dict[str, Any]
        if self.feedback_sync is None:
            learning_sync = {"status": "pending", "reason": "studio_event_ingester_not_configured",
                             "retryable": True, "event_id": result.event["event_id"]}
        else:
            try:
                self.feedback_sync(FeedbackSyncRequest(
                    item_path=item_path,
                    project_path=item_path.parents[2] / "project.json",
                    event=result.event,
                    actor_kind=ActorKind(self.config.review_actor_kind),
                    reviewer_id=reviewer_id,
                    action_id=f"studio-action-{secrets.token_hex(16)}",
                ))
            except FeedbackSyncError as exc:
                learning_sync = {"status": "pending", "reason": str(exc), "retryable": True,
                                 "event_id": result.event["event_id"]}
            else:
                learning_sync = {"status": "synced", "retryable": False,
                                 "event_id": result.event["event_id"]}
        return {"status": "applied", "revision": result.saved["workflow"]["revision"],
                "event": result.event, "learning_sync": learning_sync}

    def list_learning_examples(self) -> dict[str, Any]:
        if self.learning_library is None:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "학습 사례 저장소가 설정되지 않았습니다.")
        try:
            return self.learning_library.list_examples()
        except LearningLibraryError as exc:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, str(exc)) from exc

    def retract_learning_example(
        self, example_id: str, body: Mapping[str, Any], reviewer_id: str
    ) -> dict[str, Any]:
        if self.learning_library is None:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "학습 사례 저장소가 설정되지 않았습니다.")
        if set(body) != {"reason"} or not isinstance(body.get("reason"), str) or not body["reason"].strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "철회 사유가 필요합니다.")
        try:
            return self.learning_library.retract(
                example_id, reviewer_id=reviewer_id, reason=body["reason"]
            )
        except LearningLibraryError as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc

    @property
    def allowed_hosts(self) -> set[str]:
        hosts = {
            f"127.0.0.1:{self.config.port}",
            f"localhost:{self.config.port}",
        }
        hosts.update(urlsplit(origin).netloc.casefold() for origin in self.config.trusted_origins)
        return hosts

    @property
    def trusted_hosts(self) -> set[str]:
        return {
            urlsplit(origin).netloc.casefold() for origin in self.config.trusted_origins
        }

    @property
    def secure_trusted_hosts(self) -> set[str]:
        return {
            urlsplit(origin).netloc.casefold()
            for origin in self.config.trusted_origins
            if urlsplit(origin).scheme == "https"
        }

    @property
    def short_trusted_hosts(self) -> set[str]:
        return {
            urlsplit(origin).netloc.casefold()
            for origin in self.config.trusted_origins
            if urlsplit(origin).scheme == "http"
            and "." not in (urlsplit(origin).hostname or "")
        }

    @property
    def allowed_origins(self) -> set[str]:
        return {
            _normalize_origin(f"http://127.0.0.1:{self.config.port}"),
            _normalize_origin(f"http://localhost:{self.config.port}"),
            *self.config.trusted_origins,
        }

    def public_config(self) -> dict[str, Any]:
        from astra_cli.isolation import auth_status
        configured = {
            "codex": bool(auth_status()["available"]),
            "gemini": bool(
                (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
            ),
            "openai": bool((os.environ.get("OPENAI_API_KEY") or "").strip()),
        }
        def figure_configured(profile: Any) -> bool:
            if not figure_profile_available(profile):
                return False
            if self.figure_adapter is not None:
                return True
            return configured.get(FIGURE_PROFILES[profile].provider, False)
        return {
            "provider_profiles": ["full", "codex_only"],
            "profiles": {
                name: {
                    "provider": config.provider,
                    "model": config.model,
                    "configured": configured[config.provider],
                }
                for name, config in PROVIDER_DEFAULTS.items()
            },
            "active_figure_profile": active_figure_profile(),
            "figure_profiles": {
                name: {
                    "provider": profile.provider,
                    "model": profile.model,
                    "image_size": FIGURE_IMAGE_SIZE,
                    "estimated_output_usd": profile.estimated_output_usd,
                    "pricing_as_of": FIGURE_PRICING_AS_OF,
                    "configured": figure_configured(name),
                }
                for name, profile in FIGURE_PROFILES.items()
            },
        }

    def check_host(self, host: str | None) -> None:
        if not host or host.strip().casefold() not in self.allowed_hosts:
            raise ApiError(HTTPStatus.FORBIDDEN, "허용되지 않은 Host입니다.")

    def check_origin(self, origin: str | None, *, host: str | None = None) -> None:
        if not origin:
            return
        try:
            normalized = _normalize_origin(origin, allow_tailnet_http=True)
        except ValueError as exc:
            raise ApiError(HTTPStatus.FORBIDDEN, "허용되지 않은 Origin입니다.") from exc
        origin_host = urlsplit(normalized).netloc.casefold()
        if (
            normalized not in self.allowed_origins
            or not host
            or host.strip().casefold() != origin_host
        ):
            raise ApiError(HTTPStatus.FORBIDDEN, "허용되지 않은 Origin입니다.")

    def token_matches(self, candidate: str | None) -> bool:
        return bool(candidate) and secrets.compare_digest(candidate, self.config.token)

    def is_authorized(self, headers: Mapping[str, str], *, method: str) -> bool:
        candidates = [headers.get("X-Session-Token")]
        authorization = headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            candidates.append(authorization[7:])
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("Cookie", ""))
        except Exception:
            pass
        else:
            if SESSION_COOKIE in cookie:
                candidates.append(cookie[SESSION_COOKIE].value)
        if any(self.token_matches(candidate) for candidate in candidates):
            return True

        host = (headers.get("Host") or "").strip().casefold()
        if host not in self.secure_trusted_hosts:
            return False
        if (headers.get("Sec-Fetch-Site") or "").strip().casefold() not in {
            "",
            "none",
            "same-origin",
        }:
            return False
        login = (headers.get("Tailscale-User-Login") or "").strip().casefold()
        owner = (self.config.tailnet_owner_login or "").casefold()
        if not login or not owner or not secrets.compare_digest(login, owner):
            return False
        if method != "GET":
            origin = headers.get("Origin")
            if not origin:
                return False
            try:
                normalized = _normalize_origin(origin, allow_tailnet_http=True)
            except ValueError:
                return False
            if (
                normalized not in self.allowed_origins
                or urlsplit(normalized).netloc.casefold() != host
            ):
                return False
        return True

    def import_project(self, source_path: Any) -> dict[str, Any]:
        if not isinstance(source_path, str) or not source_path.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "source_path가 필요합니다.")
        source = Path(source_path).expanduser()
        if not source.is_absolute():
            raise ApiError(HTTPStatus.BAD_REQUEST, "source_path는 절대경로여야 합니다.")
        try:
            source = source.resolve(strict=True)
        except OSError as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, "가져올 폴더를 찾을 수 없습니다.") from exc
        if not source.is_dir() or not any(_is_within(source, root) for root in self.config.import_roots):
            raise ApiError(HTTPStatus.FORBIDDEN, "허용된 원본 폴더 밖에서는 가져올 수 없습니다.")
        # ponytail: one import lock matches the single desktop workspace; split
        # it per fingerprint only if parallel imports become a real bottleneck.
        with self._import_lock:
            result = import_leaf_run(source, self.config.workspace_root)
        return {
            "project_id": result.project_id,
            "item_count": result.item_count,
            "cached": result.cached,
        }

    def list_projects(self) -> list[dict[str, Any]]:
        try:
            return self._list_projects()
        finally:
            self._projects_listed.set()

    def _list_projects(self) -> list[dict[str, Any]]:
        is_deleted = self.trash.deletion_snapshot()
        projects = []
        for path in sorted(self.config.workspace_root.glob("*/project.json")):
            if path.parent.name.startswith("."):
                continue
            if path.parent.name.startswith("project-itemgen-"):
                # Internal storage for AI-generated items: visible only through the bank.
                continue
            if not _is_within(path.resolve(), self.config.workspace_root):
                continue
            try:
                project = load_json_object(path)
                project_id = validate_id(project.get("project_id"), field="project_id")
                provider_profile = self.batches.project_provider_profile(project_id)
            except (BatchError, ModelError, OSError):
                continue
            if is_deleted(project_id):
                continue
            try:
                projection = project_content_projection(
                    path.parent, is_deleted=lambda item_id: is_deleted(project_id, item_id)
                )
            except (ModelError, OSError):
                continue
            projects.append(
                {
                    "project_id": project_id,
                    "provider_profile": provider_profile,
                    "metadata": project.get("metadata", {}),
                    "workflow": project.get("workflow", {}),
                    "item_count": sum(not is_deleted(project_id, item_id) for item_id in project.get("item_order", [])),
                    **projection,
                }
            )
        return projects

    def bank_items(self) -> list[dict[str, Any]]:
        is_deleted = self.trash.deletion_snapshot()
        items = [item for item in list_bank_items(self.config.workspace_root, lightweight=True)
                 if not is_deleted(item["ref"]["project_id"], item["ref"]["item_id"])]
        profiles: dict[str, str] = {}
        for item in items:
            reference = item.get("ref")
            if not isinstance(reference, Mapping):
                raise BatchError("Bank item project identity is invalid", status=409)
            project_id = validate_id(reference.get("project_id"), field="project_id")
            if project_id not in profiles:
                profiles[project_id] = self.batches.project_provider_profile(project_id)
            item["provider_profile"] = profiles[project_id]
        return items

    def preflight_bank_figures(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if set(body) != {"items"}:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "그림 사전 확인 요청은 items만 포함해야 합니다.",
            )
        return self.batches.preflight_bank_figures(body["items"])

    def start_bank_figure_batch(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if set(body) != {"plan_id", "plan_sha256", "confirmed"}:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "그림 실행 요청은 plan_id, plan_sha256, confirmed만 포함해야 합니다.",
            )
        return self.batches.confirm_bank_figure_plan(
            body["plan_id"], body["plan_sha256"], body["confirmed"]
        )

    def question_sets(self) -> list[dict[str, Any]]:
        return list_question_sets(self.config.workspace_root)

    def create_question_set(self, body: Mapping[str, Any]) -> dict[str, Any]:
        unexpected = sorted(set(body) - {"title", "output_mode", "rendering"})
        if unexpected:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "문제지 생성 요청에 허용되지 않은 필드가 있습니다: " + ", ".join(unexpected),
            )
        created = create_question_set(
            self.config.workspace_root,
            title=body.get("title", "새 문제지"),
            output_mode=body.get("output_mode", "separate"),
            rendering=body.get("rendering"),
        )
        return get_question_set(self.config.workspace_root, created["set_id"])

    def get_question_set(self, set_id: str) -> dict[str, Any]:
        return get_question_set(self.config.workspace_root, set_id)

    def patch_question_set(
        self, set_id: str, expected_revision: Any, patch: Mapping[str, Any]
    ) -> dict[str, Any]:
        saved = patch_question_set(
            self.config.workspace_root, set_id, expected_revision, patch
        )
        return get_question_set(self.config.workspace_root, saved["set_id"])

    def delete_question_set(self, set_id: str, expected_revision: Any) -> dict[str, Any]:
        return delete_question_set(self.config.workspace_root, set_id, expected_revision)

    def build_question_set_hwpx(self, set_id: str) -> dict[str, Any]:
        try:
            build = create_question_set_hwpx_build(self.config.workspace_root, set_id)
        except QuestionSetHwpxError as exc:
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "문제지 HWPX 생성 실패: " + str(exc)[:2000],
            ) from exc
        outputs = [
            {
                "document_kind": artifact.artifact_kind,
                "name": artifact.name,
                "size_bytes": artifact.size,
                "sha256": artifact.sha256,
                "download_url": (
                    f"/api/question-sets/{set_id}/hwpx/builds/"
                    f"{build.build_fingerprint_sha256}/{artifact.artifact_kind}/"
                    f"{artifact.sha256}/download"
                ),
            }
            for artifact in build.outputs
        ]
        receipt = build.receipt_artifact
        return {
            "status": "built",
            "set_id": set_id,
            "build_id": build.build_id,
            "build_fingerprint_sha256": build.build_fingerprint_sha256,
            "cached": build.cached,
            "outputs": outputs,
            "receipt": build.receipt,
            "receipt_file": {
                "name": receipt.name,
                "size_bytes": receipt.size,
                "sha256": receipt.sha256,
                "download_url": (
                    f"/api/question-sets/{set_id}/hwpx/builds/"
                    f"{build.build_fingerprint_sha256}/receipt/"
                    f"{receipt.sha256}/download"
                ),
            },
        }

    def question_set_hwpx_download(
        self,
        set_id: str,
        fingerprint: str,
        artifact_kind: str,
        expected_sha256: str,
    ) -> tuple[Path, str, str, str]:
        try:
            artifact = resolve_question_set_hwpx_artifact(
                self.config.workspace_root,
                set_id,
                fingerprint,
                artifact_kind,
                expected_sha256,
            )
        except QuestionSetHwpxError as exc:
            raise ApiError(
                HTTPStatus.NOT_FOUND, "문제지 HWPX 산출물을 찾을 수 없습니다."
            ) from exc
        return (
            artifact.path,
            artifact.attachment_name,
            artifact.media_type,
            artifact.sha256,
        )

    @quality_read_scope()
    def list_items(self, project_id: str) -> list[dict[str, Any]]:
        is_deleted = self.trash.deletion_snapshot()
        project_dir, project = self._project(project_id)
        target_states = self._solution_target_states(project_id)
        summaries = []
        for item_id in project.get("item_order", []):
            if is_deleted(project_id, item_id):
                continue
            try:
                item_id = validate_id(item_id, field="item_id")
                item_path = project_dir / "items" / item_id / "question.json"
                package = load_json_object(item_path)
                registration = registration_summary(package)
                solution = solution_link_summary(package, item_root=item_path.parent)
                target = target_states.get(item_id)
                if target and target.get("asset_id") == solution["source_asset_id"] and target.get("source_sha256") == solution["source_sha256"]:
                    if target.get("verification_state") in {"verified", "review", "mismatch", "failed"}:
                        solution["verification_state"] = target["verification_state"]
                    if target.get("state") == "failed" and not solution["transcribed"]:
                        solution["transcription_state"] = "failed"
                    elif target.get("state") in {"pending", "running"} and solution["source_present"] and not solution["transcribed"]:
                        solution["transcription_state"] = "running" if target["state"] == "running" else "pending"
                summaries.append(
                    {
                        "item_id": item_id,
                        "identity": package.get("identity", {}),
                        "workflow": {**package.get("workflow", {}), "status": registration["workflow_status"]},
                        "review": package.get("review", {}),
                        "issue_count": len(package.get("review", {}).get("issues", [])),
                        "warning_count": registration["warning_count"],
                        "registration": registration,
                        "content_status": stored_content_status(package),
                        "solution_link": solution,
                        "tags": package.get("tags"),
                        "figure_generation": figure_generation_summary(package),
                        "asset_count": len(package.get("resources", {}).get("assets", [])),
                        "generation_review": self._generation_review_state(package),
                    }
                )
            except (ModelError, OSError):
                summaries.append(
                    {
                        "item_id": str(item_id),
                        "workflow": {"status": "error"},
                        "issue_count": 1,
                        "warning_count": 1,
                        "registration": {
                            "status": "unavailable",
                            "usable": False,
                            "content_mode": "unavailable",
                            "source_fallback_available": False,
                            "warning_count": 1,
                            "workflow_status": "error",
                        },
                        "solution_link": {
                            "status": "missing",
                            "confidence": 0.0,
                            "candidate_count": 0,
                            "detailed_solution_count": 0,
                            "quick_answer_count": 0,
                            "detailed_solution_present": False,
                            "quick_answer_only": False,
                            "review_candidate_count": 0,
                            "review_candidate_present": False,
                            "detailed_solution_missing": True,
                            "first_blocker": None,
                            "review_reason_count": 1,
                            "attempt_count": 0,
                            "best_effort_exhausted": True,
                        },
                        "figure_generation": {
                            "figure_count": 0,
                            "regenerated_count": 0,
                            "missing_regeneration_count": 0,
                            "selected_regenerated_count": 0,
                            "review_pending_count": 0,
                            "blocked_mixed_count": 0,
                            "has_figures": False,
                            "has_regenerated": False,
                            "has_missing_regeneration": False,
                            "has_selected_regenerated": False,
                            "needs_review": False,
                            "has_blocked_mixed": False,
                        },
                        "asset_count": 0,
                    }
                )
        return summaries

    def _solution_target_states(self, project_id: str) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for job in self.batches.list():
            if job.get("project_id") != project_id:
                continue
            for target in job.get("solution_targets", []):
                if isinstance(target, dict) and isinstance(target.get("item_id"), str):
                    latest.setdefault(target["item_id"], target)
        return latest

    def get_item(self, project_id: str, item_id: str) -> dict[str, Any]:
        item_path = self._item_path(project_id, item_id)
        package = load_json_object(item_path)
        return self._item_response(project_id, item_id, package)

    def save_item(
        self,
        project_id: str,
        item_id: str,
        expected_revision: Any,
        package: Any,
    ) -> dict[str, Any]:
        item_path = self._item_path(project_id, item_id)
        if not isinstance(package, Mapping):
            raise ApiError(HTTPStatus.BAD_REQUEST, "package는 JSON 객체여야 합니다.")
        if package.get("item_id") != item_id:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "item_id는 변경할 수 없습니다.")
        validation_package = copy.deepcopy(dict(package))
        workflow = validation_package.get("workflow")
        if not isinstance(workflow, dict):
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "package.workflow는 JSON 객체여야 합니다.",
            )
        content = validation_package.get("content")
        if isinstance(content, Mapping) and any(
            content.get(section) is not None
            for section in ("question", "answer", "explanation")
        ):
            workflow["status"] = "needs_review"
        current_content = load_json_object(item_path).get("content")
        if equation_source_changed(current_content, validation_package.get("content")):
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "원문 수식은 보존됩니다. 수정할 식은 LaTeX에 입력하세요.")
        errors = self._package_errors(item_path, validation_package)
        if errors:
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "package 검증 실패: " + errors[0],
            )
        saved = save_json_cas(item_path, validation_package, expected_revision)
        self._refresh_project_status(project_id)
        return self._item_response(project_id, item_id, saved)

    def promote_project_answers(
        self, project_id: str, *, dry_run: bool, item_ids: list[str] | None = None
    ) -> dict[str, Any]:
        if type(dry_run) is not bool:
            raise ApiError(HTTPStatus.BAD_REQUEST, "dry_run은 boolean이어야 합니다.")
        project_dir, project = self._project(project_id)
        order = project.get("item_order", [])
        if item_ids is not None and (not isinstance(item_ids, list)
                or any(not isinstance(item_id, str) for item_id in item_ids)
                or len(set(item_ids)) != len(item_ids)):
            raise ApiError(HTTPStatus.BAD_REQUEST, "item_ids는 중복 없는 문자열 배열이어야 합니다.")
        selected = order if item_ids is None else item_ids
        deleted = {item_id for item_id in selected
                   if item_id in order and self.trash.is_deleted(project_id, item_id)}
        if item_ids is None:
            selected = [item_id for item_id in selected if item_id not in deleted]
        # Resolve the entire request before making any writes.
        paths = {item_id: self._item_path(project_id, item_id)
                 for item_id in selected if item_id not in deleted}
        documents = {item_id: load_json_object(project_dir / "items" / item_id / "question.json")
                     for item_id in order if not self.trash.is_deleted(project_id, item_id)}
        source_owners: dict[str, set[str]] = {}
        for item_id, document in documents.items():
            asset_ids = {block.get("source_region", {}).get("source_asset_id")
                         for block in (document.get("content", {}).get("explanation") or {}).get("blocks", [])}
            for asset in document.get("resources", {}).get("assets", []):
                if asset.get("asset_id") in asset_ids and asset.get("sha256"):
                    source_owners.setdefault(asset["sha256"], set()).add(item_id)
        shared_items = set().union(*(owners for owners in source_owners.values() if len(owners) > 1))
        rows = []
        reasons: dict[str, int] = {}
        promoted_count = 0
        for item_id in selected:
            if item_id in deleted:
                reasons["deleted"] = reasons.get("deleted", 0) + 1
                rows.append({"item_id": item_id, "action": "skip", "reason": "deleted",
                             "answer": None})
                continue
            path = paths[item_id]
            document = documents[item_id]
            answer, reason = promote_answer_from_explanation(document)
            if answer is not None and item_id in shared_items:
                answer, reason = None, "shared_explanation"
            revision = document["workflow"]["revision"]
            if answer is not None:
                updated = copy.deepcopy(document)
                updated["content"]["answer"] = answer
                updated["review"]["answer"] = "unreviewed"
                updated["review"].update(approved_revision=None, approved_sha256=None,
                                         reviewer=None, reviewed_at=None)
                updated["workflow"]["status"] = "needs_review"
                errors = self._package_errors(path, updated)
                if errors:
                    # Historical incomplete packages must not block every other item.
                    # Report their exact validation failures without repairing source data.
                    reason = "invalid_package"
                    reasons[reason] = reasons.get(reason, 0) + 1
                    rows.append({"item_id": item_id, "revision": revision, "action": "skip",
                                 "reason": reason, "answer": None, "validation_errors": errors})
                    continue
                # The validator checks referenced draft assets only in content_ready;
                # this operation must also verify them before publishing an answer.
                file_validator = PackageValidator(path, check_files=True)
                for asset in updated.get("resources", {}).get("assets", []):
                    if asset.get("state") == "ready":
                        file_validator.check_file_for_asset(asset["asset_id"], asset)
                errors.extend(file_validator.errors)
                if errors:
                    raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "package 검증 실패: " + errors[0])
                if not dry_run:
                    # Retain source blocks/coverage exactly; approval was invalidated above.
                    saved = save_json_cas(path, updated, revision, preserve_approval=True)
                    revision = saved["workflow"]["revision"]
                promoted_count += 1
            reasons[reason] = reasons.get(reason, 0) + 1
            rows.append({"item_id": item_id, "revision": revision,
                         "action": "promote" if answer is not None else "skip",
                         "reason": reason, "answer": answer})
        if not dry_run and promoted_count:
            self._refresh_project_status(project_id)
        return {"project_id": project_id, "dry_run": dry_run, "items": rows,
                "summary": {"total": len(rows), "promote": promoted_count,
                            "skip": len(rows) - promoted_count, "reasons": reasons,
                            "deleted_skipped": len(deleted)}}

    def approve_item(
        self,
        project_id: str,
        item_id: str,
        expected_revision: Any,
        reviewer: Any,
    ) -> dict[str, Any]:
        item_path = self._item_path(project_id, item_id)
        if reviewer is not None and (not isinstance(reviewer, str) or len(reviewer) > 200):
            raise ApiError(HTTPStatus.BAD_REQUEST, "reviewer는 200자 이하 문자열이어야 합니다.")
        document = load_json_object(item_path)
        if document.get("workflow", {}).get("status") != "source_only":
            blocking = [
                issue
                for issue in self._validation_issues(item_path, document)
                if issue.get("blocking") is True
                and issue.get("code") != "figure_asset_unreviewed"
            ]
            if blocking:
                raise ApiError(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    f"검수 차단 항목 {len(blocking)}개를 해결한 뒤 승인하세요.",
                )
        approved = approve_question_cas(
            item_path,
            expected_revision,
            reviewer or "local-user",
            validate_candidate=lambda candidate: self._package_errors(item_path, candidate),
            manual_confirmation=True,
        )
        self._refresh_project_status(project_id)
        return self._item_response(project_id, item_id, approved)

    def auto_tag_item(
        self, project_id: str, item_id: str, expected_revision: Any
    ) -> dict[str, Any]:
        item_path = self._item_path(project_id, item_id)
        _, project = self._project(project_id)
        document = load_json_object(item_path)
        current_revision = document.get("workflow", {}).get("revision")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ApiError(HTTPStatus.BAD_REQUEST, "expected_revision은 정수여야 합니다.")
        if current_revision != expected_revision:
            raise RevisionConflict(expected_revision, current_revision)
        try:
            tags, warning = auto_tag_document(
                project,
                document,
                adapter=self.tagging_adapter,
                config=PROVIDER_DEFAULTS["balanced"],
            )
        except TaggingError as exc:
            raise ApiError(HTTPStatus.BAD_GATEWAY, str(exc)) from exc
        candidate = copy.deepcopy(document)
        candidate["tags"] = tags
        errors = self._package_errors(item_path, candidate)
        if errors:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "태그 검증 실패: " + errors[0])
        saved = (
            document
            if document.get("tags") == tags
            else update_tags_cas(item_path, expected_revision, tags)
        )
        response = self._item_response(project_id, item_id, saved)
        if warning:
            response["tagging_warning"] = warning
        return response

    def write_ai_explanation(
        self, project_id: str, item_id: str, expected_revision: Any, mode: str = "write"
    ) -> dict[str, Any]:
        """Write (no explanation yet) or supplement (append to the original) an explanation.

        The answer is never changed and original explanation blocks are never edited.
        If any AI flags the answer or missing figure information, nothing is saved.
        """
        if mode not in {"write", "supplement"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "mode는 write 또는 supplement여야 합니다.")
        item_path = self._item_path(project_id, item_id)
        _, project = self._project(project_id)
        document = load_json_object(item_path)
        current_revision = document.get("workflow", {}).get("revision")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ApiError(HTTPStatus.BAD_REQUEST, "expected_revision은 정수여야 합니다.")
        if current_revision != expected_revision:
            raise RevisionConflict(expected_revision, current_revision)
        raw_content = document.get("content")
        content: Mapping[str, Any] = raw_content if isinstance(raw_content, Mapping) else {}
        has_explanation = content.get("explanation") is not None
        if mode == "write" and has_explanation:
            raise ApiError(HTTPStatus.CONFLICT, "이미 해설이 있는 문항입니다. 보충을 사용하세요.")
        if mode == "supplement" and not has_explanation:
            raise ApiError(HTTPStatus.CONFLICT, "보충할 기존 해설이 없습니다.")
        if mode == "supplement" and (document.get("provenance") or {}).get("ai_explanation"):
            raise ApiError(HTTPStatus.CONFLICT, "이미 AI가 작성하거나 보충한 해설입니다.")
        answer = ai_explanation.answer_text(document)
        if not answer.strip():
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "확정 정답이 없는 문항은 AI 해설을 쓸 수 없습니다.")
        try:
            result = ai_explanation.write_explanation(
                project,
                document,
                question_image=item_path.parent / "source" / "question.png",
                runner=self.explanation_runner,
                supplement=mode == "supplement",
            )
        except ai_explanation.ExplanationError as exc:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
        summary: dict[str, Any] = {
            "mode": mode,
            "plan": result.plan,
            "saved": False,
            "sufficient": [d.writer.provider for d in result.drafts if d.verdict == "sufficient"],
            "refusals": result.refusals,
            "errors": [f"{d.writer.provider}: {d.error}" for d in result.drafts if d.error],
            "judge": result.judge,
        }
        if result.chosen is None:
            response = self._item_response(project_id, item_id, document)
            response["ai_explanation"] = summary
            return response
        candidate = copy.deepcopy(document)
        if mode == "write":
            candidate["content"]["explanation"] = ai_explanation.explanation_section(
                item_id, result.chosen.explanation
            )
            block_ids = [b["block_id"] for b in candidate["content"]["explanation"]["blocks"]]
        else:
            added = ai_explanation.supplement_blocks(item_id, result.chosen.explanation)
            candidate["content"]["explanation"]["blocks"].extend(added)
            block_ids = [b["block_id"] for b in added]
        candidate.setdefault("provenance", {})["ai_explanation"] = ai_explanation.provenance_record(
            result, answer, mode=mode, block_ids=block_ids
        )
        errors = self._package_errors(item_path, candidate)
        if errors:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "AI 해설 검증 실패: " + errors[0])
        saved = save_json_cas(item_path, candidate, expected_revision)
        self._refresh_project_status(project_id)
        summary.update(saved=True, provider=result.chosen.writer.provider, model=result.chosen.model)
        response = self._item_response(project_id, item_id, saved)
        response["ai_explanation"] = summary
        return response

    def _run_queued_explanation(self, project_id: str, item_id: str) -> dict[str, Any]:
        """Queue worker: pick write/supplement from the item's current content, then save via CAS."""
        self.trash.require_active(project_id, item_id)
        document = load_json_object(self._item_path(project_id, item_id))
        revision = document.get("workflow", {}).get("revision")
        content = document.get("content") if isinstance(document.get("content"), Mapping) else {}
        mode = "supplement" if content.get("explanation") is not None else "write"
        if (document.get("provenance") or {}).get("ai_explanation"):
            return {"state": "held", "mode": mode, "message": "이미 AI가 작성하거나 보충한 해설입니다."}
        try:
            response = self.write_ai_explanation(project_id, item_id, revision, mode)
        except RevisionConflict:
            return {"state": "failed", "mode": mode, "message": "AI 작성 중 문항이 수정되어 저장하지 않았습니다. 다시 넣어 주세요."}
        except ApiError as exc:
            return {"state": "failed", "mode": mode, "message": exc.message}
        summary = response.get("ai_explanation") or {}
        if summary.get("saved"):
            return {"state": "saved", "mode": mode, "message": f"{summary.get('provider') or ''} {summary.get('model') or ''}".strip()}
        if summary.get("refusals"):
            return {"state": "held", "mode": mode, "message": "; ".join(summary["refusals"])}
        if summary.get("sufficient"):
            return {"state": "sufficient", "mode": mode, "message": "기존 해설로 충분하다고 판단해 저장하지 않음"}
        return {"state": "failed", "mode": mode, "message": "; ".join(summary.get("errors") or []) or "해설을 만들지 못했습니다."}

    def save_item_tags(
        self,
        project_id: str,
        item_id: str,
        expected_revision: Any,
        supplied_tags: Any,
    ) -> dict[str, Any]:
        if not isinstance(supplied_tags, Mapping):
            raise ApiError(HTTPStatus.BAD_REQUEST, "tags는 객체여야 합니다.")
        item_path = self._item_path(project_id, item_id)
        _, project = self._project(project_id)
        document = load_json_object(item_path)
        try:
            tags = normalize_manual_tags(project, document, supplied_tags)
        except TaggingError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        candidate = copy.deepcopy(document)
        candidate["tags"] = tags
        errors = self._package_errors(item_path, candidate)
        if errors:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "태그 검증 실패: " + errors[0])
        saved = update_tags_cas(item_path, expected_revision, tags)
        return self._item_response(project_id, item_id, saved)

    @staticmethod
    def _package_errors(
        item_path: Path, document: Mapping[str, Any], *, check_files: bool = False
    ) -> list[str]:
        try:
            errors = json_schema_errors(document, PACKAGE_SCHEMA)
        except RuntimeError:
            # The semantic validator remains available for source checkouts;
            # the packaged runtime installs jsonschema from requirements.txt.
            errors = []
        errors.extend(PackageValidator(item_path, check_files=check_files).validate(document))
        return errors

    @classmethod
    def _validation_issues(
        cls,
        item_path: Path,
        document: Mapping[str, Any],
        *,
        check_files: bool = False,
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        for error in cls._package_errors(item_path, document, check_files=check_files):
            path, _, detail = error.partition(":")
            message = (
                "스키마 조건을 충족하지 않습니다."
                if path.startswith("schema.")
                else (detail.strip() or "문항 구조 조건을 충족하지 않습니다.")[:300]
            )
            issues.append(
                {
                    "code": "package_validation",
                    "severity": "error",
                    "path": path,
                    "message": message,
                    "blocking": True,
                }
            )
        issues.extend(evaluate_package(document))
        review = document.get("review")
        stored = review.get("issues") if isinstance(review, Mapping) else None
        known_messages = {
            issue.get("message").strip()
            for issue in issues
            if isinstance(issue.get("message"), str) and issue.get("message").strip()
        }
        if isinstance(stored, list):
            for index, message in enumerate(stored):
                if isinstance(message, str) and message.strip():
                    stored_message = message.strip()[:300]
                    if stored_message in known_messages:
                        continue
                    issues.append(
                        {
                            "code": "manual_review_issue",
                            "severity": "warning",
                            "path": f"$.review.issues[{index}]",
                            "message": stored_message,
                            "blocking": not is_advisory_review_issue(stored_message),
                        }
                    )
                    known_messages.add(stored_message)
        unique: list[dict[str, Any]] = []
        seen = set()
        for issue in issues:
            key = (issue.get("code"), issue.get("path"), issue.get("message"))
            if key not in seen:
                seen.add(key)
                unique.append(issue)
        return unique

    def validate_item(self, project_id: str, item_id: str, package: Any = None) -> dict[str, Any]:
        item_path = self._item_path(project_id, item_id)
        document = package if isinstance(package, Mapping) else load_json_object(item_path)
        issues = self._validation_issues(item_path, document)
        return {
            "ok": not any(issue.get("blocking") is True for issue in issues),
            "issues": issues,
        }

    def transcribe_item(
        self,
        project_id: str,
        item_id: str,
        source_asset_id: Any,
        section: Any,
        profile: Any,
        raw_output: Any = None,
        provider_profile: Any = None,
    ) -> dict[str, Any]:
        item_path = self._item_path(project_id, item_id)
        if not isinstance(source_asset_id, str) or not isinstance(section, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "source_asset_id와 section이 필요합니다.")
        if section not in {"question", "explanation"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "section은 question 또는 explanation이어야 합니다.")
        if profile is not None and not isinstance(profile, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "profile은 문자열이어야 합니다.")
        config = PROVIDER_DEFAULTS.get(profile or "balanced")
        if config is None:
            raise ApiError(HTTPStatus.BAD_REQUEST, "알 수 없는 provider profile입니다.")
        provider_profile = self._project_provider_profile(
            project_id, provider_profile
        )
        config = effective_provider_config(
            provider_profile, str(profile or "balanced")
        )
        package = load_json_object(item_path)
        assets = package.get("resources", {}).get("assets", [])
        allowed_sections = {section, "shared"}
        allowed_ids = {
            asset.get("asset_id")
            for asset in assets
            if isinstance(asset, Mapping)
            and asset.get("role") == "source_crop"
            and asset.get("state") == "ready"
            and asset.get("section") in allowed_sections
        }
        if source_asset_id not in allowed_ids:
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "이 문항·section에 등록된 ready source_crop asset만 전사할 수 있습니다.",
            )
        adapter = None
        if raw_output is not None:
            if not isinstance(raw_output, str) or len(raw_output.encode("utf-8")) > MAX_JSON_BYTES:
                raise ApiError(HTTPStatus.BAD_REQUEST, "raw_output이 올바르지 않습니다.")

            class InlineAdapter:
                provider_name = config.provider

                def transcribe(self, **_: Any) -> str:
                    return raw_output

            adapter = InlineAdapter()
        job = create_transcription_job(
            item_path,
            source_asset_id,
            section,
            adapter=adapter,
            config=config,
            provider_profile=provider_profile,
            use_jsonschema=False,
            fresh=True,
        )
        return {
            "job_id": job.metadata["job_id"],
            "cached": job.cached,
            "metadata": job.metadata,
            "candidate": job.candidate,
        }

    def apply_candidate(
        self,
        project_id: str,
        item_id: str,
        job_id: Any,
        expected_revision: Any,
    ) -> dict[str, Any]:
        item_path = self._item_path(project_id, item_id)
        if not isinstance(job_id, str) or not job_id or "/" in job_id or "\\" in job_id:
            raise ApiError(HTTPStatus.BAD_REQUEST, "job_id가 올바르지 않습니다.")
        job_path = item_path.parent / "jobs" / "transcription" / job_id
        applied = apply_candidate_job(
            item_path,
            job_path,
            expected_base_revision=expected_revision,
            use_jsonschema=False,
            typing_only=True,
        )
        self._refresh_project_status(project_id)
        return self._item_response(project_id, item_id, applied)

    def review_figure_asset(
        self,
        project_id: str,
        item_id: str,
        asset_id: str,
        expected_revision: Any,
        status: Any,
        boundary_uncertain: Any,
        notes: Any,
    ) -> dict[str, Any]:
        item_path = self._item_path(project_id, item_id)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ApiError(HTTPStatus.BAD_REQUEST, "expected_revision은 양의 정수여야 합니다.")
        if status not in {"approved", "rejected", "unreviewed"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "status가 올바르지 않습니다.")
        if not isinstance(boundary_uncertain, bool):
            raise ApiError(HTTPStatus.BAD_REQUEST, "boundary_uncertain은 boolean이어야 합니다.")
        if (
            not isinstance(notes, list)
            or len(notes) > 100
            or any(
                not isinstance(note, str)
                or not note.strip()
                or len(note) > 2000
                for note in notes
            )
        ):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "notes는 2,000자 이하의 비어 있지 않은 문자열 배열이어야 합니다.",
            )
        saved = review_figure_asset_cas(
            item_path,
            expected_revision,
            asset_id,
            status,
            boundary_uncertain,
            notes,
        )
        self._refresh_project_status(project_id)
        return self._item_response(project_id, item_id, saved)

    def select_figure_asset(
        self,
        project_id: str,
        item_id: str,
        block_id: str,
        expected_revision: Any,
        asset_id: Any,
    ) -> dict[str, Any]:
        item_path = self._item_path(project_id, item_id)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ApiError(HTTPStatus.BAD_REQUEST, "expected_revision은 양의 정수여야 합니다.")
        if not isinstance(asset_id, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "asset_id가 필요합니다.")
        try:
            validate_id(block_id, field="block_id")
            validate_id(asset_id, field="asset_id")
        except ModelError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        saved = select_figure_asset_cas(
            item_path, expected_revision, block_id, asset_id
        )
        self._refresh_project_status(project_id)
        return self._item_response(project_id, item_id, saved)

    def recrop_figure(
        self,
        project_id: str,
        item_id: str,
        block_id: str,
        expected_revision: Any,
        bbox: Any,
    ) -> dict[str, Any]:
        item_path = self._item_path(project_id, item_id)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ApiError(HTTPStatus.BAD_REQUEST, "expected_revision은 양의 정수여야 합니다.")
        if not isinstance(bbox, Mapping):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bbox 객체가 필요합니다.")
        try:
            validate_id(block_id, field="block_id")
        except ModelError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        saved, asset_id, created = recrop_figure_cas(
            item_path, expected_revision, block_id, bbox
        )
        self._refresh_project_status(project_id)
        result = self._item_response(project_id, item_id, saved)
        result.update(asset_id=asset_id, created=created)
        return result

    def regenerate_figure(
        self,
        project_id: str,
        item_id: str,
        block_id: str,
        expected_revision: Any,
        profile: Any,
        remove_blue_text: Any,
    ) -> dict[str, Any]:
        if profile is None:
            profile = active_figure_profile()
        if (
            isinstance(profile, str)
            and profile in FIGURE_PROFILES
            and FIGURE_PROFILES[profile].provider != "codex"
            and self._project_provider_profile(project_id) == "codex_only"
        ):
            raise ApiError(HTTPStatus.CONFLICT, "Codex-only projects cannot regenerate figures.")
        if isinstance(profile, str) and profile in FIGURE_PROFILES and not figure_profile_available(profile):
            raise ApiError(HTTPStatus.CONFLICT, GEMINI_FIGURE_GENERATION_DISABLED_MESSAGE)
        item_path = self._item_path(project_id, item_id)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ApiError(HTTPStatus.BAD_REQUEST, "expected_revision은 양의 정수여야 합니다.")
        if not isinstance(profile, str) or profile not in FIGURE_PROFILES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "profile은 codex여야 합니다(Gemini 프로필은 비활성화됨).")
        if not isinstance(remove_blue_text, bool):
            raise ApiError(HTTPStatus.BAD_REQUEST, "remove_blue_text는 boolean이어야 합니다.")
        try:
            validate_id(block_id, field="block_id")
        except ModelError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        saved, asset_id, job, created = regenerate_figure_cas(
            item_path,
            expected_revision,
            block_id,
            profile,
            remove_blue_text,
            adapter=self.figure_adapter,
        )
        self._refresh_project_status(project_id)
        result = self._item_response(project_id, item_id, saved)
        result.update(
            asset_id=asset_id,
            job_id=job.metadata["job_id"],
            created=created,
            cached=job.cached,
        )
        return result

    def asset(self, project_id: str, item_id: str, asset_id: str) -> tuple[Path, str]:
        item_path = self._item_path(project_id, item_id)
        validate_id(asset_id, field="asset_id")
        if asset_id.startswith("solution-page-"):
            pages = source_pages(item_path, project_id, item_id, tuple(self.config.import_roots))["pages"]
            if asset_id not in {page["page_ref"] for page in pages}:
                raise ApiError(HTTPStatus.NOT_FOUND, "원본 페이지를 찾을 수 없습니다.")
            return item_path.parent / "jobs" / "solution_pages" / f"{asset_id}.png", "image/png"
        package = load_json_object(item_path)
        assets = package.get("resources", {}).get("assets", [])
        asset = next(
            (
                value
                for value in assets
                if isinstance(value, Mapping) and value.get("asset_id") == asset_id
            ),
            None,
        )
        if asset is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "asset을 찾을 수 없습니다.")
        relative = asset.get("path")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "asset 경로가 올바르지 않습니다.")
        item_dir = item_path.parent.resolve()
        path = (item_dir / Path(relative.replace("/", "\\"))).resolve()
        if not _is_within(path, item_dir) or not path.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "asset 파일을 찾을 수 없습니다.")
        media_type = asset.get("media_type")
        if media_type not in {"image/png", "image/jpeg", "image/bmp"}:
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "지원하지 않는 asset 형식입니다.")
        return path, media_type

    def archive_project(self, project_id: str) -> dict[str, Any]:
        project_dir, _ = self._project(project_id)
        try:
            archive = create_project_archive(project_dir)
        except ProjectArchiveError as exc:
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "프로젝트 백업 입력의 무결성을 확인할 수 없습니다.",
            ) from exc
        return {
            "archive_id": archive.archive_id,
            "name": archive.name,
            "size": archive.size,
            "sha": archive.sha256,
            "download_url": (
                f"/api/projects/{project_id}/archives/{archive.archive_id}/download"
            ),
            "cached": archive.cached,
        }

    def archive_download(
        self, project_id: str, archive_id: str
    ) -> tuple[Path, str, str]:
        project_dir, _ = self._project(project_id)
        try:
            return resolve_project_archive(project_dir, project_id, archive_id)
        except ProjectArchiveError as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, "백업 파일을 찾을 수 없습니다.") from exc

    def build_hwpx(self, project_id: str) -> dict[str, Any]:
        project_dir, _ = self._project(project_id)

        def public_detail(value: Any) -> str:
            detail = str(value).replace(str(project_dir), "<project>")
            return detail[:2000]

        try:
            build = create_hwpx_build(project_dir)
        except ExportPreflightError as exc:
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "HWPX 생성 전 검증 실패: " + public_detail("; ".join(exc.errors[:20])),
            ) from exc
        except (HwpxArtifactError, HwpxRenderError) as exc:
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "HWPX 생성 실패: " + public_detail(exc),
            ) from exc

        outputs = []
        for artifact in build.outputs:
            receipt_output = next(
                entry
                for entry in build.receipt["outputs"]
                if entry.get("document_kind") == artifact.artifact_kind
            )
            outputs.append(
                {
                    "document_kind": artifact.artifact_kind,
                    "name": artifact.name,
                    "size_bytes": artifact.size,
                    "sha256": artifact.sha256,
                    "validation": receipt_output.get("validation", {}),
                    "download_url": (
                        f"/api/projects/{project_id}/hwpx/builds/"
                        f"{build.build_fingerprint_sha256}/{artifact.artifact_kind}/"
                        f"{artifact.sha256}/download"
                    ),
                }
            )
        receipt_artifact = build.receipt_artifact
        return {
            "status": "built",
            "build_id": build.build_id,
            "build_fingerprint_sha256": build.build_fingerprint_sha256,
            "cached": build.cached,
            "outputs": outputs,
            "receipt": build.receipt,
            "receipt_file": {
                "name": receipt_artifact.name,
                "size_bytes": receipt_artifact.size,
                "sha256": receipt_artifact.sha256,
                "download_url": (
                    f"/api/projects/{project_id}/hwpx/builds/"
                    f"{build.build_fingerprint_sha256}/receipt/"
                    f"{receipt_artifact.sha256}/download"
                ),
            },
        }

    def hwpx_download(
        self,
        project_id: str,
        fingerprint: str,
        artifact_kind: str,
        expected_sha256: str,
    ) -> tuple[Path, str, str, str]:
        project_dir, _ = self._project(project_id)
        try:
            artifact = resolve_hwpx_artifact(
                project_dir,
                project_id,
                fingerprint,
                artifact_kind,
                expected_sha256,
            )
        except HwpxArtifactError as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, "HWPX 산출물을 찾을 수 없습니다.") from exc
        return (
            artifact.path,
            artifact.attachment_name,
            artifact.media_type,
            artifact.sha256,
        )

    def static_file(self, request_path: str) -> tuple[Path, str]:
        name = "index.html" if request_path == "/" else request_path.lstrip("/")
        if "_internal" in Path(name).parts:
            raise ApiError(HTTPStatus.NOT_FOUND, "파일을 찾을 수 없습니다.")
        web_root = self.config.web_root.resolve()
        path = (web_root / name).resolve()
        if not _is_within(path, web_root) or not path.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "파일을 찾을 수 없습니다.")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return path, media_type

    def _project(self, project_id: str) -> tuple[Path, dict[str, Any]]:
        validate_id(project_id, field="project_id")
        project_dir = (self.config.workspace_root / project_id).resolve()
        if not _is_within(project_dir, self.config.workspace_root):
            raise ApiError(HTTPStatus.NOT_FOUND, "프로젝트를 찾을 수 없습니다.")
        project_path = project_dir / "project.json"
        if not project_path.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "프로젝트를 찾을 수 없습니다.")
        project = load_json_object(project_path)
        if project.get("project_id") != project_id:
            raise ApiError(HTTPStatus.CONFLICT, "프로젝트 식별자가 일치하지 않습니다.")
        return project_dir, project

    def _item_path(self, project_id: str, item_id: str) -> Path:
        project_dir, project = self._project(project_id)
        if self.trash.is_deleted(project_id, item_id):
            raise ApiError(HTTPStatus.GONE, "휴지통의 문항입니다. 복원 후 다시 열어 주세요.")
        validate_id(item_id, field="item_id")
        if item_id not in project.get("item_order", []):
            raise ApiError(HTTPStatus.NOT_FOUND, "문항을 찾을 수 없습니다.")
        path = (project_dir / "items" / item_id / "question.json").resolve()
        if not _is_within(path, project_dir) or not path.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "문항을 찾을 수 없습니다.")
        return path

    def update_project_info(self, project_id: str, expected_revision: Any, values: Mapping[str, Any]) -> dict[str, Any]:
        """Change project name/publisher/target/subject/semester with CAS protection."""
        from .project_info import ProjectInfoError, apply_info, normalize_info

        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise ApiError(HTTPStatus.BAD_REQUEST, "expected_revision은 양의 정수여야 합니다.")
        try:
            info = normalize_info(values, require_title=False)
        except ProjectInfoError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        if not info:
            raise ApiError(HTTPStatus.BAD_REQUEST, "바꿀 프로젝트 정보가 없습니다.")
        project_dir, project = self._project(project_id)
        metadata = project.get("metadata")
        if not isinstance(metadata, dict):
            raise ApiError(HTTPStatus.CONFLICT, "프로젝트 정보 형식이 올바르지 않습니다.")
        # Subject/grade used to be guessed from the project name. Freeze the
        # current guess before a rename so tagging/explanations keep working.
        if "title" in info and "target" not in info and not metadata.get("subject"):
            from .tagging import _project_context

            subject, school_level, grade = _project_context(project, {})
            metadata["subject"] = subject
            if not metadata.get("school_level"):
                metadata["school_level"] = school_level
            if not metadata.get("grade") and grade is not None:
                metadata["grade"] = str(grade)
        apply_info(metadata, info)
        try:
            saved = save_json_cas(project_dir / "project.json", project, expected_revision)
        except RevisionConflict as exc:
            raise ApiError(HTTPStatus.CONFLICT, "다른 곳에서 프로젝트가 바뀌었습니다. 새로고침 후 다시 시도하세요.") from exc
        return {
            "project_id": project_id,
            "metadata": saved.get("metadata", {}),
            "workflow": saved.get("workflow", {}),
        }

    def _refresh_project_status(self, project_id: str) -> None:
        # Project status is derived from item JSON. A racing derived-status write
        # must never turn an already committed item mutation into an HTTP failure.
        with self._project_status_lock:
            for _ in range(3):
                project_dir, project = self._project(project_id)
                statuses = []
                for item_id in project.get("item_order", []):
                    item = load_json_object(project_dir / "items" / str(item_id) / "question.json")
                    statuses.append(item.get("workflow", {}).get("status"))
                target = (
                    "content_ready"
                    if statuses and all(status == "content_ready" for status in statuses)
                    else "content_working"
                    if any(status != "source_only" for status in statuses)
                    else "source_only"
                )
                if project.get("workflow", {}).get("status") == target:
                    return
                project["workflow"]["status"] = target
                try:
                    save_json_cas(
                        project_dir / "project.json",
                        project,
                        project["workflow"]["revision"],
                    )
                    return
                except RevisionConflict:
                    continue

    def _item_response(self, project_id: str, item_id: str, package: Mapping[str, Any]) -> dict[str, Any]:
        assets = package.get("resources", {}).get("assets", [])
        asset_urls = {
            asset["asset_id"]: (
                f"/api/projects/{project_id}/items/{item_id}/assets/{asset['asset_id']}"
            )
            for asset in assets
            if isinstance(asset, Mapping) and isinstance(asset.get("asset_id"), str)
        }
        candidates = []
        item_path = self._item_path(project_id, item_id)
        jobs_root = item_path.parent / "jobs" / "transcription"
        if jobs_root.is_dir():
            for job_path in sorted(jobs_root.iterdir(), reverse=True):
                if not job_path.is_dir():
                    continue
                try:
                    job = load_candidate_job(job_path, use_jsonschema=False)
                except ModelError:
                    continue
                candidates.append(
                    {
                        "job_id": job.metadata.get("job_id"),
                        "metadata": job.metadata,
                        "candidate": job.candidate,
                    }
                )
        registration = registration_summary(
            package,
            warning_count=len(self._validation_issues(item_path, package)),
        )
        solution = solution_link_summary(package, item_root=item_path.parent)
        target = self._solution_target_states(project_id).get(item_id)
        if target and target.get("asset_id") == solution["source_asset_id"] and target.get("source_sha256") == solution["source_sha256"]:
            if target.get("verification_state") in {"verified", "review", "mismatch", "failed"}:
                solution["verification_state"] = target["verification_state"]
            if target.get("state") == "failed" and not solution["transcribed"]:
                solution["transcription_state"] = "failed"
            elif target.get("state") in {"pending", "running"} and solution["source_present"] and not solution["transcribed"]:
                solution["transcription_state"] = "running" if target["state"] == "running" else "pending"
        return {
            "package": package,
            "generation_review": self._generation_review_state(package),
            "revision": package.get("workflow", {}).get("revision"),
            "registration": registration,
            "content_status": stored_content_status(package),
            "solution_link": solution,
            "figure_generation": figure_generation_summary(package),
            "warning_count": registration["warning_count"],
            "asset_urls": asset_urls,
            "candidates": candidates,
            "solution_propagation": self.batches.solution_confirmation_capability(
                project_id, item_id
            ),
        }


    def _generation_review_state(self, package: Mapping[str, Any]) -> dict | None:
        from .item_generation_reviews import canonical_review_state
        return canonical_review_state(self.config.workspace_root, package)


class StudioHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    app: StudioApplication

    def server_close(self) -> None:
        if hasattr(self, "app"):
            self.app.batches.close(wait=False)
        lock = getattr(self, "workspace_lock", None)
        if lock is not None:
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
            self.workspace_lock = None
        super().server_close()


class StudioRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "QuestionJSONStudio"
    sys_version = ""

    @property
    def app(self) -> StudioApplication:
        return self.server.app  # type: ignore[attr-defined,no-any-return]

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:
        self._handle("OPTIONS")

    def log_message(self, format: str, *args: Any) -> None:
        # Do not write the token-bearing bootstrap URL to access logs.
        return

    def _handle(self, method: str) -> None:
        try:
            host = self.headers.get("Host")
            self.app.check_host(host)
            self.app.check_origin(self.headers.get("Origin"), host=host)
            parsed = urlsplit(self.path)
            if method == "GET" and parsed.path == "/" and parsed.query:
                query = _single_query_value(parsed.query, "token")
                if self.app.token_matches(query):
                    self._send_redirect_with_session()
                    return
            if parsed.path == "/api/integrations/homework/local/v1" or parsed.path.startswith("/api/integrations/homework/local/v1/"):
                from .homework_local import handle as handle_homework_local
                handle_homework_local(self, method, parsed)
                return
            if parsed.path.startswith("/api/"):
                if not self.app.is_authorized(self.headers, method=method):
                    raise ApiError(HTTPStatus.UNAUTHORIZED, "세션 토큰이 필요합니다.")
                if parsed.path.startswith("/api/exam/"):
                    from .exam_routes import handle_exam_api
                    handle_exam_api(self, method, parsed.path.strip("/").split("/"))
                    return
                if parsed.path.startswith("/api/item-generation/"):
                    from .item_generation_routes import handle_item_generation_api
                    query = {key: values[-1] for key, values in parse_qs(parsed.query).items() if values}
                    handle_item_generation_api(self, method, [unquote(p) for p in parsed.path.strip("/").split("/")], query)
                    return
                if method == "POST" and parsed.path.rstrip("/") == "/api/uploads/pdf":
                    self._handle_api(method, parsed.path)
                    return
                if method in {"POST", "PATCH", "DELETE"}:
                    with self.app.trash.lock:
                        self._handle_api(method, parsed.path)
                else:
                    self._handle_api(method, parsed.path)
                return
            if method != "GET":
                raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "허용되지 않은 메서드입니다.")
            path, media_type = self.app.static_file(parsed.path)
            self._send_file(path, media_type, cache="no-cache")
        except TrashError as exc:
            self._send_json(exc.status, {"error": "trash_error", "message": str(exc)})
        except ApiError as exc:
            self._send_json(exc.status, {"error": HTTPStatus(exc.status).phrase, "message": exc.message})
        except DocumentError as exc:
            self._send_json(exc.status, {"error": exc.code, "message": str(exc)})
        except RevisionConflict as exc:
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "error": "revision_conflict",
                    "message": str(exc),
                    "expected_revision": exc.expected,
                    "current_revision": exc.current,
                },
            )
        except QuestionSetArchiveConflict as exc:
            self._send_json(HTTPStatus.CONFLICT, {"error": "question_set_archive_conflict", "message": str(exc)})
        except ImportConflictError:
            self._send_json(HTTPStatus.CONFLICT, {"error": "import_conflict", "message": "원본이 다른 동일 프로젝트가 이미 있습니다."})
        except BatchError as exc:
            self._send_json(
                exc.status,
                {"error": "batch_error", "message": str(exc)},
            )
        except SolutionSourceError as exc:
            self._send_json(exc.status, {"error": "solution_source_error", "message": str(exc)})
        except ProviderNotConfigured as exc:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "provider_not_configured", "message": str(exc)},
            )
        except ProviderRequestError as exc:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": "provider_request_failed", "message": str(exc)},
            )
        except FigureProviderNotConfigured as exc:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "provider_not_configured", "message": str(exc)},
            )
        except FigureProviderRequestError as exc:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": "provider_request_failed", "message": str(exc)},
            )
        except ModelNotFound as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": str(exc)})
        except ModelError as exc:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "invalid_model", "message": str(exc)})
        except (ConnectionError, BrokenPipeError):
            return
        except Exception:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error", "message": "서버에서 요청을 처리하지 못했습니다."})

    def _handle_api(self, method: str, request_path: str) -> None:
        parts = [unquote(part) for part in request_path.strip("/").split("/")]
        if len(parts) >= 3 and parts[:2] == ["api", "projects"]:
            item_id = parts[4] if len(parts) >= 5 and parts[3] == "items" else None
            self.app.trash.require_active(parts[2], item_id)
        if len(parts) >= 6 and parts[:2] == ["api", "projects"] and parts[3] == "items":
            if method == "GET" and len(parts) == 7 and parts[5:] == ["solution-source", "pages"]:
                self._send_json(HTTPStatus.OK, self.app.item_solution_source_pages(parts[2], parts[4]))
                return
            if method == "POST" and len(parts) == 6 and parts[5] == "solution-images":
                media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                raw_length = self.headers.get("Content-Length")
                if self.headers.get("Transfer-Encoding") or raw_length is None:
                    raise ApiError(HTTPStatus.LENGTH_REQUIRED, "Content-Length가 필요합니다.")
                try:
                    length = int(raw_length)
                except ValueError as exc:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "Content-Length가 올바르지 않습니다.") from exc
                if length < 1 or length > MAX_UPLOAD_BYTES:
                    self.close_connection = True
                    raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "이미지는 25 MiB 이하여야 합니다.")
                self._send_json(HTTPStatus.CREATED, self.app.upload_solution_image(parts[2], parts[4], self.rfile.read(length), media_type, self.headers.get("X-File-Name", "")))
                return
            if method == "POST" and ((len(parts) == 6 and parts[5] == "solution-source") or (len(parts) == 7 and parts[5:] == ["solution-source", "revert"])):
                self._send_json(HTTPStatus.OK if len(parts) == 7 else HTTPStatus.ACCEPTED,
                    self.app.select_item_solution_source(parts[2], parts[4], self._read_json(), revert=len(parts) == 7))
                return
        if parts[:2] == ["api", "documents"]:
            if method == "GET" and len(parts) == 6 and parts[3] == "artifacts" and parts[5] == "download":
                from .document_artifacts import artifact_download

                path, digest, extension = artifact_download(self.app.config.workspace_root, parts[2], parts[4])
                media = "application/hwp+zip" if extension == "hwpx" else "application/zip"
                self._send_file(path, media, cache="no-store", attachment_name=f"document-{parts[4]}.{extension}", expected_sha256=digest)
                return
            if method == "GET" and len(parts) == 6 and parts[3] == "exports" and parts[5] == "download":
                from .document_pdf import pdf_download

                path, digest = pdf_download(self.app.config.workspace_root, parts[2], parts[4])
                self._send_file(path, "application/pdf", cache="no-store", attachment_name=f"document-{parts[4]}.pdf", expected_sha256=digest)
                return
            if method == "GET" and len(parts) == 5 and parts[3] == "assets":
                from .document_read import document_asset

                path, mime, digest = document_asset(self.app.config.workspace_root, parts[2], parts[4])
                self._send_file(path, mime, cache="no-store", expected_sha256=digest)
                return
            body = self._read_json() if method in {"POST", "PATCH"} else {}
            status, result = document_response(self.app.config.workspace_root, DocumentRequest(method, parts[2:], body))
            self._send_json(status, result)
            return
        if method == "GET" and parts == ["api", "health"]:
            self._send_json(HTTPStatus.OK, {"ok": True, "service": "question-json-studio"})
            return
        if method == "GET" and parts == ["api", "recovery", "progress"]:
            self._send_json(
                HTTPStatus.OK, {"progress": self.app.recovery_progress()}
            )
            return
        if method == "GET" and parts == ["api", "config"]:
            self._send_json(HTTPStatus.OK, self.app.public_config())
            return
        if method == "GET" and parts == ["api", "learning", "examples"]:
            self._send_json(HTTPStatus.OK, self.app.list_learning_examples())
            return
        if method == "GET" and parts == ["api", "learning", "status"]:
            self._send_json(
                HTTPStatus.OK, self.app.list_learning_examples()["summary"]
            )
            return
        if (
            method == "POST"
            and len(parts) == 5
            and parts[:3] == ["api", "learning", "examples"]
            and parts[4] == "retract"
        ):
            login = (self.headers.get("Tailscale-User-Login") or "").strip().casefold()
            reviewer_id = login or "local-user"
            self._send_json(
                HTTPStatus.OK,
                self.app.retract_learning_example(
                    parts[3], self._read_json(), reviewer_id
                ),
            )
            return
        if parts == ["api", "trash"] and method == "GET":
            self._send_json(HTTPStatus.OK, self.app.trash.list())
            return
        if len(parts) == 3 and parts[:2] == ["api", "trash"] and method == "POST":
            body = self._read_json()
            action = parts[2]
            if action == "preview":
                result = self.app.trash.preview(body.get("kind"), body.get("project_id"), body.get("item_id"))
            elif action == "move-items-preview":
                result = self.app.trash.preview_items(body.get("items"))
            elif action == "move-items":
                result = self.app.trash.move_items(body)
            elif action == "move-items-status":
                result = self.app.trash.move_items_status(body.get("operation_id"))
            elif action in {"move", "restore"}:
                result = self.app.trash.move_restore(action, body)
            elif action == "purge-preview":
                result = self.app.trash.purge_preview(body.get("kind"), body.get("project_id"), body.get("item_id"))
            elif action == "purge":
                result = self.app.trash.purge(body)
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "휴지통 경로를 찾을 수 없습니다.")
            self._send_json(HTTPStatus.OK, result)
            return
        if method == "GET" and parts == ["api", "projects"]:
            self._send_json(HTTPStatus.OK, {"projects": self.app.list_projects()})
            return
        if method == "GET" and parts == ["api", "bank", "items"]:
            self._send_json(HTTPStatus.OK, {"items": self.app.bank_items()})
            return
        if method == "POST" and parts == ["api", "bank", "figures", "preflight"]:
            self._send_json(
                HTTPStatus.OK,
                self.app.preflight_bank_figures(self._read_json()),
            )
            return
        if method == "POST" and parts == ["api", "bank", "figures", "batches"]:
            self._send_json(
                HTTPStatus.ACCEPTED,
                self.app.start_bank_figure_batch(self._read_json()),
            )
            return
        if parts == ["api", "question-sets"]:
            if method == "GET":
                self._send_json(
                    HTTPStatus.OK, {"question_sets": self.app.question_sets()}
                )
                return
            if method == "POST":
                body = self._read_json()
                self._send_json(
                    HTTPStatus.CREATED,
                    {"question_set": self.app.create_question_set(body)},
                )
                return
        if len(parts) == 3 and parts[:2] == ["api", "question-sets"]:
            if method == "GET":
                self._send_json(
                    HTTPStatus.OK,
                    {"question_set": self.app.get_question_set(parts[2])},
                )
                return
            if method == "PATCH":
                body = self._read_json()
                if "expected_revision" not in body:
                    raise ApiError(
                        HTTPStatus.BAD_REQUEST, "expected_revision이 필요합니다."
                    )
                patch = {key: value for key, value in body.items() if key != "expected_revision"}
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "question_set": self.app.patch_question_set(
                            parts[2], body["expected_revision"], patch
                        )
                    },
                )
                return
            if method == "DELETE":
                body = self._read_json()
                if set(body) != {"expected_revision"}:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "expected_revision만 필요합니다.")
                deleted = self.app.delete_question_set(parts[2], body["expected_revision"])
                self._send_json(HTTPStatus.OK, {"deleted_set_id": deleted["set_id"]})
                return
        if (
            method == "POST"
            and len(parts) == 5
            and parts[:2] == ["api", "question-sets"]
            and parts[3:] == ["hwpx", "build"]
        ):
            body = self._read_json()
            if body:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "문제지 HWPX 생성 요청에는 추가 필드를 사용할 수 없습니다.",
                )
            self._send_json(
                HTTPStatus.OK, self.app.build_question_set_hwpx(parts[2])
            )
            return
        if (
            method == "GET"
            and len(parts) == 9
            and parts[:2] == ["api", "question-sets"]
            and parts[3:5] == ["hwpx", "builds"]
            and parts[8] == "download"
        ):
            path, name, media_type, expected_sha = self.app.question_set_hwpx_download(
                parts[2], parts[5], parts[6], parts[7]
            )
            self._send_file(
                path,
                media_type,
                cache="no-store",
                attachment_name=name,
                expected_sha256=expected_sha,
            )
            return
        if (
            method == "PATCH"
            and len(parts) == 3
            and parts[:2] == ["api", "projects"]
        ):
            body = self._read_json()
            if "expected_revision" not in body:
                raise ApiError(HTTPStatus.BAD_REQUEST, "expected_revision이 필요합니다.")
            values = {key: value for key, value in body.items() if key != "expected_revision"}
            self._send_json(
                HTTPStatus.OK,
                self.app.update_project_info(parts[2], body["expected_revision"], values),
            )
            return
        if (
            method == "POST"
            and len(parts) == 4
            and parts[:2] == ["api", "projects"]
            and parts[3] == "archive"
        ):
            body = self._read_json()
            if body:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "백업 요청에는 추가 필드를 사용할 수 없습니다.",
                )
            self._send_json(HTTPStatus.OK, self.app.archive_project(parts[2]))
            return
        if (
            method == "GET"
            and len(parts) == 6
            and parts[:2] == ["api", "projects"]
            and parts[3] == "archives"
            and parts[5] == "download"
        ):
            path, name, expected_sha = self.app.archive_download(parts[2], parts[4])
            self._send_file(
                path,
                "application/zip",
                cache="no-store",
                attachment_name=name,
                expected_sha256=expected_sha,
            )
            return
        if (
            method == "POST"
            and len(parts) == 5
            and parts[:2] == ["api", "projects"]
            and parts[3:] == ["hwpx", "build"]
        ):
            body = self._read_json()
            if body:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "HWPX 생성 요청에는 추가 필드를 사용할 수 없습니다.",
                )
            self._send_json(HTTPStatus.OK, self.app.build_hwpx(parts[2]))
            return
        if (
            method == "GET"
            and len(parts) == 9
            and parts[:2] == ["api", "projects"]
            and parts[3:5] == ["hwpx", "builds"]
            and parts[8] == "download"
        ):
            path, name, media_type, expected_sha = self.app.hwpx_download(
                parts[2], parts[5], parts[6], parts[7]
            )
            self._send_file(
                path,
                media_type,
                cache="no-store",
                attachment_name=name,
                expected_sha256=expected_sha,
            )
            return
        if parts[:2] == ["api", "ai-explanation-queue"]:
            queue = self.app.explanation_queue
            try:
                if method == "GET" and len(parts) == 2:
                    self._send_json(HTTPStatus.OK, queue.snapshot())
                    return
                if method == "POST" and len(parts) == 2:
                    body = self._read_json()
                    if not ({"project_id", "item_id"} <= set(body) <= {"project_id", "item_id", "label"}):
                        raise ApiError(HTTPStatus.BAD_REQUEST, "대기열 요청은 project_id, item_id, label만 포함할 수 있습니다.")
                    project_id, item_id = str(body["project_id"]), str(body["item_id"])
                    self.app.trash.require_active(project_id, item_id)
                    self.app._item_path(project_id, item_id)
                    self._send_json(HTTPStatus.CREATED, {"entry": queue.enqueue(project_id, item_id, str(body.get("label") or "")), **queue.snapshot()})
                    return
                if method == "POST" and parts[2:] == ["clear"]:
                    queue.clear_finished()
                    self._send_json(HTTPStatus.OK, queue.snapshot())
                    return
                if method == "DELETE" and len(parts) == 3:
                    queue.cancel(parts[2])
                    self._send_json(HTTPStatus.OK, queue.snapshot())
                    return
            except QueueError as exc:
                raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
            except KeyError as exc:
                raise ApiError(HTTPStatus.NOT_FOUND, "대기열 항목을 찾을 수 없습니다.") from exc
            raise ApiError(HTTPStatus.NOT_FOUND, "지원하지 않는 대기열 요청입니다.")
        if method == "GET" and parts == ["api", "batches"]:
            self._send_json(HTTPStatus.OK, {"batches": self.app.batches.list()})
            return
        if method == "POST" and parts == ["api", "uploads", "pdf"]:
            self._send_json(HTTPStatus.CREATED, self._read_pdf_upload())
            return
        if (
            method == "POST"
            and len(parts) == 5
            and parts[:2] == ["api", "projects"]
            and parts[3:] == ["ocr", "missing"]
        ):
            body = self._read_json()
            unexpected = sorted(set(body) - {"profile", "provider_profile"})
            if unexpected:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "미전사 OCR 요청에 허용되지 않은 필드가 있습니다: "
                    + ", ".join(unexpected),
                )
            profile = str(body.get("profile") or "balanced")
            job = self.app.transcribe_project_missing(
                parts[2], profile, body.get("provider_profile")
            )
            self._send_json(HTTPStatus.ACCEPTED, job)
            return
        if method == "POST" and len(parts) == 5 and parts[:2] == ["api", "projects"] and parts[3:] == ["answers", "promote-from-explanation"]:
            body = self._read_json()
            if set(body) - {"dry_run", "item_ids"} or "dry_run" not in body:
                raise ApiError(HTTPStatus.BAD_REQUEST, "dry_run과 선택적인 item_ids만 사용하세요.")
            self._send_json(HTTPStatus.OK, self.app.promote_project_answers(
                parts[2], dry_run=body["dry_run"], item_ids=body.get("item_ids")))
            return
        if method == "POST" and len(parts) == 5 and parts[:2] == ["api", "projects"] and parts[3:] == ["solutions", "transcribe"]:
            body = self._read_json()
            if set(body) - {"item_ids", "answer_backfill_only"}:
                raise ApiError(HTTPStatus.BAD_REQUEST, "해설 전사 요청에는 추가 필드를 사용할 수 없습니다.")
            answer_backfill_only = body.get("answer_backfill_only", False)
            if type(answer_backfill_only) is not bool:
                raise ApiError(HTTPStatus.BAD_REQUEST, "answer_backfill_only는 boolean이어야 합니다.")
            self._send_json(HTTPStatus.ACCEPTED, self.app.transcribe_project_solutions(
                parts[2], body.get("item_ids"), answer_backfill_only=answer_backfill_only))
            return
        if (
            method == "POST"
            and len(parts) == 6
            and parts[:2] == ["api", "projects"]
            and parts[3:] == ["solutions", "relink", "preflight"]
        ):
            body = self._read_json()
            if body:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "답·해설 재연결 사전 확인 요청에는 추가 필드를 사용할 수 없습니다.",
                )
            self._send_json(
                HTTPStatus.OK,
                self.app.preflight_project_solution_relink(parts[2]),
            )
            return
        if (
            method == "POST"
            and len(parts) == 5
            and parts[:2] == ["api", "projects"]
            and parts[3:] == ["solutions", "relink"]
        ):
            body = self._read_json()
            unexpected = sorted(
                set(body) - {"plan_id", "plan_sha256", "confirmed"}
            )
            if unexpected or set(body) != {
                "plan_id",
                "plan_sha256",
                "confirmed",
            }:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "답·해설 재연결 확인 필드가 올바르지 않습니다.",
                )
            self._send_json(
                HTTPStatus.ACCEPTED,
                self.app.start_project_solution_relink(parts[2], body),
            )
            return
        if (
            method == "POST"
            and len(parts) == 5
            and parts[:2] == ["api", "projects"]
            and parts[3:] == ["solutions", "reconcile"]
        ):
            body = self._read_json()
            unexpected = sorted(set(body) - {"profile"})
            if unexpected:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "해설 재판정 요청에 허용되지 않은 필드가 있습니다: "
                    + ", ".join(unexpected),
                )
            self._send_json(
                HTTPStatus.ACCEPTED,
                self.app.reconcile_project_solutions(
                    parts[2], str(body.get("profile") or "balanced")
                ),
            )
            return
        if method == "POST" and parts == ["api", "batches"]:
            body = self._read_json()
            allowed = {
                "project_name",
                "project_info",
                "source_path",
                "solution_path",
                "source_upload_id",
                "solution_upload_id",
                "pages",
                "workers",
                "crop_dpi",
                "ocr_profile",
                "regenerate_figures",
                "figure_profile",
                "figure_budget_usd",
                "remove_blue_text",
                "provider_profile",
            }
            unexpected = sorted(set(body) - allowed)
            if unexpected:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "일괄 요청에 허용되지 않은 필드가 있습니다: " + ", ".join(unexpected),
                )
            validate_figure_options(body)
            self._send_json(HTTPStatus.ACCEPTED, self.app.create_batch(body))
            return
        if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "batches"] and parts[3] == "progress":
            self._send_json(HTTPStatus.OK, {"progress_detail": self.app.batches.progress_detail(parts[2])})
            return
        if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "batches"]:
            self._send_json(HTTPStatus.OK, self.app.batches.get(parts[2]))
            return
        if (
            method == "POST"
            and len(parts) == 4
            and parts[:2] == ["api", "batches"]
            and parts[3] == "retry-figures"
        ):
            body = self._read_json()
            if body:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "그림 재시도 요청에는 추가 필드를 사용할 수 없습니다.",
                )
            self.app.require_batch_active(parts[2])
            self._send_json(
                HTTPStatus.ACCEPTED, self.app.batches.retry_figures(parts[2])
            )
            return
        if (
            method == "POST"
            and len(parts) == 4
            and parts[:2] == ["api", "batches"]
            and parts[3] == "retry"
        ):
            body = self._read_json()
            if body:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "재시도 요청에는 추가 필드를 사용할 수 없습니다.",
                )
            self.app.require_batch_active(parts[2])
            self._send_json(
                HTTPStatus.ACCEPTED, self.app.batches.retry(parts[2])
            )
            return
        if (
            method == "POST"
            and len(parts) == 4
            and parts[:2] == ["api", "batches"]
            and parts[3] in {"cancel", "resume"}
        ):
            body = self._read_json()
            if body:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "작업 제어 요청에는 추가 필드를 사용할 수 없습니다.",
                )
            if parts[3] == "resume":
                self.app.require_batch_active(parts[2])
            action = self.app.batches.cancel if parts[3] == "cancel" else self.app.batches.resume
            self._send_json(HTTPStatus.ACCEPTED, action(parts[2]))
            return
        if (
            method == "POST"
            and len(parts) == 8
            and parts[:2] == ["api", "projects"]
            and parts[3] == "items"
            and parts[5] == "figures"
            and parts[7] == "regenerate"
        ):
            body = self._read_json()
            expected_fields = {"expected_revision", "profile", "remove_blue_text"}
            if set(body) != expected_fields:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "그림 재생성 요청은 expected_revision, profile, remove_blue_text만 포함해야 합니다.",
                )
            self._send_json(
                HTTPStatus.OK,
                self.app.regenerate_figure(
                    parts[2],
                    parts[4],
                    parts[6],
                    body["expected_revision"],
                    body["profile"],
                    body["remove_blue_text"],
                ),
            )
            return
        if method == "POST" and parts == ["api", "projects", "import"]:
            body = self._read_json()
            self._send_json(HTTPStatus.CREATED, self.app.import_project(body.get("source_path")))
            return
        if (
            method == "POST"
            and len(parts) == 8
            and parts[:2] == ["api", "projects"]
            and parts[3] == "items"
            and parts[5:] == ["solutions", "propagation", "preview"]
        ):
            body = self._read_json()
            if set(body) != {
                "expected_revision",
                "source_asset_id",
                "propagate_scope",
            }:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "범위 재확인 미리 보기 필드가 올바르지 않습니다.",
                )
            self._send_json(
                HTTPStatus.OK,
                self.app.preview_item_solution_propagation(
                    parts[2], parts[4], body
                ),
            )
            return
        if (
            method == "POST"
            and len(parts) == 8
            and parts[:2] == ["api", "projects"]
            and parts[3] == "items"
            and parts[5:] == ["solutions", "propagation", "apply"]
        ):
            body = self._read_json()
            if set(body) != {"preview_id", "preview_sha256", "confirmed"}:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "범위 재확인 적용 필드가 올바르지 않습니다.",
                )
            self._send_json(
                HTTPStatus.OK,
                self.app.apply_item_solution_propagation(
                    parts[2], parts[4], body
                ),
            )
            return
        if (
            method == "POST"
            and len(parts) == 7
            and parts[:2] == ["api", "projects"]
            and parts[3] == "items"
            and parts[5] == "solution-feedback"
            and parts[6] in {"preflight", "apply"}
        ):
            if not self.app.is_authorized(self.headers, method=method):
                raise ApiError(HTTPStatus.UNAUTHORIZED, "세션 토큰이 필요합니다.")
            login = (self.headers.get("Tailscale-User-Login") or "").strip().casefold()
            reviewer_id = (
                login or "local-user"
                if self.app.config.review_actor_kind == "human"
                else self.app.config.review_actor_kind
            )
            body = self._read_json()
            action = (
                self.app.preflight_solution_feedback
                if parts[6] == "preflight"
                else self.app.apply_solution_feedback
            )
            self._send_json(HTTPStatus.OK, action(parts[2], parts[4], body, reviewer_id))
            return
        if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "items":
            if method == "GET":
                self._send_json(HTTPStatus.OK, {"items": self.app.list_items(parts[2])})
                return
        if len(parts) == 5 and parts[:2] == ["api", "projects"] and parts[3] == "items":
            project_id, item_id = parts[2], parts[4]
            if method == "GET":
                self._send_json(HTTPStatus.OK, self.app.get_item(project_id, item_id))
                return
            if method == "PATCH":
                body = self._read_json()
                self._send_json(
                    HTTPStatus.OK,
                    self.app.save_item(
                        project_id,
                        item_id,
                        body.get("expected_revision"),
                        body.get("package"),
                    ),
                )
                return
        if len(parts) == 6 and parts[:2] == ["api", "projects"] and parts[3] == "items" and parts[5] == "approve":
            if method == "POST":
                body = self._read_json()
                self._send_json(
                    HTTPStatus.OK,
                    self.app.approve_item(
                        parts[2], parts[4], body.get("expected_revision"), body.get("reviewer")
                    ),
                )
                return
        if len(parts) == 6 and parts[:2] == ["api", "projects"] and parts[3] == "items" and parts[5] == "tag":
            if method == "POST":
                body = self._read_json()
                if set(body) != {"expected_revision"}:
                    raise ApiError(
                        HTTPStatus.BAD_REQUEST,
                        "자동 태깅 요청은 expected_revision만 포함해야 합니다.",
                    )
                self._send_json(
                    HTTPStatus.OK,
                    self.app.auto_tag_item(parts[2], parts[4], body["expected_revision"]),
                )
                return
        if len(parts) == 6 and parts[:2] == ["api", "projects"] and parts[3] == "items" and parts[5] == "ai-explanation":
            if method == "POST":
                body = self._read_json()
                if not ({"expected_revision"} <= set(body) <= {"expected_revision", "mode"}):
                    raise ApiError(
                        HTTPStatus.BAD_REQUEST,
                        "AI 해설 요청은 expected_revision과 mode만 포함할 수 있습니다.",
                    )
                self._send_json(
                    HTTPStatus.OK,
                    self.app.write_ai_explanation(
                        parts[2], parts[4], body["expected_revision"], body.get("mode", "write")
                    ),
                )
                return
        if len(parts) == 6 and parts[:2] == ["api", "projects"] and parts[3] == "items" and parts[5] == "tags":
            if method == "PATCH":
                body = self._read_json()
                if set(body) != {"expected_revision", "tags"}:
                    raise ApiError(
                        HTTPStatus.BAD_REQUEST,
                        "태그 저장 요청은 expected_revision과 tags만 포함해야 합니다.",
                    )
                self._send_json(
                    HTTPStatus.OK,
                    self.app.save_item_tags(
                        parts[2], parts[4], body["expected_revision"], body["tags"]
                    ),
                )
                return
        if len(parts) == 6 and parts[:2] == ["api", "projects"] and parts[3] == "items" and parts[5] == "validate":
            if method == "POST":
                body = self._read_json()
                self._send_json(
                    HTTPStatus.OK,
                    self.app.validate_item(parts[2], parts[4], body.get("package")),
                )
                return
        if len(parts) == 6 and parts[:2] == ["api", "projects"] and parts[3] == "items" and parts[5] == "transcribe":
            if method == "POST":
                body = self._read_json()
                unexpected = sorted(
                    set(body)
                    - {
                        "source_asset_id",
                        "section",
                        "profile",
                        "raw_output",
                        "provider_profile",
                    }
                )
                if unexpected:
                    raise ApiError(
                        HTTPStatus.BAD_REQUEST,
                        "전사 요청에 허용되지 않은 설정 필드가 있습니다: "
                        + ", ".join(unexpected),
                    )
                self._send_json(
                    HTTPStatus.CREATED,
                    self.app.transcribe_item(
                        parts[2],
                        parts[4],
                        body.get("source_asset_id"),
                        body.get("section"),
                        body.get("profile"),
                        body.get("raw_output"),
                        body.get("provider_profile"),
                    ),
                )
                return
        if (
            len(parts) == 8
            and parts[:2] == ["api", "projects"]
            and parts[3] == "items"
            and parts[5] == "candidates"
            and parts[7] == "apply"
        ):
            if method == "POST":
                body = self._read_json()
                self._send_json(
                    HTTPStatus.OK,
                    self.app.apply_candidate(
                        parts[2], parts[4], parts[6], body.get("expected_revision")
                    ),
                )
                return
        if (
            method == "POST"
            and len(parts) == 8
            and parts[:2] == ["api", "projects"]
            and parts[3] == "items"
            and parts[5] == "assets"
            and parts[7] == "review"
        ):
            body = self._read_json()
            unexpected = sorted(
                set(body)
                - {"expected_revision", "status", "boundary_uncertain", "notes"}
            )
            if unexpected:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "asset 검수 요청에 허용되지 않은 필드가 있습니다: "
                    + ", ".join(unexpected),
                )
            self._send_json(
                HTTPStatus.OK,
                self.app.review_figure_asset(
                    parts[2],
                    parts[4],
                    parts[6],
                    body.get("expected_revision"),
                    body.get("status"),
                    body.get("boundary_uncertain"),
                    body.get("notes"),
                ),
            )
            return
        if (
            method == "POST"
            and len(parts) == 8
            and parts[:2] == ["api", "projects"]
            and parts[3] == "items"
            and parts[5] == "figures"
            and parts[7] == "select"
        ):
            body = self._read_json()
            unexpected = sorted(set(body) - {"expected_revision", "asset_id"})
            if unexpected:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "그림 선택 요청에 허용되지 않은 필드가 있습니다: "
                    + ", ".join(unexpected),
                )
            self._send_json(
                HTTPStatus.OK,
                self.app.select_figure_asset(
                    parts[2],
                    parts[4],
                    parts[6],
                    body.get("expected_revision"),
                    body.get("asset_id"),
                ),
            )
            return
        if (
            method == "POST"
            and len(parts) == 8
            and parts[:2] == ["api", "projects"]
            and parts[3] == "items"
            and parts[5] == "figures"
            and parts[7] == "recrop"
        ):
            body = self._read_json()
            unexpected = sorted(set(body) - {"expected_revision", "bbox"})
            if unexpected:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "그림 재크롭 요청에 허용되지 않은 필드가 있습니다: "
                    + ", ".join(unexpected),
                )
            self._send_json(
                HTTPStatus.OK,
                self.app.recrop_figure(
                    parts[2],
                    parts[4],
                    parts[6],
                    body.get("expected_revision"),
                    body.get("bbox"),
                ),
            )
            return
        if len(parts) == 7 and parts[:2] == ["api", "projects"] and parts[3] == "items" and parts[5] == "assets":
            if method == "GET":
                path, media_type = self.app.asset(parts[2], parts[4], parts[6])
                self._send_file(path, media_type, cache="no-store")
                return
        raise ApiError(HTTPStatus.NOT_FOUND, "API 경로를 찾을 수 없습니다.")

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        if content_type != "application/json":
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type은 application/json이어야 합니다.")
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(HTTPStatus.LENGTH_REQUIRED, "Content-Length가 필요합니다.")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ApiError(HTTPStatus.LENGTH_REQUIRED, "Content-Length가 필요합니다.")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Content-Length가 올바르지 않습니다.") from exc
        if length < 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Content-Length가 올바르지 않습니다.")
        if length > MAX_JSON_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "JSON 요청이 너무 큽니다.")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "올바른 UTF-8 JSON 객체가 아닙니다.") from exc
        if not isinstance(value, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSON 루트는 객체여야 합니다.")
        return value

    def _read_pdf_upload(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        if content_type != "application/pdf":
            self.close_connection = True
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "PDF Content-Type이 필요합니다.")
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise ApiError(HTTPStatus.LENGTH_REQUIRED, "Content-Length가 필요합니다.")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self.close_connection = True
            raise ApiError(HTTPStatus.LENGTH_REQUIRED, "Content-Length가 필요합니다.")
        try:
            length = int(raw_length)
        except ValueError as exc:
            self.close_connection = True
            raise ApiError(HTTPStatus.BAD_REQUEST, "Content-Length가 올바르지 않습니다.") from exc
        if length > MAX_PDF_UPLOAD_BYTES:
            self.close_connection = True
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "PDF는 512 MiB 이하여야 합니다.")
        encoded_name = self.headers.get("X-Upload-Name", "")
        if len(encoded_name) > 1024 or "\x00" in encoded_name:
            self.close_connection = True
            raise ApiError(HTTPStatus.BAD_REQUEST, "PDF 파일명이 올바르지 않습니다.")
        try:
            original_name = unquote(encoded_name, encoding="utf-8", errors="strict")
        except (UnicodeError, ValueError) as exc:
            self.close_connection = True
            raise ApiError(HTTPStatus.BAD_REQUEST, "PDF 파일명이 올바르지 않습니다.") from exc
        previous_timeout = self.connection.gettimeout()
        self.connection.settimeout(PDF_UPLOAD_IDLE_TIMEOUT_SECONDS)
        try:
            return self.app.store_pdf_upload(
                self.rfile,
                content_length=length,
                original_name=original_name or "upload.pdf",
            )
        except ApiError:
            self.close_connection = True
            raise
        finally:
            self.connection.settimeout(previous_timeout)

    def _send_redirect_with_session(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        secure = (
            "; Secure"
            if (self.headers.get("Host") or "").strip().casefold()
            in self.app.secure_trusted_hosts
            else ""
        )
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={self.app.config.token}; HttpOnly; SameSite=Strict; Max-Age=2592000; Path=/{secure}",
        )
        self._security_headers(cache="no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        compressed = len(data) > 262144 and "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        if compressed:
            data = gzip.compress(data, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self._security_headers(cache="no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_file(
        self,
        path: Path,
        media_type: str,
        *,
        cache: str,
        attachment_name: str | None = None,
        expected_sha256: str | None = None,
    ) -> None:
        if attachment_name is not None and not re.fullmatch(
            r"[A-Za-z0-9._-]+", attachment_name
        ):
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "다운로드 이름이 올바르지 않습니다.")
        if expected_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "다운로드 SHA가 올바르지 않습니다.")
        try:
            stream = path.open("rb")
        except OSError as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, "파일을 찾을 수 없습니다.") from exc
        with stream:
            size = os.fstat(stream.fileno()).st_size
            if expected_sha256 is not None:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                if digest.hexdigest() != expected_sha256:
                    raise ApiError(HTTPStatus.NOT_FOUND, "다운로드 파일 무결성이 일치하지 않습니다.")
                stream.seek(0)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", media_type)
            if attachment_name is not None:
                self.send_header("Content-Disposition", f'attachment; filename="{attachment_name}"')
            self._security_headers(cache=cache)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                self.wfile.write(chunk)

    def _security_headers(self, *, cache: str) -> None:
        self.send_header("Cache-Control", cache)
        policy = "default-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        if self.path.split('?', 1)[0] == '/document.html':
            policy += "; style-src 'self' 'unsafe-inline'"
        self.send_header("Content-Security-Policy", policy)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")


def _single_query_value(query: str, key: str) -> str | None:
    values = []
    for pair in query.split("&"):
        name, separator, value = pair.partition("=")
        if unquote(name) == key:
            if not separator:
                value = ""
            values.append(unquote(value.replace("+", " ")))
    return values[0] if len(values) == 1 else None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def create_server(
    *,
    workspace_root: Path | str,
    import_roots: list[Path | str] | tuple[Path | str, ...],
    pdf_roots: list[Path | str] | tuple[Path | str, ...] | None = None,
    crop_root: Path | str = ASTRA.crop_root,
    recovery_root: Path | str | None = None,
    web_root: Path | str | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str | None = None,
    trusted_origins: list[str] | tuple[str, ...] | None = None,
    tailnet_owner_login: str | None = None,
    figure_adapter: FigureGenerationAdapter | None = None,
    tagging_adapter: TaggingAdapter | None = None,
    explanation_runner: "ai_explanation.Runner | None" = None,
    review_actor_kind: str = "human",
    feedback_sync: Callable[[FeedbackSyncRequest], None] | None = None,
    learning_root: Path | str | None = None,
) -> StudioHTTPServer:
    if host != DEFAULT_HOST:
        raise ValueError("Question JSON Studio는 127.0.0.1에만 바인딩할 수 있습니다.")
    if port != 0:
        port_number(port, "port")
    session_token = secrets.token_urlsafe(32) if token is None else token
    if len(session_token) > 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", session_token):
        raise ValueError("session token은 URL-safe 문자열이어야 합니다.")
    normalized_origins = tuple(
        dict.fromkeys(
            _normalize_origin(origin, allow_tailnet_http=True)
            for origin in (trusted_origins or ())
        )
    )
    if tailnet_owner_login is not None:
        tailnet_owner_login = tailnet_owner_login.strip().casefold()
        if not tailnet_owner_login or len(tailnet_owner_login) > 320 or any(
            character.isspace() for character in tailnet_owner_login
        ):
            raise ValueError("Tailscale owner login이 올바르지 않습니다.")
    if review_actor_kind not in FEEDBACK_ACTOR_KINDS:
        raise ValueError("solution feedback actor kind is invalid")
    roots = tuple(inside(root, "import_root") for root in import_roots)
    if not roots:
        raise ValueError("import root가 하나 이상 필요합니다.")
    allowed_pdf_roots = tuple(
        inside(root, "pdf_root") for root in (pdf_roots or roots)
    )
    if not allowed_pdf_roots:
        raise ValueError("PDF root가 하나 이상 필요합니다.")
    configured_crop_root = inside(crop_root, "crop_root")
    configured_recovery_root = (
        inside(recovery_root, "recovery_root")
        if recovery_root is not None
        else configured_crop_root / "runs" / "recovery"
    )
    configured_workspace = inside(workspace_root, "workspace_root")
    configured_web = inside(web_root or ASTRA.studio_root / "web", "web_root")
    configured_learning_root = inside(learning_root or ASTRA.learning_root, "learning_root")
    effective_feedback_sync = (
        feedback_sync
        if feedback_sync is not None
        else StudioLearningSync(
            learning_root=configured_learning_root,
            import_roots=roots,
            allowed_source_roots=tuple(dict.fromkeys((*roots, *allowed_pdf_roots, configured_workspace))),
        )
    )
    learning_library = LearningLibrary(
        configured_learning_root,
        tuple(dict.fromkeys((*roots, *allowed_pdf_roots, configured_workspace))),
    )
    lock_path = configured_workspace / "_internal" / "studio.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    workspace_lock = lock_path.open("a+b")
    try:
        workspace_lock.seek(0)
        workspace_lock.write(b"\0")
        workspace_lock.flush()
        workspace_lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(workspace_lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(workspace_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        workspace_lock.close()
        raise ValueError("같은 workspace를 사용하는 Studio 서버가 이미 실행 중입니다.") from exc
    try:
        server = StudioHTTPServer((host, port), StudioRequestHandler)
        server.workspace_lock = workspace_lock
        actual_port = int(server.server_address[1])
        server.app = StudioApplication(
        AppConfig(
            workspace_root=configured_workspace,
            web_root=configured_web,
            import_roots=roots,
            pdf_roots=allowed_pdf_roots,
            crop_root=configured_crop_root,
            recovery_root=configured_recovery_root,
            token=session_token,
            host=host,
            port=actual_port,
            trusted_origins=normalized_origins,
            tailnet_owner_login=tailnet_owner_login,
            review_actor_kind=review_actor_kind,
        ),
        figure_adapter=figure_adapter,
        tagging_adapter=tagging_adapter,
        explanation_runner=explanation_runner,
        feedback_sync=effective_feedback_sync,
        learning_library=learning_library,
        )
    except BaseException:
        if "server" in locals():
            server.server_close()
        else:
            workspace_lock.close()
        raise
    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    project_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--host", default=DEFAULT_HOST, choices=[DEFAULT_HOST])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workspace", type=Path, default=ASTRA.workspace)
    parser.add_argument("--web-root", type=Path, default=project_root / "web")
    parser.add_argument("--import-root", action="append", type=Path, dest="import_roots")
    parser.add_argument("--pdf-root", action="append", type=Path, dest="pdf_roots")
    parser.add_argument("--crop-root", type=Path, default=ASTRA.crop_root)
    parser.add_argument("--recovery-root", type=Path)
    parser.add_argument("--token", help=argparse.SUPPRESS)
    parser.add_argument("--trusted-origin", action="append", dest="trusted_origins")
    parser.add_argument("--tailscale-owner-login", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_parsed_paths(args)
    roots = args.import_roots or [ASTRA.crop_root]
    server = create_server(
        workspace_root=args.workspace,
        import_roots=roots,
        pdf_roots=args.pdf_roots,
        crop_root=args.crop_root,
        recovery_root=args.recovery_root,
        web_root=args.web_root,
        host=args.host,
        port=args.port,
        token=args.token,
        trusted_origins=args.trusted_origins,
        tailnet_owner_login=args.tailscale_owner_login,
    )
    url = f"http://{args.host}:{server.server_address[1]}/?token={server.app.config.token}"
    print(f"Question JSON Studio: {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
