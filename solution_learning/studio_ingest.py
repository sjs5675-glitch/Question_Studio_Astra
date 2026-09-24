from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Mapping

from .contracts import (
    ActorKind, AuthorityAssertion, Document, Example, ExampleKind, IngestCheckpoint,
    JsonObject, JsonValue, LabelScope, QualityLevel, ReviewEvent, ReviewOrigin, canonical_json,
    canonical_sha256, require_sha256,
)
from .database import PathBoundaryError, validate_source_path
from .queries import QueryError
from .store import LearningStore, RecordConflictError

OPERATION_SCOPE: Final = {"correct_pair": "content_match", "rejected_pair": "content_match",
    "crop_bounds": "crop_bounds", "heading_role": "unit_boundary",
    "source_number_alias": "number_mapping"}
OPERATION_KIND: Final = {"correct_pair": ExampleKind.CORRECT_PAIR,
    "rejected_pair": ExampleKind.REJECTED_PAIR, "crop_bounds": ExampleKind.DETAIL_BOUNDARY,
    "heading_role": ExampleKind.HEADING_ROLE, "source_number_alias": ExampleKind.SOURCE_NUMBER_ALIAS}
ACTOR_ORIGIN: Final = {"human": "user_attested", "agent": "agent", "test_fixture": "test_fixture"}
EVENT_FIELDS: Final = frozenset("schema_version event_id source_event_sha operation actor_kind label_scope source_revision before_sha after_sha payload applied_at supersedes_event_id".split())
PLAN_FIELDS: Final = frozenset("schema_version item_id operation label_scope source value before after before_sha256 after_sha256 base_revision apply_revision actor_kind reviewer_id review_origin plan_sha256".split())


class StudioIngestError(RuntimeError): pass


@dataclass(frozen=True, slots=True)
class StudioIngestResult:
    pairs_seen: int
    events_added: int
    examples_added: int


@dataclass(frozen=True, slots=True)
class TrustedFeedbackAuthority:
    actor_kind: ActorKind
    reviewer_id: str
    plan_sha256: str
    source_event_sha: str
    action_id: str


def _read_bound(path: Path, roots: tuple[Path, ...]) -> bytes:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise StudioIngestError("Studio source is unavailable") from exc
    if not path.is_absolute() or path.is_symlink() or not resolved.is_file() or not any(
            resolved.is_relative_to(root) for root in roots):
        raise StudioIngestError("Studio source is outside allowed roots")
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise StudioIngestError("Studio source cannot traverse a symlink")
        current = current.parent
    return resolved.read_bytes()


def _sha(value: JsonValue, field: str) -> str:
    if not isinstance(value, str):
        raise StudioIngestError(f"{field} must be a SHA-256")
    try:
        return require_sha256(value, field)
    except ValueError as exc:
        raise StudioIngestError(f"{field} must be a SHA-256") from exc


def _validate_event(event: JsonObject, phase: str) -> JsonObject:
    if set(event) != EVENT_FIELDS or event["schema_version"] != 1 or event["supersedes_event_id"] is not None:
        raise StudioIngestError("Studio feedback event schema is invalid")
    payload = event["payload"]
    if not isinstance(payload, dict) or set(payload) != {"phase", "plan_sha256", "plan"} or payload["phase"] != phase:
        raise StudioIngestError("Studio feedback phase is invalid")
    plan = payload["plan"]
    if not isinstance(plan, dict) or set(plan) != PLAN_FIELDS:
        raise StudioIngestError("Studio feedback plan schema is invalid")
    plan_sha = _sha(plan["plan_sha256"], "plan sha")
    if payload["plan_sha256"] != plan_sha or canonical_sha256(
            {key: value for key, value in plan.items() if key != "plan_sha256"}) != plan_sha:
        raise StudioIngestError("Studio feedback plan digest is invalid")
    before_sha, after_sha = _sha(event["before_sha"], "before sha"), _sha(event["after_sha"], "after sha")
    revision = event["source_revision"]
    expected_event_sha = canonical_sha256({"phase": phase, "plan_sha256": plan_sha,
        "source_revision": revision, "before_sha": before_sha, "after_sha": after_sha})
    if event["source_event_sha"] != expected_event_sha or event["event_id"] != f"studio-feedback-{expected_event_sha[:32]}":
        raise StudioIngestError("Studio feedback event identity is invalid")
    operation = plan["operation"]
    actor = plan["actor_kind"]
    if (operation not in OPERATION_SCOPE or plan["label_scope"] != OPERATION_SCOPE[operation]
            or event["operation"] != operation or event["label_scope"] != plan["label_scope"]
            or actor not in ACTOR_ORIGIN or event["actor_kind"] != actor
            or plan["review_origin"] != ACTOR_ORIGIN[actor]
            or not isinstance(plan["reviewer_id"], str) or not plan["reviewer_id"]):
        raise StudioIngestError("Studio feedback authority or scope is invalid")
    if canonical_sha256(plan["before"]) != plan["before_sha256"] or canonical_sha256(
            plan["after"]) != plan["after_sha256"] or plan["before_sha256"] == plan["after_sha256"]:
        raise StudioIngestError("Studio feedback effect is invalid")
    return plan


def _pairs(events: list[JsonObject]) -> tuple[tuple[JsonObject, JsonObject, JsonObject], ...]:
    result: list[tuple[JsonObject, JsonObject, JsonObject]] = []
    index = 0
    while index < len(events):
        intent = events[index]
        plan = _validate_event(intent, "intent")
        if index + 1 == len(events):
            break
        applied = events[index + 1]
        applied_plan = _validate_event(applied, "applied")
        base, apply = plan["base_revision"], plan["apply_revision"]
        if (applied_plan != plan or not isinstance(base, int) or isinstance(base, bool)
                or apply != base + 1 or intent["source_revision"] != base
                or applied["source_revision"] != apply or intent["before_sha"] != plan["before_sha256"]
                or intent["after_sha"] != plan["before_sha256"]
                or applied["before_sha"] != plan["before_sha256"]
                or applied["after_sha"] != plan["after_sha256"]):
            raise StudioIngestError("Studio feedback intent/applied chain is invalid")
        result.append((intent, applied, plan))
        index += 2
    return tuple(result)


def _current_target(document: JsonObject, plan: JsonObject) -> JsonObject:
    provenance = document.get("provenance")
    resources = document.get("resources")
    if not isinstance(provenance, dict) or not isinstance(resources, dict):
        raise StudioIngestError("Studio feedback target is unavailable")
    source = plan["source"]
    if not isinstance(source, dict):
        raise StudioIngestError("Studio feedback source is invalid")
    operation = plan["operation"]
    if operation in {"heading_role", "source_number_alias"}:
        corrections = provenance.get("solution_corrections")
        current = next((value for value in corrections if isinstance(value, dict)
            and value.get("operation") == operation and value.get("source") == source), None) if isinstance(corrections, list) else None
        return {"correction": current}
    links = provenance.get("solution_links")
    current = next((value for value in links if isinstance(value, dict)
        and value.get("source_asset_id") == source.get("candidate_asset_id")), None) if isinstance(links, list) else None
    return {"link": current, "reviewed_value": plan["value"]}


def _add_example_once(store: LearningStore, example: Example) -> bool:
    try:
        existing = store.get_example(example.example_id, example.revision).example
    except QueryError:
        store.add_example(example)
        return True
    if existing != example and replace(existing, quality_level=example.quality_level,
                                       authority_assertion_id=example.authority_assertion_id) != example:
        raise RecordConflictError("Studio feedback example identity has conflicting content")
    return False


def _source_is_bound(document: JsonObject, plan: JsonObject, asset: JsonObject) -> bool:
    source = plan["source"]
    provenance = document["provenance"]
    links = provenance.get("solution_links")
    current_link = next((value for value in links if isinstance(value, dict)
        and value.get("source_asset_id") == source["candidate_asset_id"]), None) if isinstance(links, list) else None
    before = plan["before"]
    before_link = before.get("link") if isinstance(before, dict) else None
    evidence_link = before_link if isinstance(before_link, dict) else current_link
    parts = evidence_link.get("parts") if isinstance(evidence_link, dict) else None
    if source.get("source_origin") == "uploaded_image":
        return (isinstance(current_link, dict) and current_link.get("source_origin") == "uploaded_image"
            and isinstance(evidence_link, dict) and evidence_link.get("source_origin") == "uploaded_image"
            and current_link.get("kind") == source["role"] and asset.get("sha256") == source["candidate_asset_sha256"]
            and source["source_image_sha256"] == asset.get("sha256"))
    part = next((value for value in parts if isinstance(value, dict)
        and value.get("pdf_index") == source["pdf_index"] and value.get("pdf_page") == source["pdf_page"]
        and value.get("printed_page") == source["printed_page"] and value.get("bbox") == source["bbox"]), None) if isinstance(parts, list) else None
    return (isinstance(current_link, dict) and isinstance(evidence_link, dict) and part is not None
        and current_link.get("document_sha256") == source["source_pdf_sha256"]
        and current_link.get("kind") == source["role"] and asset.get("sha256") == source["candidate_asset_sha256"])


def ingest_studio_feedback_events(
    store: LearningStore, item_path: Path, source_pdf_path: Path, *, expected_actor_kind: ActorKind,
    trusted_authorities: Mapping[str, TrustedFeedbackAuthority] | None = None,
) -> StudioIngestResult:
    raw = _read_bound(item_path, store.allowed_source_roots)
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StudioIngestError("Studio item is invalid JSON") from exc
    if not isinstance(document, dict):
        raise StudioIngestError("Studio item must be an object")
    provenance = document.get("provenance")
    raw_events = provenance.get("solution_feedback_events") if isinstance(provenance, dict) else None
    if not isinstance(raw_events, list) or any(not isinstance(value, dict) for value in raw_events):
        raise StudioIngestError("Studio feedback events are unavailable")
    all_pairs = _pairs(raw_events)
    if not all_pairs:
        return StudioIngestResult(0, 0, 0)
    latest_source = all_pairs[-1][2]["source"]
    source_field = "source_image_sha256" if latest_source.get("source_origin") == "uploaded_image" else "source_pdf_sha256"
    source_sha = _sha(latest_source[source_field], "source sha")
    pairs = tuple(pair for pair in all_pairs if pair[2]["source"].get(source_field) == source_sha)
    if any(plan["actor_kind"] != expected_actor_kind.value for _, _, plan in pairs):
        raise StudioIngestError("Studio feedback actor does not match server authority")
    if _read_bound(item_path, store.allowed_source_roots) != raw:
        raise StudioIngestError("Studio item changed during ingestion")
    if not pairs:
        return StudioIngestResult(0, 0, 0)
    workflow = document.get("workflow")
    final_revision = workflow.get("revision") if isinstance(workflow, dict) else None
    if final_revision != pairs[-1][2]["apply_revision"] + 1 or canonical_sha256(
            _current_target(document, pairs[-1][2])) != pairs[-1][2]["after_sha256"]:
        raise StudioIngestError("Studio feedback current state is stale")
    if any(plan["source"].get(source_field) != source_sha
           or plan["item_id"] != pairs[-1][2]["item_id"] for _, _, plan in pairs):
        raise StudioIngestError("Studio feedback chain changed source identity")
    try:
        resolved_source = validate_source_path(str(source_pdf_path), source_sha, store.allowed_source_roots)
    except (ValueError, OSError, PathBoundaryError) as exc:
        raise StudioIngestError("Studio source PDF is stale") from exc
    item_id = pairs[-1][2]["item_id"]
    if not isinstance(item_id, str) or not item_id:
        raise StudioIngestError("Studio item identity is invalid")
    operation_key = f"studio_feedback:{item_id}:{source_sha}"
    stored = store.queries.connection.execute(
        "SELECT source_revision,source_event_sha FROM review_events WHERE operation=? ORDER BY source_revision",
        (operation_key,),
    ).fetchall()
    if len(stored) > len(pairs) or any(row["source_revision"] != pair[1]["source_revision"]
            or row["source_event_sha"] != pair[1]["source_event_sha"] for row, pair in zip(stored, pairs)):
        raise StudioIngestError("Studio feedback history was replaced or reordered")
    blobs = tuple(store.blobs.stage(canonical_json(applied).encode()) for _, applied, _ in pairs)
    store.add_document(Document(source_sha, "solution", None, None, 1, str(resolved_source), "studio_feedback"))
    added_events = 0
    added_examples = 0
    authorities = trusted_authorities or {}
    for (intent, applied, plan), receipt_blob in zip(pairs, blobs, strict=True):
        source = plan["source"]
        asset_id, asset_sha = source["candidate_asset_id"], _sha(source["candidate_asset_sha256"], "asset sha")
        assets = document["resources"]["assets"]
        asset = next((value for value in assets if isinstance(value, dict) and value.get("asset_id") == asset_id), None)
        if not isinstance(asset, dict) or asset.get("sha256") != asset_sha or not _source_is_bound(document, plan, asset):
            raise StudioIngestError("Studio candidate asset is stale")
        asset_path = item_path.parent / str(asset.get("path"))
        asset_raw = _read_bound(asset_path, store.allowed_source_roots)
        if hashlib.sha256(asset_raw).hexdigest() != asset_sha:
            raise StudioIngestError("Studio candidate asset bytes changed")
        asset_blob = store.blobs.stage(asset_raw, asset_sha)
        event = ReviewEvent(str(applied["event_id"]), str(applied["source_event_sha"]), operation_key,
            ActorKind(str(applied["actor_kind"])), LabelScope(str(applied["label_scope"])),
            int(applied["source_revision"]), str(applied["before_sha"]), str(applied["after_sha"]),
            applied, str(applied["applied_at"]), None)
        added_events += int(store.append_review_event(event))
        example_id = f"studio-{applied['source_event_sha']}"
        reviewed = plan["after"]
        payload_sha = canonical_sha256(reviewed)
        proof = authorities.get(str(plan["plan_sha256"]))
        human = (plan["actor_kind"] == "human" and plan["review_origin"] == "user_attested"
            and proof is not None and proof.actor_kind is ActorKind.HUMAN and bool(proof.action_id)
            and proof.reviewer_id == plan["reviewer_id"] and proof.plan_sha256 == plan["plan_sha256"]
            and proof.source_event_sha == applied["source_event_sha"])
        assertion_id = f"authority-{applied['source_event_sha'][:32]}" if human else None
        if assertion_id:
            store.add_authority_assertion(AuthorityAssertion(assertion_id, str(plan["reviewer_id"]),
                ReviewOrigin.USER_ATTESTED, proof.action_id, str(applied["source_event_sha"]),
                source_sha, example_id, 1, payload_sha, LabelScope(str(plan["label_scope"])),
                str(applied["applied_at"]), "active"))
        bbox = source["bbox"]
        region = (float(bbox["left"]), float(bbox["top"]), float(bbox["right"]), float(bbox["bottom"]))
        example = Example(example_id, 1, str(applied["event_id"]), OPERATION_KIND[str(plan["operation"])],
            source_sha, int(source.get("pdf_index", 0)), region, item_id, str(asset_id), reviewed,
            (asset_blob.sha256, receipt_blob.sha256), QualityLevel.HUMAN_CONFIRMED if human else QualityLevel.UNREVIEWED,
            payload_sha, "active", assertion_id)
        added_examples += int(_add_example_once(store, example))
        store.save_checkpoint(IngestCheckpoint(operation_key, str(applied["event_id"]),
                                               int(applied["source_revision"]), "complete"))
    return StudioIngestResult(len(pairs), added_events, added_examples)
