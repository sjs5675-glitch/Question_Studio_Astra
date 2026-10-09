"""Provider-free Phase 3 snapshot and fixed-edge planning primitives.

The planner has no server or provider calls. File-bound REVIEW input validation
reuses the runtime's existing local-input validator, never its execution hooks.
It persists exact normalized inputs and plans role-local monotonic changes only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import solution_link as sl
import solution_scope
from astra_runtime import inside


ALIGNMENT_SNAPSHOT_SCHEMA_VERSION = 1
ALIGNMENT_SNAPSHOT_CONTRACT = "alignment-snapshot-v1"
ANCHORLESS_GROUP_CONTRACT = "source-bound-anchorless-v1"
MANUAL_PROPAGATION_CONTRACT = "fixed-edge-v1"
FIXED_EDGE_PLAN_CONTRACT = "fixed-edge-plan-v1"

ROLE_QUICK = "quick_answer"
ROLE_DETAIL = "detailed_solution"
ROLE_MIXED = "mixed"
PLANNABLE_ROLES = frozenset({ROLE_QUICK, ROLE_DETAIL})
DUAL_VERIFIER = sl.DUAL_VERIFIER
DUAL_SCOPE_VERIFIER = sl.DUAL_SCOPE_VERIFIER
MIN_LINK_CONFIDENCE = sl.MIN_LINK_CONFIDENCE
MIN_RUNNER_UP_MARGIN = 0.15
_PROJECTED_LOCATION_PAGE = 1

VERSION_FIELDS = frozenset(
    {
        "core_version",
        "runtime_version",
        "scope_index_version",
        "scope_alignment_version",
        "verifier_contract_version",
    }
)
SNAPSHOT_FIELDS = frozenset(
    {
        "contract",
        "schema_version",
        "problem_source_sha256",
        "solution_source_sha256",
        "problem_index",
        "solution_assets",
        "projected_groups",
        "pair_verifications",
        "baseline_decisions",
        "scope_contract",
        "alignment_index",
        "link_cache_key",
        "versions",
        "payload_sha256",
    }
)
CONSTRAINT_FIELDS = frozenset(
    {
        "confirmation_id",
        "unit_id",
        "role",
        "asset_id",
        "problem_identity_sha256",
        "asset_identity_sha256",
        "scope_id",
        "segment_id",
        "created_at",
    }
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
PAIR_VERIFICATION_FIELDS = frozenset(
    {
        "unit_id",
        "asset_id",
        "same_item",
        "confidence",
        "verifier",
        "explicit_answer_conflict",
        "corroborating_asset_id",
        "contract",
        "provider_refs",
    }
)
PROVIDER_REF_FIELDS = frozenset(
    {
        "provider",
        "namespace",
        "cache_key",
        "contract",
        "result_relative_path",
        "result_sha256",
    }
)
SCOPE_PROVIDERS = ("gemini", "qwen")
SCOPE_RESULT_FIELDS = frozenset(
    {
        "question_id",
        "candidate_ids",
        "candidate_role",
        "problem_scope_id",
        "solution_scope_id",
        "decision",
        "same_question",
        "scope_episode_match",
        "hierarchy_scope_match",
        "parent_number_match",
        "subquestion_match",
        "monotonic_order_match",
        "fingerprint_match",
        "candidate_crop_complete",
        "target_identifier_visible",
        "target_answer_visible",
        "first_line_complete",
        "last_line_complete",
        "neighbor_entry_absent",
        "next_question_absent",
        "explicit_answer_conflict",
        "evidence",
        "review_reasons",
        "confidence",
    }
)


class SnapshotError(ValueError):
    """The alignment snapshot is malformed, stale, or tampered with."""


class FixedEdgeError(ValueError):
    """A manual constraint cannot be planned safely."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clone(value: Any) -> Any:
    try:
        return json.loads(canonical_json_bytes(value))
    except (TypeError, ValueError) as exc:
        raise SnapshotError("snapshot values must be JSON serializable") from exc


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _nonempty(value: Any, label: str, error: type[ValueError] = SnapshotError) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error(f"{label} must be a non-empty string")
    return value


def _safe_relative(value: Any, label: str) -> str:
    text = _nonempty(value, label)
    relative = Path(text)
    if (
        relative.is_absolute()
        or relative.drive
        or ".." in relative.parts
        or relative.as_posix() != text
    ):
        raise SnapshotError(f"{label} must be a canonical safe relative path")
    return text


def _resolve_bound_file(run_dir: Path, relative_path: str, expected_sha256: str) -> Path:
    root = run_dir.resolve()
    target = (root / Path(relative_path)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise SnapshotError("snapshot file reference escapes the run directory") from exc
    if not target.is_file():
        raise SnapshotError(f"snapshot file is missing: {relative_path}")
    if file_sha256(target) != expected_sha256:
        raise SnapshotError(f"snapshot file SHA-256 mismatch: {relative_path}")
    return target


def _combined_scope_verification(
    provider_results: Mapping[str, Mapping[str, Any]],
    problem: Mapping[str, Any],
    asset: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Recompute the persisted dual-provider outcome without provider/runtime code."""

    def valid_identity(result: Mapping[str, Any]) -> bool:
        confidence = result.get("confidence")
        neighbor_state = sl.neighbor_evidence_state(result)
        neighbor_consistent = not (
            neighbor_state == "conflict"
            or neighbor_state == "present"
            and result.get("next_question_absent") is True
            or neighbor_state == "absent"
            and result.get("next_question_absent") is False
        )
        comparison_fields = (
            "scope_episode_match",
            "hierarchy_scope_match",
            "parent_number_match",
            "subquestion_match",
            "monotonic_order_match",
            "fingerprint_match",
        )
        boolean_fields = (
            "candidate_crop_complete",
            "target_identifier_visible",
            "first_line_complete",
            "last_line_complete",
            "neighbor_entry_absent",
            "next_question_absent",
            "explicit_answer_conflict",
        )
        return bool(
            set(result) == SCOPE_RESULT_FIELDS
            and result.get("question_id") == problem.get("item_id")
            and result.get("candidate_ids") == [asset.get("asset_id")]
            and result.get("candidate_role") == asset.get("role")
            and result.get("problem_scope_id") == problem.get("scope_id")
            and result.get("solution_scope_id")
            == str(asset.get("scope_id", "")).removeprefix("episode:")
            and not isinstance(confidence, bool)
            and isinstance(confidence, (int, float))
            and math.isfinite(float(confidence))
            and 0 <= float(confidence) <= 1
            and isinstance(result.get("evidence"), list)
            and isinstance(result.get("review_reasons"), list)
            and result.get("decision")
            in {"verified", "review", "wrong_link", "conflict"}
            and result.get("same_question") in {"yes", "no", "uncertain"}
            and result.get("target_answer_visible")
            in {"yes", "no", "not_applicable"}
            and all(
                result.get(field)
                in {"match", "mismatch", "unknown", "not_applicable"}
                for field in comparison_fields
            )
            and all(isinstance(result.get(field), bool) for field in boolean_fields)
            and all(isinstance(value, str) for value in result.get("evidence", ()))
            and all(
                isinstance(value, str) for value in result.get("review_reasons", ())
            )
            and neighbor_consistent
        )

    def strong(result: Mapping[str, Any], decision: str) -> bool:
        if (
            not valid_identity(result)
            or result.get("decision") != decision
            or result.get("same_question") != "yes"
            or bool(result.get("explicit_answer_conflict"))
            != (decision == "conflict")
            or any(
                result.get(field) != "match"
                for field in (
                    "scope_episode_match",
                    "hierarchy_scope_match",
                    "parent_number_match",
                    "monotonic_order_match",
                    "fingerprint_match",
                )
            )
            or result.get("subquestion_match")
            not in (
                {"match"}
                if problem.get("subquestion_label") is not None
                else {"match", "not_applicable"}
            )
            or any(
                result.get(field) is not True
                for field in (
                    "candidate_crop_complete",
                    "target_identifier_visible",
                    "first_line_complete",
                    "last_line_complete",
                    "neighbor_entry_absent",
                    "next_question_absent",
                )
            )
        ):
            return False
        target_answer = result.get("target_answer_visible")
        return target_answer == "yes" if asset.get("role") == ROLE_QUICK else (
            target_answer in {"yes", "not_applicable"}
        )

    try:
        values = [provider_results[provider] for provider in SCOPE_PROVIDERS]
    except KeyError:
        return None
    confidence = min(float(value.get("confidence", 0)) for value in values)
    if all(strong(value, "conflict") for value in values):
        return {
            "same_item": False,
            "confidence": confidence,
            "verifier": DUAL_SCOPE_VERIFIER,
            "explicit_answer_conflict": True,
            "corroborating_asset_id": None,
        }
    if any(
        value.get("decision") == "conflict"
        or value.get("explicit_answer_conflict")
        for value in values
    ):
        return None
    if all(
        valid_identity(value)
        and value.get("decision") == "wrong_link"
        and value.get("same_question") == "no"
        and value.get("explicit_answer_conflict") is False
        for value in values
    ):
        return {
            "same_item": False,
            "confidence": confidence,
            "verifier": DUAL_SCOPE_VERIFIER,
            "explicit_answer_conflict": False,
            "corroborating_asset_id": None,
        }
    if confidence >= MIN_LINK_CONFIDENCE and all(
        strong(value, "verified") for value in values
    ):
        return {
            "same_item": True,
            "confidence": confidence,
            "verifier": DUAL_SCOPE_VERIFIER,
            "explicit_answer_conflict": False,
            "corroborating_asset_id": None,
        }
    return None


def _payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if key != "payload_sha256"}


def _asset_id(record: Mapping[str, Any]) -> str:
    signature = record.get("signature")
    if not isinstance(signature, Mapping):
        raise SnapshotError("solution asset signature must be an object")
    return _nonempty(signature.get("asset_id"), "asset_id")


def _asset_record_identity(record: Mapping[str, Any]) -> str:
    return canonical_sha256(
        {
            "signature": record["signature"],
            "output_file": record["output_file"],
            "output_sha256": record["output_sha256"],
        }
    )


def _unit_id(item: Mapping[str, Any]) -> str:
    return _nonempty(item.get("item_id"), "problem item_id")


def _validate_problem_index(
    value: Any, expected_source_sha256: str
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"source_sha256", "page_map", "items"}:
        raise SnapshotError("problem_index must be a complete ProblemIndex.signature()")
    if value.get("source_sha256") != expected_source_sha256:
        raise SnapshotError("problem index source SHA-256 is inconsistent")
    if not isinstance(value.get("page_map"), list) or not isinstance(value.get("items"), list):
        raise SnapshotError("problem index page_map/items must be arrays")
    by_id: dict[str, Mapping[str, Any]] = {}
    sequences: set[int] = set()
    for item in value["items"]:
        if not isinstance(item, dict):
            raise SnapshotError("problem item signature must be an object")
        unit_id = _unit_id(item)
        sequence = item.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise SnapshotError("problem item sequence must be an integer")
        _nonempty(item.get("scope_id"), "problem scope_id")
        anchor = item.get("anchor")
        if anchor is not None and (
            not isinstance(anchor, dict)
            or not isinstance(anchor.get("segment_id"), str)
            or not anchor.get("segment_id")
        ):
            raise SnapshotError("problem anchor segment_id is invalid")
        if unit_id in by_id or sequence in sequences:
            raise SnapshotError("problem item ids/sequences must be unique")
        by_id[unit_id] = item
        sequences.add(sequence)
    if not by_id:
        raise SnapshotError("problem index must contain at least one item")
    return by_id


def _validate_solution_assets(
    value: Any,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, list):
        raise SnapshotError("solution_assets must be an array")
    by_id: dict[str, Mapping[str, Any]] = {}
    for record in value:
        fields = {
            "signature",
            "output_file",
            "output_sha256",
            "selection_status",
        }
        if not isinstance(record, dict) or set(record) not in (fields, fields | {'original_signature'}):
            raise SnapshotError("solution asset snapshot fields are invalid")
        asset_id = _asset_id(record)
        signature = record["signature"]
        original = record.get('original_signature', signature)
        if not isinstance(original, dict) or set(original) != set(signature) or any(original[key] != value for key, value in signature.items() if key != 'parts'):
            raise SnapshotError('original asset identity differs beyond rendered parts')
        role = signature.get("role")
        if role not in {ROLE_QUICK, ROLE_DETAIL, ROLE_MIXED}:
            raise SnapshotError("solution asset role is invalid")
        sequence = signature.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise SnapshotError("solution asset sequence must be an integer")
        _nonempty(signature.get("scope_id"), "solution asset scope_id")
        parts = signature.get("parts")
        if not isinstance(parts, list) or not parts:
            raise SnapshotError("solution asset parts must be a non-empty array")
        _safe_relative(record.get("output_file"), "solution asset output_file")
        if not _is_sha256(record.get("output_sha256")):
            raise SnapshotError("solution asset output SHA-256 is invalid")
        if record.get("selection_status") not in {
            "review_only",
            "auto_verified",
            "manual_verified",
        }:
            raise SnapshotError("solution asset selection_status is invalid")
        if asset_id in by_id:
            raise SnapshotError("solution asset ids must be unique")
        by_id[asset_id] = record
    if not by_id:
        raise SnapshotError("snapshot must contain materialized solution assets")
    return by_id


def _canonical_group(
    group: Mapping[str, Any],
    problem_by_id: Mapping[str, Mapping[str, Any]],
    asset_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Canonicalize members and edges by their production ordering keys."""

    result = _clone(group)
    result["unit_ids"] = sorted(
        result.get("unit_ids", ()),
        key=lambda item_id: (
            problem_by_id.get(item_id, {}).get("sequence", 2**63),
            str(item_id),
        ),
    )
    result["asset_ids"] = sorted(
        result.get("asset_ids", ()),
        key=lambda asset_id: (
            asset_by_id.get(asset_id, {}).get("signature", {}).get(
                "sequence", 2**63
            ),
            str(asset_id),
        ),
    )
    result["candidate_edges"] = sorted(
        result.get("candidate_edges", ()),
        key=lambda edge: (
            problem_by_id.get(edge.get("unit_id"), {}).get("sequence", 2**63),
            str(edge.get("unit_id")),
            asset_by_id.get(edge.get("asset_id"), {})
            .get("signature", {})
            .get("sequence", 2**63),
            str(edge.get("asset_id")),
        ),
    )
    return result


def _validate_projected_groups(
    value: Any,
    problem_by_id: Mapping[str, Mapping[str, Any]],
    asset_by_id: Mapping[str, Mapping[str, Any]],
    source_binding: Mapping[str, str] | None = None,
) -> tuple[dict[tuple[str, str, str], Mapping[str, Any]], set[tuple[str, str]]]:
    if not isinstance(value, list):
        raise SnapshotError("projected_groups must be an array")
    groups: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    candidate_pairs: set[tuple[str, str]] = set()
    memberships: set[tuple[str, str]] = set()
    asset_memberships: set[tuple[str, str]] = set()
    for group in value:
        required = {
            "problem_scope_id",
            "segment_id",
            "role",
            "unit_ids",
            "asset_ids",
            "candidate_edges",
        }
        if not isinstance(group, dict) or set(group) not in (required, required | {'source_binding'}):
            raise SnapshotError("projected group fields are invalid")
        anchorless = 'source_binding' in group
        if anchorless and group['source_binding'] != source_binding:
            raise SnapshotError('anchorless group source binding differs')
        scope_id = _nonempty(group.get("problem_scope_id"), "projected problem_scope_id")
        segment_id = _nonempty(group.get("segment_id"), "projected segment_id")
        role = group.get("role")
        if role not in PLANNABLE_ROLES:
            raise SnapshotError("projected group role is invalid")
        key = (scope_id, segment_id, role)
        if key in groups:
            raise SnapshotError("projected group keys must be unique")
        unit_ids = group.get("unit_ids")
        asset_ids = group.get("asset_ids")
        edges = group.get("candidate_edges")
        if (
            not isinstance(unit_ids, list)
            or not isinstance(asset_ids, list)
            or not isinstance(edges, list)
            or not unit_ids
            or not asset_ids
            or len(unit_ids) != len(set(unit_ids))
            or len(asset_ids) != len(set(asset_ids))
        ):
            raise SnapshotError("projected group members must be non-empty unique arrays")
        if group != _canonical_group(group, problem_by_id, asset_by_id):
            raise SnapshotError("projected group members/edges are not canonical")
        unit_sequences: set[int] = set()
        for unit_id in unit_ids:
            if unit_id not in problem_by_id:
                raise SnapshotError("projected group references an unknown problem item")
            item = problem_by_id[unit_id]
            if item.get("scope_id") != scope_id:
                raise SnapshotError("projected group problem scope is inconsistent")
            anchor = item.get("anchor")
            if (anchorless and anchor is not None) or (not anchorless and (not isinstance(anchor, dict) or anchor.get("segment_id") != segment_id)):
                raise SnapshotError("projected group problem segment is inconsistent")
            member_key = (unit_id, role)
            if member_key in memberships:
                raise SnapshotError("a problem item/role belongs to multiple projected groups")
            memberships.add(member_key)
            sequence = int(item["sequence"])
            if sequence in unit_sequences:
                raise SnapshotError("projected problem sequences must be unique")
            unit_sequences.add(sequence)
        asset_sequences: set[int] = set()
        for asset_id in asset_ids:
            if asset_id not in asset_by_id:
                raise SnapshotError("projected group references an unknown solution asset")
            asset_role = asset_by_id[asset_id]["signature"].get("role")
            if anchorless and {part['page']['segment_id'] for part in asset_by_id[asset_id]['signature']['parts']} != {segment_id}:
                raise SnapshotError('anchorless group asset segment differs')
            if asset_role not in {role, ROLE_MIXED}:
                raise SnapshotError("projected group asset role is inconsistent")
            sequence = int(asset_by_id[asset_id]["signature"]["sequence"])
            if sequence in asset_sequences:
                raise SnapshotError("projected asset sequences must be unique")
            asset_sequences.add(sequence)
            asset_member = (asset_id, role)
            if asset_member in asset_memberships:
                raise SnapshotError(
                    "a solution asset/role belongs to multiple projected groups"
                )
            asset_memberships.add(asset_member)
        group_pairs: set[tuple[str, str]] = set()
        for edge in edges:
            if not isinstance(edge, dict) or set(edge) != {
                "unit_id",
                "asset_id",
                "fingerprint_similarity_milli",
            }:
                raise SnapshotError("candidate edge fields are invalid")
            pair = (edge.get("unit_id"), edge.get("asset_id"))
            similarity = edge.get("fingerprint_similarity_milli")
            if (
                pair[0] not in unit_ids
                or pair[1] not in asset_ids
                or pair in group_pairs
                or isinstance(similarity, bool)
                or not isinstance(similarity, int)
                or not 0 <= similarity <= 1000
            ):
                raise SnapshotError("candidate edge is invalid")
            group_pairs.add(pair)
            candidate_pairs.add(pair)
        if not group_pairs:
            raise SnapshotError("projected group must contain candidate edges")
        groups[key] = group
    return groups, candidate_pairs


def _validate_pair_verifications(
    value: Any,
    candidate_pairs: set[tuple[str, str]],
    problem_by_id: Mapping[str, Mapping[str, Any]],
    asset_by_id: Mapping[str, Mapping[str, Any]],
    *,
    run_dir: Path | None = None,
) -> dict[tuple[str, str], Mapping[str, Any]]:
    if not isinstance(value, list):
        raise SnapshotError("pair_verifications must be an array")
    by_pair: dict[tuple[str, str], Mapping[str, Any]] = {}
    referenced_files: set[str] = set()
    for verification in value:
        if not isinstance(verification, dict) or set(verification) not in (PAIR_VERIFICATION_FIELDS, PAIR_VERIFICATION_FIELDS | {'quality_receipt', 'evidence_root'}, PAIR_VERIFICATION_FIELDS | {'evidence_kind', 'nonapproval_receipt', 'evidence_root'}):
            raise SnapshotError("pair verification fields are invalid")
        pair = (verification.get("unit_id"), verification.get("asset_id"))
        confidence = verification.get("confidence")
        if pair not in candidate_pairs or pair in by_pair:
            raise SnapshotError("pair verification references an invalid candidate edge")
        if (
            not isinstance(verification.get("same_item"), bool)
            or not isinstance(verification.get("explicit_answer_conflict"), bool)
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            raise SnapshotError("pair verification result is invalid")
        if 'quality_receipt' in verification or 'nonapproval_receipt' in verification:
            nonapproval = 'nonapproval_receipt' in verification
            receipt = verification['nonapproval_receipt'] if nonapproval else verification['quality_receipt']
            allowed_verifiers = {'astra-current-evidence', 'astra-quality-pending', 'astra-scope-quality-pending'} if nonapproval else {'astra-current-evidence'}
            if (nonapproval and verification.get('evidence_kind') != 'nonapproval') or verification.get('verifier') not in allowed_verifiers or verification.get('provider_refs') != [] or not isinstance(receipt, dict) or receipt.get('policy') != sl.quality.QUALITY_POLICY:
                raise SnapshotError('current pair verification contract is invalid')
            root_value = verification.get('evidence_root')
            if not isinstance(root_value, str) or not Path(root_value).is_absolute():
                raise SnapshotError('current evidence root is invalid')
            try:
                root = inside(Path(root_value), 'snapshot evidence root')
            except ValueError as exc:
                raise SnapshotError('current evidence root escapes the workspace') from exc
            if run_dir is not None and root != run_dir.resolve():
                raise SnapshotError('current evidence root differs from snapshot run')
            result = receipt.get('result')
            if not isinstance(result, dict) or receipt.get('receipt_id') != sl.quality.identity({key: entry for key, entry in receipt.items() if key != 'receipt_id'}):
                raise SnapshotError('current receipt digest or result is invalid')
            if not nonapproval and (verification['confidence'] != result.get('confidence') or verification['same_item'] != (result.get('decision') == 'verified' and result.get('same_question') == 'yes' and not result.get('explicit_answer_conflict')) or verification['explicit_answer_conflict'] != result.get('explicit_answer_conflict')):
                raise SnapshotError('current verification result differs from receipt')
            corroborating = verification.get('corroborating_asset_id')
            if corroborating is not None and (not isinstance(corroborating, str) or (pair[0], corroborating) not in candidate_pairs or asset_by_id[corroborating]['signature']['role'] not in {ROLE_DETAIL, ROLE_MIXED}):
                raise SnapshotError('current corroborating asset is invalid')
            problem_signature = _production_problem(problem_by_id[pair[0]]).signature()
            asset_signature = _original_asset(asset_by_id[pair[1]]).signature()
            if 'original_signature' in asset_by_id[pair[1]] and receipt.get('target', {}).get('rendered_asset') != asset_by_id[pair[1]]['signature']:
                raise SnapshotError('rendered asset differs from current receipt')
            corroborating_signature = _original_asset(asset_by_id[corroborating]).signature() if corroborating is not None else None
            if nonapproval:
                try:
                    observation = sl.quality.read_nonapproval_receipt(receipt, root, problem=problem_signature, asset=asset_signature, corroborating=corroborating_signature)
                except sl.quality.AstraError as exc:
                    raise SnapshotError('nonapproval source-bound receipt is invalid') from exc
                if (observation.same_item, observation.confidence, observation.explicit_answer_conflict) != (verification['same_item'], verification['confidence'], verification['explicit_answer_conflict']):
                    raise SnapshotError('nonapproval observation differs from receipt')
            elif sl.quality.validate_receipt(receipt, root, problem=problem_signature, asset=asset_signature, conflict=verification['explicit_answer_conflict'], corroborating=corroborating_signature) is not None:
                raise SnapshotError('current source-bound receipt is invalid')
            by_pair[pair] = verification
            continue
        if verification.get("verifier") != DUAL_SCOPE_VERIFIER:
            raise SnapshotError("pair verification is not a dual scope verification")
        contract = _nonempty(verification.get("contract"), "verification contract")
        refs = verification.get("provider_refs")
        if not isinstance(refs, list) or len(refs) != len(SCOPE_PROVIDERS):
            raise SnapshotError("pair verification requires two provider references")
        refs_by_provider: dict[str, Mapping[str, Any]] = {}
        provider_results: dict[str, Mapping[str, Any]] = {}
        for reference in refs:
            if not isinstance(reference, dict) or set(reference) != PROVIDER_REF_FIELDS:
                raise SnapshotError("provider verification reference fields are invalid")
            provider = reference.get("provider")
            namespace = reference.get("namespace")
            cache_key = reference.get("cache_key")
            if (
                provider not in SCOPE_PROVIDERS
                or provider in refs_by_provider
                or namespace != f"verify-scope-{provider}"
                or not _is_sha256(cache_key)
                or reference.get("contract") != contract
            ):
                raise SnapshotError("provider verification identity is invalid")
            result_path = _safe_relative(
                reference.get("result_relative_path"),
                "verification result_relative_path",
            )
            if result_path in referenced_files:
                raise SnapshotError("provider verification files must be unique")
            referenced_files.add(result_path)
            if not _is_sha256(reference.get("result_sha256")):
                raise SnapshotError("verification result SHA-256 is invalid")
            refs_by_provider[str(provider)] = reference
            if run_dir is not None:
                result_file = _resolve_bound_file(
                    run_dir, result_path, reference["result_sha256"]
                )
                try:
                    wrapper = json.loads(result_file.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise SnapshotError("provider verification result is invalid JSON") from exc
                meta = wrapper.get("meta") if isinstance(wrapper, Mapping) else None
                result = wrapper.get("result") if isinstance(wrapper, Mapping) else None
                if (
                    not isinstance(meta, Mapping)
                    or not isinstance(result, Mapping)
                    or meta.get("provider") != provider
                    or meta.get("namespace") != namespace
                    or meta.get("cache_key") != cache_key
                    or meta.get("contract") != contract
                ):
                    raise SnapshotError("provider verification wrapper identity changed")
                problem = problem_by_id[pair[0]]
                asset = asset_by_id[pair[1]]["signature"]
                expected_solution_scope = str(asset.get("scope_id", ""))
                if expected_solution_scope.startswith("episode:"):
                    expected_solution_scope = expected_solution_scope.split(":", 1)[1]
                if (
                    result.get("question_id") != pair[0]
                    or result.get("candidate_ids") != [pair[1]]
                    or result.get("candidate_role") != asset.get("role")
                    or result.get("problem_scope_id") != problem.get("scope_id")
                    or result.get("solution_scope_id") != expected_solution_scope
                ):
                    raise SnapshotError("provider verification result pair identity changed")
                provider_results[str(provider)] = result
        if set(refs_by_provider) != set(SCOPE_PROVIDERS):
            raise SnapshotError("pair verification provider references are incomplete")
        corroborating = verification.get("corroborating_asset_id")
        if corroborating is not None and (
            not isinstance(corroborating, str)
            or not corroborating
            or corroborating not in asset_by_id
            or asset_by_id[corroborating]["signature"].get("role")
            not in {ROLE_DETAIL, ROLE_MIXED}
            or (pair[0], corroborating) not in candidate_pairs
        ):
            raise SnapshotError("corroborating_asset_id is invalid")
        if run_dir is not None:
            expected = _combined_scope_verification(
                provider_results,
                problem_by_id[pair[0]],
                asset_by_id[pair[1]]["signature"],
            )
            actual = {
                key: verification.get(key)
                for key in expected or {
                    "same_item",
                    "confidence",
                    "verifier",
                    "explicit_answer_conflict",
                    "corroborating_asset_id",
                }
            }
            if expected is None or actual != expected:
                raise SnapshotError("combined pair verification result changed")
        by_pair[pair] = verification
    return by_pair


def _validate_baseline_decisions(
    value: Any,
    problem_by_id: Mapping[str, Mapping[str, Any]],
    asset_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, list):
        raise SnapshotError("baseline_decisions must be an array")
    required = {
        "status",
        "answer_ids",
        "explanation_ids",
        "candidate_ids",
        "solution_printed_pages",
        "confidence",
        "evidence",
        "review_reasons",
    }
    by_id: dict[str, Mapping[str, Any]] = {}
    for record in value:
        if not isinstance(record, dict) or set(record) != {"unit_id", "decision"}:
            raise SnapshotError("baseline decision wrapper is invalid")
        unit_id = record.get("unit_id")
        decision = record.get("decision")
        if unit_id not in problem_by_id or unit_id in by_id or not isinstance(decision, dict):
            raise SnapshotError("baseline decision item identity is invalid")
        if not required.issubset(decision):
            raise SnapshotError("baseline LinkDecision fields are incomplete")
        for field in ("answer_ids", "explanation_ids", "candidate_ids"):
            ids = decision.get(field)
            if (
                not isinstance(ids, list)
                or len(ids) != len(set(ids))
                or any(asset_id not in asset_by_id for asset_id in ids)
            ):
                raise SnapshotError(f"baseline {field} is invalid")
        for field, roles in (
            ("answer_ids", {ROLE_QUICK, ROLE_MIXED}),
            ("explanation_ids", {ROLE_DETAIL, ROLE_MIXED}),
        ):
            if any(
                asset_by_id[asset_id]["signature"].get("role") not in roles
                for asset_id in decision[field]
            ):
                raise SnapshotError(f"baseline {field} asset role is invalid")
        if "role_confirmations" in decision and not isinstance(
            decision["role_confirmations"], Mapping
        ):
            raise SnapshotError("baseline role_confirmations must be an object")
        if decision.get("status") not in {"verified", "review", "unmatched", "conflict"}:
            raise SnapshotError("baseline decision status is invalid")
        by_id[unit_id] = decision
    if set(by_id) != set(problem_by_id):
        raise SnapshotError("baseline decisions must cover every problem item exactly once")
    return by_id


def _validate_scope_binding(
    snapshot: Mapping[str, Any], *, run_dir: Path | None
) -> None:
    contract = snapshot.get("scope_contract")
    manifest_stub = {
        "problem": {"source_sha256": snapshot["problem_source_sha256"]},
        "solution": {"source_sha256": snapshot["solution_source_sha256"]},
    }
    try:
        sl._validate_scope_contract(contract, manifest_stub)
    except sl.ManifestError as exc:
        raise SnapshotError(f"scope_contract is invalid: {exc}") from exc

    provenance = contract["provenance"]
    index = snapshot["alignment_index"]
    if (
        provenance["alignment_index_path"] != index["relative_path"]
        or provenance["alignment_index_sha256"] != index["sha256"]
    ):
        raise SnapshotError("scope_contract alignment index binding changed")
    versions = snapshot["versions"]
    for snapshot_key, provenance_key in (
        ("core_version", "core_version"),
        ("runtime_version", "runtime_version"),
        ("scope_index_version", "scope_index_version"),
        ("scope_alignment_version", "scope_alignment_version"),
    ):
        if versions[snapshot_key] != provenance[provenance_key]:
            raise SnapshotError("scope_contract version provenance changed")
    if run_dir is None:
        return

    index_path = _resolve_bound_file(run_dir, index["relative_path"], index["sha256"])
    try:
        scope_index = solution_scope.read_scope_index(
            index_path,
            expected_solution_sha256=snapshot["solution_source_sha256"],
            expected_provider_profile=provenance.get("provider_profile", "full"),
            solutions_dir=run_dir / "solutions",
        )
    except (OSError, ValueError, solution_scope.ScopeIndexBindingError) as exc:
        raise SnapshotError(f"alignment index source binding failed: {exc}") from exc
    source = scope_index.get("source", {})
    coverage = scope_index.get("page_coverage", {})
    input_files = source.get("input_files", [])
    inventory_sha = hashlib.sha256(
        json.dumps(
            sorted((entry["path"], entry["sha256"]) for entry in input_files)
        ).encode("utf-8")
    ).hexdigest()
    if (
        source.get("input_cache_versions") != provenance["input_cache_versions"]
        or coverage.get("indexed_pages") != provenance["indexed_pages"]
        or coverage.get("missing_pages") != provenance["missing_pages"]
        or inventory_sha != provenance["input_inventory_sha256"]
    ):
        raise SnapshotError("scope_contract does not match the source-bound index")
    reviewed = provenance.get("reviewed_episode_context")
    if reviewed is None and os.path.lexists(run_dir / "solutions/reviewed_episode_context.json"):
        raise SnapshotError("scope snapshot predates the reviewed episode context input")
    if reviewed is not None:
        # Reject reparse components before resolving/hashing a REVIEW reference.
        references = [(reviewed["relative_path"], reviewed["sha256"])] + [
            ("solutions/" + proof["path"], proof["sha256"])
            for proof in reviewed["proof_files"]
        ]
        try:
            for relative, expected_sha in references:
                candidate = run_dir.absolute() / relative
                for component in (candidate, *candidate.parents):
                    info = component.lstat()
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        raise SnapshotError("reviewed scope symlink or reparse point forbidden")
                _resolve_bound_file(run_dir, relative, expected_sha)
            # Source episodes are rebuilt from the validated, original consensus,
            # not the already overridden alignment or an invented proof payload.
            from solution_runtime import SolutionRuntimeAdapter

            current_index = solution_scope.build_scope_index(
                run_dir / "solutions", provider_profile=provenance.get("provider_profile", "full")
            )
            _, current_reviewed = SolutionRuntimeAdapter._reviewed_episode_context(
                run_dir / "solutions", current_index,
                snapshot["problem_source_sha256"], snapshot["solution_source_sha256"]
            )
            if current_reviewed != reviewed:
                raise SnapshotError("reviewed episode context provenance changed")
            # Keep the snapshot binding valid across the runtime's local reads.
            for relative, expected_sha in references:
                _resolve_bound_file(run_dir, relative, expected_sha)
            solution_scope.read_scope_index(
                _resolve_bound_file(run_dir, index["relative_path"], index["sha256"]),
                expected_solution_sha256=snapshot["solution_source_sha256"],
                expected_provider_profile=provenance.get("provider_profile", "full"),
                solutions_dir=run_dir / "solutions",
            )
        except (OSError, ValueError, sl.StageHookError) as exc:
            raise SnapshotError(f"reviewed episode context file binding failed: {exc}") from exc


def validate_alignment_snapshot(
    snapshot: Any, *, run_dir: Path | None = None
) -> None:
    if not isinstance(snapshot, dict) or set(snapshot) != SNAPSHOT_FIELDS:
        raise SnapshotError("alignment snapshot fields are invalid")
    if (
        snapshot.get("contract") != ALIGNMENT_SNAPSHOT_CONTRACT
        or snapshot.get("schema_version") != ALIGNMENT_SNAPSHOT_SCHEMA_VERSION
    ):
        raise SnapshotError("unsupported alignment snapshot contract")
    problem_sha = snapshot.get("problem_source_sha256")
    solution_sha = snapshot.get("solution_source_sha256")
    if not _is_sha256(problem_sha) or not _is_sha256(solution_sha):
        raise SnapshotError("snapshot source SHA-256 is invalid")
    if snapshot.get("payload_sha256") != canonical_sha256(_payload(snapshot)):
        raise SnapshotError("alignment snapshot payload SHA-256 mismatch")
    problem_by_id = _validate_problem_index(snapshot.get("problem_index"), problem_sha)
    asset_by_id = _validate_solution_assets(snapshot.get("solution_assets"))
    groups, candidate_pairs = _validate_projected_groups(
        snapshot.get("projected_groups"), problem_by_id, asset_by_id,
        {'contract': ANCHORLESS_GROUP_CONTRACT, 'problem_source_sha256': problem_sha, 'solution_source_sha256': solution_sha},
    )
    checked_pairs = _validate_pair_verifications(
        snapshot.get("pair_verifications"),
        candidate_pairs,
        problem_by_id,
        asset_by_id,
        run_dir=run_dir,
    )
    for group in groups.values():
        if 'source_binding' in group:
            for edge in group['candidate_edges']:
                record = checked_pairs.get((edge['unit_id'], edge['asset_id']), {})
                if 'quality_receipt' not in record and 'nonapproval_receipt' not in record:
                    raise SnapshotError('anchorless edge requires current source-bound evidence')
    for verification in snapshot['pair_verifications']:
        if 'quality_receipt' in verification or 'nonapproval_receipt' in verification:
            files = verification.get('quality_receipt', verification.get('nonapproval_receipt')).get('files', {})
            if (not isinstance(files, dict) or not isinstance(files.get('problem_pdf'), dict) or not isinstance(files.get('solution_document'), dict) or files['problem_pdf'].get('sha256') != problem_sha or files['solution_document'].get('sha256') != solution_sha):
                raise SnapshotError('current receipt source differs from snapshot')
    _validate_baseline_decisions(
        snapshot.get("baseline_decisions"), problem_by_id, asset_by_id
    )
    if not isinstance(snapshot.get("scope_contract"), dict) or not snapshot["scope_contract"]:
        raise SnapshotError("scope_contract must be a non-empty object")
    alignment_index = snapshot.get("alignment_index")
    if not isinstance(alignment_index, dict) or set(alignment_index) != {
        "relative_path",
        "sha256",
    }:
        raise SnapshotError("alignment_index reference is invalid")
    _safe_relative(alignment_index.get("relative_path"), "alignment_index relative_path")
    if not _is_sha256(alignment_index.get("sha256")):
        raise SnapshotError("alignment_index SHA-256 is invalid")
    _nonempty(snapshot.get("link_cache_key"), "link_cache_key")
    versions = snapshot.get("versions")
    if not isinstance(versions, dict) or set(versions) != VERSION_FIELDS or any(
        not isinstance(value, str) or not value for value in versions.values()
    ):
        raise SnapshotError("snapshot version provenance is invalid")
    if any(
        verification["contract"] != versions["verifier_contract_version"]
        for verification in snapshot["pair_verifications"]
    ):
        raise SnapshotError("pair verification contract version changed")
    _validate_scope_binding(snapshot, run_dir=run_dir)
    if run_dir is not None:
        for asset in asset_by_id.values():
            _resolve_bound_file(
                run_dir, asset["output_file"], asset["output_sha256"]
            )


def build_alignment_snapshot(
    *,
    problem_index: Mapping[str, Any],
    solution_assets: Sequence[Mapping[str, Any]],
    projected_groups: Sequence[Mapping[str, Any]],
    pair_verifications: Sequence[Mapping[str, Any]],
    baseline_decisions: Sequence[Mapping[str, Any]],
    scope_contract: Mapping[str, Any],
    alignment_index: Mapping[str, Any],
    link_cache_key: str,
    versions: Mapping[str, str],
    problem_source_sha256: str,
    solution_source_sha256: str,
) -> dict[str, Any]:
    """Create a deterministic, self-hashed snapshot without touching files."""

    cloned_problem_index = _clone(problem_index)
    cloned_assets = sorted(
        _clone(solution_assets), key=lambda record: record["signature"]["asset_id"]
    )
    problem_by_id = {
        item["item_id"]: item for item in cloned_problem_index.get("items", ())
    }
    asset_by_id = {
        record["signature"]["asset_id"]: record for record in cloned_assets
    }
    canonical_groups = [
        _canonical_group(group, problem_by_id, asset_by_id)
        for group in projected_groups
    ]
    payload = {
        "contract": ALIGNMENT_SNAPSHOT_CONTRACT,
        "schema_version": ALIGNMENT_SNAPSHOT_SCHEMA_VERSION,
        "problem_source_sha256": problem_source_sha256,
        "solution_source_sha256": solution_source_sha256,
        "problem_index": cloned_problem_index,
        "solution_assets": cloned_assets,
        "projected_groups": sorted(
            canonical_groups,
            key=lambda group: (
                group["problem_scope_id"],
                group["segment_id"],
                group["role"],
            ),
        ),
        "pair_verifications": sorted(
            _clone(pair_verifications),
            key=lambda result: (result["unit_id"], result["asset_id"]),
        ),
        "baseline_decisions": sorted(
            _clone(baseline_decisions), key=lambda record: record["unit_id"]
        ),
        "scope_contract": _clone(scope_contract),
        "alignment_index": _clone(alignment_index),
        "link_cache_key": link_cache_key,
        "versions": _clone(versions),
    }
    snapshot = {**payload, "payload_sha256": canonical_sha256(payload)}
    validate_alignment_snapshot(snapshot)
    return snapshot


def build_pipeline_alignment_snapshot(
    *,
    problem_index: sl.ProblemIndex,
    materialized_assets: Sequence[sl.SolutionAsset],
    legacy_candidate_pairs: Sequence[sl.CandidatePair],
    verifications: Mapping[tuple[str, str], sl.PairVerification],
    pair_verification_records: Sequence[Mapping[str, Any]],
    decisions: Sequence[sl.LinkDecision],
    rendered_assets: Mapping[str, Mapping[str, Any]],
    scope_contract: Mapping[str, Any],
    link_cache_key: str,
    versions: Mapping[str, str],
) -> dict[str, Any]:
    """Translate final production link inputs into the strict v1 snapshot.

    Verification file references are supplied by the runtime that owns those
    files.  The translator never guesses a path or invents a cache/result.
    """

    problems = {item.item_id: item for item in problem_index.items}
    assets = {asset.asset_id: asset for asset in materialized_assets}
    if set(rendered_assets) - set(assets):
        raise SnapshotError("rendered asset has no production identity")

    asset_records: list[dict[str, Any]] = []
    for asset_id, rendered in sorted(rendered_assets.items()):
        asset = assets[asset_id]
        signature = rendered.get("_snapshot_signature")
        if not isinstance(signature, Mapping) or signature.get("asset_id") != asset_id:
            raise SnapshotError("exact materialized asset signature is unavailable")
        original_signature = asset.signature()
        if any(
            signature.get(key) != value
            for key, value in original_signature.items()
            if key != "parts"
        ) or any(
            key not in original_signature and key != "parts"
            for key in signature
        ):
            raise SnapshotError("materialized asset identity changed during rendering")
        expected_parts = [
            {
                "pdf_index": part["page"]["pdf_index"],
                "pdf_page": part["page"]["pdf_page"],
                "printed_page": part["page"].get("printed_page"),
                "segment_id": part["page"]["segment_id"],
                "bbox": part["bbox"],
            }
            for part in signature.get("parts", ())
        ]
        if (
            rendered.get("kind") != signature.get("role")
            or rendered.get("confidence") != signature.get("confidence")
            or rendered.get("parts") != expected_parts
            or rendered.get("covered_child_labels", [])
            != signature.get("covered_child_labels", [])
            or rendered.get("source_document", "solution")
            != signature.get("source_document", "solution")
            or rendered.get("review_reason") != signature.get("review_reason")
        ):
            raise SnapshotError("materialized asset manifest geometry changed")
        output_file = _safe_relative(
            rendered.get("output_file"), "materialized asset output_file"
        )
        output_path = Path(output_file)
        if not output_path.parts or output_path.parts[0] != "solutions":
            output_file = (Path("solutions") / output_path).as_posix()
        output_sha256 = rendered.get("output_sha256")
        if not _is_sha256(output_sha256):
            raise SnapshotError("materialized asset output SHA-256 is invalid")
        asset_records.append(
            {
                "signature": _clone(signature),
                "output_file": output_file,
                "output_sha256": output_sha256,
                "selection_status": rendered.get("selection_status"),
                **({'original_signature': _clone(original_signature)} if signature != original_signature else {}),
            }
        )

    candidate_similarity: dict[tuple[str, str], int] = {}
    for pair in legacy_candidate_pairs:
        if pair.problem_id in problems and pair.asset_id in rendered_assets:
            candidate_similarity[(pair.problem_id, pair.asset_id)] = max(
                0, min(1000, round(pair.fingerprint_similarity * 1000))
            )
    for decision in decisions:
        problem = problems.get(decision.item_id)
        if problem is None:
            continue
        for asset_id in dict.fromkeys(
            decision.answer_ids + decision.explanation_ids + decision.candidate_ids
        ):
            asset = assets.get(asset_id)
            if asset is None or asset_id not in rendered_assets:
                continue
            candidate_similarity.setdefault(
                (problem.item_id, asset_id),
                max(
                    0,
                    min(
                        1000,
                        round(
                            sl.fingerprint_similarity(
                                problem.fingerprints, asset.fingerprints
                            )
                            * 1000
                        ),
                    ),
                ),
            )

    group_data: dict[tuple[str, str, str], dict[str, Any]] = {}
    for (unit_id, asset_id), similarity in sorted(candidate_similarity.items()):
        problem = problems[unit_id]
        asset = assets[asset_id]
        anchor = problem.anchor
        anchorless = anchor is None
        if anchorless:
            observed = verifications.get((unit_id, asset_id))
            segments = {part.page.segment_id for part in asset.parts}
            if observed is None or observed.quality_receipt is None or len(segments) != 1:
                continue
            segment_id = next(iter(segments))
        elif not anchor.resolved or not anchor.segment_id:
            continue
        else:
            segment_id = anchor.segment_id
        roles = {
            ROLE_QUICK
            if asset.role == sl.SolutionRole.QUICK_ANSWER
            else ROLE_DETAIL
        }
        if asset.role == sl.SolutionRole.MIXED:
            roles = set(PLANNABLE_ROLES)
        for role in sorted(roles):
            key = (problem.scope_id, segment_id, role)
            group = group_data.setdefault(
                key,
                {
                    "problem_scope_id": problem.scope_id,
                    "segment_id": segment_id,
                    "role": role,
                    "unit_ids": set(),
                    "asset_ids": set(),
                    "candidate_edges": [],
                },
            )
            if anchorless:
                group['source_binding'] = {'contract': ANCHORLESS_GROUP_CONTRACT,
                    'problem_source_sha256': problem_index.source_sha256,
                    'solution_source_sha256': scope_contract['provenance']['solution_source_sha256']}
            group["unit_ids"].add(unit_id)
            group["asset_ids"].add(asset_id)
            group["candidate_edges"].append(
                {
                    "unit_id": unit_id,
                    "asset_id": asset_id,
                    "fingerprint_similarity_milli": similarity,
                }
            )

    projected_groups = [
        {
            **group,
            "unit_ids": list(group["unit_ids"]),
            "asset_ids": list(group["asset_ids"]),
        }
        for group in group_data.values()
        if group["candidate_edges"]
    ]
    projected_pairs = {
        (edge["unit_id"], edge["asset_id"])
        for group in projected_groups
        for edge in group["candidate_edges"]
    }

    verification_records = {
        (str(record.get("unit_id")), str(record.get("asset_id"))): record
        for record in pair_verification_records
        if isinstance(record, Mapping)
    }
    if len(verification_records) != len(pair_verification_records):
        raise SnapshotError("pair verification references are duplicated or invalid")
    if set(verification_records) != set(verifications):
        raise SnapshotError("pair verification file references are incomplete")
    if set(verifications) - projected_pairs:
        raise SnapshotError("verified pair is absent from projected candidate edges")
    for pair, verification in verifications.items():
        record = verification_records[pair]
        if record.get('evidence_kind') != 'nonapproval' and verification.verifier == 'astra-current-evidence' and verification.quality_receipt is not None and verification.evidence_root is not None:
            record = {**record, 'verifier': verification.verifier, 'provider_refs': [], 'quality_receipt': _clone(verification.quality_receipt), 'evidence_root': str(inside(verification.evidence_root, 'snapshot evidence root'))}
            verification_records[pair] = record
        expected = {
            "same_item": verification.same_item,
            "confidence": verification.confidence,
            "verifier": verification.verifier,
            "explicit_answer_conflict": verification.explicit_answer_conflict,
            "corroborating_asset_id": verification.corroborating_asset_id,
        }
        if any(record.get(field) != value for field, value in expected.items()):
            raise SnapshotError("pair verification result/reference mismatch")

    provenance = scope_contract.get("provenance")
    if not isinstance(provenance, Mapping):
        raise SnapshotError("scope contract provenance is unavailable")
    baseline = [
        {
            "unit_id": decision.item_id,
            "decision": sl.serialize_link_decision(decision, rendered_assets),
        }
        for decision in decisions
    ]
    return build_alignment_snapshot(
        problem_index=problem_index.signature(),
        solution_assets=asset_records,
        projected_groups=projected_groups,
        pair_verifications=list(verification_records.values()),
        baseline_decisions=baseline,
        scope_contract=scope_contract,
        alignment_index={
            "relative_path": provenance.get("alignment_index_path"),
            "sha256": provenance.get("alignment_index_sha256"),
        },
        link_cache_key=link_cache_key,
        versions=versions,
        problem_source_sha256=problem_index.source_sha256,
        solution_source_sha256=str(provenance.get("solution_source_sha256", "")),
    )


def write_alignment_snapshot(
    path: Path, snapshot: Mapping[str, Any], *, run_dir: Path
) -> str:
    """Atomically write a structurally and source-file validated snapshot."""

    validate_alignment_snapshot(snapshot, run_dir=run_dir)
    root = run_dir.resolve()
    output = path.resolve()
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise SnapshotError("alignment snapshot output escapes the run directory") from exc
    encoded = canonical_json_bytes(snapshot) + b"\n"
    file_sha256_value = hashlib.sha256(encoded).hexdigest()
    expected_relative = alignment_snapshot_relative_path(file_sha256_value)
    if output.relative_to(root).as_posix() != expected_relative:
        raise SnapshotError("alignment snapshot path is not content-addressed")
    inside(output.resolve(), "snapshot output")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if file_sha256(output) != file_sha256_value:
            raise SnapshotError("content-addressed alignment snapshot is corrupt")
        return file_sha256_value
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=output.parent, delete=False
        ) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        os.replace(temporary, output)
    finally:
        if temporary is not None and Path(temporary).exists():
            Path(temporary).unlink()
    return file_sha256_value


def alignment_snapshot_file_sha256(snapshot: Mapping[str, Any]) -> str:
    validate_alignment_snapshot(snapshot)
    return hashlib.sha256(canonical_json_bytes(snapshot) + b"\n").hexdigest()


def alignment_snapshot_relative_path(file_sha256_value: str) -> str:
    if not _is_sha256(file_sha256_value):
        raise SnapshotError("alignment snapshot file SHA-256 is invalid")
    return f"solutions/alignment_snapshot-{file_sha256_value}.json"


def read_alignment_snapshot(
    path: Path,
    *,
    run_dir: Path,
    expected_file_sha256: str,
    expected_problem_source_sha256: str,
    expected_solution_source_sha256: str,
    expected_link_cache_key: str,
    expected_versions: Mapping[str, str],
    expected_alignment_index_sha256: str,
) -> dict[str, Any]:
    """Read and verify every CAS/source/version/materialized-file binding."""

    root = run_dir.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(root)
    except ValueError as exc:
        raise SnapshotError("alignment snapshot path escapes the run directory") from exc
    if (
        not _is_sha256(expected_file_sha256)
        or not _is_sha256(expected_problem_source_sha256)
        or not _is_sha256(expected_solution_source_sha256)
        or not _is_sha256(expected_alignment_index_sha256)
        or not resolved_path.is_file()
    ):
        raise SnapshotError("alignment snapshot file reference is invalid")
    raw = resolved_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_file_sha256:
        raise SnapshotError("alignment snapshot file SHA-256 mismatch")
    try:
        snapshot = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError("alignment snapshot is not valid UTF-8 JSON") from exc
    validate_alignment_snapshot(snapshot, run_dir=run_dir)
    if snapshot["problem_source_sha256"] != expected_problem_source_sha256:
        raise SnapshotError("alignment snapshot problem source changed")
    if snapshot["solution_source_sha256"] != expected_solution_source_sha256:
        raise SnapshotError("alignment snapshot solution source changed")
    if snapshot["link_cache_key"] != expected_link_cache_key:
        raise SnapshotError("alignment snapshot link cache key changed")
    if snapshot["versions"] != dict(expected_versions):
        raise SnapshotError("alignment snapshot versions changed")
    if snapshot["alignment_index"]["sha256"] != expected_alignment_index_sha256:
        raise SnapshotError("alignment index reference changed")
    return snapshot


def alignment_snapshot_reference(relative_path: str, file_sha256_value: str) -> dict[str, Any]:
    _safe_relative(relative_path, "alignment snapshot relative_path")
    if not _is_sha256(file_sha256_value):
        raise SnapshotError("alignment snapshot reference SHA-256 is invalid")
    if relative_path != alignment_snapshot_relative_path(file_sha256_value):
        raise SnapshotError("alignment snapshot reference is not content-addressed")
    return {
        "relative_path": relative_path,
        "sha256": file_sha256_value,
        "schema_version": ALIGNMENT_SNAPSHOT_SCHEMA_VERSION,
    }


def add_alignment_snapshot_contract(
    link_manifest: Mapping[str, Any], snapshot_reference: Mapping[str, Any]
) -> dict[str, Any]:
    """Add the four v1 propagation fields without changing legacy fields."""

    if not isinstance(link_manifest, Mapping):
        raise SnapshotError("link manifest must be an object")
    if not isinstance(snapshot_reference, Mapping) or set(snapshot_reference) != {
        "relative_path",
        "sha256",
        "schema_version",
    }:
        raise SnapshotError("alignment snapshot reference fields are invalid")
    expected_reference = alignment_snapshot_reference(
        snapshot_reference.get("relative_path"), snapshot_reference.get("sha256")
    )
    if snapshot_reference.get("schema_version") != ALIGNMENT_SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotError("alignment snapshot reference version is invalid")
    result = _clone(link_manifest)
    additions = {
        "manual_propagation_contract": MANUAL_PROPAGATION_CONTRACT,
        "alignment_snapshot": expected_reference,
        "manual_constraints": [],
        "manual_confirmation_events": [],
    }
    for key, value in additions.items():
        if key in result and result[key] != value:
            raise SnapshotError(f"existing {key} conflicts with fixed-edge-v1")
        result.setdefault(key, value)
    return result


def problem_identity_sha256(snapshot: Mapping[str, Any], unit_id: str) -> str:
    validate_alignment_snapshot(snapshot)
    for item in snapshot["problem_index"]["items"]:
        if item["item_id"] == unit_id:
            return canonical_sha256(item)
    raise FixedEdgeError("manual constraint references an unknown problem item")


def asset_identity_sha256(snapshot: Mapping[str, Any], asset_id: str) -> str:
    validate_alignment_snapshot(snapshot)
    for asset in snapshot["solution_assets"]:
        if asset["signature"]["asset_id"] == asset_id:
            return _asset_record_identity(asset)
    raise FixedEdgeError("manual constraint references an unknown solution asset")


def _snapshot_maps(snapshot: Mapping[str, Any]) -> tuple[
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
    dict[tuple[str, str, str], Mapping[str, Any]],
    dict[tuple[str, str], Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
]:
    problems = {item["item_id"]: item for item in snapshot["problem_index"]["items"]}
    assets = {
        record["signature"]["asset_id"]: record
        for record in snapshot["solution_assets"]
    }
    groups = {
        (group["problem_scope_id"], group["segment_id"], group["role"]): group
        for group in snapshot["projected_groups"]
    }
    verifications = {
        (record["unit_id"], record["asset_id"]): record
        for record in snapshot["pair_verifications"]
    }
    decisions = {
        record["unit_id"]: record["decision"]
        for record in snapshot["baseline_decisions"]
    }
    return problems, assets, groups, verifications, decisions


def _manual_baseline_edges(
    assets: Mapping[str, Mapping[str, Any]],
    decisions: Mapping[str, Mapping[str, Any]],
) -> set[tuple[str, str, str]]:
    linked: set[tuple[str, str, str]] = set()
    referenced_manual_assets: set[str] = set()
    for unit_id, decision in decisions.items():
        has_role_confirmations = "role_confirmations" in decision
        role_confirmations = decision.get("role_confirmations", {})
        legacy_confirmation = decision.get("confirmation")
        for role, field in (
            (ROLE_QUICK, "answer_ids"),
            (ROLE_DETAIL, "explanation_ids"),
        ):
            role_confirmation = (
                role_confirmations.get(role)
                if isinstance(role_confirmations, Mapping)
                else None
            )
            role_is_manual = bool(
                isinstance(role_confirmation, Mapping)
                and role_confirmation.get("method") == "manual"
            ) if has_role_confirmations else bool(
                isinstance(legacy_confirmation, Mapping)
                and legacy_confirmation.get("method") == "manual"
            )
            for asset_id in decision.get(field, ()):
                asset_is_manual = (
                    assets[asset_id]["selection_status"] == "manual_verified"
                )
                if not asset_is_manual and not role_is_manual:
                    continue
                linked.add((unit_id, role, asset_id))
                if asset_is_manual:
                    referenced_manual_assets.add(asset_id)
                if asset_is_manual and not role_is_manual:
                    raise FixedEdgeError(
                        "manual-confirmed baseline edge lacks confirmation provenance"
                    )
    orphaned = {
        asset_id
        for asset_id, record in assets.items()
        if record["selection_status"] == "manual_verified"
        and asset_id not in referenced_manual_assets
    }
    if orphaned:
        raise FixedEdgeError("manual-verified baseline assets must remain linked")
    return linked


def make_manual_constraint(
    snapshot: Mapping[str, Any],
    *,
    confirmation_id: str,
    unit_id: str,
    role: str,
    asset_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Build a source-bound constraint from one materialized candidate edge."""

    validate_alignment_snapshot(snapshot)
    problems, assets, groups, _, _ = _snapshot_maps(snapshot)
    if role not in PLANNABLE_ROLES or unit_id not in problems or asset_id not in assets:
        raise FixedEdgeError("manual constraint identity/role is invalid")
    matches = [
        (key, group)
        for key, group in groups.items()
        if key[2] == role
        and unit_id in group["unit_ids"]
        and asset_id in group["asset_ids"]
        and any(
            edge["unit_id"] == unit_id and edge["asset_id"] == asset_id
            for edge in group["candidate_edges"]
        )
    ]
    if len(matches) != 1:
        raise FixedEdgeError("selected asset is not one unique materialized candidate")
    (scope_id, segment_id, _), _ = matches[0]
    constraint = {
        "confirmation_id": confirmation_id,
        "unit_id": unit_id,
        "role": role,
        "asset_id": asset_id,
        "problem_identity_sha256": problem_identity_sha256(snapshot, unit_id),
        "asset_identity_sha256": asset_identity_sha256(snapshot, asset_id),
        "scope_id": scope_id,
        "segment_id": segment_id,
        "created_at": created_at,
    }
    _validate_constraints(snapshot, [constraint])
    return constraint


def _validate_constraints(
    snapshot: Mapping[str, Any], constraints: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    problems, assets, groups, _, _ = _snapshot_maps(snapshot)
    normalized: list[dict[str, Any]] = []
    confirmation_ids: set[str] = set()
    unit_edges: set[tuple[str, str]] = set()
    asset_edges: set[tuple[str, str]] = set()
    by_group: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for raw in constraints:
        if not isinstance(raw, Mapping) or set(raw) != CONSTRAINT_FIELDS:
            raise FixedEdgeError("manual constraint fields are invalid")
        constraint = _clone(raw)
        confirmation_id = _nonempty(
            constraint.get("confirmation_id"), "confirmation_id", FixedEdgeError
        )
        unit_id = _nonempty(constraint.get("unit_id"), "unit_id", FixedEdgeError)
        role = constraint.get("role")
        asset_id = _nonempty(constraint.get("asset_id"), "asset_id", FixedEdgeError)
        scope_id = _nonempty(constraint.get("scope_id"), "scope_id", FixedEdgeError)
        segment_id = _nonempty(
            constraint.get("segment_id"), "segment_id", FixedEdgeError
        )
        _nonempty(constraint.get("created_at"), "created_at", FixedEdgeError)
        if role not in PLANNABLE_ROLES or unit_id not in problems or asset_id not in assets:
            raise FixedEdgeError("manual constraint identity/role is invalid")
        asset_role = assets[asset_id]["signature"]["role"]
        if asset_role == ROLE_MIXED:
            raise FixedEdgeError("mixed assets cannot be fixed in contract v1")
        if asset_role != role:
            raise FixedEdgeError("manual constraint asset has the wrong role")
        if constraint.get("problem_identity_sha256") != canonical_sha256(problems[unit_id]):
            raise FixedEdgeError("manual constraint problem identity changed")
        if constraint.get("asset_identity_sha256") != _asset_record_identity(
            assets[asset_id]
        ):
            raise FixedEdgeError("manual constraint asset identity changed")
        key = (scope_id, segment_id, role)
        group = groups.get(key)
        if group is None or unit_id not in group["unit_ids"] or asset_id not in group["asset_ids"]:
            raise FixedEdgeError("manual constraint scope/segment projection changed")
        if not any(
            edge["unit_id"] == unit_id and edge["asset_id"] == asset_id
            for edge in group["candidate_edges"]
        ):
            raise FixedEdgeError("selected asset is not a materialized candidate for the item")
        if confirmation_id in confirmation_ids:
            raise FixedEdgeError("confirmation ids must be unique")
        if (unit_id, role) in unit_edges:
            raise FixedEdgeError("duplicate unit/role manual edge")
        if (asset_id, role) in asset_edges:
            raise FixedEdgeError("duplicate asset/role manual edge")
        confirmation_ids.add(confirmation_id)
        unit_edges.add((unit_id, role))
        asset_edges.add((asset_id, role))
        normalized.append(constraint)
        by_group.setdefault(key, []).append(constraint)
    for key, fixed in by_group.items():
        fixed.sort(key=lambda value: problems[value["unit_id"]]["sequence"])
        asset_sequences = [assets[value["asset_id"]]["signature"]["sequence"] for value in fixed]
        if any(right <= left for left, right in zip(asset_sequences, asset_sequences[1:])):
            raise FixedEdgeError(f"crossing manual constraints in {key[0]}/{key[1]}/{key[2]}")
    return sorted(
        normalized,
        key=lambda value: (
            value["scope_id"],
            value["segment_id"],
            value["role"],
            problems[value["unit_id"]]["sequence"],
        ),
    )


def _production_problem(record: Mapping[str, Any]) -> sl.ProblemItem:
    anchor_record = record.get("anchor")
    anchor = None
    if isinstance(anchor_record, Mapping):
        range_record = anchor_record.get("range")
        if not isinstance(range_record, Mapping):
            raise SnapshotError("problem anchor range is invalid")
        start = range_record.get("printed_start", range_record.get("start"))
        end = range_record.get("printed_end", range_record.get("end"))
        anchor = sl.AnchorSpan(
            anchor_ids=tuple(anchor_record.get("anchor_ids", ())),
            item_ids=tuple(anchor_record.get("item_ids", ())),
            scope_id=str(anchor_record.get("scope_id", "")),
            segment_id=str(anchor_record.get("segment_id", "")),
            page_range=sl.PrintedPageRange(
                start=start,
                end=end,
                mode=sl.RangeMode(range_record.get("mode")),
                raw=str(range_record.get("raw", "")),
            ),
            confidence=float(anchor_record.get("confidence", 0.0)),
            resolved=bool(anchor_record.get("resolved")),
            boundary_source=anchor_record.get("boundary_source"),
            review_reasons=tuple(anchor_record.get("review_reasons", ())),
        )
    return sl.ProblemItem(
        item_id=str(record["item_id"]),
        sequence=int(record["sequence"]),
        scope_id=str(record["scope_id"]),
        parent_number=str(record.get("parent_number", "")),
        subquestion_label=record.get("subquestion_label"),
        anchor=anchor,
        fingerprints=tuple(record.get("fingerprints", ())),
        crop_verified=bool(record.get("crop_verified", True)),
        group_label=record.get("group_label"),
        column=record.get("column"),
        owner_id=record.get("owner_id"),
        crop_search_eligible=record.get("crop_search_eligible"),
        cache_only_detailed_review=bool(
            record.get("cache_only_detailed_review", False)
        ),
    )


def _production_asset(
    record: Mapping[str, Any], *, projected_scope: str, projected_segment: str
) -> sl.SolutionAsset:
    signature = record["signature"]
    return sl.SolutionAsset(
        asset_id=str(signature["asset_id"]),
        role=sl.SolutionRole(signature["role"]),
        sequence=int(signature["sequence"]),
        # Phase 2 alignment is the authority for scope projection.  Everything
        # else is still checked by production's strict compatibility predicate.
        scope_id=projected_scope,
        parent_number=str(signature.get("parent_number", "")),
        subquestion_label=signature.get("subquestion_label"),
        parts=tuple(
            sl.SolutionPart(
                page=sl.PageIdentity(
                    pdf_index=int(part["page"]["pdf_index"]),
                    pdf_page=int(part["page"]["pdf_page"]),
                    printed_page=part["page"].get("printed_page"),
                    segment_id=projected_segment,
                ),
                bbox=tuple(part["bbox"]),
                order=int(part.get("order", 0)),
            )
            for part in signature.get("parts", ())
        ),
        fingerprints=tuple(signature.get("fingerprints", ())),
        explicit_answer=signature.get("explicit_answer"),
        confidence=float(signature.get("confidence", 0.0)),
        covered_child_labels=tuple(signature.get("covered_child_labels", ())),
        source_document=str(signature.get("source_document", "solution")),
        review_reason=signature.get("review_reason"),
    )


def _projected_problem(record: Mapping[str, Any]) -> sl.ProblemItem:
    """Keep production gates while making trusted scope projection the location."""

    problem = _production_problem(record)
    if problem.anchor is None:
        return problem
    projected_range = sl.PrintedPageRange(
        _PROJECTED_LOCATION_PAGE,
        _PROJECTED_LOCATION_PAGE,
        sl.RangeMode.EXACT,
        "trusted_scope_projection",
    )
    return replace(problem, anchor=replace(problem.anchor, page_range=projected_range))


def _original_asset(record: Mapping[str, Any]) -> sl.SolutionAsset:
    signature = record.get('original_signature', record['signature'])
    asset = _production_asset({**record, 'signature': signature}, projected_scope=signature['scope_id'], projected_segment=signature['parts'][0]['page']['segment_id'])
    if 'expanded_source_binding' in signature:
        asset = replace(asset, expanded_source_binding=sl.ExpandedSourceBinding(**signature['expanded_source_binding']))
    return replace(asset, parts=tuple(replace(part, page=replace(part.page, segment_id=source['page']['segment_id'])) for part, source in zip(asset.parts, signature['parts'], strict=True)))


def _projected_asset(
    record: Mapping[str, Any], *, projected_scope: str, projected_segment: str
) -> sl.SolutionAsset:
    asset = _production_asset(
        record,
        projected_scope=projected_scope,
        projected_segment=projected_segment,
    )
    return replace(
        asset,
        parts=tuple(
            replace(
                part,
                page=replace(part.page, printed_page=_PROJECTED_LOCATION_PAGE),
            )
            for part in asset.parts
        ),
    )


def _production_verification(
    record: Mapping[str, Any] | None,
) -> sl.PairVerification | None:
    if record is None:
        return None
    verifier = str(record["verifier"])
    # The Phase 2c scope contract has the same dual/same-item/conflict quality
    # semantics but a separate cache namespace.  Normalize only the verifier
    # label at this adapter boundary so production DP remains the sole scorer.
    if verifier == sl.DUAL_SCOPE_VERIFIER:
        verifier = sl.DUAL_VERIFIER
    return sl.PairVerification(
        same_item=bool(record["same_item"]),
        confidence=float(record["confidence"]),
        verifier=verifier,
        explicit_answer_conflict=bool(record["explicit_answer_conflict"]),
        corroborating_asset_id=record.get("corroborating_asset_id"),
        quality_receipt=record.get('quality_receipt'),
        evidence_root=inside(Path(record['evidence_root']), 'snapshot evidence root') if 'quality_receipt' in record else None,
    )


def _strong(verification: Mapping[str, Any] | None, problem: Mapping[str, Any], asset: Mapping[str, Any]) -> bool:
    return sl._strong_verification(
        _production_verification(verification), sl.MIN_LINK_CONFIDENCE, _production_problem(problem), _original_asset(asset)
    )


def _align_interval(
    unit_ids: Sequence[str],
    asset_ids: Sequence[str],
    candidate_edges: Mapping[tuple[str, str], Mapping[str, Any]],
    verifications: Mapping[tuple[str, str], Mapping[str, Any]],
    problems: Mapping[str, Mapping[str, Any]],
    assets: Mapping[str, Mapping[str, Any]],
    *,
    projected_scope: str,
    projected_segment: str,
) -> tuple[dict[str, str], dict[str, tuple[str, ...]], dict[str, float]]:
    """Narrow projection adapter over production's verified 4-tuple DP."""

    production_problems = [_production_problem(problems[unit_id]) for unit_id in unit_ids]
    production_assets = [
        _original_asset(assets[asset_id])
        for asset_id in asset_ids
    ]
    admitted = frozenset(pair for pair in candidate_edges if pair[0] in unit_ids and pair[1] in asset_ids and sl._strict_compatible(_projected_problem(problems[pair[0]]), _projected_asset(assets[pair[1]], projected_scope=projected_scope, projected_segment=projected_segment)))
    production_verifications = {
        pair: converted
        for pair, raw in verifications.items()
        if pair[0] in unit_ids
        and pair[1] in asset_ids
        and (converted := _production_verification(raw)) is not None
    }
    allowed = set(candidate_edges)
    blocked = {
        (unit_id, asset_id)
        for unit_id in unit_ids
        for asset_id in asset_ids
        if (unit_id, asset_id) not in allowed
    }
    blocked.update(
        pair
        for pair, verification in production_verifications.items()
        if not verification.same_item and not verification.explicit_answer_conflict
    )
    aligned = sl._align_group(
        production_problems, production_assets, production_verifications, blocked, admitted_pairs=admitted
    )
    return (
        {unit_id: asset.asset_id for unit_id, asset in aligned.selected.items()},
        {
            unit_id: tuple(asset.asset_id for asset in possible)
            for unit_id, possible in aligned.possible.items()
        },
        dict(aligned.runner_up_margin),
    )


def _verification_review_reason(verification: Mapping[str, Any] | None) -> str:
    if verification is None:
        return "independent_verification_missing"
    if verification["explicit_answer_conflict"]:
        return "explicit_answer_conflict"
    return "independent_verification_failed"


def _same_item_strong_detail(
    unit_id: str,
    quick_verification: Mapping[str, Any] | None,
    *,
    problems: Mapping[str, Mapping[str, Any]],
    assets: Mapping[str, Mapping[str, Any]],
    verifications: Mapping[tuple[str, str], Mapping[str, Any]],
) -> str | None:
    if quick_verification is None:
        return None
    detail_id = quick_verification.get("corroborating_asset_id")
    if not isinstance(detail_id, str) or detail_id not in assets:
        return None
    detail = assets[detail_id]
    if detail["signature"].get("role") not in {ROLE_DETAIL, ROLE_MIXED}:
        return None
    detail_verification = verifications.get((unit_id, detail_id))
    if not _strong(detail_verification, problems[unit_id], detail):
        return None
    problem = _projected_problem(problems[unit_id])
    if problem.anchor is None:
        return None
    projected = _projected_asset(
        detail,
        projected_scope=problem.scope_id,
        projected_segment=problem.anchor.segment_id,
    )
    return detail_id if sl._strict_compatible(problem, projected) else None


def _automatic_review_reason(
    *,
    unit_id: str,
    role: str,
    asset_id: str,
    margin: float,
    problems: Mapping[str, Mapping[str, Any]],
    assets: Mapping[str, Mapping[str, Any]],
    verifications: Mapping[tuple[str, str], Mapping[str, Any]],
    projected_scope: str,
    projected_segment: str,
) -> str | None:
    problem = _projected_problem(problems[unit_id])
    asset = _projected_asset(
        assets[asset_id],
        projected_scope=projected_scope,
        projected_segment=projected_segment,
    )
    verification = verifications.get((unit_id, asset_id))
    if not problem.crop_is_search_eligible:
        return "problem_crop_unverified"
    if problem.cache_only_detailed_review:
        return "cached_detailed_solution_review"
    if (anchor_reason := sl._anchor_review_reason(problem)) is not None:
        return anchor_reason
    if asset.review_reason is not None:
        return asset.review_reason
    if not sl._strict_compatible(problem, asset):
        return "production_candidate_incompatible"
    if margin < MIN_RUNNER_UP_MARGIN:
        return "runner_up_margin_below_threshold"
    if not _strong(verification, problems[unit_id], assets[asset_id]):
        return _verification_review_reason(verification)
    strong_detail_id = (
        asset_id
        if role == ROLE_DETAIL
        else _same_item_strong_detail(
            unit_id,
            verification,
            problems=problems,
            assets=assets,
            verifications=verifications,
        )
    )
    anchor_reasons = tuple(problem.anchor.review_reasons if problem.anchor else ())
    unresolved_anchor_reasons = tuple(
        reason
        for reason in anchor_reasons
        if reason != "anchor_window_requires_verification" or strong_detail_id is None
    )
    if unresolved_anchor_reasons:
        return unresolved_anchor_reasons[0]
    if role == ROLE_QUICK and strong_detail_id is None:
        return "quick_requires_verified_detail"
    return None


def _asset_printed_pages(asset: Mapping[str, Any]) -> tuple[int, ...]:
    pages = {
        int(part["page"]["printed_page"])
        for part in asset["signature"].get("parts", [])
        if isinstance(part, dict)
        and isinstance(part.get("page"), dict)
        and part["page"].get("printed_page") is not None
    }
    return tuple(sorted(pages))


def _roles_for_asset(asset: Mapping[str, Any]) -> set[str]:
    role = asset["signature"]["role"]
    return {ROLE_QUICK, ROLE_DETAIL} if role == ROLE_MIXED else {role}


def _apply_role_result(
    decision: Mapping[str, Any],
    *,
    unit_id: str,
    role: str,
    result: str,
    reason: str,
    asset_id: str | None,
    possible_ids: Sequence[str],
    assets: Mapping[str, Mapping[str, Any]],
    verification: Mapping[str, Any] | None,
    confirmation_ids: Sequence[str] = (),
) -> dict[str, Any]:
    updated = copy.deepcopy(dict(decision))
    field = "answer_ids" if role == ROLE_QUICK else "explanation_ids"
    other = "explanation_ids" if role == ROLE_QUICK else "answer_ids"
    status_field = "answer_status" if role == ROLE_QUICK else "explanation_status"
    other_status_field = (
        "explanation_status" if role == ROLE_QUICK else "answer_status"
    )
    evidence_field = "answer_evidence" if role == ROLE_QUICK else "explanation_evidence"
    review_field = (
        "answer_review_reasons"
        if role == ROLE_QUICK
        else "explanation_review_reasons"
    )
    confidence_field = (
        "answer_confidence" if role == ROLE_QUICK else "explanation_confidence"
    )
    confirmation_field = (
        "answer_confirmation" if role == ROLE_QUICK else "explanation_confirmation"
    )
    preserved_other = {
        key: copy.deepcopy(updated.get(key))
        for key in (
            other,
            other_status_field,
            "explanation_evidence" if role == ROLE_QUICK else "answer_evidence",
            "explanation_confidence" if role == ROLE_QUICK else "answer_confidence",
            "explanation_confirmation" if role == ROLE_QUICK else "answer_confirmation",
            "explanation_review_reasons"
            if role == ROLE_QUICK
            else "answer_review_reasons",
        )
        if key in updated
    }
    updated[field] = [asset_id] if result in {"manual_verified", "auto_verified"} else []
    candidates = list(updated.get("candidate_ids", []))
    for candidate_id in possible_ids:
        if candidate_id not in candidates:
            candidates.append(candidate_id)
    updated["candidate_ids"] = candidates
    evidence = list(updated.get("evidence", []))
    marker = "manual_confirmation" if result == "manual_verified" else "fixed_edge_realign"
    if marker not in evidence:
        evidence.append(marker)
    updated["evidence"] = evidence
    role_evidence = list(updated.get(evidence_field, []))
    if marker not in role_evidence:
        role_evidence.append(marker)
    updated[evidence_field] = role_evidence
    updated[review_field] = [] if result != "review" else [reason]
    updated[status_field] = (
        "verified"
        if result in {"manual_verified", "auto_verified"}
        else "review"
        if possible_ids
        else "unmatched"
    )
    linked_ids = list(updated.get("answer_ids", [])) + list(
        updated.get("explanation_ids", [])
    )
    if linked_ids:
        updated["status"] = "verified"
    else:
        updated["status"] = "review"
    review_reasons = list(updated.get("review_reasons", []))
    if result == "review" and reason not in review_reasons:
        review_reasons.append(reason)
    updated["review_reasons"] = review_reasons
    if result == "manual_verified":
        updated[confidence_field] = 1.0
        role_confirmation = {
            "method": "manual",
            "confirmation_ids": list(confirmation_ids),
        }
        updated[confirmation_field] = role_confirmation
        role_confirmations = copy.deepcopy(updated.get("role_confirmations", {}))
        if not isinstance(role_confirmations, dict):
            role_confirmations = {}
        role_confirmations[role] = role_confirmation
        updated["role_confirmations"] = role_confirmations
        existing_confirmation = updated.get("confirmation")
        if not isinstance(existing_confirmation, dict):
            updated["confirmation"] = copy.deepcopy(role_confirmation)
    elif result == "auto_verified" and asset_id is not None and verification is not None:
        updated[confidence_field] = max(
            float(assets[asset_id]["signature"].get("confidence", 0.0)),
            float(verification["confidence"]),
        )
    else:
        updated[confidence_field] = 0.0
    updated["solution_printed_pages"] = sorted(
        {
            page
            for linked_id in linked_ids
            for page in _asset_printed_pages(assets[linked_id])
        }
    )
    for key, value in preserved_other.items():
        if updated.get(key) != value:
            raise FixedEdgeError(f"fixed-edge planning changed other-role {key}")
    answer_status = updated.get(
        "answer_status", "verified" if updated.get("answer_ids") else "unmatched"
    )
    explanation_status = updated.get(
        "explanation_status",
        "verified" if updated.get("explanation_ids") else "unmatched",
    )
    if answer_status == explanation_status == "verified":
        overall = "complete"
    elif "conflict" in {answer_status, explanation_status}:
        overall = "conflict"
    elif "verified" in {answer_status, explanation_status}:
        overall = "partial"
    elif "review" in {answer_status, explanation_status}:
        overall = "review"
    else:
        overall = "unmatched"
    updated["overall_status"] = overall
    return updated


def plan_fixed_edges(
    snapshot: Mapping[str, Any], constraints: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Revalidate bound source evidence and plan without provider calls."""

    validate_alignment_snapshot(snapshot)
    problems, assets, groups, verifications, baseline = _snapshot_maps(snapshot)
    fixed = _validate_constraints(snapshot, constraints)
    required_manual = _manual_baseline_edges(assets, baseline)
    supplied_manual = {
        (constraint["unit_id"], constraint["role"], constraint["asset_id"])
        for constraint in fixed
    }
    if not required_manual.issubset(supplied_manual):
        raise FixedEdgeError(
            "all existing manual-confirmed baseline edges require fixed constraints"
        )
    prospective = copy.deepcopy(baseline)
    prospective_asset_status = {
        asset_id: record["selection_status"] for asset_id, record in assets.items()
    }
    by_group: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for constraint in fixed:
        key = (constraint["scope_id"], constraint["segment_id"], constraint["role"])
        by_group.setdefault(key, []).append(constraint)
    effects: list[dict[str, Any]] = []
    impacted: set[tuple[str, str]] = set()
    impacted_assets: set[str] = set()
    for key, group_fixed in sorted(by_group.items()):
        group = groups[key]
        role = key[2]
        impacted_assets.update(group["asset_ids"])
        unit_ids = sorted(group["unit_ids"], key=lambda unit_id: problems[unit_id]["sequence"])
        asset_ids = sorted(
            (
                asset_id
                for asset_id in group["asset_ids"]
                if assets[asset_id]["signature"]["role"] != ROLE_MIXED
            ),
            key=lambda asset_id: assets[asset_id]["signature"]["sequence"],
        )
        candidate_edges = {
            (edge["unit_id"], edge["asset_id"]): edge
            for edge in group["candidate_edges"]
            if assets[edge["asset_id"]]["signature"]["role"] != ROLE_MIXED
        }
        group_fixed.sort(key=lambda value: problems[value["unit_id"]]["sequence"])
        fixed_by_unit = {value["unit_id"]: value for value in group_fixed}
        selected: dict[str, str] = {}
        possible: dict[str, tuple[str, ...]] = {}
        margins: dict[str, float] = {}
        boundaries: list[tuple[int | None, int | None]] = [(None, None)] + [
            (problems[value["unit_id"]]["sequence"], assets[value["asset_id"]]["signature"]["sequence"])
            for value in group_fixed
        ] + [(None, None)]
        for index in range(len(boundaries) - 1):
            lower_unit, lower_asset = boundaries[index]
            upper_unit, upper_asset = boundaries[index + 1]
            interval_units = [
                unit_id
                for unit_id in unit_ids
                if unit_id not in fixed_by_unit
                and (lower_unit is None or problems[unit_id]["sequence"] > lower_unit)
                and (upper_unit is None or problems[unit_id]["sequence"] < upper_unit)
            ]
            interval_assets = [
                asset_id
                for asset_id in asset_ids
                if asset_id not in {value["asset_id"] for value in group_fixed}
                and (lower_asset is None or assets[asset_id]["signature"]["sequence"] > lower_asset)
                and (upper_asset is None or assets[asset_id]["signature"]["sequence"] < upper_asset)
            ]
            interval_selected, interval_possible, interval_margins = _align_interval(
                interval_units,
                interval_assets,
                candidate_edges,
                verifications,
                problems,
                assets,
                projected_scope=key[0],
                projected_segment=key[1],
            )
            selected.update(interval_selected)
            possible.update(interval_possible)
            margins.update(interval_margins)
        for unit_id in unit_ids:
            before = prospective[unit_id]
            constraint = fixed_by_unit.get(unit_id)
            if constraint is not None:
                asset_id = constraint["asset_id"]
                result = "manual_verified"
                existing_manual = (unit_id, role, asset_id) in required_manual
                reason = (
                    "existing_manual_fixed_edge"
                    if existing_manual
                    else "manual_fixed_edge"
                )
                possible_ids = (asset_id,)
                confirmation_ids = (constraint["confirmation_id"],)
                verification = verifications.get((unit_id, asset_id))
                prospective_asset_status[asset_id] = "manual_verified"
            else:
                existing_manual = False
                asset_id = selected.get(unit_id)
                possible_ids = possible.get(unit_id, ())
                verification = verifications.get((unit_id, asset_id)) if asset_id else None
                confirmation_ids = ()
                if asset_id is None:
                    result = "review"
                    reason = "alignment_ambiguous" if possible_ids else "no_candidate"
                else:
                    automatic_reason = _automatic_review_reason(
                        unit_id=unit_id,
                        role=role,
                        asset_id=asset_id,
                        margin=margins.get(unit_id, 0.0),
                        problems=problems,
                        assets=assets,
                        verifications=verifications,
                        projected_scope=key[0],
                        projected_segment=key[1],
                    )
                    if automatic_reason is not None:
                        result = "review"
                        reason = automatic_reason
                    else:
                        result = "auto_verified"
                        reason = "unique_dual_edge"
                        if prospective_asset_status[asset_id] != "manual_verified":
                            prospective_asset_status[asset_id] = "auto_verified"
            after = (
                copy.deepcopy(before)
                if existing_manual
                else _apply_role_result(
                    before,
                    unit_id=unit_id,
                    role=role,
                    result=result,
                    reason=reason,
                    asset_id=asset_id,
                    possible_ids=possible_ids,
                    assets=assets,
                    verification=verification,
                    confirmation_ids=confirmation_ids,
                )
            )
            other_field = "explanation_ids" if role == ROLE_QUICK else "answer_ids"
            if after.get(other_field) != before.get(other_field):
                raise FixedEdgeError("fixed-edge planning changed the other role")
            prospective[unit_id] = after
            impacted.add((unit_id, role))
            effects.append(
                {
                    "unit_id": unit_id,
                    "role": role,
                    "result": result,
                    "reason": reason,
                    "asset_id": asset_id,
                    "before_sha256": canonical_sha256(before),
                    "after_sha256": canonical_sha256(after),
                }
            )
    linked_assets = {
        asset_id
        for decision in prospective.values()
        for field in ("answer_ids", "explanation_ids")
        for asset_id in decision.get(field, ())
    }
    for asset_id in impacted_assets:
        if prospective_asset_status[asset_id] == "manual_verified":
            continue
        prospective_asset_status[asset_id] = (
            "auto_verified" if asset_id in linked_assets else "review_only"
        )
    changed_units = {
        unit_id
        for unit_id, decision in prospective.items()
        if canonical_sha256(decision) != canonical_sha256(baseline[unit_id])
    }
    allowed_units = {unit_id for unit_id, _ in impacted}
    if not changed_units.issubset(allowed_units):
        raise FixedEdgeError("fixed-edge planning changed an out-of-scope decision")
    ordered_decisions = [
        {"unit_id": unit_id, "decision": prospective[unit_id]}
        for unit_id in sorted(problems, key=lambda value: problems[value]["sequence"])
    ]
    counts = {
        result: sum(1 for effect in effects if effect["result"] == result)
        for result in ("manual_verified", "auto_verified", "review")
    }
    plan = {
        "contract": FIXED_EDGE_PLAN_CONTRACT,
        "snapshot_payload_sha256": snapshot["payload_sha256"],
        "constraints": fixed,
        "prospective_decisions": ordered_decisions,
        "asset_selection_status": dict(sorted(prospective_asset_status.items())),
        "effects": effects,
        "counts": counts,
        "provider_calls": 0,
    }
    return {**plan, "plan_sha256": canonical_sha256(plan)}
