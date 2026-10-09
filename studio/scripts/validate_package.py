#!/usr/bin/env python3
"""Question JSON Studio 패키지의 JSON Schema와 교차 불변조건을 검사한다."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "schemas" / "question-package.schema.json"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
MEDIA_SUFFIXES = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/bmp": {".bmp"},
}
PIL_FORMAT_MEDIA = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "BMP": "image/bmp",
}


def approval_digest(data: dict[str, Any]) -> str:
    """검수가 승인한 의미 콘텐츠와 자산 계보의 canonical SHA-256."""
    assets = []
    resources = data.get("resources")
    if isinstance(resources, dict) and isinstance(resources.get("assets"), list):
        for asset in resources["assets"]:
            if not isinstance(asset, dict):
                continue
            assets.append(
                {
                    key: asset.get(key)
                    for key in (
                        "asset_id",
                        "section",
                        "role",
                        "state",
                        "path",
                        "media_type",
                        "sha256",
                        "width_px",
                        "height_px",
                        "source_region",
                        "derived_from_asset_id",
                    )
                }
            )
    assets.sort(key=lambda item: str(item.get("asset_id")))
    payload = {
        "identity": data.get("identity"),
        "content": data.get("content"),
        "source": {
            "problem": (data.get("provenance") or {}).get("problem")
            if isinstance(data.get("provenance"), dict)
            else None,
            "solution_links": (data.get("provenance") or {}).get("solution_links")
            if isinstance(data.get("provenance"), dict)
            else None,
        },
        "assets": assets,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class Validator:
    def __init__(self, package_path: Path, check_files: bool) -> None:
        self.package_path = package_path.resolve()
        self.package_root = self.package_path.parent
        self.check_files = check_files
        self._reset()

    def _reset(self) -> None:
        self.errors: list[str] = []
        self.ids: dict[str, str] = {}
        self.asset_ids: set[str] = set()
        self.asset_by_id: dict[str, dict[str, Any]] = {}
        self.asset_paths: dict[str, str] = {}
        self.references: list[tuple[str, str, set[str] | None, set[str] | None]] = []
        self.source_regions: list[tuple[str, dict[str, Any]]] = []
        self.choice_ids: dict[str, set[str]] = {
            "question": set(),
            "explanation": set(),
            "answer": set(),
        }
        self.bogi_item_ids: dict[str, set[str]] = {
            "question": set(),
            "explanation": set(),
            "answer": set(),
        }
        self.answer_choice_refs: list[tuple[str, str]] = []
        self.answer_statement_refs: list[tuple[str, str]] = []
        self.block_statuses: list[tuple[str, str]] = []
        self.equations: list[tuple[str, dict[str, Any]]] = []
        self.figure_links: list[tuple[str, str, str]] = []
        self.content_asset_ids: set[str] = set()
        self._pillow_missing_reported = False

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def register_id(self, value: Any, where: str) -> None:
        if not isinstance(value, str) or not ID_RE.fullmatch(value):
            self.error(where, "안전한 ASCII ID가 아님")
            return
        previous = self.ids.get(value)
        if previous is not None:
            self.error(where, f"ID 중복; 처음 사용한 위치={previous}")
            return
        self.ids[value] = where

    def add_reference(
        self,
        value: Any,
        where: str,
        roles: Iterable[str] | None = None,
        sections: Iterable[str] | None = None,
    ) -> None:
        if not isinstance(value, str):
            self.error(where, "asset ID 문자열이 필요함")
            return
        self.references.append(
            (
                value,
                where,
                set(roles) if roles is not None else None,
                set(sections) if sections is not None else None,
            )
        )

    def check_bbox(self, value: Any, where: str) -> None:
        if not isinstance(value, dict):
            self.error(where, "bbox 객체가 필요함")
            return
        if value.get("origin") != "top-left":
            self.error(where, "origin은 top-left여야 함")
        unit = value.get("unit")
        coords = [value.get(key) for key in ("left", "top", "right", "bottom")]
        if not isinstance(unit, str) or unit not in {"ratio", "pixel"}:
            self.error(where, "unit은 ratio 또는 pixel이어야 함")
            return
        if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in coords):
            self.error(where, "좌표 네 개가 모두 숫자여야 함")
            return
        if any(not math.isfinite(float(item)) for item in coords):
            self.error(where, "좌표는 유한한 숫자여야 함")
            return
        left, top, right, bottom = coords
        if left < 0 or top < 0 or right <= left or bottom <= top:
            self.error(where, "0 ≤ left < right, 0 ≤ top < bottom 조건 위반")
        if unit == "ratio" and any(item > 1 for item in coords):
            self.error(where, "ratio 좌표는 1을 넘을 수 없음")

    def check_source_region(
        self,
        value: Any,
        where: str,
        roles: Iterable[str] | None = None,
        sections: Iterable[str] | None = None,
    ) -> None:
        if not isinstance(value, dict):
            self.error(where, "source_region 객체가 필요함")
            return
        self.source_regions.append((where, value))
        self.add_reference(
            value.get("source_asset_id"),
            f"{where}.source_asset_id",
            roles,
            sections,
        )
        self.check_bbox(value.get("bbox"), f"{where}.bbox")

    def check_inline_runs(self, inlines: Any, where: str) -> None:
        if not isinstance(inlines, list) or not inlines:
            self.error(where, "비어 있지 않은 inline 배열이 필요함")
            return
        for index, run in enumerate(inlines):
            run_where = f"{where}[{index}]"
            if not isinstance(run, dict):
                self.error(run_where, "run 객체가 필요함")
                continue
            run_type = run.get("type")
            if run_type == "equation":
                self.equations.append((run_where, run))
                source_format = run.get("source_format")
                expected = (
                    {
                        "latex": "latex",
                        "hwp_script": "hwp_script",
                        "plain": "plain_text",
                    }.get(source_format)
                    if isinstance(source_format, str)
                    else None
                )
                if expected is None:
                    self.error(f"{run_where}.source_format", "지원하지 않는 수식 원본 형식")
                elif not run.get(expected):
                    self.error(run_where, f"source_format={source_format}에 {expected}가 필요함")
            elif run_type == "text":
                if not isinstance(run.get("text"), str):
                    self.error(f"{run_where}.text", "문자열이 필요함")
            elif run_type != "line_break":
                self.error(run_where, "지원하지 않는 inline run type")

    def walk_blocks(
        self,
        blocks: Any,
        where: str,
        section: str,
        *,
        choice_context: str | None = None,
        allow_empty: bool = False,
    ) -> None:
        if not isinstance(blocks, list):
            self.error(where, "block 배열이 필요함")
            return
        if not blocks and not allow_empty:
            self.error(where, "비어 있지 않은 block 배열이 필요함")
            return
        for index, block in enumerate(blocks):
            self.walk_block(
                block,
                f"{where}[{index}]",
                section,
                choice_context=choice_context,
            )

    def walk_block(
        self,
        block: Any,
        where: str,
        section: str,
        *,
        choice_context: str | None,
    ) -> None:
        if not isinstance(block, dict):
            self.error(where, "block 객체가 필요함")
            return
        block_id = block.get("block_id")
        self.register_id(block_id, f"{where}.block_id")
        if isinstance(block_id, str):
            self.block_statuses.append((block_id, block.get("review_status")))
        if "source_region" in block:
            asset_section = "question" if section == "answer" else section
            self.check_source_region(
                block["source_region"],
                f"{where}.source_region",
                {"source_crop", "source_page"},
                {asset_section, "shared"},
            )
            region = block.get("source_region")
            if isinstance(region, dict) and isinstance(region.get("source_asset_id"), str):
                self.content_asset_ids.add(region["source_asset_id"])

        block_type = block.get("type")
        if block_type == "paragraph":
            self.check_inline_runs(block.get("inlines"), f"{where}.inlines")
            return

        if block_type == "figure":
            role = block.get("role")
            asset_section = "question" if section == "answer" else section
            allowed_sections = {asset_section, "shared"}
            self.add_reference(
                block.get("asset_id"),
                f"{where}.asset_id",
                {"figure_original", "figure_regenerated", "figure_generated", "table_snapshot"},
                allowed_sections,
            )
            self.add_reference(
                block.get("original_asset_id"),
                f"{where}.original_asset_id",
                {"figure_original", "figure_generated", "table_snapshot"},
                allowed_sections,
            )
            if isinstance(block.get("asset_id"), str):
                self.content_asset_ids.add(block["asset_id"])
            if isinstance(block.get("asset_id"), str) and isinstance(
                block.get("original_asset_id"), str
            ):
                self.figure_links.append(
                    (block["asset_id"], block["original_asset_id"], where)
                )
            if role == "choice" and choice_context is None:
                self.error(where, "role=choice 그림은 choices item 안에 있어야 함")
            if role != "choice" and choice_context is not None:
                self.error(where, "choice item 안 그림은 role=choice여야 함")
            return

        if block_type == "bogi":
            items = block.get("items")
            if not isinstance(items, list) or not items:
                self.error(f"{where}.items", "비어 있지 않은 보기 항목이 필요함")
                return
            labels: set[str] = set()
            for index, item in enumerate(items):
                item_where = f"{where}.items[{index}]"
                if not isinstance(item, dict):
                    self.error(item_where, "보기 항목 객체가 필요함")
                    continue
                item_id = item.get("item_id")
                self.register_id(item_id, f"{item_where}.item_id")
                if isinstance(item_id, str):
                    self.bogi_item_ids[section].add(item_id)
                label = item.get("label")
                if isinstance(label, str):
                    if not label and len(items) > 1:
                        self.error(
                            f"{item_where}.label",
                            "여러 보기 항목에서는 빈 라벨을 허용하지 않음",
                        )
                    if label in labels:
                        self.error(f"{item_where}.label", "보기 라벨 중복")
                    labels.add(label)
                self.walk_blocks(
                    item.get("blocks"),
                    f"{item_where}.blocks",
                    section,
                    choice_context=choice_context,
                )
            return

        if block_type == "box":
            self.walk_blocks(
                block.get("blocks"),
                f"{where}.blocks",
                section,
                choice_context=choice_context,
            )
            return

        if block_type == "choices":
            items = block.get("items")
            if not isinstance(items, list) or len(items) < 2:
                self.error(f"{where}.items", "선택지는 최소 2개가 필요함")
                return
            labels: set[str] = set()
            for index, item in enumerate(items):
                item_where = f"{where}.items[{index}]"
                if not isinstance(item, dict):
                    self.error(item_where, "선택지 객체가 필요함")
                    continue
                choice_id = item.get("choice_id")
                self.register_id(choice_id, f"{item_where}.choice_id")
                if isinstance(choice_id, str):
                    self.choice_ids[section].add(choice_id)
                label = item.get("label")
                if isinstance(label, str):
                    if label in labels:
                        self.error(f"{item_where}.label", "선택지 라벨 중복")
                    labels.add(label)
                self.walk_blocks(
                    item.get("blocks"),
                    f"{item_where}.blocks",
                    section,
                    choice_context=choice_id if isinstance(choice_id, str) else None,
                )
            return

        if block_type == "table":
            self.check_table(block, where, section, choice_context)
            return

        self.error(f"{where}.type", f"지원하지 않는 block type={block_type!r}")

    def check_table(
        self,
        block: dict[str, Any],
        where: str,
        section: str,
        choice_context: str | None,
    ) -> None:
        column_count = block.get("column_count")
        rows = block.get("rows")
        if (
            not isinstance(column_count, int)
            or isinstance(column_count, bool)
            or column_count < 1
        ):
            self.error(f"{where}.column_count", "1 이상의 정수가 필요함")
            column_count = 0
        if not isinstance(rows, list) or not rows:
            self.error(f"{where}.rows", "표 행이 필요함")
            return

        active_rowspans = [0] * column_count
        for row_index, row in enumerate(rows):
            row_where = f"{where}.rows[{row_index}]"
            cells = row.get("cells") if isinstance(row, dict) else None
            if not isinstance(cells, list):
                self.error(f"{row_where}.cells", "표 셀 배열이 필요함")
                continue
            occupied = [remaining > 0 for remaining in active_rowspans]
            next_rowspans = [max(remaining - 1, 0) for remaining in active_rowspans]
            cursor = 0
            for cell_index, cell in enumerate(cells):
                cell_where = f"{row_where}.cells[{cell_index}]"
                if not isinstance(cell, dict):
                    self.error(cell_where, "표 셀 객체가 필요함")
                    continue
                row_span = cell.get("row_span")
                col_span = cell.get("col_span")
                valid_span = True
                for name, value in (("row_span", row_span), ("col_span", col_span)):
                    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                        self.error(f"{cell_where}.{name}", "1 이상의 정수가 필요함")
                        valid_span = False
                self.walk_blocks(
                    cell.get("blocks"),
                    f"{cell_where}.blocks",
                    section,
                    choice_context=choice_context,
                    allow_empty=True,
                )
                if not valid_span or column_count == 0:
                    continue
                while cursor < column_count and occupied[cursor]:
                    cursor += 1
                end = cursor + col_span
                if end > column_count or any(occupied[cursor:end]):
                    self.error(cell_where, "col_span이 표 범위를 넘거나 기존 병합 셀과 겹침")
                    continue
                for column in range(cursor, end):
                    occupied[column] = True
                    if row_span > 1:
                        next_rowspans[column] = max(next_rowspans[column], row_span - 1)
                cursor = end
            if column_count and not all(occupied):
                self.error(row_where, "병합을 반영한 뒤 채워지지 않은 열이 있음")
            active_rowspans = next_rowspans
        if any(active_rowspans):
            self.error(where, "row_span이 제공된 마지막 행을 넘어감")

    def walk_answer(self, answer: Any, where: str = "content.answer") -> None:
        if answer is None:
            return
        if not isinstance(answer, dict):
            self.error(where, "answer 객체 또는 null이 필요함")
            return
        kind = answer.get("kind")
        if kind == "choice":
            choice_ids = answer.get("choice_ids")
            if not isinstance(choice_ids, list) or not choice_ids:
                self.error(f"{where}.choice_ids", "비어 있지 않은 배열이 필요함")
                return
            for index, choice_id in enumerate(choice_ids):
                if isinstance(choice_id, str):
                    self.answer_choice_refs.append((choice_id, f"{where}.choice_ids[{index}]"))
                else:
                    self.error(f"{where}.choice_ids[{index}]", "ID 문자열이 필요함")
            return
        if kind == "content":
            self.walk_blocks(answer.get("blocks"), f"{where}.blocks", "answer")
            return
        if kind == "true_false":
            items = answer.get("items")
            if not isinstance(items, list) or not items:
                self.error(f"{where}.items", "참/거짓 정답 항목이 필요함")
                return
            for index, item in enumerate(items):
                item_where = f"{where}.items[{index}]"
                if not isinstance(item, dict):
                    self.error(item_where, "항목 객체가 필요함")
                    continue
                statement_id = item.get("statement_id")
                if isinstance(statement_id, str):
                    self.answer_statement_refs.append((statement_id, f"{item_where}.statement_id"))
                else:
                    self.error(f"{item_where}.statement_id", "ID 문자열이 필요함")
            return
        if kind == "composite":
            items = answer.get("items")
            if not isinstance(items, list) or not items:
                self.error(f"{where}.items", "복합 정답 항목이 필요함")
                return
            for index, item in enumerate(items):
                item_where = f"{where}.items[{index}]"
                if not isinstance(item, dict):
                    self.error(item_where, "항목 객체가 필요함")
                    continue
                self.register_id(item.get("item_id"), f"{item_where}.item_id")
                self.walk_answer(item.get("answer"), f"{item_where}.answer")
            return
        if kind != "unknown":
            self.error(f"{where}.kind", f"지원하지 않는 answer kind={kind!r}")

    def check_asset(self, asset: Any, index: int) -> None:
        where = f"resources.assets[{index}]"
        if not isinstance(asset, dict):
            self.error(where, "asset 객체가 필요함")
            return
        asset_id = asset.get("asset_id")
        self.register_id(asset_id, f"{where}.asset_id")
        if isinstance(asset_id, str):
            self.asset_ids.add(asset_id)
            self.asset_by_id[asset_id] = asset

        path_value = asset.get("path")
        if not isinstance(path_value, str) or not path_value:
            self.error(f"{where}.path", "상대경로 문자열이 필요함")
        else:
            parts = path_value.split("/")
            pure = PurePosixPath(path_value)
            unsafe = (
                "\\" in path_value
                or ":" in path_value
                or pure.is_absolute()
                or any(part in {"", ".", ".."} for part in parts)
                or pure.as_posix() != path_value
            )
            if unsafe:
                self.error(f"{where}.path", "정규화된 패키지 내부 POSIX 상대경로만 허용")
            normalized = pure.as_posix().casefold()
            previous = self.asset_paths.get(normalized)
            if previous is not None:
                self.error(f"{where}.path", f"다른 asset과 같은 실제 경로; 처음 사용={previous}")
            self.asset_paths[normalized] = where
            media_type = asset.get("media_type")
            if not isinstance(media_type, str):
                self.error(f"{where}.media_type", "문자열이 필요함")
            else:
                allowed = MEDIA_SUFFIXES.get(media_type)
                if allowed is None:
                    self.error(f"{where}.media_type", "PNG/JPEG/BMP만 허용")
                elif pure.suffix.lower() not in allowed:
                    self.error(f"{where}.path", f"확장자가 media_type={media_type}과 맞지 않음")

        digest = asset.get("sha256")
        if digest is not None and (not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)):
            self.error(f"{where}.sha256", "64자리 SHA-256 hex가 필요함")
        asset_role = asset.get("role")
        asset_section = asset.get("section")
        allowed_sections = (
            {asset_section, "shared"} if isinstance(asset_section, str) else None
        )
        if "source_region" in asset:
            source_roles = (
                {"source_crop", "source_page"}
                if isinstance(asset_role, str)
                and asset_role in {"figure_original", "table_snapshot"}
                else None
            )
            self.check_source_region(
                asset["source_region"],
                f"{where}.source_region",
                source_roles,
                allowed_sections,
            )
        derived = asset.get("derived_from_asset_id")
        if derived is not None:
            derived_roles = {
                "figure_original": {"source_crop", "source_page"},
                "figure_regenerated": {"figure_original", "figure_regenerated"},
                "table_snapshot": {"source_crop", "source_page"},
            }.get(asset_role) if isinstance(asset_role, str) else None
            self.add_reference(
                derived,
                f"{where}.derived_from_asset_id",
                derived_roles,
                allowed_sections,
            )
        generation = asset.get("generation")
        if isinstance(generation, dict):
            fallback = generation.get("fallback_asset_id")
            if fallback is not None:
                self.add_reference(
                    fallback,
                    f"{where}.generation.fallback_asset_id",
                    {"figure_original"} if asset_role == "figure_regenerated" else None,
                    allowed_sections,
                )
            prompt_text = generation.get("prompt_text")
            prompt_sha = generation.get("prompt_sha256")
            if isinstance(prompt_text, str) and isinstance(prompt_sha, str):
                actual = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
                if actual.lower() != prompt_sha.lower():
                    self.error(f"{where}.generation.prompt_sha256", "prompt_text의 SHA-256과 다름")
        if asset.get("state") == "ready":
            for key in ("sha256", "width_px", "height_px"):
                if not asset.get(key):
                    self.error(f"{where}.{key}", "state=ready asset에 필요한 메타데이터")
            if asset.get("role") == "figure_regenerated":
                if not isinstance(generation, dict) or generation.get("status") != "succeeded":
                    self.error(f"{where}.generation.status", "ready 재생성 asset은 succeeded여야 함")

    def check_source_region_bounds(self) -> None:
        for where, region in self.source_regions:
            source = self.asset_by_id.get(region.get("source_asset_id"))
            bbox = region.get("bbox")
            if not source or not isinstance(bbox, dict) or bbox.get("unit") != "pixel":
                continue
            width = source.get("width_px")
            height = source.get("height_px")
            if isinstance(width, int) and bbox.get("right", 0) > width:
                self.error(where, "right가 원본 width_px를 넘음")
            if isinstance(height, int) and bbox.get("bottom", 0) > height:
                self.error(where, "bottom이 원본 height_px를 넘음")

    def check_asset_graph(self) -> None:
        graph: dict[str, list[str]] = {asset_id: [] for asset_id in self.asset_ids}
        for asset_id, asset in self.asset_by_id.items():
            candidates = [asset.get("derived_from_asset_id")]
            region = asset.get("source_region")
            if isinstance(region, dict):
                candidates.append(region.get("source_asset_id"))
            generation = asset.get("generation")
            if isinstance(generation, dict):
                candidates.append(generation.get("fallback_asset_id"))
            for target in candidates:
                if isinstance(target, str) and target in self.asset_ids:
                    graph[asset_id].append(target)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str, chain: list[str]) -> None:
            if node in visiting:
                self.error(f"asset[{node}]", f"asset 계보 순환: {' -> '.join(chain + [node])}")
                return
            if node in visited:
                return
            visiting.add(node)
            for target in graph[node]:
                visit(target, chain + [node])
            visiting.remove(node)
            visited.add(node)

        for asset_id in graph:
            visit(asset_id, [])

    def check_figure_lineage(self) -> None:
        for selected_id, original_id, where in self.figure_links:
            current = selected_id
            seen: set[str] = set()
            while current != original_id and current not in seen:
                seen.add(current)
                asset = self.asset_by_id.get(current)
                current = (
                    asset.get("derived_from_asset_id")
                    if isinstance(asset, dict)
                    else None
                )
                if not isinstance(current, str):
                    break
            if current != original_id:
                self.error(
                    f"{where}.asset_id",
                    "선택 asset의 derived_from 계보가 original_asset_id에 도달하지 않음",
                )
            selected = self.asset_by_id.get(selected_id)
            generation = selected.get("generation") if isinstance(selected, dict) else None
            if isinstance(generation, dict):
                fallback = generation.get("fallback_asset_id")
                if fallback is not None and fallback != original_id:
                    self.error(
                        f"{where}.asset_id",
                        "선택 생성 asset의 fallback이 original_asset_id와 다름",
                    )

    def check_file_for_asset(self, asset_id: str, asset: dict[str, Any]) -> None:
        path_value = asset.get("path")
        if not isinstance(path_value, str):
            return
        pure = PurePosixPath(path_value)
        candidate = (self.package_root / Path(*pure.parts)).resolve()
        try:
            candidate.relative_to(self.package_root)
        except ValueError:
            self.error(f"asset[{asset_id}].path", "패키지 밖으로 벗어남")
            return
        if not candidate.is_file():
            self.error(f"asset[{asset_id}].path", f"파일 없음: {candidate}")
            return

        expected_sha = asset.get("sha256")
        if isinstance(expected_sha, str):
            digest = hashlib.sha256()
            with candidate.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest().lower() != expected_sha.lower():
                self.error(f"asset[{asset_id}].sha256", "실제 파일 해시와 다름")

        try:
            from PIL import Image, UnidentifiedImageError
        except ModuleNotFoundError:
            if not self._pillow_missing_reported:
                self.error("files", "실제 이미지 검증에는 Pillow가 필요함: pip install -r requirements.txt")
                self._pillow_missing_reported = True
            return
        try:
            with Image.open(candidate) as image:
                image.verify()
            with Image.open(candidate) as image:
                actual_media = PIL_FORMAT_MEDIA.get(image.format or "")
                if actual_media != asset.get("media_type"):
                    self.error(f"asset[{asset_id}].media_type", f"실제 형식은 {actual_media!r}")
                expected_size = (asset.get("width_px"), asset.get("height_px"))
                if all(isinstance(value, int) for value in expected_size) and image.size != expected_size:
                    self.error(f"asset[{asset_id}]", f"실제 크기 {image.size} != 선언 {expected_size}")
        except (OSError, SyntaxError, UnidentifiedImageError) as exc:
            self.error(f"asset[{asset_id}].path", f"유효한 래스터 이미지가 아님: {exc}")

    def validate(self, data: Any) -> list[str]:
        self._reset()
        if not isinstance(data, dict):
            return ["root: JSON 객체가 필요함"]
        if data.get("schema_version") != "0.1.0":
            self.error("schema_version", "지원 버전은 0.1.0")
        self.register_id(data.get("item_id"), "item_id")

        identity = data.get("identity")
        if not isinstance(identity, dict):
            self.error("identity", "문항 식별 정보 객체가 필요함")
        else:
            if not isinstance(identity.get("printed_number"), str):
                self.error("identity.printed_number", "문자열이 필요함")
            if identity.get("unit_kind") not in {
                "question",
                "main_question",
                "subquestion",
                "group",
                "shared_group_bundle",
            }:
                self.error("identity.unit_kind", "지원되는 문항 단위가 아님")
            if identity.get("answer_type") not in {
                "multiple_choice",
                "short_answer",
                "descriptive",
                "true_false",
                "composite",
                "mixed",
                "unknown",
            }:
                self.error("identity.answer_type", "지원되는 정답 형식이 아님")

        resources = data.get("resources")
        assets = resources.get("assets") if isinstance(resources, dict) else None
        if not isinstance(assets, list):
            self.error("resources.assets", "배열이 필요함")
            assets = []
        for index, asset in enumerate(assets):
            self.check_asset(asset, index)

        provenance = data.get("provenance")
        if isinstance(provenance, dict):
            problem = provenance.get("problem")
            if isinstance(problem, dict):
                self.add_reference(
                    problem.get("crop_asset_id"),
                    "provenance.problem.crop_asset_id",
                    {"source_crop"},
                    {"question"},
                )
                page_count = problem.get("document_page_count")
                pages = problem.get("source_pages")
                page_keys: set[tuple[Any, Any, Any]] = set()
                if isinstance(pages, list):
                    for index, page in enumerate(pages):
                        page_where = f"provenance.problem.source_pages[{index}]"
                        if not isinstance(page, dict):
                            self.error(page_where, "page source 객체가 필요함")
                            continue
                        pdf_index = page.get("pdf_page_index")
                        if isinstance(page_count, int) and isinstance(pdf_index, int) and pdf_index >= page_count:
                            self.error(f"{page_where}.pdf_page_index", "문서 페이지 수를 넘음")
                        raw = page.get("raw_source_page")
                        base = page.get("raw_page_base")
                        page_keys.add((raw, base, pdf_index))
                        if isinstance(raw, int) and isinstance(pdf_index, int):
                            expected = pdf_index if base == "zero_based" else pdf_index + 1 if base == "one_based" else None
                            if expected is not None and raw != expected:
                                self.error(page_where, "raw_source_page와 pdf_page_index가 base 규칙상 불일치")
                        page_asset = page.get("page_asset_id")
                        if page_asset is not None:
                            self.add_reference(
                                page_asset,
                                f"{page_where}.page_asset_id",
                                {"source_page"},
                                {"question", "shared"},
                            )
                            if not isinstance(page.get("render_dpi"), int):
                                self.error(f"{page_where}.render_dpi", "page asset에는 render DPI가 필요함")
                parts = problem.get("source_parts")
                if isinstance(parts, list):
                    for index, part in enumerate(parts):
                        part_where = f"provenance.problem.source_parts[{index}]"
                        if not isinstance(part, dict):
                            self.error(part_where, "source part 객체가 필요함")
                            continue
                        raw = part.get("raw_source_page")
                        base = part.get("raw_page_base")
                        pdf_index = part.get("pdf_page_index")
                        if isinstance(page_count, int) and isinstance(pdf_index, int) and pdf_index >= page_count:
                            self.error(f"{part_where}.pdf_page_index", "문서 페이지 수를 넘음")
                        if isinstance(raw, int) and isinstance(pdf_index, int):
                            expected = pdf_index if base == "zero_based" else pdf_index + 1 if base == "one_based" else None
                            if expected is not None and raw != expected:
                                self.error(part_where, "raw_source_page와 pdf_page_index가 base 규칙상 불일치")
                        if page_keys and (raw, base, pdf_index) not in page_keys:
                            self.error(part_where, "source_pages에 대응하는 page가 없음")
                        if part.get("region") is not None:
                            self.check_source_region(
                                part.get("region"),
                                f"{part_where}.region",
                                {"source_crop", "source_page"},
                                {"question", "shared"},
                            )
            else:
                source_system = provenance.get("source_system")
                generated = isinstance(source_system, dict) and source_system.get("name") == "item_generation"
                if not generated:
                    self.error("provenance.problem", "객체가 필요함")
                elif not isinstance(provenance.get("generation"), dict):
                    self.error("provenance.generation", "생성 문항에는 generation 기록이 필요함")

            links = provenance.get("solution_links")
            if isinstance(links, list):
                for index, link in enumerate(links):
                    link_where = f"provenance.solution_links[{index}]"
                    if not isinstance(link, dict):
                        self.error(link_where, "solution link 객체가 필요함")
                        continue
                    self.add_reference(
                        link.get("source_asset_id"),
                        f"{link_where}.source_asset_id",
                        {"source_crop"},
                        {"explanation"},
                    )
                    page_count = link.get("document_page_count")
                    parts = link.get("parts")
                    if isinstance(parts, list):
                        for part_index, part in enumerate(parts):
                            part_where = f"{link_where}.parts[{part_index}]"
                            if not isinstance(part, dict):
                                continue
                            pdf_index = part.get("pdf_index")
                            pdf_page = part.get("pdf_page")
                            if isinstance(pdf_index, int) and isinstance(pdf_page, int) and pdf_page != pdf_index + 1:
                                self.error(part_where, "pdf_page는 pdf_index + 1이어야 함")
                            if isinstance(page_count, int):
                                if isinstance(pdf_index, int) and pdf_index >= page_count:
                                    self.error(f"{part_where}.pdf_index", "문서 페이지 수를 넘음")
                                if isinstance(pdf_page, int) and pdf_page > page_count:
                                    self.error(f"{part_where}.pdf_page", "문서 페이지 수를 넘음")
                            self.check_bbox(part.get("bbox"), f"{part_where}.bbox")
            else:
                self.error("provenance.solution_links", "배열이 필요함")

            attempts = provenance.get("transcriptions")
            if isinstance(attempts, list):
                seen_attempts: set[tuple[str, int]] = set()
                for index, attempt in enumerate(attempts):
                    attempt_where = f"provenance.transcriptions[{index}]"
                    if not isinstance(attempt, dict):
                        continue
                    key = (attempt.get("section"), attempt.get("attempt"))
                    if key in seen_attempts:
                        self.error(attempt_where, "section/attempt 조합 중복")
                    seen_attempts.add(key)
                    for asset_index, asset_id in enumerate(attempt.get("input_asset_ids", [])):
                        self.add_reference(asset_id, f"{attempt_where}.input_asset_ids[{asset_index}]")
        else:
            self.error("provenance", "객체가 필요함")

        content = data.get("content")
        if isinstance(content, dict):
            question = content.get("question")
            if isinstance(question, dict):
                self.walk_blocks(question.get("blocks"), "content.question.blocks", "question")
            elif question is not None:
                self.error("content.question", "객체 또는 null이 필요함")
            explanation = content.get("explanation")
            if explanation is not None:
                if isinstance(explanation, dict):
                    self.walk_blocks(
                        explanation.get("blocks"),
                        "content.explanation.blocks",
                        "explanation",
                    )
                else:
                    self.error("content.explanation", "객체 또는 null이 필요함")
            self.walk_answer(content.get("answer"))
        else:
            self.error("content", "객체가 필요함")
            content = {}

        for asset_id, where, roles, sections in self.references:
            asset = self.asset_by_id.get(asset_id)
            if asset is None:
                self.error(where, f"존재하지 않는 asset_id={asset_id}")
                continue
            asset_role = asset.get("role")
            asset_section = asset.get("section")
            if roles is not None and (
                not isinstance(asset_role, str) or asset_role not in roles
            ):
                self.error(where, f"asset role={asset.get('role')!r}; 허용={sorted(roles)}")
            if sections is not None and (
                not isinstance(asset_section, str) or asset_section not in sections
            ):
                self.error(where, f"asset section={asset.get('section')!r}; 허용={sorted(sections)}")

        for choice_id, where in self.answer_choice_refs:
            if choice_id not in self.choice_ids["question"]:
                self.error(where, "문제 section의 선택지를 가리키지 않음")
        for statement_id, where in self.answer_statement_refs:
            if statement_id not in self.bogi_item_ids["question"]:
                self.error(where, "문제 section의 보기 항목을 가리키지 않음")

        self.check_source_region_bounds()
        self.check_asset_graph()
        self.check_figure_lineage()

        workflow = data.get("workflow")
        status = workflow.get("status") if isinstance(workflow, dict) else None
        revision = workflow.get("revision") if isinstance(workflow, dict) else None
        referenced_ids = {asset_id for asset_id, _, _, _ in self.references}
        question_content = content.get("question") if isinstance(content, dict) else None
        if status == "source_only":
            if (
                question_content is not None
                or content.get("answer") is not None
                or content.get("explanation") is not None
            ):
                self.error(
                    "content",
                    "source_only 상태에서는 question/answer/explanation이 모두 null이어야 함",
                )
        elif status in {"transcribed", "needs_review", "content_ready"}:
            if not isinstance(question_content, dict):
                self.error(
                    "content.question",
                    f"{status} 상태에는 question section이 필요함",
                )

        if status == "content_ready":
            review = data.get("review")
            if not isinstance(review, dict):
                self.error("review", "content_ready 상태에는 review 객체가 필요함")
            else:
                for key in ("question", "assets"):
                    if review.get(key) != "approved":
                        self.error(f"review.{key}", "content_ready 상태에서는 approved여야 함")
                explanation = content.get("explanation")
                expected_explanation = "approved" if explanation is not None else "not_applicable"
                if review.get("explanation") != expected_explanation:
                    self.error("review.explanation", f"현재 content에는 {expected_explanation}가 필요함")
                answer = content.get("answer")
                expected_answer = "approved" if answer is not None else "not_applicable"
                if review.get("answer") != expected_answer:
                    self.error("review.answer", f"현재 content에는 {expected_answer}가 필요함")
                if review.get("issues"):
                    self.error("review.issues", "content_ready 상태에서는 미해결 issue가 없어야 함")
                coverage = review.get("coverage")
                if not isinstance(coverage, dict) or coverage.get("question") != "complete":
                    self.error(
                        "review.coverage.question",
                        "content_ready에는 전체 문제 crop 대조 완료가 필요함",
                    )
                expected_coverage = (
                    "complete" if explanation is not None else "not_applicable"
                )
                if (
                    not isinstance(coverage, dict)
                    or coverage.get("explanation") != expected_coverage
                ):
                    self.error(
                        "review.coverage.explanation",
                        f"현재 content에는 {expected_coverage}가 필요함",
                    )
                if review.get("approved_revision") != revision:
                    self.error(
                        "review.approved_revision",
                        "현재 workflow.revision과 일치해야 함",
                    )
                try:
                    digest = approval_digest(data)
                except (TypeError, ValueError):
                    digest = None
                if review.get("approved_sha256") != digest:
                    self.error(
                        "review.approved_sha256",
                        "현재 콘텐츠·자산 digest와 일치해야 함",
                    )
                if not review.get("reviewer") or not review.get("reviewed_at"):
                    self.error("review", "reviewer와 reviewed_at이 필요함")

            for block_id, block_status in self.block_statuses:
                if block_status != "approved":
                    self.error(
                        f"block[{block_id}].review_status",
                        "content_ready 상태에서는 approved여야 함",
                    )
            for where, equation in self.equations:
                script = equation.get("hwp_script")
                if not isinstance(script, str) or not script.strip():
                    self.error(
                        where,
                        "content_ready 상태의 수식에는 비어 있지 않은 hwp_script가 필요함",
                    )

            blocks = (
                question_content.get("blocks")
                if isinstance(question_content, dict)
                else []
            )
            unit_kind = (
                data.get("identity", {}).get("unit_kind")
                if isinstance(data.get("identity"), dict)
                else None
            )
            if unit_kind not in {"group", "shared_group_bundle"}:
                has_prompt = any(
                    isinstance(block, dict)
                    and block.get("type") == "paragraph"
                    and block.get("role") in {"stem", "instruction"}
                    for block in blocks
                )
                if not has_prompt:
                    self.error(
                        "content.question.blocks",
                        "문항에는 stem 또는 instruction paragraph가 필요함",
                    )

            answer = content.get("answer")
            answer_kind = answer.get("kind") if isinstance(answer, dict) else None
            answer_type = data.get("identity", {}).get("answer_type") if isinstance(data.get("identity"), dict) else None
            expected_kinds = {
                "multiple_choice": {"choice"},
                "short_answer": {"content"},
                "descriptive": {"content"},
                "true_false": {"true_false"},
                "composite": {"composite"},
            }.get(answer_type) if isinstance(answer_type, str) else None
            if answer is not None and expected_kinds is not None and answer_kind not in expected_kinds:
                self.error("content.answer.kind", f"answer_type={answer_type}와 맞지 않음")
            if answer_kind == "unknown":
                self.error("content.answer", "content_ready 상태에서는 unknown 대신 확정 답 또는 null을 사용해야 함")
            if answer_type == "multiple_choice" and not self.choice_ids["question"]:
                self.error(
                    "content.question",
                    "multiple_choice 문항에는 choices block이 필요함",
                )

            problem = provenance.get("problem") if isinstance(provenance, dict) else None
            if isinstance(problem, dict):
                for index, page in enumerate(problem.get("source_pages", [])):
                    if isinstance(page, dict) and (
                        page.get("raw_page_base") == "unknown" or page.get("pdf_page_index") is None
                    ):
                        self.error(f"provenance.problem.source_pages[{index}]", "content_ready 전에 page base를 확정해야 함")
                for index, part in enumerate(problem.get("source_parts", [])):
                    if isinstance(part, dict) and (
                        part.get("raw_page_base") == "unknown" or part.get("pdf_page_index") is None
                    ):
                        self.error(
                            f"provenance.problem.source_parts[{index}]",
                            "content_ready 전에 part page base를 확정해야 함",
                        )

            if isinstance(provenance, dict):
                for index, link in enumerate(provenance.get("solution_links", [])):
                    if isinstance(link, dict) and link.get("status") != "verified":
                        self.error(
                            f"provenance.solution_links[{index}].status",
                            "content_ready 상태에서는 verified link만 허용",
                        )

            for asset_id in sorted(self.content_asset_ids):
                asset = self.asset_by_id.get(asset_id)
                if not asset:
                    continue
                if asset.get("state") != "ready":
                    self.error(f"asset[{asset_id}].state", "content_ready 패키지가 참조하는 asset은 ready여야 함")
                asset_review = asset.get("review")
                if not isinstance(asset_review, dict) or asset_review.get("status") != "approved":
                    self.error(f"asset[{asset_id}].review.status", "참조 asset은 approved여야 함")
                if isinstance(asset_review, dict) and asset_review.get("boundary_uncertain"):
                    self.error(f"asset[{asset_id}].review.boundary_uncertain", "경계 불확실성을 해소해야 함")
                if asset.get("role") == "figure_regenerated":
                    generation = asset.get("generation")
                    if not isinstance(generation, dict) or generation.get("status") != "succeeded":
                        self.error(f"asset[{asset_id}].generation.status", "선택된 재생성 그림은 succeeded여야 함")
                    elif generation.get("mode") == "crop":
                        if any(
                            generation.get(key) is not None
                            for key in ("provider", "model", "prompt_text", "prompt_sha256")
                        ) or not generation.get("completed_at"):
                            self.error(
                                f"asset[{asset_id}].generation",
                                "local crop은 provider/model/prompt가 null이고 completed_at이 필요함",
                            )
                    elif not generation.get("provider") or not generation.get("prompt_sha256") or not generation.get("completed_at"):
                        self.error(f"asset[{asset_id}].generation", "AI 생성은 provider/prompt_sha256/completed_at이 필요함")
                self.check_file_for_asset(asset_id, asset)

            for asset_id in sorted(referenced_ids - self.content_asset_ids):
                asset = self.asset_by_id.get(asset_id)
                if not asset:
                    continue
                if asset.get("state") != "ready":
                    self.error(f"asset[{asset_id}].state", "원본 audit asset은 ready여야 함")
                self.check_file_for_asset(asset_id, asset)

        if self.check_files:
            for asset_id, asset in self.asset_by_id.items():
                if asset.get("state") == "ready" and asset_id not in referenced_ids:
                    self.check_file_for_asset(asset_id, asset)
        return self.errors


def json_schema_errors(data: Any, schema_path: Path) -> list[str]:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "jsonschema가 필요합니다. `python -m pip install -r requirements.txt`를 실행하세요."
        ) from exc

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = []
    for error in sorted(validator.iter_errors(data), key=lambda item: list(item.absolute_path)):
        path = ".".join(str(part) for part in error.absolute_path) or "root"
        errors.append(f"schema.{path}: {error.message}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="검사할 question.json")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument(
        "--check-files",
        action="store_true",
        help="draft 패키지에서도 state=ready인 모든 asset 파일을 확인",
    )
    parser.add_argument(
        "--semantic-only",
        action="store_true",
        help="개발용: JSON Schema 검사를 생략",
    )
    args = parser.parse_args()

    try:
        data = json.loads(args.package.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    errors: list[str] = []
    if not args.semantic_only:
        try:
            errors.extend(json_schema_errors(data, args.schema))
        except (OSError, json.JSONDecodeError, RuntimeError) as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 2
        if errors:
            print(f"FAIL: {len(errors)}개 오류", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1
    errors.extend(Validator(args.package, args.check_files).validate(data))
    if errors:
        print(f"FAIL: {len(errors)}개 오류", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"OK: {args.package.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
