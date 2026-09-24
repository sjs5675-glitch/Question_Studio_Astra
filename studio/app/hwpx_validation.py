"""Read-only structural validation for generated HWPX documents.

This module deliberately knows nothing about renderer implementation details.  It
checks the emitted ZIP/XML package against a build-receipt contract and runs
optional external validators against disposable copies of the output.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree


REPORT_VERSION = "0.1.0"
MIMETYPE = b"application/hwp+zip"

HP_NS = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HH_NS = "http://www.hancom.co.kr/hwpml/2011/head"
HS_NS = "http://www.hancom.co.kr/hwpml/2011/section"
HA_NS = "http://www.hancom.co.kr/hwpml/2011/app"
HV_NS = "http://www.hancom.co.kr/hwpml/2011/version"
OPF_NS = "http://www.idpf.org/2007/opf/"
OCF_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
ODF_NS = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

REQUIRED_MEMBERS = frozenset(
    {
        "mimetype",
        "version.xml",
        "settings.xml",
        "Contents/header.xml",
        "Contents/content.hpf",
        "META-INF/container.xml",
        "META-INF/manifest.xml",
    }
)
REQUIRED_ROOTS = {
    "version.xml": f"{{{HV_NS}}}HCFVersion",
    "settings.xml": f"{{{HA_NS}}}HWPApplicationSetting",
    "Contents/header.xml": f"{{{HH_NS}}}head",
    "Contents/content.hpf": f"{{{OPF_NS}}}package",
    "META-INF/container.xml": f"{{{OCF_NS}}}container",
    "META-INF/manifest.xml": f"{{{ODF_NS}}}manifest",
    "META-INF/container.rdf": f"{{{RDF_NS}}}RDF",
}
SECTION_RE = re.compile(r"Contents/section(\d+)\.xml\Z")
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")

DIRECT_COUNT_ALIASES = {
    "section": "sections",
    "sections": "sections",
    "paragraph": "paragraphs",
    "paragraphs": "paragraphs",
    "equation": "equations",
    "equations": "equations",
    "picture": "pictures",
    "pictures": "pictures",
    "figure": "pictures",
    "figures": "pictures",
    "table": "tables",
    "tables": "tables",
    "bindata": "bin_data_members",
    "bin_data": "bin_data_members",
    "bin_data_members": "bin_data_members",
    "hpf_refs": "hpf_refs",
    "hpf_image_refs": "hpf_image_refs",
    "picture_refs": "picture_refs",
    "endnote": "endnotes",
    "endnotes": "endnotes",
}
SEMANTIC_COUNT_ALIASES = {
    "equation": "equations",
    "block.figure": "pictures",
}
UNVERIFIABLE_SEMANTIC_COUNTS = frozenset(
    {
        "block.paragraph",
        "block.bogi",
        "block.choices",
        "block.box",
        "block.table",
        "answer.choice",
        "fallback.source_crop",
    }
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _issue(code: str, path: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "path": path, "message": message, **details}


def _safe_member(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name or name.startswith(("/", "\\")):
        return False
    if re.match(r"^[A-Za-z]:", name):
        return False
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and name.rstrip("/") == path.as_posix()
    )


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK


def _is_image_reference(href: str, media_type: str) -> bool:
    return media_type.lower().startswith("image/") or href.lower().endswith(
        (".png", ".bmp", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
    )


def _section_sort_key(name: str) -> int:
    match = SECTION_RE.fullmatch(name)
    return int(match.group(1)) if match else sys.maxsize


def _dedupe_and_sort(issues: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for issue in issues:
        value = dict(issue)
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        unique[key] = value
    return sorted(
        unique.values(),
        key=lambda value: (
            str(value.get("code", "")),
            str(value.get("path", "")),
            str(value.get("message", "")),
        ),
    )


def _receipt_output(receipt: Mapping[str, Any] | None, kind: str) -> Mapping[str, Any]:
    if not isinstance(receipt, Mapping):
        return {}
    outputs = receipt.get("outputs")
    if not isinstance(outputs, list):
        return {}
    return next(
        (
            output
            for output in outputs
            if isinstance(output, Mapping) and output.get("document_kind") == kind
        ),
        {},
    )


def _add_text_specs(
    target: list[tuple[str, str]],
    value: Any,
    *,
    selector: str,
) -> None:
    if isinstance(value, str):
        if value:
            target.append((selector, value))
    elif isinstance(value, Mapping):
        for key in sorted(value, key=str):
            _add_text_specs(target, value[key], selector=str(key))
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            if isinstance(item, Mapping) and "selector" in item:
                nested = item.get("exact_texts", item.get("exact_text"))
                _add_text_specs(target, nested, selector=str(item["selector"]))
            else:
                _add_text_specs(target, item, selector=selector)


def _add_hash_specs(
    target: dict[str, set[str]],
    value: Any,
    *,
    selector: str,
) -> None:
    if isinstance(value, str):
        if SHA256_RE.fullmatch(value):
            target[selector].add(value.lower())
    elif isinstance(value, Mapping):
        for key in sorted(value, key=str):
            _add_hash_specs(target, value[key], selector=str(key))
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            _add_hash_specs(target, item, selector=selector)


def _forbidden_contract(
    output: Mapping[str, Any],
    forbidden_texts: Any,
    forbidden_selectors: Mapping[str, Any] | None,
) -> tuple[list[tuple[str, str]], dict[str, set[str]], list[str]]:
    texts: list[tuple[str, str]] = []
    hashes: dict[str, set[str]] = defaultdict(set)
    selector_only: list[str] = []

    _add_text_specs(texts, forbidden_texts, selector="explicit")
    if isinstance(forbidden_selectors, Mapping):
        _add_text_specs(texts, forbidden_selectors, selector="explicit")

    for holder in (output, output.get("validation", {})):
        if not isinstance(holder, Mapping):
            continue
        _add_text_specs(
            texts,
            holder.get("forbidden_exact_texts", ()),
            selector="receipt",
        )
        selector_value = holder.get("forbidden_selectors")
        if isinstance(selector_value, Mapping):
            _add_text_specs(texts, selector_value, selector="receipt")
        elif isinstance(selector_value, list):
            selector_only.extend(str(value) for value in selector_value if isinstance(value, str))
        _add_hash_specs(
            hashes,
            holder.get("forbidden_exact_text_sha256", ()),
            selector="receipt",
        )

    # Preserve selector identity but discard duplicate exact strings.
    texts = sorted(set(texts), key=lambda value: (value[0], value[1]))
    return texts, hashes, sorted(set(selector_only))


def _count_contract(
    output: Mapping[str, Any],
    explicit: Mapping[str, int] | None,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> list[tuple[str, str, int]]:
    checks: list[tuple[str, str, int]] = []

    def add(values: Any, aliases: Mapping[str, str], source: str, *, strict: bool) -> None:
        if not isinstance(values, Mapping):
            return
        for name in sorted(values, key=str):
            expected = values[name]
            actual_name = aliases.get(str(name))
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                (errors if strict else warnings).append(
                    _issue(
                        "invalid_expected_count",
                        f"receipt.{source}.{name}",
                        "Expected count must be a non-negative integer.",
                    )
                )
            elif actual_name:
                checks.append((str(name), actual_name, expected))
            elif str(name) in UNVERIFIABLE_SEMANTIC_COUNTS:
                warnings.append(
                    _issue(
                        "semantic_count_uses_renderer_expansion",
                        f"receipt.{source}.{name}",
                        "This semantic count is not one-to-one with HWPX elements; renderer_counts is used instead.",
                    )
                )
            elif strict:
                errors.append(
                    _issue(
                        "unsupported_expected_count",
                        f"receipt.{source}.{name}",
                        "No structural HWPX counter is defined for this key.",
                    )
                )

    if explicit is not None:
        add(explicit, DIRECT_COUNT_ALIASES | SEMANTIC_COUNT_ALIASES, "expected_counts", strict=True)
    else:
        renderer_counts = output.get("renderer_counts")
        if isinstance(renderer_counts, Mapping):
            add(renderer_counts, DIRECT_COUNT_ALIASES, "renderer_counts", strict=True)
        add(output.get("expected_counts"), SEMANTIC_COUNT_ALIASES, "expected_counts", strict=False)
    return checks


def _default_external_tools() -> dict[str, Path]:
    scripts = Path.home() / ".codex" / "skills" / "assemble-hwpx" / "scripts"
    return {
        "assemble_hwpx_validate": scripts / "validate_hwpx.py",
        "assemble_hwpx_inspect": scripts / "inspect_hwpx.py",
    }


def _external_tools(
    configured: Mapping[str, Path | str] | Iterable[Path | str] | None,
    selected_root: Path | str | None,
) -> list[tuple[str, Path]]:
    if configured is None:
        result = _default_external_tools()
    elif isinstance(configured, Mapping):
        result = {str(name): Path(path) for name, path in configured.items()}
    else:
        result = {Path(path).stem: Path(path) for path in configured}
    if selected_root is not None:
        result["selected_root_validate"] = (
            Path(selected_root) / "resources" / "hwpx_scripts" / "validate.py"
        )
    return sorted(result.items())


def _sanitize_external_text(text: str, paths: Iterable[Path], limit: int = 20000) -> str:
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    replacements: set[str] = set()
    for path in paths:
        raw = str(path)
        replacements.update({raw, raw.replace("\\", "/"), raw.replace("\\", "\\\\")})
    replacements.add(str(Path.home()))
    for raw in sorted((item for item in replacements if item), key=len, reverse=True):
        value = value.replace(raw, "<path>")
    value = value.strip()
    return value if len(value) <= limit else value[:limit] + "\n<truncated>"


def _run_external_validators(
    path: Path,
    tools: list[tuple[str, Path]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    source_hash = _sha256_file(path)

    for name, script in tools:
        base = {
            "name": name,
            "available": script.is_file(),
            "command": [Path(sys.executable).name, script.name, "<hwpx>"],
        }
        if not script.is_file():
            results.append({**base, "ok": None, "returncode": None, "stdout": "", "stderr": ""})
            warnings.append(
                _issue(
                    "external_tool_unavailable",
                    f"external_tools.{name}",
                    "Optional HWPX validation tool is not available.",
                )
            )
            continue

        with tempfile.TemporaryDirectory(prefix="qjs-hwpx-validation-") as temporary:
            copy_path = Path(temporary) / path.name
            shutil.copyfile(path, copy_path)
            try:
                completed = subprocess.run(
                    [sys.executable, str(script), str(copy_path)],
                    cwd=script.parent,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    check=False,
                )
                stdout = _sanitize_external_text(completed.stdout, (path, copy_path, script))
                stderr = _sanitize_external_text(completed.stderr, (path, copy_path, script))
                mutated = not copy_path.is_file() or _sha256_file(copy_path) != source_hash
                ok = completed.returncode == 0 and not mutated
                results.append(
                    {
                        **base,
                        "ok": ok,
                        "returncode": completed.returncode,
                        "mutated_copy": mutated,
                        "stdout": stdout,
                        "stderr": stderr,
                    }
                )
                if completed.returncode != 0:
                    errors.append(
                        _issue(
                            "external_tool_failed",
                            f"external_tools.{name}",
                            "External HWPX validation tool reported failure.",
                            returncode=completed.returncode,
                        )
                    )
                if mutated:
                    errors.append(
                        _issue(
                            "external_tool_modified_copy",
                            f"external_tools.{name}",
                            "External tool modified its disposable validation copy.",
                        )
                    )
            except (OSError, subprocess.SubprocessError) as exc:
                message = _sanitize_external_text(str(exc), (path, copy_path, script))
                results.append(
                    {
                        **base,
                        "ok": False,
                        "returncode": None,
                        "mutated_copy": False,
                        "stdout": "",
                        "stderr": message,
                    }
                )
                errors.append(
                    _issue(
                        "external_tool_error",
                        f"external_tools.{name}",
                        "External HWPX validation tool could not be run.",
                    )
                )
    return results, errors, warnings


def _finalize(report: dict[str, Any]) -> dict[str, Any]:
    report["errors"] = _dedupe_and_sort(report["errors"])
    report["warnings"] = _dedupe_and_sort(report["warnings"])
    report["ok"] = not report["errors"]
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    report["report_sha256"] = _sha256_bytes(payload.encode("utf-8"))
    return report


def report_json(report: Mapping[str, Any], *, indent: int | None = 2) -> str:
    """Serialize a validation report deterministically."""

    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=indent)


def validate_hwpx_output(
    path: Path | str,
    document_kind: str,
    *,
    expected_counts: Mapping[str, int] | None = None,
    forbidden_texts: Any = (),
    forbidden_selectors: Mapping[str, Any] | None = None,
    build_receipt: Mapping[str, Any] | None = None,
    external_tool_paths: Mapping[str, Path | str] | Iterable[Path | str] | None = None,
    selected_root: Path | str | None = None,
    run_external_tools: bool = True,
) -> dict[str, Any]:
    """Return a deterministic, JSON-ready report for one generated HWPX.

    ``forbidden_texts`` and selector values must be exact source strings.  A
    selector name by itself is intentionally never converted into a keyword
    heuristic.
    """

    if document_kind not in {"question", "explanation", "combined"}:
        raise ValueError("document_kind must be 'question', 'explanation', or 'combined'")
    source = Path(path)
    output = _receipt_output(build_receipt, document_kind)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    checks = _count_contract(output, expected_counts, errors, warnings)
    exact_texts, declared_hashes, selector_only = _forbidden_contract(
        output, forbidden_texts, forbidden_selectors
    )
    for selector in selector_only:
        warnings.append(
            _issue(
                "forbidden_selector_without_exact_text",
                f"forbidden_selectors.{selector}",
                "Selector has no exact source text, so no leakage heuristic was applied.",
            )
        )

    counts = {
        "sections": 0,
        "paragraphs": 0,
        "equations": 0,
        "pictures": 0,
        "tables": 0,
        "bin_data_members": 0,
        "hpf_refs": 0,
        "hpf_image_refs": 0,
        "picture_refs": 0,
        "binary_item_refs": 0,
        "visible_text_nodes": 0,
        "equation_scripts": 0,
        "endnotes": 0,
    }
    report: dict[str, Any] = {
        "schema_version": REPORT_VERSION,
        "document_kind": document_kind,
        "file": {
            "name": source.name,
            "sha256": _sha256_file(source) if source.is_file() else None,
            "size_bytes": source.stat().st_size if source.is_file() else None,
        },
        "ok": False,
        "errors": errors,
        "warnings": warnings,
        "counts": counts,
        "expected_count_checks": [],
        "references": {
            "picture_binary_item_ids": [],
            "binary_item_ids": [],
            "hpf_image_ids": [],
            "hpf_image_hrefs": [],
        },
        "xml_roots": {},
        "extracted": {"visible_text": "", "equation_scripts": []},
        "forbidden_contract": {
            "selectors": sorted({selector for selector, _ in exact_texts} | set(selector_only)),
            "exact_text_sha256": sorted(
                {
                    _sha256_bytes(text.encode("utf-8"))
                    for _, text in exact_texts
                }
                | {value for values in declared_hashes.values() for value in values}
            ),
        },
        "external_tools": [],
    }
    if not source.is_file():
        errors.append(_issue("file_not_found", "file", "Generated HWPX file does not exist."))
        return _finalize(report)
    if source.suffix.lower() != ".hwpx":
        errors.append(_issue("wrong_extension", "file.name", "Generated file must use .hwpx."))
    if isinstance(build_receipt, Mapping) and not output:
        errors.append(
            _issue(
                "receipt_output_missing",
                "receipt.outputs",
                "Build receipt has no output contract for this document kind.",
            )
        )
    if output:
        declared_path = output.get("path")
        if isinstance(declared_path, str) and Path(declared_path).name != source.name:
            errors.append(
                _issue(
                    "receipt_path_mismatch",
                    "receipt.outputs.path",
                    "Generated filename does not match the build receipt.",
                    expected=Path(declared_path).name,
                    actual=source.name,
                )
            )
        declared_sha = output.get("sha256")
        if isinstance(declared_sha, str) and SHA256_RE.fullmatch(declared_sha):
            if declared_sha.lower() != report["file"]["sha256"]:
                errors.append(
                    _issue(
                        "receipt_sha256_mismatch",
                        "receipt.outputs.sha256",
                        "Generated file SHA-256 does not match the build receipt.",
                        expected=declared_sha.lower(),
                        actual=report["file"]["sha256"],
                    )
                )
        declared_size = output.get("size_bytes")
        if isinstance(declared_size, int) and not isinstance(declared_size, bool):
            if declared_size != report["file"]["size_bytes"]:
                errors.append(
                    _issue(
                        "receipt_size_mismatch",
                        "receipt.outputs.size_bytes",
                        "Generated file size does not match the build receipt.",
                        expected=declared_size,
                        actual=report["file"]["size_bytes"],
                    )
                )
    if not zipfile.is_zipfile(source):
        errors.append(_issue("not_zip_backed", "file", "Generated HWPX is not a ZIP package."))
        return _finalize(report)

    parsed: dict[str, ElementTree.Element] = {}
    names: list[str] = []
    section_names: list[str] = []
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        name_set = set(names)
        duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
        if duplicates:
            errors.append(
                _issue("duplicate_zip_members", "zip", "ZIP contains duplicate member names.", members=duplicates)
            )
        casefolded: dict[str, list[str]] = defaultdict(list)
        for name in names:
            casefolded[name.casefold()].append(name)
        collisions = sorted(
            values for values in casefolded.values() if len(set(values)) > 1
        )
        if collisions:
            errors.append(
                _issue("case_colliding_zip_members", "zip", "ZIP member names collide on case-insensitive filesystems.", members=collisions)
            )
        for info in infos:
            if not _safe_member(info.filename):
                errors.append(
                    _issue("unsafe_zip_member", info.filename, "ZIP member path is unsafe.")
                )
            if _is_symlink(info):
                errors.append(
                    _issue("symlink_zip_member", info.filename, "ZIP symlink members are forbidden.")
                )
            if info.flag_bits & 0x1:
                errors.append(
                    _issue("encrypted_zip_member", info.filename, "Encrypted ZIP members are forbidden.")
                )

        missing = sorted(REQUIRED_MEMBERS - name_set)
        if missing:
            errors.append(
                _issue("missing_required_members", "zip", "Required HWPX members are missing.", members=missing)
            )
        if not infos or infos[0].filename != "mimetype":
            errors.append(_issue("mimetype_not_first", "zip[0]", "mimetype must be the first ZIP member."))
        elif infos[0].compress_type != zipfile.ZIP_STORED:
            errors.append(_issue("mimetype_compressed", "mimetype", "mimetype must use ZIP_STORED."))
        if "mimetype" in name_set:
            try:
                actual_mime = archive.read("mimetype")
                if actual_mime != MIMETYPE:
                    errors.append(
                        _issue("wrong_mimetype", "mimetype", "HWPX mimetype content is invalid.")
                    )
            except (KeyError, OSError, zipfile.BadZipFile) as exc:
                errors.append(_issue("zip_crc_failure", "mimetype", f"Cannot read ZIP member: {exc}"))

        try:
            bad_member = archive.testzip()
        except (OSError, zipfile.BadZipFile) as exc:
            bad_member = str(exc)
        if bad_member:
            errors.append(
                _issue("zip_crc_failure", str(bad_member), "ZIP member failed its CRC check.")
            )

        section_names = sorted(
            (name for name in name_set if SECTION_RE.fullmatch(name)), key=_section_sort_key
        )
        counts["sections"] = len(section_names)
        counts["bin_data_members"] = len(
            [name for name in name_set if name.startswith("BinData/") and not name.endswith("/")]
        )
        if not section_names:
            errors.append(_issue("missing_section", "Contents", "No Contents/sectionN.xml member exists."))

        for name in sorted(name_set):
            lower = name.lower()
            if lower.endswith(".svg"):
                errors.append(_issue("svg_member", name, "SVG members are forbidden."))
            if "macro" in lower or "endnote" in lower or "/scripts/" in f"/{lower}/":
                errors.append(_issue("forbidden_member", name, "Macro/script/endnote members are forbidden."))
            if not lower.endswith((".xml", ".hpf", ".rdf")):
                continue
            try:
                raw = archive.read(name)
            except (KeyError, OSError, zipfile.BadZipFile) as exc:
                errors.append(_issue("unreadable_member", name, f"Cannot read XML member: {exc}"))
                continue
            lowered = raw.lower()
            if b"http://www.w3.org/2000/svg" in lowered or re.search(rb"<(?:svg:)?svg\b", lowered):
                errors.append(_issue("inline_svg", name, "Inline SVG XML is forbidden."))
            if b"<!doctype" in lowered or b"<!entity" in lowered:
                errors.append(_issue("xml_doctype", name, "DOCTYPE/entity declarations are forbidden."))
            try:
                parsed[name] = ElementTree.fromstring(raw)
            except ElementTree.ParseError as exc:
                errors.append(_issue("malformed_xml", name, f"XML parsing failed: {exc}"))

    report["xml_roots"] = {name: parsed[name].tag for name in sorted(parsed)}
    for name, root in sorted(parsed.items()):
        parents = {child: parent for parent in root.iter() for child in parent}
        for element in root.iter():
            local = _local_name(element.tag).lower()
            namespace = _namespace(element.tag).lower()
            parent = parents.get(element)
            is_equation_script = (
                local == "script"
                and namespace == HP_NS.lower()
                and parent is not None
                and _local_name(parent.tag).lower() == "equation"
            )
            native_endnote = (
                document_kind == "combined"
                and name in section_names
                and local in {"endnote", "endnotepr"}
            )
            if ("endnote" in local and not native_endnote) or "macro" in local or (
                local in {"script", "scripts"} and not is_equation_script
            ):
                errors.append(
                    _issue(
                        "forbidden_xml_element",
                        name,
                        "Macro/script/endnote XML elements are forbidden.",
                        element=_local_name(element.tag),
                    )
                )
            if "svg" in namespace or local == "svg":
                errors.append(_issue("inline_svg", name, "Inline SVG XML is forbidden."))

    for name, expected_root in REQUIRED_ROOTS.items():
        root = parsed.get(name)
        if name in names and root is not None and root.tag != expected_root:
            errors.append(
                _issue(
                    "wrong_xml_root",
                    name,
                    "HWPX XML member has the wrong root element or namespace.",
                    expected=expected_root,
                    actual=root.tag,
                )
            )
    for name in section_names:
        root = parsed.get(name)
        expected = f"{{{HS_NS}}}sec"
        if root is not None and root.tag != expected:
            errors.append(
                _issue("wrong_xml_root", name, "Section XML has the wrong root element or namespace.", expected=expected, actual=root.tag)
            )

    header = parsed.get("Contents/header.xml")
    if header is not None:
        declared = header.get("secCnt")
        try:
            declared_count = int(declared) if declared is not None else None
        except ValueError:
            declared_count = None
        if declared_count != counts["sections"]:
            errors.append(
                _issue(
                    "section_count_mismatch",
                    "Contents/header.xml@secCnt",
                    "Header section count does not match section members.",
                    expected=counts["sections"],
                    actual=declared,
                )
            )

    manifest_by_id: dict[str, tuple[str, str]] = {}
    manifest_ids: Counter[str] = Counter()
    hpf = parsed.get("Contents/content.hpf")
    if hpf is not None:
        for element in hpf.iter():
            if _local_name(element.tag) != "item":
                continue
            counts["hpf_refs"] += 1
            item_id = element.get("id", "")
            href = element.get("href", "")
            media_type = element.get("media-type", "")
            if item_id:
                manifest_ids[item_id] += 1
                manifest_by_id.setdefault(item_id, (href, media_type))
            if href and not _safe_member(href):
                errors.append(_issue("unsafe_hpf_reference", "Contents/content.hpf", "HPF href is unsafe."))
            if href and href not in names:
                errors.append(
                    _issue("missing_hpf_target", f"Contents/content.hpf#{item_id}", "HPF manifest target is missing.", href=href)
                )
            is_image = _is_image_reference(href, media_type)
            if is_image:
                counts["hpf_image_refs"] += 1
                if href.lower().endswith(".svg") or "svg" in media_type.lower():
                    errors.append(_issue("svg_manifest_reference", f"Contents/content.hpf#{item_id}", "SVG manifest references are forbidden."))
            if "macro" in media_type.lower() or "endnote" in media_type.lower():
                errors.append(_issue("forbidden_manifest_reference", f"Contents/content.hpf#{item_id}", "Macro/endnote manifest references are forbidden."))
        duplicate_manifest_ids = sorted(key for key, count in manifest_ids.items() if count > 1)
        if duplicate_manifest_ids:
            errors.append(
                _issue("duplicate_manifest_ids", "Contents/content.hpf", "HPF manifest IDs must be unique.", ids=duplicate_manifest_ids)
            )

    visible_paragraphs: list[str] = []
    scripts: list[str] = []
    identity_locations: dict[str, list[str]] = defaultdict(list)
    z_order_locations: dict[str, list[str]] = defaultdict(list)
    binary_refs: list[str] = []
    picture_refs: list[str] = []
    endnote_controls: list[tuple[str, ElementTree.Element]] = []
    endnote_properties: list[tuple[str, ElementTree.Element]] = []
    for section_name in section_names:
        section = parsed.get(section_name)
        if section is None:
            continue
        paragraphs = list(section.iter(f"{{{HP_NS}}}p"))
        equations = list(section.iter(f"{{{HP_NS}}}equation"))
        pictures = list(section.iter(f"{{{HP_NS}}}pic"))
        tables = list(section.iter(f"{{{HP_NS}}}tbl"))
        endnotes = list(section.iter(f"{{{HP_NS}}}endNote"))
        endnote_prs = list(section.iter(f"{{{HP_NS}}}endNotePr"))
        counts["paragraphs"] += len(paragraphs)
        counts["equations"] += len(equations)
        counts["pictures"] += len(pictures)
        counts["tables"] += len(tables)
        counts["endnotes"] += len(endnotes)
        endnote_controls.extend((section_name, node) for node in endnotes)
        endnote_properties.extend((section_name, node) for node in endnote_prs)
        for table_index, table in enumerate(tables):
            try:
                column_count = int(table.get("colCnt", "0"))
                row_count = int(table.get("rowCnt", "0"))
            except (TypeError, ValueError):
                column_count = row_count = 0
            rows = table.findall(f"{{{HP_NS}}}tr")
            if column_count < 1 or row_count != len(rows):
                errors.append(
                    _issue(
                        "invalid_table_dimensions",
                        f"{section_name}:tbl[{table_index}]",
                        "Native table rowCnt/colCnt does not match its grid.",
                    )
                )
                continue
            active = [0] * column_count
            for row_index, row in enumerate(rows):
                covered = [value > 0 for value in active]
                updates: dict[int, int] = {}
                cursor = 0
                row_valid = True
                for cell_index, cell in enumerate(row.findall(f"{{{HP_NS}}}tc")):
                    while cursor < column_count and covered[cursor]:
                        cursor += 1
                    address = cell.find(f"{{{HP_NS}}}cellAddr")
                    span = cell.find(f"{{{HP_NS}}}cellSpan")
                    try:
                        column = int(address.get("colAddr", "-1"))
                        source_row = int(address.get("rowAddr", "-1"))
                        col_span = int(span.get("colSpan", "0"))
                        row_span = int(span.get("rowSpan", "0"))
                    except (AttributeError, TypeError, ValueError):
                        row_valid = False
                        break
                    if (
                        (source_row, column) != (row_index, cursor)
                        or col_span < 1
                        or row_span < 1
                        or row_index + row_span > row_count
                        or cursor + col_span > column_count
                        or any(covered[cursor : cursor + col_span])
                    ):
                        row_valid = False
                        break
                    for occupied in range(cursor, cursor + col_span):
                        covered[occupied] = True
                        if row_span > 1:
                            updates[occupied] = max(
                                updates.get(occupied, 0), row_span - 1
                            )
                    cursor += col_span
                if not row_valid or not all(covered):
                    errors.append(
                        _issue(
                            "invalid_table_grid",
                            f"{section_name}:tbl[{table_index}].row[{row_index}]",
                            "Native table cellAddr/cellSpan coverage is invalid.",
                        )
                    )
                active = [max(0, value - 1) for value in active]
                for column, value in updates.items():
                    active[column] = max(active[column], value)
            if any(active):
                errors.append(
                    _issue(
                        "invalid_table_grid",
                        f"{section_name}:tbl[{table_index}]",
                        "Native table rowSpan extends beyond the final row.",
                    )
                )
        for paragraph in paragraphs:
            text = "".join(node.text or "" for node in paragraph.iter(f"{{{HP_NS}}}t"))
            counts["visible_text_nodes"] += sum(1 for _ in paragraph.iter(f"{{{HP_NS}}}t"))
            if text:
                visible_paragraphs.append(text)
        for equation in equations:
            script = equation.find(f"{{{HP_NS}}}script")
            scripts.append(script.text or "" if script is not None else "")
        for element in section.iter():
            location = f"{section_name}:{_local_name(element.tag)}"
            for attribute in ("id", "instid", "instId"):
                value = element.get(attribute)
                if value:
                    identity_locations[value].append(f"{location}@{attribute}")
            z_order = element.get("zOrder")
            if z_order is not None:
                z_order_locations[z_order].append(f"{location}@zOrder")
            ref = element.get("binaryItemIDRef")
            if ref:
                binary_refs.append(ref)
                if _local_name(element.tag) == "img":
                    picture_refs.append(ref)
        for index, picture in enumerate(pictures):
            if not any(node.get("binaryItemIDRef") for node in picture.iter()):
                errors.append(
                    _issue("picture_missing_binary_reference", f"{section_name}:pic[{index}]", "Picture has no binaryItemIDRef.")
                )

    for name, root in sorted(parsed.items()):
        if name in section_names:
            continue
        binary_refs.extend(
            ref
            for element in root.iter()
            if (ref := element.get("binaryItemIDRef"))
        )

    counts["equation_scripts"] = len(scripts)
    counts["binary_item_refs"] = len(binary_refs)
    counts["picture_refs"] = len(picture_refs)
    report["extracted"] = {
        "visible_text": "\n".join(visible_paragraphs),
        "equation_scripts": scripts,
    }
    duplicate_ids = sorted(key for key, locations in identity_locations.items() if len(locations) > 1)
    if duplicate_ids:
        errors.append(
            _issue("duplicate_object_ids", "Contents/section*.xml", "Object id/instid values must be build-local unique.", ids=duplicate_ids)
        )
    duplicate_z_orders = sorted(key for key, locations in z_order_locations.items() if len(locations) > 1)
    if duplicate_z_orders:
        errors.append(
            _issue("duplicate_z_orders", "Contents/section*.xml", "Shape zOrder values must be build-local unique.", values=duplicate_z_orders)
        )

    if document_kind == "combined":
        if len(endnote_properties) != 1:
            errors.append(
                _issue(
                    "invalid_endnote_properties",
                    "Contents/section*.xml",
                    "Combined HWPX must contain exactly one endNotePr.",
                    actual=len(endnote_properties),
                )
            )
        else:
            section_name, properties = endnote_properties[0]
            placement = properties.find(f"{{{HP_NS}}}placement")
            if placement is None or placement.get("place") != "END_OF_DOCUMENT":
                errors.append(
                    _issue(
                        "invalid_endnote_placement",
                        f"{section_name}:endNotePr",
                        "Native endnotes must be placed at END_OF_DOCUMENT.",
                    )
                )
        if not endnote_controls:
            errors.append(
                _issue(
                    "missing_endnotes",
                    "Contents/section*.xml",
                    "Combined HWPX must contain at least one native endNote.",
                )
            )
        actual_numbers: list[int | None] = []
        for index, (section_name, endnote) in enumerate(endnote_controls, start=1):
            try:
                number = int(endnote.get("number", ""))
            except (TypeError, ValueError):
                number = None
            actual_numbers.append(number)
            sublists = endnote.findall(f"{{{HP_NS}}}subList")
            location = f"{section_name}:endNote[{index - 1}]"
            if len(sublists) != 1:
                errors.append(
                    _issue(
                        "invalid_endnote_sublist",
                        location,
                        "Each native endNote must contain exactly one subList.",
                    )
                )
                continue
            sublist = sublists[0]
            auto_numbers = [
                node
                for node in sublist.iter(f"{{{HP_NS}}}autoNum")
                if node.get("numType") == "ENDNOTE"
            ]
            if len(auto_numbers) != 1 or auto_numbers[0].get("num") != str(number):
                errors.append(
                    _issue(
                        "invalid_endnote_autonum",
                        location,
                        "Each native endNote must contain one matching ENDNOTE autoNum.",
                    )
                )
            text = "".join(node.text or "" for node in sublist.iter(f"{{{HP_NS}}}t"))
            answer_at, explanation_at = text.find("[답]"), text.find("[풀이]")
            if answer_at < 0 or explanation_at <= answer_at:
                errors.append(
                    _issue(
                        "invalid_endnote_sections",
                        location,
                        "Native endNote content must contain [답] followed by [풀이].",
                    )
                )
        expected_numbers = list(range(1, len(endnote_controls) + 1))
        if actual_numbers != expected_numbers:
            errors.append(
                _issue(
                    "invalid_endnote_numbers",
                    "Contents/section*.xml",
                    "Native endNote numbers must be continuous from 1 in document order.",
                    expected=expected_numbers,
                    actual=actual_numbers,
                )
            )

    binary_ref_set = set(binary_refs)
    picture_ref_set = set(picture_refs)
    image_manifest = {
        item_id: href
        for item_id, (href, media_type) in manifest_by_id.items()
        if _is_image_reference(href, media_type)
    }
    for ref in sorted(binary_ref_set - set(manifest_by_id)):
        errors.append(
            _issue("missing_image_reference", "Contents/section*.xml", "binaryItemIDRef has no HPF manifest item.", binary_item_id=ref)
        )
    for ref in sorted(picture_ref_set & set(manifest_by_id)):
        href, media_type = manifest_by_id[ref]
        if not _is_image_reference(href, media_type):
            errors.append(
                _issue(
                    "picture_reference_not_image",
                    "Contents/section*.xml",
                    "Picture binaryItemIDRef points to a non-image HPF item.",
                    binary_item_id=ref,
                )
            )
    for item_id in sorted(set(image_manifest) - binary_ref_set):
        errors.append(
            _issue("orphan_hpf_image_reference", f"Contents/content.hpf#{item_id}", "HPF image item is not referenced by document XML.", href=image_manifest[item_id])
        )
    manifest_bin_paths = {
        href for href, _media_type in manifest_by_id.values() if href.startswith("BinData/")
    }
    actual_bin_paths = {name for name in names if name.startswith("BinData/") and not name.endswith("/")}
    for member in sorted(actual_bin_paths - manifest_bin_paths):
        errors.append(
            _issue("orphan_bindata_member", member, "BinData member has no HPF image manifest item.")
        )
    report["references"] = {
        "picture_binary_item_ids": sorted(picture_ref_set),
        "binary_item_ids": sorted(binary_ref_set),
        "hpf_image_ids": sorted(image_manifest),
        "hpf_image_hrefs": sorted(image_manifest.values()),
    }

    visible_nfc = unicodedata.normalize("NFC", report["extracted"]["visible_text"])
    supplied_hashes: dict[str, set[str]] = defaultdict(set)
    for selector, text in exact_texts:
        digest = _sha256_bytes(text.encode("utf-8"))
        supplied_hashes[selector].add(digest)
        allowed = declared_hashes.get(selector, set()) | declared_hashes.get("receipt", set())
        if declared_hashes and allowed and digest not in allowed:
            errors.append(
                _issue(
                    "forbidden_text_contract_hash_mismatch",
                    f"forbidden_selectors.{selector}",
                    "Exact forbidden text does not match its receipt SHA-256 contract.",
                    text_sha256=digest,
                )
            )
        if unicodedata.normalize("NFC", text) in visible_nfc:
            errors.append(
                _issue(
                    "forbidden_exact_text_detected",
                    f"forbidden_selectors.{selector}",
                    "Exact forbidden source text is visible in the generated document.",
                    text_sha256=digest,
                )
            )
    for selector, values in sorted(declared_hashes.items()):
        unresolved = values - supplied_hashes.get(selector, set()) - supplied_hashes.get("receipt", set())
        if unresolved:
            warnings.append(
                _issue(
                    "forbidden_hash_without_exact_text",
                    f"forbidden_selectors.{selector}",
                    "Receipt hash is recorded, but exact text was not supplied; no leakage heuristic was applied.",
                    count=len(unresolved),
                )
            )

    for source_name, actual_name, expected in checks:
        actual = counts[actual_name]
        passed = actual == expected
        report["expected_count_checks"].append(
            {
                "source_key": source_name,
                "actual_key": actual_name,
                "expected": expected,
                "actual": actual,
                "ok": passed,
            }
        )
        if not passed:
            errors.append(
                _issue(
                    "expected_count_mismatch",
                    f"counts.{actual_name}",
                    "Generated HWPX count does not match the build receipt.",
                    source_key=source_name,
                    expected=expected,
                    actual=actual,
                )
            )
    report["expected_count_checks"].sort(key=lambda item: (item["actual_key"], item["source_key"]))

    if run_external_tools:
        external_results, external_errors, external_warnings = _run_external_validators(
            source, _external_tools(external_tool_paths, selected_root)
        )
        report["external_tools"] = external_results
        errors.extend(external_errors)
        warnings.extend(external_warnings)
        if _sha256_file(source) != report["file"]["sha256"]:
            errors.append(
                _issue("source_modified_during_validation", "file", "Source HWPX changed during validation.")
            )
    return _finalize(report)


# Concise compatibility aliases for callers that prefer generic names.
validate_generated_hwpx = validate_hwpx_output
validate_hwpx = validate_hwpx_output


__all__ = [
    "REPORT_VERSION",
    "report_json",
    "validate_generated_hwpx",
    "validate_hwpx",
    "validate_hwpx_output",
]
