from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

import solution_runtime as sr
from astra_runtime import inside, validate_parsed_paths
from solution_link import LinkContext


PLAN_KIND = "phase2c-a-page-enrichment-plan"
PLAN_SCHEMA_VERSION = 1
STATE_KIND = "phase2c-a-page-enrichment-state"
STATE_SCHEMA_VERSION = 1
ENRICHMENT_ENGINE_VERSION = "2"
DETECTION_ROOT = Path("solutions/detections/solution-blocks")
SOLUTION_SNAPSHOT = Path("inputs/approved-solution.pdf")


class EnrichmentError(ValueError):
    pass


class ProviderBudgetExhausted(RuntimeError):
    pass


def _reject_reparse(path: Path) -> None:
    """Reject symlinks and Windows junction/reparse points in any existing component."""

    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    components = absolute.parts[1:] if absolute.anchor else absolute.parts
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for component in components:
        current /= component
        if not current.exists() and not current.is_symlink():
            break
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise EnrichmentError(f"cannot inspect path component: {current}") from exc
        if current.is_symlink() or (
            getattr(info, "st_file_attributes", 0) & reparse_flag
        ):
            raise EnrichmentError(f"reparse path is not allowed: {current}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnrichmentError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise EnrichmentError(f"JSON root is not an object: {path}")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    inside(path.resolve(), "output")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _cache_inventory(source_leaf: Path) -> dict[str, Any]:
    root = source_leaf / DETECTION_ROOT
    _reject_reparse(root)
    files = sorted(
        (
            *root.glob("raw/*/page-*.json"),
            *root.glob("consensus/page-*.json"),
        ),
        key=lambda path: path.relative_to(source_leaf).as_posix(),
    )
    digest = hashlib.sha256()
    for path in files:
        _reject_reparse(path)
        relative = path.relative_to(source_leaf).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(path).encode("ascii"))
        digest.update(b"\0")
    return {"file_count": len(files), "sha256": digest.hexdigest()}


def _page_result(path: Path, page_index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    wrapper = _json(path)
    meta = wrapper.get("meta")
    result = wrapper.get("result")
    if not isinstance(meta, dict) or not isinstance(result, dict):
        raise EnrichmentError(f"invalid cached wrapper: {path}")
    try:
        schema = json.loads(sr.BLOCK_SCHEMA.read_text(encoding="utf-8"))
        sr.validate_contract(result, schema)
    except (OSError, json.JSONDecodeError, sr.ContractError) as exc:
        raise EnrichmentError(f"invalid cached block contract: {path}") from exc
    if (
        result.get("solution_pdf_index") != page_index
        or result.get("solution_pdf_page") != page_index + 1
    ):
        raise EnrichmentError(f"cached page identity mismatch: {path}")
    return meta, result


def completed_page(run_dir: Path, page_index: int) -> dict[str, Any] | None:
    root = run_dir / DETECTION_ROOT
    consensus_path = root / "consensus" / f"page-{page_index + 1:04d}.json"
    try:
        meta, consensus = _page_result(consensus_path, page_index)
        if meta.get("providers") == ["astra"]:
            raw_path = root / "raw" / "codex" / f"page-{page_index + 1:04d}.json"
            raw_meta, raw = _page_result(raw_path, page_index)
            if meta.get("model") != sr.astra_cli.MODEL or meta.get("policy_version") != sr.astra_cli.POLICY_VERSION or meta.get("complete_primary_agreement") is not False or meta.get("raw_sha256") != _sha256(raw_path) or raw_meta.get("policy_version") != sr.astra_cli.POLICY_VERSION:
                return None
            expected = dict(raw)
            expected["warnings"] = list(dict.fromkeys(list(raw.get("warnings", [])) + ["astra_current_evidence_authority_pending"]))
            if expected != consensus:
                return None
            return {"pdf_index": page_index, "pdf_page": page_index + 1, "mode": "astra_analysis", "consensus_sha256": _sha256(consensus_path)}
    except EnrichmentError:
        return None
    try:
        meta, consensus = _page_result(consensus_path, page_index)
        _codex_meta, codex = _page_result(
            root / "raw" / "codex" / f"page-{page_index + 1:04d}.json",
            page_index,
        )
        _gemini_meta, gemini = _page_result(
            root / "raw" / "gemini" / f"page-{page_index + 1:04d}.json",
            page_index,
        )
    except EnrichmentError:
        return None

    providers = meta.get("providers")
    if not isinstance(providers, list) or not {"codex", "gemini"}.issubset(providers):
        return None
    primary_agreement = meta.get("complete_primary_agreement") is True
    if primary_agreement:
        if not sr.SolutionRuntimeAdapter._solution_page_metadata_agrees(codex, gemini):
            return None
        proposals = sr._normalize_provider_coordinates(
            (("codex", codex), ("gemini", gemini))
        )
        canonical, canonical_complete = sr._canonical_solution_result(
            page_index, proposals
        )
        if not canonical_complete or canonical != consensus:
            return None
        mode = "primary_agreement"
    else:
        if "qwen" not in providers:
            return None
        try:
            _qwen_meta, qwen = _page_result(
                root / "raw" / "qwen" / f"page-{page_index + 1:04d}.json",
                page_index,
            )
        except EnrichmentError:
            return None
        primary = sr._normalize_provider_coordinates(
            (("codex", codex), ("gemini", gemini))
        )
        qwen = sr._inherit_agreed_primary_scope(qwen, primary)
        proposals = sr._normalize_provider_coordinates((*primary, ("qwen", qwen)))
        _blocks, used, complete, _review_count, omitted = sr._consensus_blocks(
            proposals
        )
        canonical, canonical_complete = sr._canonical_solution_result(
            page_index, proposals
        )
        if (
            not complete
            or omitted
            or not canonical_complete
            or canonical != consensus
            or not any(len(key) > 1 and key[1] == "qwen" for key in used)
        ):
            return None
        mode = "qwen_arbitration"
    return {
        "pdf_index": page_index,
        "pdf_page": page_index + 1,
        "mode": mode,
        "consensus_sha256": _sha256(consensus_path),
    }


def analyzed_source_page(source_leaf: Path, page_index: int) -> bool:
    """Existing REVIEW consensus still counts as analyzed source coverage.

    Strict primary/Qwen completion is required only for pages produced in the
    new overlay. Re-running the older 1..51 REVIEW caches would change the
    approved Phase 2c-A workload and is outside this enrichment stage.
    """

    path = (
        source_leaf
        / DETECTION_ROOT
        / "consensus"
        / f"page-{page_index + 1:04d}.json"
    )
    try:
        meta, _result = _page_result(path, page_index)
    except EnrichmentError:
        return False
    providers = meta.get("providers")
    return isinstance(providers, list) and ({"codex", "gemini"}.issubset(providers) or (providers == ["astra"] and meta.get("model") == sr.astra_cli.MODEL and meta.get("policy_version") == sr.astra_cli.POLICY_VERSION))


def _partial_cache_inventory(
    overlay_dir: Path,
    missing: Sequence[int],
    authorized_cache_keys: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe every partial cache byte and the subset safe to reuse."""

    root = overlay_dir / DETECTION_ROOT
    files: dict[str, str] = {}
    reusable = {provider: [] for provider in ("codex", "gemini", "qwen")}
    authorized: dict[str, set[str]] = {}
    for provider in reusable:
        values = (authorized_cache_keys or {}).get(provider, [])
        if not isinstance(values, list) or not all(
            isinstance(value, str) and value for value in values
        ):
            values = []
        authorized[provider] = set(values)
    completed: list[int] = []
    for page_index in missing:
        for provider in ("codex", "gemini", "qwen"):
            path = root / "raw" / provider / f"page-{page_index + 1:04d}.json"
            if not path.exists():
                continue
            _reject_reparse(path)
            relative = path.relative_to(overlay_dir).as_posix()
            files[relative] = _sha256(path)
            try:
                meta, _result = _page_result(path, page_index)
            except EnrichmentError:
                continue
            if (
                meta.get("provider") == provider
                and isinstance(meta.get("cache_key"), str)
                and meta["cache_key"] in authorized[provider]
            ):
                reusable[provider].append(page_index)
        consensus = root / "consensus" / f"page-{page_index + 1:04d}.json"
        if consensus.exists():
            _reject_reparse(consensus)
            files[consensus.relative_to(overlay_dir).as_posix()] = _sha256(consensus)
        completion = completed_page(overlay_dir, page_index)
        required = {"codex", "gemini"} | (
            {"qwen"}
            if completion is not None and completion["mode"] == "qwen_arbitration"
            else set()
        )
        if completion is not None and all(
            page_index in reusable[provider] for provider in required
        ):
            completed.append(page_index)
    return {
        "files": dict(sorted(files.items())),
        "reusable_raw": reusable,
        "completed_pages": completed,
    }


def _remaining_call_bounds(
    missing: Sequence[int], inventory: Mapping[str, Any]
) -> dict[str, dict[str, int]]:
    files = inventory.get("files")
    reusable = inventory.get("reusable_raw")
    completed = inventory.get("completed_pages")
    if not isinstance(files, dict) or not isinstance(reusable, dict) or not isinstance(
        completed, list
    ):
        raise EnrichmentError("partial cache inventory is invalid")
    for relative, digest in files.items():
        if (
            not isinstance(relative, str)
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise EnrichmentError("partial cache inventory is invalid")
    allowed = set(missing)
    completed_set = set(completed)
    if (
        len(completed_set) != len(completed)
        or not completed_set.issubset(allowed)
        or any(isinstance(value, bool) or not isinstance(value, int) for value in completed)
    ):
        raise EnrichmentError("partial cache completed-page inventory is invalid")
    reusable_sets: dict[str, set[int]] = {}
    for provider in ("codex", "gemini", "qwen"):
        pages = reusable.get(provider)
        if not isinstance(pages, list):
            raise EnrichmentError("partial raw-cache inventory is invalid")
        page_set = set(pages)
        if (
            len(page_set) != len(pages)
            or not page_set.issubset(allowed)
            or any(isinstance(value, bool) or not isinstance(value, int) for value in pages)
        ):
            raise EnrichmentError("partial raw-cache inventory is invalid")
        for page_index in page_set:
            relative = (
                DETECTION_ROOT
                / "raw"
                / provider
                / f"page-{page_index + 1:04d}.json"
            ).as_posix()
            if relative not in files:
                raise EnrichmentError("reusable raw cache is absent from inventory")
        reusable_sets[provider] = page_set
    remaining = allowed - completed_set
    maxima = {
        provider: len(remaining - reusable_sets[provider])
        for provider in ("codex", "gemini", "qwen")
    }
    return {
        provider: {"minimum": 0, "maximum": maxima[provider]}
        for provider in ("codex", "gemini", "qwen")
    } | {
        "total": {"minimum": 0, "maximum": sum(maxima.values())}
    }


def _source_binding(source_leaf: Path) -> tuple[dict[str, Any], list[int]]:
    _reject_reparse(source_leaf)
    manifest_path = source_leaf / "manifest.json"
    run_state_path = source_leaf / "run_state.json"
    page_map_path = source_leaf / "solutions" / "page_map.json"
    link_manifest_path = source_leaf / "solutions" / "link_manifest.json"
    for path in (manifest_path, run_state_path, page_map_path, link_manifest_path):
        _reject_reparse(path)
    manifest = _json(manifest_path)
    run_state = _json(run_state_path)
    page_map = _json(page_map_path)
    link_manifest = _json(link_manifest_path)
    problem_paths = {
        str(manifest.get("source_pdf") or ""),
        str(run_state.get("source_pdf") or ""),
    }
    if len(problem_paths) != 1 or not next(iter(problem_paths)):
        raise EnrichmentError("manifest/run-state problem PDF binding mismatch")
    problem_path = Path(next(iter(problem_paths))).resolve(strict=True)
    _reject_reparse(problem_path)
    problem_sha256 = _sha256(problem_path)
    if {
        str(manifest.get("source_sha256") or ""),
        str(run_state.get("source_sha256") or ""),
    } != {problem_sha256}:
        raise EnrichmentError("problem PDF/manifest/run-state source SHA mismatch")
    solution = link_manifest.get("solution")
    if not isinstance(solution, dict):
        raise EnrichmentError("link manifest has no solution source binding")
    raw_solution_path = solution.get("source_pdf")
    if not isinstance(raw_solution_path, str) or not raw_solution_path.strip():
        raise EnrichmentError("link manifest has no solution PDF path")
    solution_path = Path(raw_solution_path).resolve(strict=True)
    _reject_reparse(solution_path)
    solution_sha256 = _sha256(solution_path)
    recorded_hashes = {
        str(solution.get("source_sha256") or ""),
        str(page_map.get("source_sha256") or ""),
    }
    if recorded_hashes != {solution_sha256}:
        raise EnrichmentError("solution PDF/page-map source SHA mismatch")
    raw_pages = page_map.get("pages")
    if not isinstance(raw_pages, list) or not raw_pages:
        raise EnrichmentError("page map has no pages")
    page_indices: list[int] = []
    for position, page in enumerate(raw_pages):
        if not isinstance(page, dict):
            raise EnrichmentError("page map contains a non-object page")
        pdf_index = page.get("pdf_index")
        pdf_page = page.get("pdf_page")
        if (
            isinstance(pdf_index, bool)
            or not isinstance(pdf_index, int)
            or pdf_index < 0
            or pdf_page != pdf_index + 1
        ):
            raise EnrichmentError(f"invalid page identity at page-map position {position}")
        page_indices.append(pdf_index)
    if page_indices != list(range(len(page_indices))):
        raise EnrichmentError("page map must be contiguous and ordered")
    return (
        {
            "source_leaf": str(source_leaf),
            "problem_pdf": str(problem_path),
            "problem_sha256": problem_sha256,
            "manifest": "manifest.json",
            "manifest_sha256": _sha256(manifest_path),
            "run_state": "run_state.json",
            "run_state_sha256": _sha256(run_state_path),
            "solution_pdf": str(solution_path),
            "solution_sha256": solution_sha256,
            "page_map": "solutions/page_map.json",
            "page_map_sha256": _sha256(page_map_path),
            "link_manifest": "solutions/link_manifest.json",
            "link_manifest_sha256": _sha256(link_manifest_path),
            "detection_cache_inventory": _cache_inventory(source_leaf),
        },
        page_indices,
    )


def _contracts(model: str | None) -> dict[str, Any]:
    return {
        "enrichment_engine_version": ENRICHMENT_ENGINE_VERSION,
        "block_prompt_sha256": _sha256(sr.BLOCK_PROMPT),
        "block_schema_sha256": _sha256(sr.BLOCK_SCHEMA),
        "runtime_version": sr.RUNTIME_VERSION,
        "ai_contract_cache_version": sr.AI_CONTRACT_CACHE_VERSION,
        "boundary_consensus_version": sr.BOUNDARY_CONSENSUS_VERSION,
        "models": {
            "codex": sr.astra_cli.MODEL,
            "gemini": os.environ.get("GEMINI_MODEL", "gemini-3.7-flash"),
            "qwen": os.environ.get("QWEN_MODEL", "qwen3.7-plus"),
        },
    }


def _missing_cache_files(source_leaf: Path, missing: Sequence[int]) -> dict[str, int]:
    root = source_leaf / DETECTION_ROOT
    return {
        provider: sum(
            (
                root
                / ("consensus" if provider == "consensus" else f"raw/{provider}")
                / f"page-{index + 1:04d}.json"
            ).is_file()
            for index in missing
        )
        for provider in ("codex", "gemini", "qwen", "consensus")
    }


def create_preflight(
    source_leaf: Path,
    plan_path: Path,
    overlay_dir: Path,
    *,
    model: str | None = None,
    crop_dpi: int = 450,
) -> dict[str, Any]:
    """Create a provider-zero, source-bound Phase 2c-A plan."""

    _reject_reparse(source_leaf)
    _reject_reparse(plan_path)
    _reject_reparse(overlay_dir)
    source_leaf = source_leaf.resolve(strict=True)
    plan_path = plan_path.resolve(strict=False)
    overlay_dir = inside(overlay_dir.resolve(strict=False), "overlay output")
    if overlay_dir.exists():
        raise EnrichmentError("overlay directory must not exist during preflight")
    if _within(overlay_dir, source_leaf) or _within(source_leaf, overlay_dir):
        raise EnrichmentError("overlay and source run must be disjoint")
    if _within(plan_path, source_leaf) or _within(plan_path, overlay_dir):
        raise EnrichmentError("plan must be outside the source run and overlay")
    if crop_dpi <= 0:
        raise EnrichmentError("crop DPI must be positive")

    source, page_indices = _source_binding(source_leaf)
    missing = [index for index in page_indices if not analyzed_source_page(source_leaf, index)]
    count = len(missing)
    plan = {
        "kind": PLAN_KIND,
        "schema_version": PLAN_SCHEMA_VERSION,
        "source": source,
        "contracts": _contracts(model),
        "execution": {
            "overlay_dir": str(overlay_dir),
            "crop_dpi": crop_dpi,
            "model": model,
            "force": False,
        },
        "missing_pages": [
            {"pdf_index": index, "pdf_page": index + 1} for index in missing
        ],
        "existing_missing_page_cache_files": _missing_cache_files(
            source_leaf, missing
        ),
        "call_bounds": {
            "codex": {"minimum": count, "maximum": count},
            "gemini": {"minimum": count, "maximum": count},
            "qwen": {"minimum": 0, "maximum": count},
            "total": {"minimum": count * 2, "maximum": count * 3},
        },
        "provider_calls_made_by_preflight": 0,
        "scope_pair_calls": 0,
    }
    _atomic_json(plan_path, plan)
    return {
        "plan": str(plan_path),
        "plan_sha256": _sha256(plan_path),
        "missing_pages": count,
        "call_bounds": plan["call_bounds"],
        "provider_calls": 0,
    }


def _validate_plan(plan: Mapping[str, Any], plan_path: Path) -> tuple[Path, Path, list[int]]:
    if plan.get("kind") != PLAN_KIND or plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise EnrichmentError("unsupported enrichment plan")
    source = plan.get("source")
    contracts = plan.get("contracts")
    execution = plan.get("execution")
    raw_missing = plan.get("missing_pages")
    if not all(isinstance(value, dict) for value in (source, contracts, execution)):
        raise EnrichmentError("plan is missing source/contract/execution binding")
    if not isinstance(raw_missing, list):
        raise EnrichmentError("plan has no missing-page list")
    assert isinstance(source, dict) and isinstance(contracts, dict) and isinstance(execution, dict)
    raw_source_leaf = Path(str(source.get("source_leaf") or ""))
    raw_solution_path = Path(str(source.get("solution_pdf") or ""))
    raw_overlay_dir = Path(str(execution.get("overlay_dir") or ""))
    for path in (plan_path, raw_source_leaf, raw_solution_path, raw_overlay_dir):
        _reject_reparse(path)
    source_leaf = raw_source_leaf.resolve(strict=True)
    solution_path = raw_solution_path.resolve(strict=True)
    overlay_dir = inside(raw_overlay_dir.resolve(strict=False), "overlay output")
    if _within(overlay_dir, source_leaf) or _within(source_leaf, overlay_dir):
        raise EnrichmentError("overlay and source run must be disjoint")
    if _within(plan_path, source_leaf) or _within(plan_path, overlay_dir):
        raise EnrichmentError("plan path is not disjoint from source/overlay")

    current_source, all_pages = _source_binding(source_leaf)
    for field in (
        "problem_pdf",
        "problem_sha256",
        "manifest",
        "manifest_sha256",
        "run_state",
        "run_state_sha256",
        "solution_pdf",
        "solution_sha256",
        "page_map",
        "page_map_sha256",
        "link_manifest",
        "link_manifest_sha256",
        "detection_cache_inventory",
    ):
        if current_source.get(field) != source.get(field):
            raise EnrichmentError(f"source binding changed: {field}")
    if solution_path != Path(current_source["solution_pdf"]):
        raise EnrichmentError("solution path changed")
    expected_contracts = _contracts(execution.get("model"))
    if expected_contracts != contracts:
        raise EnrichmentError("prompt/schema/cache-version/model binding changed")

    missing: list[int] = []
    for entry in raw_missing:
        if not isinstance(entry, dict):
            raise EnrichmentError("missing-page entry is not an object")
        index = entry.get("pdf_index")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index not in all_pages
            or entry.get("pdf_page") != index + 1
        ):
            raise EnrichmentError("invalid missing-page identity")
        missing.append(index)
    if len(set(missing)) != len(missing) or missing != sorted(missing):
        raise EnrichmentError("missing pages must be unique and sorted")
    count = len(missing)
    successor = plan.get("successor")
    if successor is None:
        expected_bounds = {
            "codex": {"minimum": count, "maximum": count},
            "gemini": {"minimum": count, "maximum": count},
            "qwen": {"minimum": 0, "maximum": count},
            "total": {"minimum": count * 2, "maximum": count * 3},
        }
    else:
        if not isinstance(successor, dict) or successor.get("kind") != "retry":
            raise EnrichmentError("successor plan binding is invalid")
        prior_plan_path = Path(str(successor.get("prior_plan_path") or ""))
        prior_plan_sha256 = successor.get("prior_plan_sha256")
        prior_state_sha256 = successor.get("prior_state_sha256")
        if (
            not isinstance(prior_plan_sha256, str)
            or len(prior_plan_sha256) != 64
            or not isinstance(prior_state_sha256, str)
            or len(prior_state_sha256) != 64
        ):
            raise EnrichmentError("successor approval lineage is invalid")
        _reject_reparse(prior_plan_path)
        prior_plan_path = prior_plan_path.resolve(strict=True)
        if (
            prior_plan_path == plan_path
            or _within(prior_plan_path, source_leaf)
            or _within(prior_plan_path, overlay_dir)
        ):
            raise EnrichmentError("prior plan path is not disjoint")
        prior_plan = _approved_plan(prior_plan_path, prior_plan_sha256)
        for field in ("source", "contracts", "execution", "missing_pages"):
            if prior_plan.get(field) != plan.get(field):
                raise EnrichmentError(f"successor changed prior {field} binding")
        expected_bounds = _remaining_call_bounds(
            missing, successor.get("partial_cache_inventory", {})
        )
    if plan.get("call_bounds") != expected_bounds or plan.get("scope_pair_calls") != 0:
        raise EnrichmentError("call bounds do not match the page-only plan")
    if plan.get("existing_missing_page_cache_files") != _missing_cache_files(
        source_leaf, missing
    ):
        raise EnrichmentError("missing-page cache inventory changed")
    return source_leaf, overlay_dir, missing


def _state_path(overlay_dir: Path) -> Path:
    return overlay_dir / "enrichment_state.json"


def _validate_solution_snapshot(
    overlay_dir: Path, expected_sha256: str
) -> Path:
    state = _json(_state_path(overlay_dir))
    expected_record = {
        "relative_path": SOLUTION_SNAPSHOT.as_posix(),
        "sha256": expected_sha256,
    }
    if state.get("solution_snapshot") != expected_record:
        raise EnrichmentError("approved solution snapshot binding is invalid")
    snapshot = overlay_dir / SOLUTION_SNAPSHOT
    _reject_reparse(snapshot)
    if not snapshot.is_file() or _sha256(snapshot) != expected_sha256:
        raise EnrichmentError("approved solution snapshot was modified")
    return snapshot


def _ensure_solution_snapshot(
    overlay_dir: Path,
    source_path: Path,
    expected_sha256: str,
    plan_sha256: str,
) -> Path:
    """Atomically freeze the approved PDF before any text or render access."""

    _reject_reparse(source_path)
    snapshot = overlay_dir / SOLUTION_SNAPSHOT
    _reject_reparse(snapshot)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    _reject_reparse(snapshot.parent)
    if not snapshot.exists():
        temporary: Path | None = None
        try:
            with source_path.open("rb") as source, tempfile.NamedTemporaryFile(
                "wb",
                dir=snapshot.parent,
                prefix=snapshot.name + ".",
                suffix=".tmp",
                delete=False,
            ) as destination:
                temporary = Path(destination.name)
                shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
            if _sha256(temporary) != expected_sha256:
                raise EnrichmentError("approved solution PDF changed during snapshot copy")
            os.replace(temporary, snapshot)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    if _sha256(snapshot) != expected_sha256:
        raise EnrichmentError("approved solution snapshot SHA mismatch")
    _reject_reparse(source_path)
    if _sha256(source_path) != expected_sha256:
        raise EnrichmentError("approved solution PDF changed during snapshot copy")
    state_path = _state_path(overlay_dir)
    state = _json(state_path)
    if state.get("plan_sha256") != plan_sha256:
        raise EnrichmentError("overlay state changed during solution snapshot copy")
    record = {
        "relative_path": SOLUTION_SNAPSHOT.as_posix(),
        "sha256": expected_sha256,
    }
    recorded = state.get("solution_snapshot")
    if recorded not in (None, {}) and recorded != record:
        raise EnrichmentError("overlay has a different approved solution snapshot")
    state["solution_snapshot"] = record
    _atomic_json(state_path, state)
    return _validate_solution_snapshot(overlay_dir, expected_sha256)


def _approved_plan(plan_path: Path, expected_sha256: str) -> dict[str, Any]:
    """Hash and parse the exact same read; never reopen between approval and parse."""

    _reject_reparse(plan_path)
    try:
        payload = plan_path.read_bytes()
    except OSError as exc:
        raise EnrichmentError(f"cannot read approved plan: {plan_path}") from exc
    actual = hashlib.sha256(payload).hexdigest()
    if len(expected_sha256) != 64 or not hmac.compare_digest(actual, expected_sha256):
        raise EnrichmentError("plan SHA mismatch")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnrichmentError("approved plan is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise EnrichmentError("approved plan root is not an object")
    return value


def _read_bound_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    _reject_reparse(path)
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnrichmentError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise EnrichmentError(f"{label} root is not an object")
    return value, hashlib.sha256(payload).hexdigest()


def create_successor_preflight(
    prior_plan_path: Path,
    prior_plan_sha256: str,
    plan_path: Path,
) -> dict[str, Any]:
    """Create a provider-zero retry approval for one frozen partial overlay."""

    _reject_reparse(prior_plan_path)
    _reject_reparse(plan_path)
    prior_plan_path = prior_plan_path.resolve(strict=True)
    plan_path = plan_path.resolve(strict=False)
    prior = _approved_plan(prior_plan_path, prior_plan_sha256)
    source_leaf, overlay_dir, missing = _validate_plan(prior, prior_plan_path)
    if (
        plan_path == prior_plan_path
        or _within(plan_path, source_leaf)
        or _within(plan_path, overlay_dir)
    ):
        raise EnrichmentError("successor plan path is not disjoint")
    state_path = _state_path(overlay_dir)
    if not state_path.is_file():
        raise EnrichmentError("partial overlay has no enrichment state")
    state, state_sha256 = _read_bound_json(state_path, "prior enrichment state")
    if (
        state.get("kind") != STATE_KIND
        or state.get("schema_version") != STATE_SCHEMA_VERSION
        or state.get("plan_sha256") != prior_plan_sha256
    ):
        raise EnrichmentError("partial overlay does not belong to the prior plan")
    _validate_baseline(source_leaf, overlay_dir, missing)
    _validate_solution_snapshot(
        overlay_dir, str(prior["source"]["solution_sha256"])
    )
    inventory = _partial_cache_inventory(
        overlay_dir, missing, state.get("authorized_cache_keys")
    )
    _state_again, state_sha256_again = _read_bound_json(
        state_path, "prior enrichment state"
    )
    inventory_again = _partial_cache_inventory(
        overlay_dir, missing, state.get("authorized_cache_keys")
    )
    if state_sha256_again != state_sha256 or inventory_again != inventory:
        raise EnrichmentError("partial overlay changed during successor preflight")
    if not state.get("remaining_pages") and inventory["completed_pages"] == list(missing):
        raise EnrichmentError("completed overlay does not need a successor plan")

    successor = {
        "kind": "retry",
        "prior_plan_path": str(prior_plan_path),
        "prior_plan_sha256": prior_plan_sha256,
        "prior_state_sha256": state_sha256,
        "partial_cache_inventory": inventory,
    }
    plan = {
        "kind": PLAN_KIND,
        "schema_version": PLAN_SCHEMA_VERSION,
        "source": prior["source"],
        "contracts": prior["contracts"],
        "execution": prior["execution"],
        "missing_pages": prior["missing_pages"],
        "existing_missing_page_cache_files": prior[
            "existing_missing_page_cache_files"
        ],
        "call_bounds": _remaining_call_bounds(missing, inventory),
        "provider_calls_made_by_preflight": 0,
        "scope_pair_calls": 0,
        "successor": successor,
    }
    _atomic_json(plan_path, plan)
    _validate_plan(plan, plan_path)
    new_sha256 = _sha256(plan_path)
    if hmac.compare_digest(new_sha256, prior_plan_sha256):
        raise EnrichmentError("successor plan did not create a new approval SHA")
    return {
        "plan": str(plan_path),
        "plan_sha256": new_sha256,
        "prior_plan_sha256": prior_plan_sha256,
        "remaining_pages": len(missing) - len(inventory["completed_pages"]),
        "call_bounds": plan["call_bounds"],
        "adopted_partial_cache_files": len(inventory["files"]),
        "provider_calls": 0,
    }


def _baseline_sources(
    source_leaf: Path, missing: Sequence[int]
) -> dict[str, Path]:
    page_map = _json(source_leaf / "solutions" / "page_map.json")
    pages = page_map.get("pages")
    if not isinstance(pages, list):
        raise EnrichmentError("source page map has no pages")
    missing_set = set(missing)
    sources = {"solutions/page_map.json": source_leaf / "solutions" / "page_map.json"}
    root = source_leaf / DETECTION_ROOT
    for page in pages:
        if not isinstance(page, dict) or page.get("pdf_index") in missing_set:
            continue
        index = int(page["pdf_index"])
        relative_paths = [
            DETECTION_ROOT / "raw" / provider / f"page-{index + 1:04d}.json"
            for provider in ("codex", "gemini")
        ] + [DETECTION_ROOT / "consensus" / f"page-{index + 1:04d}.json"]
        optional_qwen = (
            DETECTION_ROOT / "raw" / "qwen" / f"page-{index + 1:04d}.json"
        )
        if (source_leaf / optional_qwen).is_file():
            relative_paths.append(optional_qwen)
        for relative in relative_paths:
            source = source_leaf / relative
            _reject_reparse(source)
            if not source.is_file():
                raise EnrichmentError(f"analyzed baseline file is missing: {source}")
            sources[relative.as_posix()] = source
    return dict(sorted(sources.items()))


def _ensure_baseline(
    source_leaf: Path,
    overlay_dir: Path,
    missing: Sequence[int],
    plan_sha256: str,
) -> None:
    sources = _baseline_sources(source_leaf, missing)
    expected = {relative: _sha256(source) for relative, source in sources.items()}
    state_path = _state_path(overlay_dir)
    state = _json(state_path)
    recorded = state.get("baseline_files")
    if recorded not in (None, {}) and recorded != expected:
        raise EnrichmentError("overlay baseline belongs to different source bytes")
    for relative, source in sources.items():
        destination = overlay_dir / Path(relative)
        _reject_reparse(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _reject_reparse(destination.parent)
        if destination.exists():
            if not destination.is_file() or _sha256(destination) != expected[relative]:
                raise EnrichmentError(f"overlay baseline was modified: {relative}")
            continue
        temporary = destination.with_name(destination.name + ".baseline.tmp")
        _reject_reparse(temporary)
        shutil.copyfile(source, temporary)
        if _sha256(temporary) != expected[relative]:
            temporary.unlink(missing_ok=True)
            raise EnrichmentError(f"baseline copy hash mismatch: {relative}")
        os.replace(temporary, destination)
    state = _json(state_path)
    if state.get("plan_sha256") != plan_sha256:
        raise EnrichmentError("overlay state changed during baseline copy")
    state["baseline_files"] = expected
    _atomic_json(state_path, state)


def _validate_baseline(
    source_leaf: Path, overlay_dir: Path, missing: Sequence[int]
) -> None:
    _reject_reparse(overlay_dir)
    sources = _baseline_sources(source_leaf, missing)
    expected = {relative: _sha256(source) for relative, source in sources.items()}
    state = _json(_state_path(overlay_dir))
    if state.get("baseline_files") != expected:
        raise EnrichmentError("overlay baseline inventory mismatch")
    for relative, source in sources.items():
        destination = overlay_dir / Path(relative)
        _reject_reparse(destination)
        if (
            not destination.is_file()
            or _sha256(destination) != expected[relative]
            or destination.read_bytes() != source.read_bytes()
        ):
            raise EnrichmentError(f"overlay baseline differs from source: {relative}")


def _write_state(
    overlay_dir: Path,
    plan_sha256: str,
    missing: Sequence[int],
    errors: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    state_path = _state_path(overlay_dir)
    prior = _json(state_path) if state_path.is_file() else {}
    completed = [
        value
        for index in missing
        if (value := completed_page(overlay_dir, index)) is not None
    ]
    completed_indices = {entry["pdf_index"] for entry in completed}
    state = {
        "kind": STATE_KIND,
        "schema_version": STATE_SCHEMA_VERSION,
        "plan_sha256": plan_sha256,
        "provider_attempts": prior.get(
            "provider_attempts", {"codex": 0, "gemini": 0, "qwen": 0}
        ),
        "baseline_files": prior.get("baseline_files", {}),
        "solution_snapshot": prior.get("solution_snapshot"),
        "successor": prior.get("successor"),
        "plan_history": prior.get("plan_history", []),
        "authorized_cache_keys": prior.get(
            "authorized_cache_keys", {"codex": [], "gemini": [], "qwen": []}
        ),
        "completed_pages": completed,
        "remaining_pages": [index for index in missing if index not in completed_indices],
        "errors": {
            str(index): message for index, message in sorted((errors or {}).items())
        },
    }
    state["partial_cache_inventory"] = _partial_cache_inventory(
        overlay_dir, missing, state["authorized_cache_keys"]
    )
    _atomic_json(state_path, state)
    return state


def _install_provider_gates(
    adapter: sr.SolutionRuntimeAdapter,
    plan_path: Path,
    plan_sha256: str,
    approved_plan: Mapping[str, Any],
    source_leaf: Path,
    overlay_dir: Path,
    missing: Sequence[int],
) -> dict[str, Any]:
    """Install crash-safe, cumulative call-budget gates on one adapter."""

    lock = threading.Lock()
    originals = {
        "codex": adapter.codex_runner,
        "gemini": adapter.gemini_runner,
        "qwen": adapter.qwen_runner,
    }

    def expected_keys(provider: str, args: tuple[Any, ...], kwargs: dict[str, Any]):
        if provider == "codex":
            stage = str(kwargs.get("stage") or "")
            prompt = str(kwargs.get("prompt") or "")
            schema_path = Path(kwargs.get("schema_path") or sr.BLOCK_SCHEMA)
            images = list(kwargs.get("images") or [])
            model = sr.astra_cli.MODEL
        else:
            if len(args) < 5:
                raise ProviderBudgetExhausted("provider call contract is invalid")
            stage = f"solution-blocks-{provider}"
            prompt = str(args[0])
            schema_path = sr.BLOCK_SCHEMA
            images = list(args[2])
            model = str(args[4])
        if stage != f"solution-blocks-{provider}":
            raise ProviderBudgetExhausted("unexpected enrichment provider stage")
        return sr._contract_cache_keys(stage, prompt, schema_path, images, model)

    def guarded(provider: str, original):
        def run(*args, **kwargs):
            with lock:
                current = _approved_plan(plan_path, plan_sha256)
                if current != approved_plan:
                    raise ProviderBudgetExhausted("approved plan bytes changed")
                current_source, current_overlay, current_missing = _validate_plan(
                    current, plan_path
                )
                if (
                    current_source != source_leaf
                    or current_overlay != overlay_dir
                    or current_missing != list(missing)
                ):
                    raise ProviderBudgetExhausted("approved plan binding changed")
                _validate_baseline(source_leaf, overlay_dir, missing)
                _validate_solution_snapshot(
                    overlay_dir,
                    str(current["source"]["solution_sha256"]),
                )
                state_path = _state_path(overlay_dir)
                state = _json(state_path)
                attempts = state.get("provider_attempts")
                if not isinstance(attempts, dict):
                    raise ProviderBudgetExhausted("provider attempt ledger is invalid")
                maximum = int(current["call_bounds"][provider]["maximum"])
                used = attempts.get(provider)
                if isinstance(used, bool) or not isinstance(used, int) or used < 0:
                    raise ProviderBudgetExhausted("provider attempt ledger is invalid")
                if used >= maximum:
                    raise ProviderBudgetExhausted(
                        f"approved {provider} attempt budget exhausted"
                    )
                authorized = state.get("authorized_cache_keys")
                if not isinstance(authorized, dict) or not isinstance(
                    authorized.get(provider), list
                ):
                    raise ProviderBudgetExhausted(
                        "authorized cache-key ledger is invalid"
                    )
                authorized[provider] = sorted(
                    set(authorized[provider])
                    | set(expected_keys(provider, args, kwargs))
                )
                # Persist before entering the provider. A crash can consume an
                # allowance but can never create an unrecorded/beyond-plan call.
                attempts[provider] = used + 1
                state["provider_attempts"] = attempts
                state["authorized_cache_keys"] = authorized
                _atomic_json(state_path, state)
            return original(*args, **kwargs)

        return run

    adapter.codex_runner = guarded("codex", originals["codex"])
    adapter.gemini_runner = guarded("gemini", originals["gemini"])
    adapter.qwen_runner = guarded("qwen", originals["qwen"])
    return originals


def _restore_provider_runners(
    adapter: sr.SolutionRuntimeAdapter, originals: Mapping[str, Any]
) -> None:
    adapter.codex_runner = originals["codex"]
    adapter.gemini_runner = originals["gemini"]
    adapter.qwen_runner = originals["qwen"]


def _adopt_or_validate_plan_state(
    plan: Mapping[str, Any],
    plan_sha256: str,
    source_leaf: Path,
    overlay_dir: Path,
    missing: Sequence[int],
) -> None:
    state_path = _state_path(overlay_dir)
    successor = plan.get("successor")
    if not overlay_dir.exists():
        if successor is not None:
            raise EnrichmentError("successor plan requires its bound partial overlay")
        overlay_dir.mkdir(parents=True)
        _reject_reparse(overlay_dir)
        _write_state(overlay_dir, plan_sha256, missing)
        return
    if not state_path.is_file():
        raise EnrichmentError("existing overlay is not a resumable enrichment overlay")
    state, state_sha256 = _read_bound_json(state_path, "enrichment state")
    if (
        state.get("kind") != STATE_KIND
        or state.get("schema_version") != STATE_SCHEMA_VERSION
    ):
        raise EnrichmentError("overlay state is invalid")
    if state.get("plan_sha256") == plan_sha256:
        if successor is not None and state.get("successor") != successor:
            raise EnrichmentError("successor state lineage is invalid")
        return
    if not isinstance(successor, dict):
        raise EnrichmentError("overlay state belongs to a different plan")
    if (
        state.get("plan_sha256") != successor.get("prior_plan_sha256")
        or state_sha256 != successor.get("prior_state_sha256")
    ):
        raise EnrichmentError("partial overlay state changed after successor approval")
    _validate_baseline(source_leaf, overlay_dir, missing)
    _validate_solution_snapshot(
        overlay_dir, str(plan["source"]["solution_sha256"])
    )
    if _partial_cache_inventory(
        overlay_dir, missing, state.get("authorized_cache_keys")
    ) != successor.get("partial_cache_inventory"):
        raise EnrichmentError("partial cache inventory changed after successor approval")
    history = state.get("plan_history")
    if not isinstance(history, list):
        raise EnrichmentError("overlay plan history is invalid")
    state["plan_history"] = history + [
        {
            "plan_sha256": successor["prior_plan_sha256"],
            "state_sha256": state_sha256,
            "provider_attempts": state.get("provider_attempts"),
        }
    ]
    state["plan_sha256"] = plan_sha256
    state["provider_attempts"] = {"codex": 0, "gemini": 0, "qwen": 0}
    state["successor"] = successor
    state["partial_cache_inventory"] = successor["partial_cache_inventory"]
    _atomic_json(state_path, state)


def execute_enrichment(
    plan_path: Path,
    plan_sha256: str,
    *,
    allow_provider_calls: bool,
    adapter: sr.SolutionRuntimeAdapter | None = None,
) -> dict[str, Any]:
    """Execute or resume one approved plan in its disjoint overlay."""

    if not allow_provider_calls:
        raise EnrichmentError("explicit --allow-provider-calls is required")
    _reject_reparse(plan_path)
    plan_path = plan_path.resolve(strict=True)
    plan = _approved_plan(plan_path, plan_sha256)
    source_leaf, overlay_dir, missing = _validate_plan(plan, plan_path)
    execution = plan["execution"]
    source_solution_path = Path(plan["source"]["solution_pdf"])

    _adopt_or_validate_plan_state(
        plan, plan_sha256, source_leaf, overlay_dir, missing
    )
    _ensure_baseline(source_leaf, overlay_dir, missing, plan_sha256)
    _validate_baseline(source_leaf, overlay_dir, missing)
    solution_path = _ensure_solution_snapshot(
        overlay_dir,
        source_solution_path,
        str(plan["source"]["solution_sha256"]),
        plan_sha256,
    )

    active_adapter = adapter or sr.SolutionRuntimeAdapter()
    try:
        texts = active_adapter.text_reader(solution_path)
    except Exception as exc:
        raise EnrichmentError(f"cannot read solution PDF: {exc}") from exc
    if len(texts) <= max(missing, default=-1):
        raise EnrichmentError("solution PDF has fewer pages than the approved plan")

    context = LinkContext(
        overlay_dir,
        solution_path,
        int(execution["crop_dpi"]),
        1,
        execution.get("model"),
        False,
    )
    runtime_state = sr.RuntimeState({}, {})
    errors: dict[int, str] = {}
    originals = _install_provider_gates(
        active_adapter,
        plan_path,
        plan_sha256,
        plan,
        source_leaf,
        overlay_dir,
        missing,
    )
    try:
        for page_index in missing:
            if completed_page(overlay_dir, page_index) is not None:
                continue
            try:
                result = active_adapter._detect_solution_block(
                    context,
                    runtime_state,
                    page_index,
                    texts[page_index].text,
                    len(texts),
                )
                if result is None or completed_page(overlay_dir, page_index) is None:
                    errors[page_index] = "current Astra analysis unavailable"
            except Exception as exc:
                errors[page_index] = f"{type(exc).__name__}: {exc}"[:240]
            _write_state(overlay_dir, plan_sha256, missing, errors)
    finally:
        _restore_provider_runners(active_adapter, originals)

    state = _write_state(overlay_dir, plan_sha256, missing, errors)
    # Detect concurrent/source writes before reporting success. Provider output
    # is intentionally confined to overlay_dir.
    final_plan = _approved_plan(plan_path, plan_sha256)
    _validate_plan(final_plan, plan_path)
    _validate_baseline(source_leaf, overlay_dir, missing)
    _validate_solution_snapshot(
        overlay_dir, str(plan["source"]["solution_sha256"])
    )
    attempts = state["provider_attempts"]
    needed = _remaining_call_bounds(
        missing, state["partial_cache_inventory"]
    )
    budget_exhausted = bool(state["remaining_pages"]) and any(
        int(needed[provider]["maximum"])
        > int(plan["call_bounds"][provider]["maximum"])
        - int(attempts[provider])
        for provider in ("codex", "gemini", "qwen")
    )
    return {
        "overlay_dir": str(overlay_dir),
        "plan_sha256": plan_sha256,
        "completed_pages": len(state["completed_pages"]),
        "remaining_pages": state["remaining_pages"],
        "completion_modes": {
            mode: sum(1 for entry in state["completed_pages"] if entry["mode"] == mode)
            for mode in ("astra_analysis", "primary_agreement", "qwen_arbitration")
        },
        "provider_attempts": state["provider_attempts"],
        "source_leaf": str(source_leaf),
        "complete": not state["remaining_pages"],
        "budget_exhausted": budget_exhausted,
        "new_approval_required": budget_exhausted,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 2c-A solution-page enrichment")
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight", help="build a provider-zero plan")
    preflight.add_argument("--source-leaf", type=Path, required=True)
    preflight.add_argument("--plan", type=Path, required=True)
    preflight.add_argument("--overlay-dir", type=Path, required=True)
    preflight.add_argument("--model")
    preflight.add_argument("--crop-dpi", type=int, default=450)
    retry = commands.add_parser(
        "retry-preflight", help="bind a new approval to a partial overlay"
    )
    retry.add_argument("--prior-plan", type=Path, required=True)
    retry.add_argument("--prior-plan-sha", required=True)
    retry.add_argument("--plan", type=Path, required=True)
    enrich = commands.add_parser("enrich", help="execute or resume an approved plan")
    enrich.add_argument("--plan", type=Path, required=True)
    enrich.add_argument("--plan-sha", required=True)
    enrich.add_argument("--allow-provider-calls", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    validate_parsed_paths(arguments, ("plan", "overlay_dir") if arguments.command != "enrich" else ())
    try:
        if arguments.command == "preflight":
            result = create_preflight(
                arguments.source_leaf,
                arguments.plan,
                arguments.overlay_dir,
                model=arguments.model,
                crop_dpi=arguments.crop_dpi,
            )
        elif arguments.command == "retry-preflight":
            result = create_successor_preflight(
                arguments.prior_plan,
                arguments.prior_plan_sha,
                arguments.plan,
            )
        else:
            result = execute_enrichment(
                arguments.plan,
                arguments.plan_sha,
                allow_provider_calls=arguments.allow_provider_calls,
            )
    except EnrichmentError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("complete", True) else 3


if __name__ == "__main__":
    raise SystemExit(main())
