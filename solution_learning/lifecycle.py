from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .authority import EXAMPLE_SCOPE
from .contracts import (
    ActorKind,
    AuthorityAssertion,
    Example,
    QualityLevel,
    ReviewEvent,
    ReviewOrigin,
    canonical_sha256,
    require_identifier,
    require_sha256,
)
from .ingest import _read_bound, ingest_manual_confirmation_events
from .queries import QueryError
from .store import LearningStore


class LifecycleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LegacyImportReport:
    manifest_sha256: str
    source_sha256: str
    events: int
    examples: int
    eligible_examples: int
    events_added: int
    examples_added: int
    dry_run: bool


@dataclass(frozen=True, slots=True)
class RetractionResult:
    example_id: str
    active_revision: int
    retracted_revision: int
    authority_assertion_id: str
    affected_profiles: tuple[tuple[str, int], ...]
    affected_snapshots: tuple[str, ...]
    affected_runs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrustedRetractionAuthority:
    actor_kind: ActorKind
    reviewer_id: str
    action_id: str
    example_id: str
    source_sha: str
    target_revision: int


def inventory_legacy_manifest(
    manifest_path: Path,
    source_path: Path,
    expected_source_sha: str,
    allowed_source_roots: tuple[Path, ...],
) -> LegacyImportReport:
    with (
        tempfile.TemporaryDirectory(prefix="astra-learning-inventory-") as directory,
        LearningStore(Path(directory) / "library", allowed_source_roots=allowed_source_roots) as store,
    ):
        return _import_report(
            store, manifest_path, source_path, expected_source_sha, dry_run=True,
        )


def import_legacy_manifest(
    store: LearningStore,
    manifest_path: Path,
    source_path: Path,
    expected_source_sha: str,
    *,
    dry_run: bool = True,
) -> LegacyImportReport:
    if dry_run:
        return inventory_legacy_manifest(
            manifest_path, source_path, expected_source_sha, store.allowed_source_roots,
        )
    return _import_report(store, manifest_path, source_path, expected_source_sha, dry_run=False)


def _import_report(
    store: LearningStore,
    manifest_path: Path,
    source_path: Path,
    expected_source_sha: str,
    *,
    dry_run: bool,
) -> LegacyImportReport:
    before = _read_bound(manifest_path, store.allowed_source_roots)
    result = ingest_manual_confirmation_events(
        store, manifest_path, source_path, expected_source_sha,
    )
    after = _read_bound(manifest_path, store.allowed_source_roots)
    if before != after:
        raise LifecycleError("legacy manifest changed during inventory")
    return LegacyImportReport(
        hashlib.sha256(before).hexdigest(),
        expected_source_sha,
        result.events_seen,
        store.count("examples"),
        len(store.list_available_examples()),
        result.events_added,
        result.examples_added,
        dry_run,
    )


def retract_example(
    store: LearningStore,
    example_id: str,
    *,
    authority: TrustedRetractionAuthority,
    reason: str,
    applied_at: str,
) -> RetractionResult:
    require_identifier(example_id, "example id")
    if not reason.strip() or not applied_at:
        raise LifecycleError("retraction reason and time are required")
    row = store.queries.connection.execute(
        "SELECT revision FROM examples WHERE example_id=? ORDER BY revision DESC LIMIT 1",
        (example_id,),
    ).fetchone()
    if row is None:
        raise QueryError("example was not found")
    latest = store.get_example(example_id, int(row["revision"])).example
    if latest.effective_state == "retracted":
        original_revision = latest.revision - 1
        original = store.get_example(example_id, original_revision).example
        _validate_retraction_authority(authority, original)
        retraction_authority_row = store.queries.connection.execute(
            "SELECT assertion_id FROM authority_assertions WHERE target_id=? AND target_revision=? "
            "AND state='retracted' ORDER BY reviewed_at DESC,assertion_id DESC LIMIT 1",
            (example_id, original_revision),
        ).fetchone()
        if retraction_authority_row is None:
            raise LifecycleError("retracted example is missing its authority record")
        authority_id = str(retraction_authority_row["assertion_id"])
        return _retraction_result(store, example_id, original_revision, latest.revision, authority_id)

    _validate_retraction_authority(authority, latest)
    event_id, authority_id, event_sha = _retraction_identity(
        example_id, latest.revision, authority.action_id,
    )
    retracted_revision = latest.revision + 1
    retraction_value = {
        "example_id": example_id,
        "retracted_revision": latest.revision,
        "reason": reason.strip(),
    }
    retraction_sha = canonical_sha256(retraction_value)
    scope = EXAMPLE_SCOPE[latest.kind]
    with store.atomic():
        store.append_review_event(ReviewEvent(
            event_id,
            event_sha,
            "retract_example",
            ActorKind.HUMAN,
            scope,
            retracted_revision,
            latest.payload_sha,
            retraction_sha,
            retraction_value,
            applied_at,
            latest.event_id,
        ))
        store.add_authority_assertion(AuthorityAssertion(
            authority_id,
            authority.reviewer_id,
            ReviewOrigin.USER_ATTESTED,
            authority.action_id,
            event_sha,
            latest.source_sha,
            latest.example_id,
            latest.revision,
            latest.payload_sha,
            scope,
            applied_at,
            "retracted",
        ))
        store.add_example(Example(
            latest.example_id,
            retracted_revision,
            event_id,
            latest.kind,
            latest.source_sha,
            latest.pdf_index,
            latest.region,
            latest.unit_id,
            latest.asset_id,
            latest.reviewed_value,
            latest.blob_hashes,
            QualityLevel.UNREVIEWED,
            latest.payload_sha,
            "retracted",
            None,
        ))
        store.queries.connection.execute(
            "UPDATE profiles SET state='retracted' WHERE state='active' AND EXISTS ("
            "SELECT 1 FROM example_refs r WHERE r.profile_id=profiles.profile_id "
            "AND r.profile_revision=profiles.revision AND r.example_id=? AND r.example_revision=?)",
            (latest.example_id, latest.revision),
        )
    return _retraction_result(
        store, example_id, latest.revision, retracted_revision, authority_id,
    )


def _validate_retraction_authority(
    authority: TrustedRetractionAuthority,
    example: Example,
) -> None:
    try:
        require_identifier(authority.reviewer_id, "reviewer id")
        require_identifier(authority.action_id, "action id")
        source_sha = require_sha256(authority.source_sha, "source sha")
    except ValueError as exc:
        raise LifecycleError("trusted retraction authority is invalid") from exc
    if (
        authority.actor_kind is not ActorKind.HUMAN
        or authority.example_id != example.example_id
        or source_sha != example.source_sha
        or authority.target_revision != example.revision
    ):
        raise LifecycleError("trusted retraction authority does not match the target")


def _retraction_identity(example_id: str, revision: int, action_id: str) -> tuple[str, str, str]:
    digest = canonical_sha256({
        "action_id": action_id,
        "example_id": example_id,
        "revision": revision,
    })
    return f"retract-{digest[:32]}", f"retract-authority-{digest[:32]}", digest


def _retraction_result(
    store: LearningStore,
    example_id: str,
    active_revision: int,
    retracted_revision: int,
    authority_assertion_id: str,
) -> RetractionResult:
    references = store.references_for(example_id, active_revision)
    return RetractionResult(
        example_id,
        active_revision,
        retracted_revision,
        authority_assertion_id,
        tuple(sorted({
            (value.profile_id, value.profile_revision)
            for value in references
            if value.profile_id is not None and value.profile_revision is not None
        })),
        tuple(sorted({value.snapshot_id for value in references if value.snapshot_id is not None})),
        tuple(sorted({value.run_id for value in references if value.run_id is not None})),
    )
