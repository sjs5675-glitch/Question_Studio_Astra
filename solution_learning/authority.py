from __future__ import annotations

import sqlite3

from .contracts import (
    ActorKind,
    DocumentRelation,
    Example,
    ExampleKind,
    LabelScope,
    ReviewOrigin,
    canonical_sha256,
)

EXAMPLE_SCOPE = {
    ExampleKind.HEADING_ROLE: LabelScope.UNIT_BOUNDARY,
    ExampleKind.NUMBERING_SCOPE: LabelScope.UNIT_BOUNDARY,
    ExampleKind.ANSWER_CELL: LabelScope.ANSWER_CELL,
    ExampleKind.DETAIL_BOUNDARY: LabelScope.CROP_BOUNDS,
    ExampleKind.CONTINUATION: LabelScope.UNIT_BOUNDARY,
    ExampleKind.CORRECT_PAIR: LabelScope.CONTENT_MATCH,
    ExampleKind.REJECTED_PAIR: LabelScope.CONTENT_MATCH,
    ExampleKind.SOURCE_NUMBER_ALIAS: LabelScope.NUMBER_MAPPING,
}


def relation_effect_sha(relation: DocumentRelation) -> str:
    return canonical_sha256({
        "relation_id": relation.relation_id,
        "revision": relation.revision,
        "left_source_sha": relation.left_source_sha,
        "right_source_sha": relation.right_source_sha,
        "relation_kind": relation.relation_kind,
        "evidence_hashes": list(relation.evidence_hashes),
        "supersedes_id": relation.supersedes_id,
        "state": relation.state,
    })


def human_authority_is_valid(connection: sqlite3.Connection, example: Example) -> bool:
    row = connection.execute(
        "SELECT a.*, e.actor_kind, e.label_scope AS event_label_scope FROM authority_assertions a JOIN review_events e ON e.source_event_sha=a.source_event_sha WHERE a.assertion_id=?",
        (example.authority_assertion_id,),
    ).fetchone()
    valid = row is not None and row["review_origin"] == ReviewOrigin.USER_ATTESTED.value
    valid = valid and row["actor_kind"] == ActorKind.HUMAN.value and row["state"] == "active"
    valid = valid and row["label_scope"] == row["event_label_scope"] == EXAMPLE_SCOPE[example.kind].value
    valid = valid and row["source_sha"] == example.source_sha and row["target_id"] == example.example_id
    return valid and row["target_revision"] == example.revision and row["expected_effect_sha"] == example.payload_sha


def relation_authority_is_valid(connection: sqlite3.Connection, relation: DocumentRelation) -> bool:
    row = connection.execute(
        "SELECT a.*, e.actor_kind, e.label_scope AS event_label_scope FROM authority_assertions a JOIN review_events e ON e.source_event_sha=a.source_event_sha WHERE a.assertion_id=?",
        (relation.authority_assertion_id,),
    ).fetchone()
    valid = row is not None and row["review_origin"] == ReviewOrigin.USER_ATTESTED.value
    valid = valid and row["actor_kind"] == ActorKind.HUMAN.value and row["state"] == "active"
    valid = valid and row["label_scope"] == row["event_label_scope"]
    valid = valid and row["target_id"] == relation.relation_id and row["target_revision"] == relation.revision
    valid = valid and row["source_sha"] in {relation.left_source_sha, relation.right_source_sha}
    valid = valid and row["expected_effect_sha"] == relation_effect_sha(relation)
    documents = connection.execute(
        "SELECT source_sha,family_id,edition_id FROM documents WHERE source_sha IN (?,?)",
        (relation.left_source_sha, relation.right_source_sha),
    ).fetchall()
    if len(documents) != 2:
        return False
    by_sha = {row["source_sha"]: row for row in documents}
    left, right = by_sha[relation.left_source_sha], by_sha[relation.right_source_sha]
    if relation.relation_kind == "same_edition":
        valid = valid and (left["edition_id"] is None or right["edition_id"] is None or left["edition_id"] == right["edition_id"])
        valid = valid and (left["family_id"] is None or right["family_id"] is None or left["family_id"] == right["family_id"])
    if relation.relation_kind == "same_family":
        valid = valid and (left["family_id"] is None or right["family_id"] is None or left["family_id"] == right["family_id"])
    return valid
