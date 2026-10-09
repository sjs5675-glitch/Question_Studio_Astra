from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed, CancelledError
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astra_runtime import IsolationError, bootstrap, inside, validate_parsed_paths
import astra_cli

ASTRA = bootstrap()
from string import Template
from typing import Any, Iterable

import pypdfium2 as pdfium
from PIL import Image, ImageDraw


VERSION = "0.1.0"
ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "detection.schema.json"
PROMPT_PATH = ROOT / "prompts" / "detect_questions.md"
TRIAGE_SCHEMA_PATH = ROOT / "triage.schema.json"
TRIAGE_PROMPT_PATH = ROOT / "prompts" / "triage_pages.md"
VERIFY_SCHEMA_PATH = ROOT / "verification.schema.json"
VERIFY_PROMPT_PATH = ROOT / "prompts" / "verify_crop.md"
_CODEX_ENV_ALLOWLIST = frozenset(
    {
        "APPDATA",
        "CODEX_HOME",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "OS",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
)
PROBLEM_PAGE_TYPES = {
    "problem",
    "mixed_problem_and_concept",
    "problem_with_inline_solution",
}
CONTEXT_PAGE_TYPES = {"shared_material_only"}
NON_PROBLEM_PAGE_TYPES = {
    "excluded_drill",
    "concept_only",
    "solution",
    "answer_key",
    "cover",
    "table_of_contents",
    "advertisement",
    "blank",
    "other",
}
PAGE_TYPES = PROBLEM_PAGE_TYPES | CONTEXT_PAGE_TYPES | NON_PROBLEM_PAGE_TYPES
LAYOUT_TYPES = {"single_column", "two_columns", "mixed", "non_problem"}
QUESTION_KINDS = {
    "standalone",
    "shared_group_member",
    "parent_with_subquestions",
    "continuation",
}
ANSWER_TYPES = {
    "multiple_choice",
    "short_answer",
    "descriptive",
    "true_false",
    "mixed",
    "unknown",
}
PART_ROLES = {
    "shared_material",
    "instruction",
    "main_number",
    "stem",
    "figure_or_table",
    "choices",
    "response_area",
    "sidecar_hint",
    "subquestion",
    "continuation",
}
VERIFICATION_BOOLEAN_FIELDS = {
    "complete_question",
    "contains_expected_identifier",
    "contains_required_context",
    "contains_answer_or_solution",
    "contains_other_question",
    "required_content_missing_or_unreadable",
    "explicit_answer_evidence",
    "target_content_clipped",
    "identifier_clipped",
    "cosmetic_or_foreign_fragment_only",
    "cut_off_content",
    "excessive_blank_space",
}
VERIFICATION_FIELDS = VERIFICATION_BOOLEAN_FIELDS | {
    "verdict",
    "reasons",
    "confidence",
}
VERIFICATION_REASON_FIELDS = (
    ("incomplete_question", "complete_question", False),
    ("expected_identifier_absent", "contains_expected_identifier", False),
    ("required_context_absent", "contains_required_context", False),
    ("contains_other_question", "contains_other_question", True),
    (
        "required_content_missing_or_unreadable",
        "required_content_missing_or_unreadable",
        True,
    ),
    ("explicit_answer_evidence", "explicit_answer_evidence", True),
    ("target_content_clipped", "target_content_clipped", True),
    ("identifier_clipped", "identifier_clipped", True),
)
VERIFICATION_REPAIR_CODES = frozenset(
    {
        "contains_other_question",
        "required_content_missing_or_unreadable",
        "explicit_answer_evidence",
        "target_content_clipped",
        "identifier_clipped",
    }
)
LOCAL_REPAIR_CODES = frozenset(
    {
        "crop_edge_contact",
        "missing_first_pass_owner",
        "owner_bbox_invalid",
        "owner_geometry_overlap",
    }
)
REPAIR_REASON_CODES = VERIFICATION_REPAIR_CODES | LOCAL_REPAIR_CODES
TILE_SPECS = [
    ("top-left", (0.00, 0.00, 0.56, 0.56), [0, 0, 560, 560]),
    ("top-right", (0.44, 0.00, 1.00, 0.56), [440, 0, 1000, 560]),
    ("bottom-left", (0.00, 0.44, 0.56, 1.00), [0, 440, 560, 1000]),
    ("bottom-right", (0.44, 0.44, 1.00, 1.00), [440, 440, 1000, 1000]),
]
DEFAULT_EXCLUDE_REGEX = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def json_bytes(data: Any) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def canonical_sha256(data: Any) -> str:
    return sha256_text(
        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def review_entry(
    page: int | None,
    item: str | None,
    reason: str,
    reason_codes: Iterable[str],
    facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "page": page,
        "item": item,
        "reason": reason,
        "reason_codes": list(reason_codes),
    }
    if facts is not None:
        result["facts"] = facts
    return result


def coverage_record(
    item: dict[str, Any],
    source_pdf_sha256: str,
    page_inventory: list[dict[str, Any]],
) -> dict[str, Any]:
    inventory = {int(page["page"]): page for page in page_inventory}
    source_pages: list[dict[str, Any]] = []
    reason_codes: list[str] = []
    for page_number in item.get("source_pages", []):
        page = inventory.get(int(page_number))
        if page is None:
            reason_codes.append("source_page_absent")
            continue
        source_image = page.get("source_page_image")
        if not isinstance(source_image, dict):
            reason_codes.append("source_page_image_absent")
        source_pages.append(
            {
                "page": int(page_number),
                "rotation": int(page.get("rotation", 0)),
                "layout": page.get("layout", "unknown"),
                "footer_regions": page.get("footer_regions", []),
                "source_page_image": source_image,
            }
        )
    ordered_target = {
        "unit_id": item.get("unit_id"),
        "owner_question_id": item.get("owner_question_id"),
        "unit_kind": item.get("unit_kind"),
        "column": item.get("column"),
        "number_bbox": item.get("number_bbox"),
        "parts": item.get("parts", []),
    }
    geometry_errors: list[str] = []
    for index, part in enumerate(item.get("parts", [])):
        if not isinstance(part, dict):
            geometry_errors.append(f"parts[{index}]")
            continue
        validate_bbox(part.get("bbox"), f"parts[{index}].bbox", geometry_errors)
        validate_bbox(
            part.get("crop_bbox"), f"parts[{index}].crop_bbox", geometry_errors
        )
    if geometry_errors:
        reason_codes.append("target_geometry_invalid")
    shared_context = any(part.get("is_context") for part in item.get("parts", []))
    shared_parent = any(part.get("is_parent_context") for part in item.get("parts", []))
    continuation = len(item.get("source_pages", [])) > 1 or any(
        part.get("role") == "continuation" for part in item.get("parts", [])
    )
    record = {
        "source_pdf_sha256": source_pdf_sha256,
        "source_pages": source_pages,
        "ordered_target": ordered_target,
        "ordered_geometry_sha256": canonical_sha256(ordered_target),
        "final_crop_sha256": item.get("output_sha256"),
        "shared_context": shared_context,
        "shared_parent": shared_parent,
        "continuation": continuation,
    }
    return {
        **record,
        "proof_sha256": canonical_sha256(record),
        "status": "unresolved" if reason_codes else "pending_verification",
        "reason_codes": sorted(set(reason_codes)),
    }


def source_pdf_identity(run_dir: Path) -> tuple[str | None, str | None]:
    """Read the current PDF once per consumer batch; originals are read-only."""
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        return None, None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_pdf = manifest.get("source_pdf")
    if not isinstance(source_pdf, str) or not source_pdf:
        return None, manifest.get("source_sha256")
    source_path = Path(source_pdf)
    source_path = source_path if source_path.is_absolute() else run_dir / source_path
    return (
        sha256_file(source_path) if source_path.is_file() else None,
        manifest.get("source_sha256"),
    )


def coverage_failures(
    run_dir: Path,
    item: dict[str, Any],
    pdf_identity: tuple[str | None, str | None] | None = None,
) -> list[str]:
    coverage = item.get("coverage")
    if not isinstance(coverage, dict):
        return ["source_coverage_absent"]
    record = {
        key: coverage.get(key)
        for key in (
            "source_pdf_sha256",
            "source_pages",
            "ordered_target",
            "ordered_geometry_sha256",
            "final_crop_sha256",
            "shared_context",
            "shared_parent",
            "continuation",
        )
    }
    failures: list[str] = []
    actual_pdf_sha, declared_pdf_sha = (
        source_pdf_identity(run_dir) if pdf_identity is None else pdf_identity
    )
    if actual_pdf_sha is None:
        failures.append("source_pdf_absent")
    elif actual_pdf_sha != declared_pdf_sha or actual_pdf_sha != coverage.get("source_pdf_sha256"):
        failures.append("source_pdf_stale")
    if coverage.get("status") == "unresolved":
        codes = coverage.get("reason_codes")
        if isinstance(codes, list):
            failures.extend(code for code in codes if isinstance(code, str))
        else:
            failures.append("source_coverage_absent")
    if canonical_sha256(record) != coverage.get("proof_sha256"):
        failures.append("coverage_proof_stale")
    current_target = {
        "unit_id": item.get("unit_id"),
        "owner_question_id": item.get("owner_question_id"),
        "unit_kind": item.get("unit_kind"),
        "column": item.get("column"),
        "number_bbox": item.get("number_bbox"),
        "parts": item.get("parts", []),
    }
    if canonical_sha256(current_target) != coverage.get("ordered_geometry_sha256"):
        failures.append("coverage_proof_stale")
    crop_path = inside(
        (run_dir / str(item.get("output_file", ""))).resolve(), "final crop"
    )
    if (
        not crop_path.is_file()
        or sha256_file(crop_path) != item.get("output_sha256")
        or item.get("output_sha256") != coverage.get("final_crop_sha256")
    ):
        failures.append("final_crop_stale")
    source_pages = coverage.get("source_pages")
    if not isinstance(source_pages, list) or not source_pages:
        failures.append("source_page_image_absent")
        return failures
    for page in source_pages:
        source_image = page.get("source_page_image") if isinstance(page, dict) else None
        if not isinstance(source_image, dict) or not isinstance(source_image.get("path"), str):
            failures.append("source_page_image_absent")
            continue
        source_path = Path(source_image["path"])
        source_path = source_path if source_path.is_absolute() else run_dir / source_path
        source_path = inside(source_path.resolve(), "source page image")
        if not source_path.is_file():
            failures.append("source_page_image_absent")
        elif sha256_file(source_path) != source_image.get("sha256"):
            failures.append("source_page_image_stale")
    return sorted(set(failures))


def codex_executable() -> str | None:
    from astra_cli.isolation import native_executable
    try:
        return str(native_executable())
    except astra_cli.AstraError:
        return None


def codex_subprocess_env() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _CODEX_ENV_ALLOWLIST and value
    }


def run_codex_process(command: list[str], prompt: str, timeout: int | float,
                      *, stage: str | None = None) -> subprocess.CompletedProcess[str]:
    schema_path = Path(command[command.index('--output-schema') + 1])
    output = inside(Path(command[command.index('--output-last-message') + 1]), 'CLI output')
    images = tuple(Path(value) for value in command[command.index('--image') + 1:])
    if stage is None:
        stage = schema_path.stem
        if 'model_reasoning_effort="high"' in command:
            stage += '-reread'
    try:
        result = astra_cli.run(stage=stage, prompt=prompt, schema=json.loads(schema_path.read_text(encoding='utf-8')), images=images, timeout=timeout)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.name + ".tmp")
        temporary.write_bytes(result.text.encode("utf-8"))
        os.replace(temporary, output)
        write_json(output.with_suffix('.runtime.json'), result.receipt)
        return subprocess.CompletedProcess(command, 0, json.dumps(result.receipt, ensure_ascii=False), '')
    except astra_cli.AstraError as exc:
        output.unlink(missing_ok=True)
        write_json(output.with_suffix('.runtime.json'), exc.receipt)
        return subprocess.CompletedProcess(command, 1, json.dumps(exc.receipt, ensure_ascii=False), str(exc))


def codex_exec_command(
    schema_path: Path,
    last_message: Path,
    reasoning_effort: str,
    model: str | None,
    attachments: Iterable[Path],
) -> list[str]:
    executable = codex_executable()
    if not executable:
        raise FileNotFoundError("codex CLI not found")
    command = [
        executable,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(last_message),
        "-c",
        'approval_policy="never"',
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
    ]
    for setting in (
        "features.shell_tool=false",
        "features.unified_exec=false",
        "features.view_image=false",
        "features.apps=false",
        "features.multi_agent=false",
        "features.hooks=false",
        "features.memories=false",
        "features.goals=false",
        'web_search="disabled"',
        'shell_environment_policy.inherit="none"',
    ):
        command.extend(["-c", setting])
    command.append("--json")
    command.extend(["--model", astra_cli.MODEL])
    # --image is variadic. Keep stdin marker as the first positional so image
    # paths cannot greedily consume the prompt argument.
    command.extend(["-", "--image", *[str(path) for path in attachments]])
    return command


def write_json(path: Path, data: Any) -> None:
    inside(path.resolve(), "output")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(json_bytes(data))
    os.replace(temp, path)


def safe_slug(value: str, limit: int = 72) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    value = re.sub(r"[^\w가-힣]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"[-_]{2,}", "-", value).strip("-_.")
    return (value or "unnamed")[:limit].rstrip("-_.")


def discover_pdfs(source: Path) -> list[Path]:
    source = source.expanduser().resolve()
    if source.is_file():
        if source.suffix.lower() != ".pdf":
            raise ValueError(f"PDF가 아닙니다: {source}")
        return [source]
    if not source.is_dir():
        raise FileNotFoundError(source)
    return sorted(
        (path for path in source.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf"),
        key=lambda path: str(path).casefold(),
    )


def parse_pages(spec: str, total: int) -> list[int]:
    if total <= 0:
        return []
    if not spec or spec.lower() == "all":
        return list(range(total))
    pages: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                start, end = end, start
            pages.update(range(start - 1, end))
        else:
            pages.add(int(token) - 1)
    invalid = sorted(page + 1 for page in pages if page < 0 or page >= total)
    if invalid:
        raise ValueError(f"페이지 범위를 벗어났습니다: {invalid}; 전체 {total}쪽")
    return sorted(pages)


def text_layer_info(page: Any, max_chars: int = 6000) -> tuple[int, str, str]:
    text_page = page.get_textpage()
    try:
        count = int(text_page.count_chars())
        text = text_page.get_text_range() if count else ""
    finally:
        text_page.close()
    text = text.replace("\x00", "").replace("\r\n", "\n").strip()
    if not text:
        return count, "none", "(텍스트 레이어 없음)"
    bad = sum(char == "\ufffd" or unicodedata.category(char) == "Co" for char in text)
    quality = "scrambled" if bad / max(len(text), 1) > 0.03 else "usable"
    if quality == "scrambled":
        return count, quality, "(텍스트 레이어가 깨져 있어 사용하지 않음)"
    return count, quality, text[:max_chars]


def inspect_pdf(path: Path) -> dict[str, Any]:
    stat = path.stat()
    result: dict[str, Any] = {
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "status": "pending",
        "pages": 0,
        "sampled_pages": [],
    }
    if stat.st_size == 0:
        result.update(status="empty_file", error="0바이트 PDF")
        return result
    try:
        document = pdfium.PdfDocument(str(path))
    except Exception as exc:  # PDFium exposes format/password errors as runtime exceptions.
        result.update(status="open_error", error=f"{type(exc).__name__}: {exc}")
        return result
    try:
        total = len(document)
        result["pages"] = total
        if total:
            candidates = {0, total - 1, total // 2}
            if total >= 8:
                candidates.update({total // 4, (total * 3) // 4})
            for index in sorted(candidates):
                page = document[index]
                try:
                    width, height = page.get_size()
                    chars, quality, _ = text_layer_info(page, max_chars=1)
                    result["sampled_pages"].append(
                        {
                            "page": index + 1,
                            "width_points": round(width, 2),
                            "height_points": round(height, 2),
                            "rotation": int(page.get_rotation()),
                            "text_chars": chars,
                            "text_quality": quality,
                        }
                    )
                finally:
                    page.close()
        result["status"] = "ok"
    except Exception as exc:
        result.update(status="read_error", error=f"{type(exc).__name__}: {exc}")
    finally:
        document.close()
    return result


def run_audit(args: argparse.Namespace) -> int:
    pdfs = discover_pdfs(Path(args.source))
    results: list[dict[str, Any]] = []
    # PDFium is not thread-safe. Separate processes keep each document handle
    # isolated while retaining parallel audit throughput.
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(inspect_pdf, path): path for path in pdfs}
        for done, future in enumerate(as_completed(futures), 1):
            item = future.result()
            results.append(item)
            print(f"[{done}/{len(pdfs)}] {item['status']}: {futures[future].name}")
    results.sort(key=lambda item: item["path"].casefold())
    ext_counts: dict[str, int] = {}
    source = Path(args.source).expanduser().resolve()
    if source.is_dir():
        for path in source.rglob("*"):
            if path.is_file():
                ext = path.suffix.lower() or "(no_extension)"
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
    else:
        ext_counts[".pdf"] = 1
    hashes: dict[str, list[str]] = {}
    if args.hash:
        for item in results:
            if item["status"] != "empty_file":
                digest = sha256_file(Path(item["path"]))
                item["sha256"] = digest
                hashes.setdefault(digest, []).append(item["path"])
    summary = {
        "version": VERSION,
        "created_at": utc_now(),
        "source": str(source),
        "file_extension_counts": dict(sorted(ext_counts.items())),
        "pdf_count": len(results),
        "total_pdf_bytes": sum(item["size_bytes"] for item in results),
        "total_pages_readable": sum(item["pages"] for item in results if item["status"] == "ok"),
        "status_counts": {
            status: sum(item["status"] == status for item in results)
            for status in sorted({item["status"] for item in results})
        },
        "duplicate_groups": [paths for paths in hashes.values() if len(paths) > 1],
        "pdfs": results,
    }
    output = Path(args.out).expanduser().resolve()
    write_json(output, summary)
    print(f"감사 결과: {output}")
    return 0


def page_image_path(page_dir: Path, page_index: int, dpi: int, rotation: int = 0) -> Path:
    return page_dir / f"page-{page_index + 1:04d}-dpi{dpi}-rot{rotation:03d}.jpg"


def tile_image_path(page_dir: Path, page_index: int, dpi: int, name: str, rotation: int = 0) -> Path:
    return page_dir / f"page-{page_index + 1:04d}-dpi{dpi}-rot{rotation:03d}-tile-{name}.jpg"


def render_page(document: Any, page_index: int, dpi: int, output: Path, rotation: int = 0) -> None:
    inside(output.resolve(), "output")
    output.parent.mkdir(parents=True, exist_ok=True)
    page = document[page_index]
    try:
        bitmap = page.render(scale=dpi / 72.0)
        try:
            image = bitmap.to_pil().convert("RGB")
        finally:
            bitmap.close()
        try:
            if rotation:
                rotated = image.rotate(-rotation, expand=True, fillcolor="white")
                image.close()
                image = rotated
            image.save(output, format="JPEG", quality=94, subsampling=0, optimize=True)
        finally:
            image.close()
    finally:
        page.close()


def create_tiles(
    overview: Path, page_index: int, dpi: int, page_dir: Path, rotation: int = 0
) -> list[Path]:
    paths: list[Path] = []
    with Image.open(overview) as source:
        width, height = source.size
        for name, (x0, y0, x1, y1), _ in TILE_SPECS:
            output = tile_image_path(page_dir, page_index, dpi, name, rotation)
            paths.append(output)
            if output.exists():
                continue
            box = (
                math.floor(x0 * width),
                math.floor(y0 * height),
                math.ceil(x1 * width),
                math.ceil(y1 * height),
            )
            tile = source.crop(box).convert("RGB")
            try:
                tile.save(output, format="JPEG", quality=95, subsampling=0, optimize=True)
            finally:
                tile.close()
    return paths


def ensure_render_assets(
    pdf_path: Path,
    run_dir: Path,
    primary_pages: Iterable[int],
    vision_dpi: int,
    tiles: str,
    neighbor_pages: Iterable[int] = (),
    rotations: dict[int, int] | None = None,
) -> dict[int, str]:
    page_dir = run_dir / "pages"
    inside(page_dir.resolve(), "output")
    page_dir.mkdir(parents=True, exist_ok=True)
    primary = set(primary_pages)
    required = primary | set(neighbor_pages)
    hints: dict[int, str] = {}
    rotations = rotations or {}
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        for page_index in sorted(required):
            if page_index < 0 or page_index >= len(document):
                continue
            rotation = int(rotations.get(page_index, 0))
            overview = page_image_path(page_dir, page_index, vision_dpi, rotation)
            if not overview.exists():
                render_page(document, page_index, vision_dpi, overview, rotation)
            if page_index in primary:
                page = document[page_index]
                try:
                    _, _, hint = text_layer_info(page)
                    hints[page_index] = hint
                finally:
                    page.close()
                if tiles == "quadrants":
                    create_tiles(overview, page_index, vision_dpi, page_dir, rotation)
    finally:
        document.close()
    return hints


def attachment_set(
    run_dir: Path,
    page_index: int,
    total_pages: int,
    vision_dpi: int,
    tiles: str,
    context_pages: int,
    rotations: dict[int, int] | None = None,
) -> tuple[list[Path], str]:
    page_dir = run_dir / "pages"
    rotations = rotations or {}
    primary_rotation = int(rotations.get(page_index, 0))
    attachments = [page_image_path(page_dir, page_index, vision_dpi, primary_rotation)]
    descriptions = [
        f"1. PRIMARY overview: physical PDF page {page_index + 1}; coordinate space [0,0,1000,1000]."
    ]
    if tiles == "quadrants":
        for name, _, normalized_box in TILE_SPECS:
            attachments.append(tile_image_path(page_dir, page_index, vision_dpi, name, primary_rotation))
            descriptions.append(
                f"{len(attachments)}. PRIMARY zoom tile {name}; overview bbox {normalized_box}; "
                "return coordinates in overview space, not tile space."
            )
    if context_pages and page_index > 0:
        attachments.append(
            page_image_path(page_dir, page_index - 1, vision_dpi, int(rotations.get(page_index - 1, 0)))
        )
        descriptions.append(
            f"{len(attachments)}. PREVIOUS overview: physical PDF page {page_index}; page_offset=-1."
        )
    if context_pages and page_index + 1 < total_pages:
        attachments.append(
            page_image_path(page_dir, page_index + 1, vision_dpi, int(rotations.get(page_index + 1, 0)))
        )
        descriptions.append(
            f"{len(attachments)}. NEXT overview: physical PDF page {page_index + 2}; page_offset=+1."
        )
    missing = [path for path in attachments if not path.exists()]
    if missing:
        raise FileNotFoundError(f"렌더 이미지가 없습니다: {missing}")
    return attachments, "\n".join(descriptions)


def build_prompt(
    template_text: str,
    pdf_path: Path,
    page_index: int,
    total_pages: int,
    attachment_map: str,
    text_hint: str,
) -> str:
    return Template(template_text).safe_substitute(
        DOCUMENT_NAME=pdf_path.name,
        PAGE_NUMBER=page_index + 1,
        TOTAL_PAGES=total_pages,
        ATTACHMENT_MAP=attachment_map,
        TEXT_HINT=text_hint,
    )


def validate_bbox(value: Any, where: str, errors: list[str]) -> None:
    if not isinstance(value, list) or len(value) != 4:
        errors.append(f"{where}: bbox는 길이 4 배열이어야 함")
        return
    if any(not isinstance(number, int) or isinstance(number, bool) for number in value):
        errors.append(f"{where}: bbox 값은 정수여야 함")
        return
    x0, y0, x1, y1 = value
    if not all(0 <= number <= 1000 for number in value):
        errors.append(f"{where}: bbox 범위는 0..1000")
    if x0 >= x1 or y0 >= y1:
        errors.append(f"{where}: x0<x1, y0<y1이어야 함")


def validate_parts(parts: Any, where: str, errors: list[str], require_one: bool = False) -> None:
    if not isinstance(parts, list):
        errors.append(f"{where}: 배열이어야 함")
        return
    if require_one and not parts:
        errors.append(f"{where}: 최소 한 영역 필요")
    for index, part in enumerate(parts):
        label = f"{where}[{index}]"
        if not isinstance(part, dict):
            errors.append(f"{label}: 객체여야 함")
            continue
        if set(part) != {"page_offset", "role", "bbox"}:
            errors.append(f"{label}: 필드 불일치")
        if part.get("page_offset") not in {-1, 0, 1}:
            errors.append(f"{label}: page_offset은 -1, 0, 1")
        if part.get("role") not in PART_ROLES:
            errors.append(f"{label}: 알 수 없는 role")
        validate_bbox(part.get("bbox"), label, errors)


def validate_detection(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["최상위 값은 객체여야 함"]
    expected = {"page_type", "layout", "questions", "ignored_regions", "warnings"}
    if set(data) != expected:
        errors.append(f"최상위 필드 불일치: {sorted(set(data) ^ expected)}")
    if data.get("page_type") not in PAGE_TYPES:
        errors.append("page_type 값 오류")
    if data.get("layout") not in LAYOUT_TYPES:
        errors.append("layout 값 오류")
    questions = data.get("questions")
    if not isinstance(questions, list):
        errors.append("questions는 배열이어야 함")
        questions = []
    ids: set[str] = set()
    for index, question in enumerate(questions):
        where = f"questions[{index}]"
        if not isinstance(question, dict):
            errors.append(f"{where}: 객체여야 함")
            continue
        required = {
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
        if set(question) != required:
            errors.append(f"{where}: 필드 불일치")
        local_id = question.get("local_id")
        if not isinstance(local_id, str) or not local_id:
            errors.append(f"{where}: local_id 오류")
        elif local_id in ids:
            errors.append(f"{where}: local_id 중복 {local_id}")
        else:
            ids.add(local_id)
        if not isinstance(question.get("printed_number"), str):
            errors.append(f"{where}: printed_number는 문자열")
        number_bbox = question.get("number_bbox")
        if number_bbox != []:
            validate_bbox(number_bbox, where + ".number_bbox", errors)
        if question.get("kind") != "continuation" and number_bbox == []:
            errors.append(f"{where}: 비-continuation 문항에는 number_bbox 필요")
        if question.get("kind") not in QUESTION_KINDS:
            errors.append(f"{where}: kind 오류")
        if question.get("answer_type") not in ANSWER_TYPES:
            errors.append(f"{where}: answer_type 오류")
        if question.get("group_label") is not None and not isinstance(question.get("group_label"), str):
            errors.append(f"{where}: group_label 오류")
        validate_parts(question.get("shared_context_parts"), where + ".shared_context_parts", errors)
        validate_parts(question.get("main_parts"), where + ".main_parts", errors)
        subquestions = question.get("subquestions")
        if not isinstance(subquestions, list):
            errors.append(f"{where}.subquestions: 배열이어야 함")
            subquestions = []
        for sub_index, subquestion in enumerate(subquestions):
            sub_where = f"{where}.subquestions[{sub_index}]"
            if not isinstance(subquestion, dict) or set(subquestion) != {
                "label",
                "answer_type",
                "parts",
                "confidence",
            }:
                errors.append(f"{sub_where}: 필드 불일치")
                continue
            if not isinstance(subquestion.get("label"), str):
                errors.append(f"{sub_where}: label은 문자열")
            if subquestion.get("answer_type") not in ANSWER_TYPES:
                errors.append(f"{sub_where}: answer_type 오류")
            validate_parts(subquestion.get("parts"), sub_where + ".parts", errors, require_one=True)
            confidence = subquestion.get("confidence")
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
                errors.append(f"{sub_where}: confidence 범위 오류")
        for boolean_name in ("starts_on_previous_page", "continues_next_page"):
            if not isinstance(question.get(boolean_name), bool):
                errors.append(f"{where}.{boolean_name}: boolean이어야 함")
        confidence = question.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            errors.append(f"{where}: confidence 범위 오류")
        if not question.get("main_parts") and not subquestions:
            errors.append(f"{where}: main_parts 또는 subquestions가 필요")
        if question.get("kind") == "parent_with_subquestions" and not subquestions:
            errors.append(f"{where}: parent 유형에는 subquestions가 필요")
    ignored = data.get("ignored_regions")
    if not isinstance(ignored, list):
        errors.append("ignored_regions는 배열이어야 함")
    else:
        for index, region in enumerate(ignored):
            where = f"ignored_regions[{index}]"
            if not isinstance(region, dict) or set(region) != {"role", "bbox"}:
                errors.append(f"{where}: 필드 불일치")
                continue
            validate_bbox(region.get("bbox"), where, errors)
    if not isinstance(data.get("warnings"), list) or any(
        not isinstance(item, str) for item in data.get("warnings", [])
    ):
        errors.append("warnings는 문자열 배열이어야 함")
    return errors


def extract_json_message(text: str) -> Any:
    text = text.strip().lstrip("\ufeff")
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def validate_triage(data: Any, expected_pages: list[int]) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict) or set(data) != {"pages", "warnings"}:
        return ["triage 최상위 필드 불일치"]
    if not isinstance(data["warnings"], list) or any(not isinstance(item, str) for item in data["warnings"]):
        errors.append("triage warnings는 문자열 배열이어야 함")
    pages = data.get("pages")
    if not isinstance(pages, list):
        return errors + ["triage pages는 배열이어야 함"]
    actual_numbers: list[int] = []
    for index, item in enumerate(pages):
        where = f"pages[{index}]"
        if not isinstance(item, dict) or set(item) != {
            "page_number",
            "page_type",
            "has_student_questions",
            "rotation_correction",
            "confidence",
        }:
            errors.append(f"{where}: 필드 불일치")
            continue
        number = item.get("page_number")
        if not isinstance(number, int) or isinstance(number, bool):
            errors.append(f"{where}: page_number 오류")
        else:
            actual_numbers.append(number)
        page_type = item.get("page_type")
        if page_type not in PAGE_TYPES:
            errors.append(f"{where}: page_type 오류")
        has_questions = item.get("has_student_questions")
        if not isinstance(has_questions, bool):
            errors.append(f"{where}: has_student_questions 오류")
        elif page_type in PROBLEM_PAGE_TYPES and not has_questions:
            errors.append(f"{where}: 문제 유형인데 has_student_questions=false")
        elif page_type in NON_PROBLEM_PAGE_TYPES and has_questions:
            errors.append(f"{where}: 비문제 유형인데 has_student_questions=true")
        elif page_type in CONTEXT_PAGE_TYPES and has_questions:
            errors.append(f"{where}: shared_material_only는 has_student_questions=false")
        if item.get("rotation_correction") not in {0, 90, 180, 270}:
            errors.append(f"{where}: rotation_correction 오류")
        confidence = item.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            errors.append(f"{where}: confidence 범위 오류")
    if actual_numbers != expected_pages:
        errors.append(f"페이지 집합/순서 불일치: expected={expected_pages}, actual={actual_numbers}")
    return errors


def create_triage_contact_sheets(
    pdf_path: Path,
    run_dir: Path,
    page_indices: list[int],
    dpi: int,
    batch_size: int,
) -> list[tuple[list[int], Path]]:
    triage_dir = run_dir / "triage"
    inside(triage_dir.resolve(), "output")
    triage_dir.mkdir(parents=True, exist_ok=True)
    batches: list[tuple[list[int], Path]] = []
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        for batch_number, start in enumerate(range(0, len(page_indices), batch_size), 1):
            batch = page_indices[start : start + batch_size]
            output = triage_dir / (
                f"contact-{batch_number:04d}-p{batch[0] + 1:04d}-p{batch[-1] + 1:04d}-dpi{dpi}.jpg"
            )
            batches.append((batch, output))
            if output.exists():
                continue
            columns = min(4, len(batch))
            rows = math.ceil(len(batch) / columns)
            cell_width, cell_height, label_height = 500, 720, 24
            sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), "white")
            draw = ImageDraw.Draw(sheet)
            for offset, page_index in enumerate(batch):
                page = document[page_index]
                try:
                    bitmap = page.render(scale=dpi / 72.0)
                    try:
                        thumbnail = bitmap.to_pil().convert("RGB")
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                thumbnail.thumbnail((cell_width - 12, cell_height - label_height - 12), Image.Resampling.LANCZOS)
                column, row = offset % columns, offset // columns
                left, top = column * cell_width, row * cell_height
                draw.rectangle((left, top, left + cell_width - 1, top + cell_height - 1), outline="#888888")
                draw.text((left + 6, top + 5), f"PDF p.{page_index + 1}", fill="black")
                x = left + (cell_width - thumbnail.width) // 2
                y = top + label_height + (cell_height - label_height - thumbnail.height) // 2
                sheet.paste(thumbnail, (x, y))
                thumbnail.close()
            sheet.save(output, format="JPEG", quality=92, subsampling=0, optimize=True)
            sheet.close()
    finally:
        document.close()
    return batches


def triage_batch_path(run_dir: Path, batch_number: int) -> Path:
    return run_dir / "triage" / f"batch-{batch_number:04d}.json"


def triage_cache_matches(path: Path, expected_meta: dict[str, Any], expected_pages: list[int]) -> bool:
    if not path.exists():
        return False
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        if wrapper.get("meta") != expected_meta:
            return False
        return not validate_triage(wrapper["result"], expected_pages)
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return False


def run_triage_batch(job: dict[str, Any]) -> dict[str, Any]:
    output: Path = job["output"]
    if not job["force"] and triage_cache_matches(output, job["meta"], job["expected_pages"]):
        return {"batch": job["batch_number"], "status": "cached", "output": str(output)}
    log_dir: Path = job["log_dir"]
    inside(log_dir.resolve(), "output")
    log_dir.mkdir(parents=True, exist_ok=True)
    last_error = "unknown error"
    for attempt in range(job["retries"] + 1):
        last_message = output.with_name(output.stem + f".attempt-{attempt + 1}.last.tmp")
        if last_message.exists():
            last_message.unlink()
        try:
            command = codex_exec_command(
                TRIAGE_SCHEMA_PATH,
                last_message,
                job["reasoning_effort"],
                job.get("model"),
                [job["contact_sheet"]],
            )
        except FileNotFoundError as exc:
            return {"batch": job["batch_number"], "status": "error", "error": str(exc)}
        retry_note = ""
        if attempt:
            retry_note = (
                "\n\nPrevious result failed validation. Return every expected page exactly once. "
                f"Validation error: {last_error[:1000]}"
            )
        started = time.monotonic()
        try:
            process = run_codex_process(
                command, job["prompt"] + retry_note, job["timeout"]
            )
            elapsed = round(time.monotonic() - started, 2)
            stem = f"triage-{job['batch_number']:04d}-attempt-{attempt + 1}"
            (log_dir / f"{stem}.events.jsonl").write_text(process.stdout, encoding="utf-8")
            (log_dir / f"{stem}.stderr.log").write_text(
                f"exit_code={process.returncode}\nelapsed_seconds={elapsed}\n{process.stderr}",
                encoding="utf-8",
            )
            if process.returncode != 0:
                last_error = f"codex exit {process.returncode}: {process.stderr[-2000:]}"
                continue
            if not last_message.exists():
                last_error = "Codex가 triage 결과 파일을 만들지 않음"
                continue
            data = extract_json_message(last_message.read_text(encoding="utf-8"))
            errors = validate_triage(data, job["expected_pages"])
            if errors:
                last_error = "; ".join(errors)
                continue
            write_json(output, {"meta": job["meta"], "result": data})
            try:
                last_message.unlink()
            except OSError:
                pass
            return {"batch": job["batch_number"], "status": "ok", "output": str(output)}
        except subprocess.TimeoutExpired as exc:
            last_error = f"timeout after {job['timeout']}s: {exc}"
        except (OSError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    write_json(
        output.with_suffix(".error.json"),
        {"batch": job["batch_number"], "error": last_error, "failed_at": utc_now()},
    )
    return {"batch": job["batch_number"], "status": "error", "error": last_error}


def triage_pdf(
    args: argparse.Namespace,
    pdf_path: Path,
    run_dir: Path,
    source_sha: str,
    page_indices: list[int],
) -> tuple[list[int], dict[str, Any]]:
    if not page_indices:
        return [], {"classified_pages": 0, "selected_pages": []}
    template_text = TRIAGE_PROMPT_PATH.read_text(encoding="utf-8")
    template_sha = sha256_text(template_text)
    schema_sha = sha256_file(TRIAGE_SCHEMA_PATH)
    batches = create_triage_contact_sheets(
        pdf_path,
        run_dir,
        page_indices,
        args.triage_dpi,
        args.triage_batch_size,
    )
    jobs: list[dict[str, Any]] = []
    for batch_number, (batch, contact_sheet) in enumerate(batches, 1):
        expected_pages = [page + 1 for page in batch]
        prompt = Template(template_text).safe_substitute(
            DOCUMENT_NAME=pdf_path.name,
            EXPECTED_PAGES=", ".join(map(str, expected_pages)),
        )
        meta = {
            "pipeline_version": VERSION,
            "source_sha256": source_sha,
            "pages": expected_pages,
            "triage_dpi": args.triage_dpi,
            "model": args.model,
            "reasoning_effort": args.triage_reasoning_effort,
            "prompt_template_sha256": template_sha,
            "schema_sha256": schema_sha,
        }
        jobs.append(
            {
                "batch_number": batch_number,
                "output": triage_batch_path(run_dir, batch_number),
                "contact_sheet": contact_sheet,
                "expected_pages": expected_pages,
                "prompt": prompt,
                "meta": meta,
                "log_dir": run_dir / "logs",
                "run_dir": run_dir,
                "model": args.model,
                "reasoning_effort": args.triage_reasoning_effort,
                "timeout": args.timeout,
                "retries": args.retries,
                "force": args.force,
            }
        )
    print(f"저비용 페이지 분류: {len(page_indices)}쪽 / {len(jobs)}개 접촉시트")
    results = execute_triage_jobs(jobs, args.workers)
    errors = [result for result in results if result["status"] == "error"]
    if errors:
        raise RuntimeError(f"triage {len(errors)}개 배치 실패")
    classified: list[dict[str, Any]] = []
    warnings: list[str] = []
    for batch_number in range(1, len(batches) + 1):
        wrapper = json.loads(triage_batch_path(run_dir, batch_number).read_text(encoding="utf-8"))
        classified.extend(wrapper["result"]["pages"])
        warnings.extend(wrapper["result"]["warnings"])
    classified.sort(key=lambda item: item["page_number"])
    expected = [page + 1 for page in page_indices]
    errors = validate_triage({"pages": classified, "warnings": warnings}, expected)
    if errors:
        raise ValueError("triage aggregate: " + "; ".join(errors))
    selected = [
        item["page_number"] - 1
        for item in classified
        if item["has_student_questions"]
        or item["page_type"] in CONTEXT_PAGE_TYPES
        or item["confidence"] < args.triage_confidence_threshold
    ]
    review = [
        {
            "page": item["page_number"],
            "page_type": item["page_type"],
            "confidence": item["confidence"],
            "reason": "낮은 triage 신뢰도로 상세 검출에 포함",
        }
        for item in classified
        if item["confidence"] < args.triage_confidence_threshold
    ]
    aggregate = {
        "meta": {
            "pipeline_version": VERSION,
            "source_pdf": str(pdf_path),
            "source_sha256": source_sha,
            "created_at": utc_now(),
            "triage_dpi": args.triage_dpi,
            "batch_size": args.triage_batch_size,
        },
        "pages": classified,
        "selected_pages": [page + 1 for page in selected],
        "shared_material_pages": [
            item["page_number"]
            for item in classified
            if item["page_type"] in CONTEXT_PAGE_TYPES
        ],
        "warnings": warnings,
    }
    write_json(run_dir / "triage.json", aggregate)
    write_json(run_dir / "review_queue.triage.json", review)
    return selected, {
        "classified_pages": len(classified),
        "selected_pages": [page + 1 for page in selected],
        "rotations": {
            str(item["page_number"]): item["rotation_correction"] for item in classified
        },
        "review_count": len(review),
    }


def execute_triage_jobs(jobs: list[dict[str, Any]], workers: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(run_triage_batch, job): job for job in jobs}
        for done, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results.append(result)
            detail = result.get("error", "")
            print(f"  [triage {done}/{len(jobs)}] batch {result['batch']}: {result['status']} {detail[:160]}")
    return sorted(results, key=lambda result: result["batch"])


def validate_verification(data: Any) -> list[str]:
    if not isinstance(data, dict) or set(data) != VERIFICATION_FIELDS:
        return ["verification 필드 불일치"]
    errors: list[str] = []
    if data["verdict"] not in {"pass", "review", "fail"}:
        errors.append("verdict 오류")
    for key in VERIFICATION_BOOLEAN_FIELDS:
        if not isinstance(data[key], bool):
            errors.append(f"{key}: boolean이어야 함")
    if not isinstance(data["reasons"], list) or any(not isinstance(item, str) for item in data["reasons"]):
        errors.append("reasons는 문자열 배열")
    confidence = data["confidence"]
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        errors.append("confidence 범위 오류")
    if verification_hard_failure(data) and data["verdict"] != "fail":
        errors.append("hard failure 플래그에는 verdict=fail 필요")
    return errors


def verification_hard_failure(data: dict[str, Any]) -> bool:
    return (
        not data["complete_question"]
        or not data["contains_expected_identifier"]
        or not data["contains_required_context"]
        or data["contains_other_question"]
        or data["required_content_missing_or_unreadable"]
        or data["explicit_answer_evidence"]
        or data["target_content_clipped"]
        or data["identifier_clipped"]
    )


def verification_reason_codes(data: dict[str, Any]) -> list[str]:
    return [
        code
        for code, field, value in VERIFICATION_REASON_FIELDS
        if data.get(field) is value
    ]


def normalize_verification(data: Any) -> Any:
    """Enforce severity without promoting harmless crop artifacts to FAIL."""
    if (
        not isinstance(data, dict)
        or set(data) != VERIFICATION_FIELDS
        or data.get("verdict") not in {"pass", "review", "fail"}
        or any(not isinstance(data[key], bool) for key in VERIFICATION_BOOLEAN_FIELDS)
    ):
        return data
    normalized = dict(data)
    if verification_hard_failure(normalized):
        normalized["verdict"] = "fail"
    elif (
        normalized["verdict"] == "review"
        and normalized["cosmetic_or_foreign_fragment_only"]
        and not normalized["contains_answer_or_solution"]
        and not normalized["excessive_blank_space"]
    ) or (
        normalized["verdict"] in {"review", "fail"}
        and normalized["contains_answer_or_solution"]
        and not normalized["explicit_answer_evidence"]
        and not normalized["cut_off_content"]
        and not normalized["excessive_blank_space"]
    ):
        normalized["verdict"] = "pass"
    elif normalized["verdict"] == "fail":
        normalized["verdict"] = "review"
    return normalized


def verification_cache_matches(path: Path, meta: dict[str, Any]) -> bool:
    if not path.exists():
        return False
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        return wrapper.get("meta") == meta and not validate_verification(wrapper["result"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return False


def verification_input_identity(prompt: str, reasoning_effort: str) -> dict[str, str]:
    stage = VERIFY_SCHEMA_PATH.stem + ("-reread" if reasoning_effort == "high" else "")
    return {
        "stage": stage,
        "model": astra_cli.MODEL,
        "reasoning_effort": astra_cli.effort_for(stage),
        "rendered_prompt_sha256": sha256_text(prompt),
    }


def run_verification_job(job: dict[str, Any]) -> dict[str, Any]:
    job = {**job, "meta": {**job["meta"], **verification_input_identity(job["prompt"], job["reasoning_effort"])}}
    output: Path = job["output"]
    if not job["force"] and verification_cache_matches(output, job["meta"]):
        wrapper = json.loads(output.read_text(encoding="utf-8"))
        return {
            "unit_id": job["unit_id"],
            "status": "cached",
            "verdict": wrapper["result"]["verdict"],
            "output": str(output),
        }
    inside(output.resolve(), "output")
    output.parent.mkdir(parents=True, exist_ok=True)
    log_dir: Path = job["log_dir"]
    inside(log_dir.resolve(), "output")
    log_dir.mkdir(parents=True, exist_ok=True)
    last_error = "unknown error"
    for attempt in range(job["retries"] + 1):
        last_message = output.with_name(output.stem + f".attempt-{attempt + 1}.last.tmp")
        if last_message.exists():
            last_message.unlink()
        try:
            command = codex_exec_command(
                VERIFY_SCHEMA_PATH,
                last_message,
                job["reasoning_effort"],
                job.get("model"),
                job["attachments"],
            )
        except FileNotFoundError as exc:
            return {"unit_id": job["unit_id"], "status": "error", "error": str(exc)}
        retry_note = ""
        if attempt:
            retry_note = f"\n\nPrevious result failed validation: {last_error[:1000]}"
        started = time.monotonic()
        try:
            process = run_codex_process(
                command, job["prompt"] + retry_note, job["timeout"], stage=job["meta"]["stage"]
            )
            elapsed = round(time.monotonic() - started, 2)
            stem = f"verify-{safe_slug(job['unit_id'], 48)}-attempt-{attempt + 1}"
            (log_dir / f"{stem}.events.jsonl").write_text(process.stdout, encoding="utf-8")
            (log_dir / f"{stem}.stderr.log").write_text(
                f"exit_code={process.returncode}\nelapsed_seconds={elapsed}\n{process.stderr}",
                encoding="utf-8",
            )
            if process.returncode != 0:
                last_error = f"codex exit {process.returncode}: {process.stderr[-2000:]}"
                continue
            if not last_message.exists():
                last_error = "Codex가 verification 결과 파일을 만들지 않음"
                continue
            data = normalize_verification(
                extract_json_message(last_message.read_text(encoding="utf-8"))
            )
            errors = validate_verification(data)
            if errors:
                last_error = "; ".join(errors)
                continue
            write_json(output, {"meta": job["meta"], "result": data})
            try:
                last_message.unlink()
            except OSError:
                pass
            return {
                "unit_id": job["unit_id"],
                "status": "ok",
                "verdict": data["verdict"],
                "output": str(output),
            }
        except subprocess.TimeoutExpired as exc:
            last_error = f"timeout after {job['timeout']}s: {exc}"
        except (OSError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    write_json(
        output.with_suffix(".error.json"),
        {"unit_id": job["unit_id"], "error": last_error, "failed_at": utc_now()},
    )
    return {"unit_id": job["unit_id"], "status": "error", "error": last_error}


def verify_run(
    run_dir: Path,
    mode: str,
    model: str | None,
    reasoning_effort: str,
    workers: int,
    timeout: int,
    retries: int,
    force: bool,
    confidence_threshold: float,
) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    review_path = run_dir / "review_queue.json"
    review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else []
    flagged_pages = {item.get("page") for item in review if item.get("page") is not None}
    if mode == "all":
        selected = list(manifest["items"])
    elif mode == "review":
        selected = [
            item
            for item in manifest["items"]
            if item["review_reasons"]
            or item["confidence"] < confidence_threshold
            or flagged_pages.intersection(item["source_pages"])
        ]
    else:
        selected = []
    template_text = VERIFY_PROMPT_PATH.read_text(encoding="utf-8")
    template_sha = sha256_text(template_text)
    schema_sha = sha256_file(VERIFY_SCHEMA_PATH)
    jobs: list[dict[str, Any]] = []
    item_by_id = {item["unit_id"]: item for item in manifest["items"]}
    pdf_identity = source_pdf_identity(run_dir)
    for item in selected:
        failures = coverage_failures(run_dir, item, pdf_identity)
        coverage = item.get("coverage")
        if not isinstance(coverage, dict) or coverage.get(
            "source_pdf_sha256"
        ) != manifest.get("source_sha256"):
            failures.append("source_pdf_stale")
            failures = sorted(set(failures))
        if failures:
            if isinstance(coverage, dict):
                coverage["status"] = "unresolved"
                coverage["reason_codes"] = failures
            review.append(
                review_entry(
                    item["source_pages"][0] if item.get("source_pages") else None,
                    item["unit_id"],
                    "원문-크롭 coverage 증거가 없거나 현재 입력과 다름",
                    failures,
                )
            )
            continue
        coverage = item["coverage"]
        crop_path = inside((run_dir / item["output_file"]).resolve(), "final crop")
        source_paths: list[Path] = []
        attachment_map: list[str] = []
        for index, page in enumerate(coverage["source_pages"], 1):
            source_image = page["source_page_image"]
            source_path = Path(source_image["path"])
            source_path = source_path if source_path.is_absolute() else run_dir / source_path
            source_path = inside(source_path.resolve(), "source page image")
            source_paths.append(source_path)
            attachment_map.append(
                f"{index}. SOURCE page {page['page']} rotation={page['rotation']} "
                f"layout={page['layout']} sha256={source_image['sha256']}"
            )
        attachment_map.append(
            f"{len(source_paths) + 1}. FINAL CROP sha256={item['output_sha256']}"
        )
        prompt = Template(template_text).safe_substitute(
            UNIT_KIND=item["unit_kind"],
            PRINTED_NUMBER=item["printed_number"],
            ANSWER_TYPE=item.get("answer_type", "unknown"),
            ANSWER_SPACE=manifest.get("answer_space", "include"),
            SIDECARS=manifest.get("sidecars", "include"),
            SOURCE_ATTACHMENT_MAP="\n".join(attachment_map),
            SOURCE_TARGET=json.dumps(
                coverage["ordered_target"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        meta = {
            "pipeline_version": VERSION,
            "unit_id": item["unit_id"],
            "source_pdf_sha256": coverage["source_pdf_sha256"],
            "source_page_image_sha256": [
                page["source_page_image"]["sha256"] for page in coverage["source_pages"]
            ],
            "ordered_geometry_sha256": coverage["ordered_geometry_sha256"],
            "coverage_proof_sha256": coverage["proof_sha256"],
            "crop_sha256": item["output_sha256"],
            **verification_input_identity(prompt, reasoning_effort),
            "prompt_template_sha256": template_sha,
            "schema_sha256": schema_sha,
        }
        jobs.append(
            {
                "unit_id": item["unit_id"],
                "output": run_dir / "verification" / f"{safe_slug(item['unit_id'], 80)}.json",
                "crop_path": crop_path,
                "attachments": source_paths + [crop_path],
                "prompt": prompt,
                "meta": meta,
                "log_dir": run_dir / "logs",
                "run_dir": run_dir,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "timeout": timeout,
                "retries": retries,
                "force": force,
            }
        )
    results: list[dict[str, Any]] = []
    if jobs:
        print(f"VLM 크롭 검수: {len(jobs)}개 ({mode})")
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(run_verification_job, job): job for job in jobs}
            for done, future in enumerate(as_completed(futures), 1):
                result = future.result()
                results.append(result)
                print(
                    f"  [verify {done}/{len(jobs)}] {result['unit_id']}: "
                    f"{result.get('verdict', result['status'])}"
                )
    errors = [result for result in results if result["status"] == "error"]
    if errors:
        raise RuntimeError(f"verification {len(errors)}개 실패")
    verdict_counts = {verdict: 0 for verdict in ("pass", "review", "fail")}
    for result in results:
        wrapper = json.loads(Path(result["output"]).read_text(encoding="utf-8"))
        verification = wrapper["result"]
        item = item_by_id[result["unit_id"]]
        item["verification"] = verification
        reason_codes = verification_reason_codes(verification)
        coverage = item["coverage"]
        coverage["status"] = (
            "verified"
            if verification["verdict"] == "pass"
            else "failed"
            if verification["verdict"] == "fail"
            else "review"
        )
        coverage["reason_codes"] = reason_codes
        coverage["verification_receipt"] = {
            "coverage_proof_sha256": coverage["proof_sha256"],
            "ordered_geometry_sha256": coverage["ordered_geometry_sha256"],
            "final_crop_sha256": coverage["final_crop_sha256"],
            "source_page_image_sha256": [
                page["source_page_image"]["sha256"] for page in coverage["source_pages"]
            ],
            "verification_sha256": canonical_sha256(verification),
            "verdict": verification["verdict"],
            "reason_codes": reason_codes,
            "verified_at": utc_now(),
        }
        verdict_counts[verification["verdict"]] += 1
        if verification["verdict"] == "pass":
            clearable = (
                "잉크가 크롭 경계",
                "너무 작은 bbox",
                "독립 문항 bbox가 크게 겹침",
                "부모 본문 bbox와 소문항",
                "형제 소문항 bbox 겹침",
            )
            item["review_reasons"] = [
                reason
                for reason in item["review_reasons"]
                if not any(marker in reason for marker in clearable)
            ]
            review = [
                queued
                for queued in review
                if not (
                    queued.get("item") == item["unit_id"]
                    and (
                        any(marker in queued.get("reason", "") for marker in clearable)
                        or bool(set(queued.get("reason_codes", [])) & REPAIR_REASON_CODES)
                    )
                )
            ]
        else:
            review.append(
                review_entry(
                    item["source_pages"][0] if item["source_pages"] else None,
                    item["unit_id"],
                    "VLM 검수 "
                    + verification["verdict"]
                    + ": "
                    + "; ".join(verification["reasons"]),
                    reason_codes,
                    {key: verification[key] for key in VERIFICATION_BOOLEAN_FIELDS},
                )
            )
    checked_ids = {result["unit_id"] for result in results}
    candidate_pages = {
        page
        for item in manifest["items"]
        if item["unit_id"] in checked_ids
        for page in item["source_pages"]
    }
    fully_passed_pages = {
        page
        for page in candidate_pages
        if all(
            item.get("verification", {}).get("verdict") == "pass"
            for item in manifest["items"]
            if page in item["source_pages"]
        )
    }
    review = [
        queued
        for queued in review
        if not (
            queued.get("item") is None
            and queued.get("page") in fully_passed_pages
            and "inline_answer_exposure" in queued.get("reason_codes", [])
        )
    ]
    unique_review: list[dict[str, Any]] = []
    seen_review: set[tuple[Any, Any, Any]] = set()
    for queued in review:
        key = (queued.get("page"), queued.get("item"), queued.get("reason"))
        if key not in seen_review:
            seen_review.add(key)
            unique_review.append(queued)
    review = unique_review
    manifest["verification"] = {
        "mode": mode,
        "checked": len(results),
        "verdict_counts": verdict_counts,
        "updated_at": utc_now(),
    }
    manifest["review_count"] = len(review)
    write_json(manifest_path, manifest)
    write_json(review_path, review)
    return manifest["verification"]


def repairable_review(item: dict[str, Any]) -> bool:
    reason_codes = item.get("reason_codes")
    if not isinstance(reason_codes, list) or any(
        not isinstance(code, str) for code in reason_codes
    ):
        return False
    selected = set(reason_codes) & REPAIR_REASON_CODES
    facts = item.get("facts")
    verification_codes = selected & VERIFICATION_REPAIR_CODES
    if verification_codes and (
        not isinstance(facts, dict)
        or any(facts.get(code) is not True for code in verification_codes)
    ):
        return False
    return bool(selected)


def repairable_pages(run_dir: Path) -> list[int]:
    review_path = run_dir / "review_queue.json"
    if not review_path.exists():
        return []
    review = json.loads(review_path.read_text(encoding="utf-8"))
    pages: set[int] = set()
    for item in review:
        if item.get("page") is None:
            continue
        if repairable_review(item):
            pages.add(int(item["page"]) - 1)
    return sorted(pages)


def repair_target_local_ids(
    run_dir: Path,
    page_index: int,
    review: list[dict[str, Any]],
    detection: dict[str, Any],
) -> set[str] | None:
    """Resolve repairable queue items to page-local question ids.

    None means the issue is page-wide or cannot be mapped safely, so the full
    repaired page remains the only usable fallback.
    """
    relevant = [
        item
        for item in review
        if item.get("page") == page_index + 1 and repairable_review(item)
    ]
    if not relevant or any(not item.get("item") for item in relevant):
        return None

    manifest_path = run_dir / "manifest.json"
    owner_by_item: dict[str, str] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in manifest.get("items", []):
            unit_id = item.get("unit_id")
            owner_id = item.get("owner_question_id")
            if unit_id and owner_id:
                owner_by_item[str(unit_id)] = str(owner_id)

    local_ids = {str(question["local_id"]) for question in detection["questions"]}
    page_prefix = f"p{page_index + 1:04d}-"
    owner_to_local = {
        page_prefix + safe_slug(local_id, 24): local_id for local_id in local_ids
    }
    continuations = [
        str(question["local_id"])
        for question in detection["questions"]
        if question["starts_on_previous_page"]
    ]
    targets: set[str] = set()
    for item in relevant:
        queued_id = str(item["item"])
        owner_id = owner_by_item.get(queued_id, queued_id)
        local_id = owner_to_local.get(owner_id)
        if local_id is None:
            candidate = owner_id[len(page_prefix) :] if owner_id.startswith(page_prefix) else owner_id
            local_id = candidate if candidate in local_ids else None
        if local_id is None and len(continuations) == 1:
            local_id = continuations[0]
        if local_id not in local_ids:
            return None
        targets.add(local_id)
    return targets or None


def merge_repaired_detection(
    current: dict[str, Any], repaired: dict[str, Any], target_local_ids: set[str]
) -> dict[str, Any]:
    """Replace only requested owner questions; keep every sibling byte-for-byte equivalent."""
    available = list(repaired["questions"])
    used: set[int] = set()
    questions: list[dict[str, Any]] = []
    for question in current["questions"]:
        if question["local_id"] not in target_local_ids:
            questions.append(question)
            continue
        match = next(
            (
                (index, candidate)
                for index, candidate in enumerate(available)
                if index not in used and candidate["local_id"] == question["local_id"]
            ),
            None,
        )
        if match is None and question["printed_number"]:
            matches = [
                (index, candidate)
                for index, candidate in enumerate(available)
                if index not in used and candidate["printed_number"] == question["printed_number"]
            ]
            match = matches[0] if len(matches) == 1 else None
        if match is None:
            questions.append(question)
            continue
        index, candidate = match
        used.add(index)
        replacement = dict(candidate)
        for field in (
            "local_id",
            "printed_number",
            "kind",
            "answer_type",
            "group_label",
            "starts_on_previous_page",
            "continues_next_page",
        ):
            replacement[field] = question[field]
        if len(candidate["subquestions"]) == len(question["subquestions"]):
            replacement["subquestions"] = [
                {
                    **incoming,
                    "label": previous["label"],
                    "answer_type": previous["answer_type"],
                }
                for previous, incoming in zip(question["subquestions"], candidate["subquestions"])
            ]
        else:
            replacement["subquestions"] = question["subquestions"]
        questions.append(replacement)
    merged = {**current, "questions": questions}
    errors = validate_detection(merged)
    if errors:
        raise ValueError("선택 문항 교정 결과가 유효하지 않음: " + "; ".join(errors))
    return merged


def next_repair_attempt(run_dir: Path, pages: list[int], minimum: int = 1) -> int:
    existing = [minimum - 1]
    for page_index in pages:
        page = page_index + 1
        paths = list((run_dir / "detections" / "repair").glob(f"page-{page:04d}-r*.json"))
        paths += list((run_dir / "detections" / "history").glob(f"page-{page:04d}-before-r*.json"))
        for path in paths:
            match = re.search(r"-r(\d+)(?:\.error)?\.json$", path.name)
            if match:
                existing.append(int(match.group(1)))
    return max(existing) + 1


def page_text_hint(pdf_path: Path, page_index: int) -> str:
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        page = document[page_index]
        try:
            return text_layer_info(page)[2]
        finally:
            page.close()
    finally:
        document.close()


def _repair_checkpoint(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "detections" / "repair" / "checkpoint.json"
    if not path.is_file():
        return {"version": 1, "pages": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "pages": {}}
    if value.get("version") != 1 or not isinstance(value.get("pages"), dict):
        return {"version": 1, "pages": {}}
    return value


REPAIR_PAYLOAD_FORMAT = "utf-8-json-v1"


def _backup_legacy_repair_checkpoint(path: Path) -> None:
    backup = path.with_name(path.name + ".legacy-lf-backup")
    inside(backup.resolve(), "output")
    if not backup.exists():
        shutil.copy2(path, backup)


def _legacy_promoted_payload_matches(path: Path, expected_sha256: Any) -> bool:
    return isinstance(expected_sha256, str) and sha256_bytes(
        path.read_bytes().replace(b"\r\n", b"\n")
    ) == expected_sha256


def _repair_relative_path(run_dir: Path, relative: str) -> Path:
    path = (run_dir / relative).resolve()
    if not path.is_relative_to(run_dir.resolve()):
        raise RuntimeError("경계 교정 체크포인트 경로가 실행 폴더를 벗어났습니다.")
    return path


def _repair_candidate(
    run_dir: Path,
    page_index: int,
    expected_meta: dict[str, Any],
    identity: dict[str, Any],
) -> Path | None:
    repair_dir = run_dir / "detections" / "repair"
    candidates = sorted(
        (
            path
            for path in repair_dir.glob(f"page-{page_index + 1:04d}-r*.json")
            if re.fullmatch(rf"page-{page_index + 1:04d}-r\d+\.json", path.name)
        ),
        reverse=True,
    )
    for path in candidates:
        try:
            wrapper = load_detection(path)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
        meta = wrapper.get("meta")
        if not isinstance(meta, dict) or not all(meta.get(key) == value for key, value in expected_meta.items()):
            continue
        recorded_identity = meta.get("repair_identity_sha256")
        if recorded_identity is not None:
            if recorded_identity == identity["identity_sha256"]:
                return path
            continue
        image_hashes = [item["sha256"] for item in identity["attachments"]]
        receipts = sorted(path.parent.glob(path.stem + ".attempt-*.last.runtime.json"))
        for receipt_path in receipts:
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                receipt.get("status") == "succeeded"
                and receipt.get("terminal_completed") is True
                and receipt.get("selected_model") == identity["model"]
                and receipt.get("selected_effort") == identity["reasoning_effort"]
                and receipt.get("prompt_sha256") == identity["prompt_sha256"]
                and receipt.get("schema_sha256") == identity["runtime_schema_sha256"]
                and receipt.get("image_sha256") == image_hashes
            ):
                return path
    return None


def _attachment_receipt(run_dir: Path, attachments: list[Path]) -> list[dict[str, str]]:
    return [
        {
            "path": str(path.resolve().relative_to(run_dir.resolve())).replace(os.sep, "/"),
            "sha256": sha256_file(path),
        }
        for path in attachments
    ]


class BoundaryRepairError(RuntimeError):
    def __init__(self, failed_pages: list[int], completed: int, total: int, preserved: dict[str, int | bool]) -> None:
        self.stage = "boundary_repair"
        self.failed_pages = sorted(page + 1 for page in failed_pages)
        self.completed = completed
        self.total = total
        self.error_code = "boundary_repair_failed"
        self.preserved = preserved
        pages = ", ".join(map(str, self.failed_pages))
        super().__init__(f"경계 교정 실패 · {pages}쪽 / {total}쪽 중 {completed}쪽 교정 결과 보존")


def repair_detection_pages(
    args: argparse.Namespace,
    pdf_path: Path,
    run_dir: Path,
    pages: list[int],
    attempt_number: int,
    *,
    audit_only: bool = False,
) -> int | dict[str, Any]:
    if not pages:
        return 0
    state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
    total_pages = int(state["total_pages"])
    source_sha = state["source_sha256"]
    if sha256_file(pdf_path) != source_sha:
        raise RuntimeError("원본 PDF 해시가 실행 기록과 일치하지 않습니다.")
    rotations = {
        int(page_number) - 1: int(rotation)
        for page_number, rotation in state.get("rotations", {}).items()
    }
    template_text = PROMPT_PATH.read_text(encoding="utf-8")
    review = json.loads((run_dir / "review_queue.json").read_text(encoding="utf-8"))
    jobs: list[dict[str, Any]] = []
    repair_paths: dict[int, Path] = {}
    identities: dict[int, dict[str, Any]] = {}
    checkpoint = _repair_checkpoint(run_dir)
    checkpoint_path = run_dir / "detections" / "repair" / "checkpoint.json"
    page_receipts = checkpoint["pages"]
    reused: set[int] = set()
    promoted: set[int] = set()
    legacy_normalized: set[int] = set()
    for page_index in pages:
        current_path = detection_path(run_dir, page_index)
        prior = page_receipts.get(str(page_index + 1))
        current_source = current_path
        prior_candidate: Path | None = None
        if isinstance(prior, dict) and prior.get("state") in {"promoting", "promoted"}:
            relative_candidate = prior.get("candidate")
            relative_history = prior.get("history")
            if not isinstance(relative_candidate, str) or not isinstance(relative_history, str):
                raise RuntimeError("경계 교정 체크포인트가 불완전합니다.")
            prior_candidate = _repair_relative_path(run_dir, relative_candidate)
            history_path = _repair_relative_path(run_dir, relative_history)
            if (
                not prior_candidate.is_file()
                or not history_path.is_file()
                or sha256_file(prior_candidate) != prior.get("candidate_sha256")
                or sha256_file(history_path) != prior.get("base_detection_sha256")
            ):
                raise RuntimeError("경계 교정 체크포인트 해시가 일치하지 않습니다.")
            current_sha = sha256_file(current_path)
            candidate_attempt = int(re.search(r"-r(\d+)\.json$", prior_candidate.name).group(1))
            repair_pass = prior.get("repair_pass", candidate_attempt)
            same_pass = type(repair_pass) is int and repair_pass == attempt_number
            if same_pass and current_sha != prior.get("promoted_sha256") and (
                prior.get("promoted_payload_format") is None
                and _legacy_promoted_payload_matches(current_path, prior.get("promoted_sha256"))
            ):
                prior.update(
                    promoted_sha256=current_sha,
                    promoted_payload_format=REPAIR_PAYLOAD_FORMAT,
                )
                if not audit_only:
                    _backup_legacy_repair_checkpoint(checkpoint_path)
                    checkpoint.update(pages=page_receipts, updated_at=utc_now())
                    write_json(checkpoint_path, checkpoint)
                legacy_normalized.add(page_index)
            if same_pass and current_sha == prior.get("promoted_sha256"):
                repair_paths[page_index] = prior_candidate
                identities[page_index] = prior
                reused.add(page_index)
                promoted.add(page_index)
                if prior.get("state") == "promoting" and not audit_only:
                    promoted_detection = load_detection(current_path)
                    overview = page_image_path(
                        run_dir / "pages", page_index, int(state["vision_dpi"]),
                        int(rotations.get(page_index, 0)),
                    )
                    create_overlay(
                        overview, promoted_detection["result"],
                        run_dir / "overlays" / f"page-{page_index + 1:04d}.jpg",
                        args.confidence_threshold,
                    )
                    prior["state"] = "promoted"
                    checkpoint.update(pages=page_receipts, updated_at=utc_now())
                    write_json(checkpoint_path, checkpoint)
                continue
            if same_pass and current_sha != prior.get("base_detection_sha256"):
                raise RuntimeError("경계 교정 적용 상태를 확인할 수 없습니다.")
            if same_pass:
                current_source = history_path
        current = load_detection(current_source)
        repair_items = [
            item
            for item in review
            if item.get("page") == page_index + 1 and repairable_review(item)
        ]
        repair_unit_ids = {
            str(item["item"]) for item in repair_items if item.get("item")
        }
        context_pages = int(current["meta"].get("context_pages", 0))
        attachments, attachment_map = attachment_set(
            run_dir,
            page_index,
            total_pages,
            int(state["vision_dpi"]),
            state["tiles"],
            context_pages,
            rotations,
        )
        overlay = run_dir / "overlays" / f"page-{page_index + 1:04d}.jpg"
        attachments.append(overlay)
        attachment_map += (
            f"\n{len(attachments)}. PREVIOUS OVERLAY for PRIMARY: green=main question, "
            "blue=shared context, magenta=subquestion, red=excluded solution. "
            "It is diagnostic only; return coordinates in the clean PRIMARY overview space."
        )
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in manifest.get("items", []):
                verification = item.get("verification")
                if (
                    page_index + 1 not in item.get("source_pages", [])
                    or not isinstance(verification, dict)
                    or verification.get("verdict") != "fail"
                    or item.get("unit_id") not in repair_unit_ids
                ):
                    continue
                failed_crop = run_dir / str(item.get("output_file") or "")
                if not failed_crop.is_file():
                    continue
                attachments.append(failed_crop)
                attachment_map += (
                    f"\n{len(attachments)}. FAILED FINAL CROP for {item.get('unit_id')}: "
                    "diagnostic evidence of what was clipped or contaminated; return "
                    "coordinates in the clean PRIMARY overview space."
                )
        column_note = ""
        if current["result"].get("layout") == "two_columns":
            document = pdfium.PdfDocument(str(pdf_path))
            cache = PageImageCache(
                document,
                int(state["vision_dpi"]),
                rotations,
            )
            try:
                split = cache.column_split(page_index)
            finally:
                cache.close()
                document.close()
            column_note = (
                f"\nThe detected physical column split is x={split}/1000. "
                "A right-column identifier and its required parts must be placed on the "
                "right side unless a visible figure or line truly crosses the gutter; "
                "apply the same coordinate frame to the identifier, stem, figure, and subquestions.\n"
            )
        issues = [
            f"[{','.join(item['reason_codes'])}] {item['reason']}"
            for item in repair_items
        ]
        base_prompt = build_prompt(
            template_text,
            pdf_path,
            page_index,
            total_pages,
            attachment_map,
            page_text_hint(pdf_path, page_index),
        )
        repair_prompt = (
            base_prompt
            + "\n\n# Boundary Correction Pass\n\n"
            + "The previous result is structurally plausible but failed deterministic crop QA. "
            "Return a complete replacement JSON object, not a patch. Inspect the clean overview and zoom tiles again. "
            "When QA says ink touches right/bottom/left/top, expand or reposition the responsible bbox until the full sentence, "
            "figure, 보기 statements, and ①~⑤ choices have visible whitespace margin. Trim content from the opposite column. "
            "A VLM failure may identify content beyond a large internal whitespace gap; inspect the whole question to its semantic end, "
            "not merely to the first blank band. Preserve every ㄱ/ㄴ/ㄷ statement and all answer choices. "
            "Do not assume a nominal column width; follow the actual longest text line. The overlay shows the prior error only.\n"
            + column_note
            + "\n"
            + "QA failures:\n- "
            + "\n- ".join(issues)
            + "\n\nPrevious JSON:\n"
            + json.dumps(current["result"], ensure_ascii=False)
        )
        attachment_receipt = _attachment_receipt(run_dir, attachments)
        identity = {
            "source_sha256": source_sha,
            "page": page_index + 1,
            "repair_pass": attempt_number,
            "base_detection_sha256": sha256_file(current_source),
            "prompt_sha256": sha256_text(repair_prompt),
            "attachments": attachment_receipt,
            "model": args.model,
            "reasoning_effort": args.repair_reasoning_effort,
            "schema_sha256": sha256_file(SCHEMA_PATH),
            "runtime_schema_sha256": sha256_text(json.dumps(
                json.loads(SCHEMA_PATH.read_text(encoding="utf-8")),
                ensure_ascii=False,
                sort_keys=True,
            )),
        }
        identity["identity_sha256"] = canonical_sha256(identity)
        identities[page_index] = identity
        expected_meta = {
            "source_sha256": source_sha,
            "page": page_index + 1,
            "model": args.model,
            "repair_reasoning_effort": args.repair_reasoning_effort,
            "schema_sha256": identity["schema_sha256"],
            "prompt_template_sha256": sha256_text(template_text),
        }
        candidate: Path | None = None
        if isinstance(prior, dict) and prior.get("identity_sha256") == identity["identity_sha256"]:
            relative = prior.get("candidate")
            if isinstance(relative, str):
                proposed = _repair_relative_path(run_dir, relative)
                if proposed.is_file() and sha256_file(proposed) == prior.get("candidate_sha256"):
                    candidate = proposed
        if candidate is None:
            candidate = _repair_candidate(run_dir, page_index, expected_meta, identity)
        if candidate is not None:
            repair_paths[page_index] = candidate
            reused.add(page_index)
            if page_index not in promoted:
                page_receipts[str(page_index + 1)] = {
                    **identity,
                    "candidate": str(candidate.relative_to(run_dir)).replace(os.sep, "/"),
                    "candidate_sha256": sha256_file(candidate),
                    "repair_pass": attempt_number,
                    "state": "candidate",
                }
            continue
        page_attempt = next_repair_attempt(run_dir, [page_index], attempt_number)
        repair_path = run_dir / "detections" / "repair" / f"page-{page_index + 1:04d}-r{page_attempt}.json"
        repair_paths[page_index] = repair_path
        meta = {
            **current["meta"],
            "repair_attempt": page_attempt,
            "repair_reasoning_effort": args.repair_reasoning_effort,
            "repair_created_at": utc_now(),
            "repair_identity_sha256": identity["identity_sha256"],
            "repair_prompt_sha256": identity["prompt_sha256"],
            "repair_attachments_sha256": canonical_sha256(attachment_receipt),
        }
        jobs.append(
            {
                "page_index": page_index,
                "output": repair_path,
                "log_dir": run_dir / "logs",
                "run_dir": run_dir,
                "attachments": attachments,
                "prompt": repair_prompt,
                "meta": meta,
                "context_pages": context_pages,
                "model": args.model,
                "reasoning_effort": args.repair_reasoning_effort,
                "timeout": args.timeout,
                "retries": args.retries,
                "force": False,
            }
        )
    if audit_only:
        return {
            "source_sha256": source_sha,
            "base_detections": len(list((run_dir / "detections").glob("page-????.json"))),
            "requested_pages": [page + 1 for page in pages],
            "reused_pages": sorted(page + 1 for page in reused),
            "pending_pages": [job["page_index"] + 1 for job in jobs],
            "candidates": {
                str(page + 1): {
                    "identity_sha256": identities[page]["identity_sha256"],
                    "candidate_sha256": page_receipts[str(page + 1)]["candidate_sha256"],
                    "attachment_count": len(identities[page]["attachments"]),
                }
                for page in reused
            },
            "provider_calls": 0,
            "legacy_normalized_pages": sorted(page + 1 for page in legacy_normalized),
        }
    checkpoint.update(source_sha256=source_sha, pages=page_receipts, updated_at=utc_now())
    write_json(checkpoint_path, checkpoint)
    pending = [job["page_index"] for job in jobs]
    print(f"경계 교정 재사용 {len(reused)}개 · 실행 {', '.join(str(page + 1) for page in pending) or '0개'}")
    results = execute_jobs(jobs, args.workers)
    for result in results:
        if result["status"] in {"ok", "cached"}:
            page_index = result["page"]
            candidate = repair_paths[page_index]
            page_receipts[str(page_index + 1)] = {
                **identities[page_index],
                "candidate": str(candidate.relative_to(run_dir)).replace(os.sep, "/"),
                "candidate_sha256": sha256_file(candidate),
                "state": "candidate",
            }
    checkpoint.update(pages=page_receipts, updated_at=utc_now())
    write_json(checkpoint_path, checkpoint)
    errors = [result for result in results if result["status"] == "error"]
    if errors:
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        preserved = {
            "base_detections": len(list((run_dir / "detections").glob("page-????.json"))),
            "repair_results": len(reused) + len(results) - len(errors),
            "preliminary_crops": int(manifest.get("crop_count") or 0),
            "registered": False,
        }
        raise BoundaryRepairError([result["page"] for result in errors], len(pages) - len(errors), len(pages), preserved)
    history = run_dir / "detections" / "history"
    inside(history.resolve(), "output")
    history.mkdir(parents=True, exist_ok=True)
    for page_index in pages:
        current_path = detection_path(run_dir, page_index)
        candidate = repair_paths[page_index]
        candidate_sha = sha256_file(candidate)
        receipt = page_receipts[str(page_index + 1)]
        if page_index in promoted:
            continue
        candidate_attempt = int(re.search(r"-r(\d+)\.json$", candidate.name).group(1))
        history_path = history / f"page-{page_index + 1:04d}-before-r{candidate_attempt}.json"
        if history_path.exists():
            if sha256_file(history_path) != receipt["base_detection_sha256"]:
                raise RuntimeError("경계 교정 이력 해시가 일치하지 않습니다.")
        else:
            if sha256_file(current_path) != receipt["base_detection_sha256"]:
                raise RuntimeError("경계 교정 원본 해시가 변경되었습니다.")
            shutil.copy2(current_path, history_path)
        current = load_detection(current_path)
        repaired = load_detection(candidate)
        target_local_ids = repair_target_local_ids(
            run_dir, page_index, review, current["result"]
        )
        if target_local_ids is not None:
            repaired = {
                **repaired,
                "result": merge_repaired_detection(
                    current["result"], repaired["result"], target_local_ids
                ),
            }
        promoted_payload = json_bytes(repaired)
        receipt["state"] = "promoting"
        receipt["promoted_sha256"] = sha256_bytes(promoted_payload)
        receipt["promoted_payload_format"] = REPAIR_PAYLOAD_FORMAT
        receipt["repair_pass"] = attempt_number
        receipt["history"] = str(history_path.relative_to(run_dir)).replace(os.sep, "/")
        checkpoint.update(pages=page_receipts, updated_at=utc_now())
        write_json(checkpoint_path, checkpoint)
        write_json(current_path, repaired)
        overview = page_image_path(
            run_dir / "pages",
            page_index,
            int(state["vision_dpi"]),
            int(rotations.get(page_index, 0)),
        )
        create_overlay(
            overview,
            repaired["result"],
            run_dir / "overlays" / f"page-{page_index + 1:04d}.jpg",
            args.confidence_threshold,
        )
        receipt["state"] = "promoted"
        checkpoint.update(pages=page_receipts, updated_at=utc_now())
        write_json(checkpoint_path, checkpoint)
    return 0


def cache_matches(path: Path, expected: dict[str, Any], requested_context: int) -> bool:
    if not path.exists():
        return False
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        if validate_detection(wrapper["result"]):
            return False
        meta = wrapper["meta"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return False
    for key, value in expected.items():
        if key == "context_pages":
            continue
        if meta.get(key) != value:
            return False
    return int(meta.get("context_pages", 0)) >= requested_context


def run_codex_detection(job: dict[str, Any]) -> dict[str, Any]:
    output: Path = job["output"]
    expected_meta = job["meta"]
    if not job["force"] and cache_matches(output, expected_meta, job["context_pages"]):
        return {"page": job["page_index"], "status": "cached", "output": str(output)}
    try:
        inside(output.resolve(), "output")
        output.parent.mkdir(parents=True, exist_ok=True)
        log_dir: Path = job["log_dir"]
        inside(log_dir.resolve(), "output")
        log_dir.mkdir(parents=True, exist_ok=True)
    except IsolationError as exc:
        try:
            print('ASTRA_INTERNAL_DIAGNOSTIC ' + json.dumps({
                'crop_id': job['run_dir'].parent.parent.name,
                'page': job['page_index'] + 1, 'stage': 'detection_prepare',
                'setting': exc.setting, 'reason': exc.reason,
                'raw': exc.path.raw if exc.path else None,
                'resolved': exc.path.resolved if exc.path else None,
                'root': exc.path.root if exc.path else None,
                'requested_output': str(output), 'requested_log_dir': str(job.get('log_dir')),
                'traceback': ''.join(traceback.format_tb(exc.__traceback__)),
            }), file=sys.stderr, flush=True)
        except Exception:  # Diagnostic output must never replace the original exception.
            pass
        raise
    last_error = "unknown error"
    for attempt in range(job["retries"] + 1):
        last_message = output.with_name(output.stem + f".attempt-{attempt + 1}.last.tmp")
        if last_message.exists():
            last_message.unlink()
        try:
            command = codex_exec_command(
                SCHEMA_PATH,
                last_message,
                job["reasoning_effort"],
                job.get("model"),
                job["attachments"],
            )
        except FileNotFoundError as exc:
            return {"page": job["page_index"], "status": "error", "error": str(exc)}
        retry_note = ""
        if attempt:
            retry_note = (
                "\n\nPrevious attempt failed validation. Recheck every required field and return one complete "
                f"schema-compliant JSON object. Validation error: {last_error[:1000]}"
            )
        started = time.monotonic()
        try:
            process = run_codex_process(
                command, job["prompt"] + retry_note, job["timeout"]
            )
            elapsed = round(time.monotonic() - started, 2)
            stem = f"page-{job['page_index'] + 1:04d}-attempt-{attempt + 1}"
            (log_dir / f"{stem}.events.jsonl").write_text(process.stdout, encoding="utf-8")
            (log_dir / f"{stem}.stderr.log").write_text(
                f"exit_code={process.returncode}\nelapsed_seconds={elapsed}\n{process.stderr}",
                encoding="utf-8",
            )
            if process.returncode != 0:
                last_error = f"codex exit {process.returncode}: {process.stderr[-2000:]}"
                continue
            if not last_message.exists():
                last_error = "Codex가 --output-last-message 파일을 만들지 않음"
                continue
            data = extract_json_message(last_message.read_text(encoding="utf-8"))
            errors = validate_detection(data)
            if errors:
                last_error = "; ".join(errors)
                continue
            wrapper = {
                "meta": {**expected_meta, "context_pages": job["context_pages"], "created_at": utc_now()},
                "result": data,
            }
            write_json(output, wrapper)
            try:
                last_message.unlink()
            except OSError:
                pass
            return {"page": job["page_index"], "status": "ok", "output": str(output)}
        except subprocess.TimeoutExpired as exc:
            last_error = f"timeout after {job['timeout']}s: {exc}"
        except (OSError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    error_path = output.with_suffix(".error.json")
    write_json(
        error_path,
        {"page": job["page_index"] + 1, "error": last_error, "failed_at": utc_now()},
    )
    return {"page": job["page_index"], "status": "error", "error": last_error}


def load_detection(path: Path) -> dict[str, Any]:
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    errors = validate_detection(wrapper["result"])
    if errors:
        raise ValueError(f"{path}: {'; '.join(errors)}")
    return wrapper


def normalized_to_pixels(bbox: list[int], width: int, height: int, padding: int = 0) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = bbox
    return (
        max(0, math.floor((x0 - padding) * width / 1000)),
        max(0, math.floor((y0 - padding) * height / 1000)),
        min(width, math.ceil((x1 + padding) * width / 1000)),
        min(height, math.ceil((y1 + padding) * height / 1000)),
    )


def padded_bbox_without_neighbors(
    bbox: list[int],
    padding: int,
    owner: str | None,
    forbidden: Iterable[tuple[str, list[int]]],
) -> list[int]:
    x0, y0, x1, y1 = bbox
    padded = [max(0, x0 - padding), max(0, y0 - padding), min(1000, x1 + padding), min(1000, y1 + padding)]
    if owner is None:
        return padded
    width, height = x1 - x0, y1 - y0
    for other_owner, other in forbidden:
        if bbox_iou(bbox, other) > 0:
            continue
        ox0, oy0, ox1, oy1 = other
        horizontal_overlap = max(0, min(x1, ox1) - max(x0, ox0)) / max(1, min(width, ox1 - ox0))
        vertical_overlap = max(0, min(y1, oy1) - max(y0, oy0)) / max(1, min(height, oy1 - oy0))
        if horizontal_overlap >= 0.25:
            if y1 <= oy0 < padded[3]:
                padded[3] = min(padded[3], (y1 + oy0) // 2)
            if padded[1] < oy1 <= y0:
                padded[1] = max(padded[1], (oy1 + y0) // 2)
        if vertical_overlap >= 0.25:
            if x1 <= ox0 < padded[2]:
                padded[2] = min(padded[2], max(x1, ox0 - 1))
            if padded[0] < ox1 <= x0:
                padded[0] = max(padded[0], min(x0, ox1 + 1))
    return padded


def constrain_bbox_to_column(
    safe_bbox: list[int], bbox: list[int], column: str | None, split: int
) -> tuple[list[int], int]:
    safe = list(safe_bbox)
    x0, _, x1, _ = bbox
    right_limit = 1000
    if column == "left" and x0 < split and x1 <= split + 120:
        right_limit = min(1000, max(x1, split - 6))
        safe[2] = min(safe[2], right_limit)
    elif column == "right" and x1 > split and x0 >= split - 120:
        safe[0] = max(safe[0], min(x0, split + 6))
    return safe, right_limit


def constrain_bbox_before_ignored_regions(
    safe_bbox: list[int],
    bbox: list[int],
    ignored_regions: Iterable[list[int]],
) -> tuple[list[int], int]:
    safe = list(safe_bbox)
    x0, y0, x1, y1 = bbox
    bottom_limit = 1000
    for region in ignored_regions:
        rx0, ry0, rx1, _ = region
        horizontal_overlap = max(0, min(x1, rx1) - max(x0, rx0)) / max(
            1, min(x1 - x0, rx1 - rx0)
        )
        if horizontal_overlap >= 0.25 and y1 <= ry0:
            bottom_limit = min(bottom_limit, max(y1, ry0 - 4))
    safe[3] = min(safe[3], bottom_limit)
    return safe, bottom_limit


def create_overlay(
    overview: Path,
    detection: dict[str, Any],
    output: Path,
    confidence_threshold: float,
) -> None:
    inside(output.resolve(), "output")
    output.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(overview) as source:
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    colors = {"context": "#1473E6", "main": "#18A558", "sub": "#C026D3", "ignored": "#777777"}
    for question_index, question in enumerate(detection["questions"], 1):
        low = question["confidence"] < confidence_threshold
        main_color = "#F59E0B" if low else colors["main"]
        groups = [
            (question["shared_context_parts"], colors["context"], f"C{question_index}"),
            (question["main_parts"], main_color, f"Q{question_index}"),
        ]
        for sub_index, subquestion in enumerate(question["subquestions"], 1):
            groups.append((subquestion["parts"], colors["sub"], f"Q{question_index}.{sub_index}"))
        for parts, color, label in groups:
            for part in parts:
                if part["page_offset"] != 0:
                    continue
                box = normalized_to_pixels(part["bbox"], width, height)
                draw.rectangle(box, outline=color, width=max(2, width // 600))
                text_position = (box[0] + 3, max(0, box[1] - 14))
                draw.rectangle(
                    (text_position[0], text_position[1], text_position[0] + 42, text_position[1] + 13),
                    fill=color,
                )
                draw.text(text_position, label, fill="white")
    for region in detection["ignored_regions"]:
        box = normalized_to_pixels(region["bbox"], width, height)
        is_solution = region["role"] == "solution_content"
        color = "#DC2626" if is_solution else colors["ignored"]
        draw.rectangle(box, outline=color, width=3 if is_solution else 1)
        if is_solution:
            draw.text((box[0] + 2, box[1] + 2), "MASK", fill=color)
    draw.rectangle((0, 0, min(width, 360), 20), fill="white")
    draw.text((4, 4), f"{detection['page_type']} / {detection['layout']}", fill="black")
    image.save(output, format="JPEG", quality=92, subsampling=0)
    image.close()


def bbox_iou(first: list[int], second: list[int]) -> float:
    ax0, ay0, ax1, ay1 = first
    bx0, by0, bx1, by1 = second
    intersection = max(0, min(ax1, bx1) - max(ax0, bx0)) * max(0, min(ay1, by1) - max(ay0, by0))
    if not intersection:
        return 0.0
    first_area = (ax1 - ax0) * (ay1 - ay0)
    second_area = (bx1 - bx0) * (by1 - by0)
    return intersection / (first_area + second_area - intersection)


def bbox_overlap_of_smaller(first: list[int], second: list[int]) -> float:
    ax0, ay0, ax1, ay1 = first
    bx0, by0, bx1, by1 = second
    intersection = max(0, min(ax1, bx1) - max(ax0, bx0)) * max(0, min(ay1, by1) - max(ay0, by0))
    smaller = min((ax1 - ax0) * (ay1 - ay0), (bx1 - bx0) * (by1 - by0))
    return intersection / max(1, smaller)


def page_quality_issues(
    detection: dict[str, Any], page_index: int, total_pages: int, threshold: float
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    def add(reason: str, reason_code: str, item: str | None = None) -> None:
        issues.append(
            review_entry(page_index + 1, item, reason, [reason_code])
        )

    if detection["page_type"] in PROBLEM_PAGE_TYPES and not detection["questions"]:
        add("문제 페이지로 분류됐지만 검출 문항이 없음", "missing_first_pass_owner")
    if detection["page_type"] in NON_PROBLEM_PAGE_TYPES and detection["questions"]:
        add("비문제 페이지에 문항이 검출됨", "unexpected_first_pass_owner")
    if detection["page_type"] == "problem_with_inline_solution":
        add(
            "문제와 정답·해설이 같은 페이지에 있어 정답 유입 검수가 필요함",
            "inline_answer_exposure",
        )
    for warning in detection["warnings"]:
        add(f"모델 경고: {warning}", "detector_warning")
    primary_boxes: list[tuple[str, list[int]]] = []
    for question in detection["questions"]:
        item = question["local_id"]
        if question["confidence"] < threshold:
            add(f"낮은 문항 신뢰도 {question['confidence']:.2f}", "low_detection_confidence", item)
        if question["starts_on_previous_page"] and page_index == 0:
            add("첫 페이지 문항이 이전 페이지에서 시작한다고 표시됨", "continuation_source_absent", item)
        if question["continues_next_page"] and page_index + 1 == total_pages:
            add("마지막 페이지 문항이 다음 페이지로 계속된다고 표시됨", "continuation_target_absent", item)
        for subquestion in question["subquestions"]:
            if subquestion["confidence"] < threshold:
                add(f"낮은 소문항 신뢰도 {subquestion['confidence']:.2f}", "low_detection_confidence", item)
        main_boxes = [part["bbox"] for part in question["main_parts"] if part["page_offset"] == 0]
        sub_boxes = [
            (subquestion["label"], part["bbox"])
            for subquestion in question["subquestions"]
            for part in subquestion["parts"]
            if part["page_offset"] == 0
        ]
        for main_box in main_boxes:
            for label, sub_box in sub_boxes:
                if bbox_overlap_of_smaller(main_box, sub_box) > 0.12:
                    add(f"부모 본문 bbox와 소문항 {label} bbox가 겹침", "owner_geometry_overlap", item)
        for sub_index, (first_label, first_box) in enumerate(sub_boxes):
            for second_label, second_box in sub_boxes[sub_index + 1 :]:
                if first_label != second_label and bbox_overlap_of_smaller(first_box, second_box) > 0.12:
                    add(f"형제 소문항 bbox 겹침: {first_label}, {second_label}", "owner_geometry_overlap", item)
        boxes = [
            part["bbox"]
            for part in question["main_parts"]
            if part["page_offset"] == 0
        ] + [
            part["bbox"]
            for subquestion in question["subquestions"]
            for part in subquestion["parts"]
            if part["page_offset"] == 0
        ]
        for box in boxes:
            if box[2] - box[0] < 20 or box[3] - box[1] < 12:
                add(f"너무 작은 bbox {box}", "owner_bbox_invalid", item)
            primary_boxes.append((item, box))
    for index, (first_id, first_box) in enumerate(primary_boxes):
        for second_id, second_box in primary_boxes[index + 1 :]:
            if first_id != second_id and bbox_iou(first_box, second_box) > 0.35:
                reason = f"독립 문항 bbox가 크게 겹침: {first_id}, {second_id}"
                add(reason, "owner_geometry_overlap", first_id)
                add(reason, "owner_geometry_overlap", second_id)
    return issues


def build_source_inventory(
    run_dir: Path,
    state: dict[str, Any],
    detections: list[tuple[int, dict[str, Any]]],
    manifest_items: list[dict[str, Any]],
    review: list[dict[str, Any]],
) -> dict[str, Any]:
    detection_by_page = {page_index + 1: detection for page_index, detection in detections}
    triage_pages: list[dict[str, Any]] = []
    triage = state.get("triage")
    if isinstance(triage, dict) and isinstance(triage.get("pages"), list):
        triage_pages = triage["pages"]
    else:
        triage_path = run_dir / "triage.json"
        if triage_path.is_file():
            triage_data = json.loads(triage_path.read_text(encoding="utf-8"))
            if isinstance(triage_data.get("pages"), list):
                triage_pages = triage_data["pages"]
    triage_by_page = {int(item["page_number"]): item for item in triage_pages}
    processed = {int(page) for page in state.get("processed_pages", [])}
    page_numbers = processed | set(detection_by_page) | set(triage_by_page)
    page_numbers.update(
        int(page)
        for item in manifest_items
        for page in item.get("source_pages", [])
    )
    referenced = {
        int(page)
        for item in manifest_items
        for page in item.get("source_pages", [])
    }
    rotations = {
        int(page): int(rotation) for page, rotation in state.get("rotations", {}).items()
    }
    dpi = int(state.get("vision_dpi", 200))
    pages: list[dict[str, Any]] = []
    for page_number in sorted(page_numbers):
        detection = detection_by_page.get(page_number)
        triage_item = triage_by_page.get(page_number)
        rotation = rotations.get(
            page_number,
            int(triage_item.get("rotation_correction", 0)) if triage_item else 0,
        )
        source_path = page_image_path(run_dir / "pages", page_number - 1, dpi, rotation)
        source_page_image = (
            {"path": str(source_path), "sha256": sha256_file(source_path)}
            if source_path.is_file()
            else None
        )
        unresolved = [
            {
                **entry,
                "reason_codes": entry.get("reason_codes", ["legacy_unresolved"]),
            }
            for entry in review
            if entry.get("page") == page_number
        ]
        if page_number in processed | referenced and source_page_image is None:
            unresolved.append(
                review_entry(
                    page_number,
                    None,
                    "검증에 필요한 현재 원문 페이지 이미지가 없음",
                    ["source_page_image_absent"],
                )
            )
        detected_owners = (
            [
                f"p{page_number:04d}-{safe_slug(str(question['local_id']), 24)}"
                for question in detection["questions"]
            ]
            if detection
            else []
        )
        outputs = [
            {
                "unit_id": item.get("unit_id"),
                "owner_question_id": item.get("owner_question_id"),
                "final_crop_sha256": item.get("output_sha256"),
            }
            for item in manifest_items
            if page_number in item.get("source_pages", [])
        ]
        pages.append(
            {
                "page": page_number,
                "triage_disposition": (
                    "selected"
                    if page_number in processed
                    else "context"
                    if page_number in referenced
                    else "excluded"
                ),
                "triage": triage_item,
                "rotation": rotation,
                "layout": detection.get("layout", "unknown") if detection else "unknown",
                "footer_regions": [
                    list(region["bbox"])
                    for region in detection.get("ignored_regions", [])
                    if region.get("role") == "footer"
                ]
                if detection
                else [],
                "source_page_image": source_page_image,
                "detected_owners": detected_owners,
                "final_outputs": outputs,
                "unresolved": unresolved,
            }
        )
    return {"source_pdf_sha256": state.get("source_sha256"), "pages": pages}


def detection_path(run_dir: Path, page_index: int) -> Path:
    return run_dir / "detections" / f"page-{page_index + 1:04d}.json"


def make_detection_jobs(
    args: argparse.Namespace,
    pdf_path: Path,
    run_dir: Path,
    source_sha: str,
    pages: list[int],
    total_pages: int,
    text_hints: dict[int, str],
    context_pages: int,
    force: bool,
    rotations: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    # Prepare shared parents before workers race Windows missing-path resolution.
    for directory in (run_dir / "detections", run_dir / "logs"):
        directory = inside(directory, "output")
        directory.mkdir(parents=True, exist_ok=True)
        inside(directory, "output")
    template_text = PROMPT_PATH.read_text(encoding="utf-8")
    template_sha = sha256_text(template_text)
    schema_sha = sha256_file(SCHEMA_PATH)
    jobs: list[dict[str, Any]] = []
    rotations = rotations or {}
    for page_index in pages:
        attachments, attachment_map = attachment_set(
            run_dir,
            page_index,
            total_pages,
            args.vision_dpi,
            args.tiles,
            context_pages,
            rotations,
        )
        prompt = build_prompt(
            template_text,
            pdf_path,
            page_index,
            total_pages,
            attachment_map,
            text_hints.get(page_index, "(텍스트 힌트 없음)"),
        )
        meta = {
            "pipeline_version": VERSION,
            "source_pdf": str(pdf_path),
            "source_sha256": source_sha,
            "page": page_index + 1,
            "vision_dpi": args.vision_dpi,
            "tiles": args.tiles,
            "context_pages": context_pages,
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "rotation_correction": int(rotations.get(page_index, 0)),
            "prompt_template_sha256": template_sha,
            "schema_sha256": schema_sha,
        }
        jobs.append(
            {
                "page_index": page_index,
                "output": detection_path(run_dir, page_index),
                "log_dir": run_dir / "logs",
                "run_dir": run_dir,
                "attachments": attachments,
                "prompt": prompt,
                "meta": meta,
                "context_pages": context_pages,
                "model": args.model,
                "reasoning_effort": args.reasoning_effort,
                "timeout": args.timeout,
                "retries": args.retries,
                "force": force,
            }
        )
    return jobs


class PageDetectionError(RuntimeError):
    def __init__(self, results: list[dict[str, Any]], pages: list[int]) -> None:
        latest = {result['page']: result for result in results}
        self.failed_pages = sorted(page + 1 for page in set(pages)
                                   if latest.get(page, {}).get('status') not in {'ok', 'cached'})
        self.total = len(set(pages))
        self.completed = self.total - len(self.failed_pages)
        self.stage = 'detection'
        self.error_code = ('output_path_rejected' if any(
            item.get('error_code') == 'output_path_rejected' for item in results
        ) else 'page_detection_failed')
        action = '저장 경로 확인이 필요합니다.' if self.error_code == 'output_path_rejected' else '작업 로그를 확인해 주세요.'
        super().__init__(f"{', '.join(map(str, self.failed_pages))}쪽 분석 실패. "
                         f"{self.total}쪽 중 {self.completed}쪽 결과 보존. {action}")


def execute_jobs(jobs: list[dict[str, Any]], workers: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(run_codex_detection, job): job for job in jobs}
        for done, future in enumerate(as_completed(futures), 1):
            job = futures[future]
            try:
                result = future.result()
            except CancelledError:
                raise
            except Exception as exc:  # Per-page boundary preserves other completed results.
                result = {'page': job['page_index'], 'status': 'error',
                          'error_code': 'output_path_rejected' if isinstance(exc, IsolationError) else 'page_detection_failed'}
                try:
                    print('ASTRA_INTERNAL_DIAGNOSTIC ' + json.dumps({
                        'page': job['page_index'] + 1, 'stage': 'detection',
                        'exception': type(exc).__name__,
                        'traceback': ''.join(traceback.format_tb(exc.__traceback__)),
                    }), file=sys.stderr, flush=True)
                except Exception:  # Logging failure cannot discard the page failure.
                    pass
            if result['status'] == 'error':
                result['error_code'] = result.get('error_code', 'page_detection_failed')
                result['error'] = ('저장 경로 확인이 필요합니다.' if result['error_code'] == 'output_path_rejected'
                                   else '페이지 분석에 실패했습니다. 작업 로그를 확인해 주세요.')
            results.append(result)
            failed = sum(item['status'] == 'error' for item in results)
            try:
                print(f"  [{done}/{len(jobs)}] p.{result['page'] + 1}: {result['status']} "
                      f"{result.get('error', '')} completed={done - failed} total={len(jobs)} failed={failed}", flush=True)
            except (OSError, ValueError):
                pass
    return sorted(results, key=lambda result: result['page'])


def continuation_pages(run_dir: Path, pages: list[int], total_pages: int) -> list[int]:
    flagged: set[int] = set()
    selected = set(pages)
    contexts: dict[int, int] = {}
    for page_index in pages:
        path = detection_path(run_dir, page_index)
        if not path.exists():
            continue
        wrapper = load_detection(path)
        detection = wrapper["result"]
        contexts[page_index] = int(wrapper.get("meta", {}).get("context_pages", 0))
        if any(question["starts_on_previous_page"] for question in detection["questions"]):
            flagged.add(page_index)
            if page_index - 1 in selected:
                flagged.add(page_index - 1)
        if any(question["continues_next_page"] for question in detection["questions"]):
            flagged.add(page_index)
            if page_index + 1 in selected:
                flagged.add(page_index + 1)
    return sorted(
        page
        for page in flagged
        if 0 <= page < total_pages and contexts.get(page, 0) < 1
    )


def detect_pdf(args: argparse.Namespace, pdf_path: Path, run_dir: Path, source_sha: str) -> dict[str, Any]:
    if not codex_executable():
        raise RuntimeError("codex CLI를 PATH에서 찾을 수 없습니다")
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        total_pages = len(document)
    finally:
        document.close()
    pages = parse_pages(args.pages, total_pages)
    if args.max_pages_per_doc:
        pages = pages[: args.max_pages_per_doc]
    triage_state: dict[str, Any] | None = None
    rotations: dict[int, int] = {}
    should_triage = args.triage == "always" or (
        args.triage == "auto" and args.pages.lower() == "all"
    )
    if should_triage:
        pages, triage_state = triage_pdf(args, pdf_path, run_dir, source_sha, pages)
        rotations = {
            int(page_number) - 1: int(rotation)
            for page_number, rotation in triage_state.get("rotations", {}).items()
        }
        print(f"상세 검출 대상: {len(pages)}쪽")
    context_mode = str(args.context_pages)
    initial_context = 1 if context_mode == "1" or (
        context_mode == "auto"
        and triage_state is not None
        and bool(triage_state.get("shared_material_pages"))
    ) else 0
    neighbors = {
        neighbor
        for page in pages
        for neighbor in (page - 1, page + 1)
        if initial_context and 0 <= neighbor < total_pages
    }
    text_hints = ensure_render_assets(
        pdf_path, run_dir, pages, args.vision_dpi, args.tiles, neighbors, rotations
    )
    jobs = make_detection_jobs(
        args,
        pdf_path,
        run_dir,
        source_sha,
        pages,
        total_pages,
        text_hints,
        initial_context,
        args.force,
        rotations,
    )
    print(f"Codex 검출: {pdf_path.name} ({len(pages)}쪽, workers={args.workers})")
    results = execute_jobs(jobs, args.workers)
    if context_mode == "auto" and not any(result["status"] == "error" for result in results):
        flagged = continuation_pages(run_dir, pages, total_pages)
        if flagged:
            context_neighbors = {
                neighbor
                for page in flagged
                for neighbor in (page - 1, page + 1)
                if 0 <= neighbor < total_pages
            }
            extra_hints = ensure_render_assets(
                pdf_path,
                run_dir,
                flagged,
                args.vision_dpi,
                args.tiles,
                context_neighbors,
                rotations,
            )
            text_hints.update(extra_hints)
            print(f"페이지 걸침 재검증: {', '.join(str(page + 1) for page in flagged)}")
            retry_jobs = make_detection_jobs(
                args,
                pdf_path,
                run_dir,
                source_sha,
                flagged,
                total_pages,
                text_hints,
                1,
                True,
                rotations,
            )
            results.extend(execute_jobs(retry_jobs, args.workers))
    failure = PageDetectionError(results, pages)
    if failure.failed_pages:
        raise failure
    review: list[dict[str, Any]] = []
    for page_index in pages:
        path = detection_path(run_dir, page_index)
        wrapper = load_detection(path)
        detection = wrapper["result"]
        review.extend(page_quality_issues(detection, page_index, total_pages, args.confidence_threshold))
        overview = page_image_path(
            run_dir / "pages",
            page_index,
            args.vision_dpi,
            int(rotations.get(page_index, 0)),
        )
        create_overlay(
            overview,
            detection,
            run_dir / "overlays" / f"page-{page_index + 1:04d}.jpg",
            args.confidence_threshold,
        )
    write_json(run_dir / "review_queue.pages.json", review)
    state = {
        "version": VERSION,
        "source_pdf": str(pdf_path),
        "source_sha256": source_sha,
        "total_pages": total_pages,
        "processed_pages": [page + 1 for page in pages],
        "triage": triage_state,
        "rotations": {str(page + 1): rotation for page, rotation in rotations.items()},
        "vision_dpi": args.vision_dpi,
        "tiles": args.tiles,
        "context_pages": context_mode,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "page_review_count": len(review),
        "updated_at": utc_now(),
    }
    write_json(run_dir / "run_state.json", state)
    return state


def absolute_part(part: dict[str, Any], primary_page: int) -> dict[str, Any]:
    return {
        "source_page": primary_page + int(part["page_offset"]),
        "role": part["role"],
        "bbox": list(part["bbox"]),
    }


def resolved_question(question: dict[str, Any], page_index: int) -> dict[str, Any]:
    return {
        "question_id": f"p{page_index + 1:04d}-{safe_slug(question['local_id'], 24)}",
        "printed_number": question["printed_number"],
        "number_part": (
            {"source_page": page_index, "role": "main_number", "bbox": list(question["number_bbox"])}
            if question["number_bbox"]
            else None
        ),
        "kind": question["kind"],
        "answer_type": question["answer_type"],
        "group_label": question["group_label"],
        "shared_context_parts": [absolute_part(part, page_index) for part in question["shared_context_parts"]],
        "main_parts": [absolute_part(part, page_index) for part in question["main_parts"]],
        "subquestions": [
            {
                "label": subquestion["label"],
                "answer_type": subquestion["answer_type"],
                "parts": [absolute_part(part, page_index) for part in subquestion["parts"]],
                "confidence": subquestion["confidence"],
            }
            for subquestion in question["subquestions"]
        ],
        "starts_on_previous_page": question["starts_on_previous_page"],
        "continues_next_page": question["continues_next_page"],
        "confidence": question["confidence"],
        "source_detection_pages": [page_index],
        "review_reasons": [],
    }


def dedupe_parts(parts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[int, tuple[int, ...]]] = set()
    result: list[dict[str, Any]] = []
    for part in parts:
        key = (int(part["source_page"]), tuple(int(value) for value in part["bbox"]))
        if key in seen:
            continue
        seen.add(key)
        result.append(part)
    return result


def coalesce_parts(parts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join nearby same-column fragments without spanning unrelated columns."""
    result = [dict(part, bbox=list(part["bbox"])) for part in dedupe_parts(parts)]
    changed = True
    while changed:
        changed = False
        for first_index, first in enumerate(result):
            ax0, ay0, ax1, ay1 = first["bbox"]
            aw = ax1 - ax0
            for second_index in range(first_index + 1, len(result)):
                second = result[second_index]
                if first["source_page"] != second["source_page"]:
                    continue
                bx0, by0, bx1, by1 = second["bbox"]
                bw = bx1 - bx0
                horizontal_overlap = max(0, min(ax1, bx1) - max(ax0, bx0))
                overlap_ratio = horizontal_overlap / max(1, min(aw, bw))
                width_ratio = max(aw, bw) / max(1, min(aw, bw))
                vertical_gap = by0 - ay1
                vertical_overlap = max(0, min(ay1, by1) - max(ay0, by0))
                height_ratio = vertical_overlap / max(1, min(ay1 - ay0, by1 - by0))
                height_size_ratio = max(ay1 - ay0, by1 - by0) / max(
                    1, min(ay1 - ay0, by1 - by0)
                )
                horizontal_gap = bx0 - ax1
                merge_vertical = (
                    overlap_ratio >= 0.85
                    and width_ratio <= 1.35
                    and by0 >= ay0 - 5
                    and vertical_gap <= 25
                )
                merge_horizontal = (
                    height_ratio >= 0.75
                    and (
                        height_size_ratio <= 1.35
                        or first.get("role") == "continuation"
                        or second.get("role") == "continuation"
                    )
                    and -10 <= horizontal_gap <= 25
                )
                if merge_vertical or merge_horizontal:
                    result[first_index] = {
                        **first,
                        "bbox": [min(ax0, bx0), min(ay0, by0), max(ax1, bx1), max(ay1, by1)],
                    }
                    result.pop(second_index)
                    changed = True
                    break
            if changed:
                break
    return result


def merge_continuation(target: dict[str, Any], continuation: dict[str, Any]) -> None:
    target["shared_context_parts"] = dedupe_parts(
        target["shared_context_parts"] + continuation["shared_context_parts"]
    )
    target["main_parts"] = dedupe_parts(target["main_parts"] + continuation["main_parts"])
    for incoming in continuation["subquestions"]:
        match = next(
            (sub for sub in reversed(target["subquestions"]) if sub["label"] and sub["label"] == incoming["label"]),
            None,
        )
        if match:
            match["parts"] = dedupe_parts(match["parts"] + incoming["parts"])
            match["confidence"] = min(match["confidence"], incoming["confidence"])
        else:
            target["subquestions"].append(incoming)
    if not target["printed_number"] and continuation["printed_number"]:
        target["printed_number"] = continuation["printed_number"]
    if target["number_part"] is None and continuation["number_part"] is not None:
        target["number_part"] = continuation["number_part"]
    if target["kind"] == "continuation" and continuation["kind"] != "continuation":
        target["kind"] = continuation["kind"]
    target["continues_next_page"] = continuation["continues_next_page"]
    target["confidence"] = min(target["confidence"], continuation["confidence"])
    target["source_detection_pages"].extend(continuation["source_detection_pages"])


def assemble_questions(
    detections: list[tuple[int, dict[str, Any]]], total_pages: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resolved: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    pending_page: int | None = None
    for page_index, detection in sorted(detections, key=lambda item: item[0]):
        raw_questions = detection["questions"]
        start_at = 0
        if pending is not None:
            if pending_page is not None and page_index != pending_page + 1:
                reason = f"다음 처리 페이지가 연속되지 않아 continuation을 결합하지 못함: p.{pending_page + 1}→p.{page_index + 1}"
                pending["review_reasons"].append(reason)
                review.append({"page": pending_page + 1, "item": pending["question_id"], "reason": reason})
                pending = None
            elif raw_questions and raw_questions[0]["starts_on_previous_page"]:
                incoming = resolved_question(raw_questions[0], page_index)
                merge_continuation(pending, incoming)
                pending_page = page_index
                start_at = 1
                if not pending["continues_next_page"]:
                    pending = None
            else:
                reason = "continues_next_page와 다음 페이지 starts_on_previous_page가 일치하지 않음"
                pending["review_reasons"].append(reason)
                review.append({"page": page_index + 1, "item": pending["question_id"], "reason": reason})
                pending = None
        for raw in raw_questions[start_at:]:
            item = resolved_question(raw, page_index)
            if raw["starts_on_previous_page"]:
                reason = "이전 문항과 연결되지 않은 continuation"
                item["review_reasons"].append(reason)
                review.append({"page": page_index + 1, "item": item["question_id"], "reason": reason})
            resolved.append(item)
            if item["continues_next_page"]:
                if pending is not None:
                    reason = "한 페이지에서 둘 이상의 문항이 다음 페이지로 계속됨"
                    item["review_reasons"].append(reason)
                    review.append({"page": page_index + 1, "item": item["question_id"], "reason": reason})
                pending = item
                pending_page = page_index
    if pending is not None:
        reason = "처리 범위 끝에서 문항이 다음 페이지로 계속됨"
        pending["review_reasons"].append(reason)
        review.append({"page": (pending_page or 0) + 1, "item": pending["question_id"], "reason": reason})
    for question in resolved:
        for part in question["shared_context_parts"] + question["main_parts"] + [
            part for subquestion in question["subquestions"] for part in subquestion["parts"]
        ]:
            if part["source_page"] < 0 or part["source_page"] >= total_pages:
                reason = f"문서 범위를 벗어난 page_offset: {part['source_page'] + 1}"
                question["review_reasons"].append(reason)
                review.append({"page": None, "item": question["question_id"], "reason": reason})
    return resolved, review


def assign_question_columns(
    questions: list[dict[str, Any]], detections: list[tuple[int, dict[str, Any]]]
) -> None:
    layouts = {page_index: detection["layout"] for page_index, detection in detections}
    by_page: dict[int, list[dict[str, Any]]] = {}
    for question in questions:
        question["column"] = None
        if question["source_detection_pages"]:
            by_page.setdefault(question["source_detection_pages"][0], []).append(question)
    for page_index, page_questions in by_page.items():
        if layouts.get(page_index) != "two_columns":
            continue
        anchored = [
            question for question in page_questions if question.get("number_part") is not None
        ]
        if len(anchored) < 2:
            continue
        centers = [
            (question["number_part"]["bbox"][0] + question["number_part"]["bbox"][2]) / 2
            for question in anchored
        ]
        if max(centers) - min(centers) < 140:
            median = sorted(centers)[len(centers) // 2]
            if median < 300:
                for question in anchored:
                    question["column"] = "left"
            elif median > 700:
                for question in anchored:
                    question["column"] = "right"
            continue
        threshold = (min(centers) + max(centers)) / 2
        for question, center in zip(anchored, centers):
            question["column"] = "left" if center < threshold else "right"


def build_units(
    questions: list[dict[str, Any]],
    unit_mode: str,
    emit_groups: bool,
    answer_space: str,
    sidecars: str,
) -> list[dict[str, Any]]:
    def allowed(parts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            part
            for part in parts
            if (answer_space == "include" or part["role"] != "response_area")
            and (sidecars == "include" or part["role"] != "sidecar_hint")
        ]

    def main_with_number(question: dict[str, Any]) -> list[dict[str, Any]]:
        parts = [dict(part, bbox=list(part["bbox"])) for part in allowed(question["main_parts"])]
        anchor = question.get("number_part")
        if not anchor:
            return parts
        candidates = [
            (index, part)
            for index, part in enumerate(parts)
            if part["source_page"] == anchor["source_page"]
        ]
        if not candidates:
            return [anchor] + parts
        _, closest = min(
            candidates,
            key=lambda pair: abs(pair[1]["bbox"][1] - anchor["bbox"][1]),
        )
        if bbox_overlap_of_smaller(anchor["bbox"], closest["bbox"]) >= 0.85:
            return parts
        return [dict(anchor, bbox=list(anchor["bbox"]))] + parts

    units: list[dict[str, Any]] = []
    for question in questions:
        excluded_parts = [
            part
            for part in question["main_parts"]
            + [part for subquestion in question["subquestions"] for part in subquestion["parts"]]
            if (answer_space == "exclude" and part["role"] == "response_area")
            or (sidecars == "exclude" and part["role"] == "sidecar_hint")
        ]
        shared = coalesce_parts(
            [{**part, "is_context": True} for part in allowed(question["shared_context_parts"])]
        )
        main = coalesce_parts(main_with_number(question))
        subquestions = question["subquestions"]
        first_subquestion_y: dict[int, int] = {}
        if subquestions:
            main = [{**part, "is_parent_context": True} for part in main]
            for subquestion in subquestions:
                for part in subquestion["parts"]:
                    page = int(part["source_page"])
                    first_subquestion_y[page] = min(
                        first_subquestion_y.get(page, 1000), int(part["bbox"][1])
                    )
        if subquestions and unit_mode in {"main", "both"}:
            parts = shared + main + [
                part
                for subquestion in subquestions
                for part in coalesce_parts(allowed(subquestion["parts"]))
            ]
            units.append(
                {
                    "unit_id": question["question_id"] + "-main",
                    "owner_question_id": question["question_id"],
                    "column": question.get("column"),
                    "number_bbox": question.get("number_part"),
                    "first_subquestion_y": first_subquestion_y,
                    "excluded_parts": excluded_parts,
                    "next_subquestion_y": {},
                    "unit_kind": "main_question",
                    "printed_number": question["printed_number"],
                    "group_label": question["group_label"],
                    "answer_type": question["answer_type"],
                    "parts": dedupe_parts(parts),
                    "confidence": min(
                        [question["confidence"]] + [subquestion["confidence"] for subquestion in subquestions]
                    ),
                    "review_reasons": list(question["review_reasons"]),
                }
            )
        if subquestions and unit_mode in {"atomic", "both"}:
            base_ids = [
                question["question_id"] + f"-sub-{safe_slug(sub['label'] or str(index), 20)}"
                for index, sub in enumerate(subquestions, 1)
            ]
            reserved_ids = set(base_ids)
            used_ids: set[str] = set()
            for index, subquestion in enumerate(subquestions, 1):
                label = subquestion["label"] or str(index)
                unit_id = base_ids[index - 1]
                if unit_id in used_ids:
                    unit_id += f"-ord-{index}"
                    while unit_id in reserved_ids or unit_id in used_ids:
                        unit_id += f"-ord-{index}"
                used_ids.add(unit_id)
                next_subquestion_y: dict[int, int] = {}
                if index < len(subquestions):
                    for next_part in subquestions[index]["parts"]:
                        page = int(next_part["source_page"])
                        next_subquestion_y[page] = min(
                            next_subquestion_y.get(page, 1000), int(next_part["bbox"][1])
                        )
                units.append(
                    {
                        "unit_id": unit_id,
                        "owner_question_id": question["question_id"],
                        "column": question.get("column"),
                        "number_bbox": question.get("number_part"),
                        "first_subquestion_y": first_subquestion_y,
                        "excluded_parts": excluded_parts,
                        "next_subquestion_y": next_subquestion_y,
                        "unit_kind": "subquestion",
                        "printed_number": f"{question['printed_number']}{label}",
                        "group_label": question["group_label"],
                        "answer_type": subquestion["answer_type"],
                        "parts": dedupe_parts(
                            shared + main + coalesce_parts(allowed(subquestion["parts"]))
                        ),
                        "confidence": min(question["confidence"], subquestion["confidence"]),
                        "review_reasons": list(question["review_reasons"]),
                    }
                )
        if not subquestions:
            units.append(
                {
                    "unit_id": question["question_id"],
                    "owner_question_id": question["question_id"],
                    "column": question.get("column"),
                    "number_bbox": question.get("number_part"),
                    "first_subquestion_y": first_subquestion_y,
                    "excluded_parts": excluded_parts,
                    "next_subquestion_y": {},
                    "unit_kind": "question",
                    "printed_number": question["printed_number"],
                    "group_label": question["group_label"],
                    "answer_type": question["answer_type"],
                    "parts": dedupe_parts(shared + main),
                    "confidence": question["confidence"],
                    "review_reasons": list(question["review_reasons"]),
                }
            )
    if emit_groups:
        current: list[dict[str, Any]] = []
        current_label: str | None = None

        def flush_group() -> None:
            nonlocal current, current_label
            if len(current) < 2 or current_label is None:
                current = []
                current_label = None
                return
            shared_parts = coalesce_parts(
                [
                    {**part, "is_context": True}
                    for part in allowed(current[0]["shared_context_parts"])
                ]
            )
            body_parts: list[dict[str, Any]] = []
            for question in current:
                body_parts.extend(coalesce_parts(main_with_number(question)))
                body_parts.extend(
                    part
                    for subquestion in question["subquestions"]
                    for part in coalesce_parts(allowed(subquestion["parts"]))
                )
            units.append(
                {
                    "unit_id": current[0]["question_id"] + "-group",
                    "owner_question_id": None,
                    "column": None,
                    "number_bbox": None,
                    "first_subquestion_y": {},
                    "excluded_parts": [
                        part
                        for question in current
                        for part in question["main_parts"]
                        + [
                            child_part
                            for subquestion in question["subquestions"]
                            for child_part in subquestion["parts"]
                        ]
                        if (answer_space == "exclude" and part["role"] == "response_area")
                        or (sidecars == "exclude" and part["role"] == "sidecar_hint")
                    ],
                    "next_subquestion_y": {},
                    "unit_kind": "shared_group_bundle",
                    "printed_number": current_label,
                    "group_label": current_label,
                    "answer_type": "mixed",
                    "parts": dedupe_parts(shared_parts + body_parts),
                    "confidence": min(question["confidence"] for question in current),
                    "review_reasons": [
                        reason for question in current for reason in question["review_reasons"]
                    ],
                }
            )
            current = []
            current_label = None

        for question in questions:
            label = question["group_label"]
            if label and (current_label is None or label == current_label):
                current_label = label
                current.append(question)
            else:
                flush_group()
                if label:
                    current_label = label
                    current = [question]
        flush_group()
    return units


class PageImageCache:
    def __init__(
        self,
        document: Any,
        dpi: int,
        rotations: dict[int, int] | None = None,
        solution_masks: dict[int, list[list[int]]] | None = None,
        solution_mask: str = "none",
        limit: int = 3,
    ) -> None:
        self.document = document
        self.dpi = dpi
        self.rotations = rotations or {}
        self.solution_masks = solution_masks or {}
        self.solution_mask = solution_mask
        self.limit = limit
        self.images: OrderedDict[int, Image.Image] = OrderedDict()
        self.column_splits: dict[int, int] = {}

    def get(self, page_index: int) -> Image.Image:
        if page_index in self.images:
            self.images.move_to_end(page_index)
            return self.images[page_index]
        page = self.document[page_index]
        try:
            bitmap = page.render(scale=self.dpi / 72.0)
            try:
                image = bitmap.to_pil().convert("RGB")
            finally:
                bitmap.close()
        finally:
            page.close()
        rotation = int(self.rotations.get(page_index, 0))
        if rotation:
            rotated = image.rotate(-rotation, expand=True, fillcolor="white")
            image.close()
            image = rotated
        if self.solution_mask == "white" and self.solution_masks.get(page_index):
            draw = ImageDraw.Draw(image)
            for bbox in self.solution_masks[page_index]:
                draw.rectangle(
                    normalized_to_pixels(bbox, image.width, image.height, padding=2),
                    fill="white",
                )
        self.images[page_index] = image
        if len(self.images) > self.limit:
            _, old = self.images.popitem(last=False)
            old.close()
        return image

    def column_split(self, page_index: int) -> int:
        if page_index in self.column_splits:
            return self.column_splits[page_index]
        image = self.get(page_index)
        gray = image.convert("L")
        try:
            sample = gray.resize((400, 600), Image.Resampling.LANCZOS)
        finally:
            gray.close()
        try:
            pixels = sample.load()
            candidates: list[tuple[int, int]] = []
            for x in range(140, 261):
                dark = 0
                for sx in range(max(0, x - 4), min(400, x + 5)):
                    for y in range(45, 570):
                        if pixels[sx, y] < 215:
                            dark += 1
                candidates.append((dark, x))
            _, best_x = min(candidates, key=lambda item: (item[0], abs(item[1] - 200)))
            split = round(best_x * 1000 / 400)
        finally:
            sample.close()
        self.column_splits[page_index] = split
        return split

    def close(self) -> None:
        for image in self.images.values():
            image.close()
        self.images.clear()
        self.column_splits.clear()


def compose_crops(crops: list[Image.Image], gap: int) -> Image.Image:
    width = max(crop.width for crop in crops)
    height = sum(crop.height for crop in crops) + gap * (len(crops) - 1)
    output = Image.new("RGB", (width, height), "white")
    y = 0
    for crop in crops:
        output.paste(crop, ((width - crop.width) // 2, y))
        y += crop.height + gap
    return output


def compose_spatial_crops(
    crops: list[Image.Image],
    parts: list[dict[str, Any]],
    page_sizes: dict[int, tuple[int, int]],
    gap: int,
    preserve_part_order: bool = False,
) -> Image.Image:
    """Keep nearby fragments in place; stack distant fragments compactly."""
    entries = list(zip(crops, parts))

    def connected(left: dict[str, Any], right: dict[str, Any]) -> bool:
        if int(left["source_page"]) != int(right["source_page"]):
            return False
        ax0, ay0, ax1, ay1 = left["crop_bbox"]
        bx0, by0, bx1, by1 = right["crop_bbox"]
        horizontal_overlap = max(0, min(ax1, bx1) - max(ax0, bx0))
        vertical_overlap = max(0, min(ay1, by1) - max(ay0, by0))
        horizontal_gap = max(0, max(ax0, bx0) - min(ax1, bx1))
        vertical_gap = max(0, max(ay0, by0) - min(ay1, by1))
        return (
            (horizontal_overlap > 0 and vertical_gap <= 30)
            or (vertical_overlap > 0 and horizontal_gap <= 30)
        )

    remaining = set(range(len(entries)))
    components: list[list[int]] = []
    while remaining:
        seed = remaining.pop()
        component = [seed]
        frontier = [seed]
        while frontier:
            current = frontier.pop()
            neighbors = [
                candidate
                for candidate in remaining
                if connected(entries[current][1], entries[candidate][1])
            ]
            for neighbor in neighbors:
                remaining.remove(neighbor)
                component.append(neighbor)
                frontier.append(neighbor)
        components.append(component)
    if preserve_part_order:
        components.sort(key=min)
    else:
        components.sort(
            key=lambda component: (
                min(int(entries[index][1]["source_page"]) for index in component),
                min(entries[index][1]["crop_bbox"][1] for index in component),
                min(entries[index][1]["crop_bbox"][0] for index in component),
            )
        )

    component_canvases: list[Image.Image] = []
    for component in components:
        component_entries = [entries[index] for index in component]
        page_index = int(component_entries[0][1]["source_page"])
        boxes = [part["crop_bbox"] for _, part in component_entries]
        union = [
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        ]
        width, height = page_sizes[page_index]
        ux0, uy0, ux1, uy1 = normalized_to_pixels(union, width, height)
        canvas = Image.new("RGB", (max(1, ux1 - ux0), max(1, uy1 - uy0)), "white")
        for crop, part in component_entries:
            px0, py0, _, _ = normalized_to_pixels(part["crop_bbox"], width, height)
            canvas.paste(crop, (px0 - ux0, py0 - uy0))
        component_canvases.append(canvas)
    try:
        return compose_crops(component_canvases, gap)
    finally:
        for canvas in component_canvases:
            canvas.close()


def ink_edge_contacts(image: Image.Image) -> list[str]:
    gray = image.convert("L")
    try:
        band = max(3, round(min(image.width, image.height) * 0.006))
        regions = {
            "left": (0, 0, min(band, image.width), image.height),
            "right": (max(0, image.width - band), 0, image.width, image.height),
            "top": (0, 0, image.width, min(band, image.height)),
            "bottom": (0, max(0, image.height - band), image.width, image.height),
        }
        contacts: list[str] = []
        for name, box in regions.items():
            strip = gray.crop(box)
            try:
                histogram = strip.histogram()
                dark_fraction = sum(histogram[:210]) / max(1, strip.width * strip.height)
                if dark_fraction >= 0.005:
                    contacts.append(name)
            finally:
                strip.close()
        return contacts
    finally:
        gray.close()


def crop_with_edge_expansion(
    page_image: Image.Image,
    normalized_bbox: list[int],
    step: int = 6,
    max_growth: int = 150,
    right_limit: int = 1000,
    bottom_limit: int = 1000,
    clear_required: int = 2,
) -> tuple[Image.Image, list[int], list[str]]:
    current = list(normalized_bbox)

    def make_crop(box: list[int]) -> Image.Image:
        return page_image.crop(normalized_to_pixels(box, page_image.width, page_image.height)).convert("RGB")

    crop = make_crop(current)
    contacts = ink_edge_contacts(crop)
    active = {edge for edge in contacts if edge in {"right", "bottom"}}
    clear_streak = {edge: 0 for edge in active}
    grown = 0
    while active and grown < max_growth:
        candidate = list(current)
        if "right" in active and candidate[2] < right_limit:
            candidate[2] = min(right_limit, candidate[2] + step)
        if "bottom" in active and candidate[3] < bottom_limit:
            candidate[3] = min(bottom_limit, candidate[3] + step)
        if candidate == current:
            break
        next_crop = make_crop(candidate)
        next_contacts = ink_edge_contacts(next_crop)
        crop.close()
        crop = next_crop
        current = candidate
        contacts = next_contacts
        for edge in list(active):
            if edge in contacts:
                clear_streak[edge] = 0
            else:
                clear_streak[edge] += 1
                if clear_streak[edge] >= clear_required:
                    active.remove(edge)
        grown += step
    return crop, current, contacts


def manifest_unit_fields(unit: dict[str, Any]) -> dict[str, Any]:
    fields = {key: value for key, value in unit.items() if key != "parts"}
    number_bbox = fields.get("number_bbox")
    if isinstance(number_bbox, dict) and isinstance(number_bbox.get("source_page"), int):
        fields["number_bbox"] = {**number_bbox, "source_page": number_bbox["source_page"] + 1}
    excluded = fields.get("excluded_parts")
    if isinstance(excluded, list):
        fields["excluded_parts"] = [
            {**part, "source_page": part["source_page"] + 1}
            if isinstance(part, dict) and isinstance(part.get("source_page"), int)
            else part
            for part in excluded
        ]
    for name in ("first_subquestion_y", "next_subquestion_y"):
        values = fields.get(name)
        if isinstance(values, dict):
            fields[name] = {str(int(page) + 1): value for page, value in values.items()}
    return fields


def crop_units(
    pdf_path: Path,
    run_dir: Path,
    units: list[dict[str, Any]],
    crop_dpi: int,
    padding: int,
    image_format: str,
    rotations: dict[int, int] | None = None,
    solution_masks: dict[int, list[list[int]]] | None = None,
    solution_mask: str = "none",
    ignored_crop_limits: dict[int, list[list[int]]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    document = pdfium.PdfDocument(str(pdf_path))
    cache = PageImageCache(document, crop_dpi, rotations, solution_masks, solution_mask)
    output_dir = run_dir / "crops"
    inside(output_dir.resolve(), "output")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_items: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    ignored_crop_limits = ignored_crop_limits or {}
    forbidden_by_page: dict[int, list[tuple[str, list[int]]]] = {}
    seen_forbidden: set[tuple[int, str, tuple[int, ...]]] = set()
    for candidate in units:
        owner = candidate.get("owner_question_id")
        if owner is None:
            continue
        for part in candidate["parts"]:
            key = (part["source_page"], owner, tuple(part["bbox"]))
            if key in seen_forbidden:
                continue
            seen_forbidden.add(key)
            forbidden_by_page.setdefault(part["source_page"], []).append((owner, part["bbox"]))
    extension = "jpg" if image_format == "jpg" else "png"
    try:
        for sequence, unit in enumerate(units, 1):
            cropped_parts: list[Image.Image] = []
            valid_parts: list[dict[str, Any]] = []
            for part in unit["parts"]:
                page_index = int(part["source_page"])
                if page_index < 0 or page_index >= len(document):
                    reason = f"크롭할 수 없는 페이지 {page_index + 1}"
                    unit["review_reasons"].append(reason)
                    review.append(
                        review_entry(None, unit["unit_id"], reason, ["source_page_absent"])
                    )
                    continue
                page_image = cache.get(page_index)
                safe_bbox = padded_bbox_without_neighbors(
                    part["bbox"],
                    padding,
                    unit.get("owner_question_id"),
                    forbidden_by_page.get(page_index, []),
                )
                right_limit = 1000
                bottom_limit = 1000
                number_part = unit.get("number_bbox")
                if (
                    part.get("is_context")
                    and number_part
                    and number_part["source_page"] == page_index
                    and part["bbox"][1] < number_part["bbox"][1]
                ):
                    context_limit = max(part["bbox"][1] + 1, number_part["bbox"][1] - 8)
                    safe_bbox[3] = min(safe_bbox[3], context_limit)
                    bottom_limit = min(bottom_limit, context_limit)
                first_sub_y = unit.get("first_subquestion_y", {}).get(page_index)
                if (
                    part.get("is_parent_context")
                    and first_sub_y is not None
                    and part["bbox"][1] < first_sub_y
                    and part["bbox"][3] <= first_sub_y + 15
                ):
                    parent_limit = max(part["bbox"][1] + 1, first_sub_y - 5)
                    safe_bbox[3] = min(safe_bbox[3], parent_limit)
                    bottom_limit = min(bottom_limit, parent_limit)
                next_sub_y = unit.get("next_subquestion_y", {}).get(page_index)
                if (
                    unit.get("unit_kind") == "subquestion"
                    and part["role"] == "subquestion"
                    and next_sub_y is not None
                    and part["bbox"][3] <= next_sub_y + 8
                ):
                    sibling_limit = max(part["bbox"][1] + 1, next_sub_y - 4)
                    safe_bbox[3] = min(safe_bbox[3], sibling_limit)
                    bottom_limit = min(bottom_limit, sibling_limit)
                column = unit.get("column")
                if column and part["bbox"][2] - part["bbox"][0] < 650:
                    split = cache.column_split(page_index)
                    safe_bbox, column_right_limit = constrain_bbox_to_column(
                        safe_bbox, part["bbox"], column, split
                    )
                    right_limit = min(right_limit, column_right_limit)
                safe_bbox, ignored_bottom_limit = constrain_bbox_before_ignored_regions(
                    safe_bbox,
                    part["bbox"],
                    ignored_crop_limits.get(page_index, []),
                )
                bottom_limit = min(bottom_limit, ignored_bottom_limit)
                px0, py0, px1, py1 = part["bbox"]
                for excluded in unit.get("excluded_parts", []):
                    if excluded["source_page"] != page_index:
                        continue
                    if column and excluded["role"] == "sidecar_hint":
                        continue
                    ex0, ey0, ex1, ey1 = excluded["bbox"]
                    vertical_overlap = max(0, min(py1, ey1) - max(py0, ey0)) / max(
                        1, min(py1 - py0, ey1 - ey0)
                    )
                    horizontal_overlap = max(0, min(px1, ex1) - max(px0, ex0)) / max(
                        1, min(px1 - px0, ex1 - ex0)
                    )
                    if vertical_overlap >= 0.25 and px1 <= ex0:
                        right_limit = min(right_limit, max(px1, ex0 - 8))
                        safe_bbox[2] = min(safe_bbox[2], right_limit)
                    if horizontal_overlap >= 0.25 and py1 <= ey0:
                        bottom_limit = min(bottom_limit, max(py1, ey0 - 8))
                        safe_bbox[3] = min(safe_bbox[3], bottom_limit)
                for _, other in forbidden_by_page.get(page_index, []):
                    if other == part["bbox"] or bbox_iou(part["bbox"], other) > 0:
                        continue
                    ox0, oy0, ox1, _ = other
                    horizontal_overlap = max(0, min(px1, ox1) - max(px0, ox0)) / max(
                        1, min(px1 - px0, ox1 - ox0)
                    )
                    if horizontal_overlap >= 0.25 and py1 <= oy0:
                        bottom_limit = min(bottom_limit, max(py1, oy0 - 1))
                pixel_box = normalized_to_pixels(safe_bbox, page_image.width, page_image.height)
                if pixel_box[2] <= pixel_box[0] or pixel_box[3] <= pixel_box[1]:
                    reason = f"빈 픽셀 bbox {part['bbox']}"
                    unit["review_reasons"].append(reason)
                    review.append(
                        review_entry(
                            page_index + 1,
                            unit["unit_id"],
                            reason,
                            ["owner_bbox_invalid"],
                        )
                    )
                    continue
                cropped, applied_bbox, contacts = crop_with_edge_expansion(
                    page_image,
                    safe_bbox,
                    right_limit=right_limit,
                    bottom_limit=bottom_limit,
                )
                cropped_parts.append(cropped)
                valid_parts.append(
                    {
                        **part,
                        "crop_bbox": applied_bbox,
                    }
                )
                if contacts:
                    reason = (
                        f"잉크가 크롭 경계({','.join(contacts)})에 닿음: "
                        f"p.{page_index + 1} {part['role']} {part['bbox']}"
                    )
                    if reason not in unit["review_reasons"]:
                        unit["review_reasons"].append(reason)
                    review.append(
                        review_entry(
                            page_index + 1,
                            unit["unit_id"],
                            reason,
                            ["crop_edge_contact"],
                        )
                    )
            if len(valid_parts) > 1:
                keep = [True] * len(valid_parts)
                for index, candidate in enumerate(valid_parts):
                    if candidate.get("role") == "main_number":
                        continue
                    candidate_box = candidate["crop_bbox"]
                    candidate_area = (candidate_box[2] - candidate_box[0]) * (
                        candidate_box[3] - candidate_box[1]
                    )
                    for other_index, other in enumerate(valid_parts):
                        if index == other_index or candidate["source_page"] != other["source_page"]:
                            continue
                        other_box = other["crop_bbox"]
                        other_area = (other_box[2] - other_box[0]) * (other_box[3] - other_box[1])
                        if (
                            candidate_area <= other_area
                            and bbox_overlap_of_smaller(candidate_box, other_box) >= 0.85
                        ):
                            keep[index] = False
                            break
                if not all(keep):
                    filtered_crops: list[Image.Image] = []
                    filtered_parts: list[dict[str, Any]] = []
                    for should_keep, crop, valid_part in zip(keep, cropped_parts, valid_parts):
                        if should_keep:
                            filtered_crops.append(crop)
                            filtered_parts.append(valid_part)
                        else:
                            crop.close()
                    cropped_parts = filtered_crops
                    valid_parts = filtered_parts
            if not cropped_parts:
                reason = "유효한 크롭 영역이 없음"
                unit["review_reasons"].append(reason)
                review.append(
                    review_entry(None, unit["unit_id"], reason, ["owner_bbox_invalid"])
                )
                continue
            page_sizes = {
                int(part["source_page"]): cache.get(int(part["source_page"])).size
                for part in valid_parts
            }
            composed = compose_spatial_crops(
                cropped_parts,
                valid_parts,
                page_sizes,
                gap=max(12, round(crop_dpi * 0.06)),
            )
            number_slug = safe_slug(unit["printed_number"] or unit["unit_id"], 36)
            filename = f"{sequence:05d}_{number_slug}_{unit['unit_kind']}.{extension}"
            output_path = output_dir / filename
            if image_format == "jpg":
                composed.save(output_path, format="JPEG", quality=95, subsampling=0, optimize=True)
            else:
                composed.save(output_path, format="PNG", optimize=True)
            composed.close()
            for crop in cropped_parts:
                crop.close()
            pages = sorted({part["source_page"] + 1 for part in valid_parts})
            output_parts = [
                {**part, "source_page": int(part["source_page"]) + 1} for part in valid_parts
            ]
            item = {
                **manifest_unit_fields(unit),
                "source_pages": pages,
                "parts": output_parts,
                "output_file": str(output_path.relative_to(run_dir)),
                "output_sha256": sha256_file(output_path),
            }
            inline_solution_regions = [
                {
                    "source_page": source_page,
                    "role": "solution_content",
                    "bbox": list(bbox),
                }
                for source_page in pages
                for bbox in (solution_masks or {}).get(source_page - 1, [])
            ]
            if inline_solution_regions:
                item["inline_solution_regions"] = inline_solution_regions
            manifest_items.append(item)
            if item["review_reasons"]:
                for reason in item["review_reasons"]:
                    review.append(
                        review_entry(
                            pages[0] if pages else None,
                            item["unit_id"],
                            reason,
                            ["legacy_unresolved"],
                        )
                    )
    finally:
        cache.close()
        document.close()
    return manifest_items, review


def crop_from_detections(
    pdf_path: Path,
    run_dir: Path,
    unit_mode: str,
    emit_groups: bool,
    crop_dpi: int,
    padding: int,
    image_format: str,
    confidence_threshold: float,
    answer_space: str,
    sidecars: str,
    solution_mask: str,
) -> dict[str, Any]:
    previous_manifest_path = run_dir / "manifest.json"
    previous_manifest = (
        json.loads(previous_manifest_path.read_text(encoding="utf-8"))
        if previous_manifest_path.is_file()
        else {"items": []}
    )
    state_path = run_dir / "run_state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        page_indices = [page - 1 for page in state["processed_pages"]]
        expected_sha = state.get("source_sha256")
        actual_sha = sha256_file(pdf_path)
        if expected_sha and expected_sha != actual_sha:
            raise ValueError("원본 PDF 해시가 검출 당시와 달라졌습니다")
        rotations = {
            int(page_number) - 1: int(rotation)
            for page_number, rotation in state.get("rotations", {}).items()
        }
    else:
        actual_sha = sha256_file(pdf_path)
        rotations = {}
        page_indices = sorted(
            int(path.stem.split("-")[-1]) - 1 for path in (run_dir / "detections").glob("page-*.json")
        )
        state = {
            "source_sha256": actual_sha,
            "processed_pages": [page + 1 for page in page_indices],
            "vision_dpi": crop_dpi,
            "rotations": {},
            "triage": None,
        }
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        total_pages = len(document)
    finally:
        document.close()
    detections: list[tuple[int, dict[str, Any]]] = []
    solution_masks: dict[int, list[list[int]]] = {}
    ignored_crop_limits: dict[int, list[list[int]]] = {}
    page_review: list[dict[str, Any]] = []
    for page_index in page_indices:
        wrapper = load_detection(detection_path(run_dir, page_index))
        detections.append((page_index, wrapper["result"]))
        solution_masks[page_index] = [
            list(region["bbox"])
            for region in wrapper["result"]["ignored_regions"]
            if region["role"] == "solution_content"
        ]
        ignored_crop_limits[page_index] = [
            list(region["bbox"])
            for region in wrapper["result"]["ignored_regions"]
            if region["role"] in {"footer", "solution_content"}
        ]
        page_review.extend(
            page_quality_issues(wrapper["result"], page_index, total_pages, confidence_threshold)
        )
    questions, merge_review = assemble_questions(detections, total_pages)
    assign_question_columns(questions, detections)
    units = build_units(questions, unit_mode, emit_groups, answer_space, sidecars)
    source_page_indices = sorted(
        set(page_indices)
        | {
            int(part["source_page"])
            for unit in units
            for part in unit["parts"]
        }
    )
    ensure_render_assets(
        pdf_path,
        run_dir,
        source_page_indices,
        int(state.get("vision_dpi", crop_dpi)),
        "none",
        rotations=rotations,
    )
    manifest_items, crop_review = crop_units(
        pdf_path,
        run_dir,
        units,
        crop_dpi,
        padding,
        image_format,
        rotations,
        solution_masks,
        solution_mask,
        ignored_crop_limits,
    )
    review = page_review + merge_review + crop_review
    unique_review: list[dict[str, Any]] = []
    seen_review: set[tuple[Any, Any, Any]] = set()
    for item in review:
        key = (item.get("page"), item.get("item"), item.get("reason"))
        if key not in seen_review:
            seen_review.add(key)
            unique_review.append(item)
    inventory = build_source_inventory(
        run_dir, state, detections, manifest_items, unique_review
    )
    previous_items = {
        item.get("unit_id"): item for item in previous_manifest.get("items", [])
    }
    for item in manifest_items:
        item["coverage"] = coverage_record(item, actual_sha, inventory["pages"])
        previous = previous_items.get(item["unit_id"])
        previous_coverage = (
            previous.get("coverage") if isinstance(previous, dict) else None
        )
        previous_receipt = (
            previous_coverage.get("verification_receipt")
            if isinstance(previous_coverage, dict)
            else None
        )
        if (
            isinstance(previous, dict)
            and isinstance(previous_coverage, dict)
            and isinstance(previous_receipt, dict)
            and previous_coverage.get("proof_sha256") == item["coverage"]["proof_sha256"]
            and previous_receipt.get("coverage_proof_sha256")
            == previous_coverage.get("proof_sha256")
            and previous_receipt.get("ordered_geometry_sha256")
            == previous_coverage.get("ordered_geometry_sha256")
            and previous_receipt.get("final_crop_sha256") == item["output_sha256"]
            and isinstance(previous.get("verification"), dict)
        ):
            item["verification"] = previous["verification"]
            item["coverage"] = previous["coverage"]
    manifest = {
        "pipeline_version": VERSION,
        "created_at": utc_now(),
        "source_pdf": str(pdf_path),
        "source_sha256": actual_sha,
        "run_dir": str(run_dir),
        "unit_mode": unit_mode,
        "emit_groups": emit_groups,
        "crop_dpi": crop_dpi,
        "padding_per_mille": padding,
        "image_format": image_format,
        "answer_space": answer_space,
        "sidecars": sidecars,
        "solution_mask": solution_mask,
        "solution_mask_count": sum(len(items) for items in solution_masks.values()),
        "rotations": {str(page + 1): rotation for page, rotation in rotations.items()},
        "question_count": len(questions),
        "crop_count": len(manifest_items),
        "review_count": len(unique_review),
        "source_inventory": inventory,
        "items": manifest_items,
    }
    write_json(run_dir / "manifest.json", manifest)
    write_json(run_dir / "review_queue.json", unique_review)
    print(f"크롭 {len(manifest_items)}개, 검토 항목 {len(unique_review)}개: {run_dir}")
    return manifest


def doc_run_dir(output_root: Path, pdf_path: Path, source_sha: str) -> Path:
    return output_root / f"{safe_slug(pdf_path.stem)}_{source_sha[:12]}"


def render_command(args: argparse.Namespace) -> int:
    pdf_path = Path(args.pdf).expanduser().resolve()
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        total = len(document)
    finally:
        document.close()
    pages = parse_pages(args.pages, total)
    run_dir = Path(args.out).expanduser().resolve()
    ensure_render_assets(pdf_path, run_dir, pages, args.vision_dpi, args.tiles)
    print(f"렌더 완료: {run_dir / 'pages'}")
    return 0


def limit_documents(pdfs: list[Path], max_docs: int) -> list[Path]:
    return pdfs[:max_docs] if max_docs else pdfs


def detect_command(args: argparse.Namespace, crop_after: bool = False) -> int:
    source = Path(args.source).expanduser().resolve()
    discovered = discover_pdfs(source)
    skipped: list[dict[str, str]] = []
    pdfs = discovered
    if source.is_dir():
        if args.include_regex:
            include = re.compile(args.include_regex, re.IGNORECASE)
            kept: list[Path] = []
            for path in pdfs:
                if include.search(str(path.relative_to(source))):
                    kept.append(path)
                else:
                    skipped.append({"source_pdf": str(path), "reason": "include_regex 불일치"})
            pdfs = kept
        if args.exclude_regex:
            exclude = re.compile(args.exclude_regex, re.IGNORECASE)
            kept = []
            for path in pdfs:
                if exclude.search(str(path.relative_to(source))):
                    skipped.append({"source_pdf": str(path), "reason": "exclude_regex 일치"})
                else:
                    kept.append(path)
            pdfs = kept
    if args.max_docs and len(pdfs) > args.max_docs:
        skipped.extend(
            {"source_pdf": str(path), "reason": "max_docs 제한"} for path in pdfs[args.max_docs :]
        )
        pdfs = limit_documents(pdfs, args.max_docs)
    output_root = Path(args.out).expanduser().resolve()
    inside(output_root.resolve(), "output")
    output_root.mkdir(parents=True, exist_ok=True)
    batch: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    seen_hashes: dict[str, str] = {}
    for index, pdf_path in enumerate(pdfs, 1):
        print(f"\n[{index}/{len(pdfs)}] {pdf_path}")
        try:
            if pdf_path.stat().st_size == 0:
                raise ValueError("0바이트 PDF")
            source_sha = sha256_file(pdf_path)
            if source_sha in seen_hashes:
                skipped.append(
                    {
                        "source_pdf": str(pdf_path),
                        "reason": f"동일 SHA-256 중복: {seen_hashes[source_sha]}",
                    }
                )
                print(f"중복 건너뜀: {pdf_path.name}")
                continue
            seen_hashes[source_sha] = str(pdf_path)
            run_dir = doc_run_dir(output_root, pdf_path, source_sha)
            inside(run_dir.resolve(), "output")
            run_dir.mkdir(parents=True, exist_ok=True)
            state = detect_pdf(args, pdf_path, run_dir, source_sha)
            manifest = None
            if crop_after:
                manifest = crop_from_detections(
                    pdf_path,
                    run_dir,
                    args.unit,
                    args.emit_groups,
                    args.crop_dpi,
                    args.padding,
                    args.format,
                    args.confidence_threshold,
                    args.answer_space,
                    args.sidecars,
                    args.solution_mask,
                )
                for repair_attempt in range(1, getattr(args, "repair_attempts", 0) + 1):
                    pages_to_repair = repairable_pages(run_dir)
                    if not pages_to_repair:
                        break
                    if repair_detection_pages(
                        args,
                        pdf_path,
                        run_dir,
                        pages_to_repair,
                        repair_attempt,
                    ):
                        raise RuntimeError("경계 교정 워커 실패")
                    manifest = crop_from_detections(
                        pdf_path,
                        run_dir,
                        args.unit,
                        args.emit_groups,
                        args.crop_dpi,
                        args.padding,
                        args.format,
                        args.confidence_threshold,
                        args.answer_space,
                        args.sidecars,
                        args.solution_mask,
                    )
                if getattr(args, "verify", "none") != "none":
                    verify_run(
                        run_dir,
                        args.verify,
                        args.model,
                        args.verification_reasoning_effort,
                        args.workers,
                        args.timeout,
                        args.retries,
                        args.force,
                        args.confidence_threshold,
                    )
                    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
                    post_verify_pages = repairable_pages(run_dir)
                    if post_verify_pages and getattr(args, "repair_attempts", 0):
                        post_attempt = getattr(args, "repair_attempts", 0) + 1
                        if repair_detection_pages(
                            args,
                            pdf_path,
                            run_dir,
                            post_verify_pages,
                            post_attempt,
                        ):
                            raise RuntimeError("VLM 후 경계 교정 워커 실패")
                        manifest = crop_from_detections(
                            pdf_path,
                            run_dir,
                            args.unit,
                            args.emit_groups,
                            args.crop_dpi,
                            args.padding,
                            args.format,
                            args.confidence_threshold,
                            args.answer_space,
                            args.sidecars,
                            args.solution_mask,
                        )
                        verify_run(
                            run_dir,
                            args.verify,
                            args.model,
                            args.verification_reasoning_effort,
                            args.workers,
                            args.timeout,
                            args.retries,
                            False,
                            args.confidence_threshold,
                        )
                        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            batch.append(
                {
                    "source_pdf": str(pdf_path),
                    "run_dir": str(run_dir),
                    "processed_pages": state["processed_pages"],
                    "crop_count": manifest["crop_count"] if manifest else None,
                    "review_count": manifest["review_count"] if manifest else state["page_review_count"],
                }
            )
        except CancelledError:
            raise
        except BoundaryRepairError as exc:
            failures.append({'source_pdf': str(pdf_path), 'error': str(exc),
                             'stage': exc.stage, 'failed_pages': exc.failed_pages,
                             'completed': exc.completed, 'total': exc.total,
                             'error_code': exc.error_code, 'preserved': exc.preserved})
            print(f"실패: {exc}", file=sys.stderr, flush=True)
        except PageDetectionError as exc:
            failures.append({'source_pdf': str(pdf_path), 'error': str(exc),
                             'stage': exc.stage, 'failed_pages': exc.failed_pages,
                             'completed': exc.completed, 'total': exc.total, 'error_code': exc.error_code})
            print(f"실패: {exc}", file=sys.stderr, flush=True)
        except Exception as exc:
            failures.append({"source_pdf": str(pdf_path), "error": f"{type(exc).__name__}: {exc}"})
            print(f"실패: {type(exc).__name__}: {exc}", file=sys.stderr)
    write_json(
        output_root / "batch_manifest.json",
        {
            "pipeline_version": VERSION,
            "created_at": utc_now(),
            "source": str(source),
            "documents": batch,
            "skipped": skipped,
            "failures": failures,
        },
    )
    if failures:
        print(f"{len(failures)}개 문서 실패: {output_root / 'batch_manifest.json'}", file=sys.stderr)
        return 1
    return 0


def crop_command(args: argparse.Namespace) -> int:
    crop_from_detections(
        Path(args.pdf).expanduser().resolve(),
        Path(args.run_dir).expanduser().resolve(),
        args.unit,
        args.emit_groups,
        args.crop_dpi,
        args.padding,
        args.format,
        args.confidence_threshold,
        args.answer_space,
        args.sidecars,
        args.solution_mask,
    )
    return 0


def verify_command(args: argparse.Namespace) -> int:
    summary = verify_run(
        Path(args.run_dir).expanduser().resolve(),
        args.mode,
        args.model,
        args.reasoning_effort,
        args.workers,
        args.timeout,
        args.retries,
        args.force,
        args.confidence_threshold,
    )
    print(f"검수 완료: {summary}")
    return 0


def benchmark_command(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan).expanduser().resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    cases = plan.get("cases")
    if not isinstance(cases, list):
        raise ValueError("benchmark plan에 cases 배열이 필요합니다")
    if args.case_regex:
        case_pattern = re.compile(args.case_regex, re.IGNORECASE)
        cases = [case for case in cases if case_pattern.search(str(case.get("id", "")))]
    if args.max_cases:
        cases = cases[: args.max_cases]
    output_root = Path(args.out).expanduser().resolve()
    inside(output_root.resolve(), "output")
    output_root.mkdir(parents=True, exist_ok=True)
    reports: list[dict[str, Any]] = []
    failed = False
    for index, case in enumerate(cases, 1):
        case_id = safe_slug(str(case["id"]), 64)
        print(f"\n[benchmark {index}/{len(cases)}] {case_id}")
        child = argparse.Namespace(**vars(args))
        child.source = str(Path(case["pdf"]).expanduser().resolve())
        child.pages = str(case["pages"])
        child.out = str(output_root / case_id)
        child.max_docs = 0
        child.triage = "always"
        status = detect_command(child, crop_after=True)
        report: dict[str, Any] = {
            "id": case["id"],
            "publisher": case.get("publisher"),
            "pdf": child.source,
            "pages": child.pages,
            "expected_features": case.get("expected_features", []),
            "status": "pass" if status == 0 else "pipeline_error",
            "checks": [],
        }
        if status == 0:
            batch = json.loads((Path(child.out) / "batch_manifest.json").read_text(encoding="utf-8"))
            if not batch["documents"]:
                report["status"] = "pipeline_error"
                report["checks"].append("batch manifest에 처리 문서가 없음")
            else:
                run_dir = Path(batch["documents"][0]["run_dir"])
                manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
                triage = json.loads((run_dir / "triage.json").read_text(encoding="utf-8"))
                report.update(
                    run_dir=str(run_dir),
                    crop_count=manifest["crop_count"],
                    review_count=manifest["review_count"],
                    page_types={str(item["page_number"]): item["page_type"] for item in triage["pages"]},
                    rotations={str(item["page_number"]): item["rotation_correction"] for item in triage["pages"]},
                    verification=manifest.get("verification"),
                )
                if "min_crops" in case and manifest["crop_count"] < int(case["min_crops"]):
                    report["checks"].append(
                        f"crop_count {manifest['crop_count']} < min_crops {case['min_crops']}"
                    )
                if "max_crops" in case and manifest["crop_count"] > int(case["max_crops"]):
                    report["checks"].append(
                        f"crop_count {manifest['crop_count']} > max_crops {case['max_crops']}"
                    )
                page_number = str(case.get("check_page", str(case["pages"]).split(",")[0]))
                if "expect_page_type" in case and report["page_types"].get(page_number) != case["expect_page_type"]:
                    report["checks"].append(
                        f"page_type {report['page_types'].get(page_number)} != {case['expect_page_type']}"
                    )
                if "expect_rotation" in case and report["rotations"].get(page_number) != int(case["expect_rotation"]):
                    report["checks"].append(
                        f"rotation {report['rotations'].get(page_number)} != {case['expect_rotation']}"
                    )
                if manifest.get("verification", {}).get("verdict_counts", {}).get("fail", 0):
                    report["checks"].append("VLM verification fail 존재")
                if report["checks"]:
                    report["status"] = "fail"
        if report["status"] != "pass":
            failed = True
        reports.append(report)
        write_json(
            output_root / "benchmark_report.json",
            {
                "pipeline_version": VERSION,
                "plan": str(plan_path),
                "updated_at": utc_now(),
                "cases": reports,
            },
        )
    print(f"벤치마크 보고서: {output_root / 'benchmark_report.json'}")
    return 1 if failed else 0


def repair_command(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    pdf_path = Path(state["source_pdf"])
    selected_pages = parse_pages(args.pages, int(state["total_pages"])) if args.pages else None
    for attempt in range(1, args.attempts + 1):
        pages = selected_pages if selected_pages is not None else repairable_pages(run_dir)
        if not pages:
            break
        if repair_detection_pages(args, pdf_path, run_dir, pages, attempt):
            raise RuntimeError("경계 교정 워커 실패")
        manifest = crop_from_detections(
            pdf_path,
            run_dir,
            manifest["unit_mode"],
            bool(manifest["emit_groups"]),
            int(manifest["crop_dpi"]),
            int(manifest["padding_per_mille"]),
            manifest["image_format"],
            args.confidence_threshold,
            manifest.get("answer_space", "include"),
            manifest.get("sidecars", "include"),
            manifest.get("solution_mask", "none"),
        )
    if args.verify != "none":
        verify_run(
            run_dir,
            args.verify,
            args.model,
            args.verification_reasoning_effort,
            args.workers,
            args.timeout,
            args.retries,
            args.force,
            args.confidence_threshold,
        )
    print(f"교정 완료; 남은 검토 {len(json.loads((run_dir / 'review_queue.json').read_text(encoding='utf-8')))}개")
    return 0


def add_render_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vision-dpi", type=int, default=200, help="비전용 전체 페이지 렌더 DPI (기본 200)")
    parser.add_argument(
        "--tiles",
        choices=("none", "quadrants"),
        default="quadrants",
        help="확대 타일 첨부 방식 (기본 quadrants)",
    )


def add_detection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pages", default="all", help="1-based 페이지: all, 1-10, 1,3,7-9")
    parser.add_argument("--workers", type=int, default=2, help="동시 Codex 워커 수 (기본 2)")
    parser.add_argument("--model", default=None, help="Codex 모델; 생략 시 CLI 기본값")
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high"),
        default="medium",
    )
    parser.add_argument(
        "--context-pages",
        choices=("0", "1", "auto"),
        default="auto",
        help="이웃 페이지 첨부: 0/1/걸침 감지 시 auto (기본 auto)",
    )
    parser.add_argument("--timeout", type=int, default=600, help="페이지당 Codex 제한 시간(초)")
    parser.add_argument("--retries", type=int, default=1, help="실패 후 재시도 횟수")
    parser.add_argument("--force", action="store_true", help="검출 캐시 무시")
    parser.add_argument("--max-docs", type=int, default=0, help="디렉터리에서 처리할 최대 PDF 수; 0=제한 없음")
    parser.add_argument(
        "--max-pages-per-doc", type=int, default=0, help="문서당 앞에서 처리할 최대 페이지 수; 0=제한 없음"
    )
    parser.add_argument("--confidence-threshold", type=float, default=0.82)
    parser.add_argument(
        "--triage",
        choices=("auto", "always", "never"),
        default="auto",
        help="all 처리 시 접촉시트 페이지 선분류 (기본 auto)",
    )
    parser.add_argument("--triage-dpi", type=int, default=72)
    parser.add_argument("--triage-batch-size", type=int, default=8)
    parser.add_argument(
        "--triage-reasoning-effort",
        choices=("low", "medium", "high"),
        default="low",
    )
    parser.add_argument("--triage-confidence-threshold", type=float, default=0.78)
    parser.add_argument(
        "--include-regex",
        default="",
        help="디렉터리 처리 시 포함할 PDF 경로 정규식",
    )
    parser.add_argument(
        "--exclude-regex",
        default=DEFAULT_EXCLUDE_REGEX,
        help="디렉터리 처리 시 제외할 PDF 경로 정규식; 빈 문자열이면 끔",
    )
    add_render_options(parser)


def add_crop_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--unit",
        choices=("atomic", "main", "both"),
        default="atomic",
        help="atomic=소문항별 자급형, main=대문항 전체, both=둘 다",
    )
    parser.add_argument("--emit-groups", action="store_true", help="공통자료 묶음 전체 크롭도 생성")
    parser.add_argument("--crop-dpi", type=int, default=300, help="최종 크롭 DPI (기본 300)")
    parser.add_argument("--padding", type=int, default=8, help="bbox 사방 여백, 페이지의 1/1000 단위")
    parser.add_argument("--format", choices=("png", "jpg"), default="png")
    parser.add_argument(
        "--answer-space",
        choices=("include", "exclude"),
        default="exclude",
        help="서술형 답안선/답안상자 포함 여부",
    )
    parser.add_argument(
        "--sidecars",
        choices=("include", "exclude"),
        default="exclude",
        help="문항별 Tip/Key Word 사이드카 포함 여부",
    )
    parser.add_argument(
        "--solution-mask",
        choices=("white", "none"),
        default="none",
        help="실험적 정답·해설 흰색 마스킹; 기본 none(원본 보존)",
    )
    parser.add_argument("--confidence-threshold", type=float, default=0.82)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="question-crop",
        description="Codex CLI 비전으로 과학 문제집 PDF를 문항 단위 이미지로 크롭합니다.",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)

    audit = commands.add_parser("audit", help="원본 폴더의 PDF/페이지/텍스트 레이어 인벤토리")
    audit.add_argument("source")
    audit.add_argument("--out", default=str(ASTRA.crop_root / "output/corpus_audit.json"))
    audit.add_argument("--workers", type=int, default=4)
    audit.add_argument("--hash", action="store_true", help="중복 확인용 SHA-256 계산")
    audit.set_defaults(handler=run_audit)

    render = commands.add_parser("render", help="모델 호출 없이 페이지/확대 타일 렌더")
    render.add_argument("pdf")
    render.add_argument("--out", default=str(ASTRA.crop_root / "runs/render_preview"))
    render.add_argument("--pages", default="1")
    add_render_options(render)
    render.set_defaults(handler=render_command)

    detect = commands.add_parser("detect", help="Codex로 문항 계층과 bbox JSON 검출")
    detect.add_argument("source")
    detect.add_argument("--out", default=str(ASTRA.crop_root / "runs"))
    add_detection_options(detect)
    detect.set_defaults(handler=lambda args: detect_command(args, crop_after=False))

    crop = commands.add_parser("crop", help="기존 검출 JSON을 300 DPI 크롭으로 적용")
    crop.add_argument("pdf")
    crop.add_argument("--run-dir", required=True)
    add_crop_options(crop)
    crop.set_defaults(handler=crop_command)

    verify = commands.add_parser("verify", help="생성된 크롭을 별도 Codex 비전 패스로 검수")
    verify.add_argument("--run-dir", required=True)
    verify.add_argument("--mode", choices=("review", "all"), default="all")
    verify.add_argument("--workers", type=int, default=2)
    verify.add_argument("--model", default=None)
    verify.add_argument(
        "--reasoning-effort", choices=("low", "medium", "high"), default="medium"
    )
    verify.add_argument("--timeout", type=int, default=600)
    verify.add_argument("--retries", type=int, default=1)
    verify.add_argument("--force", action="store_true")
    verify.add_argument("--confidence-threshold", type=float, default=0.82)
    verify.set_defaults(handler=verify_command)

    repair = commands.add_parser("repair", help="경계 QA 실패 페이지를 고난도 비전으로 교정")
    repair.add_argument("--run-dir", required=True)
    repair.add_argument(
        "--pages",
        default="",
        help="선택 교정할 1-based 페이지(all, 20,34 등); 생략 시 검토 큐에서 자동 선택",
    )
    repair.add_argument("--attempts", type=int, default=1)
    repair.add_argument("--workers", type=int, default=2)
    repair.add_argument("--model", default=None)
    repair.add_argument(
        "--repair-reasoning-effort", choices=("medium", "high", "xhigh"), default="high"
    )
    repair.add_argument("--timeout", type=int, default=600)
    repair.add_argument("--retries", type=int, default=1)
    repair.add_argument("--force", action="store_true")
    repair.add_argument("--confidence-threshold", type=float, default=0.82)
    repair.add_argument("--verify", choices=("none", "review", "all"), default="all")
    repair.add_argument(
        "--verification-reasoning-effort",
        choices=("low", "medium", "high"),
        default="medium",
    )
    repair.set_defaults(handler=repair_command)

    benchmark = commands.add_parser("benchmark", help="출판사 층화 골든 페이지 회귀 실행")
    benchmark.add_argument("plan")
    benchmark.add_argument("--out", default=str(ASTRA.crop_root / "runs/benchmark"))
    benchmark.add_argument("--case-regex", default="")
    benchmark.add_argument("--max-cases", type=int, default=0)
    add_detection_options(benchmark)
    benchmark.add_argument("--unit", choices=("atomic", "main", "both"), default="both")
    benchmark.add_argument("--emit-groups", action="store_true")
    benchmark.add_argument("--crop-dpi", type=int, default=300)
    benchmark.add_argument("--padding", type=int, default=8)
    benchmark.add_argument("--format", choices=("png", "jpg"), default="png")
    benchmark.add_argument("--answer-space", choices=("include", "exclude"), default="exclude")
    benchmark.add_argument("--sidecars", choices=("include", "exclude"), default="exclude")
    benchmark.add_argument("--solution-mask", choices=("white", "none"), default="none")
    benchmark.add_argument("--repair-attempts", type=int, default=1)
    benchmark.add_argument(
        "--repair-reasoning-effort", choices=("medium", "high", "xhigh"), default="high"
    )
    benchmark.add_argument("--verify", choices=("none", "review", "all"), default="none")
    benchmark.add_argument(
        "--verification-reasoning-effort",
        choices=("low", "medium", "high"),
        default="medium",
    )
    benchmark.set_defaults(handler=benchmark_command)

    run = commands.add_parser("run", help="검출부터 최종 크롭까지 실행")
    run.add_argument("source")
    run.add_argument("--out", default=str(ASTRA.crop_root / "runs"))
    add_detection_options(run)
    # confidence-threshold is already supplied by detection options.
    run.add_argument(
        "--unit",
        choices=("atomic", "main", "both"),
        default="atomic",
        help="atomic=소문항별 자급형, main=대문항 전체, both=둘 다",
    )
    run.add_argument("--emit-groups", action="store_true")
    run.add_argument("--crop-dpi", type=int, default=300)
    run.add_argument("--padding", type=int, default=8)
    run.add_argument("--format", choices=("png", "jpg"), default="png")
    run.add_argument("--answer-space", choices=("include", "exclude"), default="exclude")
    run.add_argument("--sidecars", choices=("include", "exclude"), default="exclude")
    run.add_argument("--solution-mask", choices=("white", "none"), default="none")
    run.add_argument(
        "--repair-attempts",
        type=int,
        default=1,
        help="경계 QA 실패 페이지 고난도 교정 횟수 (기본 1)",
    )
    run.add_argument(
        "--repair-reasoning-effort",
        choices=("medium", "high", "xhigh"),
        default="high",
    )
    run.add_argument(
        "--verify",
        choices=("none", "review", "all"),
        default="all",
        help="별도 VLM 크롭 검수 범위 (기본 review)",
    )
    run.add_argument(
        "--verification-reasoning-effort",
        choices=("low", "medium", "high"),
        default="medium",
    )
    run.set_defaults(handler=lambda args: detect_command(args, crop_after=True))
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_parsed_paths(args)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("사용자 중단", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"오류: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
