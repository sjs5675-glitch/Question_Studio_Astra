from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Mapping

from .contracts import (
    ActorKind, Document, Example, ExampleKind, IngestCheckpoint, JsonObject, JsonValue,
    LabelScope, QualityLevel, ReviewEvent, canonical_json, canonical_sha256, require_sha256,
)
from .database import PathBoundaryError, validate_source_path
from .queries import QueryError
from .store import LearningStore, RecordConflictError

EVENT_FIELDS: Final = frozenset((
    "plan_id plan_sha256 confirmation_ids source_link_manifest_sha256 selection_sha256 effects counts "
    "effects_sha256 counts_sha256 response_sha256 provider_calls applied_at output_payload_sha256 "
    "job_sha256 document_sha256 run_sha256 receipt_sha256"
).split())
EFFECT_FIELDS: Final = frozenset("unit_id role result reason asset_id before_sha256 after_sha256".split())
SHA_FIELDS: Final = EVENT_FIELDS - frozenset("plan_id confirmation_ids effects counts provider_calls applied_at".split())
INGESTIBLE_RESULTS: Final = frozenset({"manual_verified", "auto_verified"})
PLAN_ID: Final = re.compile(r"mcp-[0-9a-f]{32}")
CONFIRMATION_ID: Final = re.compile(r"mc-[0-9a-f]{32}")


class IngestError(RuntimeError): pass


@dataclass(frozen=True, slots=True)
class IngestResult:
    events_seen: int
    events_added: int
    examples_added: int
    last_receipt_sha: str | None


def _read_bound(path: Path, roots: tuple[Path, ...]) -> bytes:
    try:
        if not path.is_absolute() or path.is_symlink():
            raise IngestError("ingest path must be an absolute regular path")
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise IngestError("ingest path is unavailable") from exc
    if not resolved.is_file() or not any(resolved.is_relative_to(root) for root in roots):
        raise IngestError("ingest path is outside allowed source roots")
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise IngestError("ingest path cannot traverse a symlink")
        current = current.parent
    try:
        return resolved.read_bytes()
    except OSError as exc:
        raise IngestError("ingest path could not be read") from exc


def _parse_manifest(raw: bytes) -> tuple[JsonObject, tuple[JsonObject, ...]]:
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngestError("manual confirmation manifest is invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise IngestError("manual confirmation manifest must be an object")
    events = decoded.get("manual_confirmation_events")
    if not isinstance(events, list):
        raise IngestError("manual confirmation events are missing")
    parsed: list[JsonObject] = []
    for value in events:
        if not isinstance(value, dict):
            raise IngestError("manual confirmation event must be an object")
        parsed.append(value)
    return decoded, tuple(parsed)


def _validate_manifest_chain(manifest: JsonObject, events: tuple[JsonObject, ...]) -> None:
    constraints = manifest.get("manual_constraints")
    if not isinstance(constraints, list):
        raise IngestError("manual confirmation constraints are missing")
    constraint_ids = {
        value.get("confirmation_id")
        for value in constraints
        if isinstance(value, dict) and isinstance(value.get("confirmation_id"), str)
    }
    if any(not set(event["confirmation_ids"]).issubset(constraint_ids) for event in events):
        raise IngestError("manual confirmation event is detached from its constraints")
    if events:
        prior = dict(manifest)
        prior["manual_confirmation_events"] = list(events[:-1])
        if events[-1]["output_payload_sha256"] != canonical_sha256(prior):
            raise IngestError("manual confirmation output append link is invalid")


def _sha(value: JsonValue, field: str) -> str:
    if not isinstance(value, str):
        raise IngestError(f"{field} must be a SHA-256")
    try:
        return require_sha256(value, field)
    except ValueError as exc:
        raise IngestError(f"{field} must be a SHA-256") from exc


def _validate_event(event: JsonObject) -> tuple[JsonObject, ...]:
    if set(event) != EVENT_FIELDS:
        raise IngestError("manual confirmation event schema is invalid")
    for field in SHA_FIELDS:
        _sha(event[field], field)
    effects = event["effects"]
    counts = event["counts"]
    plan_id = event["plan_id"]
    confirmation_ids = event["confirmation_ids"]
    if (not isinstance(plan_id, str) or PLAN_ID.fullmatch(plan_id) is None
            or not isinstance(confirmation_ids, list) or not confirmation_ids
            or len(confirmation_ids) != len(set(
                value for value in confirmation_ids if isinstance(value, str)))
            or any(not isinstance(value, str) or CONFIRMATION_ID.fullmatch(value) is None
                   for value in confirmation_ids)
            or not isinstance(effects, list) or not isinstance(counts, dict)):
        raise IngestError("manual confirmation event delta is invalid")
    parsed: list[JsonObject] = []
    seen: set[tuple[str, str]] = set()
    for value in effects:
        if not isinstance(value, dict) or set(value) != EFFECT_FIELDS:
            raise IngestError("manual confirmation effect schema is invalid")
        unit_id, role, result = value["unit_id"], value["role"], value["result"]
        reason, asset_id = value["reason"], value["asset_id"]
        if (not isinstance(unit_id, str) or not unit_id or role not in {
                "quick_answer", "detailed_solution"} or result not in {
                "manual_verified", "auto_verified", "review"}
                or not isinstance(reason, str) or not reason
                or (asset_id is not None and (not isinstance(asset_id, str) or not asset_id))):
            raise IngestError("manual confirmation effect value is invalid")
        key = (unit_id, str(role))
        if key in seen:
            raise IngestError("manual confirmation event has duplicate effects")
        seen.add(key)
        _sha(value["before_sha256"], "before_sha256")
        _sha(value["after_sha256"], "after_sha256")
        parsed.append(value)
    expected_counts = {name: sum(effect["result"] == name for effect in parsed)
                       for name in ("manual_verified", "auto_verified", "review")}
    receipt_payload = {key: value for key, value in event.items() if key != "receipt_sha256"}
    if (set(counts) != set(expected_counts)
            or any(type(value) is not int or value < 0 for value in counts.values())
            or counts != expected_counts or event["provider_calls"] != 0
            or event["effects_sha256"] != canonical_sha256(effects)
            or event["counts_sha256"] != canonical_sha256(counts)
            or event["receipt_sha256"] != canonical_sha256(receipt_payload)):
        raise IngestError("manual confirmation receipt digest is invalid")
    try:
        applied_at = datetime.fromisoformat(str(event["applied_at"]))
    except ValueError as exc:
        raise IngestError("manual confirmation timestamp is invalid") from exc
    if applied_at.tzinfo is None:
        raise IngestError("manual confirmation timestamp must be timezone-aware")
    return tuple(parsed)


def _effect_hash(effects: tuple[JsonObject, ...], field: str) -> str:
    values = [effect[field] for effect in effects]
    return str(values[0]) if len(values) == 1 else canonical_sha256(values)


def _add_example_once(store: LearningStore, example: Example) -> bool:
    try:
        existing = store.get_example(example.example_id, example.revision).example
    except QueryError:
        store.add_example(example)
        return True
    if existing != example:
        raise RecordConflictError("legacy example identity has conflicting content")
    return False


def ingest_manual_confirmation_events(
    store: LearningStore, manifest_path: Path, source_path: Path, expected_source_sha: str, *,
    authority_assertion_ids: Mapping[str, str] | None = None,
) -> IngestResult:
    try:
        source_sha = require_sha256(expected_source_sha, "source sha")
        resolved_source = validate_source_path(str(source_path), source_sha, store.allowed_source_roots)
    except (ValueError, OSError, PathBoundaryError) as exc:
        raise IngestError("source path does not match its bound SHA-256") from exc
    raw = _read_bound(manifest_path, store.allowed_source_roots)
    manifest, events = _parse_manifest(raw)
    validated: list[tuple[JsonObject, tuple[JsonObject, ...]]] = []
    receipts: set[str] = set()
    plans: set[str] = set()
    for event in events:
        effects = _validate_event(event)
        receipt = str(event["receipt_sha256"])
        plan = event["plan_id"]
        if receipt in receipts or not isinstance(plan, str) or plan in plans:
            raise IngestError("manual confirmation event identity is duplicated")
        receipts.add(receipt)
        plans.add(plan)
        validated.append((event, effects))
    _validate_manifest_chain(manifest, events)
    if _read_bound(manifest_path, store.allowed_source_roots) != raw:
        raise IngestError("manual confirmation manifest changed during ingestion")
    blobs = tuple(store.blobs.stage(canonical_json(event).encode("utf-8"))
                  for event, _ in validated)
    checkpoint = store.get_checkpoint(source_sha)
    if checkpoint is not None and (checkpoint.last_revision > len(validated)
            or checkpoint.last_revision and checkpoint.last_event_id
            != f"legacy-{validated[checkpoint.last_revision - 1][0]['receipt_sha256']}"):
        raise IngestError("manual confirmation history was replaced")
    operation = f"legacy_manual_confirmation:{source_sha}"
    stored_history = store.queries.connection.execute(
        "SELECT event_id,source_event_sha,source_revision FROM review_events WHERE operation=? ORDER BY source_revision",
        (operation,),
    ).fetchall()
    if len(stored_history) > len(validated) or any(
        row["source_revision"] != index
        or row["source_event_sha"] != validated[index - 1][0]["receipt_sha256"]
        or row["event_id"] != f"legacy-{validated[index - 1][0]['receipt_sha256']}"
        for index, row in enumerate(stored_history, 1)
    ):
        raise IngestError("manual confirmation history was replaced")
    store.add_document(Document(source_sha, "solution", None, None, 1, str(resolved_source), "legacy_manual_confirmation"))
    events_added = 0
    examples_added = 0
    assertions = authority_assertion_ids or {}
    constraints = manifest["manual_constraints"]
    items = manifest.get("items")
    assets = manifest.get("assets")
    for revision, ((event, effects), blob) in enumerate(zip(validated, blobs, strict=True), 1):
        receipt = str(event["receipt_sha256"])
        event_id = f"legacy-{receipt}"
        review_event = ReviewEvent(
            event_id, receipt, operation, ActorKind.LEGACY_UNKNOWN, LabelScope.CONTENT_MATCH, revision,
            _effect_hash(effects, "before_sha256"), _effect_hash(effects, "after_sha256"), event, str(event["applied_at"]),
        )
        events_added += int(store.append_review_event(review_event))
        for index, effect in enumerate(effects):
            if effect["result"] not in INGESTIBLE_RESULTS:
                continue
            example_id = f"legacy-{receipt}-{index}"
            reviewed_value = {
                "source_sha256": source_sha, "document_sha256": event["document_sha256"],
                "effects_sha256": event["effects_sha256"], "receipt_sha256": receipt,
                "effect_index": index, "effect": effect,
            }
            assertion_id = assertions.get(example_id)
            unit_id, role, asset_id = effect["unit_id"], effect["role"], effect["asset_id"]
            item = items.get(unit_id) if isinstance(items, dict) else None
            asset = assets.get(asset_id) if isinstance(assets, dict) and isinstance(asset_id, str) else None
            field = "answer_ids" if role == "quick_answer" else "explanation_ids"
            bound = (
                effect["result"] == "manual_verified" and effect["reason"] == "manual_fixed_edge"
                and isinstance(item, dict) and canonical_sha256(item) == effect["after_sha256"]
                and isinstance(item.get(field), list) and asset_id in item[field]
                and isinstance(asset, dict) and asset.get("selection_status") == "manual_verified"
                and asset.get("kind") in {role, "mixed"}
                and any(isinstance(value, dict) and value.get("confirmation_id") in event["confirmation_ids"]
                        and value.get("unit_id") == unit_id and value.get("role") == role
                        and value.get("asset_id") == asset_id for value in constraints)
            )
            if not bound and assertion_id is None:
                continue
            example = Example(
                example_id, 1, event_id, ExampleKind.CORRECT_PAIR, source_sha, 0, None,
                str(effect["unit_id"]), str(effect["asset_id"]) if effect["asset_id"] else None,
                reviewed_value, (blob.sha256,),
                QualityLevel.HUMAN_CONFIRMED if assertion_id else QualityLevel.UNREVIEWED,
                canonical_sha256(reviewed_value), "active", assertion_id,
            )
            examples_added += int(_add_example_once(store, example))
        store.save_checkpoint(IngestCheckpoint(source_sha, event_id, revision, "complete"))
    return IngestResult(len(events), events_added, examples_added, str(events[-1]["receipt_sha256"]) if events else None)
