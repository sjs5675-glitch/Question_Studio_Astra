from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solution_learning.contracts import ActorKind
from solution_learning.lifecycle import (
    LifecycleError,
    TrustedRetractionAuthority,
    retract_example,
)
from solution_learning.queries import QueryError
from solution_learning.store import LearningStore, LearningStoreError


class LearningLibraryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LearningLibrary:
    root: Path
    allowed_source_roots: tuple[Path, ...]

    def list_examples(self) -> dict[str, Any]:
        try:
            with self._store() as store:
                rows = store.queries.connection.execute(
                    "SELECT e.*,d.locator,d.family_id,d.edition_id,d.dataset_group,"
                    "r.payload_json FROM examples e "
                    "JOIN (SELECT example_id,MAX(revision) revision FROM examples GROUP BY example_id) latest "
                    "USING(example_id,revision) JOIN documents d ON d.source_sha=e.source_sha "
                    "JOIN review_events r ON r.event_id=e.event_id "
                    "WHERE e.effective_state='active' ORDER BY r.applied_at DESC,e.example_id"
                ).fetchall()
                eligible = {
                    (record.example.example_id, record.example.revision)
                    for record in store.list_available_examples(human_confirmed_only=True)
                }
                examples = [self._present(store, row, eligible) for row in rows]
        except (LearningStoreError, OSError, sqlite3.Error) as exc:
            raise LearningLibraryError("학습 사례 저장소를 읽지 못했습니다.") from exc
        reviewed = sum(item["reviewed_eligible"] is True for item in examples)
        impacted = sum(item["impact"]["total"] for item in examples)
        return {
            "summary": {
                "active": len(examples),
                "reviewed_eligible": reviewed,
                "needs_review": len(examples) - reviewed,
                "impacted_uses": impacted,
                "benefit_status": "not_evaluated" if not reviewed else "cases_available",
            },
            "examples": examples,
        }

    def retract(self, example_id: str, *, reviewer_id: str, reason: str) -> dict[str, Any]:
        try:
            with self._store() as store:
                row = store.queries.connection.execute(
                    "SELECT e.revision,e.source_sha FROM examples e "
                    "JOIN (SELECT example_id,MAX(revision) revision FROM examples GROUP BY example_id) latest "
                    "USING(example_id,revision) WHERE e.example_id=? AND e.effective_state='active'",
                    (example_id,),
                ).fetchone()
                if row is None:
                    raise LearningLibraryError("활성 학습 사례를 찾을 수 없습니다.")
                result = retract_example(
                    store,
                    example_id,
                    authority=TrustedRetractionAuthority(
                        ActorKind.HUMAN,
                        reviewer_id,
                        f"studio-retract-{secrets.token_hex(16)}",
                        example_id,
                        str(row["source_sha"]),
                        int(row["revision"]),
                    ),
                    reason=reason,
                    applied_at=datetime.now(timezone.utc).isoformat(),
                )
        except LearningLibraryError:
            raise
        except (LifecycleError, LearningStoreError, QueryError, OSError, sqlite3.Error) as exc:
            raise LearningLibraryError("학습 사례를 철회하지 못했습니다.") from exc
        return {
            "example_id": result.example_id,
            "status": "retracted",
            "retracted_revision": result.retracted_revision,
            "affected": {
                "profiles": len(result.affected_profiles),
                "snapshots": len(result.affected_snapshots),
                "runs": len(result.affected_runs),
            },
        }

    def _store(self) -> LearningStore:
        roots = tuple(root for root in self.allowed_source_roots if root.is_dir())
        return LearningStore(self.root, allowed_source_roots=roots)

    @staticmethod
    def _present(
        store: LearningStore,
        row: sqlite3.Row,
        eligible: set[tuple[str, int]],
    ) -> dict[str, Any]:
        event = json.loads(row["payload_json"])
        payload = event.get("payload") if isinstance(event, dict) else None
        plan = payload.get("plan") if isinstance(payload, dict) else None
        before = plan.get("before") if isinstance(plan, dict) else None
        after = plan.get("after") if isinstance(plan, dict) else json.loads(row["reviewed_value_json"])
        references = store.references_for(str(row["example_id"]), int(row["revision"]))
        profiles = {(ref.profile_id, ref.profile_revision) for ref in references if ref.profile_id is not None}
        snapshots = {ref.snapshot_id for ref in references if ref.snapshot_id is not None}
        runs = {ref.run_id for ref in references if ref.run_id is not None}
        reviewed = (str(row["example_id"]), int(row["revision"])) in eligible
        return {
            "example_id": row["example_id"],
            "revision": row["revision"],
            "kind": row["kind"],
            "captured_at": event.get("applied_at") if isinstance(event, dict) else None,
            "sync_status": "synced",
            "source": {
                "book": Path(row["locator"]).name,
                "sha256": row["source_sha"],
                "family_id": row["family_id"],
                "edition_id": row["edition_id"],
                "dataset_group": row["dataset_group"],
            },
            "format_scope": {
                "pdf_page": int(row["pdf_index"]) + 1,
                "region": json.loads(row["region_json"]) if row["region_json"] else None,
                "item_id": row["unit_id"],
                "asset_id": row["asset_id"],
            },
            "correction": {"before": before, "after": after},
            "reviewed_eligible": reviewed,
            "impact": {
                "profiles": len(profiles),
                "snapshots": len(snapshots),
                "runs": len(runs),
                "total": len(profiles) + len(snapshots) + len(runs),
            },
        }
