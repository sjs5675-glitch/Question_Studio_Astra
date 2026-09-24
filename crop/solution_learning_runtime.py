from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from solution_learning.contracts import ExampleKind, JsonObject, canonical_sha256
from solution_learning.profiles import FormatFeatures
from solution_learning.queries import QueryError
from solution_learning.retrieval import RetrievalResult, RetrievalRequest, retrieve_examples
from solution_learning.snapshot import (
    RetrievalSnapshot,
    SnapshotError,
    approval_status,
    create_retrieval_snapshot,
    load_retrieval_snapshot,
)
from solution_learning.store import LearningStore, LearningStoreError

from crop.solution_learning_context import prepare_learning_context, render_learning_attachment

RUNTIME_LEARNING_VERSION: Final = "solution-learning-runtime-v1"


@dataclass(frozen=True, slots=True)
class LearningPromptRequest:
    run_id: str
    source_sha: str
    kind: ExampleKind
    features: FormatFeatures
    current_case_id: str | None
    current_source_evidence: JsonObject
    prompt_version: str
    schema_version: str


@dataclass(frozen=True, slots=True)
class LearningPromptResult:
    mode: str
    attachment: str | None
    cache_identity: str | None
    snapshot: RetrievalSnapshot | None
    current_source_evidence: JsonObject
    selected: tuple[tuple[str, int], ...]
    excluded: tuple[tuple[str, int, str], ...]
    reasons: tuple[str, ...]


class SolutionLearningRuntime:
    def __init__(self, store: LearningStore, *, enabled: bool = False) -> None:
        self.store = store
        self.enabled = enabled

    @property
    def identity(self) -> str:
        return RUNTIME_LEARNING_VERSION if self.enabled else "off"

    def prepare(self, request: LearningPromptRequest) -> LearningPromptResult:
        if not self.enabled:
            return self._normal(request, ("learning_disabled",))
        try:
            snapshot_id = self._snapshot_id(request)
            retrieval: RetrievalResult | None = None
            try:
                snapshot = load_retrieval_snapshot(self.store, snapshot_id)
            except QueryError:
                retrieval = retrieve_examples(self.store, RetrievalRequest(
                    request.source_sha,
                    request.kind,
                    request.features,
                    request.current_case_id,
                ))
                snapshot = create_retrieval_snapshot(
                    self.store,
                    snapshot_id=snapshot_id,
                    run_id=request.run_id,
                    current_source_sha=request.source_sha,
                    retrieval=retrieval,
                    profiles=(),
                    prompt_version=request.prompt_version,
                    schema_version=request.schema_version,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            context = prepare_learning_context(
                self.store, snapshot, request.current_source_evidence,
            )
            attachment = render_learning_attachment(context) if context.references else None
            reasons = list(context.reasons)
            if retrieval is not None and retrieval.reason is not None:
                reasons.append(retrieval.reason)
            selected = tuple(
                (example.example_id, example.revision) for example in snapshot.examples
            )
            excluded = () if retrieval is None else tuple(
                (example.example_id, example.revision, example.reason)
                for example in retrieval.excluded
            )
            cache_identity = None if attachment is None else canonical_sha256({
                "runtime": RUNTIME_LEARNING_VERSION,
                "snapshot_sha": snapshot.snapshot_sha,
                "retriever": snapshot.retriever_version,
                "profile": snapshot.profile_version,
                "prompt": snapshot.prompt_version,
                "schema": snapshot.schema_version,
                "selected": [list(value) for value in selected],
            })
            return LearningPromptResult(
                context.mode,
                attachment,
                cache_identity,
                snapshot,
                context.current_source_evidence,
                selected,
                excluded,
                tuple(dict.fromkeys(reasons)),
            )
        except (OSError, QueryError, SnapshotError, LearningStoreError, sqlite3.DatabaseError):
            return self._normal(request, ("learning_unavailable",))

    def approval_is_current(self, prepared: LearningPromptResult) -> bool:
        if not prepared.attachment:
            return True
        if prepared.snapshot is None:
            return False
        try:
            return approval_status(self.store, prepared.snapshot).status == "eligible"
        except (OSError, QueryError, SnapshotError, sqlite3.DatabaseError):
            return False

    def approval_reasons(self, prepared: LearningPromptResult) -> tuple[str, ...]:
        if prepared.snapshot is None:
            return prepared.reasons
        try:
            return approval_status(self.store, prepared.snapshot).reasons
        except (OSError, QueryError, SnapshotError, sqlite3.DatabaseError):
            return ("learning_unavailable",)

    @staticmethod
    def _snapshot_id(request: LearningPromptRequest) -> str:
        digest = canonical_sha256({
            "runtime": RUNTIME_LEARNING_VERSION,
            "run_id": request.run_id,
            "source_sha": request.source_sha,
            "kind": request.kind.value,
            "case_id": request.current_case_id,
            "features": {
                "page_role": request.features.page_role,
                "column_count": request.features.column_count,
                "numbering_kind": request.features.numbering_kind,
                "answer_layout": request.features.answer_layout,
            },
            "prompt": request.prompt_version,
            "schema": request.schema_version,
        })
        return f"learning-{digest}"

    @staticmethod
    def _normal(
        request: LearningPromptRequest, reasons: tuple[str, ...],
    ) -> LearningPromptResult:
        return LearningPromptResult(
            "normal_analysis", None, None, None, request.current_source_evidence,
            (), (), reasons,
        )
