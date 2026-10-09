"""Read-only importer for completed Question_Crop leaf runs."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from .model import (
    ModelError,
    atomic_write_json,
    load_json_object,
    make_source_only_project,
    make_source_only_question,
    sha256_file,
    stable_item_id,
    stable_project_id,
    utc_now,
)
from .solution_reconciliation import SolutionIntegrityError, enrich_project_from_leaf
from astra_cli.quality_manifest import decision_body, decision_reason, bind_import_origin


class SourceIntegrityError(ModelError):
    pass


class ImportConflictError(ModelError):
    pass


@dataclass(frozen=True)
class ImportResult:
    project_id: str
    project_dir: Path
    item_count: int
    cached: bool


def find_leaf_runs(root: Path | str) -> list[Path]:
    root = Path(root).expanduser().resolve()
    if (root / "manifest.json").is_file():
        return [root]
    return sorted(
        path.parent
        for path in root.rglob("manifest.json")
        if "backups" not in {part.casefold() for part in path.parts}
    )


def _safe_source_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise SourceIntegrityError("source asset path is missing")
    normalized = relative.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or ":" in normalized:
        raise SourceIntegrityError(f"unsafe source path: {relative}")
    candidate = (root / Path(*pure.parts)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise SourceIntegrityError(f"source path escapes leaf run: {relative}") from exc
    if not candidate.is_file():
        raise SourceIntegrityError(f"source asset does not exist: {candidate}")
    return candidate


def _source_files(leaf_run: Path) -> list[dict[str, Any]]:
    candidates: list[tuple[str, Path]] = [("manifest", leaf_run / "manifest.json")]
    candidates.extend(("detection", path) for path in sorted((leaf_run / "detections").glob("page-*.json")))
    candidates.extend(
        [
            ("ensemble", leaf_run / "ensemble" / "summary.json"),
            ("link_manifest", leaf_run / "solutions" / "link_manifest.json"),
        ]
    )
    records = []
    for role, path in candidates:
        if not path.is_file():
            continue
        schema_version: str | int | None = None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                schema_version = value.get("schema_version") or value.get("pipeline_version")
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        records.append(
            {
                "role": role,
                "relative_path": path.relative_to(leaf_run).as_posix(),
                "sha256": sha256_file(path),
                "schema_version": schema_version,
            }
        )
    return records


def _fingerprint(source_files: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in sorted(source_files, key=lambda item: str(item["relative_path"])):
        digest.update(str(record["role"]).encode())
        digest.update(b"\0")
        digest.update(str(record["relative_path"]).encode())
        digest.update(b"\0")
        digest.update(str(record["sha256"]).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _mime_type(path: Path) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".bmp": "image/bmp",
    }.get(path.suffix.casefold(), "image/png")


def _png_size(path: Path) -> tuple[int, int] | None:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) == 24 and header[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", header[16:24])
    return None


def _copy_asset(
    source: Path,
    destination: Path,
    *,
    asset_id: str,
    section: str,
    role: str = "source_crop",
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    source_sha256 = sha256_file(source)
    copied_sha256 = sha256_file(destination)
    if source_sha256 != copied_sha256:
        raise SourceIntegrityError(f"copied asset hash mismatch: {source}")
    if expected_sha256 is not None and copied_sha256.casefold() != expected_sha256.casefold():
        raise SourceIntegrityError(f"declared asset hash mismatch: {source}")
    size = _png_size(destination)
    asset = {
        "asset_id": asset_id,
        "section": section,
        "role": role,
        "state": "ready" if size else "pending",
        "path": destination.relative_to(destination.parents[1]).as_posix(),
        "media_type": _mime_type(destination),
        "sha256": copied_sha256,
        "generation": None,
        "review": {
            "status": "unreviewed",
            "boundary_uncertain": False,
            "notes": [],
        },
    }
    if size:
        asset["width_px"], asset["height_px"] = size
    return asset


def _page_base(values: list[int]) -> str:
    return "zero_based" if 0 in values else "one_based"


def _page_ref(raw: int, base: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
    pdf_index = raw if base == "zero_based" else raw - 1
    rotations = manifest.get("rotations")
    rotation = rotations.get(str(raw), 0) if isinstance(rotations, Mapping) else 0
    return {
        "raw_source_page": raw,
        "raw_page_base": base,
        "pdf_page_index": max(pdf_index, 0),
        "page_asset_id": None,
        "render_dpi": None,
        "rotation_degrees": rotation if rotation in {0, 90, 180, 270} else 0,
        "flip_horizontal": False,
    }


def _solution_asset_path(leaf_run: Path, output_file: str) -> Path:
    normalized = output_file.replace("\\", "/")
    if normalized.startswith("solutions/"):
        return _safe_source_path(leaf_run, normalized)
    return _safe_source_path(leaf_run / "solutions", normalized)


def _solution_parts(asset: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = []
    for part in asset.get("parts", []) if isinstance(asset.get("parts"), list) else []:
        if not isinstance(part, Mapping):
            continue
        bbox = part.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        result.append(
            {
                "pdf_index": int(part.get("pdf_index", 0)),
                "pdf_page": int(part.get("pdf_page", int(part.get("pdf_index", 0)) + 1)),
                "printed_page": part.get("printed_page"),
                "segment_id": part.get("segment_id"),
                "render_dpi": int(asset.get("dpi") or 300),
                "bbox": {
                    "unit": "ratio",
                    "origin": "top-left",
                    "left": float(bbox[0]) / 1000,
                    "top": float(bbox[1]) / 1000,
                    "right": float(bbox[2]) / 1000,
                    "bottom": float(bbox[3]) / 1000,
                },
            }
        )
    return result


def _document_page_count(manifest: Mapping[str, Any], all_pages: list[int]) -> int:
    rotations = manifest.get("rotations")
    if isinstance(rotations, Mapping) and rotations:
        return len(rotations)
    return max(all_pages, default=1) + (1 if 0 in all_pages else 0)


def import_leaf_run(
    leaf_run: Path | str,
    workspace_root: Path | str,
    *,
    project_name: str | None = None,
    source_name: str | None = None,
    project_info: Mapping[str, Any] | None = None,
) -> ImportResult:
    leaf_run = Path(leaf_run).expanduser().resolve()
    workspace_root = Path(workspace_root).expanduser().resolve()
    manifest_path = leaf_run / "manifest.json"
    manifest = load_json_object(manifest_path)
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise SourceIntegrityError("manifest.items must be a non-empty array")

    source_files = _source_files(leaf_run)
    fingerprint = _fingerprint(source_files)
    project_id = stable_project_id(fingerprint)
    project_dir = workspace_root / project_id
    if project_dir.exists():
        existing = load_json_object(project_dir / "project.json")
        source_import = existing.get("source_import")
        if isinstance(source_import, Mapping) and source_import.get("fingerprint_sha256") == fingerprint:
            if (leaf_run / "solutions/link_manifest.json").is_file():
                enrich_project_from_leaf(project_dir, leaf_run, preserve_approved=True)
            return ImportResult(project_id, project_dir, len(existing.get("item_order", [])), True)
        raise ImportConflictError(f"project directory already exists with different source: {project_dir}")

    link_path = leaf_run / "solutions" / "link_manifest.json"
    link_manifest = load_json_object(link_path) if link_path.is_file() else None
    link_items = link_manifest.get("items", {}) if isinstance(link_manifest, Mapping) else {}
    link_assets = link_manifest.get("assets", {}) if isinstance(link_manifest, Mapping) else {}

    timestamp = utc_now()
    staging = workspace_root / f".{project_id}.staging-{uuid.uuid4().hex}"
    all_pages = [
        int(page)
        for item in items
        if isinstance(item, Mapping)
        for page in item.get("source_pages", [])
        if isinstance(page, int)
    ]
    page_count = _document_page_count(manifest, all_pages)
    source_sha = str(manifest.get("source_sha256") or "")
    if len(source_sha) != 64:
        raise SourceIntegrityError("manifest.source_sha256 is invalid")

    try:
        item_ids: list[str] = []
        for sequence, item in enumerate(items, 1):
            if not isinstance(item, Mapping):
                raise SourceIntegrityError(f"manifest item {sequence} is not an object")
            unit_id = str(item.get("unit_id") or "")
            item_id = stable_item_id(source_sha, unit_id)
            item_ids.append(item_id)
            item_root = staging / "items" / item_id
            source_dir = item_root / "source"
            problem_source = _safe_source_path(leaf_run, str(item.get("output_file") or ""))
            problem_dest = source_dir / f"question{problem_source.suffix.lower()}"
            expected_question_sha = item.get("output_sha256")
            if "output_sha256" in item and (
                not isinstance(expected_question_sha, str)
                or len(expected_question_sha) != 64
                or not all(character in "0123456789abcdefABCDEF" for character in expected_question_sha)
            ):
                raise SourceIntegrityError("manifest item output_sha256 is invalid")
            assets = [
                _copy_asset(
                    problem_source,
                    problem_dest,
                    asset_id="source-question",
                    section="question",
                    expected_sha256=expected_question_sha,
                )
            ]

            raw_link_item = link_items.get(unit_id) if isinstance(link_items, Mapping) else None
            selected_link_assets: list[dict[str, Any]] = []
            solution_links: list[dict[str, Any]] = []
            answer_source = False
            explanation_source = False
            authority_reason = decision_reason(leaf_run, link_manifest, unit_id) if link_manifest is not None else "quality_crop_decision_required"
            if isinstance(raw_link_item, Mapping):
                answer_ids = (
                    raw_link_item.get("answer_ids")
                    if isinstance(raw_link_item.get("answer_ids"), list)
                    else []
                )
                explanation_ids = (
                    raw_link_item.get("explanation_ids")
                    if isinstance(raw_link_item.get("explanation_ids"), list)
                    else []
                )
                if raw_link_item.get("status") == "verified" and any(
                    not isinstance(external_id, str)
                    or not isinstance(link_assets, Mapping)
                    or link_assets.get(external_id) is None
                    or (
                        isinstance(link_assets.get(external_id), Mapping)
                        and link_assets[external_id].get("selection_status")
                        == "review_only"
                    )
                    for external_id in (*answer_ids, *explanation_ids)
                ):
                    raise SolutionIntegrityError("verified solution asset is missing")
                selected_ids = [
                    ("quick_answer", asset_id, False)
                    for asset_id in answer_ids
                ] + [
                    ("detailed_solution", asset_id, False)
                    for asset_id in explanation_ids
                ]
                for external_id in (
                    raw_link_item.get("candidate_ids")
                    if isinstance(raw_link_item.get("candidate_ids"), list)
                    else []
                ):
                    raw_asset = (
                        link_assets.get(external_id)
                        if isinstance(external_id, str)
                        and isinstance(link_assets, Mapping)
                        else None
                    )
                    if (
                        isinstance(raw_asset, Mapping)
                        and raw_asset.get("kind")
                        in {"quick_answer", "detailed_solution"}
                        and raw_asset.get("selection_status") == "review_only"
                        and raw_asset.get("output_file")
                    ):
                        selected_ids.append(
                            (str(raw_asset["kind"]), external_id, True)
                        )
                seen_external_ids: set[str] = set()
                unique_selected_ids: list[tuple[str, str, bool]] = []
                for kind, external_id, review_only in selected_ids:
                    if (
                        isinstance(external_id, str)
                        and external_id not in seen_external_ids
                    ):
                        seen_external_ids.add(external_id)
                        unique_selected_ids.append((kind, external_id, review_only))
                selected_ids = unique_selected_ids
                for link_index, (kind, external_id, review_only) in enumerate(
                    selected_ids, 1
                ):
                    raw_asset = link_assets.get(external_id) if isinstance(link_assets, Mapping) else None
                    if not isinstance(raw_asset, Mapping) or not raw_asset.get("output_file"):
                        continue
                    source_path = _solution_asset_path(leaf_run, str(raw_asset["output_file"]))
                    if review_only:
                        expected_sha = raw_asset.get("output_sha256")
                        if (
                            not isinstance(expected_sha, str)
                            or len(expected_sha) != 64
                            or sha256_file(source_path).casefold()
                            != expected_sha.casefold()
                        ):
                            raise SourceIntegrityError(
                                "review-only solution asset hash mismatch"
                            )
                    section = "explanation"
                    local_name = (
                        f"answer-{link_index:02d}{source_path.suffix.lower()}"
                        if kind == "quick_answer"
                        else f"explanation-{link_index:02d}{source_path.suffix.lower()}"
                    )
                    local_asset_id = f"source-{kind}-{link_index:02d}"
                    assets.append(
                        _copy_asset(
                            source_path,
                            source_dir / local_name,
                            asset_id=local_asset_id,
                            section=section,
                        )
                    )
                    answer_source |= kind == "quick_answer"
                    explanation_source |= kind == "detailed_solution"
                    selected_link_assets.append({"asset_id": external_id, **copy.deepcopy(dict(raw_asset))})
                    parts = _solution_parts(raw_asset)
                    link_review_reasons = list(
                        raw_link_item.get("review_reasons") or []
                    )
                    if authority_reason is not None:
                        link_review_reasons.append(authority_reason)
                    if (
                        review_only
                        and "review_only_solution_candidate"
                        not in link_review_reasons
                    ):
                        link_review_reasons.append("review_only_solution_candidate")
                    solution_links.append(
                        {
                            "kind": kind,
                            "external_asset_id": str(external_id),
                            "source_asset_id": local_asset_id,
                            "document_sha256": str(raw_asset.get("source_sha256") or source_sha),
                            "document_page_count": max(
                                [part["pdf_page"] for part in parts], default=1
                            ),
                            "status": (
                                "review"
                                if review_only or authority_reason is not None
                                else raw_link_item.get("status")
                                if raw_link_item.get("status") in {"verified", "review", "missing"}
                                else "review"
                            ),
                            "confidence": float(raw_link_item.get("confidence") or 0),
                            "evidence": [value for value in (raw_link_item.get("evidence") or [])
                                         if value != "manual_user_confirmation"],
                            "review_reasons": link_review_reasons,
                            "parts": parts,
                        }
                    )

            raw_pages = [int(page) for page in item.get("source_pages", []) if isinstance(page, int)]
            base = _page_base(raw_pages)
            provenance = {
                "imported_at": timestamp,
                "source_system": {
                    "name": "question_crop",
                    "pipeline_version": str(manifest.get("pipeline_version") or "unknown"),
                },
                "problem": {
                    "document_sha256": source_sha,
                    "document_page_count": page_count,
                    "original_filename": Path(str(manifest.get("source_pdf") or "source.pdf")).name,
                    "unit_id": unit_id,
                    "owner_question_id": item.get("owner_question_id"),
                    "source_pages": [_page_ref(page, base, manifest) for page in raw_pages],
                    "source_parts": [
                        {
                            "role": str(part.get("role") or "unknown"),
                            "raw_source_page": int(part.get("source_page", raw_pages[0] if raw_pages else 1)),
                            "raw_page_base": base,
                            "pdf_page_index": max(
                                int(part.get("source_page", raw_pages[0] if raw_pages else 1))
                                - (1 if base == "one_based" else 0),
                                0,
                            ),
                            "region": None,
                        }
                        for part in item.get("parts", [])
                        if isinstance(part, Mapping)
                    ],
                    "crop_asset_id": "source-question",
                    "rotation_degrees": 0,
                    "flip_horizontal": False,
                },
                "solution_links": solution_links,
                "transcriptions": [],
                "raw_snapshot": {
                    "leaf_run_id": leaf_run.name,
                    "source_files": source_files,
                    "manifest_item": copy.deepcopy(dict(item)),
                    "detection_items": [],
                    "ensemble_item": None,
                    "link_item": copy.deepcopy(dict(raw_link_item)) if isinstance(raw_link_item, Mapping) else None,
                    "link_assets": selected_link_assets,
                },
            }
            identity = {
                "printed_number": str(item.get("printed_number") or sequence),
                "unit_kind": (
                    item.get("unit_kind")
                    if item.get("unit_kind") in {"question", "main_question", "subquestion", "group", "shared_group_bundle"}
                    else "question"
                ),
                "answer_type": (
                    item.get("answer_type")
                    if item.get("answer_type") in {"multiple_choice", "short_answer", "descriptive", "true_false", "composite", "mixed", "unknown"}
                    else "unknown"
                ),
                "score": None,
                "parent_item_id": None,
                "group_id": None,
            }
            package = make_source_only_question(
                item_id=item_id,
                identity=identity,
                provenance=provenance,
                assets=assets,
                issues=item.get("review_reasons") or [],
                has_answer_source=answer_source,
                has_explanation_source=explanation_source,
                timestamp=timestamp,
            )
            snapshot_link = package["provenance"]["raw_snapshot"].get("link_item")
            if isinstance(snapshot_link, dict) and isinstance(raw_link_item, Mapping):
                bind_import_origin(package, leaf_run, unit_id, dict(raw_link_item), item_root)
            atomic_write_json(item_root / "question.json", package)

        source_import = {
            "leaf_run_id": leaf_run.name,
            "fingerprint_sha256": fingerprint,
            "imported_at": timestamp,
            "source_files": source_files,
        }
        project = make_source_only_project(
            project_id=project_id,
            item_ids=item_ids,
            groups=[],
            source_import=source_import,
            timestamp=timestamp,
        )
        display_name = project_name or Path(
            str(source_name or manifest.get("source_pdf") or leaf_run.name)
        ).stem
        project["metadata"]["filename_base"] = display_name
        project["metadata"]["book_title"] = display_name
        if project_info:
            from .project_info import apply_info

            apply_info(project["metadata"], {key: value for key, value in project_info.items() if key != "title"})
        atomic_write_json(staging / "project.json", project)
        if link_manifest is not None:
            enrich_project_from_leaf(staging, leaf_run)
        if link_manifest is not None:
            for item_id in item_ids:
                item_path = staging / "items" / item_id / "question.json"
                package = load_json_object(item_path)
                unit_id = package["provenance"]["problem"]["unit_id"]
                current_link = link_items.get(unit_id)
                if isinstance(current_link, dict):
                    bind_import_origin(package, leaf_run, unit_id, current_link,
                                       project_dir / "items" / item_id)
                    atomic_write_json(item_path, package)
        workspace_root.mkdir(parents=True, exist_ok=True)
        os.replace(staging, project_dir)
        return ImportResult(project_id, project_dir, len(item_ids), False)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
