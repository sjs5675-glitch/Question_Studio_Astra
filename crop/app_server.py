from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astra_runtime import bootstrap, inside, validate_parsed_paths
from process_control import OwnedProcess
import recovery_receipt
from astra_page_failure import parse_page_failure, page_failure_message

ASTRA = bootstrap()
from urllib.parse import parse_qs, quote, unquote, urlparse


ROOT = Path(__file__).resolve().parent
PYTHON = ASTRA.python
PIPELINE = ROOT / "question_crop.py"
ENSEMBLE = ROOT / "ensemble.py"
SOLUTION_LINK = ROOT / "solution_link.py"
DATA_ROOT = ASTRA.crop_root / "app_data"
UPLOAD_ROOT = DATA_ROOT / "uploads"
JOB_ROOT = ASTRA.crop_root / "runs" / "app"
ALLOWED_ORIGINS = {f"http://localhost:{ASTRA.web_port}", f"http://127.0.0.1:{ASTRA.web_port}"}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
CONFIRMATION_PLAN_TTL_SECONDS = 30 * 60
CONFIRMATION_LOCK_STALE_SECONDS = 60
CONFIRMATION_EFFECT_RESULTS = ("manual_verified", "auto_verified", "review")
CONFIRMATION_EVENT_FIELDS = {
    "plan_id",
    "plan_sha256",
    "confirmation_ids",
    "source_link_manifest_sha256",
    "selection_sha256",
    "effects",
    "counts",
    "effects_sha256",
    "counts_sha256",
    "response_sha256",
    "provider_calls",
    "applied_at",
    "output_payload_sha256",
    "job_sha256",
    "document_sha256",
    "run_sha256",
    "receipt_sha256",
}


class ConfirmationConflict(RuntimeError):
    """The confirmation preview is unavailable or no longer current."""


class ProviderProfileError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_name(value: str, default: str = "upload.pdf") -> str:
    value = Path(value).name
    value = re.sub(r"[^\w가-힣.()\- ]+", "_", value, flags=re.UNICODE).strip(" .")
    return (value or default)[:180]


def write_json(path: Path, data: object) -> None:
    inside(path.resolve(), "output")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def crop_dpi(value: object) -> int:
    dpi = int(value or 300)
    if dpi not in {300, 450}:
        raise ValueError("출력 해상도는 300 또는 450 DPI만 사용할 수 있습니다.")
    return dpi


def provider_profile(value: str | None) -> str:
    if value is None:
        return "full"
    if type(value) is not str or value not in {"full", "codex_only"}:
        raise ProviderProfileError("provider_profile must be full or codex_only")
    return value


def pdf_path(value: object, *, required: bool = False) -> Path | None:
    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError("정답과해설 PDF를 선택해주세요.")
        return None
    path = Path(text).expanduser()
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError("정답과해설 PDF 파일을 찾을 수 없습니다.")
    return path.resolve()


def codex_auth_status() -> dict[str, object]:
    from astra_cli.isolation import auth_status
    status = auth_status()
    return {**status, "label": "Astra CLI", "detail": "구독 로그인 확인됨 (추론 상태 별도)" if status["authenticated"] else status.get("error_code", "auth_missing")}


def build_pipeline_command(source: str, output_root: str, options: dict[str, object]) -> list[str]:
    return [
        str(PYTHON), "-B", "-u", str(PIPELINE), "run", source,
        "--out", output_root, "--pages", str(options["pages"]),
        "--workers", str(options["workers"]), "--model", "gpt-6-astra",
        "--reasoning-effort", "medium", "--context-pages", "auto",
        "--unit", str(options["unit"]), "--answer-space", str(options["answer_space"]),
        "--sidecars", str(options["sidecars"]), "--solution-mask", "none",
        "--crop-dpi", str(options.get("crop_dpi", 300)),
        "--repair-attempts", "1", "--verify", str(options["verify"]),
        "--exclude-regex", r"SUPER\s*GWATAM",
    ]


def build_solution_command(
    run_dir: Path,
    solution_source: Path,
    *,
    crop_dpi_value: int,
    workers: int,
    provider_profile: str = "full",
    force: bool = False,
) -> list[str]:
    command = [
        str(PYTHON), "-B", "-u", str(SOLUTION_LINK), "link",
        "--run-dir", str(run_dir), "--solution", str(solution_source),
        "--crop-dpi", str(crop_dpi_value), "--workers", str(workers),
        "--model", "gpt-6-astra",
        "--provider-profile", provider_profile,
    ]
    if force:
        command.append("--force")
    return command


def _json_clone(value: object) -> object:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256(value: object, field: str) -> str:
    text = str(value or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return text


def _canonical_descendant(path: Path, root: Path, field: str) -> tuple[Path, Path]:
    resolved_root = root.expanduser().resolve()
    resolved_path = path.expanduser().resolve()
    try:
        relative = resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ConfirmationConflict(f"{field} is outside the job output root") from exc
    if not relative.parts:
        raise ConfirmationConflict(f"{field} must be below the job output root")
    return resolved_path, relative


def _confirmation_response_sha256(response: dict) -> str:
    import phase3_fixed_edges as p3

    return p3.canonical_sha256(
        {
            "scope": response["scope"],
            "manual_item": response["manual_item"],
            "effects": response["effects"],
            "counts": response["counts"],
            "provider_calls": response["provider_calls"],
        }
    )


def _validate_confirmation_events(manifest: dict) -> None:
    """Validate the append-only receipt log more strictly than schema v1."""

    import phase3_fixed_edges as p3

    events = manifest.get("manual_confirmation_events")
    constraints = manifest.get("manual_constraints")
    if not isinstance(events, list) or not isinstance(constraints, list):
        raise ConfirmationConflict("manual propagation audit fields are invalid")
    constraint_ids = {
        value.get("confirmation_id")
        for value in constraints
        if isinstance(value, dict) and isinstance(value.get("confirmation_id"), str)
    }
    seen_plans: set[str] = set()
    seen_confirmations: set[str] = set()
    for event in events:
        if not isinstance(event, dict) or set(event) != CONFIRMATION_EVENT_FIELDS:
            raise ConfirmationConflict("manual confirmation event schema is invalid")
        plan_id = event.get("plan_id")
        confirmation_ids = event.get("confirmation_ids")
        if (
            not isinstance(plan_id, str)
            or re.fullmatch(r"mcp-[0-9a-f]{32}", plan_id) is None
            or plan_id in seen_plans
            or not isinstance(confirmation_ids, list)
            or not confirmation_ids
            or len(confirmation_ids) != len(set(confirmation_ids))
            or any(
                not isinstance(value, str)
                or re.fullmatch(r"mc-[0-9a-f]{32}", value) is None
                for value in confirmation_ids
            )
            or not set(confirmation_ids).issubset(constraint_ids)
            or seen_confirmations.intersection(confirmation_ids)
        ):
            raise ConfirmationConflict("manual confirmation event identity is invalid")
        seen_plans.add(plan_id)
        seen_confirmations.update(confirmation_ids)
        for field in (
            "plan_sha256",
            "source_link_manifest_sha256",
            "selection_sha256",
            "effects_sha256",
            "counts_sha256",
            "response_sha256",
            "output_payload_sha256",
            "job_sha256",
            "document_sha256",
            "run_sha256",
            "receipt_sha256",
        ):
            try:
                _require_sha256(event.get(field), field)
            except ValueError as exc:
                raise ConfirmationConflict("manual confirmation event digest is invalid") from exc
        effects = event.get("effects")
        counts = event.get("counts")
        if not isinstance(effects, list) or not isinstance(counts, dict) or set(counts) != set(
            CONFIRMATION_EFFECT_RESULTS
        ):
            raise ConfirmationConflict("manual confirmation event delta is invalid")
        effect_keys: set[tuple[str, str]] = set()
        for effect in effects:
            if not isinstance(effect, dict) or set(effect) != {
                "unit_id",
                "role",
                "result",
                "reason",
                "asset_id",
                "before_sha256",
                "after_sha256",
            }:
                raise ConfirmationConflict("manual confirmation effect schema is invalid")
            if (
                not isinstance(effect.get("unit_id"), str)
                or not effect["unit_id"]
                or effect.get("role") not in {p3.ROLE_QUICK, p3.ROLE_DETAIL}
                or effect.get("result") not in CONFIRMATION_EFFECT_RESULTS
                or not isinstance(effect.get("reason"), str)
                or not effect["reason"]
                or (
                    effect.get("asset_id") is not None
                    and (
                        not isinstance(effect["asset_id"], str)
                        or not effect["asset_id"]
                    )
                )
            ):
                raise ConfirmationConflict("manual confirmation effect value is invalid")
            effect_key = (effect["unit_id"], effect["role"])
            if effect_key in effect_keys:
                raise ConfirmationConflict(
                    "manual confirmation event has duplicate unit-role effects"
                )
            effect_keys.add(effect_key)
            for field in ("before_sha256", "after_sha256"):
                try:
                    _require_sha256(effect.get(field), field)
                except ValueError as exc:
                    raise ConfirmationConflict("manual confirmation effect digest is invalid") from exc
        expected_counts = {
            result: sum(effect["result"] == result for effect in effects)
            for result in CONFIRMATION_EFFECT_RESULTS
        }
        if (
            any(type(value) is not int or value < 0 for value in counts.values())
            or counts != expected_counts
            or event.get("provider_calls") != 0
            or event.get("effects_sha256") != p3.canonical_sha256(effects)
            or event.get("counts_sha256") != p3.canonical_sha256(counts)
        ):
            raise ConfirmationConflict("manual confirmation event delta digest is invalid")
        try:
            applied_at = datetime.fromisoformat(str(event.get("applied_at") or ""))
        except ValueError as exc:
            raise ConfirmationConflict("manual confirmation event timestamp is invalid") from exc
        if applied_at.tzinfo is None:
            raise ConfirmationConflict("manual confirmation event timestamp is invalid")
        receipt_payload = {
            key: value for key, value in event.items() if key != "receipt_sha256"
        }
        if event["receipt_sha256"] != p3.canonical_sha256(receipt_payload):
            raise ConfirmationConflict("manual confirmation receipt was tampered with")
    if events:
        prior = _json_clone(manifest)
        assert isinstance(prior, dict)
        prior["manual_confirmation_events"] = _json_clone(events[:-1])
        if p3.canonical_sha256(prior) != events[-1]["output_payload_sha256"]:
            raise ConfirmationConflict("latest manual confirmation receipt is detached")


def _phase3_manifest_bytes(manifest: dict) -> bytes:
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_content_addressed(path: Path, payload: bytes) -> None:
    digest = _sha256_bytes(payload)
    if path.exists():
        if _sha256_bytes(path.read_bytes()) != digest:
            raise ConfirmationConflict("confirmation transaction archive is corrupt")
        return
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if _sha256_bytes(path.read_bytes()) != digest:
            raise ConfirmationConflict("confirmation transaction archive raced")


def _acquire_confirmation_lock(
    lock_path: Path, sidecar_path: Path, source_archive: Path, expected_sha256: str
) -> int:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        return os.open(lock_path, flags)
    except FileExistsError as exc:
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            raise ConfirmationConflict("another confirmation apply is active") from exc
        if age < CONFIRMATION_LOCK_STALE_SECONDS:
            raise ConfirmationConflict("another confirmation apply is active") from exc

    stale_path = lock_path.with_name(f"stale-{uuid.uuid4().hex}.lock")
    try:
        os.replace(lock_path, stale_path)
        lock_fd = os.open(lock_path, flags)
    except OSError as exc:
        raise ConfirmationConflict("stale confirmation transaction could not be claimed") from exc
    try:
        if not sidecar_path.exists():
            if (
                not source_archive.is_file()
                or _sha256_bytes(source_archive.read_bytes()) != expected_sha256
            ):
                raise ConfirmationConflict("stale confirmation transaction needs recovery")
            try:
                os.link(source_archive, sidecar_path)
            except FileExistsError:
                pass
        stale_path.unlink(missing_ok=True)
        return lock_fd
    except Exception:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)
        raise


def _compare_and_swap_link_manifest(
    sidecar_path: Path, expected_sha256: str, manifest: dict
) -> str:
    """Replace a manifest only if the exact source bytes are still current."""

    payload = _phase3_manifest_bytes(manifest)
    output_sha = _sha256_bytes(payload)
    transaction_dir = sidecar_path.parent / ".link-manifest-transactions"
    transaction_dir.mkdir(parents=True, exist_ok=True)
    output_archive = transaction_dir / f"{output_sha}.json"
    source_archive = transaction_dir / f"{expected_sha256}.json"
    lock_path = transaction_dir / "apply.lock"
    lock_fd: int | None = None
    lock_fd = _acquire_confirmation_lock(
        lock_path, sidecar_path, source_archive, expected_sha256
    )
    backup_path: Path | None = None
    install_path: Path | None = None
    try:
        os.write(lock_fd, f"{os.getpid()} {time.time()}\n".encode("ascii"))
        os.close(lock_fd)
        lock_fd = None
        current = sidecar_path.read_bytes()
        if _sha256_bytes(current) != expected_sha256:
            raise ConfirmationConflict("link manifest changed before apply")
        _write_content_addressed(source_archive, current)
        _write_content_addressed(output_archive, payload)
        install_path = transaction_dir / f"install-{uuid.uuid4().hex}.json"
        _write_content_addressed(install_path, payload)
        # Detach and verify the exact source atomically. A writer racing before
        # this rename is preserved in backup and never overwritten.
        backup_path = transaction_dir / f"detached-{uuid.uuid4().hex}.json"
        os.replace(sidecar_path, backup_path)
        if _sha256_bytes(backup_path.read_bytes()) != expected_sha256:
            if not sidecar_path.exists():
                os.link(backup_path, sidecar_path)
            raise ConfirmationConflict("link manifest changed during apply")
        try:
            os.link(install_path, sidecar_path)
        except FileExistsError as exc:
            raise ConfirmationConflict("link manifest was concurrently replaced") from exc
        if _sha256_bytes(sidecar_path.read_bytes()) != output_sha:
            raise ConfirmationConflict("link manifest changed after apply")
        backup_path.unlink()
        backup_path = None
        install_path.unlink()
        install_path = None
        return output_sha
    except OSError as exc:
        raise ConfirmationConflict("link manifest compare-and-swap failed") from exc
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if backup_path is not None and backup_path.exists() and not sidecar_path.exists():
            try:
                os.link(backup_path, sidecar_path)
            except OSError:
                pass
        if install_path is not None and install_path.exists() and sidecar_path.exists():
            try:
                if os.path.samefile(install_path, sidecar_path):
                    install_path.unlink()
            except OSError:
                pass
        lock_path.unlink(missing_ok=True)


def _recover_stale_confirmation_source(
    sidecar_path: Path, expected_sha256: str
) -> None:
    if sidecar_path.exists():
        return
    transaction_dir = sidecar_path.parent / ".link-manifest-transactions"
    lock_path = transaction_dir / "apply.lock"
    source_archive = transaction_dir / f"{expected_sha256}.json"
    if not lock_path.exists():
        return
    lock_fd = _acquire_confirmation_lock(
        lock_path, sidecar_path, source_archive, expected_sha256
    )
    os.close(lock_fd)
    lock_path.unlink(missing_ok=True)


def _read_phase3_state(run_dir: Path) -> tuple[Path, dict, str, dict, str]:
    """Read one source-bound link/snapshot pair without mutating the run."""

    import phase3_fixed_edges as p3
    from solution_link import read_link_manifest

    sidecar_path = run_dir / "solutions" / "link_manifest.json"
    try:
        raw = sidecar_path.read_bytes()
        raw_manifest = json.loads(raw)
        manifest = read_link_manifest(run_dir)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ConfirmationConflict("link manifest is missing or invalid") from exc
    try:
        changed_while_validating = sidecar_path.read_bytes() != raw
    except OSError as exc:
        raise ConfirmationConflict("link manifest changed while being validated") from exc
    if manifest is None or manifest != raw_manifest or changed_while_validating:
        raise ConfirmationConflict("link manifest changed while being validated")
    if manifest.get("manual_propagation_contract") != p3.MANUAL_PROPAGATION_CONTRACT:
        raise ConfirmationConflict("this run has no fixed-edge propagation contract")
    reference = manifest.get("alignment_snapshot")
    if not isinstance(reference, dict) or set(reference) != {
        "relative_path",
        "sha256",
        "schema_version",
    }:
        raise ConfirmationConflict("alignment snapshot reference is invalid")
    if reference.get("schema_version") != p3.ALIGNMENT_SNAPSHOT_SCHEMA_VERSION:
        raise ConfirmationConflict("alignment snapshot version is unsupported")
    try:
        expected_snapshot_sha = _require_sha256(
            reference.get("sha256"), "snapshot sha256"
        )
    except ValueError as exc:
        raise ConfirmationConflict("alignment snapshot reference is invalid") from exc
    snapshot_path = run_dir / Path(str(reference.get("relative_path") or ""))
    try:
        encoded_snapshot = snapshot_path.read_bytes()
        if _sha256_bytes(encoded_snapshot) != expected_snapshot_sha:
            raise ValueError("alignment snapshot file SHA-256 mismatch")
        snapshot = json.loads(encoded_snapshot)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ConfirmationConflict("alignment snapshot is stale or invalid") from exc
    _validate_confirmation_events(manifest)
    try:
        changed_after_validation = sidecar_path.read_bytes() != raw
    except OSError as exc:
        raise ConfirmationConflict("link manifest changed while being validated") from exc
    if changed_after_validation:
        raise ConfirmationConflict("link manifest changed while being validated")
    return (
        sidecar_path,
        manifest,
        _sha256_bytes(raw),
        snapshot,
        expected_snapshot_sha,
    )


def _confirmation_identity(
    record: dict, document: dict, *, job_id: str, doc_index: int, run_dir: Path
) -> dict[str, str]:
    import phase3_fixed_edges as p3

    output_root = Path(str(record.get("output_root") or "")).expanduser().resolve()
    canonical_run, relative_run = _canonical_descendant(
        run_dir, output_root, "document run directory"
    )
    source = Path(str(record.get("source") or "")).expanduser().resolve()
    return {
        "job_sha256": p3.canonical_sha256(
            {
                "job_id": job_id,
                "record_id": record.get("id"),
                "source": str(source),
                "source_name": record.get("source_name"),
                "output_root": str(output_root),
                "created_at": record.get("created_at"),
                "options": record.get("options"),
            }
        ),
        "document_sha256": p3.canonical_sha256(
            {
                "doc": doc_index,
                "document_id": document.get("id"),
                "document_name": document.get("source_name")
                or document.get("name"),
                "run_dir": str(canonical_run),
                "relative_run_dir": relative_run.as_posix(),
            }
        ),
        "run_sha256": p3.canonical_sha256(
            {
                "output_root": str(output_root),
                "run_dir": str(canonical_run),
                "relative_run_dir": relative_run.as_posix(),
            }
        ),
    }


def _normalize_confirmation_selection(payload: dict) -> tuple[str, list[str], list[str], bool]:
    unit_id = str(payload.get("unit_id") or "").strip()
    if not unit_id:
        raise ValueError("unit_id is required")

    def one(field: str) -> list[str]:
        value = payload.get(field, [])
        if (
            not isinstance(value, list)
            or len(value) > 1
            or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            raise ValueError(f"{field} must contain at most one asset id")
        return [item.strip() for item in value]

    answer_ids = one("answer_ids")
    explanation_ids = one("explanation_ids")
    if not answer_ids and not explanation_ids:
        raise ValueError("one answer or explanation asset must be selected")
    propagate_scope = payload.get("propagate_scope", False)
    if not isinstance(propagate_scope, bool):
        raise ValueError("propagate_scope must be boolean")
    return unit_id, answer_ids, explanation_ids, propagate_scope


def _fixed_edge_constraints(
    snapshot: dict,
    manifest: dict,
    *,
    unit_id: str,
    answer_ids: list[str],
    explanation_ids: list[str],
    created_at: str,
) -> tuple[list[dict], list[dict]]:
    import phase3_fixed_edges as p3

    existing = _json_clone(manifest.get("manual_constraints", []))
    assert isinstance(existing, list)
    additions: list[dict] = []
    for role, asset_ids in (
        (p3.ROLE_QUICK, answer_ids),
        (p3.ROLE_DETAIL, explanation_ids),
    ):
        for asset_id in asset_ids:
            same_role = [
                value
                for value in existing
                if isinstance(value, dict)
                and value.get("unit_id") == unit_id
                and value.get("role") == role
            ]
            if same_role:
                if len(same_role) != 1 or same_role[0].get("asset_id") != asset_id:
                    raise ConfirmationConflict("the selected role already has another fixed edge")
                continue
            try:
                additions.append(
                    p3.make_manual_constraint(
                        snapshot,
                        confirmation_id="mc-" + uuid.uuid4().hex,
                        unit_id=unit_id,
                        role=role,
                        asset_id=asset_id,
                        created_at=created_at,
                    )
                )
            except p3.FixedEdgeError as exc:
                raise ConfirmationConflict(str(exc)) from exc
    if not additions:
        raise ConfirmationConflict("the selected fixed edge is already confirmed")
    return [*existing, *additions], additions


def _effective_fixed_edge_plan(
    snapshot: dict,
    manifest: dict,
    constraints: list[dict],
    additions: list[dict],
    *,
    propagate_scope: bool,
) -> dict:
    import phase3_fixed_edges as p3

    try:
        core_plan = p3.plan_fixed_edges(snapshot, constraints)
    except p3.FixedEdgeError as exc:
        raise ConfirmationConflict(str(exc)) from exc
    if propagate_scope:
        result = _json_clone(core_plan)
        assert isinstance(result, dict)
        return result

    selected = {(value["unit_id"], value["role"]) for value in additions}
    planned = {
        value["unit_id"]: value["decision"]
        for value in core_plan["prospective_decisions"]
    }
    decisions = _json_clone(manifest["items"])
    assert isinstance(decisions, dict)
    for unit_id, _ in selected:
        decisions[unit_id] = _json_clone(planned[unit_id])
    asset_status = {
        asset_id: str(asset.get("selection_status") or "review_only")
        for asset_id, asset in manifest["assets"].items()
    }
    for value in additions:
        asset_status[value["asset_id"]] = "manual_verified"
    effects = [
        value
        for value in core_plan["effects"]
        if (value["unit_id"], value["role"]) in selected
    ]
    counts = {
        result_name: sum(1 for value in effects if value["result"] == result_name)
        for result_name in ("manual_verified", "auto_verified", "review")
    }
    payload = {
        "contract": core_plan["contract"],
        "snapshot_payload_sha256": core_plan["snapshot_payload_sha256"],
        "constraints": core_plan["constraints"],
        "prospective_decisions": [
            {"unit_id": value["unit_id"], "decision": decisions[value["unit_id"]]}
            for value in core_plan["prospective_decisions"]
        ],
        "asset_selection_status": dict(sorted(asset_status.items())),
        "effects": effects,
        "counts": counts,
        "provider_calls": 0,
    }
    return {**payload, "plan_sha256": p3.canonical_sha256(payload)}


def _role_delta_state(decision: dict, role: str, assets: dict) -> dict:
    import phase3_fixed_edges as p3

    prefix = "answer" if role == p3.ROLE_QUICK else "explanation"
    role_confirmations = decision.get("role_confirmations")
    role_confirmation = (
        role_confirmations.get(role) if isinstance(role_confirmations, dict) else None
    )
    return {
        "ids": decision.get(prefix + "_ids", []),
        "status": decision.get(prefix + "_status"),
        "evidence": decision.get(prefix + "_evidence", []),
        "review_reasons": decision.get(prefix + "_review_reasons", []),
        "confidence": decision.get(prefix + "_confidence"),
        "confirmation": decision.get(prefix + "_confirmation"),
        "role_confirmation": role_confirmation,
        # candidate_ids is shared storage, but a reservation/removal still belongs
        # to the role whose projected group produced the effect.
        "candidate_ids": [
            asset_id
            for asset_id in decision.get("candidate_ids", [])
            if (assets.get(asset_id) or {}).get("kind") in {role, p3.ROLE_MIXED}
        ],
    }


def _current_delta_fixed_edge_plan(fixed_plan: dict, manifest: dict) -> dict:
    """Normalize a core plan to the current manifest and report only real deltas."""

    import phase3_fixed_edges as p3

    normalized = _json_clone(fixed_plan)
    assert isinstance(normalized, dict)
    planned = {
        value["unit_id"]: value["decision"]
        for value in normalized["prospective_decisions"]
    }
    asset_status = normalized["asset_selection_status"]
    for unit_id, decision in planned.items():
        linked = set(decision.get("answer_ids", [])) | set(
            decision.get("explanation_ids", [])
        )
        decision["candidate_ids"] = [
            asset_id
            for asset_id in decision.get("candidate_ids", [])
            if asset_id in linked or asset_status.get(asset_id) == "review_only"
        ]
        if decision.get("overall_status") == "complete":
            decision["review_reasons"] = []

    effects: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for source_effect in normalized["effects"]:
        unit_id = source_effect["unit_id"]
        role = source_effect["role"]
        key = (unit_id, role)
        if key in seen:
            raise ConfirmationConflict("fixed-edge plan contains duplicate effects")
        seen.add(key)
        before = manifest["items"][unit_id]
        after = planned[unit_id]
        if p3.canonical_sha256(
            _role_delta_state(before, role, manifest["assets"])
        ) == p3.canonical_sha256(
            _role_delta_state(after, role, manifest["assets"])
        ):
            continue
        effects.append(
            {
                **{
                    key: _json_clone(source_effect.get(key))
                    for key in ("unit_id", "role", "result", "reason", "asset_id")
                },
                "before_sha256": p3.canonical_sha256(before),
                "after_sha256": p3.canonical_sha256(after),
            }
        )
    changed_units = {
        unit_id
        for unit_id, after in planned.items()
        if p3.canonical_sha256(manifest["items"][unit_id])
        != p3.canonical_sha256(after)
    }
    effect_units = {effect["unit_id"] for effect in effects}
    changed_role_keys = {
        (unit_id, role)
        for unit_id, after in planned.items()
        for role in (p3.ROLE_QUICK, p3.ROLE_DETAIL)
        if p3.canonical_sha256(
            _role_delta_state(manifest["items"][unit_id], role, manifest["assets"])
        )
        != p3.canonical_sha256(
            _role_delta_state(after, role, manifest["assets"])
        )
    }
    effect_keys = {(effect["unit_id"], effect["role"]) for effect in effects}
    if changed_role_keys != effect_keys:
        raise ConfirmationConflict("fixed-edge plan has an unreported role delta")
    if not changed_units.issubset(effect_units):
        raise ConfirmationConflict("fixed-edge plan has an unreported decision delta")
    normalized["effects"] = effects
    normalized["counts"] = {
        result: sum(effect["result"] == result for effect in effects)
        for result in CONFIRMATION_EFFECT_RESULTS
    }
    payload = {key: value for key, value in normalized.items() if key != "plan_sha256"}
    normalized["plan_sha256"] = p3.canonical_sha256(payload)
    return normalized


class JobCancelled(RuntimeError):
    """The owned job was cancelled before another stage could start."""


class JobStore:
    def _persist_diagnostic(self, job_id: str, detail: dict) -> None:
        # Outside output/run_dir: neither asset route serves this directory.
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', job_id):
            raise ValueError('invalid diagnostic owner')
        owner = self.job_root / job_id
        path = owner / 'internal-errors.json'
        for entry in (self.job_root, owner, path):
            if entry.is_symlink() or entry.is_junction():
                raise ValueError('linked diagnostic path')
        resolved_owner = inside(owner, 'diagnostic_owner')
        if resolved_owner.parent != self.job_root.resolve():
            raise ValueError('diagnostic owner mismatch')
        owner.mkdir(parents=True, exist_ok=True)
        events = []
        if path.exists():
            if path.stat().st_size > 131072:
                raise ValueError('diagnostic history too large')
            value = json.loads(path.read_text(encoding='utf-8'))
            if value.get('version') != 1 or not isinstance(value.get('events'), list):
                raise ValueError('invalid diagnostic history')
            events = value['events']
        if len(events) >= 32:
            events = events[:1] + events[-30:]
        events.append(detail)
        payload = json.dumps({'version': 1, 'events': events}, ensure_ascii=False)
        if len(payload.encode('utf-8')) > 131072:
            raise ValueError('diagnostic history too large')
        temporary = owner / ('.diag-' + uuid.uuid4().hex[:12] + '.tmp')
        inside(temporary, 'diagnostic_temporary')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _diagnose(self, job_id, operation, exc):
        # Deliberately exclude messages, source lines, locals, argv and raw paths.
        if not hasattr(exc, '_astra_operation'):
            exc._astra_operation = operation
        operation = exc._astra_operation
        name = Path(str(getattr(exc, 'filename', '') or '')).name
        safe_name = name if name in {'job.json', 'job.json.tmp', 'worker.log'} else None
        frames = []
        tb = exc.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            filename = Path(code.co_filename).name
            frames.append({'file': filename if filename in {'app_server.py', 'process_control.py'} else 'external',
                           'line': tb.tb_lineno, 'function': code.co_name[:80]})
            tb = tb.tb_next
        detail = {'operation': operation, 'type': type(exc).__name__,
                  'errno': getattr(exc, 'errno', None), 'winerror': getattr(exc, 'winerror', None),
                  'filename': safe_name, 'traceback': frames[-16:]}
        with self.lock:
            if not hasattr(self, '_private_diagnostics'):
                self._private_diagnostics = {}
            history = self._private_diagnostics.setdefault(job_id, [])
            # Keep the first failure and bounded most recent details.
            if len(history) >= 32:
                del history[1]
            history.append(detail)
            try:
                self._persist_diagnostic(job_id, detail)
            except Exception:
                self._diagnostic_sink_failed = True

    def __init__(self, job_root: Path = JOB_ROOT, upload_root: Path = UPLOAD_ROOT) -> None:
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.processes: dict[str, OwnedProcess] = {}
        self.job_root = inside(job_root, "job_root")
        self.upload_root = inside(upload_root, "upload_root")
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="crop-job")
        self.job_root.mkdir(parents=True, exist_ok=True)
        self.upload_root.mkdir(parents=True, exist_ok=True)
        for path in self.job_root.glob("*/job.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                changed = False
                if record.get("state") in {"running", "queued"}:
                    record.update(state="interrupted", stage="중단됨", message="앱 재시작으로 작업이 중단되었습니다.")
                    changed = True
                solution = record.get("solution")
                if isinstance(solution, dict) and solution.get("state") in {"queued", "indexing", "linking"}:
                    solution.update(state="interrupted", message="앱 재시작으로 해설 연결이 중단되었습니다.")
                    changed = True
                self.jobs[record["id"]] = record
                if changed:
                    self.persist(record)
            except (OSError, KeyError, json.JSONDecodeError):
                continue

    def persist(self, record: dict) -> None:
        try:
            write_json(self.job_root / record["id"] / "job.json", record)
        except OSError as exc:
            self._diagnose(record['id'], 'state_write', exc)
            raise

    def update(self, job_id: str, **changes: object) -> dict:
        with self.lock:
            record = self.jobs[job_id]
            if record.get("cancel_requested"):
                return dict(record)
            previous = dict(record)
            record.update(changes, updated_at=now())
            try:
                self.persist(record)
            except OSError:
                record.clear()
                record.update(previous)
                raise
            return dict(record)

    def update_solution(self, job_id: str, **changes: object) -> dict:
        with self.lock:
            record = self.jobs[job_id]
            if record.get("cancel_requested"):
                return dict(record)
            solution = dict(record.get("solution") or {})
            solution.update(changes, updated_at=now())
            previous = dict(record)
            record.update(solution=solution, updated_at=now())
            try:
                self.persist(record)
            except OSError:
                record.clear()
                record.update(previous)
                raise
            return dict(record)

    def create(self, payload: dict) -> dict:
        source_value = str(payload.get("source") or "").strip()
        if not source_value:
            raise ValueError("PDF 또는 폴더 경로를 입력해주세요.")
        source = Path(source_value).expanduser()
        if not source.exists():
            raise ValueError("PDF 또는 폴더 경로를 찾을 수 없습니다.")
        if source.is_file() and source.suffix.lower() != ".pdf":
            raise ValueError("PDF 파일만 업로드할 수 있습니다.")
        solution_source = pdf_path(payload.get("solution_source"))
        selected_crop_dpi = crop_dpi(payload.get("crop_dpi"))
        selected_provider_profile = provider_profile(payload.get("provider_profile"))
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        options = {
            "pages": str(payload.get("pages") or "all"),
            "unit": str(payload.get("unit") or "atomic"),
            "workers": max(1, min(4, int(payload.get("workers") or 2))),
            "answer_space": str(payload.get("answer_space") or "exclude"),
            "sidecars": str(payload.get("sidecars") or "exclude"),
            "verify": str(payload.get("verify") or "all"),
            "ensemble": selected_provider_profile == "full" and bool(payload.get("ensemble", True)),
            "gemini": selected_provider_profile == "full" and bool(payload.get("gemini", True)),
            "qwen": selected_provider_profile == "full" and bool(payload.get("qwen", True)),
            "provider_profile": selected_provider_profile,
            "crop_dpi": selected_crop_dpi,
            "solution_source": str(solution_source) if solution_source else None,
        }
        record = {
            "id": job_id,
            "source": str(source.resolve()),
            "source_name": source.name,
            "state": "queued",
            "stage": "대기 중",
            "progress": 0,
            "message": "작업을 준비하고 있습니다.",
            "created_at": now(),
            "updated_at": now(),
            "options": options,
            "output_root": str((self.job_root / job_id / "output").resolve()),
            "log_file": str((self.job_root / job_id / "worker.log").resolve()),
            "documents": [],
            "summary": {"pages": 0, "crops": 0, "passed": 0, "review": 0, "failed": 0},
            "solution": {
                "state": "queued" if solution_source else "idle",
                "outcome": "pending" if solution_source else "not_requested",
                "progress": 0,
                "message": "문제 crop 완료 후 해설을 연결합니다." if solution_source else "정답과해설 PDF가 연결되지 않았습니다.",
                "warning_code": None,
                "solution_data_available": False,
                "explanations_available": False,
                "source": str(solution_source) if solution_source else None,
                "source_name": solution_source.name if solution_source else None,
                "crop_dpi": selected_crop_dpi,
            },
            "logs": [],
        }
        with self.lock:
            self.jobs[job_id] = record
            self.persist(record)
        self.executor.submit(self.run, job_id)
        return dict(record)

    def resume(self, job_id: str, payload: dict) -> dict:
        return self._resume(job_id, payload)

    def recovery_resume(self, job_id: str, payload: dict) -> dict:
        if type(payload) is not dict or payload:
            raise recovery_receipt.RecoveryConflict('recovery request must be empty')
        with self.lock:
            record = self.jobs[job_id]
            if job_id in self.processes or record.get('state') in {'queued', 'running'}:
                raise recovery_receipt.RecoveryConflict('job already active')
            before, intent, exit_evidence, key = recovery_receipt.load_bundle(self.job_root, job_id, inside)
            attempts = record.get('resume_attempts') or []
            if any(a.get('recovery_receipt_sha256') == key for a in attempts):
                # Receipt consumed with the durable queued transition: never launch twice.
                return dict(record)
            recovery_receipt.validate_cancelled(self.job_root, job_id, record, before, intent, exit_evidence)
            return self._resume(job_id, {}, recovery_key=key)

    def _resume(self, job_id: str, payload: dict, *, recovery_key: str | None = None) -> dict:
        if payload:
            raise ValueError("재개 요청 본문은 빈 객체여야 합니다.")
        with self.lock:
            record = self.jobs[job_id]
            allowed = {'cancelled'} if recovery_key is not None else {'failed', 'interrupted'}
            if record.get("state") not in allowed:
                raise ValueError("실패하거나 중단된 작업만 재개할 수 있습니다.")
            if job_id in self.processes:
                raise ValueError("작업이 이미 실행 중입니다.")
            owner = inside(self.job_root / job_id, "job_root")
            output = inside(record.get("output_root", ""), "output")
            if output != owner / "output":
                raise ValueError("기존 출력 폴더 소유권을 확인할 수 없습니다.")
            source = Path(str(record.get("source") or ""))
            states = list(output.glob("*/run_state.json"))
            if not source.is_file() or len(states) != 1:
                raise ValueError("재개할 원본 실행 기록을 확인할 수 없습니다.")
            state = json.loads(states[0].read_text(encoding="utf-8"))
            if state.get("source_sha256") != _sha256_file(source):
                raise ValueError("원본 PDF가 변경되어 자동 재개할 수 없습니다.")
            detections = list(states[0].parent.glob("detections/page-????.json"))
            processed = state.get("processed_pages")
            if not isinstance(processed, list) or len(detections) != len(processed):
                raise ValueError("기본 분석 결과가 불완전하여 자동 재개할 수 없습니다.")
            expected_meta = {
                "source_sha256": state["source_sha256"],
                "model": "gpt-6-astra",
                "reasoning_effort": "medium",
                "prompt_template_sha256": _sha256_file(ROOT / "prompts/detect_questions.md"),
                "schema_sha256": _sha256_file(ROOT / "detection.schema.json"),
            }
            for path in detections:
                wrapper = json.loads(path.read_text(encoding="utf-8"))
                meta = wrapper.get("meta")
                if not isinstance(meta, dict) or any(meta.get(key) != value for key, value in expected_meta.items()):
                    raise ValueError("분석 조건이 변경되어 자동 재개할 수 없습니다.")
            attempts = list(record.get("resume_attempts") or [])
            attempts.append({
                "attempt": len(attempts) + 1,
                "requested_at": now(),
                "output_root": str(output),
                "source_sha256": state["source_sha256"],
                "prior_terminal": {
                    key: record.get(key)
                    for key in ("state", "stage", "progress", "message", "error_code", "updated_at")
                },
                "log_file": record.get("log_file"),
            })
            if recovery_key is not None:
                attempts[-1]['recovery_receipt_sha256'] = recovery_key
            previous = dict(record)
            record.update(
                state="queued",
                stage="재개 대기 중",
                progress=min(int(record.get("progress") or 0), 99),
                message="기존 분석 결과에서 중단 지점을 확인하고 있습니다.",
                error_code=None,
                cancel_requested=False,
                resume_attempts=attempts,
                updated_at=now(),
            )
            try:
                self.persist(record)
            except OSError:
                record.clear()
                record.update(previous)
                raise
            queued = dict(record)
        self.executor.submit(self.run, job_id)
        return queued

    def append_log(self, job_id: str, line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        with self.lock:
            record = self.jobs[job_id]
            started = False
            try:
                Path(record["log_file"]).parent.mkdir(parents=True, exist_ok=True)
                with Path(record["log_file"]).open("a", encoding="utf-8") as stream:
                    started = True
                    stream.write(line + "\n")
            except OSError as exc:
                exc._astra_log_uncertain = started
                self._diagnose(job_id, 'log_write', exc)
                raise
            if not line.startswith("ASTRA_INTERNAL_DIAGNOSTIC "):
                record['logs'] = (record.get('logs') or [])[-79:] + [line]
            self.persist(record)

    def stage_from_line(self, job_id: str, line: str) -> None:
        if line.startswith('ASTRA_INTERNAL_DIAGNOSTIC '):
            return
        rules = [
            ("저비용 페이지 분류", "페이지 분류", 18),
            ("Codex 검출", "Codex 의미 분석", 35),
            ("경계 교정 패스", "경계 교정", 56),
            # "VLM 크롭 검수" also contains "크롭 ", so it must be matched first.
            ("VLM 크롭 검수", "크롭 검수", 78),
            ("크롭 ", "문항 이미지 크롭", 68),
        ]
        for token, stage, progress in rules:
            if token in line:
                self.update(job_id, stage=stage, progress=progress, message=line[:240])
                return

    def cancel(self, job_id: str) -> dict:
        """Confirm kernel-owned tree exit before recording cancellation."""
        with self.lock:
            record = self.jobs[job_id]
            solution = record.get("solution") or {}
            owned = self.processes.get(job_id)
            if owned is None and record.get("state") == "interrupted":
                record.update(cancel_requested=True, error_code="cancel_failed",
                              message="작업 종료를 확인하지 못했습니다. 취소를 다시 요청해 주세요.")
                self.persist(record)
                return dict(record)
            if owned is None and record.get("state") in {"cancelled", "failed"}:
                return dict(record)
            if owned is None and record.get("state") == "completed" and solution.get("state") not in {"queued", "indexing", "linking"}:
                return dict(record)
            record.update(cancel_requested=True, message="취소 요청을 처리하고 있습니다.", updated_at=now())
            self.persist(record)
            # ponytail: serialize short stop requests; per-job locks if concurrent jobs grow.
            try:
                if owned is not None:
                    owned.cancel()
            except (OSError, TimeoutError) as exc:
                record.update(error_code="cancel_failed", message="작업 종료를 확인하지 못했습니다. 취소를 다시 요청해 주세요.")
                self.persist(record)
                self.append_log(job_id, f"CANCEL ERROR: {type(exc).__name__}")
                return dict(record)
            record.update(state="cancelled", stage="취소됨", error_code="cancelled_by_user", message="작업과 하위 실행이 종료되었습니다.", updated_at=now())
            if solution.get("state") in {"queued", "indexing", "linking"}:
                record["solution"] = {**solution, "state": "cancelled", "message": "해설 연결이 취소되었습니다."}
            self.persist(record)
            return dict(record)

    def _run_owned(self, job_id: str, command: list[str], *, solution: bool) -> int:
        with self.lock:
            if self.jobs[job_id].get("cancel_requested"):
                raise JobCancelled()
            if job_id in self.processes:
                raise ValueError("이전 실행의 종료를 아직 확인하지 못했습니다.")
            owned = OwnedProcess(command, ROOT)
            self.processes[job_id] = owned
        process = owned.process
        exited = False
        pending = []
        lost = False
        operation = 'pipe_read'
        def flush_pending():
            nonlocal lost
            while pending:
                try:
                    self.append_log(job_id, pending[0])
                except OSError as exc:
                    if getattr(exc, '_astra_operation', None) == 'state_write':
                        pending.pop(0)  # log append committed; do not duplicate it
                        continue
                    if getattr(exc, '_astra_operation', None) != 'log_write':
                        raise
                    if getattr(exc, '_astra_log_uncertain', False):
                        lost = True  # never replay a potentially partial append
                        pending.pop(0)
                    break
                else:
                    pending.pop(0)
        try:
            assert process.stdout is not None
            for line in process.stdout:
                pending.append("[해설] " + line.rstrip() if solution else line)
                if len(pending) > 80:
                    pending.pop(0)
                    lost = True
                flush_pending()
                if not solution:
                    try:
                        self.stage_from_line(job_id, line)
                    except OSError as exc:
                        if getattr(exc, '_astra_operation', None) != 'state_write':
                            raise
                operation = 'pipe_read'
            operation = 'process_wait'
            code = process.wait()
            # A child can close its inherited pipe and outlive the direct parent.
            operation = 'job_query'
            while owned.active():
                time.sleep(0.05)
            exited = True
            with self.lock:
                if self.jobs[job_id].get("cancel_requested"):
                    raise JobCancelled()
                flush_pending()
                # Child failure retains its original fail-closed path.
                if not code:
                    if pending or lost:
                        exc = OSError('recording incomplete')
                        exc._astra_operation = 'log_write'
                        raise exc
                    self.persist(self.jobs[job_id])
            return code
        except JobCancelled:
            raise
        except Exception as exc:
            self._diagnose(job_id, operation, exc)
            raise
        finally:
            with self.lock:
                # No second query that could hide the primary exception.
                # Unknown/alive ownership always blocks resume.
                if exited:
                    try:
                        owned.close()
                        if process.stdout is not None:
                            process.stdout.close()
                    except Exception as exc:
                        self._diagnose(job_id, 'close', exc)
                    else:
                        self.processes.pop(job_id, None)

    def run_command(self, job_id: str, command: list[str], stage: str, progress: int) -> int:
        self.update(job_id, state="running", stage=stage, progress=progress, message=f"{stage}을 진행하고 있습니다.")
        return self._run_owned(job_id, command, solution=False)

    def run_solution_command(self, job_id: str, command: list[str]) -> int:
        return self._run_owned(job_id, command, solution=True)

    def detection_failure(self, job_id: str):
        record = self.jobs[job_id]
        try:
            owner = inside(self.job_root / job_id, 'job_root')
            output = inside(record['output_root'], 'output')
            if output != (owner / 'output').resolve() or not output.is_relative_to(owner):
                return None
            manifest = inside(output / 'batch_manifest.json', 'output')
            if not manifest.is_relative_to(output):
                return None
            batch = json.loads(manifest.read_text(encoding='utf-8'))
            failures = batch.get('failures', []) if isinstance(batch, dict) else []
            if not isinstance(failures, list):
                return None
            for value in failures:
                failure = parse_page_failure(value)
                if failure is not None:
                    return failure
        except (OSError, ValueError, KeyError):
            return None
        return None

    def run(self, job_id: str) -> None:
        record = self.jobs[job_id]
        options = record["options"]
        command = build_pipeline_command(record["source"], record["output_root"], options)
        try:
            code = self.run_command(job_id, command, "파이프라인 시작", 6)
            if code:
                raise RuntimeError(f"문항 파이프라인이 종료 코드 {code}로 중단되었습니다.")
            batch_path = Path(record["output_root"]) / "batch_manifest.json"
            batch = json.loads(batch_path.read_text(encoding="utf-8"))
            document_dirs = [Path(item["run_dir"]) for item in batch.get("documents", [])]
            ensemble_failed: set[Path] = set()
            if options["ensemble"] and ENSEMBLE.exists():
                for index, run_dir in enumerate(document_dirs):
                    ensemble_command = build_ensemble_command(run_dir, options)
                    code = self.run_command(job_id, ensemble_command, "다중 AI 합의", 82 + min(index, 2) * 3)
                    if code:
                        ensemble_failed.add(run_dir.resolve())
                        self.append_log(job_id, "다중 AI 합의가 완료되지 않아 결과를 검토 상태로 둡니다.")
            documents, summary = collect_results(
                job_id,
                document_dirs,
                ensemble_required=options["ensemble"],
                ensemble_failed=ensemble_failed,
                provider_profile=provider_profile(options.get("provider_profile")),
            )
            self.update(
                job_id,
                state="completed",
                stage="완료",
                progress=100,
                message="크롭과 검수가 완료되었습니다.",
                documents=documents,
                summary=summary,
            )
            if options.get("solution_source"):
                self.run_solution_linking(
                    job_id,
                    Path(str(options["solution_source"])),
                    crop_dpi_value=int(options["crop_dpi"]),
                    force=False,
                    run_dirs=document_dirs,
                )
        except JobCancelled:
            self.cancel(job_id)
            return
        except Exception as exc:
            operation = getattr(exc, '_astra_operation', None)
            if operation in {'state_write', 'log_write', 'pipe_read', 'job_query', 'process_wait'}:
                with self.lock:
                    if record.get('cancel_requested'):
                        return  # never overwrite an independently confirmed cancellation
                    record.update(state='interrupted', stage='기록/관측 장애',
                                  message='작업 기록 또는 실행 상태를 확인하지 못했습니다.',
                                  error_code=('recording_failed' if operation in {'state_write', 'log_write'}
                                              else 'observation_failed'), updated_at=now())
                    try:
                        self.persist(record)
                    except OSError:
                        pass  # private diagnostic retained; never claim durable success
                return
            failure = self.detection_failure(job_id)
            message = page_failure_message(failure) if failure else "문항 분석에 실패했습니다. 작업 로그를 확인해 주세요."
            self._diagnose(job_id, 'pipeline', exc)
            try:
                self.append_log(job_id, f"ERROR: {type(exc).__name__}: {message}")
            except OSError:
                pass
            current = self.get(job_id)
            try:
                self.update(job_id, state="failed", stage=("경계 교정 실패" if failure and failure["stage"] == "boundary_repair" else "실패"),
                            message=message, progress=int(current.get("progress") or 0),
                            error_code="crop_failed", detection_failure=failure)
            except OSError:
                # Child failure remains failure even when its receipt cannot commit.
                with self.lock:
                    if not record.get('cancel_requested'):
                        record.update(state='failed', stage='실패', error_code='crop_failed',
                                      message=message, recording_uncommitted=True)
                return
            if (self.get(job_id).get("solution") or {}).get("state") == "queued":
                self.update_solution(
                    job_id,
                    state="failed",
                    outcome="unavailable",
                    progress=100,
                    message="문제 crop 실패로 해설 연결을 시작하지 못했습니다.",
                    warning_code="question_crop_failed",
                    solution_data_available=False,
                    explanations_available=False,
                )

    def run_solution_linking(
        self,
        job_id: str,
        solution_source: Path,
        *,
        crop_dpi_value: int,
        force: bool,
        run_dirs: list[Path] | None = None,
    ) -> None:
        try:
            if not SOLUTION_LINK.is_file():
                raise FileNotFoundError("solution_link.py를 찾을 수 없습니다.")
            record = self.get(job_id)
            if run_dirs is None:
                run_dirs = [
                    Path(document["run_dir"])
                    for document in record.get("documents", [])
                    if isinstance(document, dict) and document.get("run_dir")
                ]
            if not run_dirs:
                raise ValueError("해설을 연결할 문제 run이 없습니다.")
            self.update_solution(
                job_id,
                state="indexing",
                outcome="pending",
                progress=5,
                message="정답과해설 PDF를 색인하고 있습니다.",
                warning_code=None,
                solution_data_available=False,
                explanations_available=False,
            )
            for index, run_dir in enumerate(run_dirs):
                self.update_solution(
                    job_id,
                    state="linking",
                    progress=10 + round(index / len(run_dirs) * 80),
                    message=f"문제와 답·해설을 연결하고 있습니다. ({index + 1}/{len(run_dirs)})",
                )
                command = build_solution_command(
                    run_dir,
                    solution_source,
                    crop_dpi_value=crop_dpi_value,
                    workers=int(record.get("options", {}).get("workers", 2)),
                    provider_profile=provider_profile(
                        record.get("options", {}).get("provider_profile")
                    ),
                    force=force,
                )
                code = self.run_solution_command(job_id, command)
                if code:
                    raise RuntimeError(f"해설 연결이 종료 코드 {code}로 중단되었습니다.")
            documents, summary = recollect_record_results(
                self.get(job_id), include_solutions=True
            )
            explanations_available = any(
                item.get("solutions", {}).get("explanations")
                for document in documents
                for item in document.get("items", [])
                if isinstance(item.get("solutions"), dict)
            )
            link_counts = {
                status: sum(
                    item.get("solutions", {}).get("overall_status") == status
                    for document in documents
                    for item in document.get("items", [])
                    if isinstance(item.get("solutions"), dict)
                )
                for status in ("complete", "partial", "review", "unmatched", "conflict")
            }
            self.update(job_id, documents=documents, summary=summary)
            self.update_solution(
                job_id,
                state="completed",
                outcome="linked",
                progress=100,
                message=(
                    "답·해설 연결 처리를 마쳤습니다. "
                    f"완료 {link_counts['complete']} · 일부 {link_counts['partial']} · "
                    f"검토 {link_counts['review'] + link_counts['conflict']} · "
                    f"없음 {link_counts['unmatched']}"
                ),
                warning_code=None,
                solution_data_available=True,
                explanations_available=explanations_available,
            )
        except JobCancelled:
            self.cancel(job_id)
            return
        except Exception as exc:
            self._diagnose(job_id, 'solution_pipeline', exc)
            with self.lock:
                if self.jobs[job_id].get('cancel_requested'):
                    return
            try:
                self.append_log(job_id, f"SOLUTION ERROR: {type(exc).__name__}")
            except OSError:
                pass
            record = self.get(job_id)
            documents = []
            for document in record.get("documents", []):
                clean_document = dict(document)
                clean_document["items"] = [
                    {**item, "solutions": None}
                    for item in document.get("items", [])
                    if isinstance(item, dict)
                ]
                documents.append(clean_document)
            # Commit one fail-closed solution state; do not let a second failed
            # receipt leave linking/completed or available=True in memory.
            operation = getattr(exc, '_astra_operation', '')
            observation = operation in {'state_write', 'log_write', 'pipe_read', 'job_query', 'process_wait'}
            with self.lock:
                live = self.jobs[job_id]
                if live.get('cancel_requested'):
                    return
                solution = dict(live.get('solution') or {})
                solution.update(state='interrupted' if observation else 'failed',
                                outcome='questions_only', progress=100,
                                message='해설 연결을 완료하지 못했습니다.',
                                warning_code='solution_observation_failed' if observation else 'solution_link_failed',
                                solution_data_available=False, explanations_available=False, updated_at=now())
                live.update(documents=documents, solution=solution, updated_at=now())
                try:
                    self.persist(live)
                except OSError:
                    live['recording_uncommitted'] = True

    def list(self) -> list[dict]:
        with self.lock:
            return sorted((dict(item) for item in self.jobs.values()), key=lambda item: item["created_at"], reverse=True)

    def get(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return dict(self.jobs[job_id])

    def _confirmation_context(self, job_id: str, doc_index: int) -> tuple[dict, dict, Path]:
        if job_id not in self.jobs:
            raise KeyError(job_id)
        record = self.jobs[job_id]
        if record.get("state") != "completed":
            raise ConfirmationConflict("the question crop job is not completed")
        if (record.get("solution") or {}).get("state") in {
            "queued",
            "indexing",
            "linking",
        }:
            raise ConfirmationConflict("solution linking is still active")
        documents = record.get("documents", [])
        if not isinstance(documents, list) or not (0 <= doc_index < len(documents)):
            raise ValueError("document index is invalid")
        document = documents[doc_index]
        if not isinstance(document, dict) or not document.get("run_dir"):
            raise ConfirmationConflict("document run identity is unavailable")
        if record.get("id") != job_id or not record.get("source") or not record.get(
            "output_root"
        ):
            raise ConfirmationConflict("job identity is incomplete")
        run_dir, _ = _canonical_descendant(
            Path(str(document["run_dir"])),
            Path(str(record["output_root"])),
            "document run directory",
        )
        return record, document, run_dir

    def _confirmation_plans(self) -> dict[str, dict]:
        plans = getattr(self, "confirmation_plans", None)
        if not isinstance(plans, dict):
            plans = {}
            self.confirmation_plans = plans
        return plans

    def create_confirmation_plan(self, job_id: str, payload: dict) -> dict:
        import phase3_fixed_edges as p3

        allowed = {
            "doc",
            "unit_id",
            "answer_ids",
            "explanation_ids",
            "propagate_scope",
            "expected_link_manifest_sha256",
            "expected_cache_key",
        }
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError("confirmation plan request fields are invalid")
        try:
            doc_index = int(payload.get("doc"))
        except (TypeError, ValueError) as exc:
            raise ValueError("document index is invalid") from exc
        unit_id, answer_ids, explanation_ids, propagate_scope = (
            _normalize_confirmation_selection(payload)
        )
        expected_link_sha = _require_sha256(
            payload.get("expected_link_manifest_sha256"),
            "expected_link_manifest_sha256",
        )
        expected_cache_key = str(payload.get("expected_cache_key") or "").strip()
        if not expected_cache_key:
            raise ValueError("expected_cache_key is required")

        with self.lock:
            record, document, run_dir = self._confirmation_context(job_id, doc_index)
            sidecar_path, manifest, link_sha, snapshot, snapshot_sha = (
                _read_phase3_state(run_dir)
            )
            if link_sha != expected_link_sha:
                raise ConfirmationConflict("link manifest changed; create a new preview")
            if manifest.get("cache_key") != expected_cache_key:
                raise ConfirmationConflict("link cache key changed; create a new preview")
            if unit_id not in manifest.get("items", {}):
                raise ValueError("unit_id is not present in this document")

            created_at = now()
            constraints, additions = _fixed_edge_constraints(
                snapshot,
                manifest,
                unit_id=unit_id,
                answer_ids=answer_ids,
                explanation_ids=explanation_ids,
                created_at=created_at,
            )
            fixed_plan = _effective_fixed_edge_plan(
                snapshot,
                manifest,
                constraints,
                additions,
                propagate_scope=propagate_scope,
            )
            fixed_plan = _current_delta_fixed_edge_plan(fixed_plan, manifest)
            selected_assets = {
                value["signature"]["asset_id"]: value
                for value in snapshot["solution_assets"]
                if value["signature"]["asset_id"]
                in {addition["asset_id"] for addition in additions}
            }
            identity = _confirmation_identity(
                record,
                document,
                job_id=job_id,
                doc_index=doc_index,
                run_dir=run_dir,
            )
            selection = {
                "unit_id": unit_id,
                "answer_ids": answer_ids,
                "explanation_ids": explanation_ids,
                "propagate_scope": propagate_scope,
            }
            bindings = {
                **identity,
                "unit_sha256": p3.problem_identity_sha256(snapshot, unit_id),
                "selection_sha256": p3.canonical_sha256(selection),
                "link_manifest_sha256": link_sha,
                "cache_key_sha256": p3.canonical_sha256(manifest["cache_key"]),
                "snapshot_file_sha256": snapshot_sha,
                "snapshot_payload_sha256": snapshot["payload_sha256"],
                "problem_source_sha256": snapshot["problem_source_sha256"],
                "solution_source_sha256": snapshot["solution_source_sha256"],
                "alignment_index_sha256": snapshot["alignment_index"]["sha256"],
                "versions_sha256": p3.canonical_sha256(snapshot["versions"]),
                "selected_assets": [
                    {
                        "asset_id": asset_id,
                        "identity_sha256": p3.asset_identity_sha256(snapshot, asset_id),
                        "output_sha256": selected_assets[asset_id]["output_sha256"],
                    }
                    for asset_id in sorted(selected_assets)
                ],
            }
            plan_id = "mcp-" + uuid.uuid4().hex
            expires_at_dt = datetime.now(timezone.utc) + timedelta(
                seconds=CONFIRMATION_PLAN_TTL_SECONDS
            )
            expires_at = expires_at_dt.isoformat(timespec="seconds")
            planned = {
                value["unit_id"]: value["decision"]
                for value in fixed_plan["prospective_decisions"]
            }
            added_groups = {
                (value["scope_id"], value["segment_id"], value["role"])
                for value in additions
            }
            scope_units = {
                candidate
                for group in snapshot["projected_groups"]
                if (
                    group["problem_scope_id"],
                    group["segment_id"],
                    group["role"],
                )
                in added_groups
                for candidate in group["unit_ids"]
            }
            problem_item = next(
                value
                for value in snapshot["problem_index"]["items"]
                if value["item_id"] == unit_id
            )
            first_constraint = additions[0]
            response = {
                "plan_id": plan_id,
                "source_link_manifest_sha256": link_sha,
                "snapshot_sha256": snapshot_sha,
                "scope": {
                    "scope_id": first_constraint["scope_id"],
                    "segment_id": first_constraint["segment_id"],
                    "label": problem_item.get("group_label")
                    or first_constraint["scope_id"],
                    "item_count": len(scope_units),
                },
                "manual_item": {
                    "unit_id": unit_id,
                    "before": _json_clone(manifest["items"][unit_id]),
                    "after": _json_clone(planned[unit_id]),
                },
                "effects": _json_clone(fixed_plan["effects"]),
                "counts": _json_clone(fixed_plan["counts"]),
                "provider_calls": 0,
                "expires_at": expires_at,
                "bindings": _json_clone(bindings),
            }
            response_sha = _confirmation_response_sha256(response)
            effects_sha = p3.canonical_sha256(response["effects"])
            counts_sha = p3.canonical_sha256(response["counts"])
            digest_payload = {
                "plan_id": plan_id,
                "expires_at": expires_at,
                "job_id": job_id,
                "doc": doc_index,
                "selection": selection,
                "bindings": bindings,
                "constraints": fixed_plan["constraints"],
                "fixed_edge_plan_sha256": fixed_plan["plan_sha256"],
                "response_sha256": response_sha,
                "effects_sha256": effects_sha,
                "counts_sha256": counts_sha,
                "provider_calls": 0,
            }
            plan_sha = p3.canonical_sha256(digest_payload)
            response.update(
                plan_sha256=plan_sha,
                response_sha256=response_sha,
                effects_sha256=effects_sha,
                counts_sha256=counts_sha,
            )
            plans = self._confirmation_plans()
            current_time = time.time()
            for stale_id in [
                key
                for key, value in plans.items()
                if float(value.get("expires_at_epoch", 0)) <= current_time
            ]:
                plans.pop(stale_id, None)
            plans[plan_id] = {
                "plan_id": plan_id,
                "plan_sha256": plan_sha,
                "digest_payload": digest_payload,
                "job_id": job_id,
                "doc": doc_index,
                "run_dir": str(run_dir),
                "selection": selection,
                "bindings": bindings,
                "constraints": fixed_plan["constraints"],
                "additions": additions,
                "fixed_plan": fixed_plan,
                "response": response,
                "expires_at_epoch": expires_at_dt.timestamp(),
            }
            # Preview intentionally does not call persist() or write any run file.
            if _sha256_bytes(sidecar_path.read_bytes()) != link_sha:
                plans.pop(plan_id, None)
                raise ConfirmationConflict("link manifest changed during preview")
            return _json_clone(response)

    def _receipt_response(
        self,
        event: dict,
        *,
        link_manifest_sha256: str,
        idempotent: bool,
    ) -> dict:
        return {
            "plan_id": event["plan_id"],
            "applied": True,
            "idempotent": idempotent,
            "confirmation_ids": list(event["confirmation_ids"]),
            "source_link_manifest_sha256": event[
                "source_link_manifest_sha256"
            ],
            "link_manifest_sha256": link_manifest_sha256,
            "receipt_sha256": event["receipt_sha256"],
            "effects": _json_clone(event["effects"]),
            "counts": _json_clone(event["counts"]),
            "effects_sha256": event["effects_sha256"],
            "counts_sha256": event["counts_sha256"],
            "response_sha256": event["response_sha256"],
            "provider_calls": 0,
        }

    def _find_confirmation_receipt(
        self, job_id: str, plan_id: str, expected_source_sha: str
    ) -> tuple[dict, int] | None:
        import phase3_fixed_edges as p3

        record = self.jobs.get(job_id)
        if not isinstance(record, dict):
            raise KeyError(job_id)
        for doc_index, document in enumerate(record.get("documents", [])):
            if not isinstance(document, dict) or not document.get("run_dir"):
                continue
            _, document, run_dir = self._confirmation_context(job_id, doc_index)
            try:
                sidecar_path, manifest, link_sha, _, _ = _read_phase3_state(run_dir)
            except ConfirmationConflict:
                continue
            events = manifest.get("manual_confirmation_events", [])
            matches = [
                value
                for value in events
                if isinstance(value, dict) and value.get("plan_id") == plan_id
            ]
            if not matches:
                continue
            if len(matches) != 1:
                raise ConfirmationConflict("confirmation receipt is ambiguous")
            event = matches[0]
            if event.get("source_link_manifest_sha256") != expected_source_sha:
                raise ConfirmationConflict("confirmation receipt source does not match")
            identity = _confirmation_identity(
                record,
                document,
                job_id=job_id,
                doc_index=doc_index,
                run_dir=run_dir,
            )
            if any(event.get(key) != value for key, value in identity.items()):
                raise ConfirmationConflict("confirmation receipt job identity changed")
            # The event is append-only and self-digested, so it remains a valid
            # receipt after later plans append newer events and decisions.
            if _sha256_bytes(sidecar_path.read_bytes()) != link_sha:
                raise ConfirmationConflict("link manifest changed while reading receipt")
            return self._receipt_response(
                event,
                link_manifest_sha256=link_sha,
                idempotent=True,
            ), doc_index
        return None

    def _refresh_confirmation_result(self, job_id: str) -> None:
        live_record = self.jobs[job_id]
        previous_documents = live_record.get("documents", [])
        refreshed_documents, summary = recollect_record_results(live_record)
        for index, refreshed in enumerate(refreshed_documents):
            previous = (
                previous_documents[index]
                if index < len(previous_documents)
                and isinstance(previous_documents[index], dict)
                else {}
            )
            for field in ("id", "source_name"):
                if field in previous and field not in refreshed:
                    refreshed[field] = previous[field]
        live_record.update(
            documents=refreshed_documents,
            summary=summary,
            updated_at=now(),
        )
        self.persist(live_record)

    def apply_confirmation_plan(self, job_id: str, plan_id: str, payload: dict) -> dict:
        import phase3_fixed_edges as p3
        from solution_link import validate_link_manifest

        if not isinstance(payload, dict) or set(payload) != {
            "expected_link_manifest_sha256"
        }:
            raise ValueError("apply request fields are invalid")
        expected_source_sha = _require_sha256(
            payload.get("expected_link_manifest_sha256"),
            "expected_link_manifest_sha256",
        )
        with self.lock:
            receipt = self._find_confirmation_receipt(
                job_id, plan_id, expected_source_sha
            )
            if receipt is not None:
                response, _ = receipt
                self._refresh_confirmation_result(job_id)
                return response
            plan = self._confirmation_plans().get(plan_id)
            if not isinstance(plan, dict) or plan.get("job_id") != job_id:
                raise ConfirmationConflict("confirmation plan is missing; create a new preview")
            if float(plan.get("expires_at_epoch", 0)) <= time.time():
                self._confirmation_plans().pop(plan_id, None)
                raise ConfirmationConflict("confirmation plan expired; create a new preview")
            if expected_source_sha != plan["bindings"]["link_manifest_sha256"]:
                raise ConfirmationConflict("apply source does not match the preview")
            doc_index = int(plan["doc"])
            record, document, run_dir = self._confirmation_context(job_id, doc_index)
            if str(run_dir) != plan["run_dir"]:
                raise ConfirmationConflict("document run identity changed")
            _recover_stale_confirmation_source(
                run_dir / "solutions" / "link_manifest.json", expected_source_sha
            )
            sidecar_path, manifest, link_sha, snapshot, snapshot_sha = (
                _read_phase3_state(run_dir)
            )
            if link_sha != expected_source_sha:
                raise ConfirmationConflict("link manifest changed; create a new preview")
            fixed_plan = _effective_fixed_edge_plan(
                snapshot,
                manifest,
                plan["constraints"],
                plan["additions"],
                propagate_scope=bool(plan["selection"]["propagate_scope"]),
            )
            fixed_plan = _current_delta_fixed_edge_plan(fixed_plan, manifest)
            selected_asset_ids = {
                value["asset_id"] for value in plan["additions"]
            }
            selected_assets = {
                value["signature"]["asset_id"]: value
                for value in snapshot["solution_assets"]
                if value["signature"]["asset_id"] in selected_asset_ids
            }
            identity = _confirmation_identity(
                record,
                document,
                job_id=job_id,
                doc_index=doc_index,
                run_dir=run_dir,
            )
            current_bindings = {
                **identity,
                "unit_sha256": p3.problem_identity_sha256(
                    snapshot, plan["selection"]["unit_id"]
                ),
                "selection_sha256": p3.canonical_sha256(plan["selection"]),
                "link_manifest_sha256": link_sha,
                "cache_key_sha256": p3.canonical_sha256(manifest["cache_key"]),
                "snapshot_file_sha256": snapshot_sha,
                "snapshot_payload_sha256": snapshot["payload_sha256"],
                "problem_source_sha256": snapshot["problem_source_sha256"],
                "solution_source_sha256": snapshot["solution_source_sha256"],
                "alignment_index_sha256": snapshot["alignment_index"]["sha256"],
                "versions_sha256": p3.canonical_sha256(snapshot["versions"]),
                "selected_assets": [
                    {
                        "asset_id": asset_id,
                        "identity_sha256": p3.asset_identity_sha256(snapshot, asset_id),
                        "output_sha256": selected_assets[asset_id]["output_sha256"],
                    }
                    for asset_id in sorted(selected_assets)
                ],
            }
            if current_bindings != plan["bindings"]:
                raise ConfirmationConflict("confirmation plan bindings changed")
            response = plan.get("response")
            if not isinstance(response, dict):
                raise ConfirmationConflict("confirmation response binding is missing")
            current_effects_sha = p3.canonical_sha256(fixed_plan["effects"])
            current_counts_sha = p3.canonical_sha256(fixed_plan["counts"])
            current_response = _json_clone(response)
            assert isinstance(current_response, dict)
            current_response["effects"] = _json_clone(fixed_plan["effects"])
            current_response["counts"] = _json_clone(fixed_plan["counts"])
            current_response["manual_item"]["before"] = _json_clone(
                manifest["items"][plan["selection"]["unit_id"]]
            )
            current_response["manual_item"]["after"] = _json_clone(
                next(
                    value["decision"]
                    for value in fixed_plan["prospective_decisions"]
                    if value["unit_id"] == plan["selection"]["unit_id"]
                )
            )
            current_response_sha = _confirmation_response_sha256(current_response)
            digest_payload = {
                **plan["digest_payload"],
                "bindings": current_bindings,
                "fixed_edge_plan_sha256": fixed_plan["plan_sha256"],
                "response_sha256": current_response_sha,
                "effects_sha256": current_effects_sha,
                "counts_sha256": current_counts_sha,
            }
            if (
                fixed_plan["plan_sha256"] != plan["fixed_plan"]["plan_sha256"]
                or current_effects_sha != plan["digest_payload"]["effects_sha256"]
                or current_counts_sha != plan["digest_payload"]["counts_sha256"]
                or current_response_sha != plan["digest_payload"]["response_sha256"]
                or p3.canonical_sha256(digest_payload) != plan["plan_sha256"]
            ):
                raise ConfirmationConflict("confirmation plan digest changed")

            prospective = _json_clone(manifest)
            assert isinstance(prospective, dict)
            prospective["items"] = {
                value["unit_id"]: _json_clone(value["decision"])
                for value in fixed_plan["prospective_decisions"]
            }
            for asset_id, status in fixed_plan["asset_selection_status"].items():
                if asset_id not in prospective["assets"]:
                    raise ConfirmationConflict("planned solution asset disappeared")
                prospective["assets"][asset_id]["selection_status"] = status
            prospective["manual_constraints"] = _json_clone(
                fixed_plan["constraints"]
            )
            applied_at = now()
            prospective["updated_at"] = applied_at
            existing_events = _json_clone(
                prospective.get("manual_confirmation_events", [])
            )
            assert isinstance(existing_events, list)
            prospective["manual_confirmation_events"] = existing_events
            output_payload_sha = p3.canonical_sha256(prospective)
            event_payload = {
                "plan_id": plan_id,
                "plan_sha256": plan["plan_sha256"],
                "confirmation_ids": [
                    value["confirmation_id"] for value in plan["additions"]
                ],
                "source_link_manifest_sha256": link_sha,
                "selection_sha256": current_bindings["selection_sha256"],
                "effects": _json_clone(fixed_plan["effects"]),
                "counts": _json_clone(fixed_plan["counts"]),
                "effects_sha256": current_effects_sha,
                "counts_sha256": current_counts_sha,
                "response_sha256": current_response_sha,
                "provider_calls": 0,
                "applied_at": applied_at,
                "output_payload_sha256": output_payload_sha,
                **identity,
            }
            event = {
                **event_payload,
                "receipt_sha256": p3.canonical_sha256(event_payload),
            }
            prospective["manual_confirmation_events"].append(event)
            try:
                validate_link_manifest(prospective)
            except ValueError as exc:
                raise ConfirmationConflict("prospective link manifest is invalid") from exc
            _validate_confirmation_events(prospective)
            output_sha = _compare_and_swap_link_manifest(
                sidecar_path, link_sha, prospective
            )
            self._confirmation_plans().pop(plan_id, None)
            self._refresh_confirmation_result(job_id)
            return self._receipt_response(
                event,
                link_manifest_sha256=output_sha,
                idempotent=False,
            )

    def refresh_results(self, job_id: str) -> dict:
        record = self.get(job_id)
        documents, summary = recollect_record_results(record)
        return self.update(
            job_id,
            documents=documents,
            summary=summary,
            message="최신 교정·합의 결과를 다시 불러왔습니다.",
        )

    def link_solutions(self, job_id: str, payload: dict) -> dict:
        solution_source = pdf_path(payload.get("source"), required=True)
        assert solution_source is not None
        force = bool(payload.get("force", False))
        with self.lock:
            live_record = self.jobs[job_id]
            if live_record.get("state") != "completed":
                raise ValueError("문제 crop이 완료된 작업에만 해설을 연결할 수 있습니다.")
            current_solution = live_record.get("solution") or {}
            if current_solution.get("state") in {"queued", "indexing", "linking"}:
                raise ValueError("해설 연결이 이미 진행 중입니다.")
            if job_id in self.processes:
                raise ValueError("이전 실행의 종료를 아직 확인하지 못했습니다.")
            selected_crop_dpi = crop_dpi(
                payload.get("crop_dpi") or live_record.get("options", {}).get("crop_dpi")
            )
            options = dict(live_record.get("options") or {})
            options.update(
                solution_source=str(solution_source),
                crop_dpi=selected_crop_dpi,
            )
            live_record["options"] = options
            self.persist(live_record)
            queued = self.update_solution(
                job_id,
                state="queued",
                outcome="pending",
                progress=0,
                message="정답과해설 연결 작업을 준비하고 있습니다.",
                warning_code=None,
                solution_data_available=False,
                explanations_available=False,
                source=str(solution_source),
                source_name=solution_source.name,
                crop_dpi=selected_crop_dpi,
                force=force,
            )
        self.executor.submit(
            self.run_solution_linking,
            job_id,
            solution_source,
            crop_dpi_value=selected_crop_dpi,
            force=force,
        )
        return queued

    def confirm_solution(self, job_id: str, payload: dict) -> dict:
        record = self.get(job_id)
        if record.get("state") != "completed":
            raise ValueError("문제 crop이 완료된 작업에서만 후보를 확정할 수 있습니다.")
        if (record.get("solution") or {}).get("state") in {"queued", "indexing", "linking"}:
            raise ValueError("해설 연결 작업이 끝난 뒤 후보를 확정해주세요.")
        try:
            doc_index = int(payload.get("doc"))
        except (TypeError, ValueError) as exc:
            raise ValueError("문서 번호가 잘못되었습니다.") from exc
        unit_id = str(payload.get("unit_id") or "").strip()
        if not unit_id:
            raise ValueError("문항 ID가 필요합니다.")
        documents = record.get("documents", [])
        if not (0 <= doc_index < len(documents)):
            raise ValueError("문서 번호가 잘못되었습니다.")
        document = documents[doc_index]
        run_value = document.get("run_dir") if isinstance(document, dict) else None
        if not run_value:
            raise ValueError("문항 결과 폴더를 찾을 수 없습니다.")
        run_dir = Path(str(run_value)).resolve()
        sidecar_path = run_dir / "solutions" / "link_manifest.json"
        sidecar = load_optional_json(sidecar_path)
        if isinstance(sidecar, dict) and (
            "alignment_snapshot" in sidecar
            or "manual_propagation_contract" in sidecar
        ):
            try:
                source_sha = _sha256_bytes(sidecar_path.read_bytes())
            except OSError as exc:
                raise ConfirmationConflict("link manifest is unavailable") from exc
            preview = self.create_confirmation_plan(
                job_id,
                {
                    "doc": doc_index,
                    "unit_id": unit_id,
                    "answer_ids": payload.get("answer_ids", []),
                    "explanation_ids": payload.get("explanation_ids", []),
                    "propagate_scope": False,
                    "expected_link_manifest_sha256": source_sha,
                    "expected_cache_key": sidecar.get("cache_key"),
                },
            )
            self.apply_confirmation_plan(
                job_id,
                preview["plan_id"],
                {"expected_link_manifest_sha256": source_sha},
            )
            return self.get(job_id)
        with self.lock:
            confirm_solution_selection(
                run_dir,
                unit_id,
                answer_ids=payload.get("answer_ids", []),
                explanation_ids=payload.get("explanation_ids", []),
            )
            refreshed_documents, summary = recollect_record_results(self.jobs[job_id])
            live_record = self.jobs[job_id]
            live_record.update(
                documents=refreshed_documents,
                summary=summary,
                updated_at=now(),
            )
            self.persist(live_record)
            return dict(live_record)


def solution_item_payload(
    job_id: str,
    doc_index: int,
    sidecar: object,
    unit_id: str,
) -> dict | None:
    from solution_link import derive_link_statuses

    if not isinstance(sidecar, dict):
        return None
    link_items = sidecar.get("items")
    assets = sidecar.get("assets")
    if not isinstance(link_items, dict) or not isinstance(assets, dict):
        return None
    link = link_items.get(unit_id)
    if not isinstance(link, dict):
        return {
            "status": "unmatched",
            "answer_status": "unmatched",
            "explanation_status": "unmatched",
            "overall_status": "unmatched",
            "answers": [],
            "explanations": [],
            "candidates": [],
            "solution_printed_pages": [],
            "confidence": 0,
            "evidence": [],
            "review_reasons": ["해설 연결 결과가 없습니다."],
        }

    missing_ids: list[str] = []

    def linked_assets(field: str) -> list[dict]:
        result: list[dict] = []
        for raw_id in link.get(field, []):
            asset_id = str(raw_id)
            asset = assets.get(asset_id)
            if not isinstance(asset, dict) or not asset.get("output_file"):
                missing_ids.append(asset_id)
                continue
            parts = asset.get("parts") if isinstance(asset.get("parts"), list) else []
            result.append(
                {
                    "id": asset_id,
                    "kind": asset.get("kind"),
                    "confidence": asset.get("confidence"),
                    "selection_status": asset.get("selection_status"),
                    "pdf_pages": [part.get("pdf_page") for part in parts if isinstance(part, dict) and part.get("pdf_page") is not None],
                    "printed_pages": [part.get("printed_page") for part in parts if isinstance(part, dict) and part.get("printed_page") is not None],
                    "image_url": f"/api/jobs/{quote(job_id, safe='')}/solution-asset?doc={doc_index}&id={quote(asset_id, safe='')}",
                }
            )
        return result

    answers = linked_assets("answer_ids")
    explanations = linked_assets("explanation_ids")
    candidates = linked_assets("candidate_ids")
    review_reasons = [str(reason) for reason in link.get("review_reasons", [])]
    if missing_ids:
        review_reasons.append("등록된 해설 asset을 찾을 수 없습니다: " + ", ".join(missing_ids))
    status = str(link.get("status") or "unmatched")
    if status not in {"verified", "review", "unmatched", "conflict"} or missing_ids:
        status = "review"
    status_source = {
        **link,
        "status": status,
        "answer_ids": [asset["id"] for asset in answers],
        "explanation_ids": [asset["id"] for asset in explanations],
        "candidate_ids": [asset["id"] for asset in candidates],
    }
    role_statuses = derive_link_statuses(status_source, assets)
    return {
        "status": status,
        **role_statuses,
        "answers": answers,
        "explanations": explanations,
        "candidates": candidates,
        "solution_printed_pages": link.get("solution_printed_pages", []),
        "confidence": link.get("confidence"),
        "evidence": link.get("evidence", []),
        "review_reasons": review_reasons,
    }


def confirm_solution_selection(
    run_dir: Path,
    unit_id: str,
    *,
    answer_ids: object,
    explanation_ids: object,
) -> dict:
    """Promote only rendered candidates explicitly selected by the user."""

    from solution_link import derive_link_statuses, validate_link_manifest

    sidecar_path = run_dir / "solutions" / "link_manifest.json"
    sidecar = load_optional_json(sidecar_path)
    if not isinstance(sidecar, dict):
        raise ValueError("해설 연결 결과가 없습니다.")
    items = sidecar.get("items")
    assets = sidecar.get("assets")
    if not isinstance(items, dict) or not isinstance(assets, dict):
        raise ValueError("해설 연결 결과가 손상되었습니다.")
    link = items.get(unit_id)
    if not isinstance(link, dict):
        raise ValueError("해당 문항의 해설 연결 결과가 없습니다.")
    existing_answers = [str(asset_id) for asset_id in link.get("answer_ids", [])]
    existing_explanations = [
        str(asset_id) for asset_id in link.get("explanation_ids", [])
    ]
    existing_selected = {*existing_answers, *existing_explanations}
    candidates = {str(asset_id) for asset_id in link.get("candidate_ids", [])}
    pending_candidates = {
        asset_id
        for asset_id in candidates - existing_selected
        if isinstance(assets.get(asset_id), dict)
        and (
            not existing_answers
            and assets[asset_id].get("kind") in {"quick_answer", "mixed"}
            or not existing_explanations
            and assets[asset_id].get("kind") in {"detailed_solution", "mixed"}
        )
    }
    current_statuses = derive_link_statuses(link, assets)
    incremental_partial = (
        link.get("status") == "verified"
        and current_statuses["overall_status"] == "partial"
        and bool(pending_candidates)
    )
    if link.get("status") not in {"review", "conflict"} and not incremental_partial:
        raise ValueError(
            "검토·충돌 또는 남은 후보가 있는 일부 연결 문항만 수동 확정할 수 있습니다."
        )

    def normalize_ids(value: object, field: str) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
            raise ValueError(f"{field}는 asset ID 배열이어야 합니다.")
        if len(value) != len(set(value)):
            raise ValueError(f"{field}에 중복 asset ID가 있습니다.")
        return value

    requested_answers = normalize_ids(answer_ids, "answer_ids")
    requested_explanations = normalize_ids(explanation_ids, "explanation_ids")
    requested = requested_answers + requested_explanations
    if not requested:
        raise ValueError("확정할 답 또는 해설 후보를 하나 이상 선택해주세요.")
    if any(asset_id not in candidates for asset_id in requested):
        raise ValueError("이 문항의 검토 후보가 아닌 asset은 확정할 수 없습니다.")
    selected_answers = (
        requested_answers
        if requested_answers or not incremental_partial
        else existing_answers
    )
    selected_explanations = (
        requested_explanations
        if requested_explanations or not incremental_partial
        else existing_explanations
    )
    selected = selected_answers + selected_explanations

    allowed_roles = {
        "answer_ids": {"quick_answer", "mixed"},
        "explanation_ids": {"detailed_solution", "mixed"},
    }
    for field, asset_ids in (
        ("answer_ids", selected_answers),
        ("explanation_ids", selected_explanations),
    ):
        for asset_id in asset_ids:
            asset = assets.get(asset_id)
            if not isinstance(asset, dict) or asset.get("kind") not in allowed_roles[field]:
                raise ValueError("선택한 후보의 답·해설 역할이 올바르지 않습니다.")
            resolve_solution_asset(run_dir, asset_id)
            if asset_id in requested and asset_id not in existing_selected:
                asset["selection_status"] = "manual_verified"

    printed_pages = sorted(
        {
            int(part["printed_page"])
            for asset_id in selected
            for part in assets[asset_id].get("parts", [])
            if isinstance(part, dict) and part.get("printed_page") is not None
        }
    )
    evidence = [str(value) for value in link.get("evidence", [])]
    if "manual_confirmation" not in evidence:
        evidence.append("manual_confirmation")
    link.update(
        status="verified",
        answer_ids=selected_answers,
        explanation_ids=selected_explanations,
        solution_printed_pages=printed_pages,
        evidence=evidence,
        review_reasons=[],
        confirmation={"method": "manual", "confirmed_at": now()},
    )
    link.update(derive_link_statuses(link, assets))
    sidecar["updated_at"] = now()
    validate_link_manifest(sidecar)
    write_json(sidecar_path, sidecar)
    return dict(link)


def resolve_solution_asset(run_dir: Path, asset_id: str) -> tuple[Path, dict]:
    sidecar = load_optional_json(run_dir / "solutions" / "link_manifest.json")
    if not isinstance(sidecar, dict) or not isinstance(sidecar.get("assets"), dict):
        raise ValueError("해설 연결 결과가 없습니다.")
    asset = sidecar["assets"].get(asset_id)
    if not isinstance(asset, dict) or not asset.get("output_file"):
        raise ValueError("등록된 해설 asset을 찾을 수 없습니다.")
    relative = Path(str(asset["output_file"]))
    if relative.is_absolute():
        raise ValueError("해설 asset 경로가 잘못되었습니다.")
    solution_root = (run_dir / "solutions").resolve()
    target = (
        run_dir / relative
        if relative.parts and relative.parts[0].lower() == "solutions"
        else solution_root / relative
    ).resolve()
    if not target.is_relative_to(solution_root) or not target.is_file():
        raise ValueError("해설 asset 파일을 찾을 수 없습니다.")
    return target, asset


def collect_results(
    job_id: str,
    run_dirs: list[Path],
    *,
    ensemble_required: bool = False,
    ensemble_failed: set[Path] | None = None,
    include_solutions: bool = True,
    provider_profile: str = "full",
) -> tuple[list[dict], dict]:
    documents: list[dict] = []
    summary = {"pages": 0, "crops": 0, "passed": 0, "review": 0, "failed": 0}
    failed_dirs = ensemble_failed or set()
    for doc_index, run_dir in enumerate(run_dirs):
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        ensemble_summary = (
            load_optional_json(run_dir / "ensemble" / "summary.json")
            if provider_profile == "full"
            else None
        )
        ensemble_items = {
            item.get("unit_id"): item
            for item in (ensemble_summary.get("items", []) if isinstance(ensemble_summary, dict) else [])
            if isinstance(item, dict) and item.get("unit_id")
        }
        solution_sidecar = (
            load_optional_json(run_dir / "solutions" / "link_manifest.json")
            if include_solutions
            else None
        )
        if isinstance(solution_sidecar, dict) and solution_sidecar.get(
            "provider_profile", "full"
        ) != provider_profile:
            solution_sidecar = None
        items: list[dict] = []
        for item in manifest.get("items", []):
            verification = item.get("verification") or {}
            reasons = verification.get("reasons") or item.get("review_reasons", [])
            warnings: list[str] = []
            verdict = verification.get("verdict") or ("review" if item.get("review_reasons") else "pass")
            if ensemble_required:
                consensus = ensemble_items.get(item.get("unit_id"))
                if consensus:
                    consensus_verdict = consensus.get("verdict")
                    verdict = consensus_verdict if consensus_verdict in {"pass", "review", "fail"} else "review"
                    reasons = consensus.get("reasons") or reasons
                    warnings = [
                        str(warning)
                        for warning in consensus.get("warnings", [])
                        if isinstance(warning, str)
                    ]
                else:
                    verdict = "review"
                    reasons = [
                        "다중 AI 합의 결과가 없습니다."
                        if run_dir.resolve() not in failed_dirs
                        else "다중 AI 합의 실행에 실패했습니다."
                    ]
            summary["passed" if verdict == "pass" else "failed" if verdict == "fail" else "review"] += 1
            relative = str(item["output_file"]).replace("\\", "/")
            items.append(
                {
                    "id": item["unit_id"],
                    "number": item.get("printed_number") or item["unit_id"],
                    "kind": item.get("unit_kind"),
                    "answer_type": item.get("answer_type"),
                    "source_pages": item.get("source_pages", []),
                    "confidence": item.get("confidence"),
                    "verdict": verdict,
                    "reasons": reasons,
                    "warnings": warnings,
                    "solutions": solution_item_payload(
                        job_id,
                        doc_index,
                        solution_sidecar,
                        str(item["unit_id"]),
                    ),
                    "image_url": f"/api/jobs/{quote(job_id)}/asset?doc={doc_index}&path={quote(relative)}",
                }
            )
        overlay_urls = [
            f"/api/jobs/{quote(job_id)}/asset?doc={doc_index}&path={quote(str(path.relative_to(run_dir)).replace(os.sep, '/'))}"
            for path in sorted((run_dir / "overlays").glob("*.jpg"))
        ]
        state_path = run_dir / "run_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        summary["pages"] += len(state.get("processed_pages", []))
        summary["crops"] += len(items)
        documents.append(
            {
                "name": Path(manifest.get("source_pdf", run_dir.name)).name,
                "run_dir": str(run_dir),
                "items": items,
                "overlays": overlay_urls,
                "ensemble": ensemble_summary,
            }
        )
    return documents, summary


def recollect_record_results(
    record: dict,
    *,
    include_solutions: bool | None = None,
) -> tuple[list[dict], dict]:
    run_dirs = [
        Path(document["run_dir"])
        for document in record.get("documents", [])
        if isinstance(document, dict) and document.get("run_dir")
    ]
    if not run_dirs:
        batch_path = Path(record["output_root"]) / "batch_manifest.json"
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        run_dirs = [Path(document["run_dir"]) for document in batch.get("documents", [])]
    if include_solutions is None:
        include_solutions = (
            (record.get("solution") or {}).get("solution_data_available") is not False
        )
    return collect_results(
        record["id"],
        run_dirs,
        ensemble_required=bool(record.get("options", {}).get("ensemble")),
        include_solutions=include_solutions,
        provider_profile=provider_profile(
            record.get("options", {}).get("provider_profile")
        ),
    )


def load_optional_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def build_ensemble_command(run_dir: Path, options: dict[str, object]) -> list[str]:
    command = [str(PYTHON), "-B", "-u", str(ENSEMBLE), str(run_dir)]
    if not options.get("gemini", True):
        command.append("--no-gemini")
    if not options.get("qwen", True):
        command.append("--no-qwen")
    return command


class CropServer(ThreadingHTTPServer):
    store: JobStore


class Handler(BaseHTTPRequestHandler):
    server: CropServer
    server_version = "QuestionCropLocal/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def send_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2 * 1024 * 1024:
            raise ValueError("요청 데이터가 너무 큽니다.")
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def require_local_request(self) -> None:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        origin = self.headers.get("Origin")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise PermissionError("로컬 요청만 허용됩니다.")
        if origin and origin not in ALLOWED_ORIGINS:
            raise PermissionError("허용되지 않은 Origin입니다.")

    def do_OPTIONS(self) -> None:
        try:
            self.require_local_request()
        except PermissionError as exc:
            self.send_json({"error": str(exc)}, 403)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.cors()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
        try:
            self.require_local_request()
            if parsed.path == "/api/health":
                gemini_ready = False
                qwen_ready = False
                self.send_json(
                    {
                        "status": "ok",
                        "engines": {
                            "codex": codex_auth_status(),
                            "gemini": {
                                "available": gemini_ready,
                                "installed": True,
                                "authenticated": gemini_ready,
                                "label": "Gemini API",
                                "detail": "API 키 연결됨" if gemini_ready else "API 키 필요",
                            },
                            "qwen": {
                                "available": qwen_ready,
                                "installed": True,
                                "authenticated": qwen_ready,
                                "label": "Qwen 3.7 Plus",
                                "detail": "API 키 연결됨" if qwen_ready else "API 키 필요",
                            },
                        },
                    }
                )
            elif parsed.path == "/api/jobs":
                self.send_json({"jobs": self.server.store.list()})
            elif len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                self.send_json(self.server.store.get(parts[2]))
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "results":
                record = self.server.store.get(parts[2])
                documents, summary = recollect_record_results(record)
                self.send_json({"documents": documents, "summary": summary})
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "asset":
                self.serve_asset(parts[2], parse_qs(parsed.query))
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "solution-asset":
                self.serve_solution_asset(parts[2], parse_qs(parsed.query))
            else:
                self.send_json({"error": "not found"}, 404)
        except PermissionError as exc:
            self.send_json({"error": str(exc)}, 403)
        except KeyError:
            self.send_json({"error": "작업을 찾을 수 없습니다."}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
        try:
            self.require_local_request()
            if parsed.path == "/api/uploads":
                self.handle_upload(parse_qs(parsed.query))
            elif parsed.path == "/api/jobs":
                if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                    raise ValueError("application/json 요청만 허용됩니다.")
                self.send_json(self.server.store.create(self.read_json()), 201)
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "resume":
                if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                    raise ValueError("application/json 요청만 허용됩니다.")
                self.send_json(self.server.store.resume(parts[2], self.read_json()), 202)
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "recovery-resume":
                if not self.headers.get('Content-Type', '').lower().startswith('application/json'):
                    raise ValueError('application/json required')
                try:
                    self.send_json(self.server.store.recovery_resume(parts[2], self.read_json()), 202)
                except recovery_receipt.RecoveryConflict as exc:
                    self.send_json({'error': str(exc)}, 409)
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
                self.send_json(self.server.store.cancel(parts[2]))
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "refresh":
                self.send_json(self.server.store.refresh_results(parts[2]))
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "solutions":
                if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                    raise ValueError("application/json 요청만 허용됩니다.")
                self.send_json(self.server.store.link_solutions(parts[2], self.read_json()), 202)
            elif len(parts) == 5 and parts[:2] == ["api", "jobs"] and parts[3:] == ["solutions", "confirmation-plans"]:
                if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                    raise ValueError("application/json requests only")
                self.send_json(
                    self.server.store.create_confirmation_plan(parts[2], self.read_json()),
                    201,
                )
            elif (
                len(parts) == 7
                and parts[:2] == ["api", "jobs"]
                and parts[3:5] == ["solutions", "confirmation-plans"]
                and parts[6] == "apply"
            ):
                if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                    raise ValueError("application/json requests only")
                self.send_json(
                    self.server.store.apply_confirmation_plan(parts[2], parts[5], self.read_json())
                )
            elif len(parts) == 5 and parts[:2] == ["api", "jobs"] and parts[3:] == ["solutions", "confirm"]:
                if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                    raise ValueError("application/json 요청만 허용됩니다.")
                self.send_json(self.server.store.confirm_solution(parts[2], self.read_json()))
            else:
                self.send_json({"error": "not found"}, 404)
        except PermissionError as exc:
            self.send_json({"error": str(exc)}, 403)
        except ConfirmationConflict as exc:
            self.send_json({"error": str(exc)}, 409)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def handle_upload(self, query: dict[str, list[str]]) -> None:
        filename = safe_name((query.get("filename") or [self.headers.get("X-Filename") or "upload.pdf"])[0])
        if not filename.lower().endswith(".pdf"):
            raise ValueError("PDF 파일만 업로드할 수 있습니다.")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            raise ValueError("파일 크기가 허용 범위를 벗어났습니다.")
        destination = self.server.store.upload_root / f"{uuid.uuid4().hex[:10]}-{filename}"
        temporary = destination.with_suffix(destination.suffix + ".part")
        remaining = length
        magic = b""
        try:
            with temporary.open("xb") as stream:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("업로드가 중간에 종료되었습니다.")
                    magic += chunk[: 5 - len(magic)]
                    stream.write(chunk)
                    remaining -= len(chunk)
            if magic != b"%PDF-":
                raise ValueError("유효한 PDF 파일이 아닙니다.")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        self.send_json({"path": str(destination.resolve()), "name": filename, "size": length}, 201)

    def serve_asset(self, job_id: str, query: dict[str, list[str]]) -> None:
        record = self.server.store.get(job_id)
        doc_index = int((query.get("doc") or ["0"])[0])
        relative = unquote((query.get("path") or [""])[0]).replace("/", os.sep)
        documents = record.get("documents", [])
        if not (0 <= doc_index < len(documents)):
            raise ValueError("문서 번호가 잘못되었습니다.")
        root = Path(documents[doc_index]["run_dir"]).resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError("결과 파일을 찾을 수 없습니다.")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        size = target.stat().st_size
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with target.open("rb") as stream:
            shutil.copyfileobj(stream, self.wfile)

    def serve_solution_asset(self, job_id: str, query: dict[str, list[str]]) -> None:
        record = self.server.store.get(job_id)
        doc_index = int((query.get("doc") or ["0"])[0])
        asset_id = str((query.get("id") or [""])[0]).strip()
        documents = record.get("documents", [])
        if not asset_id:
            raise ValueError("해설 asset ID가 필요합니다.")
        if not (0 <= doc_index < len(documents)):
            raise ValueError("문서 번호가 잘못되었습니다.")
        run_dir = Path(documents[doc_index]["run_dir"]).resolve()
        target, _ = resolve_solution_asset(run_dir, asset_id)
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        size = target.stat().st_size
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with target.open("rb") as stream:
            shutil.copyfileobj(stream, self.wfile)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Question Crop local API")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1"])
    parser.add_argument("--port", type=int, default=ASTRA.crop_port)
    args = parser.parse_args(argv)
    validate_parsed_paths(args)
    server = CropServer((args.host, args.port), Handler)
    try:
        server.store = JobStore()
        print(f"Question Crop local API: http://{args.host}:{args.port}")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
