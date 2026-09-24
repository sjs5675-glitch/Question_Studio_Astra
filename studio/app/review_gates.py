"""Conservative, read-only review gates for canonical question packages."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from difflib import SequenceMatcher
from typing import Any

from .equation_bridge import science_warnings


_PART_NUMBER = re.compile(r"\((\d+)\)\s*$")
_ANSWER_PREFIX = re.compile(r"^\s*(?:(?:\ubaa8\ubc94\s*)?\uc815\ub2f5\s*)?\((\d+)\)(?=\s|$)")


def _issue(
    code: str,
    severity: str,
    path: str,
    message: str,
    *,
    blocking: bool = True,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "path": path,
        "message": message,
        "blocking": blocking,
    }


def _walk(value: Any, path: str):
    yield path, value
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _paragraph_text(paragraph: Mapping[str, Any]) -> str:
    parts: list[str] = []
    inlines = paragraph.get("inlines")
    if not isinstance(inlines, list):
        return ""
    for inline in inlines:
        if not isinstance(inline, Mapping):
            continue
        if inline.get("type") == "text":
            value = inline.get("text")
        elif inline.get("type") == "equation":
            value = inline.get("source_text") or inline.get("latex")
        else:
            value = None
        if isinstance(value, str):
            parts.append(value)
    return "".join(parts)


def _paragraphs(section: Any, path: str) -> list[tuple[str, Mapping[str, Any], str]]:
    return [
        (node_path, node, _paragraph_text(node))
        for node_path, node in _walk(section, path)
        if isinstance(node, Mapping) and node.get("type") == "paragraph"
    ]


def _normalized_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def _near_repeat(question: str, explanation: str) -> bool:
    if len(question) < 40 or len(explanation) < 36:
        return False
    if question in explanation:
        return True
    matcher = SequenceMatcher(None, question, explanation, autojunk=False)
    blocks = matcher.get_matching_blocks()
    matched = sum(block.size for block in blocks)
    longest = max((block.size for block in blocks), default=0)
    return matched / len(question) >= 0.92 and longest >= max(24, len(question) * 0.55)


def _equation_issues(content: Mapping[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for path, node in _walk(content, "$.content"):
        if not isinstance(node, Mapping) or node.get("type") != "equation":
            continue
        script = node.get("hwp_script")
        if not isinstance(script, str) or not script.strip():
            issues.append(
                _issue(
                    "missing_hwp_script",
                    "error",
                    f"{path}.hwp_script",
                    "HWPX \ucd9c\ub825\uc5d0 \ud544\uc694\ud55c HWP \uc218\uc2dd \uc2a4\ud06c\ub9bd\ud2b8\uac00 \uc5c6\uc2b5\ub2c8\ub2e4.",
                )
            )
        latex = node.get("latex")
        warnings = science_warnings(latex) if isinstance(latex, str) else ()
        if warnings:
            issues.append(
                _issue(
                    "science_equation_warning",
                    "warning",
                    path,
                    "; ".join(warnings),
                )
            )
    return issues


def _asset_issues(document: Mapping[str, Any], content: Mapping[str, Any]) -> list[dict[str, Any]]:
    references: dict[str, str] = {}
    selected_figure_ids: set[str] = set()
    for path, node in _walk(content, "$.content"):
        if not isinstance(node, Mapping):
            continue
        if node.get("type") == "figure" and isinstance(node.get("asset_id"), str):
            selected_figure_ids.add(node["asset_id"])
        for key in ("asset_id", "original_asset_id", "source_asset_id"):
            asset_id = node.get(key)
            if isinstance(asset_id, str) and asset_id:
                references.setdefault(asset_id, f"{path}.{key}")

    resources = _mapping(document.get("resources"))
    raw_assets = resources.get("assets")
    assets: dict[str, tuple[str, Mapping[str, Any]]] = {}
    if isinstance(raw_assets, list):
        for index, asset in enumerate(raw_assets):
            if not isinstance(asset, Mapping):
                continue
            asset_id = asset.get("asset_id")
            if isinstance(asset_id, str) and asset_id:
                assets.setdefault(asset_id, (f"$.resources.assets[{index}]", asset))

    issues: list[dict[str, Any]] = []
    for asset_id, reference_path in references.items():
        found = assets.get(asset_id)
        if found is None:
            issues.append(
                _issue(
                    "missing_asset",
                    "error",
                    reference_path,
                    f"\ucc38\uc870 asset '{asset_id}'\uc774 resources.assets\uc5d0 \uc5c6\uc2b5\ub2c8\ub2e4.",
                )
            )
            continue
        asset_path, asset = found
        if asset.get("state") != "ready":
            issues.append(
                _issue(
                    "asset_not_ready",
                    "error",
                    f"{asset_path}.state",
                    f"\ucc38\uc870 asset '{asset_id}'\uc758 state\uac00 ready\uac00 \uc544\ub2d9\ub2c8\ub2e4.",
                )
            )
        review = _mapping(asset.get("review"))
        is_figure = asset.get("role") in {"figure_original", "figure_regenerated"}
        if is_figure and asset_id in selected_figure_ids:
            status = review.get("status")
            if status == "rejected":
                issues.append(
                    _issue(
                        "figure_asset_rejected",
                        "error",
                        f"{asset_path}.review.status",
                        f"참조 그림 asset '{asset_id}'이 rejected 상태입니다.",
                    )
                )
            elif status != "approved":
                issues.append(
                    _issue(
                        "figure_asset_unreviewed",
                        "warning",
                        f"{asset_path}.review.status",
                        f"참조 그림 asset '{asset_id}'을 먼저 검수·승인해야 합니다.",
                    )
                )
        if review.get("boundary_uncertain") is True and (not is_figure or asset_id in selected_figure_ids):
            issues.append(
                _issue(
                    "asset_boundary_uncertain",
                    "warning",
                    f"{asset_path}.review.boundary_uncertain",
                    f"\ucc38\uc870 asset '{asset_id}'\uc758 \ud06c\ub86d \uacbd\uacc4 \ud655\uc778\uc774 \ud544\uc694\ud569\ub2c8\ub2e4.",
                )
            )
    return issues


def _coverage_issues(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    coverage = _mapping(_mapping(document.get("review")).get("coverage"))
    return [
        _issue(
            "incomplete_coverage",
            "error",
            f"$.review.coverage.{section}",
            f"{section} OCR coverage\uac00 incomplete\uc785\ub2c8\ub2e4.",
        )
        for section, state in coverage.items()
        if state == "incomplete"
    ]


def _reprinted_question_issues(content: Mapping[str, Any]) -> list[dict[str, Any]]:
    question = _paragraphs(content.get("question"), "$.content.question")
    explanation = _paragraphs(content.get("explanation"), "$.content.explanation")
    explanation_normalized = [
        (path, _normalized_text(text)) for path, _node, text in explanation
    ]
    issues: list[dict[str, Any]] = []
    for question_path, _node, text in question:
        normalized = _normalized_text(text)
        if len(normalized) < 40:
            continue
        for explanation_path, candidate in explanation_normalized:
            if _near_repeat(normalized, candidate):
                issues.append(
                    _issue(
                        "reprinted_question",
                        "warning",
                        explanation_path,
                        f"\ud574\uc124\uc5d0 \ubb38\uc81c \ubb38\ub2e8({question_path})\uc774 \uac70\uc758 \uadf8\ub300\ub85c \ubc18\ubcf5\ub429\ub2c8\ub2e4.",
                    )
                )
                break
    return issues


def _answer_texts(value: Any, path: str) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for node_path, node in _walk(value, path):
        if not isinstance(node, Mapping):
            continue
        if node.get("type") == "paragraph":
            text = _paragraph_text(node)
            if text:
                texts.append((node_path, text))
        label = node.get("label")
        if isinstance(label, str):
            texts.append((f"{node_path}.label", label))
    return texts


def _sibling_answer_issues(document: Mapping[str, Any], content: Mapping[str, Any]) -> list[dict[str, Any]]:
    printed_number = _mapping(document.get("identity")).get("printed_number")
    match = _PART_NUMBER.search(printed_number) if isinstance(printed_number, str) else None
    if match is None or (current := int(match.group(1))) <= 1:
        return []

    candidates = _answer_texts(content.get("answer"), "$.content.answer")
    explanation = content.get("explanation")
    for path, node in _walk(explanation, "$.content.explanation"):
        if isinstance(node, Mapping) and node.get("role") == "answer":
            text = _paragraph_text(node) if node.get("type") == "paragraph" else ""
            if text:
                candidates.append((path, text))

    for path, text in candidates:
        marker = _ANSWER_PREFIX.match(unicodedata.normalize("NFKC", text))
        if marker and 0 < int(marker.group(1)) < current:
            return [
                _issue(
                    "sibling_answer_contamination",
                    "error",
                    path,
                    f"\uc18c\ubb38\ud56d ({current})\uc758 \ub2f5\uc5d0 \uc55e \uc18c\ubb38\ud56d ({marker.group(1)}) \ub2f5\uc73c\ub85c \ubcf4\uc774\ub294 \ub0b4\uc6a9\uc774 \uc788\uc2b5\ub2c8\ub2e4.",
                )
            ]
    return []


def _bbox_issues(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for path, node in _walk(document, "$"):
        if not isinstance(node, Mapping):
            continue
        region = node.get("source_region")
        if not isinstance(region, Mapping):
            continue
        bbox = region.get("bbox")
        if not isinstance(bbox, Mapping) or bbox.get("unit") != "ratio":
            continue
        values = [bbox.get(key) for key in ("left", "top", "right", "bottom")]
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
            continue
        left, top, right, bottom = values
        width, height = right - left, bottom - top
        if width <= 0.002 + 1e-12 or height <= 0.002 + 1e-12:
            issues.append(
                _issue(
                    "suspicious_bbox",
                    "warning",
                    f"{path}.source_region.bbox",
                    f"source_region bbox\uac00 \ub108\ubb34 \uc587\uc2b5\ub2c8\ub2e4 (width={width:.6g}, height={height:.6g}).",
                )
            )
    return issues


def evaluate_package(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return deterministic approval-blocking issues without mutating *document*."""

    if not isinstance(document, Mapping):
        return []
    content = _mapping(document.get("content"))
    return [
        *_equation_issues(content),
        *_asset_issues(document, content),
        *_coverage_issues(document),
        *_reprinted_question_issues(content),
        *_sibling_answer_issues(document, content),
        *_bbox_issues(document),
    ]
