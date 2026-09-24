from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from solution_learning.contracts import ActorKind, Document, Example, ExampleKind, JsonObject, LabelScope, QualityLevel, ReviewEvent, canonical_sha256
from solution_learning.database import PathBoundaryError, validate_source_path
from solution_learning.queries import QueryError
from solution_learning.store import LearningStore, LearningStoreError
from solution_learning.studio_ingest import (
    StudioIngestError,
    TrustedFeedbackAuthority,
    ingest_studio_feedback_events,
)

from .model import ModelError, atomic_write_json, load_json_object
from .solution_feedback import FeedbackSyncError
from .solution_reconciliation import SolutionIntegrityError, locate_leaf_run


SOURCE_MANIFEST: Final = "solutions/link_manifest.json"


@dataclass(frozen=True, slots=True)
class FeedbackSyncRequest:
    item_path: Path
    project_path: Path
    event: JsonObject
    actor_kind: ActorKind
    reviewer_id: str
    action_id: str


@dataclass(frozen=True, slots=True)
class StudioLearningSync:
    learning_root: Path
    import_roots: tuple[Path, ...]
    allowed_source_roots: tuple[Path, ...]

    def sync_source(self, item_path: Path, operation_id: str) -> None:
        """Record a source choice as unreviewed layout evidence, never a human match label."""
        try:
            operation_path = item_path.parent / "jobs" / "solution_operations" / operation_id / "operation.json"
            operation = load_json_object(operation_path)
            if operation.get("learning_sync") == "synced":
                return
            event = operation.get("source_event")
            if not isinstance(event, dict) or event.get("authority") != "user_selected_unverified":
                raise StudioIngestError("source choice event is unavailable")
            document = load_json_object(item_path)
            selection = document.get("provenance", {}).get("solution_selection")
            if not isinstance(selection, dict) or selection.get("revision") != event.get("selection_revision") or selection.get("source_sha256") != event.get("source_sha256"):
                raise StudioIngestError("source choice is no longer current")
            asset = next((entry for entry in document.get("resources", {}).get("assets", [])
                if isinstance(entry, dict) and entry.get("asset_id") == event.get("after_asset_id")), None)
            if not isinstance(asset, dict) or asset.get("sha256") != event.get("source_sha256"):
                raise StudioIngestError("source choice asset is stale")
            path = validate_source_path(str(item_path.parent / str(asset.get("path"))),
                str(event["source_sha256"]), self.allowed_source_roots)
            digest = canonical_sha256(event)
            example_id = f"studio-source-{operation_id}"
            with LearningStore(self.learning_root, allowed_source_roots=self.allowed_source_roots) as store:
                store.add_document(Document(str(event["source_sha256"]), "solution", None, None, 1,
                    str(path), "studio_feedback"))
                store.append_review_event(ReviewEvent(str(event["event_id"]), digest,
                    str(event["operation"]), ActorKind.HUMAN, LabelScope.CROP_BOUNDS,
                    int(event["selection_revision"]), str(event["before_sha256"]),
                    str(event["after_sha256"]), event, str(event["applied_at"])))
                example = Example(example_id, 1, str(event["event_id"]), ExampleKind.DETAIL_BOUNDARY,
                    str(event["source_sha256"]), 0, None, str(document["item_id"]),
                    str(event["after_asset_id"]), event, (), QualityLevel.UNREVIEWED, digest, "active", None)
                try:
                    existing = store.get_example(example_id, 1).example
                except QueryError:
                    store.add_example(example)
                else:
                    if existing != example:
                        raise StudioIngestError("source choice event identity changed")
                previous = store.queries.connection.execute(
                    "SELECT e.example_id,e.revision FROM examples e "
                    "JOIN (SELECT example_id,MAX(revision) revision FROM examples GROUP BY example_id) latest "
                    "USING(example_id,revision) WHERE e.unit_id=? AND e.kind=? AND e.asset_id=? "
                    "AND e.effective_state='active' AND e.example_id<>?",
                    (document["item_id"], ExampleKind.DETAIL_BOUNDARY.value,
                     event.get("before_asset_id"), example_id),
                ).fetchall()
                for row in previous:
                    old = store.get_example(str(row["example_id"]), int(row["revision"])).example
                    withdrawal = {"withdrawn_example_id": old.example_id, "source_event_id": event["event_id"],
                                  "reason": "source_replaced_or_reverted"}
                    withdrawal_sha = canonical_sha256(withdrawal)
                    withdrawal_id = f"withdraw-{operation_id}-{old.example_id}"
                    store.append_review_event(ReviewEvent(withdrawal_id, withdrawal_sha, "source_withdrawal",
                        ActorKind.HUMAN, LabelScope.CROP_BOUNDS, old.revision + 1, old.payload_sha,
                        withdrawal_sha, withdrawal, str(event["applied_at"]), old.event_id))
                    store.add_example(Example(old.example_id, old.revision + 1, withdrawal_id, old.kind,
                        old.source_sha, old.pdf_index, old.region, old.unit_id, old.asset_id,
                        old.reviewed_value, old.blob_hashes, QualityLevel.UNREVIEWED, old.payload_sha,
                        "retracted", None))
            atomic_write_json(operation_path, {**operation, "learning_sync": "synced"})
        except (LearningStoreError, ModelError, OSError, PathBoundaryError, sqlite3.Error,
                StudioIngestError, ValueError) as exc:
            raise FeedbackSyncError(str(exc)) from exc

    def __call__(self, request: FeedbackSyncRequest) -> None:
        try:
            payload = request.event["payload"]
            if not isinstance(payload, dict):
                raise StudioIngestError("Studio applied event payload is invalid")
            plan = payload.get("plan")
            source = plan.get("source") if isinstance(plan, dict) else None
            if not isinstance(source, dict):
                raise StudioIngestError("Studio feedback source is invalid")
            if source.get("source_origin") == "uploaded_image":
                item = load_json_object(request.item_path)
                asset = next((asset for asset in item.get("resources", {}).get("assets", [])
                    if isinstance(asset, dict) and asset.get("asset_id") == source.get("candidate_asset_id")), None)
                if not isinstance(asset, dict) or asset.get("sha256") != source.get("source_image_sha256"):
                    raise StudioIngestError("Studio image source is stale")
                source_path = request.item_path.parent / str(asset.get("path"))
            else:
                source_path = self._source_pdf(request.project_path)
            source_event_sha = request.event.get("source_event_sha")
            if not isinstance(plan, dict) or not isinstance(source_event_sha, str):
                raise StudioIngestError("Studio applied event identity is invalid")
            plan_sha = plan.get("plan_sha256")
            if not isinstance(plan_sha, str):
                raise StudioIngestError("Studio applied plan identity is invalid")
            proof = TrustedFeedbackAuthority(
                request.actor_kind,
                request.reviewer_id,
                plan_sha,
                source_event_sha,
                request.action_id,
            )
            source_roots = tuple(root for root in self.allowed_source_roots if root.is_dir())
            with LearningStore(
                self.learning_root,
                allowed_source_roots=source_roots,
            ) as store:
                ingest_studio_feedback_events(
                    store,
                    request.item_path,
                    source_path,
                    expected_actor_kind=request.actor_kind,
                    trusted_authorities={plan_sha: proof},
                )
        except (
            LearningStoreError,
            ModelError,
            OSError,
            PathBoundaryError,
            SolutionIntegrityError,
            sqlite3.Error,
            StudioIngestError,
        ) as exc:
            raise FeedbackSyncError(str(exc)) from exc

    def _source_pdf(self, project_path: Path) -> Path:
        project = load_json_object(project_path)
        leaf = locate_leaf_run(project, self.import_roots)
        if leaf is None:
            raise StudioIngestError("Studio source leaf is unavailable")
        manifest = load_json_object(leaf / SOURCE_MANIFEST)
        solution = manifest.get("solution")
        source_value = solution.get("source_pdf") if isinstance(solution, dict) else None
        if not isinstance(source_value, str) or not source_value.strip():
            raise StudioIngestError("Studio solution PDF locator is unavailable")
        source = Path(source_value).expanduser()
        return (source if source.is_absolute() else leaf / source).resolve(strict=True)
