from __future__ import annotations

import astra_cli
from astra_cli import quality
from astra_cli.contracts import JsonMap
from solution_quality import issue_pair, target_for

import hashlib
import base64

import scope_alignment as scope_alignment_mod
import solution_scope as solution_scope_mod
import copy
import json
import mimetypes
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path
from string import Template
from typing import Any, Callable, Mapping, Sequence

import pypdfium2 as pdfium

import ensemble
from answer_cell_geometry import AnswerCell, isolated_answer_cell_bbox
from solution_anchor_inheritance import inherit_missing_anchors
from astra_runtime import inside
import question_crop as qc
from solution_learning.contracts import ExampleKind
from solution_learning.profiles import FormatFeatures
from solution_learning_runtime import LearningPromptRequest, LearningPromptResult, SolutionLearningRuntime
from solution_link import (
    AlignmentSnapshotMetadata,
    AnchorBoundary,
    AnchorSpan,
    CandidatePair,
    ExpandedSourceBinding,
    CORE_VERSION,
    DUAL_VERIFIER,
    _confirmed_verification,
    _strong_verification,
    DUAL_SCOPE_VERIFIER,
    LinkContext,
    LinkDecision,
    LinkStageHooks,
    LinkStatus,
    MIN_LINK_CONFIDENCE,
    NumberedItem,
    PageIdentity,
    PairVerification,
    PrintedPageRange,
    ProblemBoundary,
    ProblemIndex,
    ProblemItem,
    QUICK_ANSWER_FRONT_MATTER_PAGES,
    QUICK_ANSWER_LOOKBACK_PAGES,
    SEARCH_POLICY,
    RangeMode,
    ScopeAlignmentBundle,
    ScopeCandidatePair,
    SolutionAsset,
    SolutionIndex,
    SolutionPart,
    SolutionRole,
    StageHookError,
    StartAnchor,
    assign_numbering_scopes,
    bind_parent_review_authority,
    derive_link_statuses,
    expand_start_anchors,
    expanded_candidate_pairs,
    normalize_parent_number,
    normalize_subquestion_label,
    split_printed_number,
    neighbor_evidence_state,
    page_identity,
    propagate_problem_anchors,
    validate_link_manifest,
)


ROOT = Path(__file__).resolve().parent
HINT_PROMPT = ROOT / "prompts" / "detect_solution_hints.md"
HINT_SCHEMA = ROOT / "solution_hints.schema.json"
BLOCK_PROMPT = ROOT / "prompts" / "detect_solution_blocks.md"
BLOCK_SCHEMA = ROOT / "solution_blocks.schema.json"
QUICK_PROMPT = ROOT / "prompts" / "refine_quick_answers.md"
VERIFY_PROMPT = ROOT / "prompts" / "verify_solution_link.md"
VERIFY_SCHEMA = ROOT / "solution_link_verification.schema.json"
REPAIR_PROMPT = ROOT / "prompts" / "repair_solution_crop.md"
REPAIR_SCHEMA = ROOT / "solution_crop_repair.schema.json"
# Phase 2b: scope-pair verification is a SEPARATE contract with its own cache
# key namespace ("verify-scope-*") and version. It must never be dispatched to
# a provider before the Phase 2c cost approval; only the contract definition
# and key derivation exist here.
SCOPE_VERIFY_PROMPT = ROOT / "prompts" / "verify_scope_pair.md"
SCOPE_VERIFY_SCHEMA = ROOT / "scope_pair_verification.schema.json"
SCOPE_VERIFY_CONTRACT_VERSION = "4"
RUNTIME_VERSION = "80"
AI_CONTRACT_CACHE_VERSION = "12"
LEGACY_AI_CONTRACT_CACHE_VERSIONS = ("7", "8", "9", "10", "11")
BOUNDARY_CONSENSUS_VERSION = "6"
VISUAL_FINGERPRINT = "__visual_identity__"
TARGETED_QUICK_FINGERPRINT = "__targeted_quick_locator__"
FALLBACK_ANCHOR_WINDOW_PAGES = 4
RECOVERY_PROVIDER_EDGE_DELTA = 40
RECOVERY_SEAM_MAX_GAP = 80
RECOVERY_CONSENSUS_MARKER = "boundary_consensus_extended"
RECOVERY_SEAM_MARKER = "boundary_seam_repaired"


class ContractError(ValueError):
    pass


class ProviderProfileError(ValueError):
    pass


@dataclass(frozen=True)
class PageText:
    pdf_index: int
    quality: str
    text: str


def _atomic_json(path: Path, data: Mapping[str, Any]) -> None:
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
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _boundary_error_path(output: Path) -> Path:
    return output.with_name(output.stem + ".error.json")


def _persist_boundary_error(
    output: Path, provider: str, model: str, reason: str
) -> None:
    try:
        _atomic_json(
            _boundary_error_path(output),
            {
                "provider": provider,
                "model": model,
                "reason": re.sub(r"[^0-9A-Za-z_.:-]+", "_", reason)[:160],
            },
        )
    except OSError:
        pass


def _resolve_ref(root: Mapping[str, Any], reference: str) -> Mapping[str, Any]:
    if not reference.startswith("#/"):
        raise ContractError(f"unsupported schema ref: {reference}")
    value: Any = root
    for token in reference[2:].split("/"):
        value = value[token.replace("~1", "/").replace("~0", "~")]
    if not isinstance(value, dict):
        raise ContractError(f"schema ref is not an object: {reference}")
    return value


def validate_contract(value: Any, schema: Mapping[str, Any]) -> None:
    errors: list[str] = []

    def visit(item: Any, rule: Mapping[str, Any], path: str) -> None:
        if "$ref" in rule:
            visit(item, _resolve_ref(schema, str(rule["$ref"])), path)
            return
        if "anyOf" in rule:
            for option in rule["anyOf"]:
                trial: list[str] = []
                before = len(errors)
                visit(item, option, path)
                if len(errors) == before:
                    return
                trial.extend(errors[before:])
                del errors[before:]
            errors.append(f"{path}: no anyOf branch matched")
            return
        expected = rule.get("type")
        allowed = set(expected if isinstance(expected, list) else [expected]) if expected else set()
        actual = (
            "null" if item is None else
            "boolean" if isinstance(item, bool) else
            "integer" if isinstance(item, int) else
            "number" if isinstance(item, float) else
            "string" if isinstance(item, str) else
            "array" if isinstance(item, list) else
            "object" if isinstance(item, dict) else "unknown"
        )
        if allowed and actual not in allowed and not (actual == "integer" and "number" in allowed):
            errors.append(f"{path}: expected {sorted(allowed)}, got {actual}")
            return
        if "enum" in rule and item not in rule["enum"]:
            errors.append(f"{path}: value outside enum")
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if "minimum" in rule and item < rule["minimum"]:
                errors.append(f"{path}: below minimum")
            if "maximum" in rule and item > rule["maximum"]:
                errors.append(f"{path}: above maximum")
        if isinstance(item, str) and len(item) < int(rule.get("minLength", 0)):
            errors.append(f"{path}: string too short")
        if isinstance(item, list):
            if len(item) < int(rule.get("minItems", 0)):
                errors.append(f"{path}: too few items")
            if "maxItems" in rule and len(item) > int(rule["maxItems"]):
                errors.append(f"{path}: too many items")
            child = rule.get("items")
            if isinstance(child, dict):
                for index, entry in enumerate(item):
                    visit(entry, child, f"{path}[{index}]")
        if isinstance(item, dict):
            properties = rule.get("properties", {})
            required = set(rule.get("required", []))
            missing = required - set(item)
            if missing:
                errors.append(f"{path}: missing {sorted(missing)}")
            if rule.get("additionalProperties") is False:
                extra = set(item) - set(properties)
                if extra:
                    errors.append(f"{path}: extra {sorted(extra)}")
            for key, child in properties.items():
                if key in item:
                    visit(item[key], child, f"{path}.{key}")

    visit(value, schema, "$")
    if errors:
        raise ContractError("; ".join(errors[:12]))


def _digest_file(path: Path) -> str:
    return qc.sha256_file(path)


def scope_pair_contract_key(
    provider: str,
    prompt: str,
    images: Sequence[Path],
    model: str | None,
) -> str:
    """Cache key for the Phase 2b scope-pair verification contract.

    Separate namespace (stage ``verify-scope-<provider>``) and version from
    the anchor verify contract, so existing anchor-pair caches are never
    shadowed or reused for scope pairs. Defining the key does NOT authorize a
    provider call — dispatch stays forbidden until the 2c approval.
    """
    return _contract_key(
        f"verify-scope-{provider}",
        prompt,
        SCOPE_VERIFY_SCHEMA,
        images,
        model,
        cache_version=SCOPE_VERIFY_CONTRACT_VERSION,
    )


def _provider_error_reason(
    exc: BaseException, prefix: str = "provider_error"
) -> str:
    detail = re.sub(r"\s+", " ", str(exc)).strip()[:200]
    label = f"{prefix}:{type(exc).__name__}"
    return f"{label}:{detail}" if detail else label


def _contract_key(
    stage: str,
    prompt: str,
    schema_path: Path,
    images: Sequence[Path],
    model: str | None,
    *,
    cache_version: str = AI_CONTRACT_CACHE_VERSION,
) -> str:
    digest = hashlib.sha256()
    for value in (
        cache_version,
        astra_cli.POLICY_VERSION,
        astra_cli.effort_for(stage),
        stage,
        model or astra_cli.MODEL,
        prompt,
        schema_path.read_text(encoding="utf-8"),
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    for image in images:
        digest.update(image.name.encode("utf-8"))
        digest.update(_digest_file(image).encode("ascii"))
    return digest.hexdigest()


def _contract_cache_keys(
    stage: str,
    prompt: str,
    schema_path: Path,
    images: Sequence[Path],
    model: str | None,
) -> tuple[str, ...]:
    """Accept old results only when every contract input except cache version matches."""

    versions = (AI_CONTRACT_CACHE_VERSION,)
    return tuple(
        _contract_key(
            stage,
            prompt,
            schema_path,
            images,
            model,
            cache_version=version,
        )
        for version in dict.fromkeys(versions)
    )


def run_gemini_labeled(
    prompt: str,
    schema: dict[str, Any],
    images: list[Path],
    labels: list[str],
    model: str,
    api_key: str,
    timeout: int,
) -> dict[str, Any]:
    raise astra_cli.AstraError("legacy_text_provider_disabled")


def run_qwen_labeled(
    prompt: str,
    schema: dict[str, Any],
    images: list[Path],
    labels: list[str],
    model: str,
    api_key: str,
    base_url: str,
    timeout: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raise astra_cli.AstraError("legacy_text_provider_disabled")


def _runtime_receipt_failed(receipt: object) -> bool:
    # Detection proposals are not final authority receipts, but an explicit
    # failed/incomplete transport can never count as successful detection.
    return not isinstance(receipt, Mapping) or receipt.get("terminal_completed") is False or (
        "status" in receipt and receipt["status"] != "succeeded"
    )


def run_codex_contract(
    *,
    stage: str,
    prompt: str,
    schema_path: Path,
    images: Sequence[Path],
    output: Path,
    run_dir: Path,
    model: str | None,
    force: bool,
    timeout: int | None = None,
) -> dict[str, Any] | None:
    # Full solution-page detection can exceed ten minutes; keep other stages
    # and callers' explicit limits unchanged.
    if timeout is None:
        timeout = 1200 if stage == "solution-blocks-codex" else 600
    model = astra_cli.MODEL
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    keys = _contract_cache_keys(stage, prompt, schema_path, images, model)
    key = keys[0]
    if not force and output.exists() and not _boundary_error_path(output).exists():
        try:
            wrapper = json.loads(output.read_text(encoding="utf-8"))
            if wrapper.get("meta", {}).get("cache_key") in keys and not _runtime_receipt_failed(
                wrapper.get("meta", {}).get("runtime_receipt", {})
            ):
                validate_contract(wrapper["result"], schema)
                return wrapper["result"]
        except (OSError, KeyError, json.JSONDecodeError, ContractError):
            pass
    inside(output.resolve(), "output")
    output.parent.mkdir(parents=True, exist_ok=True)
    logs = run_dir / "solutions" / "logs"
    inside(logs.resolve(), "output")
    logs.mkdir(parents=True, exist_ok=True)
    last = output.with_suffix(".last.tmp")
    try:
        command = qc.codex_exec_command(schema_path, last, astra_cli.effort_for(stage), model, images)
        process = qc.run_codex_process(command, prompt, timeout, stage=stage)
        log_stem = output.stem
        (logs / f"{log_stem}.events.jsonl").write_text(process.stdout, encoding="utf-8")
        (logs / f"{log_stem}.stderr.log").write_text(process.stderr, encoding="utf-8")
        if process.returncode != 0 or not last.exists():
            output.unlink(missing_ok=True)
            _persist_boundary_error(output, "astra", astra_cli.MODEL, process.stderr or "cli_failed")
            return None
        receipt = json.loads(process.stdout)
        if _runtime_receipt_failed(receipt):
            raise ContractError("provider_terminal_failure")
        response_text = last.read_bytes().decode("utf-8")
        result = qc.extract_json_message(response_text)
        validate_contract(result, schema)
        _atomic_json(
            output,
            {"meta": {"stage": stage, "cache_key": key, "model": model, "policy_version": astra_cli.POLICY_VERSION, "effort": astra_cli.effort_for(stage), "runtime_receipt": json.loads(process.stdout), "request_prompt": prompt, "response_text": response_text}, "result": result},
        )
        _boundary_error_path(output).unlink(missing_ok=True)
        return result
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ContractError) as error:
        output.unlink(missing_ok=True)
        _persist_boundary_error(output, "astra", astra_cli.MODEL, _provider_error_reason(error))
        return None
    finally:
        last.unlink(missing_ok=True)


def read_pdf_text_pages(path: Path) -> tuple[PageText, ...]:
    document = pdfium.PdfDocument(str(path))
    pages: list[PageText] = []
    try:
        for index in range(len(document)):
            page = document[index]
            try:
                _, quality, text = qc.text_layer_info(page, max_chars=20000)
            finally:
                page.close()
            runtime_quality = runtime_text_quality(quality, text)
            pages.append(PageText(index, runtime_quality, text if runtime_quality == "usable" else ""))
    finally:
        document.close()
    return tuple(pages)


def runtime_text_quality(base_quality: str, text: str) -> str:
    broken = sum(character in {"�", "?"} for character in text)
    return (
        "scrambled"
        if base_quality == "usable" and broken / max(1, len(text)) > 0.02
        else base_quality
    )


def printed_page_from_text(text: str) -> int | None:
    lines = [unicodedata.normalize("NFKC", line).strip() for line in text.splitlines()]
    # Page labels live at the outer header/footer.  Do not scan deeper edge
    # content: answer books commonly print references such as "문제책 93쪽"
    # near a page bottom, which is not the solution PDF's printed page.
    outer_lines = lines[:3] + lines[-3:]
    candidates: set[int] = set()
    for line in outer_lines:
        explicit = re.fullmatch(
            r"(?:p\.?\s*(\d{1,4})|(\d{1,4})\s*쪽)", line, re.IGNORECASE
        )
        if explicit:
            number = explicit.group(1) or explicit.group(2)
            if number and int(number) > 0:
                candidates.add(int(number))
        # Zero-padded/three-digit folios are a common publisher convention:
        # "정답친해 043" or "002 I. ...".  Only accept the first/last
        # token of an outer line so dates, question numbers and page referrals
        # inside the body cannot win.
        if re.fullmatch(r"\d{1,4}", line):
            continue
        for match in (
            re.match(r"^(\d{3,4})(?:\D|$)", line),
            re.search(r"(?:^|\D)(\d{3,4})$", line),
        ):
            if match and int(match.group(1)) > 0:
                candidates.add(int(match.group(1)))
    return next(iter(candidates)) if len(candidates) == 1 else None


def untrusted_edge_number(text: str) -> int | None:
    lines = [unicodedata.normalize("NFKC", line).strip() for line in text.splitlines()]
    candidates = {
        int(match.group(1))
        for line in lines[:8] + lines[-8:]
        for match in [re.fullmatch(r"(\d{1,4})", line)]
        if match and int(match.group(1)) > 0
    }
    return next(iter(candidates)) if len(candidates) == 1 else None


def stabilize_printed_page_candidates(
    printed_by_index: Mapping[int, int | None], *, neighbor_distance: int = 3
) -> dict[int, int | None]:
    confirmed = {
        index: value
        for index, value in printed_by_index.items()
        if value is not None
    }
    return {
        index: (
            value
            if value is not None
            and any(
                other_index != index
                and abs(other_index - index) <= neighbor_distance
                and other_value - value == other_index - index
                for other_index, other_value in confirmed.items()
            )
            else None
        )
        for index, value in printed_by_index.items()
    }


def recover_internal_printed_page_gaps(
    printed_by_index: Mapping[int, int | None], *, max_missing: int = 2
) -> dict[int, int | None]:
    result = dict(printed_by_index)
    observed = sorted(
        (index, value)
        for index, value in result.items()
        if value is not None
    )
    for (left_index, left_page), (right_index, right_page) in zip(
        observed, observed[1:]
    ):
        missing = right_index - left_index - 1
        if (
            not 1 <= missing <= max_missing
            or right_page - left_page != right_index - left_index
        ):
            continue
        for offset in range(1, right_index - left_index):
            if result.get(left_index + offset) is None:
                result[left_index + offset] = left_page + offset
    return result


def build_page_map(
    total_pages: int, printed_by_index: Mapping[int, int | None], prefix: str
) -> tuple[PageIdentity, ...]:
    segment = 1
    previous: int | None = None
    result: list[PageIdentity] = []
    for index in range(total_pages):
        printed = printed_by_index.get(index)
        if printed is not None and previous is not None and printed <= previous:
            segment += 1
        result.append(page_identity(index, printed, f"{prefix}-{segment}"))
        if printed is not None:
            previous = printed
    return tuple(result)


def _reconcile_solution_printed_page_map(
    solutions_dir: Path,
    consensus_dir: Path,
    *,
    expected_source_sha256: str,
    input_files: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    page_map_path = solutions_dir / "page_map.json"
    expected_hashes = {
        str(item.get("path")): str(item.get("sha256")) for item in input_files
    }
    if expected_hashes.get("page_map.json") != _digest_file(page_map_path):
        raise StageHookError("printed_page_source_changed")
    try:
        payload = json.loads(page_map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StageHookError("printed_page_map_invalid") from error
    if payload.get("source_sha256") != expected_source_sha256:
        raise StageHookError("printed_page_source_mismatch")
    raw_pages = payload.get("pages")
    if not isinstance(raw_pages, list):
        raise StageHookError("printed_page_map_invalid")
    pages: dict[int, dict[str, Any]] = {}
    for page in raw_pages:
        if (
            not isinstance(page, dict)
            or isinstance(page.get("pdf_index"), bool)
            or not isinstance(page.get("pdf_index"), int)
            or page.get("pdf_page") != page.get("pdf_index") + 1
            or not isinstance(page.get("segment_id"), str)
            or not page.get("segment_id")
        ):
            raise StageHookError("printed_page_map_invalid")
        pages[page["pdf_index"]] = page
    observations: dict[int, tuple[int, str, str]] = {}
    for path in sorted(consensus_dir.glob("page-*.json")):
        relative = path.relative_to(solutions_dir).as_posix()
        actual_hash = _digest_file(path)
        if expected_hashes.get(relative) != actual_hash:
            raise StageHookError("printed_page_source_changed")
        try:
            result = json.loads(path.read_text(encoding="utf-8"))["result"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise StageHookError("printed_page_observation_invalid") from error
        pdf_index = result.get("solution_pdf_index")
        observed = result.get("solution_printed_page")
        if (
            isinstance(pdf_index, bool)
            or not isinstance(pdf_index, int)
            or pdf_index not in pages
            or result.get("solution_pdf_page") != pdf_index + 1
            or isinstance(observed, bool)
            or observed is not None and (not isinstance(observed, int) or observed <= 0)
        ):
            raise StageHookError("printed_page_observation_invalid")
        if observed is not None:
            observations[pdf_index] = (observed, relative, actual_hash)
    duplicates: dict[tuple[str, int], set[int]] = {}
    for pdf_index, page in pages.items():
        existing = page.get("printed_page")
        if existing is not None:
            duplicates.setdefault((page["segment_id"], existing), set()).add(
                pdf_index
            )
    for pdf_index, (observed, _relative, _hash) in observations.items():
        duplicates.setdefault((pages[pdf_index]["segment_id"], observed), set()).add(
            pdf_index
        )
    duplicate_pages = {
        pdf_index
        for indices in duplicates.values()
        if len(indices) > 1
        for pdf_index in indices
    }
    conflicts: list[dict[str, Any]] = []
    updated: list[int] = []
    for pdf_index, (observed, relative, source_hash) in sorted(observations.items()):
        page = pages[pdf_index]
        existing = page.get("printed_page")
        reason = None
        if pdf_index in duplicate_pages:
            reason = "duplicate_printed_page"
        elif existing is not None and existing != observed:
            reason = "existing_observation_mismatch"
        if reason is not None:
            conflicts.append({
                "pdf_index": pdf_index,
                "existing_printed_page": existing,
                "observed_printed_page": observed,
                "reason": reason,
                "source_path": relative,
                "source_sha256": source_hash,
            })
        elif existing is None:
            page["printed_page"] = observed
            updated.append(pdf_index)
    payload["printed_page_conflicts"] = conflicts
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    changed = page_map_path.read_text(encoding="utf-8") != rendered
    if changed:
        _atomic_json(page_map_path, payload)
    return {
        "changed": changed,
        "updated_pages": updated,
        "conflicts": conflicts,
        "page_map": tuple(
            PageIdentity(
                int(page["pdf_index"]),
                int(page["pdf_page"]),
                page.get("printed_page"),
                str(page["segment_id"]),
            )
            for page in raw_pages
        ),
    }


def _runtime_review_candidate_groups(
    alignment: scope_alignment_mod.ScopeAlignment,
) -> scope_alignment_mod.ScopeAlignment:
    additions: dict[tuple[str, str], list[str]] = {}
    for group in alignment.candidate_groups:
        for scope_id in group.scope_ids:
            additions.setdefault((scope_id, group.role), []).extend(group.episode_ids)
    entries = tuple(
        entry
        if entry.aligned_episode_id is not None or not additions.get((entry.scope_id, entry.role))
        else replace(
            entry,
            allows=tuple(dict.fromkeys((*entry.allows, *additions[(entry.scope_id, entry.role)]))),
            evidence=tuple(dict.fromkeys((*entry.evidence, "scope_alignment_group_candidate"))),
        )
        for entry in alignment.entries
    )
    return replace(alignment, entries=entries)


def prepare_page_attachments(
    pdf_path: Path,
    render_root: Path,
    page_index: int,
    total_pages: int,
    text_hint: str,
    rotations: Mapping[int, int] | None = None,
) -> tuple[list[Path], str]:
    neighbors = [page for page in (page_index - 1, page_index + 1) if 0 <= page < total_pages]
    qc.ensure_render_assets(
        pdf_path,
        render_root,
        [page_index],
        200,
        "quadrants",
        neighbors,
        dict(rotations or {}),
    )
    images, attachment_map = qc.attachment_set(
        render_root,
        page_index,
        total_pages,
        200,
        "quadrants",
        1,
        dict(rotations or {}),
    )
    return images, attachment_map


def _tokens(text: str, limit: int = 3) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    values = [
        token
        for token in re.findall(r"[0-9a-z가-힣]{2,}", normalized)
        if token not in {"문제", "보기", "정답", "해설"}
    ]
    return tuple(dict.fromkeys(values))[:limit]


def extract_problem_fingerprints(
    source_pdf: Path, manifest_items: Sequence[Mapping[str, Any]]
) -> dict[str, tuple[str, ...]]:
    document = pdfium.PdfDocument(str(source_pdf))
    result: dict[str, tuple[str, ...]] = {}
    try:
        for item in manifest_items:
            fragments: list[str] = []
            for part in item.get("parts", []):
                page_index = int(part.get("source_page", 0)) - 1
                bbox = part.get("bbox")
                if not isinstance(bbox, list) or len(bbox) != 4 or not 0 <= page_index < len(document):
                    continue
                page = document[page_index]
                try:
                    width, height = page.get_size()
                    text_page = page.get_textpage()
                    try:
                        x0, y0, x1, y1 = bbox
                        text = text_page.get_text_bounded(
                            x0 * width / 1000,
                            (1000 - y1) * height / 1000,
                            x1 * width / 1000,
                            (1000 - y0) * height / 1000,
                        )
                    finally:
                        text_page.close()
                finally:
                    page.close()
                if text:
                    fragments.append(text)
            result[str(item["unit_id"])] = (VISUAL_FINGERPRINT,) + _tokens(" ".join(fragments))
    finally:
        document.close()
    return result


def render_solution_asset_image(
    solution_pdf: Path, asset: SolutionAsset, output: Path, dpi: int
) -> None:
    document = pdfium.PdfDocument(str(solution_pdf))
    cache = qc.PageImageCache(document, dpi)
    crops = []
    parts: list[dict[str, Any]] = []
    page_sizes: dict[int, tuple[int, int]] = {}
    try:
        for part in sorted(asset.parts, key=lambda value: value.order):
            page_image = cache.get(part.page.pdf_index)
            crop = page_image.crop(
                qc.normalized_to_pixels(
                    list(part.bbox), page_image.width, page_image.height, padding=0
                )
            ).convert("RGB")
            crops.append(crop)
            applied = [
                part.bbox[0],
                part.bbox[1],
                part.bbox[2],
                part.bbox[3],
            ]
            parts.append({"source_page": part.page.pdf_index, "crop_bbox": applied})
            page_sizes[part.page.pdf_index] = page_image.size
        composed = qc.compose_spatial_crops(
            crops,
            parts,
            page_sizes,
            gap=max(12, round(dpi * 0.06)),
            preserve_part_order=True,
        )
        temporary: Path | None = None
        try:
            inside(output.resolve(), "output")
            output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=output.parent,
                prefix=output.name + ".",
                suffix=".png",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
            composed.save(temporary, format="PNG", optimize=True)
            os.replace(temporary, output)
            temporary = None
        finally:
            composed.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    finally:
        for crop in crops:
            crop.close()
        cache.close()
        document.close()


def _is_recovery_detail(asset: SolutionAsset) -> bool:
    return (
        asset.source_document == "solution"
        and asset.role == SolutionRole.DETAILED_SOLUTION
        and asset.asset_id.startswith("recovery-detail-")
    )


def _same_recovery_column(
    left: tuple[int, int, int, int], right: tuple[int, int, int, int]
) -> bool:
    left_center = left[0] + left[2]
    right_center = right[0] + right[2]
    if left_center == 1000 or right_center == 1000:
        return False
    if (left_center < 1000) != (right_center < 1000):
        return False
    overlap = max(0, min(left[2], right[2]) - max(left[0], right[0]))
    return overlap * 2 >= min(left[2] - left[0], right[2] - right[0])


def _has_recovery_boundary_marker(asset: SolutionAsset) -> bool:
    return (
        len(asset.parts) == 1
        and RECOVERY_CONSENSUS_MARKER in str(asset.review_reason or "")
    )


def _consecutive_recovery_details(
    previous: SolutionAsset, following: SolutionAsset
) -> bool:
    return (
        previous.subquestion_label is None
        and following.subquestion_label is None
        and previous.parent_number.isdigit()
        and following.parent_number.isdigit()
        and int(following.parent_number) == int(previous.parent_number) + 1
    )


def _recovery_detail_seams(
    asset: SolutionAsset,
    part: SolutionPart,
    all_assets: Sequence[SolutionAsset],
) -> tuple[int | None, int | None]:
    """Return safe previous/current and current/next seams for one recovery part."""

    if not _is_recovery_detail(asset):
        return None, None
    peers: list[tuple[SolutionAsset, SolutionPart]] = []
    for other in all_assets:
        if (
            other.asset_id == asset.asset_id
            or not _is_recovery_detail(other)
            or other.scope_id != asset.scope_id
        ):
            continue
        for other_part in other.parts:
            if (
                other_part.page.pdf_index == part.page.pdf_index
                and other_part.page.segment_id == part.page.segment_id
                and _same_recovery_column(part.bbox, other_part.bbox)
            ):
                peers.append((other, other_part))
    if any(other.sequence == asset.sequence for other, _part in peers):
        return None, None

    x0, y0, x1, y1 = part.bbox
    del x0, x1
    prior_peers = [
        (other, other_part)
        for other, other_part in peers
        if other.sequence < asset.sequence
    ]
    next_peers = [
        (other, other_part)
        for other, other_part in peers
        if other.sequence > asset.sequence
    ]
    if prior_peers:
        nearest_sequence = max(other.sequence for other, _part in prior_peers)
        prior_peers = [
            value for value in prior_peers if value[0].sequence == nearest_sequence
        ]
        if len({other.asset_id for other, _part in prior_peers}) != 1:
            prior_peers = []
    if next_peers:
        nearest_sequence = min(other.sequence for other, _part in next_peers)
        next_peers = [
            value for value in next_peers if value[0].sequence == nearest_sequence
        ]
        if len({other.asset_id for other, _part in next_peers}) != 1:
            next_peers = []
    if prior_peers and (
        not _consecutive_recovery_details(prior_peers[0][0], asset)
        or not _has_recovery_boundary_marker(prior_peers[0][0])
    ):
        prior_peers = []
    if next_peers and (
        not _consecutive_recovery_details(asset, next_peers[0][0])
        or not _has_recovery_boundary_marker(asset)
    ):
        next_peers = []

    def seam(previous_bottom: int, next_top: int) -> int | None:
        gap = next_top - previous_bottom
        if not 2 <= gap <= RECOVERY_SEAM_MAX_GAP:
            return None
        return previous_bottom + gap // 2

    prior_seam = None
    if prior_peers and not any(other_part.bbox[3] > y0 for _, other_part in prior_peers):
        previous_bottom = max(other_part.bbox[3] for _, other_part in prior_peers)
        prior_seam = seam(previous_bottom, y0)

    next_seam = None
    if next_peers and not any(other_part.bbox[1] < y1 for _, other_part in next_peers):
        next_top = min(other_part.bbox[1] for _, other_part in next_peers)
        next_seam = seam(y1, next_top)
    return prior_seam, next_seam


def _materialize_recovery_detail_seams(
    assets: Sequence[SolutionAsset],
) -> tuple[tuple[SolutionAsset, ...], dict[str, str]]:
    """Freeze seam-adjusted geometry under a new deterministic candidate ID."""

    baseline_assets = tuple(
        replace(
            asset,
            review_reason=(
                str(asset.review_reason)
                .replace("+" + RECOVERY_CONSENSUS_MARKER, "")
                .replace(RECOVERY_CONSENSUS_MARKER, "")
                or None
            ),
        )
        if RECOVERY_CONSENSUS_MARKER in str(asset.review_reason or "")
        else asset
        for asset in assets
    )
    baseline_by_id = {asset.asset_id: asset for asset in baseline_assets}
    replacements: dict[str, str] = {}
    materialized: list[SolutionAsset] = []
    for asset in assets:
        if not _is_recovery_detail(asset):
            materialized.append(asset)
            continue
        rendered = output_asset_with_context(asset, assets)
        baseline = output_asset_with_context(
            baseline_by_id[asset.asset_id], baseline_assets
        )
        if rendered.parts == baseline.parts:
            materialized.append(asset)
            continue
        geometry = [
            [part.page.pdf_index, *part.bbox, part.order]
            for part in rendered.parts
        ]
        suffix = hashlib.sha256(
            json.dumps(geometry, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:10]
        new_id = f"{asset.asset_id}-seam-{suffix}"
        reason = str(asset.review_reason or "")
        reason = reason.replace(RECOVERY_CONSENSUS_MARKER, RECOVERY_SEAM_MARKER)
        if RECOVERY_SEAM_MARKER not in reason:
            reason = "+".join(value for value in (reason, RECOVERY_SEAM_MARKER) if value)
        repaired = replace(rendered, asset_id=new_id, review_reason=reason)
        replacements[asset.asset_id] = new_id
        materialized.append(repaired)
    return tuple(materialized), replacements


def output_asset_with_context(
    asset: SolutionAsset, all_assets: Sequence[SolutionAsset]
) -> SolutionAsset:
    if getattr(asset, "source_document", "solution") != "solution":
        return asset
    if RECOVERY_SEAM_MARKER in str(asset.review_reason or ""):
        return asset
    if asset.role == SolutionRole.QUICK_ANSWER:
        if len(asset.parts) > 1:
            return asset
        quick_parts: list[SolutionPart] = []
        for part in asset.parts:
            x0, y0, x1, y1 = part.bbox
            page_assets = {
                other.asset_id: other
                for other in all_assets
                if other.role == SolutionRole.QUICK_ANSWER
                and other.scope_id == asset.scope_id
                and any(
                    other_part.page.pdf_index == part.page.pdf_index
                    for other_part in other.parts
                )
            }
            page_assets[asset.asset_id] = asset
            ordered = sorted(page_assets.values(), key=lambda value: value.sequence)
            rows: list[list[tuple[SolutionAsset, SolutionPart]]] = []
            for other in ordered:
                other_part = next(
                    (
                        value
                        for value in other.parts
                        if value.page.pdf_index == part.page.pdf_index
                    ),
                    None,
                )
                if other_part is None:
                    continue
                if rows and other_part.bbox[0] <= rows[-1][-1][1].bbox[0]:
                    rows.append([])
                if not rows:
                    rows.append([])
                rows[-1].append((other, other_part))
            target_row = next(
                (
                    row
                    for row in rows
                    if any(other.asset_id == asset.asset_id for other, _part in row)
                ),
                [],
            )
            neighbors = [
                other_part
                for other, other_part in target_row
                if other.asset_id != asset.asset_id
            ]
            if not neighbors:
                neighbors = [
                    other_part
                    for other in page_assets.values()
                    if other.asset_id != asset.asset_id
                    for other_part in other.parts
                    if other_part.page.pdf_index == part.page.pdf_index
                    and min(y1, other_part.bbox[3]) > max(y0, other_part.bbox[1])
                ]
            following = [other for other in neighbors if other.bbox[0] > x0]
            # The detector's own left edge is safer than a previous entry's
            # imperfect right edge; left clipping can remove the target number.
            left_limit = 0
            right_limit = min(
                (
                    other.bbox[0] - 1
                    if other.bbox[0] <= x1
                    else other.bbox[0] - 8
                    if other.bbox[0] - x1 <= 4
                    else (x1 + other.bbox[0]) // 2
                    for other in following
                ),
                default=1000,
            )
            rendered_x0 = max(left_limit, x0 - 2)
            rendered_x1 = min(right_limit, x1 + 2)
            quick_parts.append(
                SolutionPart(
                    part.page,
                    (
                        rendered_x0,
                        max(0, y0 - 2),
                        rendered_x1,
                        min(1000, y1 + 2),
                    ),
                    part.order,
                )
            )
        return replace(asset, parts=tuple(quick_parts))
    expanded_parts: list[SolutionPart] = []
    strict_recovery_floor = (
        asset.asset_id.startswith("recovery-detail-") and len(asset.parts) == 1
    )
    for part in asset.parts:
        x0, y0, x1, y1 = part.bbox
        prior_seam, next_seam = _recovery_detail_seams(asset, part, all_assets)
        prior_bottoms = [
            other_part.bbox[3]
            for other in all_assets
            if other.asset_id != asset.asset_id
            for other_part in other.parts
            if other_part.page.pdf_index == part.page.pdf_index
            and other_part.bbox[3] <= y0
            and min(x1, other_part.bbox[2]) > max(x0, other_part.bbox[0])
        ]
        expanded_y0 = max(
            y0 + 1 if strict_recovery_floor else y0 - 20,
            max(prior_bottoms, default=-1) + 1,
            prior_seam or 0,
            0,
        )
        recovery_bottom = y1
        following_tops = [
            other_part.bbox[1]
            for other in all_assets
            if other.asset_id != asset.asset_id
            and other.role == SolutionRole.DETAILED_SOLUTION
            and other.scope_id == asset.scope_id
            and other.sequence > asset.sequence
            for other_part in other.parts
            if other_part.page.pdf_index == part.page.pdf_index
            and other_part.bbox[1] > y0
            and min(x1, other_part.bbox[2]) > max(x0, other_part.bbox[0])
        ]
        if next_seam is not None:
            recovery_bottom = next_seam
        elif following_tops:
            recovery_bottom = min(recovery_bottom, min(following_tops) - 1)
        if (
            asset.asset_id.startswith("recovery-detail-")
            and len(asset.parts) > 1
            and y1 >= 945
            and any(
                other_part.page.pdf_index == part.page.pdf_index
                and other_part.order != part.order
                and other_part.bbox[1] < 150
                for other_part in asset.parts
            )
        ):
            recovery_bottom -= 22
        expanded_parts.append(
            SolutionPart(
                part.page,
                (
                    x0,
                    expanded_y0,
                    x1,
                    recovery_bottom,
                ),
                part.order,
            )
        )
    return replace(asset, parts=tuple(expanded_parts))


def solution_asset_output_path(
    context: LinkContext,
    rendered_asset: SolutionAsset,
    source_path: Path | None = None,
) -> Path:
    source_key = _digest_file(source_path or context.solution_path)[:12]
    geometry_key = hashlib.sha256(
        json.dumps(
            rendered_asset.signature(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:10]
    return (
        context.run_dir
        / "solutions"
        / "crops"
        / f"{rendered_asset.asset_id}-{source_key}-{geometry_key}-dpi{context.crop_dpi}.png"
    )


def _split_number(value: str, unit_kind: str | None) -> tuple[str, str | None]:
    return split_printed_number(value, unit_kind)


def _semantic_label(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    text = re.sub(r"(?:p\.?\s*)?0*\d{1,4}\s*쪽\s*$", "", text)
    return re.sub(r"[^0-9a-z가-힣]+", "", text)


def _split_scoped_number(
    value: str, unit_kind: str | None, scope_label: object
) -> tuple[str, str | None]:
    parent, sublabel = _split_number(value, unit_kind)
    scope_prefix = re.match(
        r"^\s*([a-z]+)",
        unicodedata.normalize("NFKC", str(scope_label or "")).casefold(),
    )
    prefixed = re.fullmatch(r"([a-z]+)[._:-]?0*(\d+)", parent)
    if scope_prefix and prefixed and scope_prefix.group(1) == prefixed.group(1):
        parent = str(int(prefixed.group(2)))
    return parent, sublabel


def _looks_like_quick_answer_text(value: str) -> bool:
    text = unicodedata.normalize("NFKC", value)
    return bool(
        re.search(
            r"빠른\s*정답(?:\s*찾기)?|정답\s*표|정답\s*찾기|문제편\s*빠른정답",
            text,
        )
    )


def _hint_number_scope_label(hint: Mapping[str, Any]) -> object:
    scope = hint.get("numbering_scope", {}).get("scope_label")
    if re.match(
        r"^\s*[a-z]+",
        unicodedata.normalize("NFKC", str(scope or "")).casefold(),
    ):
        return scope
    prefixes = {
        match.group(1)
        for label in hint.get("numbering_scope", {}).get(
            "visible_question_labels", []
        )
        if (
            match := re.fullmatch(
                r"([a-z]+)[._:-]?0*\d+",
                unicodedata.normalize("NFKC", str(label)).casefold().strip(),
            )
        )
    }
    return next(iter(prefixes)) if len(prefixes) == 1 else scope


def _hint_reference_document(raw_text: object) -> str:
    text = unicodedata.normalize("NFKC", str(raw_text or "")).casefold()
    if "문제편" in text:
        return "problem"
    if re.search(r"해설편|해설서|정답\s*(?:과|및|&)\s*해설|정답친해", text):
        return "solution"
    return "unknown"


def _source_bound_problem_page_candidates(
    hints: Mapping[int, Mapping[str, Any]], total_pages: int
) -> dict[int, int]:
    candidates: dict[int, set[int]] = {}
    for source_index, hint in hints.items():
        source_printed = hint.get("problem_printed_page")
        if isinstance(source_printed, bool) or not isinstance(source_printed, int):
            continue
        for anchor in hint.get("anchors", []):
            if not isinstance(anchor, Mapping) or anchor.get(
                "reference_document", "unknown"
            ) != "problem":
                continue
            start = anchor.get("printed_start")
            end = anchor.get("printed_end")
            if isinstance(start, bool) or not isinstance(start, int):
                continue
            if not isinstance(end, int) or isinstance(end, bool):
                end = start + FALLBACK_ANCHOR_WINDOW_PAGES - 1
            for printed_page in range(start, end + 1):
                pdf_index = source_index + printed_page - source_printed
                if 0 <= pdf_index < total_pages:
                    candidates.setdefault(pdf_index, set()).add(printed_page)
    return {
        pdf_index: next(iter(values))
        for pdf_index, values in candidates.items()
        if len(values) == 1
    }


def _numbering_scope_path(
    hint: Mapping[str, Any], fallback: tuple[str, ...]
) -> tuple[str, ...]:
    scope = hint.get("numbering_scope")
    label = unicodedata.normalize(
        "NFKC", str(_hint_number_scope_label(hint) or "")
    ).casefold()
    prefix = re.match(r"^\s*([a-z]+)", label)
    if prefix:
        return (f"numbering:{prefix.group(1)}",)
    semantic = _semantic_label(label)
    confidence = scope.get("confidence") if isinstance(scope, Mapping) else None
    reset_signal = scope.get("reset_signal") if isinstance(scope, Mapping) else None
    if (
        semantic
        and type(confidence) in {int, float}
        and float(confidence) >= 0.9
        and reset_signal in {"yes", "no"}
    ):
        return (f"numbering:{semantic}",)
    return fallback


def _parent_identity_evidence(
    item: Mapping[str, Any], hint: Mapping[str, Any]
) -> bool:
    number_bbox = item.get("number_bbox")
    source_pages = {
        int(page)
        for page in item.get("source_pages", [])
        if isinstance(page, int) and not isinstance(page, bool)
    }
    if (
        not isinstance(number_bbox, Mapping)
        or number_bbox.get("role") != "main_number"
        or number_bbox.get("source_page") not in source_pages
    ):
        return False
    scope = hint.get("numbering_scope")
    confidence = scope.get("confidence") if isinstance(scope, Mapping) else None
    if type(confidence) not in {int, float} or float(confidence) < 0.9:
        return False
    try:
        parent, _sub = _split_scoped_number(
            str(item.get("printed_number") or item.get("unit_id") or ""),
            item.get("unit_kind"),
            _hint_number_scope_label(hint),
        )
    except ValueError:
        return False
    visible = set()
    for label in scope.get("visible_question_labels", []):
        try:
            visible.add(
                _split_scoped_number(
                    str(label), None, _hint_number_scope_label(hint)
                )[0]
            )
        except ValueError:
            continue
    return parent in visible


def _crop_search_eligible(
    item: Mapping[str, Any], hint: Mapping[str, Any], *, crop_verified: bool
) -> bool:
    if crop_verified:
        return True
    verification = item.get("verification")
    if not isinstance(verification, Mapping):
        return False
    confidence = verification.get("confidence")
    if type(confidence) not in {int, float} or float(confidence) < 0.9:
        return False
    required = (
        verification.get("complete_question") is True
        and verification.get("contains_expected_identifier") is True
        and verification.get("contains_required_context") is True
        and verification.get("contains_answer_or_solution") is False
        and verification.get("contains_other_question") is False
        and verification.get("required_content_missing_or_unreadable") is False
        and verification.get("explicit_answer_evidence") is False
        and verification.get("target_content_clipped") is False
    )
    if not required:
        return False
    identifier_clipped = verification.get("identifier_clipped") is True
    if identifier_clipped and not _parent_identity_evidence(item, hint):
        return False
    verdict = verification.get("verdict")
    return verdict in {"pass", "review"} or (
        verdict == "fail" and identifier_clipped
    )


def _recover_bracketed_anchor_spans(
    scoped_items: Sequence[Any],
    anchors: Sequence[StartAnchor],
    propagated: Mapping[str, StartAnchor],
    spans: Sequence[AnchorSpan],
    search_eligible: Mapping[str, bool],
) -> dict[str, AnchorSpan]:
    """Give otherwise-unanchored numeric items a review-only search window."""

    span_by_anchor = {
        anchor_id: span for span in spans for anchor_id in span.anchor_ids
    }
    ordered = sorted(anchors, key=lambda anchor: anchor.sequence)
    recovered: dict[str, AnchorSpan] = {}
    for scoped in scoped_items:
        item_id = scoped.item.item_id
        sequence = scoped.item.sequence
        if (
            item_id in propagated
            or not search_eligible.get(item_id, False)
            or not scoped.parent_number.isdigit()
        ):
            continue
        left_sequence = max(
            (anchor.sequence for anchor in ordered if anchor.sequence < sequence),
            default=None,
        )
        right_sequence = min(
            (anchor.sequence for anchor in ordered if anchor.sequence > sequence),
            default=None,
        )
        if left_sequence is None or right_sequence is None:
            continue
        pairs = [
            (left, right)
            for left in ordered
            if left.sequence == left_sequence
            for right in ordered
            if right.sequence == right_sequence
            and left.segment_id == right.segment_id
            and left.reference_document == right.reference_document
            and left.segment_id != "ambiguous"
            and left.page_range.start <= right.page_range.start
            and left.confidence >= 0.9
            and right.confidence >= 0.9
            and span_by_anchor.get(left.anchor_id) is not None
            and span_by_anchor.get(right.anchor_id) is not None
            and span_by_anchor[left.anchor_id].resolved
            and span_by_anchor[right.anchor_id].resolved
        ]
        if not pairs:
            continue
        left, right = min(
            pairs,
            key=lambda pair: (
                pair[0].page_range.start != pair[1].page_range.start,
                pair[1].page_range.start - pair[0].page_range.start,
                pair[0].anchor_id,
                pair[1].anchor_id,
            ),
        )
        start = left.page_range.start
        if start == right.page_range.start:
            ends = [
                span_by_anchor[anchor.anchor_id].page_range.end
                for anchor in (left, right)
            ]
            if any(end is None or end < start for end in ends):
                continue
            end = min(int(end) for end in ends if end is not None)
            reason = "anchor_inferred_from_same_start_bracket"
            source = "bracketed_same_start"
        else:
            end = right.page_range.start
            if end - start > FALLBACK_ANCHOR_WINDOW_PAGES:
                continue
            reason = "anchor_window_requires_verification"
            source = "bracketed_bounded_window"
        recovered[item_id] = AnchorSpan(
            (left.anchor_id, right.anchor_id),
            (item_id,),
            scoped.scope.identity,
            left.segment_id,
            PrintedPageRange(
                start,
                end,
                RangeMode.EXACT if start == end else RangeMode.RANGE,
                f"inferred:{start}~{end}",
            ),
            min(left.confidence, right.confidence),
            True,
            source,
            (reason,),
            left.reference_document,
        )
    return recovered


def _semantic_path(values: Sequence[str]) -> tuple[str, ...]:
    normalized = [_semantic_label(value) for value in values]
    return tuple(value for value in normalized if value)


GENERIC_HIERARCHY = {
    "완자",
    "문제",
    "해설",
    "정답",
    "정답친해",
    "핵심문제",
    "내신",
    "본문",
    "solution",
}
BOOK_VARIANTS = ("문제편", "개념편", "유형편", "실전편", "기출편")


def _hierarchy_score(
    problem_path: tuple[str, ...],
    block_path: tuple[str, ...],
    problem_scope_label: str | None,
    block_scope_label: str | None,
) -> int:
    common = (set(problem_path) & set(block_path)) - GENERIC_HIERARCHY
    score = len(common) * 2
    if problem_scope_label and block_scope_label:
        same_scope = (
            problem_scope_label == block_scope_label
            or problem_scope_label in block_scope_label
            or block_scope_label in problem_scope_label
        )
        if not same_scope:
            if len(common) < 2:
                return -1
        else:
            score += 3
    if problem_path and problem_path == block_path:
        score += 1
    return score


def _book_variant_score(state: RuntimeState, block_path: tuple[str, ...]) -> int:
    source_name = _semantic_label(
        Path(
            str(
                state.manifest.get("source_pdf")
                or state.run_state.get("source_pdf")
                or ""
            )
        ).name
    )
    source_variants = {variant for variant in BOOK_VARIANTS if variant in source_name}
    block_text = "".join(block_path)
    block_variants = {variant for variant in BOOK_VARIANTS if variant in block_text}
    if source_variants and block_variants:
        return 4 if source_variants & block_variants else -1
    return 0


def _problem_allows_solution_page(
    problem: ProblemItem, printed_page: int, role: SolutionRole
) -> bool:
    anchor = problem.anchor
    if anchor is None or not anchor.resolved or anchor.page_range.end is None:
        return False
    if role == SolutionRole.QUICK_ANSWER:
        lower = max(1, anchor.page_range.start - QUICK_ANSWER_LOOKBACK_PAGES)
        return (
            lower <= printed_page <= anchor.page_range.end
            or 1 <= printed_page <= QUICK_ANSWER_FRONT_MATTER_PAGES
            and printed_page < anchor.page_range.start
        )
    return anchor.page_range.contains(printed_page)


def _anchor_window_null_page_indices(
    page_map: Sequence[PageIdentity], problems: Sequence[ProblemItem]
) -> set[int]:
    result: set[int] = set()
    for page in page_map:
        if page.printed_page is not None:
            continue
        left = next(
            (
                value
                for value in reversed(page_map[: page.pdf_index])
                if value.segment_id == page.segment_id
                and value.printed_page is not None
            ),
            None,
        )
        right = next(
            (
                value
                for value in page_map[page.pdf_index + 1 :]
                if value.segment_id == page.segment_id
                and value.printed_page is not None
            ),
            None,
        )
        if (
            left is None
            or right is None
            or left.printed_page is None
            or right.printed_page is None
            or left.printed_page >= right.printed_page
        ):
            continue
        possible_start = left.printed_page + 1
        possible_end = right.printed_page - 1
        if any(
            problem.anchor is not None
            and problem.anchor.resolved
            and problem.anchor.segment_id == page.segment_id
            and max(possible_start, problem.anchor.page_range.start)
            <= min(possible_end, problem.anchor.page_range.end)
            for problem in problems
        ):
            result.add(page.pdf_index)
    return result


def _problem_prompt(
    template: str,
    source_pdf: Path,
    page_index: int,
    attachment_map: str,
    text_hint: str,
) -> str:
    return Template(template).safe_substitute(
        DOCUMENT_NAME=source_pdf.name,
        PDF_INDEX=page_index,
        PDF_PAGE=page_index + 1,
        ATTACHMENT_MAP=attachment_map,
        TEXT_HINT=text_hint or "(텍스트 레이어 없음)",
    )


def _solution_prompt(
    template: str,
    solution_pdf: Path,
    page_index: int,
    attachment_map: str,
    text_hint: str,
) -> str:
    return Template(template).safe_substitute(
        DOCUMENT_NAME=solution_pdf.name,
        PDF_INDEX=page_index,
        PDF_PAGE=page_index + 1,
        ATTACHMENT_MAP=attachment_map,
        TEXT_HINT=text_hint or "(텍스트 레이어 없음)",
    )


def _optional_parent(value: object) -> str | None:
    return (
        normalize_parent_number(value)
        if value is not None and str(value).strip()
        else None
    )


def _scope_tokens(block: Mapping[str, Any]) -> tuple[str, ...]:
    values = [
        value
        for value in _semantic_path(block.get("hierarchy_path", []))
        if value not in GENERIC_HIERARCHY
    ]
    label = _semantic_label(block.get("numbering_scope_label") or "")
    if label and label not in GENERIC_HIERARCHY:
        values.append(label)
    group_label = _semantic_label((block.get("coverage") or {}).get("group_label") or "")
    if group_label and group_label not in GENERIC_HIERARCHY:
        values.append(group_label)
    return tuple(dict.fromkeys(values))


def _bbox_column(value: object) -> str | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        left, _top, right, _bottom = (int(part) for part in value)
    except (TypeError, ValueError):
        return None
    if right <= 520:
        return "left"
    if left >= 480:
        return "right"
    return None


def _layout_scope_path(
    path: tuple[str, ...],
    group_label: str | None,
    column: str | None,
    *,
    column_collision: bool,
) -> tuple[str, ...]:
    values = list(path)
    if group_label:
        values.append(f"group:{group_label}")
    if column_collision and column in {"left", "right"}:
        values.append(f"column:{column}")
    return tuple(values)


def _layout_scope_score(
    problem: ProblemItem,
    block: Mapping[str, Any],
    *,
    include_numbering_scope: bool = False,
) -> int:
    problem_group = _semantic_label(problem.group_label or "")
    block_group = _semantic_label((block.get("coverage") or {}).get("group_label") or "")

    def numbered_group(value: str) -> str | None:
        match = re.search(r"(?:핵심자료|자료)\d+", value)
        return match.group(0) if match else None

    problem_numbered = numbered_group(problem_group)
    block_values = tuple(
        dict.fromkeys(
            value
            for value in (
                block_group,
                *(
                    (
                        _semantic_label(
                            block.get("numbering_scope_label") or ""
                        ),
                        *(
                            _semantic_label(item)
                            for item in block.get("hierarchy_path", [])
                        ),
                    )
                    if include_numbering_scope
                    else ()
                ),
            )
            if value
        )
    )
    block_numbered = {
        value for raw in block_values if (value := numbered_group(raw)) is not None
    }
    if problem_numbered and block_numbered:
        return 4 if block_numbered == {problem_numbered} else -1
    if problem_group and block_group:
        if not (
            problem_group == block_group
            or problem_group in block_group
            or block_group in problem_group
        ):
            return -1
        return 4
    return 0


def _block_semantic_path(block: Mapping[str, Any]) -> tuple[str, ...]:
    values = list(_semantic_path(block.get("hierarchy_path", [])))
    group_label = _semantic_label((block.get("coverage") or {}).get("group_label") or "")
    if group_label:
        values.append(group_label)
    return tuple(dict.fromkeys(values))


def _scope_agrees(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_values = _scope_tokens(left)
    right_values = _scope_tokens(right)
    if not left_values or not right_values:
        return not left_values and not right_values
    return any(
        first == second or first in second or second in first
        for first in left_values
        for second in right_values
    )


def _coverage_identity(block: Mapping[str, Any]) -> tuple[Any, ...]:
    coverage = block.get("coverage", {})
    return (
        coverage.get("kind"),
        _optional_parent(coverage.get("parent_number")),
        tuple(
            sorted(
                value
                for value in (
                    normalize_subquestion_label(item)
                    for item in coverage.get("subquestion_labels", [])
                )
                if value is not None
            )
        ),
        _optional_parent(coverage.get("range_start")),
        _optional_parent(coverage.get("range_end")),
        _semantic_label(coverage.get("group_label") or "") or None,
    )


def _same_solution_identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if left.get("role") != right.get("role") or not _scope_agrees(left, right):
        return False
    left_entries = {
        identity
        for entry in left.get("entries", [])
        if isinstance(entry, Mapping)
        and (identity := _entry_identity(entry))[0] is not None
    }
    right_entries = {
        identity
        for entry in right.get("entries", [])
        if isinstance(entry, Mapping)
        and (identity := _entry_identity(entry))[0] is not None
    }
    if left_entries and right_entries and not left_entries & right_entries:
        return False
    if _coverage_identity(left) == _coverage_identity(right):
        return True
    left_label = _semantic_label(left.get("continuation_label") or "")
    right_label = _semantic_label(right.get("continuation_label") or "")
    if (
        left.get("role") != SolutionRole.DETAILED_SOLUTION.value
        or not left_label
        or left_label != right_label
    ):
        return False
    left_coverage = left.get("coverage") or {}
    right_coverage = right.get("coverage") or {}
    if (
        _optional_parent(left_coverage.get("parent_number"))
        != _optional_parent(right_coverage.get("parent_number"))
        or _semantic_label(left_coverage.get("group_label") or "")
        != _semantic_label(right_coverage.get("group_label") or "")
        or left_coverage.get("kind")
        not in {"single", "continuation", "parent_with_subquestions"}
        or right_coverage.get("kind")
        not in {"single", "continuation", "parent_with_subquestions"}
    ):
        return False
    left_labels = {
        value
        for raw in left_coverage.get("subquestion_labels", [])
        if (value := normalize_subquestion_label(raw)) is not None
    }
    right_labels = {
        value
        for raw in right_coverage.get("subquestion_labels", [])
        if (value := normalize_subquestion_label(raw)) is not None
    }
    if left_labels and right_labels and left_labels != right_labels:
        return False
    for field in ("range_start", "range_end"):
        left_value = _optional_parent(left_coverage.get(field))
        right_value = _optional_parent(right_coverage.get(field))
        if left_value is not None and right_value is not None and left_value != right_value:
            return False
    return not left_entries or not right_entries or bool(left_entries & right_entries)


def _same_solution_unit(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return (
        _same_solution_identity(left, right)
        and bool(left.get("starts_on_previous_page"))
        == bool(right.get("starts_on_previous_page"))
        and bool(left.get("continues_next_page"))
        == bool(right.get("continues_next_page"))
        and _semantic_label(left.get("continuation_label") or "")
        == _semantic_label(right.get("continuation_label") or "")
    )


def _bbox_pair(left: object, right: object) -> tuple[float, bool]:
    if not (
        isinstance(left, list)
        and isinstance(right, list)
        and len(left) == len(right) == 4
    ):
        return 0.0, False
    lx0, ly0, lx1, ly1 = map(int, left)
    rx0, ry0, rx1, ry1 = map(int, right)
    intersection = max(0, min(lx1, rx1) - max(lx0, rx0)) * max(
        0, min(ly1, ry1) - max(ly0, ry0)
    )
    left_area = max(0, lx1 - lx0) * max(0, ly1 - ly0)
    right_area = max(0, rx1 - rx0) * max(0, ry1 - ry0)
    if not intersection or not left_area or not right_area:
        return 0.0, False
    containment = intersection / min(left_area, right_area)
    area_ratio = min(left_area, right_area) / max(left_area, right_area)
    return containment * area_ratio, containment >= 0.72 and area_ratio >= 0.45


def _bbox_envelope(*values: object) -> list[int] | None:
    boxes = [value for value in values if isinstance(value, list) and len(value) == 4]
    if not boxes:
        return None
    return [
        min(int(value[0]) for value in boxes),
        min(int(value[1]) for value in boxes),
        max(int(value[2]) for value in boxes),
        max(int(value[3]) for value in boxes),
    ]


def _recovery_content_bbox(
    primary: Mapping[str, Any], peers: Sequence[Mapping[str, Any]]
) -> list[int] | None:
    """Keep a compatible peer's farther content edge without trusting a wide disagreement."""

    primary_bbox = primary.get("bbox")
    if not isinstance(primary_bbox, list) or len(primary_bbox) != 4:
        return None
    compatible: list[tuple[float, int, list[int]]] = []
    for peer in peers:
        peer_bbox = peer.get("bbox")
        if (
            not isinstance(peer_bbox, list)
            or len(peer_bbox) != 4
            or not _same_solution_unit(primary, peer)
        ):
            continue
        score, geometry_matches = _bbox_pair(primary_bbox, peer_bbox)
        edge_delta = max(
            abs(int(left) - int(right))
            for left, right in zip(primary_bbox, peer_bbox)
        )
        if geometry_matches and edge_delta <= RECOVERY_PROVIDER_EDGE_DELTA:
            compatible.append((score, -edge_delta, peer_bbox))
    if not compatible:
        return [int(value) for value in primary_bbox]
    peer_bbox = max(compatible, key=lambda value: (value[0], value[1]))[2]
    return _bbox_envelope(primary_bbox, peer_bbox)


def _entry_identity(entry: Mapping[str, Any]) -> tuple[str | None, str | None]:
    return (
        _optional_parent(entry.get("parent_number")),
        normalize_subquestion_label(entry.get("subquestion_label")),
    )


def _answer_identity(value: object) -> str:
    text_value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text_value = re.sub(r"^\s*(?:정답|답)\s*[:：]?\s*", "", text_value)
    return re.sub(
        r"[^0-9a-z\u1100-\u11ffㄱ-ㆎ가-힣]+",
        "",
        text_value,
    )


def _recovery_answer_identity(value: object) -> str:
    identity = _answer_identity(value)
    if identity:
        return identity
    text_value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text_value = re.sub(r"^\s*(?:정답|답)\s*[:：]?\s*", "", text_value)
    compact = re.sub(r"[\s,，·/|]+", "", text_value)
    return (
        f"replacement:{len(compact)}"
        if compact and set(compact) == {"\ufffd"}
        else ""
    )


def _normalize_qwen_solution_result(
    value: Mapping[str, Any], page_index: int | None = None
) -> dict[str, Any]:
    """Map Qwen's compact JSON aliases into the strict local block contract."""

    result = copy.deepcopy(dict(value))
    if page_index is not None:
        result.setdefault("solution_pdf_index", page_index)
        result.setdefault("solution_pdf_page", page_index + 1)
        result.setdefault("solution_printed_label", None)
        result.setdefault("solution_printed_page", None)
    if result.get("page_role") in {"answer_key", "answer_table", "answers"}:
        result["page_role"] = SolutionRole.QUICK_ANSWER.value
    elif result.get("page_role") in {"solutions", "worked_solutions"}:
        result["page_role"] = SolutionRole.DETAILED_SOLUTION.value
    result.setdefault("hierarchy_path", [])
    result.setdefault("warnings", [])
    blocks = result.get("blocks")
    if not isinstance(blocks, list):
        return result
    visible: list[str] = []
    for index, raw_block in enumerate(blocks, 1):
        if not isinstance(raw_block, dict):
            continue
        block = raw_block
        block_answer = block.pop("explicit_answer_text", None)
        block_answer_bbox = block.pop("answer_bbox", None)
        block_subquestion = block.pop("subquestion_label", None)
        block_fingerprints = list(block.pop("fingerprint_tokens", None) or [])
        problem_title = block.pop("problem_title", None)
        if problem_title:
            block_fingerprints.append(str(problem_title))
        block_type = block.pop("type", None)
        if not block.get("role") and block_type in {
            SolutionRole.QUICK_ANSWER.value,
            SolutionRole.DETAILED_SOLUTION.value,
            SolutionRole.MIXED.value,
        }:
            block["role"] = block_type
        if not block.get("bbox") and _valid_bbox(block_answer_bbox):
            block["bbox"] = block_answer_bbox
        scope_label = block.pop("scope_label", None)
        block["local_id"] = str(
            block.get("local_id") or block.pop("block_id", None) or f"qwen-{index:03d}"
        )
        block.pop("block_id", None)
        parent = block.pop("parent_number", None) or block.pop(
            "problem_number", None
        )
        coverage_kind = block.pop("coverage_kind", None)
        coverage = block.get("coverage")
        if not isinstance(coverage, dict):
            coverage = {}
        coverage = {
            "kind": coverage.get("kind") or coverage_kind or "single",
            "parent_number": coverage.get("parent_number", parent),
            "subquestion_labels": list(coverage.get("subquestion_labels") or []),
            "range_start": coverage.get("range_start"),
            "range_end": coverage.get("range_end"),
            "group_label": coverage.get("group_label"),
        }
        if coverage["kind"] in {
            "list",
            "table",
            "answer_list",
            "multi_item_list",
        }:
            coverage["kind"] = "range"
        if coverage["kind"] == "range" and (
            coverage["range_start"] is None or coverage["range_end"] is None
        ):
            raw_parents = [
                str(
                    entry.get("parent_number")
                    or entry.get("problem_identifier")
                    or ""
                )
                for entry in (block.get("entries") or [])
                if isinstance(entry, dict)
            ]
            raw_parents = [parent for parent in raw_parents if parent]
            if raw_parents:
                coverage["range_start"] = coverage["range_start"] or raw_parents[0]
                coverage["range_end"] = coverage["range_end"] or raw_parents[-1]
        block["coverage"] = coverage
        block.setdefault("hierarchy_path", [])
        block.setdefault("numbering_scope_label", scope_label)
        block.setdefault("starts_on_previous_page", False)
        block.setdefault("continues_next_page", False)
        block.setdefault("continuation_label", None)
        if (
            not block.get("continuation_label")
            and (block["starts_on_previous_page"] or block["continues_next_page"])
            and coverage.get("parent_number") is not None
        ):
            block["continuation_label"] = str(coverage["parent_number"])
        block.setdefault("confidence", 0.5)
        entries = block.get("entries")
        if not isinstance(entries, list):
            entries = []
            block["entries"] = entries
        if not entries and coverage.get("parent_number") is not None:
            entries.append(
                {
                    "entry_id": f"{block['local_id']}-entry-01",
                    "parent_number": coverage.get("parent_number"),
                    "subquestion_label": block_subquestion,
                    "answer_bbox": (
                        block_answer_bbox
                        if block.get("role") == SolutionRole.QUICK_ANSWER.value
                        and _valid_bbox(block_answer_bbox)
                        else None
                    ),
                    "explicit_answer_text": block_answer,
                    "stem_fingerprint_tokens": block_fingerprints[:20],
                    "figure_labels": [],
                    "confidence": block["confidence"],
                }
            )
        for entry_index, raw_entry in enumerate(entries, 1):
            if not isinstance(raw_entry, dict):
                continue
            entry_parent = raw_entry.pop("problem_identifier", None)
            entry_label = raw_entry.pop("label", None)
            raw_entry.setdefault(
                "entry_id", f"{block['local_id']}-entry-{entry_index:02d}"
            )
            raw_entry.setdefault(
                "parent_number",
                entry_parent
                or entry_label
                or coverage.get("parent_number")
                or (
                    raw_entry.get("entry_id")
                    if re.fullmatch(r"\d+", str(raw_entry.get("entry_id") or ""))
                    else None
                ),
            )
            raw_entry.setdefault("subquestion_label", None)
            raw_entry.setdefault("answer_bbox", None)
            raw_entry.setdefault("explicit_answer_text", block_answer)
            raw_entry.setdefault("stem_fingerprint_tokens", block_fingerprints[:20])
            raw_entry.setdefault("figure_labels", [])
            raw_entry.setdefault("confidence", block["confidence"])
            if raw_entry.get("parent_number") is not None:
                visible.append(str(raw_entry["parent_number"]))
        if coverage.get("parent_number") is not None:
            visible.append(str(coverage["parent_number"]))
    for block in result.get("blocks", []):
        if not isinstance(block, dict):
            continue
        bbox = block.get("bbox")
        if isinstance(bbox, list) and len(bbox) == 4:
            block["bbox"] = [max(0, min(1000, int(value))) for value in bbox]
        for entry in block.get("entries", []):
            if not isinstance(entry, dict):
                continue
            answer_bbox = entry.get("answer_bbox")
            if isinstance(answer_bbox, list) and len(answer_bbox) == 4:
                clipped = [
                    max(0, min(1000, int(value))) for value in answer_bbox
                ]
                entry["answer_bbox"] = clipped if _valid_bbox(clipped) else None
    result["blocks"] = [
        block
        for block in result.get("blocks", [])
        if isinstance(block, dict) and _valid_bbox(block.get("bbox"))
    ]
    result.setdefault(
        "numbering_scope",
        {
            "scope_label": None,
            "reset_detected": False,
            "repeated_numbering_detected": False,
            "visible_number_sequence": list(dict.fromkeys(visible)),
            "confidence": 0.5,
        },
    )
    if result.get("page_role") not in {
        SolutionRole.QUICK_ANSWER.value,
        SolutionRole.DETAILED_SOLUTION.value,
        SolutionRole.MIXED.value,
        "other",
    }:
        roles = {
            block.get("role")
            for block in result.get("blocks", [])
            if isinstance(block, Mapping)
        }
        result["page_role"] = (
            next(iter(roles))
            if len(roles) == 1
            else SolutionRole.MIXED.value
            if roles
            else "other"
        )
    if "qwen_shape_normalized" not in result["warnings"]:
        result["warnings"].append("qwen_shape_normalized")
    return result


def _inherit_agreed_primary_scope(
    proposal: Mapping[str, Any], peers: Sequence[tuple[str, Mapping[str, Any]]]
) -> dict[str, Any]:
    """Fill only scope labels on which two primary providers already agree."""

    result = copy.deepcopy(dict(proposal))
    primary = [value for provider, value in peers if provider in {"codex", "gemini"}]
    if len(primary) < 2:
        return result
    primary_paths = [list(value.get("hierarchy_path") or []) for value in primary]
    if (
        not result.get("hierarchy_path")
        and primary_paths[0]
        and _semantic_path(primary_paths[0]) == _semantic_path(primary_paths[1])
    ):
        result["hierarchy_path"] = primary_paths[0]
    for block in result.get("blocks", []):
        if not isinstance(block, dict):
            continue
        matched: list[Mapping[str, Any]] = []
        for peer in primary:
            candidates = [
                candidate
                for candidate in peer.get("blocks", [])
                if isinstance(candidate, dict)
                and candidate.get("role") == block.get("role")
                and _coverage_identity(candidate) == _coverage_identity(block)
            ]
            if len(candidates) == 1:
                matched.append(candidates[0])
        if len(matched) != 2 or not _scope_agrees(matched[0], matched[1]):
            continue
        if not block.get("hierarchy_path"):
            block["hierarchy_path"] = list(matched[0].get("hierarchy_path") or [])
        if not block.get("numbering_scope_label"):
            block["numbering_scope_label"] = matched[0].get(
                "numbering_scope_label"
            )
    return result


def _valid_bbox(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(number, int) and not isinstance(number, bool) for number in value)
        and 0 <= value[0] < value[2] <= 1000
        and 0 <= value[1] < value[3] <= 1000
    )


def _transpose_bbox(value: object) -> object:
    if not isinstance(value, list) or len(value) != 4:
        return value
    return [value[1], value[0], value[3], value[2]]


def _transpose_proposal_bboxes(result: Mapping[str, Any]) -> dict[str, Any]:
    transposed = copy.deepcopy(dict(result))
    for block_value in transposed.get("blocks", []):
        if not isinstance(block_value, dict):
            continue
        block_value["bbox"] = _transpose_bbox(block_value.get("bbox"))
        for entry_value in block_value.get("entries", []):
            if isinstance(entry_value, dict) and entry_value.get("answer_bbox") is not None:
                entry_value["answer_bbox"] = _transpose_bbox(
                    entry_value.get("answer_bbox")
                )
    return transposed


def _coordinate_alignment_score(
    candidate: Mapping[str, Any], peers: Sequence[Mapping[str, Any]]
) -> tuple[float, int]:
    boxes = [
        value
        for block_value in candidate.get("blocks", [])
        if isinstance(block_value, dict)
        for value in (
            [block_value.get("bbox")]
            + [
                entry_value.get("answer_bbox")
                for entry_value in block_value.get("entries", [])
                if isinstance(entry_value, dict)
                and entry_value.get("answer_bbox") is not None
            ]
        )
    ]
    if any(not _valid_bbox(value) for value in boxes):
        return -1.0, 0
    score = 0.0
    semantic_matches = 0
    for block_value in candidate.get("blocks", []):
        if not isinstance(block_value, dict):
            continue
        for peer in peers:
            compatible_peers = [
                peer_block
                for peer_block in peer.get("blocks", [])
                if isinstance(peer_block, dict)
                and _same_solution_unit(block_value, peer_block)
            ]
            if compatible_peers:
                semantic_matches += 1
                block_scores = [
                    _bbox_pair(block_value.get("bbox"), peer_block.get("bbox"))[0]
                    for peer_block in compatible_peers
                ]
                score += max(block_scores, default=0.0) * 4
                for entry_value in block_value.get("entries", []):
                    if not isinstance(entry_value, dict) or entry_value.get("answer_bbox") is None:
                        continue
                    entry_scores = [
                        _bbox_pair(
                            entry_value.get("answer_bbox"), peer_entry.get("answer_bbox")
                        )[0]
                        for peer_block in compatible_peers
                        for peer_entry in peer_block.get("entries", [])
                        if isinstance(peer_entry, dict)
                        and _entry_identity(entry_value) == _entry_identity(peer_entry)
                        and peer_entry.get("answer_bbox") is not None
                    ]
                    score += max(entry_scores, default=0.0)
        quick_entries = _quick_entries(block_value)
        for peer in peers:
            quick_peers = [
                (peer_block, peer_entry)
                for peer_block in peer.get("blocks", [])
                if isinstance(peer_block, dict)
                and _scope_agrees(block_value, peer_block)
                for peer_entry in _quick_entries(peer_block)
            ]
            for entry_value in quick_entries:
                entry_scores = [
                    _bbox_pair(
                        entry_value.get("answer_bbox"),
                        peer_entry.get("answer_bbox"),
                    )[0]
                    for _peer_block, peer_entry in quick_peers
                    if _entry_identity(entry_value) == _entry_identity(peer_entry)
                    and (
                        not _answer_identity(entry_value.get("explicit_answer_text"))
                        or not _answer_identity(peer_entry.get("explicit_answer_text"))
                        or _answer_identity(entry_value.get("explicit_answer_text"))
                        == _answer_identity(peer_entry.get("explicit_answer_text"))
                    )
                ]
                if entry_scores:
                    semantic_matches += 1
                    score += max(entry_scores)
    return score, semantic_matches


def _normalize_provider_coordinates(
    proposals: Sequence[tuple[str, Mapping[str, Any]]],
) -> list[tuple[str, Mapping[str, Any]]]:
    """Normalize every secondary provider against the Codex coordinate frame."""

    codex = [proposal for provider, proposal in proposals if provider == "codex"]
    if len(codex) != 1:
        return []
    anchor = codex[0]
    normalized: list[tuple[str, Mapping[str, Any]]] = [("codex", anchor)]
    provider_order = {"gemini": 0, "qwen": 1}
    for provider, proposal in sorted(
        ((provider, proposal) for provider, proposal in proposals if provider != "codex"),
        key=lambda value: (provider_order.get(value[0], 99), value[0]),
    ):
        transposed = _transpose_proposal_bboxes(proposal)
        identity_score, identity_matches = _coordinate_alignment_score(
            proposal, [anchor]
        )
        transposed_score, transposed_matches = _coordinate_alignment_score(
            transposed, [anchor]
        )
        if (
            transposed_matches > 0
            and transposed_matches >= identity_matches
            and transposed_score > identity_score + 0.08
        ):
            normalized.append((provider, transposed))
        else:
            normalized.append((provider, proposal))
    return normalized


def _quick_entries(block: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if block.get("role") not in {
        SolutionRole.QUICK_ANSWER.value,
        SolutionRole.MIXED.value,
    }:
        return []
    return [
        entry_value
        for entry_value in block.get("entries", [])
        if isinstance(entry_value, dict)
        and entry_value.get("answer_bbox") is not None
        and bool(_answer_identity(entry_value.get("explicit_answer_text")))
    ]


def _combined_entry_pair(
    left: Mapping[str, Any], right: Mapping[str, Any], role: str
) -> dict[str, Any]:
    base = dict(
        left
        if float(left.get("confidence", 0)) >= float(right.get("confidence", 0))
        else right
    )
    base["answer_bbox"] = (
        None
        if role == SolutionRole.DETAILED_SOLUTION.value
        else _bbox_envelope(left.get("answer_bbox"), right.get("answer_bbox"))
    )
    base["explicit_answer_text"] = (
        left.get("explicit_answer_text") or right.get("explicit_answer_text")
    )
    base["stem_fingerprint_tokens"] = list(
        dict.fromkeys(
            list(left.get("stem_fingerprint_tokens", []))
            + list(right.get("stem_fingerprint_tokens", []))
        )
    )[:20]
    base["figure_labels"] = list(
        dict.fromkeys(
            list(left.get("figure_labels", []))
            + list(right.get("figure_labels", []))
        )
    )[:20]
    base["confidence"] = min(
        float(left.get("confidence", 0)), float(right.get("confidence", 0))
    )
    return base


def _merge_entry_pair(
    left: Mapping[str, Any], right: Mapping[str, Any], role: str
) -> dict[str, Any] | None:
    if _entry_identity(left) != _entry_identity(right):
        return None
    left_answer = _answer_identity(left.get("explicit_answer_text"))
    right_answer = _answer_identity(right.get("explicit_answer_text"))
    if left_answer and right_answer and left_answer != right_answer:
        return None
    if role == SolutionRole.QUICK_ANSWER.value:
        _score, compatible = _bbox_pair(
            left.get("answer_bbox"), right.get("answer_bbox")
        )
        if not compatible or not left_answer or not right_answer:
            return None
    elif (
        role != SolutionRole.DETAILED_SOLUTION.value
        and left.get("answer_bbox")
        and right.get("answer_bbox")
    ):
        _score, compatible = _bbox_pair(
            left.get("answer_bbox"), right.get("answer_bbox")
        )
        if not compatible:
            return None
    return _combined_entry_pair(left, right, role)


def _merge_entry_pair_for_review(
    left: Mapping[str, Any], right: Mapping[str, Any], role: str
) -> tuple[dict[str, Any], bool] | None:
    exact = _merge_entry_pair(left, right, role)
    if exact is not None:
        return exact, True
    if role != SolutionRole.QUICK_ANSWER.value or _entry_identity(left) != _entry_identity(right):
        return None
    left_answer = _answer_identity(left.get("explicit_answer_text"))
    right_answer = _answer_identity(right.get("explicit_answer_text"))
    if (
        not left_answer
        or left_answer != right_answer
        or not _valid_bbox(left.get("answer_bbox"))
        or not _valid_bbox(right.get("answer_bbox"))
    ):
        return None
    return _combined_entry_pair(left, right, role), False


def _merge_block_pair(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> tuple[dict[str, Any], bool] | None:
    if not _same_solution_identity(left, right):
        return None
    if not _valid_bbox(left.get("bbox")) or not _valid_bbox(right.get("bbox")):
        return None
    _score, compatible = _bbox_pair(left.get("bbox"), right.get("bbox"))
    flags_exact = _same_solution_unit(left, right)
    if not flags_exact:
        left_label = _semantic_label(left.get("continuation_label") or "")
        right_label = _semantic_label(right.get("continuation_label") or "")
        if not left_label or left_label != right_label:
            return None
    right_entries = list(right.get("entries", []))
    used_right: set[int] = set()
    entries: list[dict[str, Any]] = []
    entries_exact = True
    for left_entry in left.get("entries", []):
        for index, right_entry in enumerate(right_entries):
            if index in used_right:
                continue
            merged = _merge_entry_pair_for_review(
                left_entry, right_entry, str(left["role"])
            )
            if merged is not None:
                used_right.add(index)
                entry_value, entry_exact = merged
                entries.append(entry_value)
                entries_exact = entries_exact and entry_exact
                break
    left_entries = list(left.get("entries", []))
    if (left_entries or right_entries) and not entries:
        if left.get("role") != SolutionRole.DETAILED_SOLUTION.value:
            return None
        entries_exact = False
    base = dict(
        left
        if float(left.get("confidence", 0)) >= float(right.get("confidence", 0))
        else right
    )
    base["bbox"] = _bbox_envelope(left.get("bbox"), right.get("bbox"))
    base["entries"] = entries
    base["starts_on_previous_page"] = bool(
        left.get("starts_on_previous_page") or right.get("starts_on_previous_page")
    )
    base["continues_next_page"] = bool(
        left.get("continues_next_page") or right.get("continues_next_page")
    )
    base["continuation_label"] = (
        left.get("continuation_label") or right.get("continuation_label")
    )
    base["confidence"] = min(
        float(left.get("confidence", 0)), float(right.get("confidence", 0))
    )
    return (
        base,
        compatible
        and flags_exact
        and entries_exact
        and len(entries) == len(left_entries) == len(right_entries),
    )


def _merge_quick_entry_block(
    left_block: Mapping[str, Any],
    left_entry: Mapping[str, Any],
    right_block: Mapping[str, Any],
    right_entry: Mapping[str, Any],
) -> tuple[dict[str, Any], bool] | None:
    if not _scope_agrees(left_block, right_block):
        return None
    left_group = _semantic_label((left_block.get("coverage") or {}).get("group_label") or "")
    right_group = _semantic_label((right_block.get("coverage") or {}).get("group_label") or "")
    if left_group and right_group and left_group != right_group:
        return None
    merged_entry = _merge_entry_pair_for_review(
        left_entry, right_entry, SolutionRole.QUICK_ANSWER.value
    )
    if merged_entry is None:
        return None
    entry_value, exact = merged_entry
    base = copy.deepcopy(
        dict(
            left_block
            if float(left_block.get("confidence", 0))
            >= float(right_block.get("confidence", 0))
            else right_block
        )
    )
    parent, subquestion = _entry_identity(entry_value)
    base.update(
        role=SolutionRole.QUICK_ANSWER.value,
        bbox=list(entry_value["answer_bbox"]),
        coverage={
            "kind": "single" if subquestion is None else "parent_with_subquestions",
            "parent_number": parent,
            "subquestion_labels": [] if subquestion is None else [subquestion],
            "range_start": None,
            "range_end": None,
            "group_label": None,
        },
        entries=[entry_value],
        starts_on_previous_page=False,
        continues_next_page=False,
        continuation_label=None,
        confidence=min(
            float(left_block.get("confidence", 0)),
            float(right_block.get("confidence", 0)),
            float(entry_value.get("confidence", 0)),
        ),
    )
    return base, exact


def _consensus_blocks(
    proposals: Sequence[tuple[str, Mapping[str, Any]]],
) -> tuple[list[dict[str, Any]], set[tuple[Any, ...]], bool, int, bool]:
    candidates = [
        (provider, index, block)
        for provider, result in proposals
        for index, block in enumerate(result.get("blocks", []))
    ]
    edges: list[
        tuple[float, str, int, str, int, dict[str, Any], bool]
    ] = []
    for left_index, (left_provider, left_local, left) in enumerate(candidates):
        for right_provider, right_local, right in candidates[left_index + 1 :]:
            if left_provider == right_provider:
                continue
            if (
                left.get("role") == SolutionRole.MIXED.value
                and _quick_entries(left)
            ) or (
                right.get("role") == SolutionRole.MIXED.value
                and _quick_entries(right)
            ):
                continue
            merged = _merge_block_pair(left, right)
            if merged is None:
                continue
            block, complete = merged
            score, _compatible = _bbox_pair(left.get("bbox"), right.get("bbox"))
            edges.append(
                (
                    score,
                    left_provider,
                    left_local,
                    right_provider,
                    right_local,
                    block,
                    complete,
                )
            )
    used_blocks: set[tuple[str, int]] = set()
    accepted: list[dict[str, Any]] = []
    complete = True
    review_geometry_count = 0
    for _score, left_provider, left_local, right_provider, right_local, block, exact in sorted(
        edges, key=lambda value: (value[0], value[6]), reverse=True
    ):
        left_key = (left_provider, left_local)
        right_key = (right_provider, right_local)
        if left_key in used_blocks or right_key in used_blocks:
            continue
        used_blocks.update((left_key, right_key))
        accepted.append(block)
        complete = complete and exact
        if not exact:
            review_geometry_count += 1

    quick_candidates = [
        (provider, block_index, entry_index, block_value, entry_value)
        for provider, block_index, block_value in candidates
        if (provider, block_index) not in used_blocks
        for entry_index, entry_value in enumerate(_quick_entries(block_value))
    ]
    quick_edges: list[
        tuple[
            float,
            str,
            int,
            int,
            str,
            int,
            int,
            dict[str, Any],
            bool,
        ]
    ] = []
    for left_index, (
        left_provider,
        left_block_index,
        left_entry_index,
        left_block,
        left_entry,
    ) in enumerate(quick_candidates):
        for (
            right_provider,
            right_block_index,
            right_entry_index,
            right_block,
            right_entry,
        ) in quick_candidates[left_index + 1 :]:
            if left_provider == right_provider:
                continue
            merged = _merge_quick_entry_block(
                left_block, left_entry, right_block, right_entry
            )
            if merged is None:
                continue
            block_value, exact = merged
            score, _compatible = _bbox_pair(
                left_entry.get("answer_bbox"), right_entry.get("answer_bbox")
            )
            quick_edges.append(
                (
                    score,
                    left_provider,
                    left_block_index,
                    left_entry_index,
                    right_provider,
                    right_block_index,
                    right_entry_index,
                    block_value,
                    exact,
                )
            )
    used_entries: set[tuple[str, int, int]] = set()
    for (
        _score,
        left_provider,
        left_block_index,
        left_entry_index,
        right_provider,
        right_block_index,
        right_entry_index,
        block_value,
        exact,
    ) in sorted(quick_edges, key=lambda value: (value[0], value[8]), reverse=True):
        left_key = (left_provider, left_block_index, left_entry_index)
        right_key = (right_provider, right_block_index, right_entry_index)
        if left_key in used_entries or right_key in used_entries:
            continue
        used_entries.update((left_key, right_key))
        accepted.append(block_value)
        complete = complete and exact
        if not exact:
            review_geometry_count += 1

    omitted = False
    for provider, block_index, block_value in candidates:
        if (provider, block_index) in used_blocks:
            continue
        entries = _quick_entries(block_value)
        if not entries or any(
            (provider, block_index, entry_index) not in used_entries
            for entry_index in range(len(entries))
        ):
            complete = False
            omitted = True
    primary_blocks = [
        block
        for provider, result in proposals
        if provider == "codex"
        for block in result.get("blocks", [])
    ]

    def primary_order(block: Mapping[str, Any], fallback: int) -> tuple[int, int, int]:
        for block_index, primary in enumerate(primary_blocks):
            if _same_solution_identity(block, primary):
                return block_index, -1, fallback
            if not _scope_agrees(block, primary):
                continue
            for entry_index, primary_entry in enumerate(_quick_entries(primary)):
                if any(
                    _entry_identity(entry) == _entry_identity(primary_entry)
                    for entry in _quick_entries(block)
                ):
                    return block_index, entry_index, fallback
        return len(primary_blocks), fallback, fallback

    accepted = [
        block
        for fallback, block in sorted(
            enumerate(accepted), key=lambda value: primary_order(value[1], value[0])
        )
    ]
    for index, block in enumerate(accepted, 1):
        block["local_id"] = f"consensus-{index:03d}"
    used: set[tuple[Any, ...]] = {
        ("block", provider, index) for provider, index in used_blocks
    } | {
        ("entry", provider, block_index, entry_index)
        for provider, block_index, entry_index in used_entries
    }
    return accepted, used, complete, review_geometry_count, omitted


def _canonical_solution_result(
    page_index: int,
    proposals: Sequence[tuple[str, Mapping[str, Any]]],
) -> tuple[dict[str, Any], bool]:
    blocks, _used, complete, review_geometry_count, omitted = _consensus_blocks(proposals)
    printed_values = [
        result.get("solution_printed_page")
        for _provider, result in proposals
        if result.get("solution_printed_page") is not None
    ]
    printed = next(
        (value for value in printed_values if printed_values.count(value) >= 2),
        printed_values[0] if len(proposals) == 1 and printed_values else None,
    )
    labels = [
        result.get("solution_printed_label")
        for _provider, result in proposals
        if result.get("solution_printed_label")
    ]
    label = next(
        (value for value in labels if labels.count(value) >= 2),
        labels[0] if len(proposals) == 1 and labels else None,
    )
    roles = {block["role"] for block in blocks}
    provider_roles = {
        result.get("page_role")
        for _provider, result in proposals
        if result.get("page_role") in {
            SolutionRole.QUICK_ANSWER.value,
            SolutionRole.MIXED.value,
        }
    }
    page_role = (
        SolutionRole.QUICK_ANSWER.value
        if not roles and len(proposals) >= 2 and len(provider_roles) == len(
            {
                result.get("page_role")
                for _provider, result in proposals
                if result.get("page_role") is not None
            }
        )
        and provider_roles
        else "other"
        if not roles
        else next(iter(roles))
        if len(roles) == 1
        else "mixed"
    )
    base = dict(proposals[0][1]) if proposals else {}
    numbering_scope = dict(
        base.get("numbering_scope")
        or {
            "scope_label": None,
            "reset_detected": False,
            "repeated_numbering_detected": False,
            "visible_number_sequence": [],
            "confidence": 0.0,
        }
    )
    result = {
        "solution_pdf_index": page_index,
        "solution_pdf_page": page_index + 1,
        "solution_printed_label": label,
        "solution_printed_page": printed,
        "page_role": page_role,
        "hierarchy_path": list(base.get("hierarchy_path", [])),
        "numbering_scope": numbering_scope,
        "blocks": blocks,
        "warnings": list(
            dict.fromkeys(
                [
                    str(warning)
                    for _provider, proposal in proposals
                    for warning in proposal.get("warnings", [])
                ]
                + (
                    ["boundary_consensus_review_geometry"]
                    if review_geometry_count
                    else []
                )
                + (["boundary_consensus_omitted_disagreement"] if omitted else [])
            )
        ),
    }
    return result, complete


def _verification_envelope(schema: Mapping[str, Any]) -> dict[str, Any]:
    envelope = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["questions"],
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": {
                    key: value
                    for key, value in schema.items()
                    if key not in {"$schema", "$defs"}
                },
            }
        },
    }
    if "$defs" in schema:
        envelope["$defs"] = schema["$defs"]
    return envelope


def _ordered_neighbor_context(
    problem: ProblemItem,
    asset: SolutionAsset,
    problems: Sequence[ProblemItem],
    assets: Sequence[SolutionAsset],
    problem_source_sha256: str,
    solution_source_sha256: str,
) -> dict[str, Any]:
    problem_scope = sorted(
        (item for item in problems if item.scope_id == problem.scope_id),
        key=lambda item: item.sequence,
    )
    candidate_scope = sorted(
        (
            item
            for item in assets
            if item.scope_id == asset.scope_id
            and item.source_document == asset.source_document
        ),
        key=lambda item: item.sequence,
    )

    def window[T](values: Sequence[T], target: T, sequence: Callable[[T], int]) -> tuple[T, ...]:
        before = [value for value in values if sequence(value) < sequence(target)][-2:]
        after = [value for value in values if sequence(value) > sequence(target)][:2]
        return tuple((*before, *after))

    problem_neighbors = window(problem_scope, problem, lambda item: item.sequence)
    candidate_neighbors = window(candidate_scope, asset, lambda item: item.sequence)
    problem_relations: dict[str, set[str]] = {}
    candidate_relations: dict[str, set[str]] = {}
    for item in problem_neighbors:
        problem_relations.setdefault(item.parent_number, set()).add(
            "before" if item.sequence < problem.sequence else "after"
        )
    for item in candidate_neighbors:
        candidate_relations.setdefault(item.parent_number, set()).add(
            "before" if item.sequence < asset.sequence else "after"
        )
    shared = set(problem_relations) & set(candidate_relations)
    comparable = {
        label
        for label in shared
        if len(problem_relations[label]) == len(candidate_relations[label]) == 1
    }
    if any(problem_relations[label] != candidate_relations[label] for label in comparable):
        order_match = "mismatch"
    elif comparable:
        order_match = "match"
    else:
        order_match = "unknown"
    candidate_source_sha256 = (
        problem_source_sha256
        if asset.source_document == "problem"
        else solution_source_sha256
    )
    context = {
        "problem_sequence": problem.sequence,
        "candidate_sequence": asset.sequence,
        "problem_source_sha256": problem_source_sha256,
        "candidate_source_sha256": candidate_source_sha256,
        "candidate_source_document": asset.source_document,
        "problem_neighbors": [
            {
                "item_id": item.item_id,
                "parent_number": item.parent_number,
                "sequence": item.sequence,
                "relation": "before" if item.sequence < problem.sequence else "after",
            }
            for item in problem_neighbors
        ],
        "candidate_neighbors": [
            {
                "asset_id": item.asset_id,
                "parent_number": item.parent_number,
                "sequence": item.sequence,
                "relation": "before" if item.sequence < asset.sequence else "after",
                "printed_pages": list(item.printed_pages),
                "source_document": item.source_document,
            }
            for item in candidate_neighbors
        ],
        "computed_monotonic_order_match": order_match,
    }
    context["context_sha256"] = quality.identity(context)
    return context


def _verification_prompt(
    template: str,
    problem: ProblemItem,
    asset: SolutionAsset,
    pair: CandidatePair,
    corroborating: SolutionAsset | None = None,
    source_context: Mapping[str, Any] | None = None,
    order_context: Mapping[str, Any] | None = None,
) -> str:
    metadata = {
        "item_id": problem.item_id,
        "scope_id": problem.scope_id,
        "parent_number": problem.parent_number,
        "subquestion_label": problem.subquestion_label,
        "group_label": problem.group_label,
        "column": problem.column,
        "owner_id": problem.owner_id,
        "anchor": None if problem.anchor is None else {
            "segment_id": problem.anchor.segment_id,
            "printed_start": problem.anchor.page_range.start,
            "printed_end": problem.anchor.page_range.end,
        },
    }
    if expanded_candidate_pairs((problem,), (asset,)):
        metadata['search_policy'] = SEARCH_POLICY
        metadata['anchor_provenance'] = {
            'anchor_ids': list(problem.anchor.anchor_ids),
            'item_ids': list(problem.anchor.item_ids),
            'scope_id': problem.anchor.scope_id,
            'boundary_source': problem.anchor.boundary_source,
            'confidence': problem.anchor.confidence,
        }
    if problem.anchor is not None and asset.role == SolutionRole.QUICK_ANSWER:
        metadata["quick_answer_allowed_ranges"] = [
            {
                "kind": "front_matter",
                "printed_start": 1,
                "printed_end": QUICK_ANSWER_FRONT_MATTER_PAGES,
                "requires_quick_answer_heading": True,
                "same_segment_required": True,
            },
            {
                "kind": "near_anchor",
                "printed_start": max(
                    1,
                    problem.anchor.page_range.start - QUICK_ANSWER_LOOKBACK_PAGES,
                ),
                "printed_end": problem.anchor.page_range.end,
                "same_segment_required": True,
            },
        ]
    candidate = asset.signature()
    if source_context is not None:
        metadata['source_bound_context'] = source_context
    if corroborating is not None:
        candidate["corroborating_detailed_solution"] = corroborating.signature()
    prompt = Template(template).safe_substitute(
        QUESTION_ID=problem.item_id,
        QUESTION_METADATA=json.dumps(metadata, ensure_ascii=False),
        CANDIDATE_METADATA=json.dumps(candidate, ensure_ascii=False),
        SEQUENCE_CONTEXT=json.dumps(
            order_context
            or {
                "problem_sequence": problem.sequence,
                "candidate_sequence": asset.sequence,
                "computed_monotonic_order_match": "unknown",
            },
            ensure_ascii=False,
        ),
        ATTACHMENT_MAP=(
            "1. PROBLEM crop\n2. SOLUTION candidate crop"
            + ("\n3. CORROBORATING detailed-solution crop" if corroborating else "")
        ),
    )
    if source_context is not None:
        prompt += '\nSource context attachments (not candidate crop boundaries):\n' + '\n'.join(
            f"{index}. {record['role']} (PDF page {record['pdf_page']})"
            for index, record in enumerate(source_context['source_context_images'], 4))
    return prompt + (
        "\n\n# Transport Envelope\n"
        "For structured API transport, place the one verification object inside "
        'a top-level {"questions":[...]} envelope. Do not change any verification fields.'
    )


def _scope_verification_prompt(
    template: str,
    problem: ProblemItem,
    asset: SolutionAsset,
    pair: CandidatePair,
    *,
    solution_scope_id: str,
    scope_context: Mapping[str, Any],
) -> str:
    """Build the Phase 2 scope-pair prompt without anchor-window metadata."""

    question_metadata = {
        "item_id": problem.item_id,
        "scope_id": problem.scope_id,
        "parent_number": problem.parent_number,
        "subquestion_label": problem.subquestion_label,
        "group_label": problem.group_label,
        "owner_id": problem.owner_id,
        "fingerprints": list(problem.fingerprints),
    }
    context_images = scope_context.get("source_context_images", [])
    visible_context = {**scope_context, "source_context_images": [
        {key: value for key, value in record.items() if key != "path"}
        for record in context_images
    ]} if context_images else dict(scope_context)
    if context_images:
        visible_context["binding"] = {key: value for key, value in scope_context["binding"].items() if key not in {"snapshot_path", "snapshot_sha256", "hint_files"}}
        visible_context["binding"]["hint_files"] = [{"sha256": entry["sha256"]} for entry in scope_context["binding"]["hint_files"]]
    prompt = Template(template).safe_substitute(
        QUESTION_ID=problem.item_id,
        CANDIDATE_ID=asset.asset_id,
        CANDIDATE_ROLE=asset.role.value,
        PROBLEM_SCOPE_ID=problem.scope_id,
        SOLUTION_SCOPE_ID=solution_scope_id,
        QUESTION_METADATA=json.dumps(question_metadata, ensure_ascii=False),
        CANDIDATE_METADATA=json.dumps(asset.signature(), ensure_ascii=False),
        SCOPE_CONTEXT=json.dumps(visible_context, ensure_ascii=False),
        SEQUENCE_CONTEXT=json.dumps(
            {
                "problem_sequence": problem.sequence,
                "candidate_sequence": asset.sequence,
                "fingerprint_similarity": pair.fingerprint_similarity,
                "problem_scope_sequence": scope_context.get("ordered_context", []),
                "episode_sequence": scope_context.get("episode", {}).get("entry_numbers", []),
            },
            ensure_ascii=False,
        ),
        ATTACHMENT_MAP="1. PROBLEM crop\n2. CANDIDATE solution crop" + "".join(
            f"\n{index}. {record['role']} (context only, PDF page {record['pdf_page']})"
            for index, record in enumerate(context_images, 3)
        ),
    )
    return prompt + (
        "\n\n# Transport Envelope\n"
        "For structured API transport, place the one verification object inside "
        'a top-level {"questions":[...]} envelope. Do not change any identity field.'
    )


def _scope_verification_asset(
    asset: SolutionAsset, scope_context: Mapping[str, Any]
) -> SolutionAsset:
    if asset.role != SolutionRole.QUICK_ANSWER:
        return asset
    geometry = scope_context.get("candidate_geometry")
    if not isinstance(geometry, Mapping):
        raise StageHookError("scope_answer_cell_geometry_missing")
    bbox = geometry.get("target_bbox")
    if (
        geometry.get("answer_cell_geometry") != "isolated"
        or geometry.get("target_number") != asset.parent_number
        or not _valid_bbox(bbox)
        or len(asset.parts) != 1
        or geometry.get("pdf_page") != asset.parts[0].page.pdf_page
    ):
        raise StageHookError("scope_answer_cell_geometry_untrusted")
    return replace(
        asset,
        parts=(replace(asset.parts[0], bbox=tuple(int(value) for value in bbox)),),
    )


def _scope_source_reference_errors(
    result: Mapping[str, Any], scope_context: Mapping[str, Any]
) -> tuple[str, ...]:
    if result.get("decision") != "verified":
        return ()
    references = result.get("current_source_corroboration")
    available = scope_context.get("source_references")
    if not isinstance(references, list) or not isinstance(available, list):
        return ("verified result lacks current attached source references",)
    errors: list[str] = []
    for reference in references:
        if not isinstance(reference, Mapping) or not any(
            dict(reference) == candidate
            for candidate in available
            if isinstance(candidate, Mapping)
        ):
            errors.append("source reference is not an attached current-source record")
    required_role = (
        "current_problem"
        if result.get("candidate_role") == SolutionRole.QUICK_ANSWER.value
        else "current_detailed_solution"
    )
    if not any(
        isinstance(reference, Mapping)
        and reference.get("source_role") == required_role
        for reference in references
    ):
        errors.append(f"verified result lacks {required_role} corroboration")
    for evidence in result.get("evidence", []):
        if not isinstance(evidence, Mapping):
            errors.append("verified evidence is not source-bound")
            continue
        index = evidence.get("source_reference_index")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(references):
            errors.append("evidence source_reference_index is out of range")
    return tuple(dict.fromkeys(errors))


def _scope_context_image_errors(
    scope_context: Mapping[str, Any],
) -> tuple[str, ...]:
    records = scope_context.get("source_context_images")
    if not isinstance(records, list) or not any(
        isinstance(record, Mapping) and record.get("episode_heading") is True
        for record in records
    ):
        return ("scope context has no source-bound episode heading",)
    errors = []
    for record in records:
        if not isinstance(record, Mapping):
            errors.append("scope context image record is malformed")
            continue
        path, expected = record.get("path"), record.get("sha256")
        if not isinstance(path, str) or not isinstance(expected, str):
            errors.append("scope context image record is malformed")
            continue
        try:
            if _digest_file(Path(path)) != expected:
                errors.append("scope context image changed")
        except OSError:
            errors.append("scope context image changed")
    return tuple(dict.fromkeys(errors))


def _verification_correction_prompt(
    prompt: str,
    errors: Sequence[str],
    previous: Mapping[str, Any],
) -> str:
    return (
        prompt
        + "\n\n# Correction Pass\n"
        "Goal: Reinspect the same images and return one self-consistent verification.\n\n"
        "Validation errors:\n"
        + json.dumps(list(errors), ensure_ascii=False)
        + "\n\nPrevious response:\n"
        + json.dumps(previous, ensure_ascii=False, sort_keys=True)
        + "\n\nConstraints:\n"
        "- Correct only fields that the images and supplied metadata support.\n"
        "- Do not preserve `verified` merely to satisfy the schema; use `review` when any "
        "required evidence remains unknown or a crop edge is uncertain.\n"
        "- Keep the supplied question id, candidate id, and candidate role unchanged.\n\n"
        "Output: Return a fresh object in the required transport envelope and schema."
    )


@dataclass
class RuntimeState:
    manifest: dict[str, Any]
    run_state: dict[str, Any]
    problem_index: ProblemIndex | None = None
    solution_index: SolutionIndex | None = None
    solution_page_map: tuple[PageIdentity, ...] = ()
    solution_texts: tuple[PageText, ...] = ()
    problem_texts: tuple[PageText, ...] = ()
    problem_hints: dict[int, dict[str, Any]] = field(default_factory=dict)
    block_results: dict[int, dict[str, Any]] = field(default_factory=dict)
    problem_block_results: dict[int, dict[str, Any]] = field(default_factory=dict)
    calibration_results: dict[int, dict[str, Any]] = field(default_factory=dict)
    problem_items_by_id: dict[str, ProblemItem] = field(default_factory=dict)
    problem_hierarchy_by_id: dict[str, tuple[str, ...]] = field(default_factory=dict)
    problem_scope_label_by_id: dict[str, str | None] = field(default_factory=dict)
    problem_crop_verified_by_id: dict[str, bool] = field(default_factory=dict)
    problem_crop_search_eligible_by_id: dict[str, bool] = field(default_factory=dict)
    manifest_items_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    scope_section_paths: dict[str, tuple[str, ...]] = field(default_factory=dict)
    assets_by_id: dict[str, SolutionAsset] = field(default_factory=dict)
    recovery_asset_ids: set[str] = field(default_factory=set)
    approved_recovery_asset_ids: set[str] = field(default_factory=set)
    prior_empty_candidate_ids: set[str] = field(default_factory=set)
    prior_missing_detailed_ids: set[str] = field(default_factory=set)
    prior_link_present: bool = False
    reviewed_context_provenance: Mapping[str, Any] | None = None
    alignment_reviewed_context_provenance: Mapping[str, Any] | None = None
    search_attempted_pages: set[int] = field(default_factory=set)
    expanded_assets: dict[str, SolutionAsset] = field(default_factory=dict)
    scope_input_contexts: dict[tuple[str, str], JsonMap] = field(default_factory=dict)
    search_trace: list[dict[str, Any]] = field(default_factory=list)
    recovery_trace: list[dict[str, Any]] = field(default_factory=list)
    verification_blocked_reason: str | None = None
    verification_retry_context: LinkContext | None = None


def _ordinary_solution_index(
    state: RuntimeState, current: SolutionIndex,
    canonical: tuple[SolutionAsset, ...] | None = None,
) -> SolutionIndex:
    ordinary = {asset.asset_id: state.assets_by_id.get(asset.asset_id, asset)
                for asset in current.assets}
    ordinary.update({asset_id: asset for asset_id, asset in state.assets_by_id.items()
                     if asset.source_document == 'problem' or asset_id in state.recovery_asset_ids})
    if canonical is not None:
        for asset_id, previous in state.expanded_assets.items():
            if ordinary.get(asset_id) == previous:
                ordinary.pop(asset_id)
        ordinary.update({asset.asset_id: asset for asset in canonical})
    index_pages = {page.pdf_index: page for page in current.page_map}
    for asset in ordinary.values():
        if asset.source_document != 'solution':
            continue
        for part in asset.parts:
            existing = index_pages.get(part.page.pdf_index)
            if existing is None or existing.segment_id != part.page.segment_id:
                raise StageHookError('expanded_asset_page_identity_conflict')
            if existing.printed_page is not None and existing != part.page:
                raise StageHookError('expanded_asset_page_identity_conflict')
            if existing.printed_page is None and part.page.printed_page is not None:
                if any(page.pdf_index != part.page.pdf_index
                       and page.segment_id == part.page.segment_id
                       and page.printed_page == part.page.printed_page
                       for page in index_pages.values()):
                    raise StageHookError('expanded_asset_duplicate_printed_page')
            index_pages[part.page.pdf_index] = part.page
    return replace(current, assets=tuple(ordinary.values()),
                   page_map=tuple(index_pages.values()))


CodexRunner = Callable[..., dict[str, Any] | None]
TextReader = Callable[[Path], tuple[PageText, ...]]
AttachmentProvider = Callable[..., tuple[list[Path], str]]
AssetRenderer = Callable[[Path, SolutionAsset, Path, int], None]


class SolutionRuntimeAdapter:
    def __init__(
        self,
        *,
        provider_profile: str = "full",
        codex_runner: CodexRunner = run_codex_contract,
        text_reader: TextReader = read_pdf_text_pages,
        attachment_provider: AttachmentProvider = prepare_page_attachments,
        fingerprint_reader: Callable[
            [Path, Sequence[Mapping[str, Any]]], dict[str, tuple[str, ...]]
        ] = extract_problem_fingerprints,
        gemini_runner: Callable[..., dict[str, Any]] = run_gemini_labeled,
        qwen_runner: Callable[..., tuple[dict[str, Any], dict[str, Any]]] = run_qwen_labeled,
        asset_renderer: AssetRenderer = render_solution_asset_image,
        learning_runtime: SolutionLearningRuntime | None = None,
    ) -> None:
        if provider_profile not in {"full", "codex_only"}:
            raise ProviderProfileError("provider_profile must be full or codex_only")
        self.provider_profile = provider_profile
        self.codex_runner = codex_runner
        self.text_reader = text_reader
        self.attachment_provider = attachment_provider
        self.fingerprint_reader = fingerprint_reader
        self.gemini_runner = gemini_runner
        self.qwen_runner = qwen_runner
        self.asset_renderer = asset_renderer
        self.learning_runtime = learning_runtime
        self.pdf_lock = threading.Lock()
        self.qwen_lock = threading.Lock()
        self.states: dict[tuple[str, str], RuntimeState] = {}

    def hooks(self) -> LinkStageHooks:
        version_payload = {
            "runtime": RUNTIME_VERSION,
            "search_policy": SEARCH_POLICY,
            "provider_profile": self.provider_profile,
            "model": astra_cli.MODEL,
            "policy_version": astra_cli.POLICY_VERSION,
            "scope_index_version": solution_scope_mod.SCOPE_INDEX_VERSION,
            "scope_alignment_version": scope_alignment_mod.SCOPE_ALIGNMENT_VERSION,
            "learning_context": (
                self.learning_runtime.identity if self.learning_runtime is not None else "off"
            ),
            "contracts": {
                path.name: _digest_file(path)
                for path in (
                    HINT_PROMPT,
                    HINT_SCHEMA,
                    BLOCK_PROMPT,
                    BLOCK_SCHEMA,
                    QUICK_PROMPT,
                    VERIFY_PROMPT,
                    VERIFY_SCHEMA,
                    REPAIR_PROMPT,
                    REPAIR_SCHEMA,
                )
            },
        }
        if self.provider_profile == "codex_only":
            version_payload["solution_verification"] = "astra-quality-v2"
        version = hashlib.sha256(
            json.dumps(version_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return LinkStageHooks(
            self.detect_problem_index,
            self.detect_solution_index,
            self.verify_candidate_pairs,
            self.render_solution_assets,
            version=f"solution-runtime-{version}",
            build_scope_alignment=self.build_scope_alignment,
            load_alignment_snapshot_metadata=self.load_alignment_snapshot_metadata,
            provider_profile=self.provider_profile,
            expand_solution_index=self.expand_solution_index,
            verify_scope_pairs=self.verify_pipeline_scope_pairs,
            solution_search_trace=lambda context: self._state(context).search_trace,
        )

    _NATIVE_ROLE_MAP = {
        "quick_answer": SolutionRole.QUICK_ANSWER,
        "detailed_solution": SolutionRole.DETAILED_SOLUTION,
        "mixed": SolutionRole.MIXED,
    }

    @staticmethod
    def _scope_label_tokens(section_path: tuple[str, ...]) -> tuple[str, ...]:
        labels = [
            part.split(":", 1)[1]
            for part in section_path
            if ":" in part and part.split(":", 1)[0] in {"group", "numbering"}
        ]
        return scope_alignment_mod.normalize_tokens(labels)

    @staticmethod
    def _scope_section_tokens(section_path: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = [
            part.split(":", 1)[1] if ":" in part else part
            for part in section_path
            if not part.startswith("column:")
        ]
        return scope_alignment_mod.normalize_tokens(cleaned)

    @staticmethod
    def _reviewed_episode_context(solutions_dir, snapshot, problem_sha, solution_sha):
        """Read a run-local reviewed input; never mutate the source snapshot."""
        path = solutions_dir / "reviewed_episode_context.json"
        if not os.path.lexists(path):
            return {}, None

        def require(condition, reason):
            if not condition:
                raise StageHookError("reviewed episode context " + reason)

        def sha(value):
            return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None

        def strings(value, nonempty=False):
            return (isinstance(value, list) and (bool(value) or not nonempty)
                    and all(isinstance(v, str) and bool(v.strip()) for v in value))

        def local_file(relative):
            require(isinstance(relative, str) and bool(relative)
                    and "\\" not in relative and ":" not in relative
                    and not relative.startswith("/")
                    and all(part not in {"", ".", ".."} for part in relative.split("/")),
                    "proof path invalid")
            candidate = solutions_dir.absolute() / relative
            # Check *every* ancestor, including run/root, before reading bytes.
            for component in reversed((candidate, *candidate.parents)):
                info = component.lstat()
                require(not stat.S_ISLNK(info.st_mode)
                        and not (getattr(info, "st_file_attributes", 0) & 0x400),
                        "symlink or junction forbidden")
            require(candidate.resolve().is_relative_to(solutions_dir.resolve())
                    and stat.S_ISREG(candidate.lstat().st_mode), "proof boundary invalid")
            return candidate

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                require(key not in result, "duplicate JSON field")
                result[key] = value
            return result

        try:
            raw = local_file(path.name).read_bytes()
            payload = json.loads(raw, object_pairs_hook=unique_object)
            require(isinstance(payload, dict) and set(payload) == {
                "schema_version", "mode", "trust_flag", "confirmed", "source_solution_sha256",
                "problem_source_sha256", "corrections"}, "schema fields invalid")
            require(type(payload["schema_version"]) is int and payload["schema_version"] == 1
                    and payload["mode"] == "REVIEW"
                    and type(payload["trust_flag"]) is int and payload["trust_flag"] == 0
                    and type(payload["confirmed"]) is int and payload["confirmed"] == 0,
                    "schema or review authority invalid")
            require(sha(problem_sha) and sha(solution_sha)
                    and sha(payload["problem_source_sha256"])
                    and sha(payload["source_solution_sha256"])
                    and payload["problem_source_sha256"] == problem_sha
                    and payload["source_solution_sha256"] == solution_sha
                    and snapshot.solution_sha256 == solution_sha, "source binding mismatch")
            require(isinstance(payload["corrections"], list) and bool(payload["corrections"]),
                    "corrections missing")
            return SolutionRuntimeAdapter._validated_reviewed_context(
                solutions_dir, snapshot, payload, raw, local_file, require, sha, strings,
                problem_sha, solution_sha
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StageHookError("reviewed episode context invalid input") from exc

    @staticmethod
    def _validated_reviewed_context(solutions_dir, snapshot, payload, raw, local_file,
                                   require, sha, strings, problem_sha, solution_sha):
        overrides = {}
        proofs = {}
        episodes = {episode.episode_id: episode for episode in snapshot.episodes}
        for correction in payload["corrections"]:
            require(isinstance(correction, dict) and set(correction) == {
                "episode_id", "old_section_tokens", "expected_ordered_numbers",
                "expected_order_key", "reviewed_hierarchy_path", "proof_files",
                "reviewer", "observation"}, "correction fields invalid")
            require(isinstance(correction["episode_id"], str)
                    and correction["episode_id"] in episodes
                    and correction["episode_id"] not in overrides, "duplicate or unknown target")
            episode = episodes[correction["episode_id"]]
            order = correction["expected_order_key"]
            require(strings(correction["old_section_tokens"])
                    and strings(correction["expected_ordered_numbers"], True)
                    and isinstance(order, list) and len(order) == 2
                    and all(type(value) is int and value >= 0 for value in order)
                    and list(episode.section_tokens) == correction["old_section_tokens"]
                    and list(episode.entry_numbers) == correction["expected_ordered_numbers"]
                    and [episode.page_span[0], episode.block_span[0]] == order,
                    "target changed")
            require(strings(correction["reviewed_hierarchy_path"], True)
                    and isinstance(correction["reviewer"], str) and bool(correction["reviewer"].strip())
                    and isinstance(correction["observation"], str) and bool(correction["observation"].strip()),
                    "review text invalid")
            overrides[episode.episode_id] = scope_alignment_mod.normalize_tokens(
                correction["reviewed_hierarchy_path"]
            )
            require(bool(overrides[episode.episode_id]), "review tokens empty")
            require(isinstance(correction["proof_files"], list) and bool(correction["proof_files"]),
                    "proof files missing")
            targets = set()
            correction_paths = set()
            for proof in correction["proof_files"]:
                require(isinstance(proof, dict), "proof fields invalid")
                target = proof.get("target")
                require(isinstance(target, str) and target in {
                    "problem_source", "solution_source", "problem_page", "solution_page"},
                    "unknown proof target")
                expected_fields = {"path", "sha256", "target", "source_sha256"}
                if target.endswith("_page"):
                    expected_fields.add("pdf_page")
                    require(type(proof.get("pdf_page")) is int and proof["pdf_page"] > 0,
                            "proof page invalid")
                require(set(proof) == expected_fields and sha(proof["sha256"])
                        and sha(proof["source_sha256"]), "proof fields invalid")
                expected_sha = problem_sha if target.startswith("problem") else solution_sha
                require(proof["source_sha256"] == expected_sha, "proof source mismatch")
                proof_path = local_file(proof["path"])
                # Windows paths are case-insensitive; reject aliases within a correction.
                identity = os.path.normcase(str(proof_path.resolve()))
                require(identity not in correction_paths, "duplicate proof")
                correction_paths.add(identity)
                actual = _digest_file(proof_path)
                require(actual == proof["sha256"], "proof changed")
                if target.endswith("_source"):
                    require(actual == expected_sha, "proof source bytes mismatch")
                require(proof["path"] not in proofs or proofs[proof["path"]] == proof,
                        "conflicting proof binding")
                proofs[proof["path"]] = {**proof, "sha256": actual}
                targets.add(target)
            require(targets == {"problem_source", "solution_source", "problem_page", "solution_page"},
                    "proof targets incomplete")
        inventory = [proofs[key] for key in sorted(proofs)]
        require(local_file("reviewed_episode_context.json").read_bytes() == raw,
                "input changed during validation")
        for proof in inventory:
            require(_digest_file(local_file(proof["path"])) == proof["sha256"],
                    "proof changed during validation")
        provenance = {
            "relative_path": "solutions/reviewed_episode_context.json",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "proof_files": inventory,
            "proof_inventory_sha256": hashlib.sha256(
                json.dumps(inventory, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "episode_ids": sorted(overrides),
            "mode": "REVIEW", "trust_flag": 0, "confirmed": 0,
        }
        return overrides, provenance

    def build_scope_alignment(
        self,
        context: LinkContext,
        problem_index: ProblemIndex,
        solution_index: SolutionIndex,
    ) -> ScopeAlignmentBundle | None:
        """Phase 2b hook: in-process scope index + role-aware alignment.

        Pure cached-file reads (solution_scope) — never a provider call. When
        the run has no block-detection cache the hook returns None and the
        pipeline behaves exactly as before Phase 2.
        """
        solutions_dir = context.run_dir / "solutions"
        consensus_dir = (
            solutions_dir / "detections" / "solution-blocks" / "consensus"
        )
        if self.provider_profile == "codex_only":
            consensus_dir = consensus_dir / "codex_only"
        if not (solutions_dir / "page_map.json").is_file():
            if os.path.lexists(solutions_dir / "reviewed_episode_context.json"):
                raise StageHookError("reviewed episode context scope cache missing")
            return None
        if not any(consensus_dir.glob("page-*.json")):
            if os.path.lexists(solutions_dir / "reviewed_episode_context.json"):
                raise StageHookError("reviewed episode context consensus missing")
            return None
        snapshot = solution_scope_mod.build_scope_index(
            solutions_dir, provider_profile=self.provider_profile
        )
        # Direct in-process build skips read_scope_index(); re-assert the
        # same trust conditions explicitly (fail-closed).
        if solution_index is None:
            raise StageHookError(
                "scope alignment requires the solution index for SHA binding"
            )
        if snapshot.solution_sha256 != solution_index.source_sha256:
            raise StageHookError(
                "scope index is bound to a different solution PDF"
            )
        actual_solution_sha256 = _digest_file(context.solution_path)
        if snapshot.solution_sha256 != actual_solution_sha256:
            raise StageHookError(
                "scope index is bound to different solution PDF bytes"
            )
        for name, version in snapshot.input_cache_versions:
            if version is None:
                raise StageHookError(
                    f"cannot resolve current {name} from runtime source"
                )
        reviewed_context, reviewed_provenance = self._reviewed_episode_context(
            solutions_dir, snapshot, problem_index.source_sha256, snapshot.solution_sha256
        )
        if reviewed_provenance is not None:
            # Validate actual problem PDF before publishing any derived index.
            state = self._state(context)
            if _digest_file(self._source_pdf(state)) != problem_index.source_sha256:
                raise StageHookError("reviewed episode context actual problem source changed")
        reconciliation = _reconcile_solution_printed_page_map(
            solutions_dir,
            consensus_dir,
            expected_source_sha256=snapshot.solution_sha256,
            input_files=snapshot.input_files,
        )
        if reconciliation["changed"]:
            snapshot = solution_scope_mod.build_scope_index(
                solutions_dir, provider_profile=self.provider_profile
            )
        alignment_index_path = solutions_dir / "alignment_index.json"
        if self.provider_profile == "codex_only":
            alignment_index_path = (
                solutions_dir
                / "provider_profiles"
                / "codex_only"
                / "alignment_index.json"
            )
        solution_scope_mod.write_scope_index(snapshot, alignment_index_path)
        alignment_index_sha256 = _digest_file(alignment_index_path)
        state = self._state(context)
        state.solution_page_map = reconciliation["page_map"]

        printed_by_pdf_page = {
            page.pdf_page: page.printed_page for page in problem_index.page_map
        }
        by_scope: dict[str, list[ProblemItem]] = {}
        for item in problem_index.items:
            by_scope.setdefault(item.scope_id, []).append(item)
        profiles: list[scope_alignment_mod.ProblemScopeProfile] = []
        for scope_id, items in sorted(by_scope.items()):
            items.sort(key=lambda item: item.sequence)
            section_path = state.scope_section_paths.get(scope_id, ())
            retained_hierarchy = tuple(
                str(part)
                for item in items
                for pdf_page in state.manifest_items_by_id.get(item.item_id, {}).get("source_pages", ())
                for part in state.problem_hints.get(int(pdf_page) - 1, {}).get("hierarchy_path", ())
                if part
            )
            printed_pages: set[int] = set()
            anchor_ranges: list[tuple[int, int]] = []
            for item in items:
                manifest_item = state.manifest_items_by_id.get(item.item_id, {})
                for pdf_page in manifest_item.get("source_pages", ()):
                    printed = printed_by_pdf_page.get(int(pdf_page))
                    if printed is not None:
                        printed_pages.add(int(printed))
                anchor = item.anchor
                if (
                    anchor is not None
                    and anchor.resolved
                    and anchor.page_range.end is not None
                ):
                    anchor_ranges.append(
                        (anchor.page_range.start, anchor.page_range.end)
                    )
            profiles.append(
                scope_alignment_mod.ProblemScopeProfile(
                    scope_id=scope_id,
                    first_sequence=items[0].sequence,
                    section_tokens=(scope_alignment_mod.normalize_tokens(retained_hierarchy)
                                    if retained_hierarchy else self._scope_section_tokens(section_path)),
                    explicit_label_tokens=self._scope_label_tokens(section_path),
                    ordered_numbers=tuple(item.parent_number for item in items),
                    printed_pages=tuple(sorted(printed_pages)),
                    anchor_printed_ranges=tuple(sorted(set(anchor_ranges))),
                )
            )

        natives_by_episode: dict[str, list[Any]] = {}
        for native in snapshot.native_assets:
            natives_by_episode.setdefault(native.solution_scope_id, []).append(
                native
            )
        episode_profiles: list[scope_alignment_mod.EpisodeProfile] = []
        episode_assets: dict[str, tuple[SolutionAsset, ...]] = {}
        for episode in snapshot.episodes:
            natives = natives_by_episode.get(episode.episode_id, [])
            printed_solution_pages = sorted(
                {
                    part.printed_page
                    for native in natives
                    for part in native.parts
                    if part.printed_page is not None
                }
            )
            episode_profiles.append(
                scope_alignment_mod.EpisodeProfile(
                    episode_id=episode.episode_id,
                    order_key=(episode.page_span[0], episode.block_span[0]),
                    roles=episode.role_mix,
                    section_tokens=reviewed_context.get(episode.episode_id, episode.section_tokens),
                    label_tokens=scope_alignment_mod.normalize_tokens(
                        [episode.raw_label] if episode.raw_label else []
                    ),
                    ordered_numbers=episode.entry_numbers,
                    printed_problem_page_range=episode.printed_problem_page_range,
                    printed_solution_pages=tuple(printed_solution_pages),
                    explicit_answers=tuple(
                        (native.parent_number, native.explicit_answer)
                        for native in natives
                        if native.parent_number and native.explicit_answer
                    ),
                    coverage_complete=episode.coverage_complete,
                )
            )
            converted: list[SolutionAsset] = []
            for native in natives:
                role = self._NATIVE_ROLE_MAP.get(native.role)
                if role is None or native.parent_number is None:
                    continue
                converted.append(
                    SolutionAsset(
                        asset_id=native.native_asset_id,
                        role=role,
                        sequence=native.sequence,
                        scope_id=f"episode:{episode.episode_id}",
                        parent_number=native.parent_number or "",
                        subquestion_label=native.subquestion_label,
                        parts=tuple(
                            SolutionPart(
                                PageIdentity(
                                    part.pdf_index,
                                    part.pdf_page,
                                    part.printed_page,
                                    part.segment_id,
                                ),
                                part.bbox,
                            )
                            for part in native.parts
                        ),
                        fingerprints=tuple(native.fingerprints),
                        explicit_answer=native.explicit_answer,
                        confidence=native.confidence,
                        source_document="solution",
                        review_reason=(
                            "continuation_unresolved"
                            if native.continuation_unresolved
                            else None
                        ),
                    )
                )
            if converted:
                episode_assets[episode.episode_id] = tuple(converted)

        alignment = _runtime_review_candidate_groups(
            scope_alignment_mod.align_scopes(profiles, episode_profiles)
        )
        provenance = {
            "alignment_index_path": alignment_index_path.relative_to(
                context.run_dir
            ).as_posix(),
            "alignment_index_sha256": alignment_index_sha256,
            "provider_profile": self.provider_profile,
            "consensus_path": consensus_dir.relative_to(context.run_dir).as_posix(),
            "problem_source_sha256": problem_index.source_sha256,
            "solution_source_sha256": snapshot.solution_sha256,
            "scope_index_version": solution_scope_mod.SCOPE_INDEX_VERSION,
            "scope_alignment_version": scope_alignment_mod.SCOPE_ALIGNMENT_VERSION,
            "core_version": CORE_VERSION,
            "runtime_version": RUNTIME_VERSION,
            "input_cache_versions": dict(snapshot.input_cache_versions),
            "indexed_pages": list(snapshot.indexed_pages),
            "missing_pages": list(snapshot.missing_pages),
            "input_inventory_sha256": hashlib.sha256(
                json.dumps(
                    sorted(
                        (entry["path"], entry["sha256"])
                        for entry in snapshot.input_files
                    )
                ).encode("utf-8")
            ).hexdigest(),
        }
        if reviewed_provenance is not None:
            provenance["reviewed_episode_context"] = reviewed_provenance
            # A manually supplied title is candidate evidence, not authority.
            alignment = replace(alignment, entries=tuple(
                replace(entry, aligned_episode_id=None, margin=0.0,
                        evidence=tuple(dict.fromkeys((*entry.evidence, "reviewed_episode_context_review_only"))))
                if entry.aligned_episode_id in reviewed_context else entry
                for entry in alignment.entries
            ), edges=tuple(
                replace(edge, trusted=False, score=(0, *edge.score[1:]),
                        status="allowed" if edge.status == "selected" else edge.status)
                if edge.episode_id in reviewed_context else edge
                for edge in alignment.edges
            ))
            _, current_reviewed_provenance = self._reviewed_episode_context(
                solutions_dir, snapshot, problem_index.source_sha256, snapshot.solution_sha256
            )
            if current_reviewed_provenance != reviewed_provenance:
                raise StageHookError("reviewed episode context changed during alignment")
        state.alignment_reviewed_context_provenance = reviewed_provenance
        parent_review_authority = bind_parent_review_authority(
            problem_index.items, episode_assets, snapshot.to_dict(),
            problem_source_sha256=problem_index.source_sha256,
            solution_source_sha256=snapshot.solution_sha256,
        )
        return ScopeAlignmentBundle(
            alignment, episode_assets, provenance,
            parent_review_authority=parent_review_authority,
        )

    def _current_reviewed_context(self, context, state):
        solutions_dir = context.run_dir / "solutions"
        if not os.path.lexists(solutions_dir / "reviewed_episode_context.json"):
            return None
        problem_sha = _digest_file(self._source_pdf(state))
        if problem_sha != state.manifest.get("source_sha256"):
            raise StageHookError("reviewed episode context actual problem source changed")
        snapshot = solution_scope_mod.build_scope_index(
            solutions_dir, provider_profile=self.provider_profile
        )
        _, provenance = self._reviewed_episode_context(
            solutions_dir, snapshot, problem_sha, _digest_file(context.solution_path)
        )
        return provenance

    def _state(self, context: LinkContext) -> RuntimeState:
        key = (str(context.run_dir.resolve()), str(context.solution_path.resolve()))
        if key in self.states:
            state = self.states[key]
            if (os.path.lexists(context.run_dir / "solutions/reviewed_episode_context.json")
                    or getattr(state, "reviewed_context_provenance", None) is not None):
                current = self._current_reviewed_context(context, state)
                try:
                    prior = json.loads((context.run_dir / "solutions/link_manifest.json").read_bytes())
                    old = prior.get("scope_contract", {}).get("provenance", {}).get("reviewed_episode_context")
                except (OSError, ValueError, AttributeError):
                    old = None
                if current != old:
                    state.prior_link_present = False
                    state.prior_empty_candidate_ids.clear()
                    state.prior_missing_detailed_ids.clear()
                state.reviewed_context_provenance = current
            return state
        manifest_path = context.run_dir / "manifest.json"
        run_state_path = context.run_dir / "run_state.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        run_state = json.loads(run_state_path.read_text(encoding="utf-8"))
        items = manifest.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError("problem manifest has no crop items")
        state = RuntimeState(manifest, run_state)
        state.manifest_items_by_id = {
            str(item["unit_id"]): item for item in items if isinstance(item, dict) and item.get("unit_id")
        }
        state.reviewed_context_provenance = self._current_reviewed_context(context, state)
        prior_link = context.run_dir / "solutions" / "link_manifest.json"
        try:
            prior_manifest = json.loads(prior_link.read_text(encoding="utf-8"))
            prior_problem = prior_manifest.get("problem")
            prior_solution = prior_manifest.get("solution")
            prior_profile = prior_manifest.get("provider_profile", "full")
            prior_stage_version = prior_manifest.get("stage_version")
            legacy_full = (
                "provider_profile" not in prior_manifest
                and "stage_version" not in prior_manifest
                and self.provider_profile == "full"
            )
            scope_contract = prior_manifest.get("scope_contract")
            scope_provenance = (
                scope_contract.get("provenance")
                if isinstance(scope_contract, Mapping)
                else None
            )
            scope_profile = (
                scope_provenance.get("provider_profile", "full")
                if isinstance(scope_provenance, Mapping)
                else prior_profile
            )
            state.prior_link_present = bool(
                isinstance(prior_problem, Mapping)
                and isinstance(prior_solution, Mapping)
                and prior_profile == self.provider_profile
                and scope_profile == self.provider_profile
                and (scope_provenance.get("reviewed_episode_context")
                     if isinstance(scope_provenance, Mapping) else None)
                == state.reviewed_context_provenance
                and (
                    legacy_full
                    or prior_stage_version == self.hooks().version
                )
                and prior_problem.get("source_sha256")
                == manifest.get("source_sha256")
                and prior_solution.get("source_sha256")
                == _digest_file(context.solution_path)
            )
            prior_items = prior_manifest.get("items", {})
            prior_assets = prior_manifest.get("assets", {})
        except (OSError, json.JSONDecodeError):
            prior_items = {}
            prior_assets = {}
        if not state.prior_link_present:
            prior_items = {}
            prior_assets = {}
        if not isinstance(prior_assets, Mapping):
            prior_assets = {}
        if isinstance(prior_items, Mapping):
            state.prior_empty_candidate_ids = {
                str(item_id)
                for item_id, decision in prior_items.items()
                if isinstance(decision, Mapping)
                and not decision.get("candidate_ids")
            }
            state.prior_missing_detailed_ids = {
                str(item_id)
                for item_id, decision in prior_items.items()
                if isinstance(decision, Mapping)
                and not any(
                    isinstance(prior_assets.get(asset_id), Mapping)
                    and prior_assets[asset_id].get("kind")
                    in {
                        SolutionRole.DETAILED_SOLUTION.value,
                        SolutionRole.MIXED.value,
                    }
                    for asset_id in decision.get("candidate_ids", [])
                )
            }
        ensemble_path = context.run_dir / "ensemble" / "summary.json"
        ensemble_summary = (
            json.loads(ensemble_path.read_text(encoding="utf-8"))
            if ensemble_path.exists()
            else None
        )
        ensemble_items = {
            str(item["unit_id"]): item
            for item in (
                ensemble_summary.get("items", [])
                if isinstance(ensemble_summary, dict)
                else []
            )
            if isinstance(item, dict) and item.get("unit_id")
        }
        for item_id, item in state.manifest_items_by_id.items():
            verified = (item.get("verification") or {}).get("verdict") == "pass"
            if ensemble_summary is not None:
                verified = verified and ensemble_items.get(item_id, {}).get("verdict") == "pass"
            state.problem_crop_verified_by_id[item_id] = verified
        self.states[key] = state
        return state

    @staticmethod
    def _source_pdf(state: RuntimeState) -> Path:
        return Path(state.manifest.get("source_pdf") or state.run_state["source_pdf"]).resolve()

    def _attachments(
        self,
        pdf_path: Path,
        render_root: Path,
        page_index: int,
        total_pages: int,
        text_hint: str,
        rotations: Mapping[int, int] | None = None,
    ) -> tuple[list[Path], str]:
        # PDFium is not safe when two worker threads open/render the same
        # document concurrently.  Serialize only local rendering; the slow AI
        # calls that follow still run in parallel.
        with self.pdf_lock:
            return self.attachment_provider(
                pdf_path,
                render_root,
                page_index,
                total_pages,
                text_hint,
                rotations,
            )

    @staticmethod
    def _learning_features(
        state: RuntimeState, asset: SolutionAsset,
    ) -> tuple[ExampleKind, FormatFeatures]:
        page_indices = {part.page.pdf_index for part in asset.parts}
        boxes = [
            part.bbox
            for candidate in state.assets_by_id.values()
            for part in candidate.parts
            if part.page.pdf_index in page_indices
        ]
        left = any(box[2] <= 550 for box in boxes)
        right = any(box[0] >= 450 for box in boxes)
        wide = any(box[2] - box[0] >= 700 for box in boxes)
        column_count = 2 if left and right else 1 if wide else None
        numbering_kind = "plain" if asset.parent_number.isdecimal() else None
        if asset.role == SolutionRole.QUICK_ANSWER:
            return ExampleKind.ANSWER_CELL, FormatFeatures(
                "quick_answer", column_count, numbering_kind, None,
            )
        return ExampleKind.DETAIL_BOUNDARY, FormatFeatures(
            "detail", column_count, numbering_kind, None,
        )

    def _learning_prompt(
        self,
        context: LinkContext,
        state: RuntimeState,
        problem: ProblemItem,
        asset: SolutionAsset,
    ) -> LearningPromptResult | None:
        if self.learning_runtime is None:
            return None
        kind, features = self._learning_features(state, asset)
        problem_source = _digest_file(self._source_pdf(state))
        solution_source = _digest_file(context.solution_path)
        return self.learning_runtime.prepare(LearningPromptRequest(
            run_id=str(state.manifest.get("run_id") or context.run_dir.resolve()),
            source_sha=solution_source,
            kind=kind,
            features=features,
            current_case_id=f"{problem.item_id}:{asset.asset_id}",
            current_source_evidence={
                "problem": problem.signature(),
                "asset": asset.signature(),
                "problem_source_sha256": problem_source,
                "solution_source_sha256": solution_source,
            },
            prompt_version=_digest_file(VERIFY_PROMPT),
            schema_version=_digest_file(VERIFY_SCHEMA),
        ))

    def _detect_problem_hint(
        self,
        context: LinkContext,
        state: RuntimeState,
        page_index: int,
        text: str,
        total_pages: int,
    ) -> dict[str, Any] | None:
        source = self._source_pdf(state)
        rotations = {
            int(page) - 1: int(value)
            for page, value in state.run_state.get("rotations", {}).items()
        }
        try:
            images, attachment_map = self._attachments(
                source, context.run_dir, page_index, total_pages, text, rotations
            )
            prompt = _problem_prompt(
                HINT_PROMPT.read_text(encoding="utf-8"),
                source,
                page_index,
                attachment_map,
                text,
            )
            result = self.codex_runner(
                stage="problem_hint",
                prompt=prompt,
                schema_path=HINT_SCHEMA,
                images=images,
                output=context.run_dir / "solutions" / "detections" / "problem-hints" / f"page-{page_index + 1:04d}.json",
                run_dir=context.run_dir,
                model=context.model,
                force=context.force,
            )
            if result is None:
                return None
            for anchor in result.get("anchors", []):
                if isinstance(anchor, dict) and "reference_document" not in anchor:
                    anchor["reference_document"] = _hint_reference_document(
                        anchor.get("raw_text")
                    )
            validate_contract(result, json.loads(HINT_SCHEMA.read_text(encoding="utf-8")))
            if result["problem_pdf_index"] != page_index or result["problem_pdf_page"] != page_index + 1:
                return None
            return result
        except (OSError, ValueError, ContractError):
            return None

    @staticmethod
    def _solution_proposal_valid(
        result: Mapping[str, Any], page_index: int
    ) -> bool:
        return (
            result.get("solution_pdf_index") == page_index
            and result.get("solution_pdf_page") == page_index + 1
        )

    def _solution_boundary_proposal(self, context: LinkContext, provider: str, stage: str, prompt: str, images: list[Path], output: Path, page_index: int, *, cache_only: bool = False) -> dict[str, Any] | None:
        if provider != "codex":
            return None
        schema = json.loads(BLOCK_SCHEMA.read_text(encoding="utf-8"))
        key = _contract_key(f"{stage}-codex", prompt, BLOCK_SCHEMA, images, astra_cli.MODEL)
        if not context.force and output.exists() and not _boundary_error_path(output).exists():
            try:
                wrapper = json.loads(output.read_text(encoding="utf-8"))
                cached = wrapper["result"]
                validate_contract(cached, schema)
                if wrapper.get("meta", {}).get("cache_key") == key and self._solution_proposal_valid(cached, page_index) and not _runtime_receipt_failed(wrapper.get("meta", {}).get("runtime_receipt", {})):
                    return cached
            except (OSError, KeyError, json.JSONDecodeError, ContractError):
                cached = None
        if cache_only:
            return None
        # Retry only a failed page, at most twice per detection request. Never
        # widen the provider timeout or force unrelated, valid page caches.
        value = None
        for _attempt in range(2):
            value = None
            try:
                value = self.codex_runner(stage=f"{stage}-codex", prompt=prompt, schema_path=BLOCK_SCHEMA, images=images, output=output, run_dir=context.run_dir, model=astra_cli.MODEL, force=context.force)
                if isinstance(value, dict):
                    validate_contract(value, schema)
                    if not self._solution_proposal_valid(value, page_index):
                        raise ContractError("invalid_page_identity")
                    break
            except (OSError, subprocess.TimeoutExpired, ValueError) as error:
                reason = ("invalid_page_identity" if str(error) == "invalid_page_identity"
                          else "schema_invalid" if isinstance(error, ContractError)
                          else _provider_error_reason(error))
                _persist_boundary_error(output, "astra", astra_cli.MODEL, reason)
            value = None
            output.unlink(missing_ok=True)
        if not isinstance(value, dict):
            if not _boundary_error_path(output).exists():
                _persist_boundary_error(output, "astra", astra_cli.MODEL, "detection_failed")
            return None
        meta = {}
        if output.exists():
            try:
                meta = json.loads(output.read_text(encoding="utf-8")).get("meta", {})
            except (OSError, json.JSONDecodeError):
                meta = {}
        meta.update({"model": astra_cli.MODEL, "policy_version": astra_cli.POLICY_VERSION, "cache_key": key})
        _atomic_json(output, {"meta": meta, "result": value})
        _boundary_error_path(output).unlink(missing_ok=True)
        return value

    def _legacy_page_calibration(
        self,
        context: LinkContext,
        prompt: str,
        images: list[Path],
        page_index: int,
    ) -> dict[str, Any] | None:
        """Reuse two-provider page identity without reusing calibration blocks."""

        raw_root = (
            context.run_dir
            / "solutions"
            / "detections"
            / "solution-blocks"
            / "raw"
        )
        proposals = [
            self._solution_boundary_proposal(
                context,
                provider,
                "solution-blocks",
                prompt,
                images,
                raw_root / provider / f"page-{page_index + 1:04d}.json",
                page_index,
                cache_only=True,
            )
            for provider in ("codex", "gemini")
        ]
        if any(value is None for value in proposals):
            return None
        left, right = proposals
        assert left is not None and right is not None
        if (
            not self._solution_page_metadata_agrees(left, right)
            or left.get("solution_printed_page") is None
        ):
            return None
        calibrated = copy.deepcopy(dict(left))
        calibrated["blocks"] = []
        calibrated["warnings"] = list(
            dict.fromkeys(
                list(calibrated.get("warnings", []))
                + ["page_identity_reused_from_full_detection"]
            )
        )
        return calibrated

    @staticmethod
    def _solution_page_metadata_agrees(
        left: Mapping[str, Any], right: Mapping[str, Any]
    ) -> bool:
        return (
            left.get("solution_printed_page") == right.get("solution_printed_page")
            and left.get("solution_printed_label")
            == right.get("solution_printed_label")
        )

    def _solution_boundary_consensus(self, context: LinkContext, stage: str, prompt: str, images: list[Path], page_index: int) -> dict[str, Any] | None:
        root = context.run_dir / "solutions" / "detections" / stage
        raw = root / "raw" / "codex" / f"page-{page_index + 1:04d}.json"
        value = self._solution_boundary_proposal(context, "codex", stage, prompt, images, raw, page_index)
        directory = root / "consensus"
        if self.provider_profile == "codex_only":
            directory /= "codex_only"
        if value is None:
            (directory / raw.name).unlink(missing_ok=True)
            return None
        canonical = copy.deepcopy(value)
        canonical["warnings"] = list(dict.fromkeys(list(canonical.get("warnings", [])) + ["astra_current_evidence_authority_pending"]))
        payload = {"meta": {"providers": ["astra"], "model": astra_cli.MODEL, "policy_version": astra_cli.POLICY_VERSION, "complete_primary_agreement": False, "provider_profile": self.provider_profile, "raw_sha256": _digest_file(raw)}, "result": canonical}
        consensus = directory / raw.name
        try:
            if json.loads(consensus.read_text(encoding="utf-8")) == payload:
                return canonical
        except (OSError, json.JSONDecodeError):
            pass
        _atomic_json(consensus, payload)
        return canonical

    def _detect_solution_block(
        self,
        context: LinkContext,
        state: RuntimeState,
        page_index: int,
        text: str,
        total_pages: int,
        target_labels: Sequence[str] = (),
        source_document: str = "solution",
    ) -> dict[str, Any] | None:
        calibration_only = bool(target_labels)
        source_pdf = (
            self._source_pdf(state)
            if source_document == "problem"
            else context.solution_path
        )
        cache = (
            state.calibration_results
            if calibration_only
            else state.problem_block_results
            if source_document == "problem"
            else state.block_results
        )
        if page_index in cache:
            return cache[page_index]
        try:
            render_root = (
                context.run_dir
                / "solutions"
                / "render"
                / _digest_file(source_pdf)[:12]
            )
            images, attachment_map = self._attachments(
                source_pdf, render_root, page_index, total_pages, text, None
            )
            prompt = _solution_prompt(
                BLOCK_PROMPT.read_text(encoding="utf-8"),
                source_pdf,
                page_index,
                attachment_map,
                text,
            )
            if target_labels:
                prompt += (
                    "\n\n# Targeted Front-Matter Quick-Answer Probe\n\n"
                    "This is a bounded page-map calibration for a possible dense, book-wide "
                    "quick-answer table. Report the visible printed page and page_role accurately, "
                    "but return an empty blocks array at this calibration stage even when answers "
                    "are visible. A separate target locator will extract only the requested entries.\n"
                    "Requested targets: "
                    + json.dumps(list(target_labels), ensure_ascii=False)
                )
                if self.provider_profile == "full":
                    migrated = self._legacy_page_calibration(
                        context, prompt, images, page_index
                    )
                    if migrated is not None:
                        cache[page_index] = migrated
                        return migrated
            result = self._solution_boundary_consensus(
                context,
                "solution-page-calibration"
                if calibration_only
                else "problem-solution-blocks"
                if source_document == "problem"
                else "solution-blocks",
                prompt,
                images,
                page_index,
            )
            if result is None:
                return None
            validate_contract(result, json.loads(BLOCK_SCHEMA.read_text(encoding="utf-8")))
            if result["solution_pdf_index"] != page_index or result["solution_pdf_page"] != page_index + 1:
                return None
            cache[page_index] = result
            return result
        except (OSError, ValueError, ContractError):
            return None

    def _refine_quick_answer_blocks(
        self,
        context: LinkContext,
        state: RuntimeState,
        page_index: int,
        text: str,
        total_pages: int,
    ) -> None:
        existing = state.block_results.get(page_index)
        if not isinstance(existing, dict):
            return
        page = next(
            (
                value
                for value in state.solution_page_map
                if value.pdf_index == page_index
            ),
            None,
        )
        relevant_problem_ids = {
            problem.item_id
            for problem in (state.problem_index.items if state.problem_index else ())
            if page is not None
            and page.printed_page is not None
            and _problem_allows_solution_page(
                problem, page.printed_page, SolutionRole.QUICK_ANSWER
            )
        }
        raw_targets = [
            str(item.get("printed_number") or "").strip()
            for item_id, item in state.manifest_items_by_id.items()
            if not relevant_problem_ids or item_id in relevant_problem_ids
            if str(item.get("printed_number") or "").strip()
        ]
        target_keys = {
            (problem.parent_number, problem.subquestion_label)
            for problem in (state.problem_index.items if state.problem_index else ())
            if not relevant_problem_ids or problem.item_id in relevant_problem_ids
        }
        if not target_keys:
            target_keys = {
                _split_number(
                    str(item.get("printed_number") or ""), item.get("unit_kind")
                )
                for item_id, item in state.manifest_items_by_id.items()
                if not relevant_problem_ids or item_id in relevant_problem_ids
                if str(item.get("printed_number") or "").strip()
            }
        expected: set[tuple[str, str | None]] = set()
        existing_with_bbox: set[tuple[str, str | None]] = set()
        dense_table = False
        for block in existing.get("blocks", []):
            if block.get("role") != SolutionRole.QUICK_ANSWER.value:
                continue
            block_path = _semantic_path(block.get("hierarchy_path", []))
            if _book_variant_score(state, block_path) < 0:
                continue
            for entry in block.get("entries", []):
                parent = self._entry_parent(block, entry)
                if parent is None:
                    continue
                key = (parent, normalize_subquestion_label(entry.get("subquestion_label")))
                if key in target_keys:
                    expected.add(key)
                    if entry.get("answer_bbox"):
                        existing_with_bbox.add(key)
            coverage = block.get("coverage", {})
            try:
                start = int(
                    _split_scoped_number(
                        str(coverage.get("range_start")),
                        None,
                        block.get("numbering_scope_label"),
                    )[0]
                )
                end = int(
                    _split_scoped_number(
                        str(coverage.get("range_end")),
                        None,
                        block.get("numbering_scope_label"),
                    )[0]
                )
            except (TypeError, ValueError):
                continue
            dense_table = dense_table or end - start + 1 > len(target_keys)
            expected.update(
                key
                for key in target_keys
                if key[0].isdigit() and start <= int(key[0]) <= end
            )
        if (
            not expected
            and existing.get("page_role")
            in {SolutionRole.QUICK_ANSWER.value, SolutionRole.MIXED.value}
        ):
            expected.update(target_keys)
        if not expected or (expected <= existing_with_bbox and not dense_table):
            return
        try:
            render_root = (
                context.run_dir
                / "solutions"
                / "render"
                / _digest_file(context.solution_path)[:12]
            )
            images, attachment_map = self._attachments(
                context.solution_path,
                render_root,
                page_index,
                total_pages,
                text,
                None,
            )
            prompt = Template(QUICK_PROMPT.read_text(encoding="utf-8")).safe_substitute(
                DOCUMENT_NAME=context.solution_path.name,
                PDF_INDEX=page_index,
                PDF_PAGE=page_index + 1,
                TARGET_QUESTION_LABELS=json.dumps(raw_targets, ensure_ascii=False),
                TARGET_QUESTION_UNITS=json.dumps(
                    [
                        {
                            "item_id": problem.item_id,
                            "scope_id": problem.scope_id,
                            "parent_number": problem.parent_number,
                            "subquestion_label": problem.subquestion_label,
                            "hierarchy_path": list(
                                state.problem_hierarchy_by_id.get(problem.item_id, ())
                            ),
                            "anchor_start": (
                                problem.anchor.page_range.start
                                if problem.anchor is not None
                                else None
                            ),
                            "anchor_end": (
                                problem.anchor.page_range.end
                                if problem.anchor is not None
                                else None
                            ),
                        }
                        for problem in (state.problem_index.items if state.problem_index else ())
                        if not relevant_problem_ids
                        or problem.item_id in relevant_problem_ids
                    ],
                    ensure_ascii=False,
                ),
                ATTACHMENT_MAP=attachment_map,
                TEXT_HINT=text or "(텍스트 레이어 없음)",
            )
            result = self._solution_boundary_consensus(
                context,
                "solution-quick-targets",
                prompt,
                images,
                page_index,
            )
            if result is None:
                return
            validate_contract(result, json.loads(BLOCK_SCHEMA.read_text(encoding="utf-8")))
            refined = [
                block
                for block in result.get("blocks", [])
                if block.get("role") == SolutionRole.QUICK_ANSWER.value
            ]
            located = {
                (
                    self._entry_parent(block, entry),
                    normalize_subquestion_label(entry.get("subquestion_label")),
                )
                for block in refined
                for entry in block.get("entries", [])
                if self._entry_parent(block, entry) is not None
                and entry.get("answer_bbox")
            }
            expected.intersection_update(located)
            if not expected:
                return
            refined_blocks: list[dict[str, Any]] = []
            refined_entries: dict[
                tuple[tuple[str, ...], str, str | None], Mapping[str, Any]
            ] = {}
            conflicted_entries: set[
                tuple[tuple[str, ...], str, str | None]
            ] = set()
            for block in refined:
                kept_entries: list[dict[str, Any]] = []
                for entry in block.get("entries", []):
                    parent = self._entry_parent(block, entry)
                    entry_key = (
                        parent,
                        normalize_subquestion_label(entry.get("subquestion_label")),
                    )
                    if parent is None or entry_key not in expected:
                        continue
                    scoped_key = (_scope_tokens(block), parent, entry_key[1])
                    if scoped_key in conflicted_entries:
                        continue
                    prior = refined_entries.get(scoped_key)
                    if prior is not None:
                        prior_answer = _answer_identity(prior.get("explicit_answer_text"))
                        answer = _answer_identity(entry.get("explicit_answer_text"))
                        _score, compatible = _bbox_pair(
                            prior.get("answer_bbox"), entry.get("answer_bbox")
                        )
                        if (prior_answer and answer and prior_answer != answer) or not compatible:
                            refined_entries.pop(scoped_key, None)
                            conflicted_entries.add(scoped_key)
                            continue
                        continue
                    entry["stem_fingerprint_tokens"] = list(
                        dict.fromkeys(
                            [TARGETED_QUICK_FINGERPRINT]
                            + list(entry.get("stem_fingerprint_tokens", []))
                        )
                    )
                    refined_entries[scoped_key] = entry
                    kept_entries.append(entry)
                if kept_entries:
                    refined_block = copy.deepcopy(block)
                    refined_block["entries"] = kept_entries
                    refined_blocks.append(refined_block)
            if conflicted_entries:
                for block in refined_blocks:
                    block["entries"] = [
                        entry
                        for entry in block["entries"]
                        if (
                            _scope_tokens(block),
                            self._entry_parent(block, entry),
                            normalize_subquestion_label(
                                entry.get("subquestion_label")
                            ),
                        )
                        not in conflicted_entries
                    ]
                refined_blocks = [
                    block for block in refined_blocks if block["entries"]
                ]
            if not refined_blocks:
                return
            updated = dict(existing)
            updated_blocks: list[dict[str, Any]] = []
            consumed: set[tuple[tuple[str, ...], str, str | None]] = set()
            for block in existing.get("blocks", []):
                if block.get("role") != SolutionRole.QUICK_ANSWER.value:
                    updated_blocks.append(block)
                    continue
                merged_entries: list[dict[str, Any]] = []
                for entry in block.get("entries", []):
                    scoped_key = (
                        _scope_tokens(block),
                        self._entry_parent(block, entry),
                        normalize_subquestion_label(entry.get("subquestion_label")),
                    )
                    replacement = refined_entries.get(scoped_key)
                    if replacement is None:
                        merged_entries.append(copy.deepcopy(entry))
                    elif scoped_key not in consumed:
                        merged_entries.append(copy.deepcopy(replacement))
                        consumed.add(scoped_key)
                retained = copy.deepcopy(block)
                retained["entries"] = merged_entries
                updated_blocks.append(retained)
            for block in refined_blocks:
                unmerged = [
                    entry
                    for entry in block["entries"]
                    if (
                        _scope_tokens(block),
                        self._entry_parent(block, entry),
                        normalize_subquestion_label(entry.get("subquestion_label")),
                    )
                    not in consumed
                ]
                if unmerged:
                    appended = copy.deepcopy(block)
                    appended["entries"] = unmerged
                    updated_blocks.append(appended)
            updated["blocks"] = updated_blocks
            state.block_results[page_index] = updated
        except (OSError, ValueError, ContractError):
            return

    def _ensure_solution_page_map(
        self, context: LinkContext, state: RuntimeState
    ) -> tuple[PageIdentity, ...]:
        if state.solution_page_map:
            return state.solution_page_map
        try:
            with self.pdf_lock:
                texts = self.text_reader(context.solution_path)
        except Exception as exc:
            raise StageHookError(f"cannot read solution PDF: {exc}") from exc
        state.solution_texts = texts
        printed = recover_internal_printed_page_gaps(
            stabilize_printed_page_candidates(
                {
                page.pdf_index: (
                    printed_page_from_text(page.text)
                    if page.quality == "usable"
                    else None
                )
                for page in texts
                }
            )
        )
        anchor_ranges = [
            (
                int(anchor["printed_start"]),
                int(anchor["printed_end"])
                if anchor.get("printed_end") is not None
                else None,
            )
            for hint in state.problem_hints.values()
            for anchor in hint.get("anchors", [])
            if anchor.get("reference_document", "unknown") in {"solution", "unknown"}
        ]
        anchor_starts = {start for start, _end in anchor_ranges}
        anchor_required_pages = anchor_starts | {
            end for _start, end in anchor_ranges if end is not None
        }
        probe_indices: set[int] = set()
        front_probe_indices = {
            page.pdf_index
            for page in texts[:QUICK_ANSWER_FRONT_MATTER_PAGES]
            if page.quality != "usable" or _looks_like_quick_answer_text(page.text)
        }
        front_target_labels = tuple(
            dict.fromkeys(
                str(label).strip()
                for hint in state.problem_hints.values()
                for label in hint.get("numbering_scope", {}).get(
                    "visible_question_labels", []
                )
                if str(label).strip()
            )
        )
        for start in anchor_starts:
            # Physical-page equality is only a search hypothesis; VLM must confirm it.
            for index in range(
                start - QUICK_ANSWER_LOOKBACK_PAGES - 1,
                start + 1,
            ):
                if 0 <= index < len(texts):
                    probe_indices.add(index)
        probe_indices.update(
            end - 1
            for _start, end in anchor_ranges
            if end is not None and 0 <= end - 1 < len(texts)
        )
        probe_indices.update(front_probe_indices)
        for page in texts:
            raw = untrusted_edge_number(page.text) if page.quality == "usable" else None
            if raw in anchor_required_pages:
                probe_indices.add(page.pdf_index)
        # Probe neighbors of already confirmed anchors so a start can expand to
        # the next independently confirmed boundary without assuming an offset.
        for index, value in printed.items():
            if value in anchor_required_pages:
                probe_indices.update(
                    neighbor for neighbor in (index - 1, index + 1) if 0 <= neighbor < len(texts)
                )
        attempted: set[int] = set()
        pending = set(probe_indices)
        # A scanned answer PDF may have covers/inserts, so printed page 43 is
        # not necessarily physical page 43.  Any VLM-confirmed page gives a
        # local offset hypothesis; follow that prediction for at most two
        # additional rounds instead of assuming one global offset.
        for _round in range(3):
            batch = sorted(
                index
                for index in pending
                if index not in attempted and printed.get(index) is None
            )
            if batch:
                with ThreadPoolExecutor(max_workers=max(1, context.workers)) as executor:
                    futures = {
                        executor.submit(
                            self._detect_solution_block,
                            context,
                            state,
                            page_index,
                            texts[page_index].text,
                            len(texts),
                            front_target_labels
                            if page_index in front_probe_indices
                            else (),
                        ): page_index
                        for page_index in batch
                    }
                    for future in as_completed(futures):
                        page_index = futures[future]
                        attempted.add(page_index)
                        detected = future.result()
                        if detected is not None:
                            printed[page_index] = detected.get("solution_printed_page")
            printed = recover_internal_printed_page_gaps(printed)
            confirmed = [
                (index, value)
                for index, value in printed.items()
                if value is not None
            ]
            for start, end in anchor_ranges:
                if end is None or end <= start:
                    continue
                start_indices = [index for index, value in confirmed if value == start]
                end_indices = [index for index, value in confirmed if value == end]
                if (
                    len(start_indices) == 1
                    and len(end_indices) == 1
                    and end_indices[0] - start_indices[0] == end - start
                ):
                    for offset in range(end - start + 1):
                        printed[start_indices[0] + offset] = start + offset
            confirmed = [
                (index, value)
                for index, value in printed.items()
                if value is not None
            ]
            missing_required = anchor_required_pages - {
                value for _index, value in confirmed
            }
            if not missing_required:
                break
            predicted: set[int] = set()
            for start in missing_required:
                for index, value in confirmed:
                    target = index + (start - value)
                    predicted.update(
                        neighbor
                        for neighbor in (target - 1, target, target + 1)
                        if 0 <= neighbor < len(texts)
                    )
            pending = predicted
        state.solution_page_map = build_page_map(len(texts), printed, "solution")
        _atomic_json(
            context.run_dir / "solutions" / "page_map.json",
            {
                "source_sha256": _digest_file(context.solution_path),
                "pages": [page.to_dict() for page in state.solution_page_map],
            },
        )
        return state.solution_page_map

    def detect_problem_index(self, context: LinkContext) -> ProblemIndex:
        state = self._state(context)
        if state.problem_index is not None:
            return state.problem_index
        source = self._source_pdf(state)
        expected_sha = state.manifest.get("source_sha256") or state.run_state.get("source_sha256")
        if expected_sha and _digest_file(source) != expected_sha:
            raise ValueError("problem source SHA-256 differs from the crop run")
        try:
            with self.pdf_lock:
                problem_texts = self.text_reader(source)
        except Exception as exc:
            raise StageHookError(f"cannot read problem PDF: {exc}") from exc
        total_pages = int(state.run_state.get("total_pages") or len(problem_texts))
        state.problem_texts = problem_texts
        source_pages = sorted(
            {
                int(page) - 1
                for item in state.manifest_items_by_id.values()
                for page in item.get("source_pages", [])
                if int(page) > 0
            }
        )
        text_by_page = {page.pdf_index: page.text for page in problem_texts}
        with ThreadPoolExecutor(max_workers=max(1, context.workers)) as executor:
            futures = {
                executor.submit(
                    self._detect_problem_hint,
                    context,
                    state,
                    page_index,
                    text_by_page.get(page_index, ""),
                    total_pages,
                ): page_index
                for page_index in source_pages
            }
            for future in as_completed(futures):
                hint = future.result()
                if hint is not None:
                    state.problem_hints[futures[future]] = hint
        if source_pages and not state.problem_hints:
            raise StageHookError("all required problem-hint detections failed")

        try:
            with self.pdf_lock:
                fingerprints = self.fingerprint_reader(
                    source, tuple(state.manifest_items_by_id.values())
                )
        except Exception as exc:
            raise StageHookError(f"cannot read problem crop fingerprints: {exc}") from exc
        prepared_items: list[dict[str, Any]] = []
        collision_values: dict[tuple[int, str | None, str], set[tuple[str, str | None]]] = {}
        for sequence, item in enumerate(state.manifest_items_by_id.values(), 1):
            first_page = int((item.get("source_pages") or [1])[0]) - 1
            page_hint = state.problem_hints.get(first_page, {})
            number_scope_label = _hint_number_scope_label(page_hint)
            parent, sublabel = _split_scoped_number(
                str(item.get("printed_number") or item["unit_id"]),
                item.get("unit_kind"),
                number_scope_label,
            )
            path = tuple(page_hint.get("hierarchy_path", []))
            numbering_path = _numbering_scope_path(page_hint, path)
            item_id = str(item["unit_id"])
            crop_verified = state.problem_crop_verified_by_id.get(item_id, False)
            state.problem_crop_search_eligible_by_id[item_id] = _crop_search_eligible(
                item, page_hint, crop_verified=crop_verified
            )
            group_label = _semantic_label(item.get("group_label") or "") or None
            column = str(item.get("column") or "").strip().casefold() or None
            column = column if column in {"left", "right"} else None
            owner_id = str(item.get("owner_question_id") or item_id)
            collision_key = (first_page, group_label, parent)
            collision_values.setdefault(collision_key, set()).add((owner_id, column))
            prepared_items.append(
                {
                    "sequence": sequence,
                    "item_id": item_id,
                    "parent": parent,
                    "sublabel": sublabel,
                    "path": path,
                    "numbering_path": numbering_path,
                    "group_label": group_label,
                    "column": column,
                    "owner_id": owner_id,
                    "collision_key": collision_key,
                    "number_scope_label": number_scope_label,
                }
            )
        column_collisions = {
            key
            for key, values in collision_values.items()
            if len({owner for owner, _column in values}) > 1
            and len({column for _owner, column in values if column}) > 1
        }
        numbered: list[NumberedItem] = []
        layout_by_item: dict[str, tuple[str | None, str | None, str]] = {}
        for prepared in prepared_items:
            item_id = prepared["item_id"]
            problem_path = list(_semantic_path(prepared["path"]))
            if prepared["group_label"]:
                problem_path.append(prepared["group_label"])
            state.problem_hierarchy_by_id[item_id] = tuple(dict.fromkeys(problem_path))
            state.problem_scope_label_by_id[item_id] = (
                _semantic_label(prepared["number_scope_label"])
                if prepared["number_scope_label"]
                else None
            )
            layout_by_item[item_id] = (
                prepared["group_label"],
                prepared["column"],
                prepared["owner_id"],
            )
            numbered.append(
                NumberedItem(
                    item_id,
                    prepared["parent"],
                    prepared["sequence"],
                    _layout_scope_path(
                        prepared["numbering_path"],
                        prepared["group_label"],
                        prepared["column"],
                        column_collision=prepared["collision_key"] in column_collisions,
                    ),
                    prepared["sublabel"],
                    prepared["owner_id"],
                )
            )
        common_book_id = str(state.manifest.get("source_sha256") or state.run_state.get("source_sha256") or _digest_file(source))
        scoped = assign_numbering_scopes(numbered, common_book_id)
        scope_by_item = {item.item.item_id: item for item in scoped}
        state.scope_section_paths = {
            item.scope.identity: item.scope.section_path for item in scoped
        }
        printed_problem = recover_internal_printed_page_gaps(
            stabilize_printed_page_candidates(
                {
                    page.pdf_index: (
                        printed_page_from_text(page.text)
                        if page.quality == "usable"
                        else None
                    )
                    for page in problem_texts
                }
            )
        )
        printed_problem.update(
            {
                page: hint.get("problem_printed_page")
                for page, hint in state.problem_hints.items()
                if hint.get("problem_printed_page") is not None
            }
        )
        for page_index, printed_page in _source_bound_problem_page_candidates(
            state.problem_hints, total_pages
        ).items():
            current = printed_problem.get(page_index)
            if current in {None, printed_page}:
                printed_problem[page_index] = printed_page
        problem_page_map = build_page_map(total_pages, printed_problem, "problem")
        solution_map = self._ensure_solution_page_map(context, state)

        raw_anchors: list[StartAnchor] = []
        for page_index, hint in sorted(state.problem_hints.items()):
            page_items = [
                item_id
                for item_id, item in state.manifest_items_by_id.items()
                if page_index + 1 in item.get("source_pages", [])
            ]
            for anchor_index, anchor in enumerate(hint.get("anchors", []), 1):
                reference_document = str(
                    anchor.get("reference_document")
                    or _hint_reference_document(anchor.get("raw_text"))
                ).casefold()
                labels: set[str] = set()
                for label in anchor.get("target_question_labels", []):
                    if not str(label).strip():
                        continue
                    try:
                        labels.add(
                            _split_scoped_number(
                                str(label),
                                None,
                                _hint_number_scope_label(hint),
                            )[0]
                        )
                    except ValueError:
                        continue
                targets = [
                    item_id
                    for item_id in page_items
                    if not labels or scope_by_item[item_id].parent_number in labels
                ]
                for scope_id in dict.fromkeys(scope_by_item[item_id].scope.identity for item_id in targets):
                    scoped_targets = [
                        item_id for item_id in targets if scope_by_item[item_id].scope.identity == scope_id
                    ]
                    reference_map = (
                        problem_page_map
                        if reference_document == "problem"
                        else solution_map
                        if reference_document == "solution"
                        else (*problem_page_map, *solution_map)
                    )
                    segments = {
                        page.segment_id
                        for page in reference_map
                        if page.printed_page == int(anchor["printed_start"])
                    }
                    segment_id = (
                        "unknown"
                        if reference_document == "unknown"
                        else next(iter(segments))
                        if len(segments) == 1
                        else "ambiguous"
                    )
                    page_range = PrintedPageRange(
                        int(anchor["printed_start"]),
                        int(anchor["printed_end"]) if anchor.get("printed_end") is not None else None,
                        RangeMode.RANGE if anchor["mode"] == "range" else RangeMode.START,
                        str(anchor["raw_text"]),
                    )
                    raw_anchors.append(
                        StartAnchor(
                            f"hint-p{page_index + 1:04d}-a{anchor_index}-{scope_id[-6:]}",
                            tuple(scoped_targets),
                            min(scope_by_item[item_id].item.sequence for item_id in scoped_targets),
                            scope_id,
                            segment_id,
                            page_range,
                            float(anchor["confidence"]) if segments else 0.0,
                            reference_document,
                        )
                    )

        boundary_sequences = {
            min(
                scope_by_item[item_id].item.sequence
                for item_id in state.manifest_items_by_id
                if page_index + 1
                in state.manifest_items_by_id[item_id].get("source_pages", [])
            )
            for page_index, hint in state.problem_hints.items()
            if hint.get("numbering_scope", {}).get("reset_signal") == "yes"
            and any(
                page_index + 1 in item.get("source_pages", [])
                for item in state.manifest_items_by_id.values()
            )
        }
        problem_boundaries = tuple(
            ProblemBoundary(sequence, "numbering_reset")
            for sequence in sorted(boundary_sequences)
        )
        propagated = propagate_problem_anchors(raw_anchors, scoped, problem_boundaries)
        propagated_ids: dict[str, set[str]] = {anchor.anchor_id: set(anchor.item_ids) for anchor in raw_anchors}
        for item_id, anchor in propagated.items():
            propagated_ids.setdefault(anchor.anchor_id, set()).add(item_id)
        enriched = tuple(
            replace(anchor, item_ids=tuple(sorted(propagated_ids[anchor.anchor_id])))
            for anchor in raw_anchors
        )
        # A user may crop only one problem range, so the next problem-book
        # anchor is not always present in the run.  Give the final start anchor
        # a small, explicit probe window (start..start+3).  This covers the
        # common "20쪽" -> 20~23 continuation while hierarchy, numbering,
        # monotonic order and independent VLM verification still guard every
        # candidate.  Full-book runs continue to use the earlier next anchor.
        available_by_segment: dict[str, set[int]] = {}
        for page in (*problem_page_map, *solution_map):
            if page.printed_page is not None:
                available_by_segment.setdefault(page.segment_id, set()).add(page.printed_page)
                available_by_segment.setdefault("unknown", set()).add(page.printed_page)
        fallback_boundaries: list[AnchorBoundary] = []
        for anchor in enriched:
            has_later_anchor = any(
                other.segment_id == anchor.segment_id
                and other.page_range.start > anchor.page_range.start
                for other in enriched
            )
            boundary_page = anchor.page_range.start + FALLBACK_ANCHOR_WINDOW_PAGES
            if (
                not has_later_anchor
                and boundary_page in available_by_segment.get(anchor.segment_id, set())
            ):
                fallback_boundaries.append(
                    AnchorBoundary(anchor.segment_id, boundary_page, "bounded_probe_window")
                )
        unknown_map = tuple(
            replace(page, segment_id="unknown")
            for page in (*problem_page_map, *solution_map)
        )
        spans = tuple(
            span
            for reference_document, page_map in (
                ("problem", problem_page_map),
                ("solution", solution_map),
                ("unknown", unknown_map),
            )
            for span in expand_start_anchors(
                tuple(
                    anchor
                    for anchor in enriched
                    if anchor.reference_document == reference_document
                ),
                page_map,
                tuple(
                    boundary
                    for boundary in fallback_boundaries
                    if any(
                        anchor.reference_document == reference_document
                        and anchor.segment_id == boundary.segment_id
                        for anchor in enriched
                    )
                ),
            )
        )
        span_by_anchor = {anchor_id: span for span in spans for anchor_id in span.anchor_ids}
        bracketed_spans = _recover_bracketed_anchor_spans(
            scoped,
            raw_anchors,
            propagated,
            spans,
            state.problem_crop_search_eligible_by_id,
        )
        fingerprints = {
            item_id: tuple(dict.fromkeys((VISUAL_FINGERPRINT,) + tuple(values)))[:4]
            for item_id, values in fingerprints.items()
        }
        problem_items: list[ProblemItem] = []
        for scoped_item in scoped:
            item_id = scoped_item.item.item_id
            propagated_anchor = propagated.get(item_id)
            span = (
                span_by_anchor.get(propagated_anchor.anchor_id)
                if propagated_anchor
                else bracketed_spans.get(item_id)
            )
            group_label, column, owner_id = layout_by_item[item_id]
            problem_items.append(
                ProblemItem(
                    item_id,
                    scoped_item.item.sequence,
                    scoped_item.scope.identity,
                    scoped_item.parent_number,
                    scoped_item.subquestion_label,
                    span,
                    fingerprints.get(item_id, (VISUAL_FINGERPRINT,)),
                    state.problem_crop_verified_by_id.get(item_id, False),
                    group_label,
                    column,
                    owner_id,
                    state.problem_crop_search_eligible_by_id.get(item_id, False),
                    item_id in state.prior_missing_detailed_ids and not context.force,
                )
            )
        state.problem_index = ProblemIndex(
            common_book_id,
            problem_page_map,
            tuple(problem_items),
        )
        state.problem_index = inherit_missing_anchors(
            state.problem_index,
            {
                item_id: int((item.get("source_pages") or [1])[0]) - 1
                for item_id, item in state.manifest_items_by_id.items()
            },
            problem_boundaries,
        )
        state.problem_items_by_id = {
            item.item_id: item for item in state.problem_index.items
        }
        return state.problem_index

    @staticmethod
    def _entry_parent(block: Mapping[str, Any], entry: Mapping[str, Any]) -> str | None:
        value = entry.get("parent_number") or block.get("coverage", {}).get("parent_number")
        return (
            _split_scoped_number(
                str(value), None, block.get("numbering_scope_label")
            )[0]
            if value is not None and str(value).strip()
            else None
        )

    @classmethod
    def _recovery_target_entries(
        cls, block: Mapping[str, Any], problem: ProblemItem
    ) -> list[tuple[int, Mapping[str, Any]]]:
        result: list[tuple[int, Mapping[str, Any]]] = []
        for index, entry in enumerate(block.get("entries", [])):
            if not isinstance(entry, Mapping):
                continue
            try:
                parent = cls._entry_parent(block, entry)
            except ValueError:
                continue
            if (
                parent == problem.parent_number
                and normalize_subquestion_label(entry.get("subquestion_label"))
                == problem.subquestion_label
            ):
                result.append((index, entry))
        return result

    @classmethod
    def _recovery_block_targets_problem(
        cls,
        block: Mapping[str, Any],
        problem: ProblemItem,
        role: SolutionRole,
    ) -> bool:
        if block.get("role") != role.value:
            return False
        if role == SolutionRole.QUICK_ANSWER:
            return any(
                _valid_bbox(entry.get("answer_bbox"))
                and bool(
                    _recovery_answer_identity(entry.get("explicit_answer_text"))
                )
                for _index, entry in cls._recovery_target_entries(block, problem)
            )
        coverage = block.get("coverage") or {}
        try:
            parent = _optional_parent(coverage.get("parent_number"))
        except ValueError:
            return False
        entries = [
            entry
            for entry in block.get("entries", [])
            if isinstance(entry, Mapping)
        ]
        if problem.subquestion_label is None:
            return (
                parent == problem.parent_number
                and coverage.get("kind")
                in {"single", "continuation", "parent_with_subquestions"}
            ) or any(
                cls._entry_parent(block, entry) == problem.parent_number
                and normalize_subquestion_label(entry.get("subquestion_label")) is None
                for entry in entries
            )
        labels = {
            value
            for raw in coverage.get("subquestion_labels", [])
            if (value := normalize_subquestion_label(raw)) is not None
        }
        return (
            parent == problem.parent_number
            and problem.subquestion_label in labels
        ) or any(
            cls._entry_parent(block, entry) == problem.parent_number
            and normalize_subquestion_label(entry.get("subquestion_label"))
            == problem.subquestion_label
            for entry in entries
        )

    @staticmethod
    def _recovery_block_scope_compatible(
        state: RuntimeState,
        problem: ProblemItem,
        block: Mapping[str, Any],
        role: SolutionRole,
        *,
        allow_identity_fallback: bool = False,
    ) -> bool:
        problem_path = state.problem_hierarchy_by_id.get(problem.item_id, ())
        block_path = _block_semantic_path(block)
        variant_score = _book_variant_score(state, block_path)
        layout_score = _layout_scope_score(
            problem, block, include_numbering_scope=True
        )
        if variant_score < 0 or layout_score < 0:
            return False
        raw_scope = block.get("numbering_scope_label")
        if not block_path and not raw_scope and layout_score == 0:
            # Missing scope text is not an identity disagreement. Recovery still
            # requires the same anchored page/target from both primary workers
            # and exact final-image dual verification.
            return True
        hierarchy_score = _hierarchy_score(
            problem_path,
            block_path,
            state.problem_scope_label_by_id.get(problem.item_id),
            _semantic_label(raw_scope) if raw_scope else None,
        )
        if layout_score > 0 or (set(problem_path) & set(block_path)) - GENERIC_HIERARCHY:
            return True
        return (
            hierarchy_score >= 0
            or variant_score > 0
            or allow_identity_fallback
            and problem.group_label is None
        )

    @classmethod
    def _recovery_block_answers(
        cls,
        block: Mapping[str, Any],
        problem: ProblemItem,
        role: SolutionRole,
    ) -> set[str]:
        entries = (
            [entry for _index, entry in cls._recovery_target_entries(block, problem)]
            if role == SolutionRole.QUICK_ANSWER
            or problem.subquestion_label is not None
            else [
                entry
                for entry in block.get("entries", [])
                if isinstance(entry, Mapping)
                and cls._entry_parent(block, entry) == problem.parent_number
            ]
        )
        return {
            answer
            for entry in entries
            if (
                answer := _recovery_answer_identity(
                    entry.get("explicit_answer_text")
                )
            )
        }

    def _raw_primary_solution_blocks(
        self,
        context: LinkContext,
        page_index: int,
        expected_printed_page: int | None = None,
        stage: str = "solution-blocks",
    ) -> dict[str, list[Mapping[str, Any]]]:
        root = (
            context.run_dir
            / "solutions"
            / "detections"
            / stage
            / "raw"
        )
        proposals: list[tuple[str, Mapping[str, Any]]] = []
        schema = json.loads(BLOCK_SCHEMA.read_text(encoding="utf-8"))
        for provider in ("codex", "gemini"):
            path = root / provider / f"page-{page_index + 1:04d}.json"
            try:
                wrapper = json.loads(path.read_text(encoding="utf-8"))
                result = wrapper["result"]
                validate_contract(result, schema)
            except (OSError, KeyError, json.JSONDecodeError, ContractError):
                return {}
            if not self._solution_proposal_valid(result, page_index):
                return {}
            proposals.append((provider, result))
        if not self._solution_page_metadata_agrees(proposals[0][1], proposals[1][1]):
            return {}
        if expected_printed_page is not None and any(
            result.get("solution_printed_page") != expected_printed_page
            for _provider, result in proposals
        ):
            return {}
        normalized = _normalize_provider_coordinates(proposals)
        if [provider for provider, _result in normalized] != ["codex", "gemini"]:
            return {}
        return {
            provider: [
                block
                for block in result.get("blocks", [])
                if isinstance(block, Mapping)
            ]
            for provider, result in normalized
        }

    @staticmethod
    def _identity_fallback_allowed(
        problem_index: ProblemIndex, problem: ProblemItem
    ) -> bool:
        """Allow review recovery for a bounded monotonic run, never one lone mismatch."""

        if (
            problem.anchor is None
            or not problem.anchor.resolved
            or problem.group_label is not None
            or problem.subquestion_label is not None
            or not problem.parent_number.isdigit()
        ):
            return False
        peers = sorted(
            {
                int(item.parent_number)
                for item in problem_index.items
                if item.scope_id == problem.scope_id
                and item.anchor is not None
                and item.anchor.resolved
                and item.anchor.segment_id == problem.anchor.segment_id
                and item.anchor.page_range == problem.anchor.page_range
                and item.group_label is None
                and item.subquestion_label is None
                and item.parent_number.isdigit()
            }
        )
        target = int(problem.parent_number)
        return any(
            target in peers[index : index + 3]
            and peers[index + 2] - peers[index] == 2
            for index in range(max(0, len(peers) - 2))
        )

    @staticmethod
    def _recovery_cluster_rank(
        state: RuntimeState,
        problem: ProblemItem,
        cluster: Sequence[Mapping[str, Any]],
    ) -> tuple[int, int]:
        def variants(raw: object) -> set[str]:
            value = _semantic_label(raw or "")
            if not value:
                return set()
            stripped = re.sub(r"^\d+강", "", value)
            return {item for item in (value, stripped) if len(item) >= 4}

        problem_values: set[str] = set()
        for value in (
            *state.problem_hierarchy_by_id.get(problem.item_id, ()),
            state.problem_scope_label_by_id.get(problem.item_id),
            problem.group_label,
        ):
            problem_values.update(variants(value))
        block_values: set[str] = set()
        for agreement in cluster:
            for provider in ("codex", "gemini"):
                for _index, block in agreement["matched"][provider]:
                    for value in (
                        *block.get("hierarchy_path", []),
                        block.get("numbering_scope_label"),
                        (block.get("coverage") or {}).get("group_label"),
                    ):
                        block_values.update(variants(value))
        affinity = sum(
            min(len(problem_value), len(block_value))
            for problem_value in problem_values
            if problem_value not in GENERIC_HIERARCHY
            for block_value in block_values
            if block_value not in GENERIC_HIERARCHY
            and (
                problem_value in block_value or block_value in problem_value
            )
        )
        first_page = min(
            int(agreement["page"].printed_page) for agreement in cluster
        )
        assert problem.anchor is not None
        return affinity, -abs(first_page - problem.anchor.page_range.start)

    def _missing_recovery_assets(
        self,
        context: LinkContext,
        state: RuntimeState,
        problem_index: ProblemIndex,
        canonical_assets: Sequence[SolutionAsset],
    ) -> tuple[SolutionAsset, ...]:
        page_by_index = {page.pdf_index: page for page in state.solution_page_map}
        recoveries: list[SolutionAsset] = []
        existing_any_role = {
            (asset.scope_id, asset.parent_number, asset.subquestion_label)
            for asset in canonical_assets
        }
        role_specs = (
            (SolutionRole.QUICK_ANSWER, "solution-quick-targets", "recovery-answer"),
            (SolutionRole.DETAILED_SOLUTION, "solution-blocks", "recovery-detail"),
        )
        for role, stage, prefix in role_specs:
            accepted_roles = {role, SolutionRole.MIXED}
            existing = {
                (asset.scope_id, asset.parent_number, asset.subquestion_label)
                for asset in canonical_assets
                if asset.role in accepted_roles
            }
            for problem in sorted(problem_index.items, key=lambda item: item.sequence):
                target = (
                    problem.scope_id,
                    problem.parent_number,
                    problem.subquestion_label,
                )
                if (
                    target in existing
                    or problem.anchor is None
                    or not problem.anchor.resolved
                ):
                    continue
                identity_fallback = self._identity_fallback_allowed(
                    problem_index, problem
                ) and target not in existing_any_role
                missing_detailed = (
                    role == SolutionRole.DETAILED_SOLUTION
                    and problem.item_id in state.prior_missing_detailed_ids
                )
                if (
                    missing_detailed
                    and problem.group_label is None
                    and problem.subquestion_label is None
                ):
                    identity_fallback = True
                agreements: list[dict[str, Any]] = []
                for page_index in sorted(state.block_results):
                    page = page_by_index.get(page_index)
                    if (
                        page is None
                        or page.printed_page is None
                        or page.segment_id != problem.anchor.segment_id
                        or not _problem_allows_solution_page(
                            problem, page.printed_page, role
                        )
                    ):
                        continue
                    raw = self._raw_primary_solution_blocks(
                        context,
                        page_index,
                        page.printed_page,
                        stage,
                    )
                    if not raw:
                        continue
                    matched = {
                        provider: [
                            (index, block)
                            for index, block in enumerate(raw[provider])
                            if self._recovery_block_targets_problem(
                                block, problem, role
                            )
                            and self._recovery_block_scope_compatible(
                                state, problem, block, role
                            )
                        ]
                        for provider in ("codex", "gemini")
                    }
                    fallback_used = False
                    if (
                        role == SolutionRole.DETAILED_SOLUTION
                        and identity_fallback
                        and (not matched["codex"] or not matched["gemini"])
                    ):
                        relaxed = {
                            provider: [
                                (index, block)
                                for index, block in enumerate(raw[provider])
                                if self._recovery_block_targets_problem(
                                    block, problem, role
                                )
                                and self._recovery_block_scope_compatible(
                                    state,
                                    problem,
                                    block,
                                    role,
                                    allow_identity_fallback=True,
                                )
                            ]
                            for provider in ("codex", "gemini")
                        }
                        if relaxed["codex"] and relaxed["gemini"]:
                            matched = relaxed
                            fallback_used = True
                    if not matched["codex"] or not matched["gemini"]:
                        continue
                    target_entries = {
                        provider: [
                            (block_index, entry_index, block, entry)
                            for block_index, block in matched[provider]
                            for entry_index, entry in self._recovery_target_entries(
                                block, problem
                            )
                        ]
                        for provider in ("codex", "gemini")
                    }
                    answers = {
                        provider: set().union(
                            *(
                                self._recovery_block_answers(
                                    block, problem, role
                                )
                                for _index, block in matched[provider]
                            )
                        )
                        for provider in ("codex", "gemini")
                    }
                    if role == SolutionRole.QUICK_ANSWER:
                        if (
                            any(len(target_entries[provider]) != 1 for provider in target_entries)
                            or any(len(values) != 1 for values in answers.values())
                            or answers["codex"] != answers["gemini"]
                        ):
                            continue
                    elif (
                        any(len(values) > 1 for values in answers.values())
                        or answers["codex"]
                        and answers["gemini"]
                        and answers["codex"] != answers["gemini"]
                    ):
                        continue
                    agreements.append(
                        {
                            "page": page,
                            "matched": matched,
                            "target_entries": target_entries,
                            "continues": role == SolutionRole.DETAILED_SOLUTION
                            and all(
                                any(
                                    block.get("continues_next_page")
                                    for _index, block in matched[provider]
                                )
                                for provider in ("codex", "gemini")
                            ),
                            "starts": role == SolutionRole.DETAILED_SOLUTION
                            and all(
                                any(
                                    block.get("starts_on_previous_page")
                                    for _index, block in matched[provider]
                                )
                                for provider in ("codex", "gemini")
                            ),
                            "identity_fallback": fallback_used,
                        }
                    )
                if not agreements:
                    continue
                clusters: list[list[dict[str, Any]]] = []
                for agreement in agreements:
                    if (
                        clusters
                        and agreement["page"].pdf_index
                        == clusters[-1][-1]["page"].pdf_index + 1
                        and agreement["page"].segment_id
                        == clusters[-1][-1]["page"].segment_id
                        and (clusters[-1][-1]["continues"] or agreement["starts"])
                    ):
                        clusters[-1].append(agreement)
                    else:
                        clusters.append([agreement])
                if role == SolutionRole.QUICK_ANSWER:
                    if len(clusters) != 1 or len(clusters[0]) != 1:
                        continue
                elif len(clusters) != 1:
                    if not missing_detailed:
                        continue
                    ranked = [
                        (
                            self._recovery_cluster_rank(state, problem, cluster),
                            cluster,
                        )
                        for cluster in clusters
                    ]
                    ranked.sort(key=lambda value: value[0])
                    if len(ranked) > 1 and ranked[-1][0] == ranked[-2][0]:
                        continue
                    clusters = [ranked[-1][1]]
                parts: list[SolutionPart] = []
                primary_entries: list[Mapping[str, Any]] = []
                confidences: list[float] = []
                boundary_bottom_extended = False
                if role == SolutionRole.QUICK_ANSWER:
                    agreement = clusters[0][0]
                    block_index, entry_index, block, entry = agreement[
                        "target_entries"
                    ]["codex"][0]
                    bbox = entry.get("answer_bbox")
                    if not _valid_bbox(bbox):
                        continue
                    parts.append(SolutionPart(agreement["page"], tuple(bbox), 0))
                    primary_entries.append(entry)
                    primary_order = (block_index + 1) * 100 + entry_index + 1
                    confidences.extend(
                        min(
                            float(provider_block.get("confidence", 0)),
                            float(provider_entry.get("confidence", 0)),
                        )
                        for provider in ("codex", "gemini")
                        for _block_index, _entry_index, provider_block, provider_entry in agreement[
                            "target_entries"
                        ][provider]
                    )
                else:
                    for agreement in clusters[0]:
                        for _index, block in agreement["matched"]["codex"]:
                            primary_bbox = block.get("bbox")
                            bbox = _recovery_content_bbox(
                                block,
                                [
                                    peer
                                    for _peer_index, peer in agreement["matched"][
                                        "gemini"
                                    ]
                                ],
                            )
                            if not _valid_bbox(bbox):
                                parts = []
                                break
                            if (
                                isinstance(primary_bbox, list)
                                and len(primary_bbox) == 4
                                and bbox[3] > int(primary_bbox[3])
                            ):
                                boundary_bottom_extended = True
                            if not parts or (
                                parts[-1].page.pdf_index,
                                parts[-1].bbox,
                            ) != (agreement["page"].pdf_index, tuple(bbox)):
                                parts.append(
                                    SolutionPart(
                                        agreement["page"], tuple(bbox), len(parts)
                                    )
                                )
                            primary_entries.extend(
                                entry
                                for entry in block.get("entries", [])
                                if isinstance(entry, Mapping)
                            )
                            confidences.append(float(block.get("confidence", 0)))
                        if not parts:
                            break
                    primary_order = (
                        clusters[0][0]["matched"]["codex"][0][0] * 100 + 99
                    )
                if not parts or not confidences:
                    continue
                explicit_answer = next(
                    (
                        str(entry.get("explicit_answer_text"))
                        for entry in primary_entries
                        if _recovery_answer_identity(
                            entry.get("explicit_answer_text")
                        )
                    ),
                    None,
                )
                fingerprints = tuple(
                    dict.fromkeys(
                        (VISUAL_FINGERPRINT,)
                        + tuple(
                            str(value)
                            for entry in primary_entries
                            for value in (
                                list(entry.get("stem_fingerprint_tokens", []))[:2]
                                + list(entry.get("figure_labels", []))[:1]
                            )
                        )
                    )
                )
                safe_item = re.sub(
                    r"[^0-9A-Za-z가-힣_-]+", "-", problem.item_id
                )
                review_reason = (
                    "raw_identity_recovery"
                    if any(
                        agreement.get("identity_fallback")
                        for agreement in clusters[0]
                    )
                    else "cached_raw_recovery"
                    if problem.item_id in state.prior_empty_candidate_ids
                    else "cached_raw_detail_recovery"
                    if missing_detailed
                    else None
                )
                if boundary_bottom_extended and review_reason is not None:
                    review_reason += "+" + RECOVERY_CONSENSUS_MARKER
                asset = SolutionAsset(
                    f"{prefix}-{safe_item}",
                    role,
                    parts[0].page.pdf_index * 10000 + primary_order,
                    problem.scope_id,
                    problem.parent_number,
                    problem.subquestion_label,
                    tuple(parts),
                    fingerprints,
                    explicit_answer,
                    min(confidences),
                    review_reason=review_reason,
                )
                state.recovery_asset_ids.add(asset.asset_id)
                recoveries.append(asset)
        return tuple(sorted(recoveries, key=lambda asset: asset.sequence))

    @staticmethod
    def _enumerated_child_labels(block: Mapping[str, Any]) -> tuple[str, ...]:
        labels: list[str] = []
        coverage = block.get("coverage")
        if isinstance(coverage, Mapping):
            labels.extend(str(value) for value in coverage.get("subquestion_labels", []))
        for entry in block.get("entries", []):
            if not isinstance(entry, Mapping):
                continue
            if entry.get("subquestion_label") is not None:
                labels.append(str(entry["subquestion_label"]))
            # Structural labels are safe to reuse; the answer following each
            # label is deliberately ignored and never distributed to children.
            labels.extend(
                match.group(1)
                for match in re.finditer(
                    r"[\(\[]\s*([0-9]+)\s*[\)\]]",
                    str(entry.get("explicit_answer_text") or ""),
                )
            )
        return tuple(
            dict.fromkeys(
                label
                for raw in labels
                if (label := normalize_subquestion_label(raw)) is not None
            )
        )

    def _missing_shared_parent_assets(
        self,
        context: LinkContext,
        state: RuntimeState,
        problem_index: ProblemIndex,
        existing_assets: Sequence[SolutionAsset],
    ) -> tuple[SolutionAsset, ...]:
        """Recover one review-only parent crop for many explicitly listed children."""

        page_by_index = {page.pdf_index: page for page in state.solution_page_map}
        exact_detail = {
            (asset.scope_id, asset.parent_number, asset.subquestion_label)
            for asset in existing_assets
            if asset.role in {SolutionRole.DETAILED_SOLUTION, SolutionRole.MIXED}
            and asset.subquestion_label is not None
        }
        exact_any = {
            (asset.scope_id, asset.parent_number, asset.subquestion_label)
            for asset in existing_assets
            if asset.subquestion_label is not None
        }
        groups: dict[tuple[str, str], list[ProblemItem]] = {}
        for problem in problem_index.items:
            if problem.subquestion_label is not None:
                groups.setdefault((problem.scope_id, problem.parent_number), []).append(
                    problem
                )
        assets: list[SolutionAsset] = []
        for (scope_id, parent), children in groups.items():
            missing = {
                child.subquestion_label
                for child in children
                if (scope_id, parent, child.subquestion_label) not in exact_detail
                and (
                    child.item_id in state.prior_missing_detailed_ids
                    or not child.crop_verified
                    or (scope_id, parent, child.subquestion_label) not in exact_any
                )
            }
            missing.discard(None)
            allow_single = any(
                child.item_id in state.prior_missing_detailed_ids
                and child.subquestion_label in missing
                for child in children
            )
            if not missing or len(missing) < 2 and not allow_single:
                continue
            representative = min(children, key=lambda item: item.sequence)
            if representative.anchor is None or not representative.anchor.resolved:
                continue
            agreements: list[
                tuple[
                    PageIdentity,
                    dict[str, list[tuple[int, Mapping[str, Any]]]],
                    tuple[str, ...],
                ]
            ] = []
            for page_index in sorted(state.block_results):
                page = page_by_index.get(page_index)
                if (
                    page is None
                    or page.printed_page is None
                    or not _problem_allows_solution_page(
                        representative,
                        page.printed_page,
                        SolutionRole.DETAILED_SOLUTION,
                    )
                ):
                    continue
                raw = self._raw_primary_solution_blocks(
                    context, page_index, page.printed_page, "solution-blocks"
                )
                if not raw:
                    continue
                matched: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
                labels_by_provider: dict[str, set[str]] = {}
                for provider in ("codex", "gemini"):
                    provider_blocks: list[tuple[int, Mapping[str, Any]]] = []
                    provider_labels: set[str] = set()
                    for block_index, block in enumerate(raw[provider]):
                        if block.get("role") != SolutionRole.DETAILED_SOLUTION.value:
                            continue
                        coverage = block.get("coverage") or {}
                        try:
                            block_parent = _optional_parent(
                                coverage.get("parent_number")
                            )
                        except ValueError:
                            continue
                        if (
                            block_parent != parent
                            or not self._recovery_block_scope_compatible(
                                state,
                                representative,
                                block,
                                SolutionRole.DETAILED_SOLUTION,
                            )
                        ):
                            continue
                        labels = set(self._enumerated_child_labels(block))
                        if not labels:
                            continue
                        provider_blocks.append((block_index, block))
                        provider_labels.update(labels)
                    matched[provider] = provider_blocks
                    labels_by_provider[provider] = provider_labels
                agreed_labels = labels_by_provider["codex"] & labels_by_provider["gemini"]
                covered = tuple(
                    sorted(
                        missing & agreed_labels,
                        key=lambda value: (
                            0 if value.isdigit() else 1,
                            int(value) if value.isdigit() else value,
                        ),
                    )
                )
                if (
                    covered
                    and matched["codex"]
                    and matched["gemini"]
                    and set(covered) == missing
                ):
                    agreements.append((page, matched, covered))
            if len(agreements) != 1:
                continue
            page, matched, covered = agreements[0]
            parts: list[SolutionPart] = []
            seen_parts: set[tuple[int, int, int, int]] = set()
            confidences: list[float] = []
            for _block_index, block in matched["codex"]:
                bbox = block.get("bbox")
                if not _valid_bbox(bbox) or not set(
                    self._enumerated_child_labels(block)
                ) & set(covered):
                    continue
                normalized_bbox = tuple(int(value) for value in bbox)
                if normalized_bbox not in seen_parts:
                    seen_parts.add(normalized_bbox)
                    parts.append(SolutionPart(page, normalized_bbox, len(parts)))
                confidences.append(float(block.get("confidence", 0)))
            confidences.extend(
                float(block.get("confidence", 0))
                for _block_index, block in matched["gemini"]
            )
            if not parts or not confidences:
                continue
            owner = str(representative.owner_id or representative.item_id)
            safe_owner = re.sub(r"[^0-9A-Za-z가-힣_-]+", "-", owner)
            asset = SolutionAsset(
                f"shared-detail-{safe_owner}",
                SolutionRole.DETAILED_SOLUTION,
                page.pdf_index * 10000 + matched["codex"][0][0] * 100 + 99,
                scope_id,
                parent,
                None,
                tuple(parts),
                (VISUAL_FINGERPRINT,),
                None,
                min(confidences),
                covered_child_labels=covered,
                review_reason="shared_parent_solution",
            )
            state.recovery_asset_ids.add(asset.asset_id)
            assets.append(asset)
        return tuple(sorted(assets, key=lambda asset: asset.sequence))

    def _missing_parent_review_assets(
        self,
        context: LinkContext,
        state: RuntimeState,
        problem_index: ProblemIndex,
        existing_assets: Sequence[SolutionAsset],
    ) -> tuple[SolutionAsset, ...]:
        """Keep a dual-provider parent block when child answer lists disagree."""

        page_by_index = {page.pdf_index: page for page in state.solution_page_map}
        existing = {
            (asset.scope_id, asset.parent_number, asset.subquestion_label)
            for asset in existing_assets
            if asset.role in {
                SolutionRole.DETAILED_SOLUTION,
                SolutionRole.MIXED,
            }
        }
        assets: list[SolutionAsset] = []
        for problem in problem_index.items:
            target = (problem.scope_id, problem.parent_number, problem.subquestion_label)
            if (
                problem.subquestion_label is not None
                or target in existing
                or problem.anchor is None
                or not problem.anchor.resolved
            ):
                continue
            agreements: list[
                tuple[PageIdentity, dict[str, list[tuple[int, Mapping[str, Any]]]]]
            ] = []
            for page_index in sorted(state.block_results):
                page = page_by_index.get(page_index)
                if (
                    page is None
                    or page.printed_page is None
                    or not _problem_allows_solution_page(
                        problem, page.printed_page, SolutionRole.DETAILED_SOLUTION
                    )
                ):
                    continue
                raw = self._raw_primary_solution_blocks(
                    context, page_index, page.printed_page, "solution-blocks"
                )
                if not raw:
                    continue
                matched = {
                    provider: [
                        (block_index, block)
                        for block_index, block in enumerate(raw[provider])
                        if self._recovery_block_targets_problem(
                            block, problem, SolutionRole.DETAILED_SOLUTION
                        )
                        and self._recovery_block_scope_compatible(
                            state, problem, block, SolutionRole.DETAILED_SOLUTION
                        )
                    ]
                    for provider in ("codex", "gemini")
                }
                if matched["codex"] and matched["gemini"]:
                    agreements.append((page, matched))
            if len(agreements) != 1:
                continue
            page, matched = agreements[0]
            parts: list[SolutionPart] = []
            seen: set[tuple[int, int, int, int]] = set()
            for _block_index, block in matched["codex"]:
                bbox = block.get("bbox")
                if not _valid_bbox(bbox):
                    parts = []
                    break
                normalized = tuple(int(value) for value in bbox)
                if normalized not in seen:
                    seen.add(normalized)
                    parts.append(SolutionPart(page, normalized, len(parts)))
            if not parts:
                continue
            confidences = [
                float(block.get("confidence", 0))
                for provider in ("codex", "gemini")
                for _index, block in matched[provider]
            ]
            safe_item = re.sub(r"[^0-9A-Za-z가-힣_-]+", "-", problem.item_id)
            asset = SolutionAsset(
                f"parent-review-{safe_item}",
                SolutionRole.DETAILED_SOLUTION,
                page.pdf_index * 10000 + matched["codex"][0][0] * 100 + 98,
                problem.scope_id,
                problem.parent_number,
                None,
                tuple(parts),
                (VISUAL_FINGERPRINT,),
                None,
                min(confidences),
                review_reason="raw_parent_solution",
            )
            state.recovery_asset_ids.add(asset.asset_id)
            assets.append(asset)
        return tuple(sorted(assets, key=lambda asset: asset.sequence))

    @classmethod
    def _explicit_child_proxy_entries(
        cls,
        block: Mapping[str, Any],
        entries: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        coverage = block.get("coverage")
        if (
            block.get("role") != SolutionRole.DETAILED_SOLUTION.value
            or not isinstance(coverage, Mapping)
            or coverage.get("kind") != "parent_with_subquestions"
            or not coverage.get("parent_number")
        ):
            return []
        try:
            parent = _split_scoped_number(
                str(coverage["parent_number"]),
                None,
                block.get("numbering_scope_label"),
            )[0]
        except ValueError:
            return []
        labels = tuple(
            dict.fromkeys(
                label
                for raw in coverage.get("subquestion_labels", [])
                if (label := normalize_subquestion_label(raw)) is not None
            )
        )
        existing = {
            normalize_subquestion_label(entry.get("subquestion_label"))
            for entry in entries
            if isinstance(entry, Mapping)
        }
        return [
            {
                "entry_id": f"explicit-child-proxy-{label}",
                "parent_number": parent,
                "subquestion_label": label,
                "answer_bbox": None,
                "explicit_answer_text": None,
                "stem_fingerprint_tokens": [],
                "figure_labels": [],
                "confidence": float(block.get("confidence", 0)),
            }
            for label in labels
            if label not in existing
        ]

    def _block_assets(
        self,
        state: RuntimeState,
        problem_index: ProblemIndex,
        *,
        expanded: bool = False,
        page_map: Sequence[PageIdentity] | None = None,
        block_results: Mapping[int, Mapping[str, Any]] | None = None,
        source_document: str = "solution",
    ) -> tuple[SolutionAsset, ...]:
        current_page_map = tuple(page_map or state.solution_page_map)
        current_results = block_results or state.block_results
        page_by_index = {page.pdf_index: page for page in current_page_map}
        drafts: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for page_index, result in sorted(current_results.items()):
            page_identity_value = page_by_index.get(page_index)
            if expanded and page_identity_value is not None:
                try:
                    validate_contract(result, json.loads(BLOCK_SCHEMA.read_text(encoding="utf-8")))
                except ContractError:
                    continue
                observed = result.get("solution_printed_page")
                if result["solution_pdf_index"] != page_index or result["solution_pdf_page"] != page_index + 1:
                    continue
                if observed is not None and page_identity_value.printed_page not in {None, observed}:
                    continue
                if page_identity_value.printed_page is None and observed is not None:
                    if any(
                        other.pdf_index != page_index
                        and other.segment_id == page_identity_value.segment_id
                        and (other.printed_page == observed or current_results.get(other.pdf_index, {}).get("solution_printed_page") == observed)
                        for other in current_page_map
                    ):
                        continue
                    page_identity_value = replace(page_identity_value, printed_page=observed)
            if page_identity_value is None or page_identity_value.printed_page is None:
                continue
            for block_index, block in enumerate(result.get("blocks", []), 1):
                role = SolutionRole(block["role"])
                continuation_label = block.get("continuation_label") or block.get("numbering_scope_label")
                if block.get("starts_on_previous_page"):
                    matches = [
                        draft
                        for draft in pending
                        if draft["next_pdf_index"] == page_index
                        and draft["role"] == role
                        and (
                            not continuation_label
                            or not draft["continuation_label"]
                            or continuation_label == draft["continuation_label"]
                        )
                    ]
                    for draft in matches:
                        draft["parts"].append(
                            SolutionPart(
                                page_identity_value,
                                tuple(block["bbox"]),
                                len(draft["parts"]),
                            )
                        )
                        if block.get("continues_next_page"):
                            draft["next_pdf_index"] = page_index + 1
                        else:
                            pending.remove(draft)
                    if matches:
                        continue
                    # A continuation without its preceding canonical part is
                    # incomplete by construction; only omission recovery may
                    # turn it into a candidate.
                    continue

                entries = list(block.get("entries", []))
                entries.extend(self._explicit_child_proxy_entries(block, entries))
                coverage = block.get("coverage", {})
                if (
                    coverage.get("parent_number")
                    and entries
                    and all(
                        normalize_subquestion_label(entry.get("subquestion_label")) is not None
                        for entry in entries
                    )
                ):
                    # The problem crop may intentionally keep a parent with all
                    # (가)/(나) or (1)/(2) parts together.  Keep child proxies,
                    # but also expose the whole detailed block as one parent
                    # asset instead of silently dropping that main-question run.
                    entries.insert(
                        0,
                        {
                            "entry_id": "parent-aggregate",
                            "parent_number": coverage["parent_number"],
                            "subquestion_label": None,
                            "answer_bbox": None,
                            "explicit_answer_text": None,
                            "stem_fingerprint_tokens": list(
                                dict.fromkeys(
                                    token
                                    for entry in entries
                                    for token in entry.get("stem_fingerprint_tokens", [])
                                )
                            )[:4],
                            "figure_labels": list(
                                dict.fromkeys(
                                    label
                                    for entry in entries
                                    for label in entry.get("figure_labels", [])
                                )
                            )[:4],
                            "confidence": min(
                                float(entry.get("confidence", block["confidence"]))
                                for entry in entries
                            ),
                        },
                    )
                if not entries and coverage.get("parent_number"):
                    entries = [
                        {
                            "entry_id": "coverage",
                            "parent_number": coverage["parent_number"],
                            "subquestion_label": None,
                            "answer_bbox": None,
                            "explicit_answer_text": None,
                            "stem_fingerprint_tokens": [],
                            "figure_labels": [],
                            "confidence": block["confidence"],
                        }
                    ]
                if not entries and coverage.get("kind") in {"range", "group"}:
                    try:
                        start = int(
                            _split_scoped_number(
                                str(coverage.get("range_start")),
                                None,
                                block.get("numbering_scope_label"),
                            )[0]
                        )
                        end = int(
                            _split_scoped_number(
                                str(coverage.get("range_end")),
                                None,
                                block.get("numbering_scope_label"),
                            )[0]
                        )
                    except (TypeError, ValueError):
                        start = end = -1
                    entries = [
                        {
                            "entry_id": problem.item_id,
                            "parent_number": problem.parent_number,
                            "subquestion_label": problem.subquestion_label,
                            "answer_bbox": None,
                            "explicit_answer_text": None,
                            "stem_fingerprint_tokens": [],
                            "figure_labels": [],
                            "confidence": block["confidence"],
                        }
                        for problem in problem_index.items
                        if problem.parent_number.isdigit()
                        and start <= int(problem.parent_number) <= end
                        and problem.anchor is not None
                        and (expanded or _problem_allows_solution_page(
                            problem, page_identity_value.printed_page, role
                        ))
                    ]
                for entry_index, entry in enumerate(entries, 1):
                    parent = self._entry_parent(block, entry)
                    if parent is None:
                        continue
                    sublabel = normalize_subquestion_label(entry.get("subquestion_label"))
                    block_path = _block_semantic_path(block)
                    raw_block_scope = block.get("numbering_scope_label")
                    block_scope_label = (
                        _semantic_label(raw_block_scope) if raw_block_scope else None
                    )
                    variant_score = _book_variant_score(state, block_path)

                    def scope_score(problem: ProblemItem) -> int:
                        if expanded:
                            problem_path = tuple(value for value in state.problem_hierarchy_by_id.get(problem.item_id, ()) if value not in GENERIC_HIERARCHY)
                            candidate_path = tuple(value for value in block_path if value not in GENERIC_HIERARCHY)
                            problem_label = state.problem_scope_label_by_id.get(problem.item_id)
                            if problem.anchor is None:
                                chapter_labels = [value for value in problem_path if re.match(r'^\d+[가-힣]', value)]
                                topic_label = chapter_labels[-1] if chapter_labels else (problem_path[-1] if problem_path and not (len(problem_path) > 1 and problem_path[-1] == problem_label) else '')
                                topic = re.sub(r"^(?:[a-z]|\d+)(?=[가-힣])", "", topic_label)
                                topic_matches = len(topic) >= 2 and any(
                                    topic == value or re.match(r"^\d+[가-힣]", value) and topic in re.sub(r"^\d+", "", value)
                                    for value in candidate_path
                                )
                                scope_matches = bool(problem_label and (
                                    problem_label in candidate_path or problem_label == block_scope_label
                                ))
                                return 1 if topic_matches and scope_matches and variant_score >= 0 and _layout_scope_score(problem, block) >= 0 else -1
                            if (problem_path and candidate_path and problem_path[-1] != candidate_path[-1]
                                    or problem_label and block_scope_label and problem_label != block_scope_label):
                                return -1
                        layout_score = _layout_scope_score(problem, block)
                        hierarchy_score = _hierarchy_score(
                            state.problem_hierarchy_by_id.get(problem.item_id, ()),
                            block_path,
                            state.problem_scope_label_by_id.get(problem.item_id),
                            block_scope_label,
                        )
                        if variant_score < 0 or layout_score < 0:
                            return -1
                        pooled_quick = bool(
                            role == SolutionRole.QUICK_ANSWER
                            and problem.anchor is not None
                            and page_identity_value.printed_page
                            < problem.anchor.page_range.start
                        )
                        if pooled_quick:
                            if hierarchy_score > 0:
                                return hierarchy_score + variant_score + layout_score
                            generic_quick_scope = any(
                                "빠른정답" in value or "정답표" in value
                                for value in block_path + ((block_scope_label or ""),)
                            )
                            if hierarchy_score < 0 and not generic_quick_scope:
                                return -1
                            return (variant_score or 1) + layout_score
                        if hierarchy_score < 0:
                            return (
                                variant_score + layout_score
                                if variant_score > 0 or layout_score > 0
                                else -1
                            )
                        return hierarchy_score + variant_score + layout_score

                    scored_problems = [
                        (problem, scope_score(problem))
                        for problem in problem_index.items
                        if problem.parent_number == parent
                        and problem.subquestion_label == sublabel
                        and (
                            expanded and problem.anchor is None
                            or problem.anchor is not None
                            and problem.anchor.resolved
                            and problem.anchor.reference_document
                            in {"unknown", source_document}
                            and (
                                problem.anchor.reference_document == "unknown"
                                or problem.anchor.segment_id
                                == page_identity_value.segment_id
                            )
                            and (expanded or _problem_allows_solution_page(
                                problem, page_identity_value.printed_page, role
                            ))
                        )
                    ]
                    best_score = max((score for _problem, score in scored_problems), default=0)
                    matching_scopes = {
                        problem.scope_id
                        for problem, score in scored_problems
                        if score == best_score and score > 0
                    }
                    review_reason = None
                    unique_quick_entry = False
                    if not matching_scopes and role == SolutionRole.QUICK_ANSWER:
                        # Scope labels may differ, but a repeated answer number
                        # anywhere in the existing search window is not unique.
                        occurrences = sum(
                            len(self._recovery_target_entries(candidate_block, problem))
                            for candidate_page_index, candidate_result in current_results.items()
                            for candidate_page in (page_by_index.get(candidate_page_index),)
                            if candidate_page is not None
                            and candidate_page.printed_page is not None
                            for problem, _score in scored_problems
                            if problem.anchor is not None
                            and problem.anchor.reference_document in {"unknown", source_document}
                            and (problem.anchor.reference_document == "unknown"
                                 or problem.anchor.segment_id == candidate_page.segment_id)
                            and _problem_allows_solution_page(
                                problem, candidate_page.printed_page, role
                            )
                            for candidate_block in candidate_result.get("blocks", [])
                            if candidate_block.get("role") == SolutionRole.QUICK_ANSWER.value
                        )
                        unique_quick_entry = (
                            occurrences == 1 and variant_score >= 0
                            and all(_layout_scope_score(problem, block) >= 0
                                    for problem, _score in scored_problems)
                        )
                    if not matching_scopes and (
                        role == SolutionRole.DETAILED_SOLUTION or unique_quick_entry
                    ):
                        referenced_scopes = {
                            problem.scope_id for problem, _score in scored_problems
                        }
                        if len(referenced_scopes) == 1:
                            matching_scopes = referenced_scopes
                            review_reason = "referenced_scope_mismatch"
                    if len(matching_scopes) > 1 and all(problem.anchor is not None for problem, _score in scored_problems):
                        block_column = _bbox_column(block.get("bbox"))
                        column_scopes = {
                            problem.scope_id
                            for problem, score in scored_problems
                            if score == best_score
                            and block_column is not None
                            and problem.column == block_column
                        }
                        if len(column_scopes) == 1:
                            matching_scopes = column_scopes
                    if len(matching_scopes) != 1:
                        continue
                    bbox = (
                        entry.get("answer_bbox")
                        if role == SolutionRole.QUICK_ANSWER and entry.get("answer_bbox")
                        else block["bbox"]
                    )
                    prefix = "answer" if role == SolutionRole.QUICK_ANSWER else "explanation"
                    if role == SolutionRole.MIXED:
                        prefix = "mixed"
                    if source_document == "problem":
                        prefix = "problem-" + prefix
                    draft = {
                        "asset_id": f"{prefix}-p{page_index + 1:04d}-b{block_index:02d}-e{entry_index:02d}",
                        "role": role,
                        "sequence": page_index * 10000 + block_index * 100 + entry_index,
                        "scope_id": next(iter(matching_scopes)),
                        "parent": parent,
                        "sublabel": sublabel,
                        "parts": [SolutionPart(page_identity_value, tuple(bbox), 0)],
                        "fingerprints": tuple(
                            dict.fromkeys(
                                (VISUAL_FINGERPRINT,)
                                + tuple(entry.get("stem_fingerprint_tokens", []))[:2]
                                + tuple(entry.get("figure_labels", []))[:1]
                            )
                        ),
                        "explicit_answer": entry.get("explicit_answer_text"),
                        "confidence": min(float(block["confidence"]), float(entry["confidence"])),
                        "review_reason": review_reason,
                        "continuation_label": continuation_label,
                        "next_pdf_index": page_index + 1,
                    }
                    drafts.append(draft)
                    if block.get("continues_next_page"):
                        pending.append(draft)
        merged_drafts: list[dict[str, Any]] = []
        continuation_drafts: dict[tuple[Any, ...], dict[str, Any]] = {}
        for draft in drafts:
            label = draft.get("continuation_label")
            key = (
                draft["role"],
                draft["scope_id"],
                draft["parent"],
                draft["sublabel"],
                label,
            )
            target = continuation_drafts.get(key) if label else None
            if target is None:
                merged_drafts.append(draft)
                if label:
                    continuation_drafts[key] = draft
                continue
            target["parts"].extend(
                replace(part, order=len(target["parts"]) + offset)
                for offset, part in enumerate(draft["parts"])
            )
            target["fingerprints"] = tuple(
                dict.fromkeys(target["fingerprints"] + draft["fingerprints"])
            )
            target["explicit_answer"] = (
                target["explicit_answer"] or draft["explicit_answer"]
            )
            target["confidence"] = min(
                target["confidence"], draft["confidence"]
            )
        assets = tuple(
            SolutionAsset(
                draft["asset_id"],
                draft["role"],
                draft["sequence"],
                draft["scope_id"],
                draft["parent"],
                draft["sublabel"],
                tuple(draft["parts"]),
                tuple(str(value) for value in draft["fingerprints"]),
                draft["explicit_answer"],
                draft["confidence"],
                source_document=source_document,
                review_reason=draft["review_reason"],
            )
            for draft in merged_drafts
        )
        if not expanded or state.solution_index is None:
            return assets
        bound = []
        for asset in assets:
            owners = [problem for problem in problem_index.items
                      if problem.scope_id == asset.scope_id and problem.parent_number == asset.parent_number
                      and problem.subquestion_label == asset.subquestion_label]
            if (len(owners) == 1 and owners[0].anchor is None
                    and all(re.fullmatch(r'[0-9a-f]{64}', value) for value in
                            (problem_index.source_sha256, state.solution_index.source_sha256))):
                asset = replace(asset, expanded_source_binding=ExpandedSourceBinding(
                    problem_index.source_sha256, state.solution_index.source_sha256,
                    quality.identity(owners[0].signature()), quality.identity(asset.signature())))
            bound.append(asset)
        return tuple(bound)

    def _inline_solution_assets(
        self,
        context: LinkContext,
        state: RuntimeState,
        problem_index: ProblemIndex,
    ) -> tuple[SolutionAsset, ...]:
        """Expose already-detected worked solutions without recropping questions."""

        problem_by_id = {problem.item_id: problem for problem in problem_index.items}
        page_by_index = {page.pdf_index: page for page in problem_index.page_map}
        items_by_page: dict[int, list[str]] = {}
        for item_id, item in state.manifest_items_by_id.items():
            for source_page in item.get("source_pages", []):
                if isinstance(source_page, int) and not isinstance(source_page, bool):
                    items_by_page.setdefault(source_page - 1, []).append(item_id)
        assets: list[SolutionAsset] = []
        for page_index, item_ids in sorted(items_by_page.items()):
            page = page_by_index.get(page_index)
            if page is None:
                continue
            explicit: dict[str, list[list[int]]] = {}
            for item_id in item_ids:
                item = state.manifest_items_by_id[item_id]
                parts = item.get("inline_solution_regions")
                if isinstance(parts, list):
                    explicit[item_id] = [
                        list(part["bbox"])
                        for part in parts
                        if isinstance(part, Mapping)
                        and part.get("source_page") == page_index + 1
                        and _valid_bbox(part.get("bbox"))
                    ]
            if not any(explicit.values()):
                detection = context.run_dir / "detections" / f"page-{page_index + 1:04d}.json"
                try:
                    result = json.loads(detection.read_text(encoding="utf-8"))["result"]
                except (OSError, KeyError, json.JSONDecodeError):
                    continue
                regions = [
                    list(region["bbox"])
                    for region in result.get("ignored_regions", [])
                    if isinstance(region, Mapping)
                    and region.get("role") == "solution_content"
                    and _valid_bbox(region.get("bbox"))
                ]
                if result.get("page_type") != "problem_with_inline_solution" or len(item_ids) != 1:
                    continue
                explicit[item_ids[0]] = regions
            for item_id, regions in explicit.items():
                problem = problem_by_id.get(item_id)
                if problem is None or not regions:
                    continue
                safe_item = re.sub(r"[^0-9A-Za-z가-힣_-]+", "-", item_id)
                assets.append(
                    SolutionAsset(
                        f"inline-detail-{safe_item}",
                        SolutionRole.DETAILED_SOLUTION,
                        problem.sequence * 10000 + 9999,
                        problem.scope_id,
                        problem.parent_number,
                        problem.subquestion_label,
                        tuple(
                            SolutionPart(page, tuple(region), order)
                            for order, region in enumerate(regions)
                        ),
                        (VISUAL_FINGERPRINT,),
                        None,
                        0.99,
                        source_document="problem",
                        review_reason="inline_solution",
                    )
                )
        return tuple(sorted(assets, key=lambda asset: asset.sequence))

    def recover_solution_detections(
        self, context: LinkContext, *, expected_source_sha256: str,
    ) -> dict[str, Any]:
        """Validate/recover every solution page in an explicitly supplied copy.

        Does not read problem crops, relink, or fabricate page results. Valid
        input-keyed raw caches are reused; only failed/missing pages invoke
        the runner (at most two attempts each). Caller owns copying the leaf.
        """
        if context.force:
            raise StageHookError("detection_recovery_requires_force_false")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_source_sha256):
            raise StageHookError("solution_source_sha256_invalid")
        if _digest_file(context.solution_path) != expected_source_sha256:
            raise StageHookError("solution_source_sha256_mismatch")
        texts = self.text_reader(context.solution_path)
        if not texts or [page.pdf_index for page in texts] != list(range(len(texts))):
            raise StageHookError("solution_detection_page_inventory_invalid")
        state = RuntimeState({}, {}, solution_texts=texts)
        for page in texts:
            self._detect_solution_block(context, state, page.pdf_index, page.text, len(texts))
        return self._require_detection_coverage(context, state, expected_source_sha256,
                                               {"solution": set(range(len(texts)))})

    def _require_detection_coverage(
        self, context: LinkContext, state: RuntimeState, source_sha256: str,
        requested_by_document: Mapping[str, set[int]],
    ) -> dict[str, Any]:
        documents = {}
        for document, requested in requested_by_document.items():
            results = state.block_results if document == "solution" else state.problem_block_results
            completed = requested & set(results)
            documents[document] = {
                "requested_pages": sorted(requested),
                "completed_pages": sorted(completed),
                "failed_pages": sorted(requested - completed),
            }
        source_matches = _digest_file(context.solution_path) == source_sha256
        complete = source_matches and not any(row["failed_pages"] for row in documents.values())
        coverage = {"source_sha256": source_sha256, "provider_profile": self.provider_profile,
                    "stage": "solution-blocks", "complete": complete,
                    "source_matches": source_matches, "documents": documents}
        _atomic_json(context.run_dir / "solutions" / "detection_coverage.json", coverage)
        if not source_matches:
            state.solution_index = None
            raise StageHookError("solution_source_changed_during_detection")
        if not complete:
            state.solution_index = None
            prefix = "all" if not any(row["completed_pages"] for row in documents.values()) else "some"
            raise StageHookError(f"{prefix} required solution-block detections failed")
        return coverage

    def detect_solution_index(self, context: LinkContext) -> SolutionIndex:
        state = self._state(context)
        if state.solution_index is not None:
            if _digest_file(context.solution_path) != state.solution_index.source_sha256:
                state.solution_index = None
                raise StageHookError("solution_source_changed_during_detection")
            return state.solution_index
        source_sha256 = _digest_file(context.solution_path)
        problem_index = self.detect_problem_index(context)
        page_map = self._ensure_solution_page_map(context, state)
        text_by_page = {page.pdf_index: page.text for page in state.solution_texts}
        quality_by_page = {page.pdf_index: page.quality for page in state.solution_texts}
        problem_text_by_page = {
            page.pdf_index: page.text for page in state.problem_texts
        }
        problem_quality_by_page = {
            page.pdf_index: page.quality for page in state.problem_texts
        }

        def candidate_pages_for(
            document: str,
            document_page_map: Sequence[PageIdentity],
            document_text: Mapping[int, str],
            document_quality: Mapping[int, str],
        ) -> set[int]:
            def document_quick_probe(page: PageIdentity) -> bool:
                if document_quality.get(page.pdf_index) != "usable":
                    return True
                return _looks_like_quick_answer_text(
                    document_text.get(page.pdf_index, "")
                )

            return {
                page.pdf_index
                for page in document_page_map
                if page.printed_page is not None
                and any(
                    problem.anchor is not None
                    and problem.anchor.resolved
                    and problem.anchor.reference_document in {"unknown", document}
                    and (
                        problem.anchor.reference_document == "unknown"
                        or problem.anchor.segment_id == page.segment_id
                    )
                    and (
                        problem.anchor.page_range.contains(page.printed_page)
                        or (
                            max(
                                1,
                                problem.anchor.page_range.start
                                - QUICK_ANSWER_LOOKBACK_PAGES,
                            )
                            <= page.printed_page
                            < problem.anchor.page_range.start
                            and document_quick_probe(page)
                        )
                        or (
                            page.pdf_index < QUICK_ANSWER_FRONT_MATTER_PAGES
                            and page.printed_page < problem.anchor.page_range.start
                            and document_quick_probe(page)
                        )
                    )
                    for problem in problem_index.items
                )
            }

        candidate_pages = candidate_pages_for(
            "solution", page_map, text_by_page, quality_by_page
        )
        problem_candidate_pages = candidate_pages_for(
            "problem",
            problem_index.page_map,
            problem_text_by_page,
            problem_quality_by_page,
        )
        candidate_pages.update(
            _anchor_window_null_page_indices(page_map, problem_index.items)
        )
        with ThreadPoolExecutor(max_workers=max(1, context.workers)) as executor:
            futures = [
                executor.submit(
                    self._detect_solution_block,
                    context,
                    state,
                    page_index,
                    text_by_page.get(page_index, ""),
                    len(page_map),
                )
                for page_index in sorted(candidate_pages)
            ] + [
                executor.submit(
                    self._detect_solution_block,
                    context,
                    state,
                    page_index,
                    problem_text_by_page.get(page_index, ""),
                    len(problem_index.page_map),
                    (),
                    "problem",
                )
                for page_index in sorted(problem_candidate_pages)
            ]
            for future in as_completed(futures):
                future.result()
        for document, requested, completed, document_map in (
            ("solution", candidate_pages, set(state.block_results), page_map),
            (
                "problem",
                problem_candidate_pages,
                set(state.problem_block_results),
                problem_index.page_map,
            ),
        ):
            if requested:
                state.search_trace.append(
                    {
                        "stage": "current",
                        "source_document": document,
                        "new_pages": sorted(requested & completed),
                        "failed_pages": sorted(requested - completed),
                        "remaining_pages": [],
                        "reused_pages": [],
                        "uninspected_pages": sorted(
                            {page.pdf_index for page in document_map} - completed
                        ),
                    }
                )
        self._require_detection_coverage(context, state, source_sha256,
            {"solution": candidate_pages, "problem": problem_candidate_pages})
        for page_index in sorted(candidate_pages):
            self._refine_quick_answer_blocks(
                context,
                state,
                page_index,
                text_by_page.get(page_index, ""),
                len(page_map),
            )
        canonical_assets = self._block_assets(state, problem_index)
        problem_document_assets = self._block_assets(
            state,
            problem_index,
            page_map=problem_index.page_map,
            block_results=state.problem_block_results,
            source_document="problem",
        )
        recovery_assets: tuple[SolutionAsset, ...] = ()
        shared_parent_assets: tuple[SolutionAsset, ...] = ()
        parent_review_assets: tuple[SolutionAsset, ...] = ()
        if self.provider_profile == "full":
            recovery_assets = self._missing_recovery_assets(
                context, state, problem_index, canonical_assets
            )
            shared_parent_assets = self._missing_shared_parent_assets(
                context,
                state,
                problem_index,
                canonical_assets + recovery_assets,
            )
            parent_review_assets = self._missing_parent_review_assets(
                context,
                state,
                problem_index,
                canonical_assets + recovery_assets + shared_parent_assets,
            )
        inline_assets = self._inline_solution_assets(
            context, state, problem_index
        )
        assets = (
            canonical_assets
            + problem_document_assets
            + recovery_assets
            + shared_parent_assets
            + parent_review_assets
            + inline_assets
        )
        assets, seam_replacements = _materialize_recovery_detail_seams(assets)
        if seam_replacements:
            state.recovery_asset_ids.difference_update(seam_replacements)
            state.recovery_asset_ids.update(seam_replacements.values())
            approved = state.approved_recovery_asset_ids & seam_replacements.keys()
            state.approved_recovery_asset_ids.difference_update(approved)
            state.approved_recovery_asset_ids.update(
                seam_replacements[asset_id] for asset_id in approved
            )
        state.assets_by_id = {asset.asset_id: asset for asset in assets}
        state.solution_index = SolutionIndex(
            _digest_file(context.solution_path), page_map, assets
        )
        return state.solution_index

    def expand_solution_index(
        self, context: LinkContext, problems: tuple[ProblemItem, ...], stage: str
    ) -> SolutionIndex:
        state = self._state(context)
        current = self.detect_solution_index(context)
        if _digest_file(context.solution_path) != current.source_sha256:
            raise StageHookError('solution_source_changed_during_search')
        page_map = state.solution_page_map
        texts = {page.pdf_index: page.text for page in state.solution_texts}
        anchored = {page.pdf_index for page in page_map if page.printed_page is not None
                    and any(problem.anchor is not None and problem.anchor.resolved
                        and problem.anchor.segment_id == page.segment_id
                        and problem.anchor.page_range.contains(page.printed_page) for problem in problems)}
        unit_tokens = {token for problem in problems
                       for token in state.problem_hierarchy_by_id.get(problem.item_id, ())
                       if token not in GENERIC_HIERARCHY
                       and not (token == state.problem_scope_label_by_id.get(problem.item_id)
                                and re.fullmatch(r'(?:기본|실력|심화|응용|확인|연습|기출|내신|수능|실전|유형|핵심)*문제(?:익히기|풀기|풀이)?', token))}
        match stage:
            case 'neighbor':
                requested = {page.pdf_index for page in page_map
                             if any(abs(page.pdf_index - index) <= 2 for index in anchored)}
            case 'unit':
                requested = {page.pdf_index for page in page_map
                             if any(token in _semantic_label(texts.get(page.pdf_index, '')) for token in unit_tokens)}
            case 'document':
                requested = {page.pdf_index for page in page_map}
            case _:
                raise StageHookError('unknown_solution_search_stage')
        needed = sorted(requested - set(state.block_results) - state.search_attempted_pages)
        selected = needed
        trace = {'stage': stage, 'new_pages': [], 'failed_pages': [],
                 'remaining_pages': needed[len(selected):], 'reused_pages': sorted(requested & set(state.block_results))}
        state.search_trace.append(trace)
        for page_index in selected:
            result = self._detect_solution_block(context, state, page_index,
                                                texts.get(page_index, ''), len(page_map))
            state.search_attempted_pages.add(page_index)
            if result is None:
                trace['failed_pages'].append(page_index)
            else:
                trace['new_pages'].append(page_index)
                self._refine_quick_answer_blocks(context, state, page_index,
                                                texts.get(page_index, ''), len(page_map))
        trace['uninspected_pages'] = sorted({page.pdf_index for page in page_map} - set(state.block_results))
        self._require_detection_coverage(context, state, current.source_sha256,
                                         {"solution": requested})
        canonical = self._block_assets(state, state.problem_index, expanded=True)
        next_index = _ordinary_solution_index(state, current, canonical)
        for asset_id, previous in state.expanded_assets.items():
            if state.assets_by_id.get(asset_id) == previous:
                state.assets_by_id.pop(asset_id)
        state.expanded_assets = {asset.asset_id: asset for asset in canonical}
        state.assets_by_id.update({asset.asset_id: asset for asset in canonical})
        state.solution_index = next_index
        trace['uninspected_pages'] = sorted({page.pdf_index for page in page_map} - set(state.block_results))
        return state.solution_index

    def verify_pipeline_scope_pairs(
        self, context: LinkContext, pairs: tuple[ScopeCandidatePair, ...],
        problem: ProblemIndex, bundle: ScopeAlignmentBundle,
    ) -> Mapping[tuple[str, str], PairVerification]:
        contexts = {}
        if pairs:
            try:
                contexts = self._scope_verifier_contexts(context, pairs, problem, bundle)
            except (OSError, ValueError, KeyError, TypeError, StageHookError):
                self._state(context).verification_blocked_reason = "scope_context_unavailable"
                return {}
        return self.verify_scope_candidate_pairs(context, pairs,
            {item.item_id: item for item in problem.items},
            {asset.asset_id: asset for asset in bundle.flat_assets()}, contexts, allow_provider_calls=True,
            independent_roles=True)

    def _scope_verifier_contexts(
        self, context: LinkContext, pairs: tuple[ScopeCandidatePair, ...],
        problem: ProblemIndex, bundle: ScopeAlignmentBundle,
    ) -> dict[tuple[str, str], JsonMap]:
        state = self._state(context)
        state.scope_input_contexts = {}
        provenance = dict(bundle.provenance or {})
        relative = Path(str(provenance.get("alignment_index_path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise StageHookError("scope_context_snapshot_path_invalid")
        snapshot_path = context.run_dir / relative
        consensus_relative = Path("solutions/detections/solution-blocks/consensus")
        if self.provider_profile == "codex_only":
            consensus_relative /= "codex_only"
        if Path(str(provenance.get("consensus_path", ""))) != consensus_relative:
            raise StageHookError("scope_context_consensus_profile_changed")
        solution_sha = _digest_file(context.solution_path)
        problem_sha = _digest_file(self._source_pdf(state))
        if (problem_sha != problem.source_sha256
                or provenance.get("problem_source_sha256") != problem_sha
                or provenance.get("alignment_index_sha256") != _digest_file(snapshot_path)):
            raise StageHookError("scope_context_source_changed")
        snapshot = solution_scope_mod.read_scope_index(
            snapshot_path, expected_solution_sha256=solution_sha,
            expected_provider_profile=self.provider_profile,
            solutions_dir=context.run_dir / "solutions",
        )
        episodes = {entry["episode_id"]: entry for entry in snapshot["episodes"]}
        native_assets = {entry["native_asset_id"]: entry for entry in snapshot["native_assets"]}
        bundle_assets = {asset.asset_id: asset for asset in bundle.flat_assets()}
        items = {item.item_id: item for item in problem.items}
        result: dict[tuple[str, str], JsonMap] = {}
        for pair in pairs:
            item = items[pair.problem_id]
            manifest_item = state.manifest_items_by_id[item.item_id]
            if pair.solution_scope_id not in episodes or pair.asset_id not in native_assets or pair.asset_id not in bundle_assets:
                raise StageHookError("scope_context_membership_missing")
            episode = episodes[pair.solution_scope_id]
            native = native_assets[pair.asset_id]
            asset = bundle_assets[pair.asset_id]
            if tuple((part.page.pdf_index, part.page.pdf_page, part.page.printed_page, part.page.segment_id, tuple(part.bbox), part.order) for part in asset.parts) != tuple((part["pdf_index"], part["pdf_page"], part["printed_page"], part["segment_id"], tuple(part["bbox"]), 0) for part in native["parts"]):
                raise StageHookError("scope_context_asset_parts_changed")
            alignment = bundle.alignment.get(item.scope_id, "detailed_solution" if pair.role == SolutionRole.DETAILED_SOLUTION else "quick_answer")
            if (native["solution_scope_id"] != pair.solution_scope_id
                    or native["parent_number"] is None
                    or normalize_parent_number(native["parent_number"]) != item.parent_number
                    or native["subquestion_label"] != item.subquestion_label
                    or alignment is None or alignment.ambiguous
                    or alignment.aligned_episode_id != pair.solution_scope_id):
                raise StageHookError("scope_context_pair_not_selected")
            records = []
            source_references = []
            hints = []
            hint_files = []
            number_bbox = manifest_item.get("number_bbox")
            for source_page in manifest_item["source_pages"]:
                index = int(source_page) - 1
                hint_file = context.run_dir / "solutions" / "detections" / "problem-hints" / f"page-{index + 1:04d}.json"
                wrapper = json.loads(hint_file.read_text(encoding="utf-8"))
                hint = wrapper["result"]
                if hint != state.problem_hints.get(index):
                    raise StageHookError("scope_context_hint_changed")
                receipt = wrapper.get("meta", {}).get("runtime_receipt", {})
                hashes = receipt.get("image_sha256", [])
                if receipt.get("status") != "succeeded" or len(hashes) < 5:
                    raise StageHookError("scope_context_hint_provenance_missing")
                hints.append(hint)
                hint_files.append({"path": str(hint_file), "sha256": _digest_file(hint_file)})
                pages = [(index, "PRIMARY problem page", 0)]
                offset = 5
                for neighbor, role in ((index - 1, "PREVIOUS problem identity context"), (index + 1, "NEXT problem identity context")):
                    if 0 <= neighbor < int(state.run_state["total_pages"]):
                        pages.append((neighbor, role, offset))
                        offset += 1
                for page, role, position in pages:
                    path = qc.page_image_path(context.run_dir / "pages", page, 200)
                    if position >= len(hashes) or _digest_file(path) != hashes[position]:
                        raise StageHookError("scope_context_image_changed")
                    record = {"path": str(path), "sha256": hashes[position], "role": role, "pdf_page": page + 1, "source_sha256": problem_sha}
                    records.append(record)
                    if (
                        role == "PRIMARY problem page"
                        and isinstance(number_bbox, Mapping)
                        and number_bbox.get("source_page") == page + 1
                        and _valid_bbox(number_bbox.get("bbox"))
                    ):
                        source_references.append({
                            "source_role": "current_problem",
                            "source_sha256": problem_sha,
                            "image_sha256": hashes[position],
                            "pdf_page": page + 1,
                            "target_number": item.parent_number,
                            "target_bbox": [int(value) for value in number_bbox["bbox"]],
                        })

            candidate_pages = tuple(dict.fromkeys(part["pdf_index"] for part in native["parts"]))
            page_start, page_end = (int(value) for value in episode["page_span"])
            if not candidate_pages or not all(page_start <= page <= page_end for page in candidate_pages):
                raise StageHookError("scope_context_episode_pages_changed")
            chain_pages = tuple(range(page_start, max(candidate_pages) + 1))
            page_evidence = {}
            episode_label = _semantic_label(episode.get("raw_label"))
            heading_page = None
            for page in chain_pages:
                raw_path = context.run_dir / "solutions/detections/solution-blocks/raw/codex" / f"page-{page + 1:04d}.json"
                consensus_path = context.run_dir / consensus_relative / f"page-{page + 1:04d}.json"
                raw = json.loads(raw_path.read_text(encoding="utf-8"))
                consensus = json.loads(consensus_path.read_text(encoding="utf-8"))
                receipt = raw.get("meta", {}).get("runtime_receipt", {})
                if consensus.get("meta", {}).get("raw_sha256") != _digest_file(raw_path) or receipt.get("status") != "succeeded":
                    raise StageHookError("scope_context_solution_provenance_missing")
                path = qc.page_image_path(context.run_dir / "solutions/render" / solution_sha[:12] / "pages", page, 200)
                if not receipt.get("image_sha256") or _digest_file(path) != receipt["image_sha256"][0]:
                    raise StageHookError("scope_context_solution_image_changed")
                blocks = consensus.get("result", {}).get("blocks", [])
                if not isinstance(blocks, list):
                    raise StageHookError("scope_context_solution_blocks_missing")
                if heading_page is None and episode_label and any(
                    episode_label in {
                        _semantic_label(block.get("numbering_scope_label")),
                        *(_semantic_label(value) for value in block.get("hierarchy_path", [])),
                    }
                    for block in blocks
                    if isinstance(block, Mapping)
                ):
                    heading_page = page
                page_evidence[page] = (path, receipt["image_sha256"][0], blocks)
            if heading_page is None:
                raise StageHookError("scope_context_heading_unavailable")

            candidate_geometry = None
            if asset.role == SolutionRole.QUICK_ANSWER:
                if len(native["parts"]) != 1:
                    raise StageHookError("scope_answer_cell_geometry_untrusted")
                part = native["parts"][0]
                blocks = page_evidence[part["pdf_index"]][2]
                matching_blocks = [
                    block for block in blocks
                    if isinstance(block, Mapping)
                    and block.get("role") == SolutionRole.QUICK_ANSWER.value
                    and tuple(block.get("bbox", ())) == tuple(part["bbox"])
                    and any(
                        isinstance(entry, Mapping)
                        and normalize_parent_number(entry.get("parent_number")) == item.parent_number
                        and normalize_subquestion_label(entry.get("subquestion_label")) == item.subquestion_label
                        for entry in block.get("entries", [])
                    )
                ]
                if len(matching_blocks) != 1:
                    raise StageHookError("scope_answer_cell_geometry_untrusted")
                block = matching_blocks[0]
                target_entries = [
                    entry for entry in block["entries"]
                    if normalize_parent_number(entry.get("parent_number")) == item.parent_number
                    and normalize_subquestion_label(entry.get("subquestion_label")) == item.subquestion_label
                ]
                if len(target_entries) != 1:
                    raise StageHookError("scope_answer_cell_geometry_untrusted")
                cells = tuple(
                    AnswerCell(
                        str(entry.get("entry_id", "")),
                        tuple(int(value) for value in entry["answer_bbox"])
                        if _valid_bbox(entry.get("answer_bbox")) else None,
                    )
                    for entry in block["entries"]
                )
                cell_bbox = isolated_answer_cell_bbox(
                    tuple(int(value) for value in block["bbox"]),
                    cells,
                    str(target_entries[0].get("entry_id", "")),
                )
                if cell_bbox is None:
                    raise StageHookError("scope_answer_cell_geometry_untrusted")
                candidate_geometry = {
                    "source_sha256": solution_sha,
                    "image_sha256": page_evidence[part["pdf_index"]][1],
                    "pdf_page": part["pdf_page"],
                    "target_number": item.parent_number,
                    "target_bbox": list(cell_bbox),
                    "answer_cell_geometry": "isolated",
                }

            for page in chain_pages:
                path, image_sha, _blocks = page_evidence[page]
                candidate_part = next((part for part in native["parts"] if part["pdf_index"] == page), None)
                role = (
                    "CANDIDATE solution page"
                    if candidate_part is not None
                    else "EPISODE heading context"
                    if page == heading_page
                    else "EPISODE continuity context"
                )
                record = {"path": str(path), "sha256": image_sha, "role": role, "pdf_page": page + 1, "source_sha256": solution_sha, "episode_heading": page == heading_page, "episode_continuity_index": page - page_start}
                records.append(record)
                if candidate_part is not None:
                    target_bbox = (
                        candidate_geometry["target_bbox"]
                        if candidate_geometry is not None
                        else [int(value) for value in candidate_part["bbox"]]
                    )
                    source_references.append({
                        "source_role": "ordered_source_context" if asset.role == SolutionRole.QUICK_ANSWER else "current_detailed_solution",
                        "source_sha256": solution_sha,
                        "image_sha256": image_sha,
                        "pdf_page": page + 1,
                        "target_number": item.parent_number,
                        "target_bbox": target_bbox,
                    })
            result[(pair.problem_id, pair.asset_id)] = {
                "problem": {"hierarchy": hints[0]["hierarchy_path"], "numbering_scope": hints[0]["numbering_scope"], "context_warnings": [warning for hint in hints for warning in hint.get("warnings", [])]},
                "episode": {key: episode[key] for key in ("episode_id", "raw_label", "section_tokens", "entry_numbers", "page_span", "printed_problem_page_range")},
                "ordered_context": [{"item_id": value.item_id, "parent_number": value.parent_number, "sequence": value.sequence} for value in sorted(problem.items, key=lambda value: value.sequence) if value.scope_id == item.scope_id],
                "binding": {"problem_id": pair.problem_id, "asset_id": pair.asset_id, "problem_source_sha256": problem_sha, "solution_source_sha256": solution_sha, "snapshot_path": str(snapshot_path), "snapshot_sha256": _digest_file(snapshot_path), "hint_files": hint_files},
                "source_context_images": records,
                "source_references": source_references,
                "candidate_geometry": candidate_geometry,
            }
        state.scope_input_contexts = copy.deepcopy(result)
        return result

    def _verification_images(
        self,
        context: LinkContext,
        state: RuntimeState,
        problem: ProblemItem,
        asset: SolutionAsset,
        corroborating: SolutionAsset | None = None,
        *,
        exact_geometry: bool = False,
    ) -> list[Path]:
        item = state.manifest_items_by_id[problem.item_id]
        source_crop = context.run_dir / str(item["output_file"])
        directory = (
            context.run_dir
            / "solutions"
            / "detections"
            / "verification-images"
            / _digest_file(context.solution_path)[:12]
        )
        inside(directory.resolve(), "output")
        directory.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^0-9A-Za-z가-힣_-]+", "-", problem.item_id + "-" + asset.asset_id)
        crop_key = _digest_file(source_crop)[:12]
        problem_image = directory / f"page-0001-{safe}-problem-{crop_key}.png"
        if context.force or not problem_image.exists():
            shutil.copy2(source_crop, problem_image)
        all_assets = tuple(state.assets_by_id.values())
        rendered_candidate = asset if exact_geometry else output_asset_with_context(asset, all_assets)
        candidate_source = self._source_pdf(state) if rendered_candidate.source_document == "problem" else context.solution_path
        candidate_image = solution_asset_output_path(context, rendered_candidate, candidate_source)
        if context.force or not candidate_image.exists():
            with self.pdf_lock:
                self.asset_renderer(
                    self._source_pdf(state) if rendered_candidate.source_document == "problem" else context.solution_path,
                    rendered_candidate,
                    candidate_image,
                    context.crop_dpi,
                )
        images = [problem_image, candidate_image]
        if corroborating is not None:
            rendered_detail = output_asset_with_context(corroborating, all_assets)
            detail_source = self._source_pdf(state) if rendered_detail.source_document == "problem" else context.solution_path
            detail_image = solution_asset_output_path(context, rendered_detail, detail_source)
            if context.force or not detail_image.exists():
                with self.pdf_lock:
                    self.asset_renderer(
                        self._source_pdf(state) if rendered_detail.source_document == "problem" else context.solution_path,
                        rendered_detail,
                        detail_image,
                        context.crop_dpi,
                    )
            images.append(detail_image)
        return images

    @staticmethod
    def _unwrap_verification(result: Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(result.get("questions"), list) and len(result["questions"]) == 1:
            value = result["questions"][0]
            if isinstance(value, dict):
                return value
        return dict(result)

    @staticmethod
    def _normalize_qwen_verification_result(
        value: Mapping[str, Any],
        *,
        question_id: str,
        asset_id: str,
        role: SolutionRole,
        subquestion_label: str | None,
    ) -> dict[str, Any]:
        result = copy.deepcopy(dict(value))

        def comparison(raw: object) -> object:
            text = str(raw or "").strip().casefold()
            return {
                "yes": "match",
                "true": "match",
                "no": "mismatch",
                "false": "mismatch",
                "uncertain": "unknown",
                "n/a": "not_applicable",
                "na": "not_applicable",
            }.get(text, raw)

        def boolean(raw: object) -> object:
            if isinstance(raw, bool):
                return raw
            text = str(raw or "").strip().casefold()
            if text in {"true", "yes"}:
                return True
            if text in {"false", "no"}:
                return False
            return raw

        result["question_id"] = str(
            result.get("question_id") or result.pop("item_id", None) or question_id
        )
        result.pop("item_id", None)
        candidate_id = result.pop("candidate_asset_id", None)
        result.setdefault("candidate_ids", [str(candidate_id or asset_id)])
        result.setdefault("candidate_role", role.value)
        nested_evidence = result.get("evidence")
        if isinstance(nested_evidence, Mapping):
            for field, field_value in nested_evidence.items():
                result.setdefault(str(field), field_value)
            result["evidence"] = [
                f"{field}={field_value}"
                for field, field_value in nested_evidence.items()
            ]
        crop_checks = result.pop("crop_checks", None)
        if isinstance(crop_checks, Mapping):
            for field, field_value in crop_checks.items():
                result.setdefault(str(field), field_value)
        aliases = {
            "hierarchy_match": "hierarchy_scope_match",
            "scope_match": "hierarchy_scope_match",
            "subquestion_label_match": "subquestion_match",
            "order_monotonic": "monotonic_order_match",
        }
        for source, target in aliases.items():
            if target not in result and source in result:
                result[target] = result[source]
            result.pop(source, None)
        same_question = result.get("same_question")
        if isinstance(same_question, bool):
            result["same_question"] = "yes" if same_question else "no"
        elif str(same_question).strip().casefold() in {"true", "false"}:
            result["same_question"] = (
                "yes"
                if str(same_question).strip().casefold() == "true"
                else "no"
            )
        for field in (
            "anchor_segment_match",
            "hierarchy_scope_match",
            "parent_number_match",
            "subquestion_match",
            "range_or_group_match",
            "monotonic_order_match",
            "fingerprint_match",
        ):
            if field in result:
                result[field] = comparison(result[field])
        if (
            "parent_number_match" not in result
            and result.get("same_question") == "yes"
            and boolean(result.get("target_identifier_visible")) is True
        ):
            result["parent_number_match"] = "match"
        if "subquestion_match" not in result and subquestion_label is None:
            result["subquestion_match"] = "not_applicable"
        for field in (
            "candidate_crop_complete",
            "target_identifier_visible",
            "first_line_complete",
            "last_line_complete",
            "neighbor_entry_absent",
            "next_question_absent",
            "explicit_answer_conflict",
        ):
            if field in result:
                result[field] = boolean(result[field])
        if "target_answer_visible" in result:
            visible = boolean(result["target_answer_visible"])
            if isinstance(visible, bool):
                result["target_answer_visible"] = "yes" if visible else "no"
            elif str(visible).strip().casefold() in {"n/a", "na"}:
                result["target_answer_visible"] = "not_applicable"
        if "target_answer_visible" not in result and role != SolutionRole.QUICK_ANSWER:
            result["target_answer_visible"] = "not_applicable"
        fallback_comparison = (
            "mismatch"
            if result.get("decision") in {"wrong_link", "conflict"}
            or result.get("same_question") == "no"
            else "unknown"
        )
        for field in (
            "anchor_segment_match",
            "hierarchy_scope_match",
            "parent_number_match",
            "subquestion_match",
            "range_or_group_match",
            "monotonic_order_match",
            "fingerprint_match",
        ):
            result.setdefault(field, fallback_comparison)
        evidence = result.pop("evidence_ko", None)
        if "evidence" not in result:
            result["evidence"] = [] if not evidence else [str(evidence)]
        elif isinstance(result["evidence"], str):
            result["evidence"] = [result["evidence"]]
        review_reason = result.pop("review_reason_ko", None)
        review_reason = result.pop("review_reason", review_reason)
        if not isinstance(result.get("review_reasons"), list) or (
            not result["review_reasons"] and review_reason
        ):
            result["review_reasons"] = (
                [] if not review_reason else [str(review_reason)]
            )
        if (
            role == SolutionRole.QUICK_ANSWER
            and result.get("decision") == "verified"
            and result.get("same_question") == "yes"
            and result.get("target_identifier_visible") is True
            and result.get("target_answer_visible") == "yes"
            and result.get("fingerprint_match") in {None, "unknown"}
        ):
            result["fingerprint_match"] = "not_applicable"
        categorical_pass = (
            result.get("decision") == "verified"
            and result.get("same_question") == "yes"
            and result.get("candidate_crop_complete") is True
            and result.get("target_identifier_visible") is True
            and result.get("first_line_complete") is True
            and result.get("last_line_complete") is True
            and result.get("neighbor_entry_absent") is True
            and result.get("next_question_absent") is True
        )
        if "confidence" not in result or (
            categorical_pass and float(result.get("confidence", 0)) <= 0.5
        ):
            result["confidence"] = 0.9 if categorical_pass else 0.5
        return result

    @staticmethod
    def _normalize_verification_context(
        value: Mapping[str, Any], problem: ProblemItem, asset: SolutionAsset
    ) -> dict[str, Any]:
        return quality.normalize_link_result(dict(value), problem.signature(), asset.signature())


    @staticmethod
    def _neighbor_evidence_state(result: Mapping[str, Any]) -> str | None:
        return neighbor_evidence_state(result)

    @classmethod
    def _verification_semantic_errors(
        cls,
        result: Mapping[str, Any],
        problem: ProblemItem,
        asset: SolutionAsset,
        order_context: Mapping[str, Any] | None = None,
    ) -> tuple[str, ...]:
        errors: list[str] = []
        if result.get("question_id") != problem.item_id:
            errors.append("question_id does not match the supplied problem")
        if asset.asset_id not in result.get("candidate_ids", []):
            errors.append("candidate_ids do not contain the supplied asset")
        if result.get("candidate_role") != asset.role.value:
            errors.append("candidate_role does not match the supplied asset")
        if (
            problem.anchor is not None
            and problem.anchor.reference_document != "unknown"
            and problem.anchor.reference_document != asset.source_document
        ):
            errors.append("candidate source document contradicts the problem reference")
        computed_order = (
            order_context.get("computed_monotonic_order_match")
            if order_context is not None
            else None
        )
        if computed_order in {"mismatch", "unknown"} and result.get(
            "monotonic_order_match"
        ) != computed_order:
            errors.append(
                "monotonic_order_match contradicts deterministic neighbor evidence"
            )

        neighbor_state = cls._neighbor_evidence_state(result)
        if neighbor_state == "present" and result.get("next_question_absent") is True:
            errors.append("evidence says a neighboring question is present but next_question_absent=true")
        elif neighbor_state == "absent" and result.get("next_question_absent") is False:
            errors.append("evidence says no neighboring question is present but next_question_absent=false")
        elif neighbor_state == "conflict":
            errors.append("evidence contains conflicting neighboring-question claims")

        decision = result.get("decision")
        if decision not in {"verified", "conflict"}:
            return tuple(dict.fromkeys(errors))
        if problem.anchor is None and asset.expanded_source_binding is not None:
            for field in ('hierarchy_scope_match', 'fingerprint_match'):
                if result.get(field) != 'match':
                    errors.append(f'anchorless source-bound candidate requires {field}=match')
        if result.get("same_question") != "yes":
            errors.append(f"decision={decision} requires same_question=yes")
        if decision == "verified" and result.get("explicit_answer_conflict"):
            errors.append("decision=verified cannot contain an explicit answer conflict")
        if decision == "conflict" and result.get("explicit_answer_conflict") is not True:
            errors.append("decision=conflict requires an explicit answer conflict")
        for field in (
            "candidate_crop_complete",
            "target_identifier_visible",
            "first_line_complete",
            "last_line_complete",
            "neighbor_entry_absent",
            "next_question_absent",
        ):
            if result.get(field) is not True:
                errors.append(f"decision={decision} requires {field}=true")
        if asset.role == SolutionRole.QUICK_ANSWER:
            if result.get("target_answer_visible") != "yes":
                errors.append(
                    f"{decision} quick answers require target_answer_visible=yes"
                )
        elif result.get("target_answer_visible") not in {"yes", "not_applicable"}:
            errors.append(
                f"{decision} detailed solutions require a valid target_answer_visible state"
            )

        required_matches = [
            "anchor_segment_match",
            "parent_number_match",
            "monotonic_order_match",
        ]
        if asset.role != SolutionRole.QUICK_ANSWER:
            required_matches.extend(("hierarchy_scope_match", "fingerprint_match"))
        for field in required_matches:
            if result.get(field) != "match":
                errors.append(f"decision={decision} requires {field}=match")
        optional_matches = ["subquestion_match", "range_or_group_match"]
        if asset.role == SolutionRole.QUICK_ANSWER:
            optional_matches.extend(("hierarchy_scope_match", "fingerprint_match"))
        for field in optional_matches:
            if result.get(field) not in {"match", "not_applicable"}:
                errors.append(
                    f"decision={decision} requires {field}=match or not_applicable"
                )
        return tuple(dict.fromkeys(errors))

    @staticmethod
    def _scope_verification_semantic_errors(
        result: Mapping[str, Any],
        problem: ProblemItem,
        asset: SolutionAsset,
        solution_scope_id: str,
    ) -> tuple[str, ...]:
        errors: list[str] = []
        exact = {
            "question_id": problem.item_id,
            "candidate_role": asset.role.value,
            "problem_scope_id": problem.scope_id,
            "solution_scope_id": solution_scope_id,
        }
        for field, expected in exact.items():
            if result.get(field) != expected:
                errors.append(f"{field} does not match the supplied identity")
        if result.get("candidate_ids") != [asset.asset_id]:
            errors.append("candidate_ids must contain exactly the supplied asset")
        neighbor_state = SolutionRuntimeAdapter._neighbor_evidence_state(result)
        if neighbor_state == "present" and result.get("next_question_absent") is True:
            errors.append(
                "evidence says a neighboring question is present but next_question_absent=true"
            )
        elif neighbor_state == "absent" and result.get("next_question_absent") is False:
            errors.append(
                "evidence says no neighboring question is present but next_question_absent=false"
            )
        elif neighbor_state == "conflict":
            errors.append("evidence contains conflicting neighboring-question claims")
        confidence = result.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            errors.append("confidence must be a finite number in 0..1")

        decision = result.get("decision")
        if decision not in {"verified", "conflict"}:
            return tuple(dict.fromkeys(errors))
        if result.get("same_question") != "yes":
            errors.append(f"decision={decision} requires same_question=yes")
        if decision == "verified" and result.get("explicit_answer_conflict"):
            errors.append("decision=verified cannot contain an explicit answer conflict")
        if decision == "conflict" and result.get("explicit_answer_conflict") is not True:
            errors.append("decision=conflict requires an explicit answer conflict")
        for field in (
            "scope_episode_match",
            "hierarchy_scope_match",
            "parent_number_match",
            "monotonic_order_match",
            "fingerprint_match",
        ):
            if result.get(field) != "match":
                errors.append(f"decision={decision} requires {field}=match")
        expected_subquestion = (
            {"match"}
            if problem.subquestion_label is not None
            else {"match", "not_applicable"}
        )
        if result.get("subquestion_match") not in expected_subquestion:
            errors.append(f"decision={decision} requires a valid subquestion_match")
        for field in (
            "candidate_crop_complete",
            "target_identifier_visible",
            "first_line_complete",
            "last_line_complete",
            "neighbor_entry_absent",
            "next_question_absent",
        ):
            if result.get(field) is not True:
                errors.append(f"decision={decision} requires {field}=true")
        if asset.role == SolutionRole.QUICK_ANSWER:
            if result.get("target_answer_visible") != "yes":
                errors.append(
                    f"{decision} quick answers require target_answer_visible=yes"
                )
        elif result.get("target_answer_visible") not in {"yes", "not_applicable"}:
            errors.append(
                f"{decision} detailed solutions require a valid target_answer_visible state"
            )
        return tuple(dict.fromkeys(errors))

    @classmethod
    def _scope_verification_pair(
        cls,
        result: Mapping[str, Any],
        provider: str,
        problem: ProblemItem,
        asset: SolutionAsset,
        solution_scope_id: str,
    ) -> PairVerification:
        same = (
            result.get("decision") == "verified"
            and not cls._scope_verification_semantic_errors(
                result, problem, asset, solution_scope_id
            )
        )
        confidence = float(result.get("confidence", 0.0))
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            confidence = 0.0
        return PairVerification(
            same,
            confidence,
            provider,
            bool(
                result.get("decision") == "conflict"
                or result.get("explicit_answer_conflict")
            ),
        )

    @classmethod
    def _combine_scope_verifications(
        cls,
        gemini: Mapping[str, Any] | None,
        qwen: Mapping[str, Any] | None,
        problem: ProblemItem,
        asset: SolutionAsset,
        solution_scope_id: str,
    ) -> PairVerification | None:
        if gemini is None or qwen is None:
            return None
        pairs = (
            cls._scope_verification_pair(
                gemini, "gemini", problem, asset, solution_scope_id
            ),
            cls._scope_verification_pair(
                qwen, "qwen", problem, asset, solution_scope_id
            ),
        )
        semantic_errors = (
            cls._scope_verification_semantic_errors(
                gemini, problem, asset, solution_scope_id
            ),
            cls._scope_verification_semantic_errors(
                qwen, problem, asset, solution_scope_id
            ),
        )
        conflict_flags = (
            bool(
                gemini.get("decision") == "conflict"
                or gemini.get("explicit_answer_conflict")
            ),
            bool(
                qwen.get("decision") == "conflict"
                or qwen.get("explicit_answer_conflict")
            ),
        )
        if (
            gemini.get("decision") == qwen.get("decision") == "conflict"
            and all(pair.explicit_answer_conflict for pair in pairs)
            and not any(semantic_errors)
        ):
            return PairVerification(
                False,
                min(pair.confidence for pair in pairs),
                "scope-gemini+qwen",
                True,
            )
        if any(conflict_flags):
            return None

        def wrong(value: Mapping[str, Any]) -> bool:
            return (
                value.get("decision") == "wrong_link"
                and value.get("same_question") == "no"
                and value.get("explicit_answer_conflict") is False
            )

        if wrong(gemini) and wrong(qwen) and not any(semantic_errors):
            return PairVerification(
                False,
                min(pair.confidence for pair in pairs),
                "scope-gemini+qwen",
            )
        if (
            all(pair.same_item for pair in pairs)
            and min(pair.confidence for pair in pairs) >= MIN_LINK_CONFIDENCE
        ):
            return PairVerification(
                True,
                min(pair.confidence for pair in pairs),
                "scope-gemini+qwen",
            )
        return None

    def _scope_provider_result(
        self,
        context: LinkContext,
        provider: str,
        prompt: str,
        envelope: dict[str, Any],
        images: list[Path],
        output: Path,
        *,
        problem: ProblemItem,
        asset: SolutionAsset,
        solution_scope_id: str,
        scope_context: Mapping[str, Any] | None = None,
        cache_only: bool = False,
    ) -> dict[str, Any] | None:
        """One-shot Phase 2c provider call; invalid output stays REVIEW."""

        if provider != "astra":
            return None
        model = astra_cli.MODEL
        key = scope_pair_contract_key(provider, prompt, images, model)
        invalid_output = output.with_name(output.stem + ".invalid.json")
        contract_schema = json.loads(
            SCOPE_VERIFY_SCHEMA.read_text(encoding="utf-8")
        )

        def cached(path: Path) -> Mapping[str, Any] | None:
            if context.force or not path.exists():
                return None
            try:
                wrapper = json.loads(path.read_text(encoding="utf-8"))
                meta = wrapper.get("meta", {})
                if (
                    meta.get("provider") == provider
                    and meta.get("namespace") == f"verify-scope-{provider}"
                    and meta.get("cache_key") == key
                    and meta.get("contract") == SCOPE_VERIFY_CONTRACT_VERSION
                ):
                    value = wrapper.get("result")
                    return value if isinstance(value, Mapping) else None
            except (OSError, json.JSONDecodeError):
                return None
            return None

        raw = cached(output)
        if raw is None and cached(invalid_output) is not None:
            return None
        if raw is None:
            if cache_only:
                _atomic_json(quality.pointer(context.run_dir, problem.item_id, asset.asset_id),
                             {"status": "review", "reason": "scope_verification_cache_miss"})
                return None
            _atomic_json(quality.pointer(context.run_dir, problem.item_id, asset.asset_id),
                         {"status": "review", "reason": "quality_scope_verification_started"})
            labels = ["PROBLEM crop", "CANDIDATE solution crop"]
            try:
                raw = self.codex_runner(stage="solution-scope-verify", prompt=prompt, schema_path=SCOPE_VERIFY_SCHEMA, images=images, output=output.with_name(output.stem + ".raw.json"), run_dir=context.run_dir, model=astra_cli.MODEL, force=context.force)
                if raw is None:
                    raise astra_cli.AstraError("scope_verification_unavailable")
            except (OSError, RuntimeError, ValueError, ContractError) as exc:
                _persist_boundary_error(
                    output, provider, model, _provider_error_reason(exc)
                )
                return None
        try:
            value = self._unwrap_verification(raw)
            validate_contract(value, contract_schema)
            semantic_errors = self._scope_verification_semantic_errors(
                value, problem, asset, solution_scope_id
            )
            if scope_context is not None:
                semantic_errors += _scope_source_reference_errors(value, scope_context)
        except (TypeError, ValueError, ContractError) as exc:
            value = dict(raw) if isinstance(raw, Mapping) else {}
            semantic_errors = (f"contract:{exc}",)
        if semantic_errors:
            output.unlink(missing_ok=True)
            _atomic_json(
                invalid_output,
                {
                    "meta": {
                        "provider": provider,
                        "model": model,
                        "cache_key": key,
                        "prompt_sha256": quality.digest(prompt),
                        "namespace": f"verify-scope-{provider}",
                        "contract": SCOPE_VERIFY_CONTRACT_VERSION,
                    },
                    "validation_errors": list(semantic_errors),
                    "result": value,
                },
            )
            return None
        _atomic_json(
            output,
            {
                "meta": {
                    "provider": provider,
                    "model": model,
                    "cache_key": key,
                        "prompt_sha256": quality.digest(prompt),
                    "namespace": f"verify-scope-{provider}",
                    "contract": SCOPE_VERIFY_CONTRACT_VERSION,
                },
                "result": value,
            },
        )
        invalid_output.unlink(missing_ok=True)
        _boundary_error_path(output).unlink(missing_ok=True)
        return value

    def verify_scope_candidate_pairs(
        self,
        context: LinkContext,
        pairs: tuple[ScopeCandidatePair, ...],
        problems: Mapping[str, ProblemItem],
        assets: Mapping[str, SolutionAsset],
        scope_contexts: Mapping[tuple[str, str], Mapping[str, Any]],
        *,
        cache_only: bool = False,
        allow_provider_calls: bool = False,
        independent_roles: bool = False,
    ) -> Mapping[tuple[str, str], PairVerification]:
        """Verify preflight-approved hard-selected scope pairs only."""

        if pairs and not cache_only and not allow_provider_calls:
            raise StageHookError(
                "scope-pair provider calls require an approved Phase 2c-B plan"
            )
        blocker_markers = {
            "scope_alignment_ambiguous",
            "scope_alignment_untrusted",
            "scope_alignment_unresolved",
            "episode_coverage_incomplete",
            "scope_alignment_low_margin",
            "pooled_quick_candidate",
            "quick_uncorroborated",
        }
        for pair in pairs:
            problem = problems.get(pair.problem_id)
            asset = assets.get(pair.asset_id)
            if (
                problem is None
                or asset is None
                or pair.problem_scope_id != problem.scope_id
                or pair.role != asset.role
                or asset.scope_id != f"episode:{pair.solution_scope_id}"
                or not pair.alignment_roles
                or not set(pair.alignment_roles).issubset(
                    {"detailed_solution", "quick_answer"}
                )
                or blocker_markers.intersection(pair.alignment_evidence)
                or not problem.crop_is_search_eligible
                or problem.cache_only_detailed_review
                or asset.review_reason is not None
                or asset.parent_number != problem.parent_number
                or asset.subquestion_label != problem.subquestion_label
            ):
                raise StageHookError("scope candidate pair is not provider-eligible")
            expected_alignment_role = {
                SolutionRole.QUICK_ANSWER: ("quick_answer",),
                SolutionRole.DETAILED_SOLUTION: ("detailed_solution",),
            }.get(asset.role)
            if expected_alignment_role is None or pair.alignment_roles != expected_alignment_role:
                raise StageHookError("scope candidate pair role provenance is invalid")
        state = self._state(context)
        state.assets_by_id.update(assets)
        schema = json.loads(SCOPE_VERIFY_SCHEMA.read_text(encoding="utf-8"))
        envelope = _verification_envelope(schema)
        template = SCOPE_VERIFY_PROMPT.read_text(encoding="utf-8")
        directory = (
            context.run_dir
            / "solutions"
            / "detections"
            / "scope-verifications"
        )
        results: dict[tuple[str, str], PairVerification] = {}
        confirmed_detail_problem_ids: set[str] = set()
        ordered_pairs = sorted(
            pairs,
            key=lambda value: (
                "detailed_solution" not in value.alignment_roles,
                value.problem_id,
                value.asset_id,
            ),
        )
        for pair in ordered_pairs:
            problem = problems.get(pair.problem_id)
            asset = assets.get(pair.asset_id)
            if problem is None or asset is None:
                continue
            if (not independent_roles and pair.role == SolutionRole.QUICK_ANSWER
                    and pair.problem_id not in confirmed_detail_problem_ids):
                continue
            try:
                supplied_context = scope_contexts.get((pair.problem_id, pair.asset_id), {})
                verification_asset = _scope_verification_asset(asset, supplied_context)
                images = self._verification_images(
                    context,
                    state,
                    problem,
                    verification_asset,
                    exact_geometry=asset.role == SolutionRole.QUICK_ANSWER,
                )
                if "source_context_images" in supplied_context:
                    if supplied_context != state.scope_input_contexts.get((pair.problem_id, pair.asset_id)):
                        raise StageHookError("scope context was not built from current sources")
                    binding = supplied_context["binding"]
                    if (_digest_file(Path(binding["snapshot_path"])) != binding["snapshot_sha256"]
                            or _digest_file(context.solution_path) != binding["solution_source_sha256"]
                            or _digest_file(self._source_pdf(state)) != binding["problem_source_sha256"]):
                        raise StageHookError("scope context source changed")
                    solution_scope_mod.read_scope_index(Path(binding["snapshot_path"]), expected_solution_sha256=binding["solution_source_sha256"], expected_provider_profile=self.provider_profile, solutions_dir=context.run_dir / "solutions")
                    if any(_digest_file(Path(entry["path"])) != entry["sha256"] for entry in binding["hint_files"]):
                        raise StageHookError("scope context hint changed")
                    image_errors = _scope_context_image_errors(supplied_context)
                    if image_errors:
                        raise StageHookError("; ".join(image_errors))
                    for record in supplied_context["source_context_images"]:
                        image = inside(Path(record["path"]).resolve(), "scope context image")
                        images.append(image)
            except (OSError, ValueError, KeyError, TypeError, StageHookError):
                _atomic_json(quality.pointer(context.run_dir, problem.item_id, asset.asset_id),
                             {"status": "review", "reason": "scope_context_unavailable"})
                continue
            prompt = _scope_verification_prompt(
                template,
                problem,
                asset,
                CandidatePair(
                    pair.problem_id,
                    pair.asset_id,
                    pair.role,
                    pair.fingerprint_similarity,
                ),
                solution_scope_id=pair.solution_scope_id,
                scope_context=scope_contexts.get(
                    (pair.problem_id, pair.asset_id),
                    {
                        "alignment_roles": list(pair.alignment_roles),
                        "alignment_evidence": list(pair.alignment_evidence),
                    },
                ),
            )
            rendered_asset = verification_asset
            try:
                if asset.role != SolutionRole.QUICK_ANSWER:
                    rendered_asset = output_asset_with_context(asset, tuple(state.assets_by_id.values()))
                target = target_for(context.run_dir, problem, asset, rendered_asset,
                    solution_path=context.solution_path, images=images)
                prompt += "\nCURRENT_EVIDENCE_TARGET_SHA256=" + quality.identity(target)
            except (astra_cli.AstraError, OSError, ValueError, KeyError):
                target = None
                if "source_context_images" in scope_contexts.get((pair.problem_id, pair.asset_id), {}):
                    _atomic_json(quality.pointer(context.run_dir, problem.item_id, asset.asset_id),
                                 {"status": "review", "reason": "scope_context_target_unavailable"})
                    continue
            stem = f"{pair.problem_id}-{pair.asset_id}"
            result = self._scope_provider_result(context, "astra", prompt, envelope, images, directory / f"{stem}-astra.json", problem=problem, asset=asset, solution_scope_id=pair.solution_scope_id, scope_context=supplied_context, cache_only=cache_only)
            combined = self._scope_verification_pair(result, "astra-scope-quality-pending", problem, asset, pair.solution_scope_id) if result is not None else None
            if combined is not None:
                combined = self._bind_quality(context, state, problem, asset, images,
                    directory / f"{stem}-astra.json", SCOPE_VERIFY_PROMPT,
                    SCOPE_VERIFY_SCHEMA, result, combined,
                    rendered_asset=rendered_asset)
                results[(pair.problem_id, pair.asset_id)] = combined
                if (pair.role == SolutionRole.DETAILED_SOLUTION
                        and _confirmed_verification(combined, problem, asset)
                        and combined.confidence >= MIN_LINK_CONFIDENCE):
                    confirmed_detail_problem_ids.add(pair.problem_id)
        return results

    def load_alignment_snapshot_metadata(
        self,
        context: LinkContext,
        verifications: Mapping[tuple[str, str], PairVerification],
    ) -> AlignmentSnapshotMetadata | None:
        """Return only complete, on-disk dual scope-verifier provenance."""
        key = (str(context.run_dir.resolve()), str(context.solution_path.resolve()))
        if (os.path.lexists(context.run_dir / "solutions/reviewed_episode_context.json")
                or getattr(self.states.get(key), "reviewed_context_provenance", None) is not None
                or getattr(self.states.get(key), "alignment_reviewed_context_provenance", None) is not None):
            state = self._state(context)
            if state.reviewed_context_provenance != state.alignment_reviewed_context_provenance:
                raise StageHookError("reviewed episode context changed before metadata publication")
        versions = {
            "core_version": CORE_VERSION,
            "runtime_version": RUNTIME_VERSION,
            "scope_index_version": solution_scope_mod.SCOPE_INDEX_VERSION,
            "scope_alignment_version": scope_alignment_mod.SCOPE_ALIGNMENT_VERSION,
            "verifier_contract_version": SCOPE_VERIFY_CONTRACT_VERSION,
        }
        if verifications and any(value.quality_receipt is not None or value.verifier in {'astra-current-evidence', 'astra-quality-pending', 'astra-scope-quality-pending'} for value in verifications.values()):
            root = inside(context.run_dir.resolve(), 'snapshot evidence root')
            state = self._state(context)
            current_records = []
            for (unit_id, asset_id), verification in sorted(verifications.items()):
                problem = state.problem_items_by_id.get(unit_id)
                asset = state.assets_by_id.get(asset_id)
                receipt = verification.quality_receipt
                if problem is None or asset is None or verification.evidence_root is None or verification.evidence_root.resolve() != root or not isinstance(receipt, dict):
                    return None
                result = receipt.get('result')
                if not isinstance(result, dict):
                    return None
                stage = receipt.get('stage')
                if stage == 'solution-scope-verify':
                    normalized = self._scope_verification_pair(result, verification.verifier, problem, asset, asset.scope_id.removeprefix('episode:'))
                elif stage in {'solution-verify', 'solution-verify-correction'}:
                    normalized = self._verification_pair(result, verification.verifier)
                else:
                    return None
                if (verification.same_item, verification.confidence, verification.explicit_answer_conflict) != (normalized.same_item, normalized.confidence, normalized.explicit_answer_conflict):
                    return None
                corroborating = state.assets_by_id.get(verification.corroborating_asset_id)
                if verification.corroborating_asset_id is not None and corroborating is None:
                    return None
                bound_corroborating = corroborating.signature() if corroborating is not None else None
                record = {'unit_id': unit_id, 'asset_id': asset_id, 'same_item': verification.same_item, 'confidence': verification.confidence, 'verifier': verification.verifier, 'explicit_answer_conflict': verification.explicit_answer_conflict, 'corroborating_asset_id': verification.corroborating_asset_id, 'contract': SCOPE_VERIFY_CONTRACT_VERSION, 'provider_refs': [], 'evidence_root': str(root)}
                if quality.validate_receipt(receipt, root, problem=problem.signature(), asset=asset.signature(), conflict=verification.explicit_answer_conflict, corroborating=bound_corroborating) is None:
                    record['quality_receipt'] = copy.deepcopy(receipt)
                else:
                    try:
                        observation = quality.read_nonapproval_receipt(receipt, root, problem=problem.signature(), asset=asset.signature(), corroborating=bound_corroborating)
                    except astra_cli.AstraError:
                        return None
                    if (observation.same_item, observation.confidence, observation.explicit_answer_conflict) != (normalized.same_item, normalized.confidence, normalized.explicit_answer_conflict):
                        return None
                    record.update(evidence_kind='nonapproval', nonapproval_receipt=copy.deepcopy(receipt))
                current_records.append(record)
            return AlignmentSnapshotMetadata(tuple(current_records), versions)
        if self.provider_profile == "codex_only":
            return AlignmentSnapshotMetadata((), versions)
        if not verifications or any(
            value.verifier != DUAL_SCOPE_VERIFIER
            for value in verifications.values()
        ):
            return None
        root = context.run_dir.resolve()
        directory = root / "solutions" / "detections" / "scope-verifications"
        records: list[Mapping[str, Any]] = []
        referenced: set[str] = set()
        for (unit_id, asset_id), verification in sorted(verifications.items()):
            refs: list[Mapping[str, Any]] = []
            for provider in ("gemini", "qwen"):
                path = directory / f"{unit_id}-{asset_id}-{provider}.json"
                try:
                    relative = path.resolve().relative_to(root).as_posix()
                    wrapper = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    return None
                meta = wrapper.get("meta") if isinstance(wrapper, Mapping) else None
                result = wrapper.get("result") if isinstance(wrapper, Mapping) else None
                namespace = f"verify-scope-{provider}"
                cache_key = meta.get("cache_key") if isinstance(meta, Mapping) else None
                if (
                    not isinstance(meta, Mapping)
                    or not isinstance(result, Mapping)
                    or meta.get("provider") != provider
                    or meta.get("namespace") != namespace
                    or meta.get("contract") != SCOPE_VERIFY_CONTRACT_VERSION
                    or not isinstance(cache_key, str)
                    or re.fullmatch(r"[0-9a-f]{64}", cache_key) is None
                    or result.get("question_id") != unit_id
                    or result.get("candidate_ids") != [asset_id]
                    or relative in referenced
                ):
                    return None
                referenced.add(relative)
                refs.append(
                    {
                        "provider": provider,
                        "namespace": namespace,
                        "cache_key": cache_key,
                        "contract": SCOPE_VERIFY_CONTRACT_VERSION,
                        "result_relative_path": relative,
                        "result_sha256": _digest_file(path),
                    }
                )
            records.append(
                {
                    "unit_id": unit_id,
                    "asset_id": asset_id,
                    "same_item": verification.same_item,
                    "confidence": verification.confidence,
                    "verifier": verification.verifier,
                    "explicit_answer_conflict": (
                        verification.explicit_answer_conflict
                    ),
                    "corroborating_asset_id": verification.corroborating_asset_id,
                    "contract": SCOPE_VERIFY_CONTRACT_VERSION,
                    "provider_refs": refs,
                }
            )
        return AlignmentSnapshotMetadata(
            tuple(records),
            versions,
        )

    def _provider_result(
        self,
        context: LinkContext,
        provider: str,
        prompt: str,
        schema: dict[str, Any],
        images: list[Path],
        output: Path,
        *,
        problem: ProblemItem,
        asset: SolutionAsset,
        order_context: Mapping[str, Any] | None = None,
        cache_only: bool = False,
    ) -> dict[str, Any] | None:
        if provider != "astra":
            return None
        model = astra_cli.MODEL
        state = self.states.get((str(context.run_dir.resolve()), str(context.solution_path.resolve())))
        contract_schema = json.loads(VERIFY_SCHEMA.read_text(encoding="utf-8"))
        labels = ["PROBLEM crop", "SOLUTION candidate"] + [
            "CORROBORATING detailed solution"
        ] * max(0, len(images) - 2)
        keys = _contract_cache_keys(
            f"verify-{provider}", prompt, VERIFY_SCHEMA, images, model
        )
        key = keys[0]
        invalid_output = output.with_name(output.stem + ".invalid.json")
        correction_output = output.with_name(output.stem + ".correction.json")
        correction_invalid_output = correction_output.with_name(
            correction_output.stem + ".invalid.json"
        )

        def normalize(raw: Mapping[str, Any]) -> dict[str, Any]:
            value = self._unwrap_verification(raw)
            if provider == "qwen":
                value = self._normalize_qwen_verification_result(
                    value,
                    question_id=problem.item_id,
                    asset_id=asset.asset_id,
                    role=asset.role,
                    subquestion_label=problem.subquestion_label,
                )
            return self._normalize_verification_context(value, problem, asset)

        def evaluate(
            raw: Mapping[str, Any],
        ) -> tuple[dict[str, Any], str | None, tuple[str, ...]]:
            try:
                value = normalize(raw)
            except (TypeError, ValueError):
                return {}, "response is not a verification object", ()
            try:
                validate_contract(value, contract_schema)
            except ContractError as exc:
                return value, str(exc), ()
            return value, None, self._verification_semantic_errors(
                value, problem, asset, order_context
            )

        def cached(
            path: Path, cache_keys: Sequence[str]
        ) -> Mapping[str, Any] | None:
            if context.force or not path.exists():
                return None
            try:
                wrapper = json.loads(path.read_text(encoding="utf-8"))
                if wrapper.get("meta", {}).get("cache_key") in cache_keys:
                    value = wrapper.get("result")
                    if state is not None:
                        state.recovery_trace.append({'stage': 'solution-verify', 'code': 'verification_exact_cache_hit',
                            'problem_id': problem.item_id, 'asset_id': asset.asset_id, 'cache_key': wrapper['meta']['cache_key']})
                    return value if isinstance(value, Mapping) else None
            except (OSError, json.JSONDecodeError):
                return None
            return None

        def invoke(current_prompt: str) -> Mapping[str, Any]:
            stage = "solution-verify" if current_prompt == prompt else "solution-verify-correction"
            if state is not None and state.verification_blocked_reason is not None:
                state.recovery_trace.append({'stage': stage, 'code': 'verification_runtime_blocked',
                    'reason': state.verification_blocked_reason, 'problem_id': problem.item_id, 'asset_id': asset.asset_id})
                raise astra_cli.AstraError(state.verification_blocked_reason)
            try:
                if state is not None:
                    state.recovery_trace.append({'stage': stage, 'code': 'verification_request_call',
                        'problem_id': problem.item_id, 'asset_id': asset.asset_id})
                value = self.codex_runner(stage=stage, prompt=current_prompt, schema_path=VERIFY_SCHEMA, images=images, output=output.with_name(output.stem + (".raw.json" if current_prompt == prompt else ".correction.raw.json")), run_dir=context.run_dir, model=astra_cli.MODEL, force=context.force)
            except astra_cli.AstraError as exc:
                if state is not None and exc.code in {
                    'cli_version_unsupported', 'cli_missing', 'auth_api_only', 'auth_missing',
                    'auth_refresh_needed', 'auth_expiry_unknown', 'auth_unreadable',
                }:
                    state.verification_blocked_reason = exc.code
                raise
            if value is None:
                raise astra_cli.AstraError("solution_verification_unavailable")
            return value

        base_raw = cached(output, keys)
        if base_raw is None:
            base_raw = cached(invalid_output, keys)
        if base_raw is None:
            if cache_only:
                return None
            try:
                base_raw = invoke(prompt)
            except (OSError, RuntimeError, ValueError, ContractError) as exc:
                _persist_boundary_error(
                    output, provider, model, _provider_error_reason(exc)
                )
                return None
        value, contract_error, semantic_errors = evaluate(base_raw)
        if contract_error is None:
            _atomic_json(
                output,
                {
                    "meta": {
                        "provider": provider,
                        "model": model,
                        "cache_key": key,
                        "prompt_sha256": quality.digest(prompt),
                    },
                    "result": value,
                },
            )
        else:
            _atomic_json(
                invalid_output,
                {
                    "meta": {
                        "provider": provider,
                        "model": model,
                        "cache_key": key,
                        "prompt_sha256": quality.digest(prompt),
                    },
                    "contract_error": contract_error,
                    "result": value,
                },
            )
        errors = (
            ((f"contract: {contract_error}",) if contract_error else ())
            + semantic_errors
        )
        if errors and state is not None:
            state.recovery_trace.append({'stage': 'solution-verify',
                'code': 'verification_schema_invalid' if contract_error else 'verification_semantic_invalid',
                'problem_id': problem.item_id, 'asset_id': asset.asset_id})
        if not errors:
            _boundary_error_path(output).unlink(missing_ok=True)
            return value

        correction_prompt = _verification_correction_prompt(prompt, errors, value)
        correction_keys = _contract_cache_keys(
            f"verify-{provider}-correction",
            correction_prompt,
            VERIFY_SCHEMA,
            images,
            model,
        )
        correction_key = correction_keys[0]
        corrected_raw = cached(correction_output, correction_keys)
        if corrected_raw is None:
            if cached(correction_invalid_output, correction_keys) is not None:
                return value if contract_error is None else None
            if cache_only:
                return value if contract_error is None else None
            try:
                corrected_raw = invoke(correction_prompt)
            except (OSError, RuntimeError, ValueError, ContractError) as exc:
                _persist_boundary_error(
                    output,
                    provider,
                    model,
                    _provider_error_reason(exc, "correction_provider_error"),
                )
                return value if contract_error is None else None
        corrected, corrected_contract_error, corrected_semantic_errors = evaluate(
            corrected_raw
        )
        corrected_errors = (
            (
                (f"contract: {corrected_contract_error}",)
                if corrected_contract_error
                else ()
            )
            + corrected_semantic_errors
        )
        if corrected_errors:
            _atomic_json(
                correction_invalid_output,
                {
                    "meta": {
                        "provider": provider,
                        "model": model,
                        "cache_key": correction_key,
                    "prompt_sha256": quality.digest(correction_prompt),
                        "corrected_from": key,
                    },
                    "validation_errors": list(corrected_errors),
                    "result": corrected,
                },
            )
            _persist_boundary_error(
                output,
                provider,
                model,
                "correction_invalid:" + ";".join(corrected_errors),
            )
            return value if contract_error is None else None

        _atomic_json(
            correction_output,
            {
                "meta": {
                    "provider": provider,
                    "model": model,
                    "cache_key": correction_key,
                    "prompt_sha256": quality.digest(correction_prompt),
                    "corrected_from": key,
                },
                "result": corrected,
            },
        )
        _boundary_error_path(output).unlink(missing_ok=True)
        return corrected

    @staticmethod
    def _verification_pair(result: Mapping[str, Any], provider: str) -> PairVerification:
        quick_answer = result.get("candidate_role") == SolutionRole.QUICK_ANSWER.value
        required_matches = (
            "anchor_segment_match",
            "parent_number_match",
            "monotonic_order_match",
        )
        optional_matches = (
            "subquestion_match",
            "range_or_group_match",
            *(("hierarchy_scope_match", "fingerprint_match") if quick_answer else ()),
        )
        detailed_required = () if quick_answer else (
            "hierarchy_scope_match",
            "fingerprint_match",
        )
        same = (
            result.get("decision") == "verified"
            and result.get("same_question") == "yes"
            and not result.get("explicit_answer_conflict")
            and result.get("candidate_crop_complete") is True
            and result.get("target_identifier_visible") is True
            and result.get("first_line_complete") is True
            and result.get("last_line_complete") is True
            and result.get("neighbor_entry_absent") is True
            and result.get("next_question_absent") is True
            and (
                result.get("target_answer_visible") == "yes"
                if quick_answer
                else result.get("target_answer_visible") in {"yes", "not_applicable"}
            )
            and all(result.get(field) == "match" for field in required_matches)
            and all(result.get(field) == "match" for field in detailed_required)
            and all(result.get(field) in {"match", "not_applicable"} for field in optional_matches)
        )
        return PairVerification(
            same,
            float(result.get("confidence", 0)),
            provider,
            bool(
                result.get("explicit_answer_conflict")
                or result.get("decision") == "conflict"
            ),
        )

    @staticmethod
    def _verification_outcome(result: Mapping[str, Any] | None) -> str:
        if result is None:
            return "missing"
        if result.get("decision") == "wrong_link" or result.get("same_question") == "no":
            return "no"
        if result.get("decision") == "conflict" or result.get("explicit_answer_conflict"):
            return "conflict"
        if result.get("decision") == "verified" and result.get("same_question") == "yes":
            return "yes"
        return "review"

    @staticmethod
    def _combine_verifications(
        gemini: Mapping[str, Any] | None,
        qwen: Mapping[str, Any] | None,
    ) -> PairVerification | None:
        gemini_outcome = SolutionRuntimeAdapter._verification_outcome(gemini)
        qwen_outcome = SolutionRuntimeAdapter._verification_outcome(qwen)
        if gemini_outcome == qwen_outcome == "conflict":
            if gemini is None or qwen is None:
                return None
            gemini_pair = SolutionRuntimeAdapter._verification_pair(gemini, "gemini")
            qwen_pair = SolutionRuntimeAdapter._verification_pair(qwen, "qwen")
            return PairVerification(
                False,
                min(gemini_pair.confidence, qwen_pair.confidence),
                DUAL_VERIFIER,
                True,
            )
        if "conflict" in {gemini_outcome, qwen_outcome}:
            return None
        if {gemini_outcome, qwen_outcome} == {"yes", "no"}:
            return None
        if gemini is None or qwen is None:
            return None
        gemini_pair = SolutionRuntimeAdapter._verification_pair(gemini, "gemini")
        qwen_pair = SolutionRuntimeAdapter._verification_pair(qwen, "qwen")
        if gemini_pair.same_item and qwen_pair.same_item:
            return PairVerification(
                True,
                min(gemini_pair.confidence, qwen_pair.confidence),
                DUAL_VERIFIER,
            )
        if not gemini_pair.same_item and not qwen_pair.same_item:
            return PairVerification(
                False,
                min(gemini_pair.confidence, qwen_pair.confidence),
                DUAL_VERIFIER,
            )
        return None

    @staticmethod
    def _verification_identity_matches(
        result: Mapping[str, Any] | None, role: SolutionRole
    ) -> bool:
        if (
            result is None
            or result.get("same_question") != "yes"
            or result.get("decision") in {"wrong_link", "conflict"}
            or result.get("explicit_answer_conflict")
        ):
            return False
        quick_answer = role == SolutionRole.QUICK_ANSWER
        required = (
            "anchor_segment_match",
            "parent_number_match",
            "monotonic_order_match",
            *(() if quick_answer else ("hierarchy_scope_match", "fingerprint_match")),
        )
        optional = (
            "subquestion_match",
            "range_or_group_match",
            *(("hierarchy_scope_match", "fingerprint_match") if quick_answer else ()),
        )
        return all(result.get(field) == "match" for field in required) and all(
            result.get(field) in {"match", "not_applicable"} for field in optional
        )

    @staticmethod
    def _verification_crop_complete(
        result: Mapping[str, Any], role: SolutionRole
    ) -> bool:
        return (
            result.get("candidate_crop_complete") is True
            and result.get("target_identifier_visible") is True
            and result.get("first_line_complete") is True
            and result.get("last_line_complete") is True
            and result.get("neighbor_entry_absent") is True
            and result.get("next_question_absent") is True
            and (
                result.get("target_answer_visible") == "yes"
                if role == SolutionRole.QUICK_ANSWER
                else result.get("target_answer_visible") in {"yes", "not_applicable"}
            )
        )

    @classmethod
    def _crop_repair_eligible(
        cls,
        gemini: Mapping[str, Any] | None,
        qwen: Mapping[str, Any] | None,
        role: SolutionRole,
    ) -> bool:
        def boundary_only_uncertain(result: Mapping[str, Any] | None) -> bool:
            return bool(
                result is not None
                and result.get("decision") == "review"
                and result.get("same_question") == "uncertain"
                and not result.get("explicit_answer_conflict")
                and result.get("candidate_crop_complete") is False
                and any(
                    result.get(field) is False
                    for field in (
                        "target_identifier_visible",
                        "first_line_complete",
                        "last_line_complete",
                    )
                )
            )

        if qwen is None:
            return bool(gemini is not None and (cls._verification_identity_matches(gemini, role) or boundary_only_uncertain(gemini)) and not cls._verification_crop_complete(gemini, role))
        gemini_identity = cls._verification_identity_matches(gemini, role)
        qwen_identity = cls._verification_identity_matches(qwen, role)
        return (
            (
                gemini_identity
                and (qwen_identity or boundary_only_uncertain(qwen))
                or qwen_identity
                and (gemini_identity or boundary_only_uncertain(gemini))
            )
            and not (
                cls._verification_crop_complete(gemini, role)
                and cls._verification_crop_complete(qwen, role)
            )
        )

    @classmethod
    def _valid_verification_result(
        cls,
        result: dict[str, Any] | None,
        problem: ProblemItem,
        asset: SolutionAsset,
    ) -> dict[str, Any] | None:
        if result is not None and (
            result.get("question_id") != problem.item_id
            or asset.asset_id not in result.get("candidate_ids", [])
            or result.get("candidate_role") != asset.role.value
        ):
            return None
        if result is not None and cls._verification_semantic_errors(
            result, problem, asset
        ):
            return None
        return result

    def _bind_quality(self, context: LinkContext, state: RuntimeState,
                      problem: ProblemItem, asset: SolutionAsset, images: list[Path],
                      output: Path, template: Path, schema: Path, result: JsonMap,
                      verification: PairVerification,
                      corroborating: SolutionAsset | None = None,
                      *, rendered_asset: SolutionAsset | None = None) -> PairVerification:
        receipt = None
        reason = "quality_runtime_provenance_missing"
        try:
            receipt = issue_pair(context, problem, asset,
                rendered_asset or output_asset_with_context(asset, tuple(state.assets_by_id.values())),
                images, output, template, schema, result, corroborating)
        except astra_cli.AstraError as exc:
            reason = exc.code
        except (OSError, ValueError, TypeError, KeyError):
            reason = "quality_runtime_provenance_missing"
        _atomic_json(quality.pointer(context.run_dir, problem.item_id, asset.asset_id),
                     receipt or {"status": "review", "reason": reason})
        if receipt is not None:
            reason = quality.validate_receipt(receipt, context.run_dir,
                problem=problem.signature(), asset=asset.signature(),
                conflict=verification.explicit_answer_conflict,
                corroborating=corroborating.signature() if corroborating is not None else None)
        return replace(verification, quality_receipt=receipt, evidence_root=context.run_dir,
                       quality_reason=reason,
                       verifier="astra-current-evidence" if reason is None else verification.verifier)

    def _verify_exact_asset(
        self,
        context: LinkContext,
        state: RuntimeState,
        problem: ProblemItem,
        asset: SolutionAsset,
        pair: CandidatePair,
        schema: dict[str, Any],
        envelope: dict[str, Any],
        template: str,
        *,
        tag: str = "",
        corroborating: SolutionAsset | None = None,
        cache_only: bool = False,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, PairVerification | None]:
        _atomic_json(quality.pointer(context.run_dir, problem.item_id, asset.asset_id),
                     {"status": "review", "reason": "quality_verification_started"})
        source_context = None
        try:
            images = self._verification_images(
                context, state, problem, asset, corroborating
            )
            if problem.anchor is None and asset.expanded_source_binding is not None:
                if corroborating is None or not expanded_candidate_pairs((problem,), (asset,),
                        problem_source_sha256=_digest_file(self._source_pdf(state)),
                        solution_source_sha256=_digest_file(context.solution_path)):
                    raise ValueError('anchorless current source corroboration missing')
                supplied = state.scope_input_contexts[(problem.item_id, corroborating.asset_id)]
                binding = supplied['binding']
                if (binding['problem_id'] != problem.item_id or binding['asset_id'] != corroborating.asset_id
                        or _digest_file(Path(binding['snapshot_path'])) != binding['snapshot_sha256']
                        or binding['problem_source_sha256'] != _digest_file(self._source_pdf(state))
                        or binding['solution_source_sha256'] != _digest_file(context.solution_path)):
                    raise ValueError('source context changed')
                solution_scope_mod.read_scope_index(Path(binding['snapshot_path']), expected_solution_sha256=binding['solution_source_sha256'], expected_provider_profile=self.provider_profile, solutions_dir=context.run_dir/'solutions')
                if any(_digest_file(Path(entry['path'])) != entry['sha256'] for entry in binding['hint_files']):
                    raise ValueError('source context hint changed')
                covered = {record['pdf_page'] for record in supplied['source_context_images'] if record['role']=='CANDIDATE solution page'}
                if not all(part.page.pdf_page in covered for part in asset.parts):
                    raise ValueError('candidate source page context missing')
                for record in supplied['source_context_images']:
                    image = inside(Path(record['path']).resolve(), 'source context image')
                    if _digest_file(image) != record['sha256']:
                        raise ValueError('source context image changed')
                    images.append(image)
                source_context = {key: supplied[key] for key in ('problem','episode','ordered_context')}
                source_context['source_context_images'] = [{key:value for key,value in record.items() if key!='path'} for record in supplied['source_context_images']]
        except (OSError, ValueError, KeyError):
            return None, None, None
        order_context = _ordered_neighbor_context(
            problem,
            asset,
            state.problem_index.items if state.problem_index is not None else (problem,),
            tuple(state.assets_by_id.values()),
            _digest_file(self._source_pdf(state)),
            _digest_file(context.solution_path),
        )
        prompt = _verification_prompt(
            template,
            problem,
            asset,
            pair,
            corroborating,
            source_context,
            order_context,
        )
        learning = self._learning_prompt(context, state, problem, asset)
        if (
            learning is not None
            and learning.attachment is not None
            and learning.cache_identity is not None
        ):
            prompt += (
                "\nLEARNING_CACHE_IDENTITY="
                + learning.cache_identity
                + "\n"
                + learning.attachment
            )
        try:
            target = target_for(context.run_dir, problem, asset,
                output_asset_with_context(asset, tuple(state.assets_by_id.values())), corroborating,
                solution_path=context.solution_path, images=images)
            prompt += "\nCURRENT_EVIDENCE_TARGET_SHA256=" + quality.identity(target)
        except (astra_cli.AstraError, OSError, ValueError, KeyError):
            target = None
        directory = context.run_dir / "solutions" / "detections" / "verifications"
        suffix = "" if not tag else "-" + re.sub(r"[^0-9A-Za-z_-]+", "-", tag)
        stem = f"{problem.item_id}-{asset.asset_id}{suffix}"
        gemini = self._provider_result(
            context,
            "astra",
            prompt,
            envelope,
            images,
            directory / f"{stem}-astra.json",
            problem=problem,
            asset=asset,
            order_context=order_context,
            cache_only=cache_only,
        )
        gemini = self._valid_verification_result(gemini, problem, asset)
        qwen = None
        verification = self._verification_pair(gemini, "astra-quality-pending") if gemini is not None else None
        if (
            verification is not None
            and verification.explicit_answer_conflict
            and corroborating is not None
        ):
            verification = replace(
                verification, corroborating_asset_id=corroborating.asset_id
            )
        if verification is not None:
            verification = self._bind_quality(context, state, problem, asset, images,
                directory / f"{stem}-astra.json", VERIFY_PROMPT, VERIFY_SCHEMA,
                gemini, verification, corroborating)
        learning_revoked = (
            verification is not None
            and learning is not None
            and self.learning_runtime is not None
            and not self.learning_runtime.approval_is_current(learning)
        )
        if learning_revoked:
            assert learning is not None
            assert self.learning_runtime is not None
            reasons = self.learning_runtime.approval_reasons(learning)
            state.recovery_trace.append({
                "stage": "solution-verify",
                "code": "learning_reference_revoked",
                "problem_id": problem.item_id,
                "asset_id": asset.asset_id,
                "reasons": list(reasons),
            })
            _atomic_json(
                quality.pointer(context.run_dir, problem.item_id, asset.asset_id),
                {"status": "review", "reason": reasons[0] if reasons else "learning_reference_revoked"},
            )
            verification = None
        elif verification is None:
            _atomic_json(quality.pointer(context.run_dir, problem.item_id, asset.asset_id),
                         {"status": "review", "reason": "quality_verification_unavailable"})
        return gemini, qwen, verification

    def _repair_attachments(
        self,
        context: LinkContext,
        state: RuntimeState,
        problem: ProblemItem,
        asset: SolutionAsset,
    ) -> tuple[list[Path], str]:
        if (
            asset.asset_id in state.recovery_asset_ids
            and asset.asset_id not in state.approved_recovery_asset_ids
        ):
            item = state.manifest_items_by_id[problem.item_id]
            source_crop = context.run_dir / str(item["output_file"])
            directory = (
                context.run_dir
                / "solutions"
                / "detections"
                / "targeted-repairs"
                / "provisional-images"
            )
            inside(directory.resolve(), "output")
            directory.mkdir(parents=True, exist_ok=True)
            safe = re.sub(
                r"[^0-9A-Za-z가-힣_-]+",
                "-",
                problem.item_id + "-" + asset.asset_id,
            )
            problem_image = directory / f"{safe}-problem.png"
            provisional_image = directory / f"{safe}-provisional.png"
            if context.force or not problem_image.exists():
                shutil.copy2(source_crop, problem_image)
            if context.force or not provisional_image.exists():
                with self.pdf_lock:
                    self.asset_renderer(
                        context.solution_path,
                        asset,
                        provisional_image,
                        context.crop_dpi,
                    )
            images = [problem_image, provisional_image]
        else:
            images = self._verification_images(context, state, problem, asset, None)
        labels = ["1. PROBLEM crop", "2. FAILED final-DPI solution crop"]
        seen = {str(path.resolve()) for path in images}
        text_by_page = {page.pdf_index: page.text for page in state.solution_texts}
        total_pages = max(
            (page.pdf_index for page in state.solution_page_map), default=-1
        ) + 1
        render_root = (
            context.run_dir
            / "solutions"
            / "render"
            / _digest_file(context.solution_path)[:12]
        )
        for page_index in sorted({part.page.pdf_index for part in asset.parts}):
            page_images, _attachment_map = self._attachments(
                context.solution_path,
                render_root,
                page_index,
                total_pages,
                text_by_page.get(page_index, ""),
                None,
            )
            for page_image in page_images:
                key = str(page_image.resolve())
                if key in seen:
                    continue
                seen.add(key)
                images.append(page_image)
                labels.append(
                    f"{len(labels) + 1}. SOLUTION page view around pdf index {page_index} ({page_image.name})"
                )
        return images, "\n".join(labels)

    def _repair_proposal(self, context: LinkContext, provider: str, prompt: str, images: list[Path], output: Path, *, cache_only: bool = False) -> dict[str, Any] | None:
        if provider != "codex":
            return None
        if cache_only:
            if context.force:
                return None
            try:
                wrapper = json.loads(output.read_text(encoding='utf-8'))
                keys = _contract_cache_keys('targeted-solution-repair-codex', prompt, REPAIR_SCHEMA, images, astra_cli.MODEL)
                if wrapper.get('meta', {}).get('cache_key') not in keys:
                    return None
                validate_contract(wrapper['result'], json.loads(REPAIR_SCHEMA.read_text(encoding='utf-8')))
                return wrapper['result']
            except (OSError, KeyError, ValueError, ContractError):
                return None
        return self.codex_runner(stage="targeted-solution-repair-codex", prompt=prompt, schema_path=REPAIR_SCHEMA, images=images, output=output, run_dir=context.run_dir, model=astra_cli.MODEL, force=context.force)

    @staticmethod
    def _normalize_repair_coordinates(
        proposals: Sequence[tuple[str, Mapping[str, Any]]],
    ) -> list[tuple[str, Mapping[str, Any]]]:
        codex = [proposal for provider, proposal in proposals if provider == "codex"]
        if len(codex) != 1:
            return list(proposals)
        anchor = codex[0]
        normalized: list[tuple[str, Mapping[str, Any]]] = [("codex", anchor)]
        for provider, proposal in sorted(
            (
                (provider, proposal)
                for provider, proposal in proposals
                if provider != "codex"
            ),
            key=lambda value: value[0],
        ):
            transposed = copy.deepcopy(dict(proposal))
            for part in transposed.get("parts", []):
                if isinstance(part, dict):
                    part["bbox"] = _transpose_bbox(part.get("bbox"))

            def alignment(value: Mapping[str, Any]) -> float:
                return sum(
                    _bbox_pair(part.get("bbox"), peer_part.get("bbox"))[0]
                    for part in value.get("parts", [])
                    if isinstance(part, dict) and _valid_bbox(part.get("bbox"))
                    for peer_part in anchor.get("parts", [])
                    if isinstance(peer_part, dict)
                    and part.get("solution_pdf_index")
                    == peer_part.get("solution_pdf_index")
                    and part.get("order") == peer_part.get("order")
                )

            identity_score = alignment(proposal)
            transposed_score = alignment(transposed)
            normalized.append(
                (provider, transposed)
                if transposed_score > identity_score + 0.08
                else (provider, proposal)
            )
        return normalized

    @staticmethod
    def _repair_asset_from_proposal(
        state: RuntimeState,
        problem: ProblemItem,
        asset: SolutionAsset,
        proposal: Mapping[str, Any],
    ) -> SolutionAsset | None:
        if (
            proposal.get("question_id") != problem.item_id
            or proposal.get("asset_id") != asset.asset_id
            or proposal.get("candidate_role") != asset.role.value
            or proposal.get("repairable") is not True
            or not proposal.get("parts")
        ):
            return None
        page_by_index = {page.pdf_index: page for page in state.solution_page_map}
        original_pages = {part.page.pdf_index for part in asset.parts}
        for part in asset.parts:
            current = page_by_index.get(part.page.pdf_index)
            if current is None or current.segment_id != part.page.segment_id or current.printed_page not in {None, part.page.printed_page}:
                return None
            page_by_index[part.page.pdf_index] = part.page
        allowed_pages = set(original_pages)
        if asset.role != SolutionRole.QUICK_ANSWER:
            allowed_pages.update(
                index
                for page_index in original_pages
                for index in (page_index - 1, page_index + 1)
                if index in page_by_index
                and page_by_index[index].segment_id == asset.segment_id
            )
        raw_parts = sorted(proposal.get("parts", []), key=lambda part: part.get("order", -1))
        if len({part.get("order") for part in raw_parts}) != len(raw_parts):
            return None
        if asset.role == SolutionRole.QUICK_ANSWER and (
                not 1 <= len(raw_parts) <= 4
                or [part.get('order') for part in raw_parts] != list(range(len(raw_parts)))):
            return None
        parts: list[SolutionPart] = []
        for raw_part in raw_parts:
            page_index = raw_part.get("solution_pdf_index")
            bbox = raw_part.get("bbox")
            if page_index not in allowed_pages or not _valid_bbox(bbox):
                return None
            parts.append(
                SolutionPart(
                    page_by_index[page_index],
                    tuple(bbox),
                    int(raw_part["order"]),
                )
            )
        try:
            confidence = min(asset.confidence, float(proposal.get("confidence", 0)))
            candidate = replace(asset, parts=tuple(parts), confidence=confidence)
            if asset.role == SolutionRole.QUICK_ANSWER and any(
                    left.page == right.page and min(left.bbox[2],right.bbox[2]) > max(left.bbox[0],right.bbox[0])
                    and min(left.bbox[3],right.bbox[3]) > max(left.bbox[1],right.bbox[1])
                    for index,left in enumerate(parts) for right in parts[index+1:]):
                return None
            if asset.expanded_source_binding is not None:
                if state.problem_index is None or state.solution_index is None or not expanded_candidate_pairs(
                        (problem,), (asset,), problem_source_sha256=state.problem_index.source_sha256,
                        solution_source_sha256=state.solution_index.source_sha256):
                    return None
                candidate = replace(candidate, expanded_source_binding=replace(asset.expanded_source_binding,
                    asset_signature_sha256=quality.identity(replace(candidate, expanded_source_binding=None).signature())))
            return candidate
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _asset_area(asset: SolutionAsset, all_assets: Sequence[SolutionAsset]) -> int:
        rendered = output_asset_with_context(asset, all_assets)
        return sum(
            (part.bbox[2] - part.bbox[0]) * (part.bbox[3] - part.bbox[1])
            for part in rendered.parts
        )

    @staticmethod
    def _quick_axis_hybrid_candidates(
        candidates: Sequence[tuple[str, SolutionAsset]],
    ) -> list[tuple[str, SolutionAsset]]:
        if len(candidates) != 2:
            return []
        (left_name, left), (right_name, right) = candidates
        if (
            left.role != SolutionRole.QUICK_ANSWER
            or right.role != SolutionRole.QUICK_ANSWER
            or len(left.parts) != 1
            or len(right.parts) != 1
            or left.parts[0].page.pdf_index != right.parts[0].page.pdf_index
        ):
            return []
        left_box = left.parts[0].bbox
        right_box = right.parts[0].bbox
        raw = (
            (
                f"hybrid-{left_name}-x-{right_name}-y",
                (left_box[0], right_box[1], left_box[2], right_box[3]),
            ),
            (
                f"hybrid-{right_name}-x-{left_name}-y",
                (right_box[0], left_box[1], right_box[2], left_box[3]),
            ),
        )
        existing = {left_box, right_box}
        hybrids: list[tuple[str, SolutionAsset]] = []
        for name, bbox in raw:
            if bbox in existing or not _valid_bbox(list(bbox)):
                continue
            existing.add(bbox)
            hybrids.append(
                (
                    name,
                    replace(
                        left,
                        parts=(SolutionPart(left.parts[0].page, bbox, 0),),
                        confidence=min(left.confidence, right.confidence),
                    ),
                )
            )
        return hybrids

    @staticmethod
    def _quick_vertical_shift_candidates(
        original: SolutionAsset,
        *,
        widen_left: bool = False,
        widen_right: bool = False,
    ) -> list[tuple[str, SolutionAsset]]:
        if (
            original.role != SolutionRole.QUICK_ANSWER
            or len(original.parts) != 1
            or (widen_left and widen_right)
        ):
            return []
        part = original.parts[0]
        x0, y0, x1, y1 = part.bbox
        height = y1 - y0
        if height <= 0:
            return []
        shifted: list[tuple[str, SolutionAsset]] = []
        suffix = "-wide-left" if widen_left else "-wide-right" if widen_right else ""
        for direction, sign in (("up", -1), ("down", 1)):
            for rows in (1, 2):
                bbox = (
                    x0 - height if widen_left else x0,
                    y0 + sign * height * rows,
                    x1 + height if widen_right else x1,
                    y1 + sign * height * rows,
                )
                if not _valid_bbox(list(bbox)):
                    continue
                shifted.append(
                    (
                        f"row-shift-{direction}-{rows}{suffix}",
                        replace(
                            original,
                            parts=(SolutionPart(part.page, bbox, part.order),),
                        ),
                    )
                )
        return shifted

    @staticmethod
    def _detail_edge_candidates(
        original: SolutionAsset,
        candidates: Sequence[tuple[str, SolutionAsset]],
    ) -> list[tuple[str, SolutionAsset]]:
        if (
            original.role != SolutionRole.DETAILED_SOLUTION
            or len(original.parts) != 1
            or not candidates
            or any(not candidate.parts for _name, candidate in candidates)
            or any(
                candidate.parts[0].page.pdf_index
                != original.parts[0].page.pdf_index
                for _name, candidate in candidates
            )
        ):
            return []
        original_box = original.parts[0].bbox
        existing = {original_box} | {
            candidate.parts[0].bbox for _name, candidate in candidates
        }
        results: list[tuple[str, SolutionAsset]] = []
        for name, candidate in candidates:
            box = candidate.parts[0].bbox
            if len(candidate.parts) > 1:
                for numerator, denominator in ((3, 4), (7, 8)):
                    bbox = (
                        min(box[0], original_box[0]),
                        (
                            box[1] * (denominator - numerator)
                            + original_box[1] * numerator
                        )
                        // denominator,
                        max(box[2], original_box[2]),
                        max(box[3], original_box[3]),
                    )
                    if bbox in existing or not _valid_bbox(list(bbox)):
                        continue
                    existing.add(bbox)
                    results.append(
                        (
                            f"edge-{name}-continuation-top-{numerator}of{denominator}",
                            replace(
                                candidate,
                                parts=(
                                    SolutionPart(
                                        candidate.parts[0].page,
                                        bbox,
                                        candidate.parts[0].order,
                                    ),
                                )
                                + candidate.parts[1:],
                                confidence=min(
                                    original.confidence, candidate.confidence
                                ),
                            ),
                        )
                    )
                continue
            options = (
                (
                    f"edge-{name}-top-original-bottom",
                    (box[0], box[1], box[2], original_box[3]),
                ),
                (
                    f"edge-original-top-{name}-bottom",
                    (box[0], original_box[1], box[2], box[3]),
                ),
                (
                    f"edge-{name}-mid-bottom",
                    (
                        box[0],
                        box[1],
                        box[2],
                        (box[3] + original_box[3]) // 2,
                    ),
                ),
                (
                    f"edge-{name}-mid-top",
                    (
                        box[0],
                        (box[1] + original_box[1]) // 2,
                        box[2],
                        box[3],
                    ),
                ),
            )
            for candidate_name, bbox in options:
                if bbox in existing or not _valid_bbox(list(bbox)):
                    continue
                existing.add(bbox)
                results.append(
                    (
                        candidate_name,
                        replace(
                            candidate,
                            parts=(
                                SolutionPart(candidate.parts[0].page, bbox, 0),
                            ),
                            confidence=min(original.confidence, candidate.confidence),
                        ),
                    )
                )
        return results

    def _targeted_crop_repair(
        self,
        context: LinkContext,
        state: RuntimeState,
        problem: ProblemItem,
        asset: SolutionAsset,
        pair: CandidatePair,
        gemini_failure: Mapping[str, Any],
        qwen_failure: Mapping[str, Any],
        schema: dict[str, Any],
        envelope: dict[str, Any],
        verification_template: str,
        corroborating: SolutionAsset | None = None,
        cache_only: bool = False,
    ) -> tuple[SolutionAsset, PairVerification] | None:
        try:
            images, attachment_map = self._repair_attachments(
                context, state, problem, asset
            )
        except (OSError, ValueError):
            return None
        crop_fields = (
            "decision",
            "same_question",
            "candidate_crop_complete",
            "target_identifier_visible",
            "target_answer_visible",
            "first_line_complete",
            "last_line_complete",
            "neighbor_entry_absent",
            "next_question_absent",
            "review_reasons",
        )
        failure_evidence = {
            provider: {field: result.get(field) for field in crop_fields}
            for provider, result in (
                ("astra", gemini_failure),
            )
        }
        prompt = Template(REPAIR_PROMPT.read_text(encoding="utf-8")).safe_substitute(
            QUESTION_METADATA=json.dumps(problem.signature(), ensure_ascii=False),
            FAILED_CANDIDATE=json.dumps(asset.signature(), ensure_ascii=False),
            FAILURE_EVIDENCE=json.dumps(failure_evidence, ensure_ascii=False),
            ATTACHMENT_MAP=attachment_map,
        )
        safe = re.sub(
            r"[^0-9A-Za-z가-힣_-]+", "-", problem.item_id + "-" + asset.asset_id
        )
        root = (
            context.run_dir
            / "solutions"
            / "detections"
            / "targeted-repairs"
            / "raw"
        )
        state.recovery_trace.append({'stage': 'targeted-repair', 'code': 'repair_proposal_request',
            'problem_id': problem.item_id, 'asset_id': asset.asset_id})
        observation = root/'codex'/f'{safe}-{quality.identity(asset.signature())[:12]}.input.json'
        if not cache_only and not observation.exists():
            _atomic_json(observation, {'problem':problem.signature(),'asset':asset.signature(),'failure':dict(gemini_failure)})
        value = self._repair_proposal(context, "codex", prompt, images, root / "codex" / f"{safe}.json", cache_only=cache_only)
        proposals = [("codex", value)] if value is not None else []
        if not proposals:
            state.recovery_trace.append({'stage': 'targeted-repair', 'code': 'repair_proposal_unavailable',
                'problem_id': problem.item_id, 'asset_id': asset.asset_id})
            return None
        proposals = self._normalize_repair_coordinates(proposals)
        candidates: list[tuple[str, SolutionAsset]] = []
        geometry_index: dict[
            tuple[tuple[int, tuple[int, int, int, int], int], ...], int
        ] = {}
        for provider, proposal in proposals:
            candidate = self._repair_asset_from_proposal(
                state, problem, asset, proposal
            )
            if candidate is None:
                state.recovery_trace.append({'stage': 'targeted-repair', 'code': 'repair_proposal_rejected',
                    'problem_id': problem.item_id, 'asset_id': asset.asset_id, 'candidate': provider})
                continue
            geometry = tuple(
                (part.page.pdf_index, part.bbox, part.order)
                for part in candidate.parts
            )
            if geometry in geometry_index:
                index = geometry_index[geometry]
                if candidate.confidence > candidates[index][1].confidence:
                    candidates[index] = (provider, candidate)
                continue
            geometry_index[geometry] = len(candidates)
            candidates.append((provider, candidate))
        if not candidates:
            return None
        passed: list[tuple[int, str, SolutionAsset, PairVerification]] = []
        all_assets = tuple(state.assets_by_id.values())
        verified_geometry: set[
            tuple[tuple[int, tuple[int, int, int, int], int], ...]
        ] = set()

        def verify_candidates(values: Sequence[tuple[str, SolutionAsset]]) -> None:
            for provider, candidate in sorted(values, key=lambda value: (
                self._asset_area(value[1], all_assets), value[0]
            )):
                exact_geometry = tuple(
                    (part.page.pdf_index, part.bbox, part.order)
                    for part in candidate.parts
                )
                trace = {'stage': 'targeted-repair', 'problem_id': problem.item_id,
                         'asset_id': candidate.asset_id, 'candidate': provider,
                         'geometry': exact_geometry}
                state.recovery_trace.append(trace)
                if passed:
                    trace['code'] = 'repair_candidate_skipped_after_pass'
                    continue
                if exact_geometry in verified_geometry:
                    trace['code'] = 'repair_duplicate_geometry'
                    continue
                verified_geometry.add(exact_geometry)
                geometry = hashlib.sha256(
                    json.dumps(candidate.signature(), sort_keys=True).encode("utf-8")
                ).hexdigest()[:10]
                _gemini, _qwen, verification = self._verify_exact_asset(
                    context,
                    state,
                    problem,
                    candidate,
                    pair,
                    schema,
                    envelope,
                    verification_template,
                    tag=f"repair-{provider}-{geometry}",
                    corroborating=corroborating,
                    cache_only=cache_only,
                )
                trace['code'] = 'repair_candidate_nonpass' if verification is not None else 'repair_verification_unavailable'
                if _confirmed_verification(verification, problem, candidate, corroborating=corroborating):
                    trace['code'] = 'repair_candidate_pass'
                    passed.append(
                        (
                            self._asset_area(candidate, all_assets),
                            provider,
                            candidate,
                            verification,
                        )
                    )

        if (
            asset.role == SolutionRole.QUICK_ANSWER
            and asset.asset_id in state.recovery_asset_ids
        ):
            verify_candidates(self._quick_vertical_shift_candidates(asset))
            if not passed:
                verify_candidates(
                    self._quick_vertical_shift_candidates(asset, widen_left=True)
                )
            if not passed:
                verify_candidates(
                    self._quick_vertical_shift_candidates(asset, widen_right=True)
                )
            if passed:
                _area, _provider, best_asset, best_verification = min(
                    passed, key=lambda value: (value[0], value[1])
                )
                if best_verification.quality_receipt is not None:
                    _atomic_json(quality.pointer(context.run_dir, problem.item_id, best_asset.asset_id),
                                 best_verification.quality_receipt)
                return best_asset, best_verification

        verify_candidates(candidates)
        if not passed and asset.role == SolutionRole.QUICK_ANSWER:
            verify_candidates(self._quick_axis_hybrid_candidates(candidates))
            if not passed and asset.asset_id not in state.recovery_asset_ids:
                verify_candidates(self._quick_vertical_shift_candidates(asset))
            if not passed and asset.asset_id not in state.recovery_asset_ids:
                verify_candidates(
                    self._quick_vertical_shift_candidates(asset, widen_left=True)
                )
            if not passed and asset.asset_id not in state.recovery_asset_ids:
                verify_candidates(
                    self._quick_vertical_shift_candidates(asset, widen_right=True)
                )
        elif not passed and asset.role == SolutionRole.DETAILED_SOLUTION:
            verify_candidates(self._detail_edge_candidates(asset, candidates))
        if not passed:
            return None
        _area, _provider, best_asset, best_verification = min(
            passed, key=lambda value: (value[0], value[1])
        )
        if best_verification.quality_receipt is not None:
            _atomic_json(quality.pointer(context.run_dir, problem.item_id, best_asset.asset_id),
                         best_verification.quality_receipt)
        return best_asset, best_verification

    def verify_candidate_pairs(
        self, context: LinkContext, pairs: tuple[CandidatePair, ...]
    ) -> Mapping[tuple[str, str], PairVerification]:
        state = self._state(context)
        cache_only = state.prior_link_present and not context.force
        if context.force and state.verification_retry_context is not context:
            state.verification_blocked_reason = None
            state.verification_retry_context = context
        if cache_only and state.solution_index is None:
            return {}
        self.detect_solution_index(context)
        schema = json.loads(VERIFY_SCHEMA.read_text(encoding="utf-8"))
        envelope = _verification_envelope(schema)
        template = VERIFY_PROMPT.read_text(encoding="utf-8")
        results: dict[tuple[str, str], PairVerification] = {}
        detailed_pairs: dict[str, list[CandidatePair]] = {}
        for pair in pairs:
            candidate = state.assets_by_id.get(pair.asset_id)
            if (
                candidate is not None
                and candidate.role == SolutionRole.DETAILED_SOLUTION
            ):
                detailed_pairs.setdefault(pair.problem_id, []).append(pair)
        ordered_pairs = tuple(
            sorted(
                pairs,
                key=lambda pair: (
                    state.assets_by_id.get(pair.asset_id) is not None
                    and state.assets_by_id[pair.asset_id].role
                    == SolutionRole.QUICK_ANSWER
                ),
            )
        )
        allow_new_corroboration = context.force or not state.prior_link_present
        for pair in ordered_pairs:
            problem = state.problem_items_by_id.get(pair.problem_id)
            asset = state.assets_by_id.get(pair.asset_id)
            if problem is None or asset is None:
                continue
            if not context.force and any(
                owner == problem.item_id
                and (selected := state.assets_by_id.get(selected_id)) is not None
                and selected.role == asset.role
                and _strong_verification(verified, MIN_LINK_CONFIDENCE, problem, selected)
                for (owner, selected_id), verified in results.items()
            ):
                continue
            confirmed_details = [
                state.assets_by_id[detail_pair.asset_id]
                for detail_pair in detailed_pairs.get(problem.item_id, [])
                if (
                    (detail_verification := results.get(
                        (problem.item_id, detail_pair.asset_id)
                    ))
                    is not None
                    and detail_verification.same_item
                    and not detail_verification.explicit_answer_conflict
                    and _confirmed_verification(detail_verification, problem, state.assets_by_id[detail_pair.asset_id])
                    and detail_verification.confidence >= MIN_LINK_CONFIDENCE
                )
            ]
            if asset.role == SolutionRole.QUICK_ANSWER and not confirmed_details:
                for (owner_id, detail_id), source_context in state.scope_input_contexts.items():
                    detail = state.assets_by_id.get(detail_id)
                    if (owner_id != problem.item_id or detail is None or detail.role != SolutionRole.DETAILED_SOLUTION
                            or detail.parent_number != problem.parent_number or detail.subquestion_label != problem.subquestion_label):
                        continue
                    try:
                        receipt = json.loads(quality.pointer(context.run_dir, owner_id, detail_id).read_text(encoding='utf-8'))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if quality.validate_receipt(receipt, context.run_dir, problem=problem.signature(), asset=detail.signature()) is None:
                        confirmed_details.append(detail)
            corroborating = (
                confirmed_details[0]
                if asset.role == SolutionRole.QUICK_ANSWER
                and allow_new_corroboration
                and len(confirmed_details) == 1
                else None
            )
            if not context.force and corroborating is not None:
                try:
                    receipt = json.loads(quality.pointer(context.run_dir, problem.item_id, asset.asset_id).read_text(encoding='utf-8'))
                    previous_asset = receipt.get('target', {}).get('asset')
                    if (previous_asset != asset.signature() and isinstance(previous_asset, dict)
                            and previous_asset.get('asset_id') == asset.asset_id
                            and quality.validate_receipt(receipt, context.run_dir, problem=problem.signature(), asset=previous_asset) is None):
                        failed_path = context.run_dir/'solutions/detections/verifications'/f'{problem.item_id}-{asset.asset_id}-astra.json'
                        if failed_path.exists():
                            failure = json.loads(failed_path.read_text(encoding='utf-8'))['result']
                        else:
                            safe = re.sub(r'[^0-9A-Za-z가-힣_-]+', '-', problem.item_id+'-'+asset.asset_id)
                            observation = context.run_dir/'solutions/detections/targeted-repairs/raw/codex'/f'{safe}-{quality.identity(asset.signature())[:12]}.input.json'
                            saved = json.loads(observation.read_text(encoding='utf-8'))
                            if saved['problem'] != problem.signature() or saved['asset'] != asset.signature():
                                raise ValueError('cached repair input changed')
                            failure = saved['failure']
                        repaired = self._targeted_crop_repair(context, state, problem, asset, pair, failure, {}, schema, envelope, template,
                            corroborating=corroborating, cache_only=True)
                        if repaired is not None:
                            state.assets_by_id[asset.asset_id], results[(problem.item_id, asset.asset_id)] = repaired
                            continue
                except (OSError, KeyError, ValueError, ContractError) as error:
                    state.recovery_trace.append({'stage':'targeted-repair','code':'cached_repair_not_reusable',
                        'problem_id':problem.item_id,'asset_id':asset.asset_id,'error_type':type(error).__name__})
            if asset.asset_id in state.recovery_asset_ids:
                _gemini, _qwen, provisional_verification = self._verify_exact_asset(
                    context,
                    state,
                    problem,
                    asset,
                    pair,
                    schema,
                    envelope,
                    template,
                    tag="recovery-provisional",
                    corroborating=corroborating,
                    cache_only=cache_only,
                )
                if (
                    provisional_verification is not None
                    and _confirmed_verification(provisional_verification, problem, asset)
                ):
                    state.approved_recovery_asset_ids.add(asset.asset_id)
                    results[(problem.item_id, asset.asset_id)] = (
                        provisional_verification
                    )
                    continue
                if cache_only or state.verification_blocked_reason is not None:
                    continue
                incomplete = {
                    "decision": "review",
                    "same_question": "yes",
                    "candidate_crop_complete": False,
                    "target_identifier_visible": False,
                    "target_answer_visible": (
                        "no"
                        if asset.role == SolutionRole.QUICK_ANSWER
                        else "not_applicable"
                    ),
                    "first_line_complete": False,
                    "last_line_complete": False,
                    "neighbor_entry_absent": False,
                    "next_question_absent": False,
                    "review_reasons": ["canonical_boundary_omitted"],
                }
                repaired = self._targeted_crop_repair(
                    context,
                    state,
                    problem,
                    asset,
                    pair,
                    incomplete,
                    incomplete,
                    schema,
                    envelope,
                    template,
                    corroborating,
                    cache_only,
                )
                if repaired is not None:
                    repaired_asset, repaired_verification = repaired
                    state.assets_by_id[asset.asset_id] = repaired_asset
                    state.approved_recovery_asset_ids.add(asset.asset_id)
                    results[(problem.item_id, asset.asset_id)] = repaired_verification
                else:
                    results[(problem.item_id, asset.asset_id)] = PairVerification(
                        False, 0.0, "targeted-recovery"
                    )
                continue
            gemini, qwen, verification = self._verify_exact_asset(
                context,
                state,
                problem,
                asset,
                pair,
                schema,
                envelope,
                template,
                corroborating=corroborating,
                cache_only=cache_only,
            )
            if verification is not None and (
                verification.same_item or verification.explicit_answer_conflict
            ):
                results[(problem.item_id, asset.asset_id)] = verification
                continue
            if not cache_only and self._crop_repair_eligible(
                gemini, qwen, asset.role
            ):
                repaired = self._targeted_crop_repair(
                    context,
                    state,
                    problem,
                    asset,
                    pair,
                    gemini,
                    qwen,
                    schema,
                    envelope,
                    template,
                    corroborating,
                    cache_only,
                )
                if repaired is not None:
                    repaired_asset, repaired_verification = repaired
                    state.assets_by_id[asset.asset_id] = repaired_asset
                    results[(problem.item_id, asset.asset_id)] = repaired_verification
                    continue
                results[(problem.item_id, asset.asset_id)] = PairVerification(
                    False,
                    float((gemini or {}).get("confidence", 0)),
                    "astra-quality-pending",
                )
                continue
            if verification is not None:
                results[(problem.item_id, asset.asset_id)] = verification
        if state.solution_index is not None:
            state.solution_index = _ordinary_solution_index(state, state.solution_index)
        if state.recovery_trace:
            _atomic_json(context.run_dir / 'solutions' / 'recovery_trace.json', state.recovery_trace)
        return results

    def render_solution_assets(
        self,
        context: LinkContext,
        assets: tuple[SolutionAsset, ...],
        decisions: tuple[LinkDecision, ...],
    ) -> Mapping[str, Mapping[str, Any]]:
        requested = {
            asset_id
            for decision in decisions
            for asset_id in (
                decision.answer_ids + decision.explanation_ids + decision.candidate_ids
            )
        }
        auto_verified = {
            asset_id
            for decision in decisions
            if decision.status == LinkStatus.VERIFIED
            for asset_id in decision.answer_ids + decision.explanation_ids
        }
        state = self.states.get(
            (str(context.run_dir.resolve()), str(context.solution_path.resolve()))
        )
        by_id = {
            asset.asset_id: (
                state.assets_by_id.get(asset.asset_id, asset)
                if state is not None
                else asset
            )
            for asset in assets
        }
        current_assets = tuple(by_id.values())
        records: dict[str, Mapping[str, Any]] = {}
        unapproved_recovery = (
            state.recovery_asset_ids - state.approved_recovery_asset_ids
            if state is not None
            else set()
        )
        blocked = auto_verified & unapproved_recovery
        if blocked:
            raise ValueError(
                "unapproved recovery candidates cannot be auto-verified: "
                + ", ".join(sorted(blocked))
            )
        for asset_id in sorted(requested):
            asset = by_id.get(asset_id)
            if asset is None:
                continue
            output_asset = output_asset_with_context(asset, current_assets)
            source_path = (
                self._source_pdf(state)
                if state is not None
                and getattr(output_asset, "source_document", "solution") == "problem"
                else context.solution_path
            )
            output = solution_asset_output_path(
                context, output_asset, source_path
            )
            try:
                if not output.exists():
                    with self.pdf_lock:
                        self.asset_renderer(
                            source_path,
                            output_asset,
                            output,
                            context.crop_dpi,
                        )
                record = output_asset.manifest_record(
                    Path("crops") / output.name, _digest_file(output)
                )
                record.update(
                    dpi=context.crop_dpi,
                    source_sha256=_digest_file(source_path),
                    source_document=getattr(
                        output_asset, "source_document", "solution"
                    ),
                    selection_status=(
                        "auto_verified" if asset_id in auto_verified else "review_only"
                    ),
                )
                # Internal pipeline-only identity. run_link_pipeline strips it
                # from link_manifest after Phase 3 snapshots capture the exact
                # context-adjusted geometry that was rendered.
                record["_snapshot_signature"] = output_asset.signature()
                record["quality_signature"] = output_asset.signature()
                records[asset_id] = record
            except (OSError, ValueError) as exc:
                raise ValueError(f"solution candidate render failed: {asset_id}: {exc}") from exc
        return records


_OFFLINE_UNIT_ID = re.compile(r"-q(?P<parent>\d+)(?:-sub-(?P<child>[^-]+))?$")


def _offline_unit_identity(unit_id: str) -> tuple[str, str | None]:
    match = _OFFLINE_UNIT_ID.search(unit_id)
    if match is None:
        raise ValueError(f"cannot parse recovery unit identity: {unit_id}")
    return (
        normalize_parent_number(match.group("parent")),
        normalize_subquestion_label(match.group("child")),
    )


def _offline_block_targets(
    block: Mapping[str, Any], parent: str, child: str | None
) -> bool:
    if block.get("role") != SolutionRole.DETAILED_SOLUTION.value:
        return False
    coverage = block.get("coverage")
    if isinstance(coverage, Mapping):
        try:
            coverage_parent = _optional_parent(coverage.get("parent_number"))
        except ValueError:
            coverage_parent = None
        labels = {
            label
            for raw in coverage.get("subquestion_labels", [])
            if (label := normalize_subquestion_label(raw)) is not None
        }
        if coverage_parent == parent and (child is None or child in labels):
            return True
    for entry in block.get("entries", []):
        if not isinstance(entry, Mapping):
            continue
        try:
            entry_parent = _optional_parent(entry.get("parent_number"))
        except ValueError:
            continue
        if (
            entry_parent == parent
            and normalize_subquestion_label(entry.get("subquestion_label")) == child
        ):
            return True
    return False


def _offline_page_blocks(
    leaf: Path,
    page_index: int,
    cache: dict[
        int,
        tuple[
            list[Mapping[str, Any]],
            list[Mapping[str, Any]],
            list[Mapping[str, Any]],
        ],
    ],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    if page_index in cache:
        return cache[page_index]
    root = leaf / "solutions" / "detections" / "solution-blocks"
    proposals: list[tuple[str, Mapping[str, Any]]] = []
    schema = json.loads(BLOCK_SCHEMA.read_text(encoding="utf-8"))
    for provider in ("codex", "gemini"):
        path = root / "raw" / provider / f"page-{page_index + 1:04d}.json"
        try:
            result = json.loads(path.read_text(encoding="utf-8"))["result"]
            validate_contract(result, schema)
        except (OSError, KeyError, json.JSONDecodeError, ContractError) as exc:
            raise ValueError(f"cached {provider} solution blocks are unavailable: {path}") from exc
        proposals.append((provider, result))
    normalized = _normalize_provider_coordinates(proposals)
    if [provider for provider, _result in normalized] != ["codex", "gemini"]:
        raise ValueError(f"cannot normalize cached solution blocks for page {page_index + 1}")
    by_provider = {
        provider: [
            block for block in result.get("blocks", []) if isinstance(block, Mapping)
        ]
        for provider, result in normalized
    }
    consensus_path = root / "consensus" / f"page-{page_index + 1:04d}.json"
    try:
        consensus_result = json.loads(consensus_path.read_text(encoding="utf-8"))["result"]
        consensus = [
            block
            for block in consensus_result.get("blocks", [])
            if isinstance(block, Mapping)
        ]
    except (OSError, KeyError, json.JSONDecodeError):
        consensus = []
    cache[page_index] = (by_provider["codex"], by_provider["gemini"], consensus)
    return cache[page_index]


def _offline_recovery_assets(
    leaf: Path,
    manifest: Mapping[str, Any],
    target_unit_ids: set[str],
) -> tuple[tuple[SolutionAsset, ...], dict[str, str]]:
    assets = manifest.get("assets")
    items = manifest.get("items")
    item_order = manifest.get("problem", {}).get("item_ids")
    if (
        not isinstance(assets, Mapping)
        or not isinstance(items, Mapping)
        or not isinstance(item_order, list)
    ):
        raise ValueError("existing link manifest is missing assets/items/item order")
    owners: dict[str, str] = {}
    for unit_id, decision in items.items():
        if not isinstance(unit_id, str) or not isinstance(decision, Mapping):
            continue
        for asset_id in decision.get("candidate_ids", []):
            if isinstance(asset_id, str) and asset_id.startswith("recovery-detail-"):
                if asset_id in owners and owners[asset_id] != unit_id:
                    raise ValueError(f"recovery asset has multiple owners: {asset_id}")
                owners[asset_id] = unit_id
    sequence_by_unit = {unit_id: index for index, unit_id in enumerate(item_order, 1)}
    page_cache: dict[
        int,
        tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]]],
    ] = {}
    recovered: list[SolutionAsset] = []
    owner_by_asset: dict[str, str] = {}
    for asset_id, unit_id in owners.items():
        record = assets.get(asset_id)
        if not isinstance(record, Mapping) or record.get("kind") != "detailed_solution":
            continue
        raw_parts = record.get("parts")
        if not isinstance(raw_parts, list) or not raw_parts:
            continue
        parent, child = _offline_unit_identity(unit_id)
        parts: list[SolutionPart] = []
        scope_values: list[str] = []
        bottom_extended = False
        used: dict[int, set[int]] = {}
        for order, raw_part in enumerate(raw_parts):
            if not isinstance(raw_part, Mapping):
                parts = []
                break
            try:
                page_index = int(raw_part["pdf_index"])
                old_bbox = [int(value) for value in raw_part["bbox"]]
                page = PageIdentity(
                    page_index,
                    int(raw_part.get("pdf_page", page_index + 1)),
                    raw_part.get("printed_page"),
                    str(raw_part["segment_id"]),
                )
            except (KeyError, TypeError, ValueError):
                parts = []
                break
            codex, gemini, consensus = _offline_page_blocks(leaf, page_index, page_cache)
            candidates = [
                (index, block)
                for index, block in enumerate(codex)
                if index not in used.setdefault(page_index, set())
                and _offline_block_targets(block, parent, child)
                and _valid_bbox(block.get("bbox"))
            ]
            if not candidates:
                parts = []
                break
            primary_index, primary = min(
                candidates,
                key=lambda value: sum(
                    abs(int(left) - int(right))
                    for left, right in zip(value[1]["bbox"], old_bbox)
                ),
            )
            used[page_index].add(primary_index)
            peers = [
                block
                for block in (*gemini, *consensus)
                if _offline_block_targets(block, parent, child)
            ]
            bbox = _recovery_content_bbox(primary, peers)
            if not _valid_bbox(bbox):
                parts = []
                break
            if unit_id in target_unit_ids and bbox[3] > int(primary["bbox"][3]):
                bottom_extended = True
            scope_values.extend(
                str(value)
                for value in (
                    primary.get("numbering_scope_label"),
                    *primary.get("hierarchy_path", []),
                )
                if value
            )
            parts.append(SolutionPart(page, tuple(bbox), order))
        if not parts:
            if unit_id in target_unit_ids:
                raise ValueError(f"cannot reconstruct cached recovery geometry: {unit_id}")
            continue
        scope_text = "|".join(
            dict.fromkeys(
                label
                for value in scope_values
                if (label := _semantic_label(value))
            )
        )
        scope_id = "offline-" + hashlib.sha256(scope_text.encode("utf-8")).hexdigest()[:12]
        reason = str(record.get("review_reason") or "raw_identity_recovery")
        if bottom_extended and RECOVERY_CONSENSUS_MARKER not in reason:
            reason += "+" + RECOVERY_CONSENSUS_MARKER
        recovered.append(
            SolutionAsset(
                asset_id,
                SolutionRole.DETAILED_SOLUTION,
                sequence_by_unit.get(unit_id, len(sequence_by_unit) + 1),
                scope_id,
                parent,
                child,
                tuple(parts),
                confidence=float(record.get("confidence", 0)),
                review_reason=reason,
            )
        )
        owner_by_asset[asset_id] = unit_id
    missing = target_unit_ids - set(owner_by_asset.values())
    if missing:
        raise ValueError(
            "target recovery candidates are unavailable: "
            + ", ".join(sorted(missing))
        )
    return tuple(sorted(recovered, key=lambda asset: asset.sequence)), owner_by_asset


def _validate_offline_asset_files(leaf: Path, manifest: Mapping[str, Any]) -> None:
    solution_root = (leaf / "solutions").resolve()
    for asset_id, record in manifest.get("assets", {}).items():
        if not isinstance(record, Mapping):
            raise ValueError(f"invalid materialized asset record: {asset_id}")
        relative = Path(str(record.get("output_file") or ""))
        target = (solution_root / relative).resolve()
        if not target.is_relative_to(solution_root) or not target.is_file():
            raise ValueError(f"materialized asset file is missing: {asset_id}")
        expected = record.get("output_sha256")
        if not isinstance(expected, str) or _digest_file(target) != expected:
            raise ValueError(f"materialized asset hash differs: {asset_id}")


def _problem_crop_digests(leaf: Path) -> tuple[str | None, dict[str, str]]:
    manifest = leaf / "manifest.json"
    manifest_sha = _digest_file(manifest) if manifest.is_file() else None
    crop_root = leaf / "crops"
    crops = (
        {
            path.relative_to(crop_root).as_posix(): _digest_file(path)
            for path in crop_root.rglob("*")
            if path.is_file()
        }
        if crop_root.is_dir()
        else {}
    )
    return manifest_sha, crops


def repair_solution_seams_offline(
    source_run_dir: Path | str,
    output_run_dir: Path | str,
    target_unit_ids: Sequence[str],
    *,
    asset_renderer: AssetRenderer | None = None,
) -> dict[str, Any]:
    """Copy one leaf and repair only cached recovery seams without any AI stage."""

    source = Path(source_run_dir).expanduser().resolve()
    output = inside(Path(output_run_dir).expanduser().absolute(), "output run")
    targets = {str(value).strip() for value in target_unit_ids if str(value).strip()}
    if not source.is_dir() or not targets:
        raise ValueError("existing run directory and at least one target unit are required")
    if output.exists():
        raise ValueError(f"output run directory already exists: {output}")
    output_resolved = output.resolve(strict=False)
    if output_resolved.is_relative_to(source) or source.is_relative_to(output_resolved):
        raise ValueError("output run directory must be separate from the existing leaf")
    manifest_path = source / "solutions" / "link_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_link_manifest(manifest)
    _validate_offline_asset_files(source, manifest)
    recovery_assets, owner_by_asset = _offline_recovery_assets(source, manifest, targets)
    materialized, replacements = _materialize_recovery_detail_seams(recovery_assets)
    if not replacements or not targets.issubset(
        {owner_by_asset[asset_id] for asset_id in replacements if asset_id in owner_by_asset}
    ):
        raise ValueError("cached geometry did not produce every requested seam repair")
    materialized_by_id = {asset.asset_id: asset for asset in materialized}
    old_assets = manifest["assets"]
    old_items = copy.deepcopy(manifest["items"])
    missing_items = [
        unit_id
        for unit_id, decision in old_items.items()
        if not decision.get("candidate_ids")
    ]
    if missing_items:
        raise ValueError(
            "existing manifest still has missing candidates: "
            + ", ".join(missing_items[:10])
        )
    solution = manifest.get("solution")
    source_pdf_value = solution.get("source_pdf") if isinstance(solution, Mapping) else None
    source_pdf = Path(str(source_pdf_value or "")).expanduser().resolve()
    expected_source_sha = (
        str(solution.get("source_sha256")) if isinstance(solution, Mapping) else ""
    )
    if not source_pdf.is_file() or _digest_file(source_pdf) != expected_source_sha:
        raise ValueError("hash-bound solution PDF is unavailable")
    before_problem = _problem_crop_digests(source)
    inside(output.resolve(), "output")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging: Path | None = Path(
        tempfile.mkdtemp(prefix=output.name + ".tmp-", dir=output.parent)
    )
    renderer = asset_renderer or render_solution_asset_image
    try:
        shutil.copytree(source, staging, dirs_exist_ok=True, copy_function=shutil.copy2)
        source_snapshot = staging / "solutions" / f".seam-source-{expected_source_sha[:12]}.pdf"
        shutil.copy2(source_pdf, source_snapshot)
        if _digest_file(source_snapshot) != expected_source_sha:
            raise ValueError("solution PDF changed while creating the seam snapshot")
        updated = copy.deepcopy(manifest)
        changed_items: set[str] = set()
        for old_id, new_id in replacements.items():
            repaired = materialized_by_id[new_id]
            old_record = old_assets[old_id]
            dpi = int(old_record.get("dpi", 300))
            context = LinkContext(staging, source_snapshot, dpi, 1, None)
            output_file = solution_asset_output_path(context, repaired, source_snapshot)
            renderer(source_snapshot, repaired, output_file, dpi)
            record = repaired.manifest_record(
                Path("crops") / output_file.name, _digest_file(output_file)
            )
            record.update(
                dpi=dpi,
                source_sha256=expected_source_sha,
                source_document="solution",
                selection_status="review_only",
            )
            updated["assets"][new_id] = record
            for unit_id, decision in updated["items"].items():
                if old_id not in decision.get("candidate_ids", []):
                    continue
                if new_id not in decision["candidate_ids"]:
                    decision["candidate_ids"].append(new_id)
                if RECOVERY_SEAM_MARKER not in decision["review_reasons"]:
                    decision["review_reasons"].append(RECOVERY_SEAM_MARKER)
                evidence = f"{new_id}:{RECOVERY_SEAM_MARKER}"
                if evidence not in decision["evidence"]:
                    decision["evidence"].append(evidence)
                changed_items.add(unit_id)
        if not changed_items:
            raise ValueError("no manifest item received a seam-repaired candidate")
        for unit_id, decision in old_items.items():
            if unit_id not in changed_items and updated["items"][unit_id] != decision:
                raise ValueError(f"unrelated decision changed: {unit_id}")
        if any(
            updated["assets"].get(asset_id) != record
            for asset_id, record in old_assets.items()
        ):
            raise ValueError("an existing asset record changed")
        for decision in updated["items"].values():
            decision.update(derive_link_statuses(decision, updated["assets"]))
        updated["cache_key"] = hashlib.sha256(
            json.dumps(
                {
                    "kind": "offline-seam-repair-v2",
                    "source": manifest["cache_key"],
                    "replacements": sorted(replacements.items()),
                },
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        updated["seam_repair"] = {
            "version": 2,
            "source_cache_key": manifest["cache_key"],
            "target_unit_ids": sorted(targets),
            "replacements": dict(sorted(replacements.items())),
            "changed_item_ids": sorted(changed_items),
            "missing_candidate_count": 0,
        }
        validate_link_manifest(updated)
        _atomic_json(staging / "solutions" / "link_manifest.json", updated)
        _validate_offline_asset_files(staging, updated)
        if _problem_crop_digests(staging) != before_problem:
            raise ValueError("problem manifest or crops changed during seam repair")
        if _digest_file(source_snapshot) != expected_source_sha:
            raise ValueError("solution PDF snapshot changed while rendering seam crops")
        source_snapshot.unlink()
        if _digest_file(source_pdf) != expected_source_sha:
            raise ValueError("solution PDF changed before finalizing the seam overlay")
        os.replace(staging, output)
        staging = None
        return updated
    finally:
        if staging is not None and staging.exists():
            staging_resolved = staging.resolve()
            if (
                staging_resolved.parent == output.parent.resolve()
                and staging_resolved.name.startswith(output.name + ".tmp-")
            ):
                shutil.rmtree(staging_resolved)


DEFAULT_RUNTIME_ADAPTER = SolutionRuntimeAdapter()
DEFAULT_RUNTIME_HOOKS = DEFAULT_RUNTIME_ADAPTER.hooks()
