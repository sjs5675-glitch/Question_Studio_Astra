from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astra_runtime import bootstrap, inside, validate_parsed_paths

ASTRA = bootstrap()
import astra_cli
from string import Template
from typing import Any


ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "detection.schema.json"
PROMPT_PATH = ROOT / "prompts" / "propose_boundaries.md"
QUESTION_KINDS = {
    "standalone",
    "shared_group_member",
    "parent_with_subquestions",
    "continuation",
}
TILE_NAMES = ("top-left", "top-right", "bottom-left", "bottom-right")
PAGE_RE = re.compile(r"page-(\d{4})\.json$")
IGNORED_GEOMETRY_ROLES = {"sidecar_hint", "response_area"}
# Boundary workers often include different amounts of surrounding whitespace.
# A shared semantic identity plus a meaningful common region is corroboration;
# the generated crop still needs an independent visual PASS before approval.
CONTENT_OVERLAP_THRESHOLD = 0.35
QWEN_DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
QWEN_USAGE_PATH = ROOT / "app_data" / "qwen_usage.json"
QWEN_INPUT_CNY_PER_MILLION = 2.998
QWEN_OUTPUT_CNY_PER_MILLION = 11.991


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, data: Any) -> None:
    inside(path.resolve(), "output")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def extract_json(text: str) -> Any:
    text = text.strip().lstrip("\ufeff")
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def unwrap_proposal(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list) and len(payload) == 1:
        return unwrap_proposal(payload[0])
    if isinstance(payload, dict) and isinstance(payload.get("questions"), list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        return unwrap_proposal(payload["result"])
    if isinstance(payload, dict) and isinstance(payload.get("structured_output"), dict):
        return unwrap_proposal(payload["structured_output"])
    if isinstance(payload, dict) and isinstance(payload.get("result"), str):
        return unwrap_proposal(extract_json(payload["result"]))
    raise ValueError("structured detection object not found")


def valid_bbox(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(number, int) and not isinstance(number, bool) for number in value)
        and all(0 <= number <= 1000 for number in value)
        and value[0] < value[2]
        and value[1] < value[3]
    )


def proposal_errors(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ["top-level value is not an object"]
    required_top = {"page_type", "layout", "questions", "ignored_regions", "warnings"}
    missing_top = required_top - set(data)
    if missing_top:
        return [f"missing top-level fields: {sorted(missing_top)}"]
    questions = data.get("questions")
    if not isinstance(questions, list):
        return ["questions is not an array"]
    errors: list[str] = []
    required_question = {
        "local_id",
        "printed_number",
        "number_bbox",
        "kind",
        "answer_type",
        "group_label",
        "shared_context_parts",
        "main_parts",
        "subquestions",
        "starts_on_previous_page",
        "continues_next_page",
        "confidence",
    }
    for index, question in enumerate(questions):
        where = f"questions[{index}]"
        if not isinstance(question, dict):
            errors.append(f"{where} is not an object")
            continue
        missing_question = required_question - set(question)
        if missing_question:
            errors.append(f"{where} missing fields: {sorted(missing_question)}")
        if not isinstance(question.get("printed_number"), str):
            errors.append(f"{where}.printed_number is not a string")
        if question.get("kind") not in QUESTION_KINDS:
            errors.append(f"{where}.kind is invalid")
        number_bbox = question.get("number_bbox")
        if number_bbox != [] and not valid_bbox(number_bbox):
            errors.append(f"{where}.number_bbox is invalid")
        if question.get("kind") != "continuation" and number_bbox == []:
            errors.append(f"{where}.number_bbox is required")
        for field in ("shared_context_parts", "main_parts"):
            parts = question.get(field)
            if not isinstance(parts, list):
                errors.append(f"{where}.{field} is not an array")
                continue
            for part_index, part in enumerate(parts):
                if (
                    not isinstance(part, dict)
                    or not isinstance(part.get("role"), str)
                    or part.get("page_offset") not in {-1, 0, 1}
                    or not valid_bbox(part.get("bbox"))
                ):
                    errors.append(f"{where}.{field}[{part_index}] is invalid")
        subquestions = question.get("subquestions")
        if not isinstance(subquestions, list):
            errors.append(f"{where}.subquestions is not an array")
            continue
        for sub_index, subquestion in enumerate(subquestions):
            sub_where = f"{where}.subquestions[{sub_index}]"
            if not isinstance(subquestion, dict) or not isinstance(subquestion.get("label"), str):
                errors.append(f"{sub_where} is invalid")
                continue
            parts = subquestion.get("parts")
            if not isinstance(parts, list) or not parts:
                errors.append(f"{sub_where}.parts is empty or invalid")
                continue
            for part_index, part in enumerate(parts):
                if (
                    not isinstance(part, dict)
                    or not isinstance(part.get("role"), str)
                    or part.get("page_offset") not in {-1, 0, 1}
                    or not valid_bbox(part.get("bbox"))
                ):
                    errors.append(f"{sub_where}.parts[{part_index}] is invalid")
    ignored = data.get("ignored_regions")
    if not isinstance(ignored, list):
        errors.append("ignored_regions is not an array")
    elif any(not isinstance(item, dict) or not valid_bbox(item.get("bbox")) for item in ignored):
        errors.append("ignored_regions contains an invalid bbox")
    if not isinstance(data.get("warnings"), list):
        errors.append("warnings is not an array")
    return errors


def canonical_label(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    numeric = re.findall(r"\d+", text)
    if numeric:
        return "/".join(str(int(item)) for item in numeric)
    return re.sub(r"[^0-9a-z가-힣]+", "", text)


def relationship_signatures(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    groups: dict[str, int] = {}
    signatures: list[dict[str, Any]] = []
    for question in proposal["questions"]:
        raw_group = canonical_label(question.get("group_label"))
        group = None
        if raw_group:
            group = groups.setdefault(raw_group, len(groups))
        signatures.append(
            {
                "number": canonical_label(question.get("printed_number")),
                "kind": question.get("kind"),
                "group": group,
                "subquestions": [
                    canonical_label(item.get("label")) for item in question.get("subquestions", [])
                ],
                "starts_on_previous_page": bool(question.get("starts_on_previous_page")),
                "continues_next_page": bool(question.get("continues_next_page")),
            }
        )
    return signatures


def union_bbox(boxes: list[list[int]]) -> list[int]:
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def boxes_by_offset(parts: list[dict[str, Any]]) -> dict[int, list[list[int]]]:
    result: dict[int, list[list[int]]] = {}
    for part in parts:
        result.setdefault(int(part["page_offset"]), []).append(part["bbox"])
    return result


def question_geometry(question: dict[str, Any]) -> dict[str, list[int]]:
    geometry: dict[str, list[int]] = {}
    all_by_offset: dict[int, list[list[int]]] = {}

    number_bbox = question.get("number_bbox")
    if number_bbox:
        geometry["number@0"] = number_bbox
        all_by_offset.setdefault(0, []).append(number_bbox)

    for category, parts in (
        ("shared", question.get("shared_context_parts", [])),
        ("main", question.get("main_parts", [])),
    ):
        parts = [part for part in parts if part.get("role") not in IGNORED_GEOMETRY_ROLES]
        for offset, boxes in boxes_by_offset(parts).items():
            geometry[f"{category}@{offset}"] = union_bbox(boxes)
            all_by_offset.setdefault(offset, []).extend(boxes)

    for sub_index, subquestion in enumerate(question.get("subquestions", [])):
        label = canonical_label(subquestion.get("label"))
        parts = [
            part
            for part in subquestion.get("parts", [])
            if part.get("role") not in IGNORED_GEOMETRY_ROLES
        ]
        for offset, boxes in boxes_by_offset(parts).items():
            geometry[f"sub:{sub_index}:{label}@{offset}"] = union_bbox(boxes)
            all_by_offset.setdefault(offset, []).extend(boxes)

    for offset, boxes in all_by_offset.items():
        geometry[f"whole@{offset}"] = union_bbox(boxes)
    return geometry


def usable_question(question: Any) -> bool:
    """Validate one question without letting a bad sibling discard the page."""
    if not isinstance(question, dict):
        return False
    probe = {
        "page_type": "problem",
        "layout": "single_column",
        "questions": [question],
        "ignored_regions": [],
        "warnings": [],
    }
    return not proposal_errors(probe)


def valid_questions(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    questions = proposal.get("questions", []) if isinstance(proposal, dict) else []
    if not isinstance(questions, list):
        return []
    return [question for question in questions if usable_question(question)]


def usable_proposal(proposal: Any) -> bool:
    if not isinstance(proposal, dict) or not isinstance(proposal.get("questions"), list):
        return False
    return not proposal["questions"] or bool(valid_questions(proposal))


def group_members(proposal: dict[str, Any], question: dict[str, Any]) -> tuple[str, ...] | None:
    group = canonical_label(question.get("group_label"))
    if not group:
        return None
    return tuple(
        sorted(
            canonical_label(item.get("printed_number"))
            for item in valid_questions(proposal)
            if canonical_label(item.get("group_label")) == group
        )
    )


def question_relationship(
    proposal: dict[str, Any], question: dict[str, Any]
) -> dict[str, Any]:
    return {
        "number": canonical_label(question.get("printed_number")),
        "kind": question.get("kind"),
        "group_members": group_members(proposal, question),
        "subquestions": [
            canonical_label(item.get("label")) for item in question.get("subquestions", [])
        ],
        "starts_on_previous_page": bool(question.get("starts_on_previous_page")),
        "continues_next_page": bool(question.get("continues_next_page")),
    }


def match_questions(
    reference: dict[str, Any], alternative: dict[str, Any]
) -> dict[str, Any]:
    """Match independently detected questions without relying on list position."""
    reference_questions = valid_questions(reference)
    alternative_questions = valid_questions(alternative)
    used: set[int] = set()
    matches: dict[str, dict[str, Any]] = {}
    unmatched_reference: list[str] = []

    for reference_index, question in enumerate(reference_questions):
        local_id = str(question.get("local_id") or f"question-{reference_index + 1}")
        number = canonical_label(question.get("printed_number"))
        numbered = [
            index
            for index, candidate in enumerate(alternative_questions)
            if index not in used and canonical_label(candidate.get("printed_number")) == number
        ]
        exact = [
            index
            for index in numbered
            if question_relationship(reference, question)
            == question_relationship(alternative, alternative_questions[index])
        ]
        candidates = exact or numbered
        if not number:
            candidates = [
                index
                for index, candidate in enumerate(alternative_questions)
                if index not in used
                and question_relationship(reference, question)
                == question_relationship(alternative, candidate)
            ]
        if len(candidates) != 1:
            unmatched_reference.append(local_id)
            continue
        alternative_index = candidates[0]
        used.add(alternative_index)
        matches[local_id] = {
            "reference_index": reference_index,
            "alternative_index": alternative_index,
            "question": alternative_questions[alternative_index],
        }

    return {
        "matches": matches,
        "unmatched_reference": unmatched_reference,
        "unmatched_alternative": [
            {
                "local_id": str(question.get("local_id", "")),
                "printed_number": str(question.get("printed_number", "")),
            }
            for index, question in enumerate(alternative_questions)
            if index not in used
        ],
        "invalid_alternative_count": len(alternative.get("questions", []))
        - len(alternative_questions)
        if isinstance(alternative.get("questions"), list)
        else 0,
    }


def bbox_iou(left: list[int], right: list[int]) -> float:
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0, x1 - x0) * max(0, y1 - y0)
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def bbox_smaller_coverage(left: list[int], right: list[int]) -> float:
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0, x1 - x0) * max(0, y1 - y0)
    smaller = min(
        (left[2] - left[0]) * (left[3] - left[1]),
        (right[2] - right[0]) * (right[3] - right[1]),
    )
    return intersection / smaller if smaller else 0.0


def compare_questions(
    left_proposal: dict[str, Any],
    left_question: dict[str, Any],
    right_proposal: dict[str, Any],
    right_question: dict[str, Any],
    bbox_tolerance: int = 25,
    bbox_iou_threshold: float = 0.75,
) -> dict[str, Any]:
    reasons: list[str] = []
    left_relationship = question_relationship(left_proposal, left_question)
    right_relationship = question_relationship(right_proposal, right_question)
    if left_relationship != right_relationship:
        reasons.append("relationship_mismatch")

    left_geometry = question_geometry(left_question)
    right_geometry = question_geometry(right_question)
    if set(left_geometry) != set(right_geometry):
        reasons.append("geometry_parts_mismatch")
    geometry_checks: list[dict[str, Any]] = []
    for key in sorted(set(left_geometry) & set(right_geometry)):
        left_box, right_box = left_geometry[key], right_geometry[key]
        edge_delta = max(abs(a - b) for a, b in zip(left_box, right_box))
        iou = bbox_iou(left_box, right_box)
        match = edge_delta <= bbox_tolerance or iou >= bbox_iou_threshold
        geometry_checks.append(
            {
                "region": key,
                "left": left_box,
                "right": right_box,
                "max_edge_delta": edge_delta,
                "iou": round(iou, 4),
                "match": match,
            }
        )
        if not match:
            reasons.append(f"{key}_bbox_delta:{edge_delta}")
    left_whole = {key for key in left_geometry if key.startswith("whole@")}
    right_whole = {key for key in right_geometry if key.startswith("whole@")}
    content_geometry = [
        {
            "region": key,
            "left": left_geometry[key],
            "right": right_geometry[key],
            "smaller_coverage": round(
                bbox_smaller_coverage(left_geometry[key], right_geometry[key]), 4
            ),
        }
        for key in sorted(left_whole & right_whole)
    ]
    content_agree = (
        left_relationship == right_relationship
        and bool(left_whole)
        and left_whole == right_whole
        and all(
            check["smaller_coverage"] >= CONTENT_OVERLAP_THRESHOLD
            for check in content_geometry
        )
    )
    return {
        "agree": not reasons,
        "content_agree": content_agree,
        "content_overlap_threshold": CONTENT_OVERLAP_THRESHOLD,
        "content_geometry": content_geometry,
        "bbox_tolerance": bbox_tolerance,
        "bbox_iou_threshold": bbox_iou_threshold,
        "left_relationship": left_relationship,
        "right_relationship": right_relationship,
        "geometry": geometry_checks,
        "reasons": reasons,
    }


def compare_proposals(
    left: dict[str, Any],
    right: dict[str, Any],
    bbox_tolerance: int = 25,
    bbox_iou_threshold: float = 0.75,
) -> dict[str, Any]:
    reasons: list[str] = []
    left_questions = valid_questions(left)
    right_questions = valid_questions(right)
    left_signatures = [question_relationship(left, question) for question in left_questions]
    right_signatures = [question_relationship(right, question) for question in right_questions]
    if len(left_questions) != len(right_questions):
        reasons.append(f"question_count:{len(left_questions)}!={len(right_questions)}")
    matching = match_questions(left, right)
    geometry_checks: list[dict[str, Any]] = []
    for index, question in enumerate(left_questions, start=1):
        local_id = str(question.get("local_id") or f"question-{index}")
        match = matching["matches"].get(local_id)
        if not match:
            reasons.append(f"question_{index}_unmatched")
            continue
        comparison = compare_questions(
            left,
            question,
            right,
            match["question"],
            bbox_tolerance,
            bbox_iou_threshold,
        )
        geometry_checks.extend(
            {"question": index, **check} for check in comparison["geometry"]
        )
        reasons.extend(f"question_{index}_{reason}" for reason in comparison["reasons"])
    if matching["unmatched_alternative"]:
        reasons.append(
            "unmatched_right_questions:"
            + ",".join(item["printed_number"] for item in matching["unmatched_alternative"])
        )
    if matching["invalid_alternative_count"]:
        reasons.append(f"invalid_right_questions:{matching['invalid_alternative_count']}")

    return {
        "agree": not reasons,
        "bbox_tolerance": bbox_tolerance,
        "bbox_iou_threshold": bbox_iou_threshold,
        "left_relationships": left_signatures,
        "right_relationships": right_signatures,
        "geometry": geometry_checks,
        "reasons": reasons,
    }


def resolve_consensus(
    proposals: dict[str, dict[str, Any]],
    bbox_tolerance: int = 25,
    bbox_iou_threshold: float = 0.75,
) -> dict[str, Any]:
    worker_order = ("codex", "gemini", "qwen")
    pairs = tuple(
        (left, right)
        for index, left in enumerate(worker_order)
        for right in worker_order[index + 1 :]
    )
    comparisons: dict[str, Any] = {}
    for left, right in pairs:
        if left in proposals and right in proposals:
            comparisons[f"{left}_{right}"] = compare_proposals(
                proposals[left], proposals[right], bbox_tolerance, bbox_iou_threshold
            )

    codex = proposals.get("codex")
    if not codex:
        return {
            "status": "review",
            "selected_worker": None,
            "comparisons": comparisons,
            "item_decisions": {},
            "matching": {},
            "needs_tiebreaker": True,
            "reasons": ["codex_proposal_missing"],
        }

    matching = {
        worker: match_questions(codex, proposal)
        for worker, proposal in proposals.items()
        if worker != "codex"
    }
    item_decisions: dict[str, dict[str, Any]] = {}
    codex_questions = valid_questions(codex)
    for index, codex_question in enumerate(codex_questions, start=1):
        local_id = str(codex_question.get("local_id") or f"question-{index}")
        item_comparisons: dict[str, Any] = {}
        matched: dict[str, dict[str, Any]] = {}
        for worker in ("gemini", "qwen"):
            match = matching.get(worker, {}).get("matches", {}).get(local_id)
            if not match:
                continue
            matched[worker] = match["question"]
            item_comparisons[f"codex_{worker}"] = compare_questions(
                codex,
                codex_question,
                proposals[worker],
                match["question"],
                bbox_tolerance,
                bbox_iou_threshold,
            )
        if "gemini" in matched and "qwen" in matched:
            item_comparisons["gemini_qwen"] = compare_questions(
                proposals["gemini"],
                matched["gemini"],
                proposals["qwen"],
                matched["qwen"],
                bbox_tolerance,
                bbox_iou_threshold,
            )

        codex_agreements = [
            pair
            for pair, comparison in item_comparisons.items()
            if pair.startswith("codex_") and comparison["agree"]
        ]
        content_agreements = [
            pair
            for pair, comparison in item_comparisons.items()
            if pair.startswith("codex_") and comparison.get("content_agree")
        ]
        alternative_agreements = [
            pair
            for pair, comparison in item_comparisons.items()
            if not pair.startswith("codex_") and comparison["agree"]
        ]
        if codex_agreements:
            status, selected_worker, reasons = "pass", "codex", []
        elif content_agreements:
            status, selected_worker = "review", None
            reasons = ["content_agreement_requires_crop_verification"]
        elif alternative_agreements:
            status, selected_worker = "review", None
            reasons = [
                f"{pair.replace('_', '_and_')}_agree_but_existing_codex_crop_requires_rebuild"
                for pair in alternative_agreements
            ]
        else:
            status, selected_worker = "review", None
            reasons = [
                "fewer_than_two_valid_proposals"
                if not item_comparisons
                else "no_two_independent_proposals_agree"
            ]
            for worker in ("gemini", "qwen"):
                if worker in proposals and worker not in matched:
                    reasons.append(f"{worker}_question_unmatched")
        item_decisions[local_id] = {
            "status": status,
            "selected_worker": selected_worker,
            "printed_number": str(codex_question.get("printed_number", "")),
            "comparisons": item_comparisons,
            "content_agree": bool(content_agreements),
            "content_agreement_pairs": content_agreements,
            "reasons": reasons,
        }

    page_reasons = [
        f"{local_id}:{reason}"
        for local_id, decision in item_decisions.items()
        if decision["status"] != "pass"
        for reason in decision["reasons"]
    ]
    for worker, state in matching.items():
        if state["unmatched_alternative"]:
            page_reasons.append(
                f"{worker}_unmatched_questions:"
                + ",".join(item["printed_number"] for item in state["unmatched_alternative"])
            )
        if state["invalid_alternative_count"]:
            page_reasons.append(
                f"{worker}_invalid_questions:{state['invalid_alternative_count']}"
            )
    status = "pass" if item_decisions and not page_reasons else "review"
    if not item_decisions and not page_reasons:
        page_reasons = ["no_valid_codex_questions"]
    matching_report = {
        worker: {
            "matched": {
                local_id: str(match["question"].get("local_id", ""))
                for local_id, match in state["matches"].items()
            },
            "unmatched_codex": state["unmatched_reference"],
            "unmatched_alternative": state["unmatched_alternative"],
            "invalid_alternative_count": state["invalid_alternative_count"],
        }
        for worker, state in matching.items()
    }
    needs_tiebreaker = any(
        decision["status"] != "pass" and not decision.get("content_agree")
        for decision in item_decisions.values()
    )
    return {
        "status": status,
        "selected_worker": "codex" if status == "pass" else None,
        "comparisons": comparisons,
        "item_decisions": item_decisions,
        "matching": matching_report,
        "needs_tiebreaker": needs_tiebreaker,
        "reasons": page_reasons,
    }


def page_spec(value: str) -> set[int] | None:
    if value.lower() == "all":
        return None
    pages: set[int] = set()
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start, end = (int(item) for item in token.split("-", 1))
            if start > end:
                start, end = end, start
            pages.update(range(start, end + 1))
        else:
            pages.add(int(token))
    if any(page < 1 for page in pages):
        raise ValueError("pages are 1-based and must be positive")
    return pages


def discover_detections(run_dir: Path, selected: set[int] | None) -> list[tuple[int, Path]]:
    detection_dir = run_dir / "detections"
    found: list[tuple[int, Path]] = []
    for path in detection_dir.glob("page-*.json"):
        match = PAGE_RE.fullmatch(path.name)
        if match:
            page = int(match.group(1))
            if selected is None or page in selected:
                found.append((page, path))
    found.sort()
    if not found:
        raise FileNotFoundError(f"no matching detections in {detection_dir}")
    return found


def discover_images(run_dir: Path, page: int, detection_wrapper: dict[str, Any]) -> list[Path]:
    pages_dir = run_dir / "pages"
    meta = detection_wrapper.get("meta", {}) if isinstance(detection_wrapper, dict) else {}
    preferred = None
    if isinstance(meta.get("vision_dpi"), int) and isinstance(meta.get("rotation_correction"), int):
        stem = f"page-{page:04d}-dpi{meta['vision_dpi']}-rot{meta['rotation_correction']:03d}"
        for suffix in (".jpg", ".jpeg", ".png", ".webp"):
            candidate = pages_dir / (stem + suffix)
            if candidate.exists():
                preferred = candidate
                break
    if preferred is None:
        candidates = [
            path
            for path in pages_dir.glob(f"page-{page:04d}-dpi*-rot*.*")
            if "-tile-" not in path.stem and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        ]
        if not candidates:
            raise FileNotFoundError(f"overview image missing for page {page}")
        preferred = sorted(candidates)[-1]
    images = [preferred]
    for name in TILE_NAMES:
        matches = list(preferred.parent.glob(preferred.stem + f"-tile-{name}.*"))
        matches = [path for path in matches if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
        if not matches:
            raise FileNotFoundError(f"{name} tile missing for page {page}")
        images.append(sorted(matches)[0])
    if int(meta.get("context_pages", 0)):
        for neighbor in (page - 1, page + 1):
            if neighbor < 1:
                continue
            dpi = meta.get("vision_dpi")
            dpi_pattern = str(dpi) if isinstance(dpi, int) else "*"
            candidates = [
                path
                for path in pages_dir.glob(f"page-{neighbor:04d}-dpi{dpi_pattern}-rot*.*")
                if "-tile-" not in path.stem
                and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            ]
            if candidates:
                images.append(sorted(candidates)[-1])
    return images


def image_labels(primary_page: int, images: list[Path]) -> list[str]:
    labels = ["PRIMARY overview", *(f"PRIMARY {name} tile" for name in TILE_NAMES)]
    for path in images[len(labels) :]:
        match = re.search(r"page-(\d{4})-dpi", path.name)
        page = int(match.group(1)) if match else primary_page
        if page == primary_page - 1:
            labels.append("PREVIOUS overview; page_offset=-1")
        elif page == primary_page + 1:
            labels.append("NEXT overview; page_offset=+1")
        else:
            labels.append(f"CONTEXT overview; physical page {page}")
    return labels


def build_prompt(page: int, images: list[Path]) -> str:
    labels = image_labels(page, images)
    image_list = "\n".join(
        f"{index}. {label}: {path.resolve()}" for index, (label, path) in enumerate(zip(labels, images), 1)
    )
    return Template(PROMPT_PATH.read_text(encoding="utf-8")).safe_substitute(
        PAGE_NUMBER=page,
        IMAGE_LIST=image_list,
    )


def cache_key(worker: str, model: str, prompt: str, schema: dict[str, Any], images: list[Path]) -> str:
    digest = hashlib.sha256()
    for text in (worker, model, prompt, json.dumps(schema, sort_keys=True)):
        digest.update(text.encode("utf-8"))
        digest.update(b"\0")
    for path in images:
        digest.update(path.name.encode("utf-8"))
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def cached_proposal(path: Path, expected_key: str, force: bool) -> dict[str, Any] | None:
    if force or not path.exists():
        return None
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        result = unwrap_proposal(wrapper)
        if wrapper.get("meta", {}).get("cache_key") == expected_key and usable_proposal(result):
            return result
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return None


def gemini_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        result = {key: gemini_schema(value) for key, value in schema.items() if key != "$schema"}
        if isinstance(result.get("type"), list):
            result["anyOf"] = [{"type": item} for item in result.pop("type")]
        return result
    if isinstance(schema, list):
        return [gemini_schema(item) for item in schema]
    return schema


def run_gemini(
    prompt: str,
    schema: dict[str, Any],
    images: list[Path],
    model: str,
    api_key: str,
    timeout: int,
) -> dict[str, Any]:
    raise astra_cli.AstraError("legacy_text_provider_disabled")


def qwen_endpoint(base_url: str) -> str:
    url = base_url.strip().rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("QWEN_BASE_URL must be an HTTPS URL without embedded credentials")
    return url


def qwen_response_format(schema: dict[str, Any], model: str) -> dict[str, Any]:
    # DashScope's OpenAI-compatible structured-output contract uses JSON mode.
    # Validate the returned object locally against `schema` after parsing.
    del schema, model
    return {"type": "json_object"}


def normalize_qwen_proposal(data: dict[str, Any]) -> dict[str, Any]:
    for question in data.get("questions", []):
        printed_number = question.get("printed_number")
        if isinstance(printed_number, (int, float)) and not isinstance(printed_number, bool):
            question["printed_number"] = str(printed_number).removesuffix(".0")
        if not valid_bbox(question.get("number_bbox")):
            number_part = next(
                (
                    part
                    for part in question.get("main_parts", [])
                    if part.get("role") == "main_number" and valid_bbox(part.get("bbox"))
                ),
                None,
            )
            if number_part:
                question["number_bbox"] = number_part["bbox"]
                question["main_parts"].remove(number_part)
    ignored = data.get("ignored_regions")
    if isinstance(ignored, list):
        data["ignored_regions"] = [
            item for item in ignored if isinstance(item, dict) and valid_bbox(item.get("bbox"))
        ]
    return data


def qwen_budget_config() -> tuple[float, float]:
    try:
        budget_usd = float(os.environ.get("QWEN_MONTHLY_BUDGET_USD", "100"))
        cny_per_usd = float(os.environ.get("QWEN_CNY_PER_USD", "7.2"))
    except ValueError as exc:
        raise ValueError("QWEN_MONTHLY_BUDGET_USD and QWEN_CNY_PER_USD must be numbers") from exc
    if budget_usd < 0 or cny_per_usd <= 0:
        raise ValueError("QWEN_MONTHLY_BUDGET_USD must be non-negative and QWEN_CNY_PER_USD positive")
    return budget_usd, cny_per_usd


def qwen_usage_state(path: Path | None = None) -> dict[str, Any]:
    path = path or QWEN_USAGE_PATH
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    initial = {
        "month": month,
        "estimated_usd": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "calls": 0,
        "blocked": False,
    }
    if not path.exists():
        return initial
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Qwen usage ledger is unreadable: {path}") from exc
    if not isinstance(state, dict) or state.get("month") != month:
        return initial
    return {**initial, **state}


def ensure_qwen_budget(path: Path | None = None) -> dict[str, Any]:
    budget_usd, _ = qwen_budget_config()
    state = qwen_usage_state(path)
    if state.get("blocked") or float(state.get("estimated_usd", 0)) >= budget_usd:
        raise RuntimeError(
            f"Qwen monthly budget reached: ${float(state.get('estimated_usd', 0)):.4f} "
            f"of ${budget_usd:.2f}"
        )
    return state


def record_qwen_usage(
    usage: dict[str, Any],
    path: Path | None = None,
) -> dict[str, Any]:
    path = path or QWEN_USAGE_PATH
    budget_usd, cny_per_usd = qwen_budget_config()
    state = qwen_usage_state(path)
    try:
        prompt_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens")))
        completion_tokens = int(usage.get("completion_tokens", usage.get("output_tokens")))
    except (TypeError, ValueError) as exc:
        state.update(
            {
                "blocked": True,
                "blocked_reason": "provider_response_missing_token_usage",
                "updated_at": utc_now(),
            }
        )
        write_json(path, state)
        raise ValueError("Qwen response did not include usable token counts; budget guard locked") from exc
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("Qwen response contained negative token counts")
    estimated_usd = (
        prompt_tokens * QWEN_INPUT_CNY_PER_MILLION
        + completion_tokens * QWEN_OUTPUT_CNY_PER_MILLION
    ) / 1_000_000 / cny_per_usd
    state.update(
        {
            "estimated_usd": round(float(state.get("estimated_usd", 0)) + estimated_usd, 8),
            "prompt_tokens": int(state.get("prompt_tokens", 0)) + prompt_tokens,
            "completion_tokens": int(state.get("completion_tokens", 0)) + completion_tokens,
            "calls": int(state.get("calls", 0)) + 1,
            "blocked": False,
            "budget_usd": budget_usd,
            "cny_per_usd": cny_per_usd,
            "updated_at": utc_now(),
        }
    )
    write_json(path, state)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_usd": round(estimated_usd, 8),
        "month_total_usd": state["estimated_usd"],
        "monthly_budget_usd": budget_usd,
    }


def run_qwen(
    prompt: str,
    schema: dict[str, Any],
    images: list[Path],
    model: str,
    api_key: str,
    base_url: str,
    timeout: int,
    usage_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raise astra_cli.AstraError("legacy_text_provider_disabled")


def proposal_wrapper(
    worker: str,
    page: int,
    result: dict[str, Any],
    *,
    model: str,
    key: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "worker": worker,
        "page": page,
        "model": model,
        "created_at": utc_now(),
    }
    if key:
        meta["cache_key"] = key
    if source:
        meta["source"] = source
    return {"meta": meta, "status": "ok", "result": result}


def agreement_pairs(consensus: dict[str, Any]) -> list[str]:
    return [
        name.replace("_", "+")
        for name, comparison in consensus.get("comparisons", {}).items()
        if comparison.get("agree")
    ]


def review_reasons(consensus: dict[str, Any]) -> list[str]:
    reasons = list(consensus.get("reasons", []))
    for pair, comparison in consensus.get("comparisons", {}).items():
        if comparison.get("agree"):
            continue
        reasons.extend(f"{pair}:{reason}" for reason in comparison.get("reasons", []))
    return reasons


def coverage_is_verified(
    run_dir: Path,
    item: dict[str, Any],
    pdf_identity: tuple[str | None, str | None] | None = None,
) -> bool:
    from question_crop import coverage_failures

    coverage = item.get("coverage")
    if not isinstance(coverage, dict):
        return False
    receipt = coverage.get("verification_receipt")
    if not isinstance(receipt, dict):
        return False
    return (
        not coverage_failures(run_dir, item, pdf_identity)
        and coverage.get("status") == "verified"
        and coverage.get("proof_sha256") == receipt.get("coverage_proof_sha256")
        and coverage.get("ordered_geometry_sha256")
        == receipt.get("ordered_geometry_sha256")
        and coverage.get("final_crop_sha256") == item.get("output_sha256")
        and coverage.get("final_crop_sha256") == receipt.get("final_crop_sha256")
        and receipt.get("verdict") == "pass"
    )


def verified_crop_is_clean(
    verification: dict[str, Any], coverage_verified: bool
) -> bool:
    return (
        coverage_verified
        and verification.get("verdict") == "pass"
        and verification.get("complete_question") is True
        and verification.get("contains_expected_identifier") is True
        and verification.get("contains_required_context") is True
        and verification.get("contains_answer_or_solution") is False
        and verification.get("contains_other_question") is False
        and verification.get("required_content_missing_or_unreadable") is False
        and verification.get("cut_off_content") is False
        and verification.get("excessive_blank_space") is False
        and float(verification.get("confidence", 0)) >= 0.9
    )


def decision_is_worker_abstention(decision: dict[str, Any]) -> bool:
    reasons = set(decision.get("reasons", []))
    allowed = {
        "fewer_than_two_valid_proposals",
        "gemini_question_unmatched",
        "qwen_question_unmatched",
    }
    return (
        decision.get("status") == "review"
        and not decision.get("comparisons")
        and "fewer_than_two_valid_proposals" in reasons
        and reasons <= allowed
    )


def unresolved_inventory_entries(
    page_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str, str]] = set()
    for result in page_results:
        page = int(result["page"])
        matching = result.get("matching", {})
        if not isinstance(matching, dict):
            continue
        for worker, state in matching.items():
            if not isinstance(state, dict):
                continue
            candidates = state.get("unmatched_alternative", [])
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                local_id = str(candidate.get("local_id", ""))
                printed_number = str(candidate.get("printed_number", ""))
                key = (page, str(worker), local_id, printed_number)
                if key in seen:
                    continue
                seen.add(key)
                entries.append(
                    {
                        "page": page,
                        "owner_question_id": None,
                        "candidate_local_id": local_id,
                        "printed_number": printed_number,
                        "source": str(worker),
                        "status": "unresolved",
                        "reason_codes": ["missing_first_pass_owner"],
                    }
                )
    return entries


def manifest_item_summaries(run_dir: Path, page_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from question_crop import source_pdf_identity

    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    results_by_page = {int(item["page"]): item for item in page_results}
    summaries: list[dict[str, Any]] = []
    pdf_identity = source_pdf_identity(run_dir)
    for item in manifest.get("items", []):
        source_pages = [int(page) for page in item.get("source_pages", [])]
        owner_question_id = str(item.get("owner_question_id") or "")
        owner_match = re.fullmatch(r"p(\d{4})-(.+)", owner_question_id)
        owner_result = results_by_page.get(int(owner_match.group(1))) if owner_match else None
        owner_decision = None
        if owner_result is not None and "item_decisions" in owner_result and owner_match:
            owner_decision = owner_result.get("item_decisions", {}).get(owner_match.group(2))

        verification = item.get("verification") if isinstance(item.get("verification"), dict) else {}
        verification_verdict = verification.get("verdict", "not_run")
        coverage_verified = coverage_is_verified(run_dir, item, pdf_identity)
        local_review = [str(reason) for reason in item.get("review_reasons", [])]
        warnings: list[str] = []
        if (
            verification_verdict == "pass"
            and verification.get("cosmetic_or_foreign_fragment_only") is True
        ):
            warnings.append("cosmetic_fragment")

        if owner_decision is not None:
            content_verified = (
                owner_decision.get("content_agree") and verification_verdict == "pass"
            )
            abstention_verified = (
                decision_is_worker_abstention(owner_decision)
                and verified_crop_is_clean(verification, coverage_verified)
            )
            boundary_verdict = (
                "pass"
                if owner_decision["status"] == "pass" or content_verified or abstention_verified
                else "review"
            )
            if boundary_verdict == "pass":
                if abstention_verified:
                    reasons = ["boundary_abstention_verified"]
                    warnings.append("model_abstention")
                elif owner_decision["status"] == "pass":
                    pairs = agreement_pairs(owner_decision)
                    reasons = [f"boundary_agreement:{pair}" for pair in pairs] or [
                        "boundary_agreement"
                    ]
                else:
                    pairs = [
                        pair.replace("_", "+")
                        for pair in owner_decision.get("content_agreement_pairs", [])
                    ]
                    reasons = [f"boundary_content_agreement:{pair}" for pair in pairs]
                    reasons.extend(
                        f"boundary_diagnostic:{pair}:{reason}"
                        for pair, comparison in owner_decision.get("comparisons", {}).items()
                        if pair.startswith("codex_") and not comparison.get("agree")
                        for reason in comparison.get("reasons", [])
                    )
            else:
                reasons = review_reasons(owner_decision)
        elif owner_result is not None and "item_decisions" in owner_result:
            boundary_verdict = "not_checked"
            reasons = [f"owner_question_not_matched:{owner_question_id}"]
        else:
            # Compatibility for older page-level consensus files and hard page errors.
            page_consensus = [results_by_page[page] for page in source_pages if page in results_by_page]
            missing_pages = [page for page in source_pages if page not in results_by_page]
            if not source_pages or missing_pages:
                boundary_verdict = "not_checked"
                reasons = [
                    f"ensemble_not_run_for_pages:{','.join(map(str, missing_pages or source_pages))}"
                ]
            elif any(result["status"] != "pass" for result in page_consensus):
                boundary_verdict = "review"
                reasons = [
                    f"page_{result['page']}:{reason}"
                    for result in page_consensus
                    if result["status"] != "pass"
                    for reason in review_reasons(result)
                ]
            else:
                boundary_verdict = "pass"
                pairs = sorted(
                    {pair for result in page_consensus for pair in agreement_pairs(result)}
                )
                reasons = [f"boundary_agreement:{pair}" for pair in pairs] or [
                    "boundary_agreement"
                ]

        if verification_verdict == "fail":
            verdict = "fail"
            reasons.extend(str(reason) for reason in verification.get("reasons", []))
        elif not coverage_verified:
            verdict = "review" if verification_verdict in {"pass", "review"} else "not_checked"
            reasons.append("source_coverage_unverified")
        elif boundary_verdict == "not_checked":
            verdict = "not_checked"
        elif boundary_verdict == "review":
            verdict = "review"
        elif verification_verdict == "review" or local_review:
            verdict = "review"
            reasons.extend(local_review)
            reasons.extend(str(reason) for reason in verification.get("reasons", []))
        else:
            verdict = "pass"
        summaries.append(
            {
                "unit_id": item.get("unit_id"),
                "owner_question_id": item.get("owner_question_id"),
                "printed_number": item.get("printed_number"),
                "unit_kind": item.get("unit_kind"),
                "source_pages": source_pages,
                "output_file": item.get("output_file"),
                "verdict": verdict,
                "boundary_verdict": boundary_verdict,
                "verification_verdict": verification_verdict,
                "coverage_verdict": "verified" if coverage_verified else "unresolved",
                "reasons": reasons,
                "warnings": sorted(set(warnings)),
            }
        )
    return summaries


def process_page(run_dir: Path, page: int, detection_path: Path, schema: dict[str, Any], args: argparse.Namespace, gemini_api_key: str | None, qwen_api_key: str | None) -> dict[str, Any]:
    wrapper = json.loads(detection_path.read_text(encoding="utf-8"))
    proposal = unwrap_proposal(wrapper)
    errors = proposal_errors(proposal)
    result = {
        "page": page, "status": "review", "selected_worker": "codex" if not errors else None,
        "workers": {"codex": {"status": "reused", "source": str(detection_path.relative_to(run_dir))}},
        "comparisons": {}, "item_decisions": {}, "matching": {},
        "reasons": errors or ["astra_current_evidence_authority_pending"],
        "result": proposal if not errors else None, "created_at": utc_now(),
    }
    write_json(run_dir / "ensemble" / "consensus" / f"page-{page:04d}.json", result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare Codex with independent Gemini and Qwen question-boundary proposals."
    )
    parser.add_argument("run_dir", type=Path, help="Leaf question_crop.py run directory")
    parser.add_argument("--pages", default="all", help="1-based list/ranges, e.g. 2,5-8")
    parser.add_argument("--bbox-tolerance", type=int, default=25, help="Maximum 0..1000 edge delta")
    parser.add_argument(
        "--bbox-iou-threshold",
        type=float,
        default=0.75,
        help="Accept non-identical boxes when their shared-area IoU reaches this value",
    )
    parser.add_argument(
        "--gemini-model",
        default=os.environ.get("GEMINI_MODEL", "gemini-3.7-flash"),
    )
    parser.add_argument("--no-gemini", action="store_true")
    parser.add_argument(
        "--qwen-model",
        default=os.environ.get("QWEN_MODEL", "qwen3.7-plus"),
    )
    parser.add_argument(
        "--qwen-base-url",
        default=os.environ.get("QWEN_BASE_URL", QWEN_DEFAULT_BASE_URL),
    )
    parser.add_argument("--no-qwen", action="store_true")
    parser.add_argument("--timeout", type=int, default=300, help="Per-worker timeout in seconds")
    parser.add_argument("--force", action="store_true", help="Ignore cached worker proposals")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_parsed_paths(args)
    if not 0 <= args.bbox_tolerance <= 100:
        parser.error("--bbox-tolerance must be between 0 and 100")
    if not 0.5 <= args.bbox_iou_threshold <= 1:
        parser.error("--bbox-iou-threshold must be between 0.5 and 1")
    run_dir = args.run_dir.expanduser().resolve()
    try:
        selected = page_spec(args.pages)
        detections = discover_detections(run_dir, selected)
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    gemini_api_key = None
    qwen_api_key = None
    results: list[dict[str, Any]] = []
    for page, detection_path in detections:
        try:
            results.append(
                process_page(
                    run_dir,
                    page,
                    detection_path,
                    schema,
                    args,
                    gemini_api_key,
                    qwen_api_key,
                )
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failure = {
                "page": page,
                "status": "review",
                "selected_worker": None,
                "workers": {},
                "comparisons": {},
                "reasons": [f"page_processing_error:{type(exc).__name__}:{str(exc)[:1000]}"],
                "result": None,
                "created_at": utc_now(),
            }
            write_json(run_dir / "ensemble" / "consensus" / f"page-{page:04d}.json", failure)
            results.append(failure)

    reviews = [
        {
            "page": item["page"],
            "consensus": f"ensemble/consensus/page-{item['page']:04d}.json",
            "reasons": item["reasons"],
        }
        for item in results
        if item["status"] == "review"
    ]
    item_summaries = manifest_item_summaries(run_dir, results)
    unresolved_inventory = unresolved_inventory_entries(results)
    item_verdict_counts = {
        verdict: sum(item["verdict"] == verdict for item in item_summaries)
        for verdict in ("pass", "review", "fail", "not_checked")
    }
    summary = {
        "run_dir": str(run_dir),
        "pages": len(results),
        "passed": sum(item["status"] == "pass" for item in results),
        "review": len(reviews),
        "astra": {"model": astra_cli.MODEL, "policy_version": astra_cli.POLICY_VERSION, "status": "existing_analysis_reused", "usage": None},
        "gemini": {
            "status": "disabled",
            "model": args.gemini_model,
        },
        "qwen": {
            "status": "disabled",
            "model": args.qwen_model,
            "usage": None,
        },
        "item_verdict_counts": item_verdict_counts,
        "unresolved_inventory": unresolved_inventory,
        "items": item_summaries,
        "created_at": utc_now(),
    }
    write_json(
        run_dir / "ensemble" / "review_queue.json",
        {
            "pages": reviews,
            "items": [item for item in item_summaries if item["verdict"] in {"review", "fail"}],
            "unresolved_inventory": unresolved_inventory,
        },
    )
    write_json(run_dir / "ensemble" / "summary.json", summary)
    console_summary = {key: value for key, value in summary.items() if key != "items"}
    print(json.dumps(console_summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
