"""Atomic solution-only shadow projects built from trusted canonical projects."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .model import atomic_write_json, load_json_object, sha256_file, utc_now, validate_id
from .project_archive import (
    ProjectArchiveError,
    materialize_project_snapshot,
    project_input_fingerprint,
)


class SolutionShadowError(ValueError):
    pass


@dataclass(frozen=True)
class ShadowOcrTarget:
    item_id: str
    asset_id: str
    section: str
    unit_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "item_id": self.item_id,
            "asset_id": self.asset_id,
            "section": self.section,
            "unit_id": self.unit_id,
        }


@dataclass(frozen=True)
class SolutionShadowResult:
    project_id: str
    project_dir: Path
    receipt_path: Path
    receipt_sha256: str
    missing_explanation_targets: tuple[ShadowOcrTarget, ...]
    cached: bool


RECEIPT_NAME = "solution-shadow-receipt.json"
RECOVERY_STAGING_DIR = ".solution-recovery-staging"


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _plain_workspace_dir(path: Path | str, *, label: str) -> Path:
    raw = Path(path).expanduser().absolute()
    if _is_link(raw) or not raw.is_dir():
        raise SolutionShadowError(f"{label} must be a plain directory")
    try:
        return raw.resolve(strict=True)
    except OSError as exc:
        raise SolutionShadowError(f"{label} cannot be resolved") from exc


def _solution_output_root(
    workspace_raw: Path, workspace: Path, output_root: Path | str | None
) -> Path:
    if output_root is None:
        return workspace
    candidate = Path(output_root).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_raw / candidate
    candidate = candidate.absolute()
    try:
        relative = candidate.relative_to(workspace_raw)
    except ValueError as exc:
        raise SolutionShadowError("solution shadow output root escapes workspace") from exc
    current = workspace_raw
    for index, part in enumerate(relative.parts):
        current /= part
        if _is_link(current):
            raise SolutionShadowError("solution shadow output root cannot use links")
        if current.exists():
            if not current.is_dir():
                raise SolutionShadowError("solution shadow output root is not a directory")
            continue
        if index != len(relative.parts) - 1 or not current.parent.is_dir():
            raise SolutionShadowError("solution shadow output root parent is missing")
        current.mkdir()
    resolved = _plain_workspace_dir(candidate, label="solution shadow output root")
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise SolutionShadowError("solution shadow output root escapes workspace") from exc
    return resolved


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in value
    )


def _question_digest(document: Mapping[str, Any]) -> str:
    content = document.get("content")
    provenance = document.get("provenance")
    return _digest(
        {
            "identity": document.get("identity"),
            "question": content.get("question") if isinstance(content, Mapping) else None,
            "problem": provenance.get("problem") if isinstance(provenance, Mapping) else None,
            "tags": document.get("tags"),
        }
    )


def _question_figure_digest(document: Mapping[str, Any], item_dir: Path) -> str:
    resources = document.get("resources")
    assets = resources.get("assets") if isinstance(resources, Mapping) else None
    records = []
    for asset in assets if isinstance(assets, list) else []:
        if (
            not isinstance(asset, Mapping)
            or asset.get("section") != "question"
            or asset.get("role") not in {"figure_original", "figure_regenerated"}
        ):
            continue
        path = _safe_asset_file(item_dir, asset)
        records.append(
            {
                "asset": copy.deepcopy(dict(asset)),
                "actual_sha256": sha256_file(path),
            }
        )
    records.sort(key=lambda value: str(value["asset"].get("asset_id")))
    return _digest(records)


def _sealed_invariants(
    question_digest: str,
    question_figure_digest: str,
    item_order_digest: str,
) -> dict[str, Any]:
    invariants: dict[str, Any] = {
        "question_digests_sha256": question_digest,
        "question_figure_digests_sha256": question_figure_digest,
        "item_order_sha256": item_order_digest,
        "non_null_content_preserved": True,
    }
    invariants["seal_sha256"] = _digest(invariants)
    return invariants


def _valid_invariant_seal(invariants: Mapping[str, Any]) -> bool:
    payload = {
        key: invariants.get(key)
        for key in (
            "question_digests_sha256",
            "question_figure_digests_sha256",
            "item_order_sha256",
            "non_null_content_preserved",
        )
    }
    return (
        all(
            _is_sha256(payload[key])
            for key in (
                "question_digests_sha256",
                "question_figure_digests_sha256",
                "item_order_sha256",
            )
        )
        and payload["non_null_content_preserved"] is True
        and _is_sha256(invariants.get("seal_sha256"))
        and invariants["seal_sha256"] == _digest(payload)
    )


def _safe_asset_file(item_dir: Path, asset: Mapping[str, Any]) -> Path:
    relative = asset.get("path")
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise SolutionShadowError("solution asset path is invalid")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or ":" in relative:
        raise SolutionShadowError("solution asset path is unsafe")
    current = item_dir
    for part in pure.parts:
        current /= part
        if current.is_symlink() or bool(
            getattr(current, "is_junction", lambda: False)()
        ):
            raise SolutionShadowError("solution asset path cannot use links")
    path = item_dir.joinpath(*pure.parts).resolve()
    try:
        path.relative_to(item_dir.resolve())
    except ValueError as exc:
        raise SolutionShadowError("solution asset path escapes its item") from exc
    expected = asset.get("sha256")
    if (
        not path.is_file()
        or not isinstance(expected, str)
        or len(expected) != 64
        or sha256_file(path) != expected
    ):
        raise SolutionShadowError("solution asset integrity does not match")
    return path


def _is_protected_link(link: Mapping[str, Any]) -> bool:
    return link.get("status") == "verified" or "manual_user_confirmation" in (
        link.get("evidence") or []
    )


def _has_protected_detailed_link(document: Mapping[str, Any]) -> bool:
    provenance = document.get("provenance")
    links = provenance.get("solution_links") if isinstance(provenance, Mapping) else None
    return any(
        isinstance(link, Mapping)
        and link.get("kind") in {"detailed_solution", "mixed"}
        and _is_protected_link(link)
        for link in (links if isinstance(links, list) else [])
    )


def _link_identity(
    link: Mapping[str, Any], assets_by_id: Mapping[object, Mapping[str, Any]]
) -> tuple[object, object, object]:
    asset = assets_by_id.get(link.get("source_asset_id"))
    return (
        link.get("kind"),
        link.get("external_asset_id"),
        asset.get("sha256") if isinstance(asset, Mapping) else None,
    )


def _detailed_explanation_assets(document: Mapping[str, Any]) -> tuple[str, ...]:
    resources = document.get("resources")
    assets = resources.get("assets") if isinstance(resources, Mapping) else None
    ready = {
        asset.get("asset_id")
        for asset in (assets if isinstance(assets, list) else [])
        if isinstance(asset, Mapping)
        and asset.get("role") == "source_crop"
        and asset.get("section") in {"answer", "explanation"}
        and asset.get("state") == "ready"
    }
    provenance = document.get("provenance")
    links = provenance.get("solution_links") if isinstance(provenance, Mapping) else None
    eligible = [
        link
        for link in (links if isinstance(links, list) else [])
        if isinstance(link, Mapping)
        and link.get("status") in {"verified", "review"}
        and link.get("source_asset_id") in ready
    ]
    detailed = [
        link
        for link in eligible
        if link.get("kind") in {"detailed_solution", "mixed"}
    ]
    detailed.sort(
        key=lambda link: (
            0 if link.get("status") == "verified" else 1,
            -float(link.get("confidence") or 0),
            str(link.get("source_asset_id")),
        )
    )
    return tuple(
        dict.fromkeys(str(link["source_asset_id"]) for link in detailed)
    )


def _copy_donor_asset(
    donor_item_dir: Path,
    staged_item_dir: Path,
    donor_asset: Mapping[str, Any],
    staged_assets: list[dict[str, Any]],
) -> str:
    if (
        donor_asset.get("role") != "source_crop"
        or donor_asset.get("section") not in {"answer", "explanation"}
        or donor_asset.get("state") != "ready"
    ):
        raise SolutionShadowError("donor solution asset is not a ready source crop")
    source = _safe_asset_file(donor_item_dir, donor_asset)
    digest = str(donor_asset["sha256"])
    for asset in staged_assets:
        if (
            asset.get("role") == "source_crop"
            and asset.get("section") in {"answer", "explanation"}
            and asset.get("sha256") == digest
        ):
            return str(asset["asset_id"])
    asset_id = validate_id(f"solution-shadow-{digest[:24]}", field="asset_id")
    suffix = source.suffix.casefold() or ".png"
    relative = f"source/{asset_id}{suffix}"
    destination = staged_item_dir.joinpath(*PurePosixPath(relative).parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and sha256_file(destination) != digest:
        raise SolutionShadowError("solution shadow asset destination collides")
    if not destination.exists():
        shutil.copyfile(source, destination)
    if sha256_file(destination) != digest:
        raise SolutionShadowError("solution shadow asset copy failed integrity check")
    copied = copy.deepcopy(dict(donor_asset))
    copied.update(asset_id=asset_id, path=relative)
    staged_assets.append(copied)
    return asset_id


def _cached_result(target: Path, operation_sha256: str) -> SolutionShadowResult | None:
    receipt_path = target / RECEIPT_NAME
    if not target.is_dir() or not receipt_path.is_file():
        return None
    receipt = load_json_object(receipt_path)
    if receipt.get("operation_sha256") != operation_sha256:
        return None
    expected_fingerprint = receipt.get("shadow_project_fingerprint_sha256")
    if not isinstance(expected_fingerprint, str):
        return None
    try:
        if project_input_fingerprint(target) != expected_fingerprint:
            return None
    except ProjectArchiveError:
        return None
    targets = tuple(
        ShadowOcrTarget(
            str(value["item_id"]),
            str(value["asset_id"]),
            str(value["section"]),
            str(value["unit_id"]),
        )
        for value in receipt.get("missing_explanation_targets", [])
        if isinstance(value, Mapping)
    )
    return SolutionShadowResult(
        project_id=target.name,
        project_dir=target,
        receipt_path=receipt_path,
        receipt_sha256=sha256_file(receipt_path),
        missing_explanation_targets=targets,
        cached=True,
    )


def create_solution_shadow(
    source_project_dir: Path | str,
    donor_project_dir: Path | str,
    workspace_root: Path | str,
    *,
    output_root: Path | str | None = None,
) -> SolutionShadowResult:
    """Create a solution shadow in the workspace or an internal hidden root."""

    source_root = Path(source_project_dir).expanduser().resolve(strict=True)
    donor_root = Path(donor_project_dir).expanduser().resolve(strict=True)
    workspace_raw = Path(workspace_root).expanduser().absolute()
    workspace = _plain_workspace_dir(workspace_raw, label="workspace root")
    output = _solution_output_root(workspace_raw, workspace, output_root)
    if source_root == donor_root or not source_root.is_relative_to(workspace):
        raise SolutionShadowError("solution shadow project roots are invalid")
    source_project = load_json_object(source_root / "project.json")
    donor_project = load_json_object(donor_root / "project.json")
    source_id = validate_id(source_project.get("project_id"), field="source_project_id")
    donor_import = donor_project.get("source_import")
    donor_fingerprint = (
        donor_import.get("fingerprint_sha256")
        if isinstance(donor_import, Mapping)
        else None
    )
    if not isinstance(donor_fingerprint, str) or len(donor_fingerprint) != 64:
        raise SolutionShadowError("donor source fingerprint is invalid")
    if source_project.get("item_order") != donor_project.get("item_order"):
        raise SolutionShadowError("source and donor item order do not match")

    try:
        source_fingerprint = project_input_fingerprint(source_root)
        donor_project_fingerprint = project_input_fingerprint(donor_root)
        donor_solution_digest = _digest(
            [
                load_json_object(
                    donor_root / "items" / item_id / "question.json"
                ).get("provenance", {}).get("solution_links", [])
                for item_id in donor_project.get("item_order", [])
            ]
        )
    except (OSError, ProjectArchiveError, ValueError) as exc:
        raise SolutionShadowError("solution shadow inputs are invalid") from exc
    operation_sha256 = _digest(
        [
            source_id,
            source_fingerprint,
            donor_project_fingerprint,
            donor_fingerprint,
            donor_solution_digest,
        ]
    )
    shadow_id = validate_id(
        "project-"
        + str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "question-json-studio:solution-shadow:" + operation_sha256,
            )
        ),
        field="shadow_project_id",
    )
    target = output / shadow_id
    if _is_link(target):
        raise SolutionShadowError("solution shadow target cannot use links")
    cached = _cached_result(target, operation_sha256)
    if cached is not None:
        return cached
    if target.exists():
        raise SolutionShadowError("solution shadow target already exists with other content")

    staging_root = output / f".shadow-{uuid.uuid4().hex[:12]}"
    staging = staging_root / shadow_id
    try:
        snapshot = materialize_project_snapshot(source_root, staging)
        staged_project_path = staging / "project.json"
        staged_project = load_json_object(staged_project_path)
        staged_project["project_id"] = shadow_id
        staged_project["source_import"] = copy.deepcopy(donor_import)
        workflow = staged_project.get("workflow")
        if not isinstance(workflow, dict):
            raise SolutionShadowError("staged project workflow is invalid")
        workflow["revision"] = int(workflow.get("revision") or 0) + 1
        workflow["status"] = "content_working"
        workflow["updated_at"] = utc_now()
        export = staged_project.get("export")
        if isinstance(export, dict):
            export["engine_id"] = None
            export["engine_fingerprint_sha256"] = None
        atomic_write_json(staged_project_path, staged_project)

        overlaid = 0
        protected = 0
        assets_added = 0
        reused_solution_content = 0
        targets: list[ShadowOcrTarget] = []
        question_digests: dict[str, str] = {}
        question_figure_digests: dict[str, str] = {}
        for item_id in staged_project.get("item_order", []):
            item_id = validate_id(item_id, field="item_id")
            source_item_path = source_root / "items" / item_id / "question.json"
            staged_item_path = staging / "items" / item_id / "question.json"
            donor_item_path = donor_root / "items" / item_id / "question.json"
            source_item = load_json_object(source_item_path)
            staged_item = load_json_object(staged_item_path)
            donor_item = load_json_object(donor_item_path)
            question_digests[item_id] = _question_digest(source_item)
            question_figure_digests[item_id] = _question_figure_digest(
                source_item, source_item_path.parent
            )
            non_null_content = {
                key: copy.deepcopy(value)
                for key, value in (source_item.get("content") or {}).items()
                if value is not None
            }
            reused_solution_content += sum(
                key in non_null_content for key in ("answer", "explanation")
            )
            if _has_protected_detailed_link(source_item):
                protected += 1
            else:
                staged_provenance = staged_item.get("provenance")
                donor_provenance = donor_item.get("provenance")
                staged_resources = staged_item.get("resources")
                donor_resources = donor_item.get("resources")
                staged_assets = (
                    staged_resources.get("assets")
                    if isinstance(staged_resources, dict)
                    else None
                )
                donor_assets = (
                    donor_resources.get("assets")
                    if isinstance(donor_resources, Mapping)
                    else None
                )
                donor_links = (
                    donor_provenance.get("solution_links")
                    if isinstance(donor_provenance, Mapping)
                    else None
                )
                if (
                    not isinstance(staged_provenance, dict)
                    or not isinstance(staged_assets, list)
                    or not isinstance(donor_assets, list)
                    or not isinstance(donor_links, list)
                ):
                    raise SolutionShadowError("solution overlay structure is invalid")
                donor_by_id = {
                    asset.get("asset_id"): asset
                    for asset in donor_assets
                    if isinstance(asset, Mapping)
                }
                existing_links = staged_provenance.get("solution_links")
                if not isinstance(existing_links, list):
                    raise SolutionShadowError("existing solution links are invalid")
                existing_by_id = {
                    asset.get("asset_id"): asset
                    for asset in staged_assets
                    if isinstance(asset, Mapping)
                }
                existing_identities = {
                    _link_identity(link, existing_by_id)
                    for link in existing_links
                    if isinstance(link, Mapping)
                }
                rewritten_links = copy.deepcopy(existing_links)
                links_added = 0
                for donor_link in donor_links:
                    if not isinstance(donor_link, Mapping):
                        raise SolutionShadowError("donor solution link is invalid")
                    donor_asset = donor_by_id.get(donor_link.get("source_asset_id"))
                    if not isinstance(donor_asset, Mapping):
                        raise SolutionShadowError("donor solution link asset is missing")
                    before = len(staged_assets)
                    local_id = _copy_donor_asset(
                        donor_item_path.parent,
                        staged_item_path.parent,
                        donor_asset,
                        staged_assets,
                    )
                    assets_added += len(staged_assets) - before
                    rewritten = copy.deepcopy(dict(donor_link))
                    rewritten["source_asset_id"] = local_id
                    staged_by_id = {
                        asset.get("asset_id"): asset
                        for asset in staged_assets
                        if isinstance(asset, Mapping)
                    }
                    identity = _link_identity(rewritten, staged_by_id)
                    if identity not in existing_identities:
                        rewritten_links.append(rewritten)
                        existing_identities.add(identity)
                        links_added += 1
                staged_provenance["solution_links"] = rewritten_links
                staged_snapshot = staged_provenance.get("raw_snapshot")
                donor_snapshot = (
                    donor_provenance.get("raw_snapshot")
                    if isinstance(donor_provenance, Mapping)
                    else None
                )
                if isinstance(staged_snapshot, dict) and isinstance(
                    donor_snapshot, Mapping
                ):
                    for key in ("source_files", "link_item", "link_assets"):
                        staged_snapshot[key] = copy.deepcopy(donor_snapshot.get(key))
                if links_added:
                    review = staged_item.get("review")
                    if isinstance(review, dict):
                        for key in (
                            "approved_revision",
                            "approved_sha256",
                            "reviewer",
                            "reviewed_at",
                        ):
                            review[key] = None
                        review["assets"] = "unreviewed"
                    item_workflow = staged_item.get("workflow")
                    if isinstance(item_workflow, dict):
                        item_workflow["status"] = "needs_review"
                        item_workflow["updated_at"] = utc_now()
                    overlaid += 1

            for key, value in non_null_content.items():
                if staged_item.get("content", {}).get(key) != value:
                    raise SolutionShadowError("non-null canonical content changed")
            if _question_digest(staged_item) != question_digests[item_id]:
                raise SolutionShadowError("question digest changed during solution overlay")
            if (
                _question_figure_digest(staged_item, staged_item_path.parent)
                != question_figure_digests[item_id]
            ):
                raise SolutionShadowError("figure digest changed during solution overlay")
            atomic_write_json(staged_item_path, staged_item)

            content = staged_item.get("content")
            explanation = (
                content.get("explanation") if isinstance(content, Mapping) else None
            )
            explanation_blocks = (
                explanation.get("blocks")
                if isinstance(explanation, Mapping)
                else None
            )
            if not (isinstance(explanation_blocks, list) and explanation_blocks):
                problem = staged_item.get("provenance", {}).get("problem", {})
                unit_id = problem.get("unit_id") if isinstance(problem, Mapping) else None
                asset_ids = _detailed_explanation_assets(staged_item)
                if isinstance(unit_id, str) and asset_ids:
                    targets.append(
                        ShadowOcrTarget(
                            item_id, asset_ids[0], "explanation", unit_id
                        )
                    )

        if project_input_fingerprint(source_root) != source_fingerprint:
            raise SolutionShadowError("source project changed during shadow creation")
        shadow_project_fingerprint = project_input_fingerprint(staging)
        receipt = {
            "schema_version": "0.1.0",
            "operation_sha256": operation_sha256,
            "source_project_id": source_id,
            "source_project_fingerprint_sha256": source_fingerprint,
            "source_snapshot_file_count": snapshot.file_count,
            "donor_project_id": donor_project.get("project_id"),
            "donor_project_fingerprint_sha256": donor_project_fingerprint,
            "donor_import_fingerprint_sha256": donor_fingerprint,
            "shadow_project_id": shadow_id,
            "shadow_project_fingerprint_sha256": shadow_project_fingerprint,
            "created_at": utc_now(),
            "counts": {
                "items": len(staged_project.get("item_order", [])),
                "solution_overlaid": overlaid,
                "protected_verified_or_manual": protected,
                "solution_assets_added": assets_added,
                "reused_answer_or_explanation_content": reused_solution_content,
                "missing_explanation_targets": len(targets),
            },
            "invariants": _sealed_invariants(
                _digest(question_digests),
                _digest(question_figure_digests),
                _digest(staged_project.get("item_order", [])),
            ),
            "missing_explanation_targets": [target.to_dict() for target in targets],
        }
        receipt_path = staging / RECEIPT_NAME
        atomic_write_json(receipt_path, receipt)
        os.replace(staging, target)
        staging_root.rmdir()
        published_receipt = target / RECEIPT_NAME
        return SolutionShadowResult(
            project_id=shadow_id,
            project_dir=target,
            receipt_path=published_receipt,
            receipt_sha256=sha256_file(published_receipt),
            missing_explanation_targets=tuple(targets),
            cached=False,
        )
    except (OSError, ProjectArchiveError, ValueError) as exc:
        shutil.rmtree(staging_root, ignore_errors=True)
        if isinstance(exc, SolutionShadowError):
            raise
        raise SolutionShadowError("solution shadow could not be created") from exc


def _hidden_shadow_context(
    staged_project_dir: Path | str,
    workspace_root: Path | str,
) -> tuple[Path, Path, dict[str, Any], str, Path, dict[str, Any]]:
    workspace_raw = Path(workspace_root).expanduser().absolute()
    workspace = _plain_workspace_dir(workspace_raw, label="workspace root")
    hidden = workspace / RECOVERY_STAGING_DIR
    if _is_link(hidden) or not hidden.is_dir():
        raise SolutionShadowError("solution recovery staging root is invalid")
    hidden = _plain_workspace_dir(hidden, label="solution recovery staging root")
    staged_raw = Path(staged_project_dir).expanduser().absolute()
    if _is_link(staged_raw):
        raise SolutionShadowError("staged solution shadow cannot use links")
    try:
        staged_relative = staged_raw.relative_to(workspace_raw)
    except ValueError as exc:
        raise SolutionShadowError("staged solution shadow escapes workspace") from exc
    current = workspace_raw
    for part in staged_relative.parts:
        current /= part
        if _is_link(current):
            raise SolutionShadowError("staged solution shadow cannot use links")
    try:
        staged = staged_raw.resolve(strict=True)
    except OSError as exc:
        raise SolutionShadowError("staged solution shadow is missing") from exc
    if not staged.is_dir() or staged.parent != hidden:
        raise SolutionShadowError(
            "staged solution shadow must be under the recovery staging root"
        )
    project = load_json_object(staged / "project.json")
    project_id = validate_id(project.get("project_id"), field="shadow_project_id")
    if staged.name != project_id:
        raise SolutionShadowError("staged folder and project_id do not match")
    receipt_path = staged / RECEIPT_NAME
    if _is_link(receipt_path) or not receipt_path.is_file():
        raise SolutionShadowError("solution shadow receipt is missing")
    receipt = load_json_object(receipt_path)
    if (
        receipt.get("schema_version") != "0.1.0"
        or receipt.get("shadow_project_id") != project_id
        or not _is_sha256(receipt.get("operation_sha256"))
        or not _is_sha256(receipt.get("shadow_project_fingerprint_sha256"))
    ):
        raise SolutionShadowError("solution shadow receipt is invalid")
    return workspace, staged, project, project_id, receipt_path, receipt


def _shadow_invariant_digests(
    staged: Path, project: Mapping[str, Any]
) -> tuple[str, str, str]:
    item_order = project.get("item_order")
    if not isinstance(item_order, list):
        raise SolutionShadowError("staged project item_order is invalid")
    question_digests: dict[str, str] = {}
    question_figure_digests: dict[str, str] = {}
    for raw_item_id in item_order:
        item_id = validate_id(raw_item_id, field="item_id")
        item_path = staged / "items" / item_id / "question.json"
        document = load_json_object(item_path)
        question_digests[item_id] = _question_digest(document)
        question_figure_digests[item_id] = _question_figure_digest(
            document, item_path.parent
        )
    return (
        _digest(question_digests),
        _digest(question_figure_digests),
        _digest(item_order),
    )


def _legacy_source_invariant_digests(
    workspace: Path, receipt: Mapping[str, Any]
) -> tuple[str, str, str]:
    try:
        source_id = validate_id(
            receipt.get("source_project_id"), field="source_project_id"
        )
    except ValueError as exc:
        raise SolutionShadowError("legacy solution shadow source is invalid") from exc
    source_path = workspace / source_id
    if _is_link(source_path) or not source_path.is_dir():
        raise SolutionShadowError("legacy solution shadow source is missing")
    source = _plain_workspace_dir(source_path, label="legacy solution shadow source")
    if source.parent != workspace:
        raise SolutionShadowError("legacy solution shadow source escapes workspace")
    recorded = receipt.get("source_project_fingerprint_sha256")
    if not _is_sha256(recorded):
        raise SolutionShadowError("legacy solution shadow source fingerprint is invalid")
    try:
        before = project_input_fingerprint(source)
        project = load_json_object(source / "project.json")
        digests = _shadow_invariant_digests(source, project)
        after = project_input_fingerprint(source)
    except (OSError, ProjectArchiveError, ValueError) as exc:
        if isinstance(exc, SolutionShadowError):
            raise
        raise SolutionShadowError("legacy solution shadow source is invalid") from exc
    if before != recorded or after != recorded:
        raise SolutionShadowError("legacy solution shadow source fingerprint differs")
    return digests


def finalize_solution_shadow(
    staged_project_dir: Path | str,
    workspace_root: Path | str,
) -> SolutionShadowResult:
    """Re-seal a hidden shadow after OCR while preserving question invariants."""

    workspace, staged, project, project_id, receipt_path, receipt = (
        _hidden_shadow_context(staged_project_dir, workspace_root)
    )
    invariants = receipt.get("invariants")
    if not isinstance(invariants, Mapping):
        raise SolutionShadowError("solution shadow invariants are missing")
    try:
        question_digest, question_figure_digest, item_order_digest = (
            _shadow_invariant_digests(staged, project)
        )
    except (OSError, ProjectArchiveError, ValueError) as exc:
        if isinstance(exc, SolutionShadowError):
            raise
        raise SolutionShadowError("solution shadow invariants cannot be checked") from exc
    if invariants.get("non_null_content_preserved") is not True:
        raise SolutionShadowError("solution shadow canonical invariants differ")
    if "question_figure_digests_sha256" in invariants or "seal_sha256" in invariants:
        if not _valid_invariant_seal(invariants) or (
            invariants.get("question_digests_sha256") != question_digest
            or invariants.get("question_figure_digests_sha256")
            != question_figure_digest
            or invariants.get("item_order_sha256") != item_order_digest
        ):
            raise SolutionShadowError("solution shadow canonical invariants differ")
    else:
        try:
            source_digests = _legacy_source_invariant_digests(workspace, receipt)
        except (OSError, ProjectArchiveError, ValueError) as exc:
            if isinstance(exc, SolutionShadowError):
                raise
            raise SolutionShadowError(
                "legacy solution shadow invariants cannot be checked"
            ) from exc
        if source_digests != (
            question_digest,
            question_figure_digest,
            item_order_digest,
        ):
            raise SolutionShadowError("solution shadow canonical invariants differ")

    targets: list[ShadowOcrTarget] = []
    for raw_item_id in project.get("item_order", []):
        item_id = validate_id(raw_item_id, field="item_id")
        item_path = staged / "items" / item_id / "question.json"
        document = load_json_object(item_path)
        content = document.get("content")
        explanation = (
            content.get("explanation") if isinstance(content, Mapping) else None
        )
        blocks = (
            explanation.get("blocks")
            if isinstance(explanation, Mapping)
            else None
        )
        if isinstance(blocks, list) and blocks:
            continue
        problem = document.get("provenance", {}).get("problem", {})
        unit_id = problem.get("unit_id") if isinstance(problem, Mapping) else None
        assets = _detailed_explanation_assets(document)
        if isinstance(unit_id, str) and assets:
            targets.append(
                ShadowOcrTarget(item_id, assets[0], "explanation", unit_id)
            )

    try:
        fingerprint = project_input_fingerprint(staged)
    except (OSError, ProjectArchiveError, ValueError) as exc:
        raise SolutionShadowError("finalized solution shadow is invalid") from exc
    finalized = copy.deepcopy(receipt)
    counts = finalized.get("counts")
    if not isinstance(counts, dict):
        raise SolutionShadowError("solution shadow receipt counts are invalid")
    counts["missing_explanation_targets"] = len(targets)
    finalized["missing_explanation_targets"] = [
        target.to_dict() for target in targets
    ]
    finalized["invariants"] = _sealed_invariants(
        question_digest,
        question_figure_digest,
        item_order_digest,
    )
    finalized["shadow_project_fingerprint_sha256"] = fingerprint
    finalized["finalized_at"] = utc_now()
    atomic_write_json(receipt_path, finalized)
    return SolutionShadowResult(
        project_id=project_id,
        project_dir=staged,
        receipt_path=receipt_path,
        receipt_sha256=sha256_file(receipt_path),
        missing_explanation_targets=tuple(targets),
        cached=False,
    )


def publish_solution_shadow(
    staged_project_dir: Path | str,
    workspace_root: Path | str,
) -> SolutionShadowResult:
    """Atomically publish a validated hidden shadow into the main workspace."""

    workspace, staged, _project, project_id, _receipt_path, receipt = (
        _hidden_shadow_context(staged_project_dir, workspace_root)
    )
    try:
        actual_fingerprint = project_input_fingerprint(staged)
    except (OSError, ProjectArchiveError, ValueError) as exc:
        raise SolutionShadowError("staged solution shadow is invalid") from exc
    if actual_fingerprint != receipt["shadow_project_fingerprint_sha256"]:
        raise SolutionShadowError("staged solution shadow fingerprint differs")

    target = workspace / project_id
    if _is_link(target) or target.exists():
        raise SolutionShadowError("solution shadow publish target already exists")
    if staged.stat().st_dev != workspace.stat().st_dev:
        raise SolutionShadowError("solution shadow publish must stay on one volume")

    targets = tuple(
        ShadowOcrTarget(
            str(value["item_id"]),
            str(value["asset_id"]),
            str(value["section"]),
            str(value["unit_id"]),
        )
        for value in receipt.get("missing_explanation_targets", [])
        if isinstance(value, Mapping)
    )
    try:
        os.replace(staged, target)
    except OSError as exc:
        raise SolutionShadowError("solution shadow publish failed") from exc
    published_receipt = target / RECEIPT_NAME
    return SolutionShadowResult(
        project_id=project_id,
        project_dir=target,
        receipt_path=published_receipt,
        receipt_sha256=sha256_file(published_receipt),
        missing_explanation_targets=targets,
        cached=False,
    )
