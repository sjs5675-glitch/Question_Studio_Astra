"""Safety preflight for optimistic question/explanation HWPX builds.

Content readiness is recorded as a warning, not forged into an approval or used
as an export gate.  This module only plans builds; it never fabricates content
and does not contain a solver, endnote builder, or an upstream ``hwpx_base``
path.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from xml.etree import ElementTree


from . import equation_bridge
from .equation_bridge import normalize_equations


RECEIPT_VERSION = "0.2.0"
BUILD_RECEIPT_VERSION = RECEIPT_VERSION
MIMETYPE = b"application/hwp+zip"
APP_ROOT = Path(__file__).resolve().parents[1]
DESIGN_PROFILE_SCHEMA_PATH = APP_ROOT / "schemas" / "design-profile.schema.json"

HP_NS = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HH_NS = "http://www.hancom.co.kr/hwpml/2011/head"
HS_NS = "http://www.hancom.co.kr/hwpml/2011/section"
HA_NS = "http://www.hancom.co.kr/hwpml/2011/app"
HV_NS = "http://www.hancom.co.kr/hwpml/2011/version"
OPF_NS = "http://www.idpf.org/2007/opf/"
OCF_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
ODF_NS = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

NATIVE_RENDERER_ENGINE_ID = "question-json-studio.native_ordered.v1.1"
HANCOM_VERIFIED_QUESTION_PROMPT_VERSIONS = ("transcribe-exam-crop-v2",)
MAX_BLOCK_DEPTH = 12
REQUIRED_STYLE_BINDINGS = {
    "paragraph": "neutral-paragraph",
    "equation": "neutral-equation",
    "choice": "neutral-choice",
    "figure_caption": "neutral-figure-caption",
}
_CONTAINER_BLOCKS = frozenset(
    {"paragraph", "figure", "bogi", "choices", "box", "table"}
)
BLOCK_CAPABILITY_MATRIX = {
    "question": _CONTAINER_BLOCKS,
    "explanation": _CONTAINER_BLOCKS,
    "answer": _CONTAINER_BLOCKS,
    "box": _CONTAINER_BLOCKS,
    "bogi": _CONTAINER_BLOCKS,
    "choices": _CONTAINER_BLOCKS,
    "table": frozenset({"paragraph", "figure"}),
}
NATIVE_RENDERER_CONTRACT = {
    "contract_version": "1.1.0",
    "approval_status": "approved",
    "engine_id": NATIVE_RENDERER_ENGINE_ID,
    "renderer": "native_ordered",
    "output_mode": "separate_hwpx",
    "block_capabilities": {
        key: sorted(value) for key, value in BLOCK_CAPABILITY_MATRIX.items()
    },
    "inline_capabilities": {"paragraph": ["equation", "line_break", "text"]},
    "text_marks": {
        "supported": ["underline"],
        "unsupported": ["bold", "italic", "subscript", "superscript"],
    },
    "separation_policy": {
        "question_excludes_answer_and_explanation": True,
        "answer_exact_text_leak_check": "all_saved_answer_text",
        "explanation_exact_text_min_semantic_chars": 4,
    },
    "hancom_compatibility": {
        "verified_question_prompt_versions": list(
            HANCOM_VERIFIED_QUESTION_PROMPT_VERSIONS
        ),
        "verified_equation_prompt_versions": ["transcribe-exam-crop-v5"],
        "unverified_native_risk": "ready_source_crop_item_fallback",
        "native_risk_features": ["block.box", "block.table", "equation"],
        "workflow_or_review_state_mutation": False,
    },
    "answer_kinds": ["choice", "content"],
    "figure_asset_roles": ["figure_original", "figure_regenerated", "source_crop"],
    "unsupported_figure_asset_roles": ["table_snapshot"],
    "required_styles": REQUIRED_STYLE_BINDINGS,
    "max_block_depth": MAX_BLOCK_DEPTH,
    "line_layout": {
        "implementation_revision": "0.5.0",
        "line_segments": "single_textpos_zero_per_paragraph",
        "wrap_policy": "editable_paragraph_expansion",
        "explicit_newline": "paragraph_boundary",
        "equation_policy": "atomic_inline_object",
        "available_width_scope": "page_or_table_cell",
    },
    "unsupported_answer_kinds": ["composite", "true_false", "unknown"],
    "container_policies": {
        "box": "editable_neutral_single_cell_table",
        "table": "editable_native_grid",
        "table_children": ["figure", "paragraph"],
        "choices": "hancom_reference_three_column_grid_for_short_choices_else_vertical",
        "bogi": "hancom_reference_4x5_title_gap_native_table",
        "cycle_policy": "fail_closed",
    },
    "question_set_combined": {
        "native_endnotes": True,
        "placement": "end_of_document",
        "numbering": "continuous_1_to_n",
        "generated_labels": ["[답]", "[풀이]"],
    },
    "project_assembly": {"columns": 2, "numbering": "continuous_1_to_n"},
    "question_set_layout": {
        "columns": 2,
        "gap_mm": 8,
        "separator": "solid_0.12_mm_black",
        "margins_mm": {"top": 8, "right": 13, "bottom": 8, "left": 13},
    },
    "readiness_policy": {
        "mode": "optimistic_with_warnings",
        "accepted_workflow_statuses": [
            "source_only",
            "transcribed",
            "needs_review",
            "content_ready",
            "content_working",
            "export_ready",
        ],
        "review_state_mutation": False,
        "unreviewed_canonical_blocks": "render_in_order",
        "missing_answer_or_explanation": "omit_without_inference",
        "unsupported_answer_or_explanation": "omit_without_inference",
        "stored_review_issues": "receipt_warning_count_and_sha256",
        "unrenderable_question": "ready_source_crop_full_page_fallback",
        "unapproved_selected_figure": "ready_original_same_lineage",
        "unapproved_ai_candidate": "never_auto_select",
    },
}
ACCEPTED_EXPORT_WORKFLOW_STATUSES = frozenset(
    NATIVE_RENDERER_CONTRACT["readiness_policy"]["accepted_workflow_statuses"]
)
_NATIVE_RENDERER_CONTRACT_DIGEST = hashlib.sha256(
    json.dumps(
        NATIVE_RENDERER_CONTRACT,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
# This literal is an approval pin. Changing capabilities requires a version and
# fingerprint update, not a silent re-hash of the accepted project contract.
NATIVE_RENDERER_ENGINE_FINGERPRINT_SHA256 = (
    "dbd99b91f951b280903d1f28c219bb8aa89ef17c67a0efbf408b59fd5f445228"
)
if _NATIVE_RENDERER_CONTRACT_DIGEST != NATIVE_RENDERER_ENGINE_FINGERPRINT_SHA256:
    raise RuntimeError("approved native renderer contract fingerprint is stale")
REQUIRED_HWPX_MEMBERS = frozenset(
    {
        "mimetype",
        "version.xml",
        "settings.xml",
        "Contents/content.hpf",
        "Contents/header.xml",
        "Contents/section0.xml",
        "META-INF/container.xml",
        "META-INF/container.rdf",
        "META-INF/manifest.xml",
        "Preview/PrvText.txt",
        "Preview/PrvImage.png",
    }
)
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

REQUIRED_XML_ROOTS = {
    "version.xml": f"{{{HV_NS}}}HCFVersion",
    "settings.xml": f"{{{HA_NS}}}HWPApplicationSetting",
    "Contents/content.hpf": f"{{{OPF_NS}}}package",
    "Contents/header.xml": f"{{{HH_NS}}}head",
    "Contents/section0.xml": f"{{{HS_NS}}}sec",
    "META-INF/container.xml": f"{{{OCF_NS}}}container",
    "META-INF/container.rdf": f"{{{RDF_NS}}}RDF",
    "META-INF/manifest.xml": f"{{{ODF_NS}}}manifest",
}


class ExportPreflightError(ValueError):
    """All hard errors found before an HWPX renderer may run."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(dict.fromkeys(str(error) for error in errors if error))
        detail = "\n".join(f"- {error}" for error in self.errors)
        super().__init__(f"HWPX export preflight failed:\n{detail}")


@dataclass(frozen=True)
class OutputPlan:
    document_kind: str
    output_path: Path
    design_profile_path: Path
    design_profile_sha256: str
    template_path: Path
    template_sha256: str
    item_ids: tuple[str, ...]
    content_selectors: tuple[str, ...]
    expected_counts: Mapping[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_kind": self.document_kind,
            "output_path": str(self.output_path),
            "design_profile_path": str(self.design_profile_path),
            "design_profile_sha256": self.design_profile_sha256,
            "template_path": str(self.template_path),
            "template_sha256": self.template_sha256,
            "item_ids": list(self.item_ids),
            "content_selectors": list(self.content_selectors),
            "expected_counts": dict(self.expected_counts),
        }


@dataclass(frozen=True)
class ExportPlan:
    project_path: Path
    project_id: str
    question: OutputPlan
    explanation: OutputPlan
    receipt: Mapping[str, Any]

    @property
    def outputs(self) -> tuple[OutputPlan, OutputPlan]:
        return self.question, self.explanation

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_path": str(self.project_path),
            "project_id": self.project_id,
            "outputs": [output.as_dict() for output in self.outputs],
            "receipt": dict(self.receipt),
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path, errors: list[str], label: str) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"{label} file does not exist: {path}")
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read {label} JSON {path}: {exc}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{label} JSON root must be an object: {path}")
        return None
    return value


def _validate_json_schema(
    value: Mapping[str, Any],
    schema_path: Path,
    errors: list[str],
    label: str,
) -> None:
    """Apply the repository's required Draft 2020-12 validation fail-closed."""

    try:
        from jsonschema import Draft202012Validator, FormatChecker, SchemaError
    except ModuleNotFoundError:
        errors.append(
            f"cannot validate {label}: jsonschema is required; install requirements.txt"
        )
        return
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError) as exc:
        errors.append(f"cannot load {label} schema {schema_path}: {exc}")
        return
    for error in sorted(
        validator.iter_errors(value), key=lambda item: list(item.absolute_path)
    ):
        where = ".".join(str(part) for part in error.absolute_path) or "root"
        errors.append(f"{label} schema.{where}: {error.message}")


def _resolve_relative(base: Path, raw: Any, errors: list[str], label: str) -> Path | None:
    if not isinstance(raw, str) or not raw:
        errors.append(f"{label} must be a non-empty POSIX relative path")
        return None
    if "\\" in raw or ":" in raw:
        errors.append(f"{label} must be a POSIX relative path: {raw!r}")
        return None
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        errors.append(f"{label} contains an unsafe path: {raw!r}")
        return None
    base = base.resolve()
    candidate = base.joinpath(*relative.parts).resolve()
    if not candidate.is_relative_to(base):
        errors.append(f"{label} escapes its package directory: {raw!r}")
        return None
    return candidate


def _xml_root(archive: zipfile.ZipFile, name: str, errors: list[str]) -> Any | None:
    try:
        return ElementTree.fromstring(archive.read(name))
    except (KeyError, ElementTree.ParseError, OSError) as exc:
        errors.append(f"template member is not well-formed XML ({name}): {exc}")
        return None


def _local_name(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _validate_package_links(
    archive: zipfile.ZipFile,
    names: set[str],
    roots: Mapping[str, Any],
    errors: list[str],
) -> None:
    content = roots.get("Contents/content.hpf")
    if content is not None:
        manifest = content.find(f"{{{OPF_NS}}}manifest")
        spine = content.find(f"{{{OPF_NS}}}spine")
        if manifest is None or spine is None:
            errors.append("template content.hpf must contain opf:manifest and opf:spine")
        else:
            items: dict[str, str] = {}
            for item in manifest.findall(f"{{{OPF_NS}}}item"):
                item_id, href = item.get("id"), item.get("href")
                if not item_id or not href:
                    errors.append("template content.hpf manifest items need id and href")
                    continue
                if item_id in items:
                    errors.append(f"template content.hpf has duplicate item id: {item_id}")
                items[item_id] = href
                member = PurePosixPath(href)
                if (
                    "\\" in href
                    or ":" in href
                    or member.is_absolute()
                    or ".." in member.parts
                ):
                    errors.append(f"template content.hpf has unsafe href: {href!r}")
                elif href not in names:
                    errors.append(f"template content.hpf href is missing from ZIP: {href}")
            required_hrefs = {
                "Contents/header.xml",
                "Contents/section0.xml",
                "settings.xml",
            }
            missing_hrefs = sorted(required_hrefs.difference(items.values()))
            if missing_hrefs:
                errors.append(
                    f"template content.hpf is missing required manifest hrefs: {missing_hrefs}"
                )
            spine_refs = {
                item.get("idref")
                for item in spine.findall(f"{{{OPF_NS}}}itemref")
                if item.get("idref")
            }
            required_spine_ids = {
                item_id
                for item_id, href in items.items()
                if href in {"Contents/header.xml", "Contents/section0.xml"}
            }
            if not required_spine_ids.issubset(spine_refs):
                errors.append("template content.hpf spine must reference header and section0")

    container = roots.get("META-INF/container.xml")
    if container is not None:
        rootfiles = container.find(f"{{{OCF_NS}}}rootfiles")
        paths = (
            {
                node.get("full-path")
                for node in rootfiles.findall(f"{{{OCF_NS}}}rootfile")
            }
            if rootfiles is not None
            else set()
        )
        if "Contents/content.hpf" not in paths:
            errors.append(
                "template container.xml must point to Contents/content.hpf"
            )

    if "Preview/PrvText.txt" in names:
        try:
            if archive.read("Preview/PrvText.txt").strip():
                errors.append("clean template preview text must be empty")
        except (KeyError, OSError) as exc:
            errors.append(f"cannot read template preview text: {exc}")


def _validate_styles_and_empty_section(
    header: Any,
    section: Any,
    required_style_refs: Iterable[str],
    errors: list[str],
) -> None:
    def indexed(local_name: str) -> tuple[dict[str, Any], set[str]]:
        result: dict[str, Any] = {}
        duplicates: set[str] = set()
        for element in header.iter(f"{{{HH_NS}}}{local_name}"):
            value = element.get("id")
            if not value:
                errors.append(f"template header {local_name} must have an id")
            elif value in result:
                duplicates.add(value)
            else:
                result[value] = element
        if duplicates:
            errors.append(
                f"template header has duplicate {local_name} IDs: {sorted(duplicates)}"
            )
        return result, set(result)

    char_properties, char_ids = indexed("charPr")
    para_properties, para_ids = indexed("paraPr")
    styles, style_ids = indexed("style")
    if not char_properties or not para_properties or not styles:
        errors.append(
            "template header.xml must define charPr, paraPr, and named style records"
        )

    for style in styles.values():
        style_id = style.get("id", "?")
        if style.get("type") != "PARA":
            errors.append(f"template style {style_id} must be a PARA style")
        if style.get("paraPrIDRef") not in para_ids:
            errors.append(
                f"template style {style_id} paraPrIDRef does not exist: "
                f"{style.get('paraPrIDRef')!r}"
            )
        if style.get("charPrIDRef") not in char_ids:
            errors.append(
                f"template style {style_id} charPrIDRef does not exist: "
                f"{style.get('charPrIDRef')!r}"
            )
        next_style = style.get("nextStyleIDRef")
        if next_style is not None and next_style not in style_ids:
            errors.append(
                f"template style {style_id} nextStyleIDRef does not exist: {next_style!r}"
            )

    for reference in dict.fromkeys(required_style_refs):
        matches = [
            style
            for style in styles.values()
            if reference in {style.get("id"), style.get("name"), style.get("engName")}
        ]
        if not matches:
            errors.append(f"template is missing required style reference: {reference!r}")
        elif len(matches) > 1:
            errors.append(f"template style reference is ambiguous: {reference!r}")

    paragraphs = section.findall(f"{{{HP_NS}}}p")
    if len(paragraphs) != 1:
        errors.append("clean template section0.xml must contain exactly one empty paragraph")
        return
    paragraph = paragraphs[0]
    if paragraph.get("styleIDRef") not in style_ids:
        errors.append(
            f"template empty paragraph styleIDRef does not exist: {paragraph.get('styleIDRef')!r}"
        )
    if paragraph.get("paraPrIDRef") not in para_ids:
        errors.append(
            f"template empty paragraph paraPrIDRef does not exist: {paragraph.get('paraPrIDRef')!r}"
        )
    runs = paragraph.findall(f"{{{HP_NS}}}run")
    if not runs:
        errors.append("template empty paragraph must contain an hp:run")
    for run in runs:
        if run.get("charPrIDRef") not in char_ids:
            errors.append(
                f"template empty run charPrIDRef does not exist: {run.get('charPrIDRef')!r}"
            )
    if paragraph.find(f".//{{{HP_NS}}}secPr") is None:
        errors.append("template empty paragraph must contain hp:secPr")
    line_segments = paragraph.find(f"{{{HP_NS}}}linesegarray")
    if (
        line_segments is None
        or line_segments.find(f"{{{HP_NS}}}lineseg") is None
    ):
        errors.append("template empty paragraph must contain hp:linesegarray/hp:lineseg")


def _validate_clean_xml(
    archive: zipfile.ZipFile,
    names: set[str],
    roots: Mapping[str, Any],
    required_style_refs: Iterable[str],
    errors: list[str],
) -> None:
    for name, expected_tag in REQUIRED_XML_ROOTS.items():
        root = roots.get(name)
        if root is not None and root.tag != expected_tag:
            errors.append(
                f"template member has wrong HWPX root/namespace ({name}): "
                f"expected {expected_tag}, got {root.tag}"
            )

    header = roots.get("Contents/header.xml")
    section = roots.get("Contents/section0.xml")
    if header is not None:
        if header.attrib.get("secCnt") != "1":
            errors.append("template header.xml secCnt must be exactly 1")
    if header is not None and section is not None:
        _validate_styles_and_empty_section(
            header, section, required_style_refs, errors
        )

    for name, root in roots.items():
        if root is None:
            continue
        for element in root.iter():
            local = _local_name(element.tag).lower()
            namespace = str(element.tag).split("}", 1)[0].lstrip("{").lower()
            if local in {"tbl", "pic", "picture", "equation"}:
                errors.append(
                    f"clean template cannot contain {local} elements ({name})"
                )
            if "endnote" in local:
                errors.append(f"endnote elements are forbidden ({name})")
            if local in {"macro", "macros", "script", "scripts"}:
                errors.append(f"macro/script elements are forbidden ({name})")
            if "svg" in namespace:
                errors.append(f"SVG XML is forbidden ({name})")
            for attribute, value in element.attrib.items():
                attribute_name = _local_name(attribute).lower()
                text = str(value).lower()
                if attribute_name in {"href", "full-path"} and text.startswith(
                    "bindata/"
                ):
                    errors.append(f"BinData references are forbidden ({name})")
                if attribute_name in {"media-type", "mediatype"} and "svg" in text:
                    errors.append(f"SVG media types are forbidden ({name})")

    if section is not None:
        visible_text = [
            element.text
            for element in section.iter(f"{{{HP_NS}}}t")
            if isinstance(element.text, str) and element.text.strip()
        ]
        if visible_text:
            errors.append("clean template section0.xml must not contain visible body text")

    _validate_package_links(archive, names, roots, errors)


def validate_clean_hwpx_template(
    path: Path | str,
    *,
    required_style_refs: Iterable[str] = (),
) -> tuple[str, tuple[str, ...]]:
    """Return the template digest and every package-integrity error.

    A clean template is a one-section, empty-body, macro-free HWPX with no
    inherited BinData.  Visible content and approved assets belong to canonical
    item packages, not to an opaque workbook base.
    """

    path = Path(path)
    errors: list[str] = []
    if path.suffix.lower() != ".hwpx":
        errors.append(f"template must use the .hwpx extension: {path}")
    if "hwpx_base" in str(path).lower():
        errors.append("upstream hwpx_base templates are forbidden")
    if not path.is_file():
        errors.append(f"template file does not exist: {path}")
        return "", tuple(errors)

    digest = _sha256(path)
    if not zipfile.is_zipfile(path):
        errors.append(f"template is not a ZIP-backed HWPX: {path}")
        return digest, tuple(errors)

    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            duplicates = [name for name, count in Counter(names).items() if count > 1]
            if duplicates:
                errors.append(f"template has duplicate ZIP members: {duplicates}")
            for info in infos:
                member = PurePosixPath(info.filename)
                if (
                    "\\" in info.filename
                    or ":" in info.filename
                    or member.is_absolute()
                    or ".." in member.parts
                ):
                    errors.append(f"template has unsafe ZIP member: {info.filename!r}")
                if info.flag_bits & 0x1:
                    errors.append(f"template has encrypted ZIP member: {info.filename}")
            missing = sorted(REQUIRED_HWPX_MEMBERS.difference(names))
            if missing:
                errors.append(f"template is missing required HWPX members: {missing}")
            if not infos or infos[0].filename != "mimetype":
                errors.append("template mimetype must be the first ZIP member")
            elif infos[0].compress_type != zipfile.ZIP_STORED:
                errors.append("template mimetype must be stored without compression")
            if "mimetype" in names and archive.read("mimetype") != MIMETYPE:
                errors.append("template mimetype content must be application/hwp+zip")
            if archive.testzip() is not None:
                errors.append("template has a member with a bad CRC")

            section_names = sorted(
                name for name in names if re.fullmatch(r"Contents/section\d+\.xml", name)
            )
            if section_names != ["Contents/section0.xml"]:
                errors.append("v0.2 clean template must contain exactly Contents/section0.xml")
            inherited = [
                name
                for name in names
                if name.lower().startswith(("bindata/", "scripts/"))
                or "macro" in PurePosixPath(name).name.lower()
            ]
            if inherited:
                errors.append(f"clean template cannot contain inherited binary/scripts: {inherited}")
            if any(name.lower().endswith(".svg") for name in names):
                errors.append("SVG members are forbidden in HWPX templates")
            if any("endnote" in name.lower() for name in names):
                errors.append("endnote members are forbidden")

            required_xml = list(REQUIRED_XML_ROOTS)
            xml_members = {
                name
                for name in names
                if name.lower().endswith((".xml", ".hpf", ".rdf"))
            }
            roots = {
                name: _xml_root(archive, name, errors)
                for name in sorted(xml_members)
            }
            for required in required_xml:
                if required in names and required not in roots:
                    errors.append(f"template required XML could not be parsed: {required}")
            _validate_clean_xml(
                archive,
                set(names),
                roots,
                tuple(required_style_refs),
                errors,
            )
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        errors.append(f"cannot inspect HWPX template {path}: {exc}")
    return digest, tuple(errors)


def _profile(
    project_dir: Path,
    reference: Any,
    kind: str,
    errors: list[str],
) -> tuple[Path, dict[str, Any], str, Path, str] | None:
    profile_path = _resolve_relative(
        project_dir, reference, errors, f"rendering.{kind}_design_profile_ref"
    )
    if profile_path is None:
        return None
    profile = _load_object(profile_path, errors, f"{kind} design profile")
    if profile is None:
        return None
    _validate_json_schema(
        profile,
        DESIGN_PROFILE_SCHEMA_PATH,
        errors,
        f"{kind} design profile",
    )
    if profile.get("schema_version") != "0.1.0":
        errors.append(f"{kind} design profile schema_version must be 0.1.0")
    if profile.get("document_kind") != kind:
        errors.append(f"{kind} design profile document_kind must be {kind!r}")
    approval = profile.get("approval")
    if not isinstance(approval, dict) or approval.get("status") != "approved":
        errors.append(f"{kind} design profile must be explicitly approved")
    template = profile.get("template")
    if not isinstance(template, dict):
        errors.append(f"{kind} design profile.template must be an object")
        return None
    if template.get("source") != "user_approved_clean_hwpx":
        errors.append(
            f"{kind} template.source must be 'user_approved_clean_hwpx'; upstream bases are forbidden"
        )
    template_path = _resolve_relative(
        profile_path.parent, template.get("path"), errors, f"{kind} template.path"
    )
    if template_path is None:
        return None
    design = profile.get("design")
    styles = design.get("styles") if isinstance(design, Mapping) else None
    required_style_refs: list[str] = []
    if isinstance(styles, Mapping):
        for style_key, expected in REQUIRED_STYLE_BINDINGS.items():
            actual = styles.get(style_key)
            if actual != expected:
                errors.append(
                    f"{kind} design.styles.{style_key} must be {expected!r} "
                    "for the approved native renderer"
                )
            if isinstance(actual, str) and actual:
                required_style_refs.append(actual)
    actual_template_sha, template_errors = validate_clean_hwpx_template(
        template_path,
        required_style_refs=required_style_refs,
    )
    errors.extend(f"{kind}: {error}" for error in template_errors)
    declared_sha = template.get("sha256")
    if not isinstance(declared_sha, str) or not SHA256_RE.fullmatch(declared_sha):
        errors.append(f"{kind} template.sha256 must be a SHA-256 digest")
    elif actual_template_sha and declared_sha.lower() != actual_template_sha:
        errors.append(f"{kind} template SHA-256 does not match the approved digest")
    return profile_path, profile, _sha256(profile_path), template_path, actual_template_sha


def _iter_child_blocks(block: Mapping[str, Any]) -> Iterable[tuple[str, Any]]:
    block_type = block.get("type")
    if block_type == "box":
        yield "blocks", block.get("blocks")
    elif block_type in {"bogi", "choices"}:
        for index, item in enumerate(block.get("items", [])):
            if isinstance(item, Mapping):
                yield f"items[{index}].blocks", item.get("blocks")
    elif block_type == "table":
        for row_index, row in enumerate(block.get("rows", [])):
            if not isinstance(row, Mapping):
                continue
            for cell_index, cell in enumerate(row.get("cells", [])):
                if isinstance(cell, Mapping):
                    yield (
                        f"rows[{row_index}].cells[{cell_index}].blocks",
                        cell.get("blocks"),
                    )


def _warning(code: str, path: str, message: str, **details: Any) -> dict[str, Any]:
    warning = {"code": code, "path": path, "message": message}
    warning.update(details)
    return warning


def _review_warning(
    value: Any,
    item_id: str,
    selector: str,
) -> dict[str, Any] | None:
    statuses: Counter[str] = Counter()

    def visit(current: Any) -> None:
        if isinstance(current, Mapping):
            if isinstance(current.get("block_id"), str):
                status = current.get("review_status")
                if status != "approved":
                    statuses[str(status or "missing")] += 1
            for child in current.values():
                visit(child)
        elif isinstance(current, list):
            for child in current:
                visit(child)

    visit(value)
    if not statuses:
        return None
    return _warning(
        "unreviewed_canonical_blocks",
        f"item[{item_id}].{selector}",
        "Canonical blocks were rendered in saved order without changing their review state.",
        item_id=item_id,
        selector=selector,
        count=sum(statuses.values()),
        statuses=dict(sorted(statuses.items())),
    )


def _hancom_compatibility_quarantine(
    item: Mapping[str, Any],
    question: Mapping[str, Any],
) -> tuple[str, tuple[str, ...]] | None:
    provenance = item.get("provenance")
    transcriptions = (
        provenance.get("transcriptions") if isinstance(provenance, Mapping) else None
    )
    prompt_version = next(
        (
            value.get("prompt_version")
            for value in reversed(transcriptions)
            if isinstance(value, Mapping)
            and value.get("section") == "question"
            and value.get("status") == "succeeded"
            and isinstance(value.get("prompt_version"), str)
        ),
        None,
    ) if isinstance(transcriptions, list) else None
    if (
        not isinstance(prompt_version, str)
        or not prompt_version.startswith("transcribe-exam-crop-")
        or prompt_version in HANCOM_VERIFIED_QUESTION_PROMPT_VERSIONS
    ):
        return None

    features: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            value_type = value.get("type")
            if value_type == "equation":
                features.add("equation")
            elif value_type in {"box", "table"}:
                features.add(f"block.{value_type}")
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(question)
    if prompt_version == "transcribe-exam-crop-v5" and features == {"equation"}:
        return None
    return (prompt_version, tuple(sorted(features))) if features else None


def _check_table_grid(block: Mapping[str, Any], path: str, errors: list[str]) -> None:
    column_count = block.get("column_count")
    rows = block.get("rows")
    if (
        isinstance(column_count, bool)
        or not isinstance(column_count, int)
        or column_count < 1
    ):
        errors.append(f"{path}.column_count must be a positive integer")
        return
    if not isinstance(rows, list) or not rows:
        errors.append(f"{path}.rows must be a non-empty array")
        return
    active_row_spans = [0] * column_count
    for row_index, row in enumerate(rows):
        row_path = f"{path}.rows[{row_index}]"
        if not isinstance(row, Mapping) or not isinstance(row.get("cells"), list):
            errors.append(f"{row_path}.cells must be an array")
            continue
        covered = [remaining > 0 for remaining in active_row_spans]
        updates: dict[int, int] = {}
        cursor = 0
        for cell_index, cell in enumerate(row["cells"]):
            cell_path = f"{row_path}.cells[{cell_index}]"
            if not isinstance(cell, Mapping):
                errors.append(f"{cell_path} must be an object")
                continue
            while cursor < column_count and covered[cursor]:
                cursor += 1
            col_span, row_span = cell.get("col_span"), cell.get("row_span")
            if (
                isinstance(col_span, bool)
                or not isinstance(col_span, int)
                or col_span < 1
                or isinstance(row_span, bool)
                or not isinstance(row_span, int)
                or row_span < 1
            ):
                errors.append(f"{cell_path} row_span and col_span must be positive integers")
                continue
            if cursor + col_span > column_count:
                errors.append(f"{cell_path} exceeds table column_count")
                continue
            if any(covered[cursor : cursor + col_span]):
                errors.append(f"{cell_path} overlaps another table cell")
                continue
            for column in range(cursor, cursor + col_span):
                covered[column] = True
                if row_span > 1:
                    updates[column] = max(updates.get(column, 0), row_span - 1)
            cursor += col_span
        if not all(covered):
            errors.append(f"{row_path} does not cover all {column_count} table columns")
        active_row_spans = [max(0, remaining - 1) for remaining in active_row_spans]
        for column, remaining in updates.items():
            active_row_spans[column] = max(active_row_spans[column], remaining)
    if any(active_row_spans):
        errors.append(f"{path} has a row_span extending beyond the final row")


def _check_blocks(
    blocks: Any,
    path: str,
    errors: list[str],
    counts: Counter[str],
    figure_assets: list[tuple[str, str, str, str]],
    *,
    container: str,
    depth: int = 0,
    ancestors: frozenset[int] = frozenset(),
) -> None:
    if depth > MAX_BLOCK_DEPTH:
        errors.append(f"{path} exceeds renderer max block depth {MAX_BLOCK_DEPTH}")
        return
    if not isinstance(blocks, list):
        errors.append(f"{path} must be a block array")
        return
    for index, block in enumerate(blocks):
        here = f"{path}[{index}]"
        if not isinstance(block, dict):
            errors.append(f"{here} must be an object")
            continue
        identity = id(block)
        if identity in ancestors:
            errors.append(f"{here} contains a cyclic block reference")
            continue
        block_type = block.get("type")
        allowed = BLOCK_CAPABILITY_MATRIX.get(container, frozenset())
        if block_type not in allowed:
            errors.append(
                f"unsupported nesting at {here}: {container} -> {block_type!r} "
                "is not declared by the approved renderer capability matrix"
            )
            continue
        counts[f"block.{block_type}"] += 1
        if block_type == "table":
            _check_table_grid(block, here, errors)
        if block_type == "box" and (
            not isinstance(block.get("blocks"), list) or not block["blocks"]
        ):
            errors.append(f"{here}.blocks must be non-empty")
        if block_type in {"bogi", "choices"}:
            items = block.get("items")
            if not isinstance(items, list) or not items:
                errors.append(f"{here}.items must be non-empty")
            else:
                for item_index, child in enumerate(items):
                    item_path = f"{here}.items[{item_index}]"
                    if not isinstance(child, Mapping):
                        errors.append(f"{item_path} must be an object")
                        continue
                    if not isinstance(child.get("label"), str) or not child["label"]:
                        errors.append(f"{item_path}.label must be visible")
                    if not isinstance(child.get("blocks"), list) or not child["blocks"]:
                        errors.append(f"{item_path}.blocks must be non-empty")
            if block_type == "bogi" and not isinstance(block.get("title"), Mapping):
                errors.append(f"{here}.title must be an object")
        if block_type == "figure":
            asset_id = block.get("asset_id")
            original_id = block.get("original_asset_id")
            if not isinstance(asset_id, str):
                errors.append(f"{here}.asset_id must identify an asset")
            elif not isinstance(original_id, str):
                errors.append(f"{here}.original_asset_id must identify an asset")
            else:
                section = "question" if ".content.question" in path else "explanation"
                figure_assets.append((here, asset_id, original_id, section))
        for child_name, child_blocks in _iter_child_blocks(block):
            _check_blocks(
                child_blocks,
                f"{here}.{child_name}",
                errors,
                counts,
                figure_assets,
                container=str(block_type),
                depth=depth + 1,
                ancestors=ancestors | {identity},
            )


def _check_equations(
    value: Any,
    path: str,
    errors: list[str],
    counts: Counter[str],
    *,
    paragraph_inlines: bool = False,
) -> None:
    if isinstance(value, dict):
        value_type = value.get("type")
        if value_type == "paragraph" and (
            not isinstance(value.get("inlines"), list) or not value["inlines"]
        ):
            errors.append(f"{path}.inlines must be a non-empty array")
        if paragraph_inlines and value_type not in {"equation", "line_break", "text"}:
            errors.append(f"unsupported inline type at {path}: {value_type!r}")
        if paragraph_inlines and value_type == "text":
            if not isinstance(value.get("text"), str):
                errors.append(f"{path}.text must be a string")
            marks = value.get("marks")
            if marks is not None and (
                not isinstance(marks, list) or marks not in ([], ["underline"])
            ):
                errors.append(
                    f"{path}.marks contains unsupported renderer v0.6 formatting"
                )
        if value_type == "equation":
            counts["equation"] += 1
            if not paragraph_inlines:
                errors.append(
                    f"{path} equation is unsupported outside paragraph.inlines"
                )
            derivation = value.get("hwp_derivation")
            if isinstance(derivation, dict) and derivation.get("status") == "unsupported":
                errors.extend(f"{path}: {message}" for message in derivation.get("diagnostics", []))
            script = value.get("hwp_script")
            if not isinstance(script, str) or not script.strip():
                errors.append(f"{path}.hwp_script must be non-empty before HWPX export")
        for key, child in value.items():
            _check_equations(
                child,
                f"{path}.{key}",
                errors,
                counts,
                paragraph_inlines=(value.get("type") == "paragraph" and key == "inlines"),
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check_equations(
                child,
                f"{path}[{index}]",
                errors,
                counts,
                paragraph_inlines=paragraph_inlines,
            )


def _assets(
    item: Mapping[str, Any],
    item_path: Path,
    errors: list[str],
) -> dict[str, Mapping[str, Any]]:
    resources = item.get("resources")
    assets = resources.get("assets") if isinstance(resources, Mapping) else None
    if not isinstance(assets, list):
        errors.append(f"{item_path}: resources.assets must be an array")
        return {}
    by_id: dict[str, Mapping[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, Mapping) or not isinstance(asset.get("asset_id"), str):
            continue
        asset_id = str(asset["asset_id"])
        if asset_id in by_id:
            errors.append(f"{item_path}: duplicate asset_id: {asset_id}")
        else:
            by_id[asset_id] = asset
    return by_id


def _lineage_reaches(
    selected: Mapping[str, Any],
    original_id: str,
    assets: Mapping[str, Mapping[str, Any]],
) -> bool:
    current = selected
    seen: set[str] = set()
    while True:
        current_id = current.get("asset_id")
        if current_id == original_id:
            return True
        if not isinstance(current_id, str) or current_id in seen:
            return False
        if current.get("role") != "figure_regenerated" or not isinstance(
            current.get("generation"), Mapping
        ):
            return False
        seen.add(current_id)
        parent_id = current.get("derived_from_asset_id")
        if not isinstance(parent_id, str) or parent_id not in assets:
            return False
        current = assets[parent_id]


def _approved_ready(asset: Mapping[str, Any]) -> bool:
    review = asset.get("review")
    return (
        asset.get("state") == "ready"
        and isinstance(review, Mapping)
        and review.get("status") == "approved"
        and review.get("boundary_uncertain") is False
    )


def _check_asset_file(
    item_path: Path,
    asset: Mapping[str, Any],
    errors: list[str],
) -> None:
    asset_id = str(asset.get("asset_id") or "unknown")
    if asset.get("state") != "ready":
        errors.append(f"asset[{asset_id}] must be ready before HWPX export")
    if asset.get("media_type") != "image/png":
        errors.append(f"asset[{asset_id}] must be image/png")
    asset_path = _resolve_relative(
        item_path.parent, asset.get("path"), errors, f"asset[{asset_id}].path"
    )
    if asset_path is None:
        return
    if not asset_path.is_file():
        errors.append(f"asset file does not exist: {asset_path}")
        return
    if asset_path.suffix.lower() != ".png":
        errors.append(f"asset[{asset_id}] must use the .png extension")
    declared = asset.get("sha256")
    if not isinstance(declared, str) or not SHA256_RE.fullmatch(declared):
        errors.append(f"asset[{asset_id}].sha256 must be a SHA-256 digest")
    elif _sha256(asset_path) != declared.lower():
        errors.append(f"asset[{asset_id}] SHA-256 does not match its file")
    for key in ("width_px", "height_px"):
        value = asset.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            errors.append(f"asset[{asset_id}].{key} must be a positive integer")


def _check_assets(
    item: Mapping[str, Any],
    item_path: Path,
    figure_assets: list[tuple[str, str, str, str]],
    errors: list[str],
    warnings: list[dict[str, Any]],
    item_id: str,
) -> None:
    assets = _assets(item, item_path, errors)
    for block_path, asset_id, original_id, section in figure_assets:
        selected = assets.get(asset_id)
        original = assets.get(original_id)
        if selected is None:
            errors.append(f"{block_path}.asset_id does not identify a local asset")
            continue
        if original is None:
            errors.append(f"{block_path}.original_asset_id does not identify a local asset")
            continue
        if selected.get("role") == "figure_generated" or original.get("role") == "figure_generated":
            # AI-generated item figure: no source crop lineage; it must be self-referential.
            if asset_id != original_id or selected.get("section") not in {section, "shared"}:
                errors.append(f"{block_path} generated figure must reference itself in its section")
                continue
            _check_asset_file(item_path, selected, errors)
            continue
        if selected.get("role") not in {"figure_original", "figure_regenerated"}:
            errors.append(
                f"{block_path} selected figure role is unsupported: {selected.get('role')!r}"
            )
            continue
        original_region = original.get("source_region")
        if (
            original.get("role") != "figure_original"
            or original.get("section") not in {section, "shared"}
            or not isinstance(original_region, Mapping)
            or original.get("derived_from_asset_id")
            != original_region.get("source_asset_id")
        ):
            errors.append(f"{block_path} figure original has invalid source lineage")
            continue
        if selected.get("section") not in {section, "shared"} or not _lineage_reaches(
            selected, original_id, assets
        ):
            errors.append(f"{block_path} selected figure is outside its section/original lineage")
            continue
        effective = selected if _approved_ready(selected) else original
        if effective is original and not _approved_ready(selected):
            warnings.append(
                _warning(
                    "figure_original_used",
                    block_path,
                    "The selected figure was not approved and ready; the ready original from the same lineage was used.",
                    item_id=item_id,
                    selected_asset_id=asset_id,
                    rendered_asset_id=original_id,
                )
            )
        _check_asset_file(item_path, effective, errors)


def _source_question_asset(
    item: Mapping[str, Any],
    item_path: Path,
    errors: list[str],
) -> str | None:
    assets = _assets(item, item_path, errors)
    provenance = item.get("provenance")
    problem = provenance.get("problem") if isinstance(provenance, Mapping) else None
    preferred = problem.get("crop_asset_id") if isinstance(problem, Mapping) else None
    candidates = [
        asset
        for asset in assets.values()
        if asset.get("role") == "source_crop"
        and asset.get("section") in {"question", "shared"}
        and asset.get("state") == "ready"
    ]
    selected = assets.get(preferred) if isinstance(preferred, str) else None
    if selected not in candidates:
        selected = candidates[0] if len(candidates) == 1 else None
    if selected is None:
        errors.append(
            f"{item_path}: canonical question cannot render and no unique ready question source crop exists"
        )
        return None
    _check_asset_file(item_path, selected, errors)
    return str(selected["asset_id"])


def _has_banned_feature(value: Any) -> str | None:
    banned = {"solver", "solver_result", "endnote", "endnotes", "make_endnote", "hwpx_base"}
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in banned:
                return str(key)
            found = _has_banned_feature(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _has_banned_feature(child)
            if found:
                return found
    return None


def _safe_filename(value: Any, fallback: str) -> str:
    candidate = str(value).strip() if value is not None else ""
    candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", candidate).strip(" .-")
    return candidate or fallback


def preflight_export(
    project_path: Path | str,
    output_dir: Path | str | None = None,
) -> ExportPlan:
    """Validate every renderer input and return two plans plus a receipt draft.

    No directory or output file is created.  A renderer may run only when this
    function returns successfully.
    """

    project_path = Path(project_path)
    if project_path.is_dir():
        project_path = project_path / "project.json"
    project_path = project_path.resolve()
    project_dir = project_path.parent
    errors: list[str] = []
    warnings: list[dict[str, Any]] = []
    project = _load_object(project_path, errors, "project")
    if project is None:
        raise ExportPreflightError(errors)

    workflow = project.get("workflow")
    project_status = workflow.get("status") if isinstance(workflow, dict) else None
    if project_status not in ACCEPTED_EXPORT_WORKFLOW_STATUSES:
        errors.append(f"project workflow.status is invalid: {project_status!r}")
    elif project_status not in {"content_ready", "export_ready"}:
        warnings.append(
            _warning(
                "project_workflow_not_ready",
                "project.workflow.status",
                "Project readiness is not approved; export continued without changing workflow state.",
                workflow_status=project_status,
            )
        )
    export = project.get("export")
    if not isinstance(export, dict):
        errors.append("project.export must be an object")
        export = {}
    if export.get("renderer") != "native_ordered":
        errors.append("only the native_ordered renderer is permitted in v0.6")
    if export.get("output_mode") != "separate_hwpx":
        errors.append("HWPX output_mode must be separate_hwpx")
    if export.get("engine_id") != NATIVE_RENDERER_ENGINE_ID:
        errors.append(
            f"export.engine_id must exactly match approved engine {NATIVE_RENDERER_ENGINE_ID!r}"
        )
    if (
        export.get("engine_fingerprint_sha256")
        != NATIVE_RENDERER_ENGINE_FINGERPRINT_SHA256
    ):
        errors.append(
            "export.engine_fingerprint_sha256 must exactly match the approved "
            "native renderer contract"
        )
    for include_key in ("include_answers", "include_explanations"):
        if not isinstance(export.get(include_key), bool):
            errors.append(f"project.export.{include_key} must be a boolean")
    banned = _has_banned_feature({"export": export})
    if banned:
        errors.append(f"forbidden solver/endnote/upstream feature in project export: {banned}")

    rendering = project.get("rendering")
    if not isinstance(rendering, dict):
        errors.append("project.rendering must be an object")
        rendering = {}
    question_profile = _profile(
        project_dir, rendering.get("question_design_profile_ref"), "question", errors
    )
    explanation_profile = _profile(
        project_dir,
        rendering.get("explanation_design_profile_ref"),
        "explanation",
        errors,
    )

    item_ids = project.get("item_order")
    if not isinstance(item_ids, list) or not item_ids:
        errors.append("project.item_order must be a non-empty array")
        item_ids = []
    elif len(item_ids) != len(set(item_ids)):
        errors.append("project.item_order must not contain duplicate item IDs")

    item_receipts: list[dict[str, Any]] = []
    question_counts: Counter[str] = Counter()
    explanation_counts: Counter[str] = Counter()
    for item_id in item_ids:
        if not isinstance(item_id, str) or not item_id:
            errors.append(f"invalid item ID in project.item_order: {item_id!r}")
            continue
        item_path = project_dir / "items" / item_id / "question.json"
        item = _load_object(item_path, errors, f"item {item_id}")
        if item is None:
            continue
        normalize_equations(item.get("content"))
        item_workflow = item.get("workflow")
        item_status = item_workflow.get("status") if isinstance(item_workflow, dict) else None
        if item_status not in ACCEPTED_EXPORT_WORKFLOW_STATUSES:
            errors.append(f"item {item_id} workflow.status is invalid: {item_status!r}")
        elif item_status != "content_ready":
            warnings.append(
                _warning(
                    "item_workflow_not_ready",
                    f"item[{item_id}].workflow.status",
                    "Item readiness is not approved; saved content was exported without changing workflow state.",
                    item_id=item_id,
                    workflow_status=item_status,
                )
            )
        if item.get("item_id") != item_id:
            errors.append(f"item path/identity mismatch for {item_id}")
        review = item.get("review")
        stored_issues = review.get("issues") if isinstance(review, Mapping) else None
        if isinstance(stored_issues, list) and stored_issues:
            issue_hashes = sorted(
                {
                    hashlib.sha256(
                        json.dumps(
                            issue,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    for issue in stored_issues
                }
            )
            warnings.append(
                _warning(
                    "stored_review_issues",
                    f"item[{item_id}].review.issues",
                    "Stored review issues remain unresolved and were preserved for later editing.",
                    item_id=item_id,
                    count=len(stored_issues),
                    issue_sha256=issue_hashes,
                )
            )
        provenance = item.get("provenance")
        solution_links = (
            provenance.get("solution_links") if isinstance(provenance, Mapping) else None
        )
        verified_links = [
            link
            for link in (solution_links if isinstance(solution_links, list) else [])
            if isinstance(link, Mapping)
            and link.get("kind") in {"detailed_solution", "mixed"}
            and link.get("status") == "verified"
        ]
        review_links = [] if verified_links else [
            link
            for link in (solution_links if isinstance(solution_links, list) else [])
            if isinstance(link, Mapping)
            and link.get("kind") in {"detailed_solution", "mixed"}
            and link.get("status") == "review"
        ]
        if review_links:
            reasons = sorted(
                {
                    str(reason)
                    for link in review_links
                    for reason in (link.get("review_reasons") or [])
                    if isinstance(reason, str) and reason
                }
            )
            warnings.append(
                _warning(
                    "solution_link_needs_review",
                    f"item[{item_id}].provenance.solution_links",
                    "A best-effort solution link was exported and remains tagged for review.",
                    item_id=item_id,
                    confidence=max(float(link.get("confidence") or 0) for link in review_links),
                    reason_count=len(reasons),
                    reason_sha256=[
                        hashlib.sha256(reason.encode("utf-8")).hexdigest()
                        for reason in reasons
                    ],
                )
            )
        raw_snapshot = (
            provenance.get("raw_snapshot") if isinstance(provenance, Mapping) else None
        )
        raw_link = raw_snapshot.get("link_item") if isinstance(raw_snapshot, Mapping) else None
        if (
            not review_links
            and not verified_links
            and isinstance(raw_link, Mapping)
            and raw_link.get("best_effort_exhausted") is True
        ):
            warnings.append(
                _warning(
                    "solution_link_missing",
                    f"item[{item_id}].provenance.raw_snapshot.link_item",
                    "Best-effort solution matching finished without a detailed-solution candidate.",
                    item_id=item_id,
                )
            )
        content = item.get("content")
        if not isinstance(content, dict):
            errors.append(f"item {item_id}.content must be an object")
            continue
        banned = _has_banned_feature(content)
        if banned:
            errors.append(f"item {item_id} contains forbidden solver/endnote feature: {banned}")

        question_render_mode = "canonical"
        source_question_asset_id: str | None = None
        question = content.get("question")
        item_question_errors: list[str] = []
        item_question_warnings: list[dict[str, Any]] = []
        item_question_counts: Counter[str] = Counter()
        question_assets: list[tuple[str, str, str, str]] = []
        hancom_quarantine: tuple[str, tuple[str, ...]] | None = None
        question_blocks = question.get("blocks") if isinstance(question, Mapping) else None
        if isinstance(question_blocks, list) and question_blocks:
            _check_blocks(
                question_blocks,
                f"item[{item_id}].content.question.blocks",
                item_question_errors,
                item_question_counts,
                question_assets,
                container="question",
            )
            _check_equations(
                question,
                f"item[{item_id}].content.question",
                item_question_errors,
                item_question_counts,
            )
            _check_assets(
                item,
                item_path,
                question_assets,
                item_question_errors,
                item_question_warnings,
                item_id,
            )
            hancom_quarantine = _hancom_compatibility_quarantine(item, question)
            if hancom_quarantine:
                item_question_errors.append(
                    "saved question uses an unverified transcription prompt with "
                    "native structures that are quarantined for Hancom compatibility"
                )
        else:
            item_question_errors.append(f"item {item_id} has no renderable canonical question blocks")

        if item_question_errors:
            fallback_errors: list[str] = []
            source_question_asset_id = _source_question_asset(item, item_path, fallback_errors)
            if source_question_asset_id is None or fallback_errors:
                errors.extend(item_question_errors)
                errors.extend(fallback_errors)
            else:
                question_render_mode = "source_crop"
                question_counts["block.figure"] += 1
                question_counts["fallback.source_crop"] += 1
                warnings.append(
                    _warning(
                        "question_source_crop_fallback",
                        f"item[{item_id}].content.question",
                        "Canonical question content could not be rendered safely; the complete ready source crop was used instead.",
                        item_id=item_id,
                        source_asset_id=source_question_asset_id,
                        canonical_issue_count=len(item_question_errors),
                        canonical_issues=list(item_question_errors),
                        fallback_reason=(
                            "hancom_unverified_native_structure"
                            if hancom_quarantine
                            else "canonical_question_unrenderable"
                        ),
                        compatibility_prompt_version=(
                            hancom_quarantine[0] if hancom_quarantine else None
                        ),
                        native_risk_features=(
                            list(hancom_quarantine[1]) if hancom_quarantine else []
                        ),
                    )
                )
        else:
            question_counts.update(item_question_counts)
            warnings.extend(item_question_warnings)
            review_warning = _review_warning(question, item_id, "content.question")
            if review_warning:
                warnings.append(review_warning)

        explanation = content.get("explanation")
        explanation_render_mode = "omitted"
        if export.get("include_explanations") and explanation is not None:
            if not isinstance(explanation, dict):
                errors.append(f"item {item_id}.content.explanation must be a section or null")
            else:
                explanation_errors: list[str] = []
                explanation_warnings: list[dict[str, Any]] = []
                explanation_item_counts: Counter[str] = Counter()
                explanation_assets: list[tuple[str, str, str, str]] = []
                explanation_blocks = explanation.get("blocks")
                if not isinstance(explanation_blocks, list) or not explanation_blocks:
                    explanation_errors.append(
                        f"item {item_id}.content.explanation.blocks must be non-empty"
                    )
                else:
                    _check_blocks(
                        explanation_blocks,
                        f"item[{item_id}].content.explanation.blocks",
                        explanation_errors,
                        explanation_item_counts,
                        explanation_assets,
                        container="explanation",
                    )
                    _check_equations(
                        explanation,
                        f"item[{item_id}].content.explanation",
                        explanation_errors,
                        explanation_item_counts,
                    )
                    _check_assets(
                        item,
                        item_path,
                        explanation_assets,
                        explanation_errors,
                        explanation_warnings,
                        item_id,
                    )
                if explanation_errors:
                    warnings.append(
                        _warning(
                            "explanation_omitted_unsupported",
                            f"item[{item_id}].content.explanation",
                            "The saved explanation could not be rendered safely and was omitted without inference.",
                            item_id=item_id,
                            issue_count=len(explanation_errors),
                        )
                    )
                else:
                    explanation_render_mode = "saved"
                    explanation_counts.update(explanation_item_counts)
                    warnings.extend(explanation_warnings)
                    review_warning = _review_warning(
                        explanation, item_id, "content.explanation"
                    )
                    if review_warning:
                        warnings.append(review_warning)
        answer = content.get("answer")
        answer_render_mode = "omitted"
        if export.get("include_answers"):
            if answer is None:
                warnings.append(
                    _warning(
                        "answer_missing",
                        f"item[{item_id}].content.answer",
                        "No saved answer was available; the answer portion was omitted without inference.",
                        item_id=item_id,
                    )
                )
            elif not isinstance(answer, Mapping):
                errors.append(f"item {item_id}.content.answer must be an object or null")
            else:
                answer_errors: list[str] = []
                answer_warnings: list[dict[str, Any]] = []
                answer_counts: Counter[str] = Counter()
                answer_assets: list[tuple[str, str, str, str]] = []
                if answer.get("kind") == "choice":
                    if not isinstance(answer.get("choice_ids"), list) or not answer[
                        "choice_ids"
                    ]:
                        answer_errors.append(
                            f"item {item_id}.content.answer.choice_ids must be non-empty"
                        )
                    else:
                        answer_counts["answer.choice"] += 1
                elif answer.get("kind") == "content":
                    blocks = answer.get("blocks")
                    if not isinstance(blocks, list) or not blocks:
                        answer_errors.append(
                            f"item {item_id}.content.answer.blocks must be non-empty"
                        )
                    else:
                        answer_counts["answer.content"] += 1
                        _check_blocks(
                            blocks,
                            f"item[{item_id}].content.answer.blocks",
                            answer_errors,
                            answer_counts,
                            answer_assets,
                            container="answer",
                        )
                        _check_equations(
                            answer,
                            f"item[{item_id}].content.answer",
                            answer_errors,
                            answer_counts,
                        )
                        _check_assets(
                            item,
                            item_path,
                            answer_assets,
                            answer_errors,
                            answer_warnings,
                            item_id,
                        )
                else:
                    answer_errors.append(
                        f"item {item_id}.content.answer kind is unsupported by renderer v0.6"
                    )
                if answer_errors:
                    warnings.append(
                        _warning(
                            "answer_omitted_unsupported",
                            f"item[{item_id}].content.answer",
                            "The saved answer could not be rendered safely and was omitted without inference.",
                            item_id=item_id,
                            issue_count=len(answer_errors),
                        )
                    )
                else:
                    answer_render_mode = "saved"
                    explanation_counts.update(answer_counts)
                    warnings.extend(answer_warnings)
                    review_warning = _review_warning(answer, item_id, "content.answer")
                    if review_warning:
                        warnings.append(review_warning)
        if export.get("include_explanations") and explanation is None:
            warnings.append(
                _warning(
                    "explanation_missing",
                    f"item[{item_id}].content.explanation",
                    "No saved explanation was available; the explanation portion was omitted without inference.",
                    item_id=item_id,
                )
            )
        item_receipts.append(
            {
                "item_id": item_id,
                "relative_path": f"items/{item_id}/question.json",
                "sha256": _sha256(item_path),
                "revision": item_workflow.get("revision") if isinstance(item_workflow, dict) else None,
                "workflow_status": item_status,
                "question_render_mode": question_render_mode,
                "source_question_asset_id": source_question_asset_id,
                "answer_render_mode": answer_render_mode,
                "explanation_render_mode": explanation_render_mode,
            }
        )

    if errors:
        raise ExportPreflightError(errors)
    assert question_profile is not None and explanation_profile is not None

    if output_dir is None:
        output_root = project_dir / "build"
    else:
        output_root = Path(output_dir)
        if not output_root.is_absolute():
            output_root = project_dir / output_root
    output_root = output_root.resolve()
    project_id = str(project.get("project_id") or project_dir.name)
    metadata = project.get("metadata")
    filename_base = _safe_filename(
        metadata.get("filename_base") if isinstance(metadata, dict) else None,
        _safe_filename(project_id, "question-json-studio"),
    )

    q_profile_path, _, q_profile_sha, q_template_path, q_template_sha = question_profile
    e_profile_path, _, e_profile_sha, e_template_path, e_template_sha = explanation_profile
    question_plan = OutputPlan(
        document_kind="question",
        output_path=output_root / f"{filename_base}-문제.hwpx",
        design_profile_path=q_profile_path,
        design_profile_sha256=q_profile_sha,
        template_path=q_template_path,
        template_sha256=q_template_sha,
        item_ids=tuple(item_ids),
        content_selectors=("content.question",),
        expected_counts=dict(sorted(question_counts.items())),
    )
    explanation_plan = OutputPlan(
        document_kind="explanation",
        output_path=output_root / f"{filename_base}-해설.hwpx",
        design_profile_path=e_profile_path,
        design_profile_sha256=e_profile_sha,
        template_path=e_template_path,
        template_sha256=e_template_sha,
        item_ids=tuple(item_ids),
        content_selectors=("content.answer", "content.explanation"),
        expected_counts=dict(sorted(explanation_counts.items())),
    )

    project_sha = _sha256(project_path)
    fingerprint_payload = {
        "equation_converter_version": equation_bridge.CONVERTER_VERSION,
        "project_sha256": project_sha,
        "items": item_receipts,
        "profiles": [q_profile_sha, e_profile_sha],
        "templates": [q_template_sha, e_template_sha],
        "renderer": "native_ordered",
        "engine_id": NATIVE_RENDERER_ENGINE_ID,
        "engine_fingerprint_sha256": NATIVE_RENDERER_ENGINE_FINGERPRINT_SHA256,
        "output_mode": "separate_hwpx",
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt = {
        "schema_version": RECEIPT_VERSION,
        "receipt_type": "question_json_studio.hwpx_build",
        "status": "planned",
        "build_id": f"build-{fingerprint[:24]}",
        "build_fingerprint_sha256": fingerprint,
        "project": {
            "project_id": project_id,
            "sha256": project_sha,
            "revision": workflow.get("revision") if isinstance(workflow, dict) else None,
        },
        "inputs": {
            "equation_converter_version": equation_bridge.CONVERTER_VERSION,
            "items": item_receipts,
            "design_profiles": {
                "question": q_profile_sha,
                "explanation": e_profile_sha,
            },
            "templates": {
                "question": q_template_sha,
                "explanation": e_template_sha,
            },
            "engine": {
                "engine_id": NATIVE_RENDERER_ENGINE_ID,
                "engine_fingerprint_sha256": NATIVE_RENDERER_ENGINE_FINGERPRINT_SHA256,
            },
        },
        "outputs": [
            {
                "document_kind": plan.document_kind,
                "path": plan.output_path.name,
                "sha256": None,
                "size_bytes": None,
                "expected_counts": dict(plan.expected_counts),
                "validation": {
                    "package": "pending",
                    "inspection": "pending",
                    "hancom_visual": "pending",
                },
            }
            for plan in (question_plan, explanation_plan)
        ],
        "policy": {
            "renderer": "native_ordered",
            "engine_id": NATIVE_RENDERER_ENGINE_ID,
            "engine_fingerprint_sha256": NATIVE_RENDERER_ENGINE_FINGERPRINT_SHA256,
            "output_mode": "separate_hwpx",
            "solver_used": False,
            "endnotes_used": False,
            "upstream_hwpx_base_used": False,
            "content_readiness": "optimistic_with_warnings",
            "review_state_mutated": False,
            "missing_content": "omit_without_inference",
            "question_fallback": "ready_source_crop_full_page_inline_raster",
        },
        "warnings": sorted(
            warnings,
            key=lambda value: (
                str(value.get("path", "")),
                str(value.get("code", "")),
                json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ),
        ),
    }
    return ExportPlan(
        project_path=project_path,
        project_id=project_id,
        question=question_plan,
        explanation=explanation_plan,
        receipt=receipt,
    )


def build_receipt_contract(plan: ExportPlan) -> dict[str, Any]:
    """Return a detached JSON-ready receipt draft for the eventual renderer."""

    return json.loads(json.dumps(plan.receipt, ensure_ascii=False))


create_export_plan = preflight_export
make_build_receipt = build_receipt_contract
