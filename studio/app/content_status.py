"""Read-only summaries of the actual content saved in a question package."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final, Literal, TypeAlias, TypedDict, assert_never


JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | Mapping[str, "JsonValue"]
ContentState: TypeAlias = Literal[
    "question_only", "question_answer", "question_answer_explanation",
    "question_explanation_without_answer", "question_missing",
]
AnswerKind: TypeAlias = Literal["choice", "content", "true_false", "composite"]


class ContentStatus(TypedDict):
    status: ContentState
    label: str
    has_question: bool
    has_answer: bool
    has_explanation: bool
    input_complete: bool


CONTENT_STATUS_LABELS: Final[Mapping[ContentState, str]] = {
    "question_only": "문제만",
    "question_answer": "문제+정답",
    "question_answer_explanation": "문제+정답+해설",
    "question_explanation_without_answer": "문제+해설(정답 없음)",
    "question_missing": "문제 없음",
}
_PAYLOAD_TEXT: Final = ("text", "latex", "hwp_script", "plain_text", "asset_id")
_PAYLOAD_CHILDREN: Final = ("blocks", "inlines", "items", "rows", "cells")
_ANSWER_KINDS: Final[Mapping[str, AnswerKind]] = {
    "choice": "choice", "content": "content", "true_false": "true_false", "composite": "composite",
}


def _payload_present(value: JsonValue) -> bool:
    """Inspect payload fields only; IDs, labels and review metadata are not content."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_payload_present(child) for child in value)
    if isinstance(value, Mapping):
        return any(
            bool(text.strip()) if isinstance(text := value.get(key), str) else False
            for key in _PAYLOAD_TEXT
        ) or any(_payload_present(value.get(key)) for key in _PAYLOAD_CHILDREN)
    return False


def _answer_present(answer: JsonValue) -> bool:
    if not isinstance(answer, Mapping):
        return False
    stored_kind = answer.get("kind")
    kind = _ANSWER_KINDS.get(stored_kind) if isinstance(stored_kind, str) else None
    match kind:
        case "choice":
            values = answer.get("choice_ids")
            return isinstance(values, list) and any(
                isinstance(value, str) and bool(value.strip()) for value in values
            )
        case "content":
            return _payload_present(answer.get("blocks"))
        case "true_false":
            items = answer.get("items")
            return isinstance(items, list) and any(
                isinstance(item, Mapping) and isinstance(item.get("value"), bool)
                for item in items
            )
        case "composite":
            items = answer.get("items")
            return isinstance(items, list) and any(
                isinstance(item, Mapping) and _answer_present(item.get("answer"))
                for item in items
            )
        case None:
            return False
        case unreachable:
            assert_never(unreachable)


def stored_content_status(document: Mapping[str, JsonValue]) -> ContentStatus:
    """Classify saved inputs independently of link confidence and approval evidence."""
    content = document.get("content")
    content = content if isinstance(content, Mapping) else {}
    has_question = _payload_present(content.get("question"))
    has_answer = _answer_present(content.get("answer"))
    has_explanation = _payload_present(content.get("explanation"))
    state: ContentState = "question_missing"
    if has_question:
        if has_answer:
            state = "question_answer_explanation" if has_explanation else "question_answer"
        else:
            state = "question_explanation_without_answer" if has_explanation else "question_only"
    return {
        "status": state,
        "label": CONTENT_STATUS_LABELS[state],
        "has_question": has_question,
        "has_answer": has_answer,
        "has_explanation": has_explanation,
        "input_complete": has_question and has_answer,
    }


def content_status_counts(documents: Iterable[Mapping[str, JsonValue]]) -> dict[ContentState, int]:
    """Count each saved question once, including absent question content explicitly."""
    counts: dict[ContentState, int] = dict.fromkeys(CONTENT_STATUS_LABELS, 0)
    for document in documents:
        counts[stored_content_status(document)["status"]] += 1
    return counts
