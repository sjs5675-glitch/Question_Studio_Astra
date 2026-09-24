from __future__ import annotations

from typing import Literal, TypedDict

from .model import ModelError

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]
type JsonMap = dict[str, Json]
type NodeKind = Literal['question', 'paragraph', 'text', 'equation', 'figure', 'table', 'row', 'cell', 'box', 'choice', 'answer', 'explanation', 'line_break']


class DocumentError(ModelError):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        self.code = code
        self.status = status
        super().__init__(message)


class Provenance(TypedDict):
    snapshot_id: str
    original_pointer: str


class Node(TypedDict):
    id: str
    kind: NodeKind
    children: list[str]
    payload: JsonMap
    provenance: Provenance | None


class Content(TypedDict):
    roots: list[str]
    nodes: list[Node]


class Import(TypedDict):
    snapshot_id: str
    project_id: str
    item_id: str
    source_revision: int
    package_sha256: str
    relative_path: str
    asset_bindings: dict[str, str]


class Asset(TypedDict):
    id: str
    role: str
    section: str
    relative_path: str
    mime_type: str
    size: int
    sha256: str
    width_px: int
    height_px: int


class Group(TypedDict):
    id: str
    source_key: str
    common_node_ids: list[str]
    child_question_ids: list[str]


class Origin(TypedDict):
    set_id: str
    set_revision: int


class Styles(TypedDict):
    defaults: JsonMap
    node_overrides: dict[str, JsonMap]


class Document(TypedDict):
    schema_version: int
    document_id: str
    revision: int
    created_at: str
    updated_at: str
    origin: Origin
    imports: list[Import]
    content: Content
    groups: list[Group]
    assets: list[Asset]
    styles: Styles
    layout: JsonMap
    output_role: Literal['question_only', 'explanation_only', 'combined_end_section']


def mapping(value: Json, field: str) -> JsonMap:
    if not isinstance(value, dict):
        raise DocumentError('invalid_document', f'{field} must be an object')
    return value


def sequence(value: Json, field: str) -> list[Json]:
    if not isinstance(value, list):
        raise DocumentError('invalid_document', f'{field} must be an array')
    return value


def text(value: Json, field: str) -> str:
    if not isinstance(value, str):
        raise DocumentError('invalid_document', f'{field} must be a string')
    return value


def positive(value: Json, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DocumentError('invalid_document', f'{field} must be a positive integer')
    return value
