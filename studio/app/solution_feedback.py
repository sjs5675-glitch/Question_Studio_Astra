from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping, assert_never

from solution_learning.contracts import canonical_sha256

from .model import RevisionConflict, load_json_object, save_json_cas, utc_now


OPERATIONS: Final = {"correct_pair": "content_match", "rejected_pair": "content_match",
                     "crop_bounds": "crop_bounds", "heading_role": "unit_boundary",
                     "source_number_alias": "number_mapping"}
ACTOR_KINDS: Final = frozenset({"human", "agent", "test_fixture"})
REQUEST_FIELDS: Final = frozenset({"expected_revision", "operation", "label_scope",
                                  "source", "value", "attested"})
SOURCE_FIELDS: Final = frozenset({"source_pdf_sha256", "candidate_asset_id",
    "candidate_asset_sha256", "pdf_index", "pdf_page", "printed_page", "role", "bbox"})
IMAGE_SOURCE_FIELDS: Final = frozenset({"source_origin", "source_image_sha256",
    "candidate_asset_id", "candidate_asset_sha256", "role", "bbox"})
APPLY_FIELDS: Final = frozenset({"expected_revision", "plan_sha256", "confirmed"})
CORRECTION_OPERATIONS: Final = frozenset({"heading_role", "source_number_alias"})


class FeedbackRequestError(ValueError): pass


class FeedbackSourceConflict(ValueError): pass


class FeedbackSyncError(RuntimeError): pass


@dataclass(frozen=True, slots=True)
class FeedbackApplyResult:
    saved: dict[str, Any]
    event: dict[str, Any]


def _revision(document: Mapping[str, Any]) -> int:
    workflow = document.get("workflow")
    revision = workflow.get("revision") if isinstance(workflow, Mapping) else None
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise FeedbackSourceConflict("item workflow revision is invalid")
    return revision


def _bbox(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"unit", "origin", "left", "top", "right", "bottom"}:
        raise FeedbackRequestError("source bbox is invalid")
    result = dict(value)
    if result["unit"] != "ratio" or result["origin"] != "top-left":
        raise FeedbackRequestError("source bbox must use top-left ratio coordinates")
    coordinates = [result[name] for name in ("left", "top", "right", "bottom")]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in coordinates):
        raise FeedbackRequestError("source bbox coordinates must be numbers")
    left, top, right, bottom = (float(value) for value in coordinates)
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise FeedbackRequestError("source bbox must be a non-empty ratio rectangle")
    return {"unit": "ratio", "origin": "top-left", "left": left, "top": top,
            "right": right, "bottom": bottom}


def _parsed_request(body: Mapping[str, Any]) -> tuple[int, str, str, dict[str, Any], Any]:
    if set(body) != REQUEST_FIELDS:
        raise FeedbackRequestError("feedback preflight fields are invalid")
    expected = body["expected_revision"]
    operation = body["operation"]
    scope = body["label_scope"]
    source = body["source"]
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        raise FeedbackRequestError("expected_revision must be a positive integer")
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise FeedbackRequestError("feedback operation is invalid")
    if scope != OPERATIONS[operation]:
        raise FeedbackRequestError("label_scope must name only the reviewed aspect")
    if body["attested"] is not True:
        raise FeedbackRequestError("explicit review attestation is required")
    if not isinstance(source, Mapping) or set(source) not in (SOURCE_FIELDS, IMAGE_SOURCE_FIELDS):
        raise FeedbackRequestError("source binding fields are invalid")
    parsed_source = dict(source)
    parsed_source["bbox"] = _bbox(source["bbox"])
    image_source = set(source) == IMAGE_SOURCE_FIELDS
    if image_source and source["source_origin"] != "uploaded_image":
        raise FeedbackRequestError("image source origin is invalid")
    for name in (("source_image_sha256", "candidate_asset_sha256") if image_source else
                 ("source_pdf_sha256", "candidate_asset_sha256")):
        value = parsed_source[name]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise FeedbackRequestError(f"{name} must be a lowercase SHA-256")
    if not isinstance(parsed_source["candidate_asset_id"], str) or not parsed_source["candidate_asset_id"]:
        raise FeedbackRequestError("candidate_asset_id is required")
    for name, minimum in (() if image_source else (("pdf_index", 0), ("pdf_page", 1))):
        value = parsed_source[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise FeedbackRequestError(f"{name} is invalid")
    if not image_source and parsed_source["printed_page"] is not None and (
        isinstance(parsed_source["printed_page"], bool)
        or not isinstance(parsed_source["printed_page"], int)
    ):
        raise FeedbackRequestError("printed_page is invalid")
    if parsed_source["role"] not in {"quick_answer", "detailed_solution", "mixed"}:
        raise FeedbackRequestError("source role is invalid")
    return expected, operation, str(scope), parsed_source, body["value"]


def _target(document: Mapping[str, Any], source: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    provenance = document.get("provenance")
    resources = document.get("resources")
    links = provenance.get("solution_links") if isinstance(provenance, Mapping) else None
    assets = resources.get("assets") if isinstance(resources, Mapping) else None
    if not isinstance(links, list) or not isinstance(assets, list):
        raise FeedbackSourceConflict("item solution provenance is unavailable")
    link = next((value for value in links if isinstance(value, dict)
                 and value.get("source_asset_id") == source["candidate_asset_id"]), None)
    asset = next((value for value in assets if isinstance(value, dict)
                  and value.get("asset_id") == source["candidate_asset_id"]), None)
    if link is None or asset is None:
        raise FeedbackSourceConflict("candidate asset is not current")
    if link.get("kind") != source["role"] or asset.get("sha256") != source["candidate_asset_sha256"]:
        raise FeedbackSourceConflict("candidate source identity changed")
    if source.get("source_origin") == "uploaded_image":
        if link.get("source_origin") != "uploaded_image" or source["source_image_sha256"] != asset["sha256"]:
            raise FeedbackSourceConflict("candidate image source identity changed")
        return link, {}, asset
    if link.get("document_sha256") != source["source_pdf_sha256"]:
        raise FeedbackSourceConflict("candidate PDF source identity changed")
    parts = link.get("parts")
    part = next((value for value in parts if isinstance(value, dict)
                 and value.get("pdf_index") == source["pdf_index"]
                 and value.get("pdf_page") == source["pdf_page"]
                 and value.get("printed_page") == source["printed_page"]
                 and value.get("bbox") == source["bbox"]), None) if isinstance(parts, list) else None
    if part is None:
        raise FeedbackSourceConflict("candidate page or bounds changed")
    return link, part, asset


def _mutate(document: dict[str, Any], operation: str, source: Mapping[str, Any], value: Any) -> dict[str, Any]:
    link, part, _ = _target(document, source)
    if "manual_user_confirmation" in (link.get("evidence") or []) and operation != "correct_pair":
        raise FeedbackSourceConflict("an existing manual solution confirmation is protected")
    original_value: str | None = None
    match operation:
        case "correct_pair":
            if value is not None:
                raise FeedbackRequestError("correct_pair value must be null")
            link["status"] = "verified"
            link["evidence"] = list(dict.fromkeys([*(link.get("evidence") or []), "manual_user_confirmation"]))
            link["review_reasons"] = [reason for reason in (link.get("review_reasons") or []) if reason != "cross_document_verification_pending"]
        case "rejected_pair":
            if value is not None:
                raise FeedbackRequestError("rejected_pair value must be null")
            link["status"] = "missing"
            link["review_reasons"] = list(dict.fromkeys([*(link.get("review_reasons") or []), "user_rejected_solution_pair"]))
        case "crop_bounds":
            part["bbox"] = _bbox(value)
        case "heading_role":
            if not isinstance(value, str) or value not in {"heading", "body", "continuation"}:
                raise FeedbackRequestError("heading_role value is invalid")
            original_value = None
        case "source_number_alias":
            if not isinstance(value, str) or not value.strip() or len(value) > 100:
                raise FeedbackRequestError("source_number_alias value is invalid")
            original_value = document["identity"]["printed_number"]
            if value == original_value:
                raise FeedbackSourceConflict("source number alias does not change the canonical value")
        case unreachable:
            assert_never(unreachable)
    if operation in CORRECTION_OPERATIONS:
        corrections = document["provenance"].setdefault("solution_corrections", [])
        correction = next((entry for entry in corrections if isinstance(entry, dict)
                           and entry.get("operation") == operation
                           and entry.get("source") == source), None)
        if correction is None:
            correction = {"operation": operation, "label_scope": OPERATIONS[operation],
                          "source": copy.deepcopy(dict(source)), "original_value": original_value,
                          "reviewed_value": value}
            corrections.append(correction)
        else:
            correction["reviewed_value"] = value
        return {"correction": copy.deepcopy(correction)}
    return {"link": copy.deepcopy(link), "reviewed_value": copy.deepcopy(value)}


def _event(*, phase: str, plan: Mapping[str, Any], source_revision: int,
           before_sha: str, after_sha: str) -> dict[str, Any]:
    payload = {"phase": phase, "plan_sha256": plan["plan_sha256"], "plan": copy.deepcopy(dict(plan))}
    source_event_sha = canonical_sha256({
        "phase": phase,
        "plan_sha256": plan["plan_sha256"],
        "source_revision": source_revision,
        "before_sha": before_sha,
        "after_sha": after_sha,
    })
    return {"schema_version": 1, "event_id": f"studio-feedback-{source_event_sha[:32]}",
            "source_event_sha": source_event_sha, "operation": plan["operation"],
            "actor_kind": plan["actor_kind"], "label_scope": plan["label_scope"],
            "source_revision": source_revision, "before_sha": before_sha, "after_sha": after_sha,
            "payload": payload, "applied_at": utc_now(), "supersedes_event_id": None}


def preflight_feedback(item_path: Path, body: Mapping[str, Any], *, actor_kind: str,
                       reviewer_id: str) -> dict[str, Any]:
    if actor_kind not in ACTOR_KINDS or not reviewer_id:
        raise FeedbackRequestError("server review authority is invalid")
    expected, operation, scope, source, value = _parsed_request(body)
    current = load_json_object(item_path)
    current_revision = _revision(current)
    if current_revision != expected:
        raise RevisionConflict(expected, current_revision)
    before_link, _, _ = _target(current, source)
    corrections = current["provenance"].get("solution_corrections", [])
    prior = next((entry for entry in corrections if isinstance(entry, dict)
                  and entry.get("operation") == operation
                  and entry.get("source") == source), None)
    before = ({"correction": copy.deepcopy(prior)} if operation in CORRECTION_OPERATIONS
              else {"link": copy.deepcopy(before_link), "reviewed_value": None})
    preview_candidate = copy.deepcopy(current)
    after = _mutate(preview_candidate, operation, source, value)
    if canonical_sha256(preview_candidate) == canonical_sha256(current):
        raise FeedbackSourceConflict("feedback correction is a no-op")
    plan = {"schema_version": 1, "item_id": current.get("item_id"), "operation": operation,
            "label_scope": scope, "source": source, "value": copy.deepcopy(value),
            "before": before, "after": after, "before_sha256": canonical_sha256(before),
            "after_sha256": canonical_sha256(after), "base_revision": expected,
            "apply_revision": expected + 1, "actor_kind": actor_kind, "reviewer_id": reviewer_id,
            "review_origin": "user_attested" if actor_kind == "human" else actor_kind}
    plan["plan_sha256"] = canonical_sha256(plan)
    event = _event(phase="intent", plan=plan, source_revision=expected,
                   before_sha=plan["before_sha256"], after_sha=plan["before_sha256"])
    intent_candidate = copy.deepcopy(current)
    provenance = intent_candidate["provenance"]
    provenance["solution_feedback_events"] = [
        *(provenance.get("solution_feedback_events") or []), event
    ]
    saved = save_json_cas(item_path, intent_candidate, expected, preserve_approval=True)
    return {**plan, "revision": _revision(saved), "intent_event_id": event["event_id"]}


def apply_feedback(item_path: Path, body: Mapping[str, Any], *, actor_kind: str,
                   reviewer_id: str) -> FeedbackApplyResult:
    if set(body) != APPLY_FIELDS or body.get("confirmed") is not True:
        raise FeedbackRequestError("feedback apply fields or confirmation are invalid")
    expected = body["expected_revision"]
    plan_sha = body["plan_sha256"]
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        raise FeedbackRequestError("expected_revision must be a positive integer")
    if not isinstance(plan_sha, str) or len(plan_sha) != 64:
        raise FeedbackRequestError("plan_sha256 is invalid")
    current = load_json_object(item_path)
    current_revision = _revision(current)
    if current_revision != expected:
        raise RevisionConflict(expected, current_revision)
    events = current.get("provenance", {}).get("solution_feedback_events", [])
    intent = next((event for event in reversed(events) if isinstance(event, Mapping)
                   and event.get("payload", {}).get("phase") == "intent"
                   and event.get("payload", {}).get("plan_sha256") == plan_sha), None)
    if intent is None:
        raise FeedbackSourceConflict("feedback preflight intent is unavailable")
    plan = intent["payload"]["plan"]
    if (
        plan.get("plan_sha256") != plan_sha
        or canonical_sha256({key: value for key, value in plan.items() if key != "plan_sha256"}) != plan_sha
        or plan.get("apply_revision") != expected
        or plan.get("actor_kind") != actor_kind
        or plan.get("reviewer_id") != reviewer_id
    ):
        raise FeedbackSourceConflict("feedback preflight is stale or belongs to another authority")
    candidate = copy.deepcopy(current)
    after = _mutate(candidate, str(plan["operation"]), plan["source"], plan["value"])
    if canonical_sha256(after) != plan["after_sha256"]:
        raise FeedbackSourceConflict("feedback target changed after preflight")
    event = _event(phase="applied", plan=plan, source_revision=expected,
                   before_sha=str(plan["before_sha256"]), after_sha=str(plan["after_sha256"]))
    candidate["provenance"]["solution_feedback_events"].append(event)
    return FeedbackApplyResult(save_json_cas(item_path, candidate, expected), event)
