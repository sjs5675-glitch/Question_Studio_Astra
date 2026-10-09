"""Deterministic v0.6 renderer for optimistic HWPX exports.

The only production entry point accepts an :class:`ExportPlan`.  It refreshes
the safety preflight before reading saved content and commits the two
finished packages as one rollback-capable filesystem transaction.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import tempfile
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from xml.etree import ElementTree

from .equation_bridge import normalize_equations

from PIL import Image

from .hwpx_export import (
    HH_NS,
    HP_NS,
    HS_NS,
    MIMETYPE,
    MAX_BLOCK_DEPTH,
    OPF_NS,
    REQUIRED_HWPX_MEMBERS,
    ExportPreflightError,
    ExportPlan,
    OutputPlan,
    preflight_export,
)
from .hwpx_native import (
    HC_NAMESPACE,
    BuildIdAllocator,
    equation_xml,
    line_segment_xml,
    paragraph_runs_xml,
    picture_xml,
    png_to_rgb_bmp,
    text_xml,
)
from .hwpx_validation import validate_hwpx_output


ElementTree.register_namespace("hs", HS_NS)
ElementTree.register_namespace("hp", HP_NS)
ElementTree.register_namespace("hc", HC_NAMESPACE)
ElementTree.register_namespace("hh", HH_NS)
ElementTree.register_namespace("opf", OPF_NS)

HWPUNIT_PER_MM = 7200 / 25.4
# The approved mechanics use 200 dpi as the no-upscale natural-size ceiling.
HWPUNIT_PER_SOURCE_PIXEL = 37.8
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_LINE_WRAP_WIDTH_RATIO = 0.92
_LEAKAGE_MIN_SEMANTIC_CHARS = 4
_MARK_NAMESPACE = "urn:question-json-studio:renderer"
_MARK_TAG = f"{{{_MARK_NAMESPACE}}}mark"
_BASE_PACKAGE_ORDER = (
    "mimetype",
    "version.xml",
    "Contents/header.xml",
    "Contents/section0.xml",
    "Preview/PrvText.txt",
    "settings.xml",
    "Preview/PrvImage.png",
    "META-INF/container.rdf",
    "Contents/content.hpf",
    "META-INF/container.xml",
    "META-INF/manifest.xml",
)


class HwpxRenderError(RuntimeError):
    """A stale, unsupported, or unsafe build was rejected before publication."""


@dataclass(frozen=True)
class _StyleRefs:
    style_id: str
    paragraph_property_id: str
    character_property_id: str


@dataclass(frozen=True)
class _LoadedItem:
    item_id: str
    path: Path
    value: Mapping[str, Any]
    question_render_mode: str
    source_question_asset_id: str | None
    answer_render_mode: str
    explanation_render_mode: str


@dataclass(frozen=True)
class _TemplateContext:
    members: Mapping[str, bytes]
    styles: Mapping[str, _StyleRefs]
    section_root: ElementTree.Element
    section_properties: ElementTree.Element
    column_control: ElementTree.Element
    page_writable_width: int
    writable_width: int
    writable_height: int
    page_break_between_items: bool
    centered_paragraph_property_id: str
    neutral_border_fill_id: str
    neutral_header_fill_id: str
    bogi_border_fill_id: str
    choice_grid_border_fill_id: str
    bogi_border_fill_ids: Mapping[str, str]
    manifest_ids: frozenset[str]
    underline_character_property_ids: Mapping[str, str]


@dataclass(frozen=True)
class _RenderedPackage:
    data: bytes
    canonical_counts: Mapping[str, int]
    renderer_counts: Mapping[str, int]
    selector_texts: Mapping[str, tuple[str, ...]]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_exact(path: Path, expected_sha256: str, label: str) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise HwpxRenderError(f"cannot read {label}: {path}: {exc}") from exc
    actual = _sha256_bytes(data)
    if actual != expected_sha256.lower():
        raise HwpxRenderError(f"{label} changed after preflight: {path}")
    return data


def _json_bytes(data: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HwpxRenderError(f"cannot decode {label} JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise HwpxRenderError(f"{label} JSON root must be an object")
    return value


def _fragment(xml: str) -> ElementTree.Element:
    wrapper = ElementTree.fromstring(
        f'<root xmlns:hp="{HP_NS}" xmlns:hc="{HC_NAMESPACE}">{xml}</root>'
    )
    return wrapper[0]


def _xml_bytes_with_original_namespaces(
    root: ElementTree.Element,
    original: bytes,
) -> bytes:
    for _event, (prefix, uri) in ElementTree.iterparse(
        io.BytesIO(original), events=("start-ns",)
    ):
        try:
            ElementTree.register_namespace(prefix or "", uri)
        except ValueError:
            pass
    return ElementTree.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
        short_empty_elements=True,
    )


def _style_refs(
    header: ElementTree.Element,
    style_names: Mapping[str, Any],
) -> dict[str, _StyleRefs]:
    char_ids = {
        node.get("id") for node in header.iter(f"{{{HH_NS}}}charPr") if node.get("id")
    }
    para_ids = {
        node.get("id") for node in header.iter(f"{{{HH_NS}}}paraPr") if node.get("id")
    }
    styles = list(header.iter(f"{{{HH_NS}}}style"))
    result: dict[str, _StyleRefs] = {}
    for role in ("paragraph", "equation", "choice", "figure_caption"):
        name = style_names.get(role)
        matches = [
            node
            for node in styles
            if name in {node.get("id"), node.get("name"), node.get("engName")}
        ]
        if not isinstance(name, str) or not name or len(matches) != 1:
            raise HwpxRenderError(f"template style binding is missing or ambiguous: {role}")
        node = matches[0]
        style_id = node.get("id")
        para_id = node.get("paraPrIDRef")
        char_id = node.get("charPrIDRef")
        if not style_id or para_id not in para_ids or char_id not in char_ids:
            raise HwpxRenderError(f"template style binding has invalid references: {role}")
        result[role] = _StyleRefs(style_id, str(para_id), str(char_id))
    return result


def _underline_character_properties(
    header: ElementTree.Element,
    base_ids: set[str],
) -> tuple[dict[str, str], bool]:
    """Reuse or add a base-equivalent character shape with a solid underline."""

    container = header.find(f".//{{{HH_NS}}}charProperties")
    if container is None:
        raise HwpxRenderError("template header has no character properties")
    properties = list(container.findall(f"{{{HH_NS}}}charPr"))
    by_id = {
        str(node.get("id")): node for node in properties if node.get("id") is not None
    }
    missing = sorted(base_ids.difference(by_id))
    if missing:
        raise HwpxRenderError(
            f"template style references missing character properties: {missing}"
        )

    def make_underlined(node: ElementTree.Element) -> ElementTree.Element:
        clone = copy.deepcopy(node)
        clone.attrib.pop("id", None)
        underline = clone.find(f"{{{HH_NS}}}underline")
        if underline is None:
            underline = ElementTree.Element(
                f"{{{HH_NS}}}underline",
                {
                    "type": "BOTTOM",
                    "shape": "SOLID",
                    "color": str(clone.get("textColor") or "#000000"),
                },
            )
            children = list(clone)
            insert_at = next(
                (
                    index
                    for index, child in enumerate(children)
                    if child.tag
                    in {
                        f"{{{HH_NS}}}strikeout",
                        f"{{{HH_NS}}}outline",
                        f"{{{HH_NS}}}shadow",
                    }
                ),
                len(children),
            )
            clone.insert(insert_at, underline)
        else:
            underline.set("type", "BOTTOM")
            underline.set("shape", "SOLID")
            underline.set("color", str(clone.get("textColor") or "#000000"))
        return clone

    def signature(node: ElementTree.Element) -> bytes:
        clone = copy.deepcopy(node)
        clone.attrib.pop("id", None)
        return ElementTree.tostring(clone, encoding="utf-8")

    used_ids = set(by_id)
    numeric_ids = [int(value) for value in used_ids if value.isdigit()]
    next_id = max(numeric_ids, default=-1) + 1
    result: dict[str, str] = {}
    changed = False
    for base_id in sorted(base_ids):
        desired = make_underlined(by_id[base_id])
        desired_signature = signature(desired)
        existing = next(
            (
                node
                for node in properties
                if signature(node) == desired_signature and node.get("id") is not None
            ),
            None,
        )
        if existing is None:
            while str(next_id) in used_ids:
                next_id += 1
            underline_id = str(next_id)
            next_id += 1
            desired.set("id", underline_id)
            container.append(desired)
            properties.append(desired)
            used_ids.add(underline_id)
            changed = True
        else:
            underline_id = str(existing.get("id"))
        result[base_id] = underline_id
    if changed:
        container.set("itemCnt", str(len(properties)))
    return result, changed


def _border_fill_refs(
    header: ElementTree.Element,
) -> tuple[str, str, str, str, Mapping[str, str]]:
    candidates = list(header.iter(f"{{{HH_NS}}}borderFill"))

    def solid_sides(node: ElementTree.Element) -> bool:
        return all(
            (border := node.find(f"{{{HH_NS}}}{side}Border")) is not None
            and border.get("type") == "SOLID"
            for side in ("left", "right", "top", "bottom")
        )

    neutral = next(
        (
            node.get("id")
            for node in candidates
            if node.get("id")
            and solid_sides(node)
            and all(
                node.find(f"{{{HH_NS}}}{side}Border").get("color")
                in {"#999999", "#AAAAAA", "#BBBBBB", "#CCCCCC"}
                for side in ("left", "right", "top", "bottom")
            )
        ),
        None,
    )
    header_fill = next(
        (
            node.get("id")
            for node in candidates
            if node.get("id")
            and solid_sides(node)
            and (brush := node.find(f".//{{{HC_NAMESPACE}}}winBrush")) is not None
            and brush.get("faceColor") in {"#D9D9D9", "#E7E6E6", "#EEEEEE", "#F2F2F2"}
        ),
        None,
    )
    black = next(
        (
            node.get("id")
            for node in candidates
            if node.get("id")
            and solid_sides(node)
            and all(
                node.find(f"{{{HH_NS}}}{side}Border").get("color") == "#000000"
                for side in ("left", "right", "top", "bottom")
            )
        ),
        None,
    )
    invisible = next(
        (
            node.get("id")
            for node in candidates
            if node.get("id")
            and all(
                (border := node.find(f"{{{HH_NS}}}{side}Border")) is not None
                and border.get("type") == "NONE"
                for side in ("left", "right", "top", "bottom")
            )
        ),
        None,
    )

    def by_signature(signature: tuple[str, str, str, str]) -> str | None:
        return next(
            (
                str(node.get("id"))
                for node in candidates
                if node.get("id")
                and tuple(
                    (
                        border.get("type")
                        if (border := node.find(f"{{{HH_NS}}}{side}Border"))
                        is not None
                        else None
                    )
                    for side in ("left", "right", "top", "bottom")
                )
                == signature
            ),
            None,
        )

    bogi_parts = {
        name: by_signature(signature)
        for name, signature in {
            "none": ("NONE", "NONE", "NONE", "NONE"),
            "all": ("SOLID", "SOLID", "SOLID", "SOLID"),
            "bottom": ("NONE", "NONE", "NONE", "SOLID"),
            "left_top": ("SOLID", "NONE", "SOLID", "NONE"),
            "right_top": ("NONE", "SOLID", "SOLID", "NONE"),
            "left": ("SOLID", "NONE", "NONE", "NONE"),
            "right": ("NONE", "SOLID", "NONE", "NONE"),
            "left_right_bottom": ("SOLID", "SOLID", "NONE", "SOLID"),
        }.items()
    }
    if (
        neutral is None
        or header_fill is None
        or black is None
        or invisible is None
        or any(value is None for value in bogi_parts.values())
    ):
        raise HwpxRenderError(
            "template needs the reference bogi and choice borderFill records"
        )
    return (
        str(neutral),
        str(header_fill),
        str(black),
        str(invisible),
        {key: str(value) for key, value in bogi_parts.items()},
    )


def _hwpunit(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise HwpxRenderError(f"{label} must be a non-negative number")
    return round(float(value) * HWPUNIT_PER_MM)


def _load_template_context(output: OutputPlan) -> _TemplateContext:
    profile_data = _read_exact(
        output.design_profile_path,
        output.design_profile_sha256,
        f"{output.document_kind} design profile",
    )
    profile = _json_bytes(profile_data, f"{output.document_kind} design profile")
    template_data = _read_exact(
        output.template_path,
        output.template_sha256,
        f"{output.document_kind} template",
    )
    try:
        with zipfile.ZipFile(io.BytesIO(template_data)) as archive:
            members = {name: archive.read(name) for name in REQUIRED_HWPX_MEMBERS}
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise HwpxRenderError(f"cannot load clean HWPX template: {exc}") from exc

    try:
        header = ElementTree.fromstring(members["Contents/header.xml"])
        section = ElementTree.fromstring(members["Contents/section0.xml"])
        hpf = ElementTree.fromstring(members["Contents/content.hpf"])
    except ElementTree.ParseError as exc:
        raise HwpxRenderError(f"template XML changed after preflight: {exc}") from exc
    design = profile.get("design")
    if not isinstance(design, Mapping):
        raise HwpxRenderError("design profile has no design object")
    style_names = design.get("styles")
    if not isinstance(style_names, Mapping):
        raise HwpxRenderError("design profile has no style bindings")
    styles = _style_refs(header, style_names)
    underline_ids, header_changed = _underline_character_properties(
        header,
        {refs.character_property_id for refs in styles.values()},
    )
    if header_changed:
        members["Contents/header.xml"] = _xml_bytes_with_original_namespaces(
            header, members["Contents/header.xml"]
        )

    paragraphs = section.findall(f"{{{HP_NS}}}p")
    if len(paragraphs) != 1:
        raise HwpxRenderError("clean template must have exactly one paragraph")
    first = paragraphs[0]
    section_properties = first.find(f".//{{{HP_NS}}}secPr")
    column_controls = [
        control
        for control in first.findall(f".//{{{HP_NS}}}ctrl")
        if control.find(f"{{{HP_NS}}}colPr") is not None
    ]
    if section_properties is None or len(column_controls) != 1:
        raise HwpxRenderError("template must expose one secPr and one colPr control")
    page = section_properties.find(f"{{{HP_NS}}}pagePr")
    margin = page.find(f"{{{HP_NS}}}margin") if page is not None else None
    column = column_controls[0].find(f"{{{HP_NS}}}colPr")
    if page is None or margin is None or column is None:
        raise HwpxRenderError("template page or column geometry is incomplete")
    try:
        page_width = int(page.attrib["width"])
        page_height = int(page.attrib["height"])
        margins = {key: int(margin.attrib[key]) for key in ("left", "right", "top", "bottom")}
        column_count = int(column.attrib["colCount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HwpxRenderError("template page or column geometry is invalid") from exc
    page_profile = design.get("page")
    columns_profile = design.get("columns")
    flow = design.get("flow")
    if not all(isinstance(value, Mapping) for value in (page_profile, columns_profile, flow)):
        raise HwpxRenderError("design profile page, columns, and flow are required")
    expected_geometry = {
        "width": _hwpunit(page_profile.get("width_mm"), "page.width_mm"),
        "height": _hwpunit(page_profile.get("height_mm"), "page.height_mm"),
        **{
            side: _hwpunit(page_profile.get("margins_mm", {}).get(side), f"margin.{side}")
            for side in ("left", "right", "top", "bottom")
        },
    }
    actual_geometry = {"width": page_width, "height": page_height, **margins}
    if any(abs(actual_geometry[key] - expected_geometry[key]) > 2 for key in expected_geometry):
        raise HwpxRenderError("approved profile geometry does not match the clean template")
    if column_count != columns_profile.get("count"):
        raise HwpxRenderError("approved profile column count does not match the clean template")
    try:
        column_gap = int(column.get("sameGap", "0"))
    except ValueError as exc:
        raise HwpxRenderError("template column gap is invalid") from exc
    expected_gap = _hwpunit(columns_profile.get("gap_mm"), "columns.gap_mm")
    if abs(column_gap - expected_gap) > 2:
        raise HwpxRenderError("approved profile column gap does not match the clean template")
    page_writable_width = page_width - margins["left"] - margins["right"]
    writable_height = page_height - margins["top"] - margins["bottom"]
    writable_width = (
        page_writable_width - column_gap * (column_count - 1)
    ) // column_count
    if writable_width <= 0 or writable_height <= 0:
        raise HwpxRenderError("template has no positive writable page area")
    manifest = hpf.find(f"{{{OPF_NS}}}manifest")
    if manifest is None:
        raise HwpxRenderError("template content.hpf has no manifest")
    manifest_ids = frozenset(
        node.get("id") for node in manifest.findall(f"{{{OPF_NS}}}item") if node.get("id")
    )
    centered = [
        node.get("id")
        for node in header.iter(f"{{{HH_NS}}}paraPr")
        if (
            node.get("id")
            and (align := node.find(f"{{{HH_NS}}}align")) is not None
            and align.get("horizontal") == "CENTER"
        )
    ]
    if not centered:
        raise HwpxRenderError("template needs a centered paragraph property for page numbers")
    (
        neutral_border,
        header_fill,
        bogi_border,
        choice_grid_border,
        bogi_border_parts,
    ) = _border_fill_refs(header)
    return _TemplateContext(
        members=members,
        styles=styles,
        section_root=section,
        section_properties=copy.deepcopy(section_properties),
        column_control=copy.deepcopy(column_controls[0]),
        page_writable_width=page_writable_width,
        writable_width=writable_width,
        writable_height=writable_height,
        page_break_between_items=flow.get("page_break_between_items") is True,
        centered_paragraph_property_id=str(centered[0]),
        neutral_border_fill_id=neutral_border,
        neutral_header_fill_id=header_fill,
        bogi_border_fill_id=bogi_border,
        choice_grid_border_fill_id=choice_grid_border,
        bogi_border_fill_ids=bogi_border_parts,
        manifest_ids=manifest_ids,
        underline_character_property_ids=underline_ids,
    )


def _equation_geometry(script: str, writable_width: int) -> tuple[int, int, int, int]:
    if re.fullmatch(r"[A-Za-z0-9_{}\s,~+\-]+", script) and re.search(r"\bvec\b", script):
        vector_count = len(re.findall(r"\bvec\b", script))
        visible = re.sub(r"\bvec\b|[{}_~\s]", "", script)
        width = min(max(len(visible) * 450 + vector_count * 625, 525), writable_width)
        return width, 1125, 85, 956
    reduced = re.sub(
        r"\b(?:LEFT|RIGHT|over|sqrt|times|leq|geq|rm|it|bar|sigma)\b",
        ".",
        script,
        flags=re.IGNORECASE,
    )
    width = min(max(len(reduced) * 400, 525), 30000, writable_width)
    fraction = bool(re.search(r"(?:^|\s)over(?:\s|$)", script))
    root = "sqrt" in script or re.search(r"(?:^|\s)root(?:\s|$)", script)
    integral = bool(re.search(r"(?:^|\s)(?:d?int|tint|o(?:d|t)?int)(?:_|\s)", script))
    if fraction or integral:
        return width, 2580, 65, 1677
    if root:
        return width, 1478, 87, 1301
    return width, 1125, 85, 956


def _estimate_text_units(text: str) -> int:
    """Estimate visible width using the selected engine validator's units."""

    total = 0
    for character in text or "":
        code = ord(character)
        if character.isspace():
            total += 260
        elif (
            0xAC00 <= code <= 0xD7AF
            or 0x3130 <= code <= 0x318F
            or 0x4E00 <= code <= 0x9FFF
            or 0x3040 <= code <= 0x30FF
        ):
            total += 620
        elif character in ",.;:!?()[]{}<>/\\-+*=~'\"":
            total += 260
        else:
            total += 360
    return total


def _text_fragments(text: str, max_units: int) -> list[str]:
    """Split text at word boundaries, then characters when one word is too wide."""

    result: list[str] = []
    for token in re.findall(r"\s+|\S+", text):
        if _estimate_text_units(token) <= max_units:
            result.append(token)
            continue
        chunk = ""
        units = 0
        for character in token:
            width = _estimate_text_units(character)
            if chunk and units + width > max_units:
                result.append(chunk)
                chunk = ""
                units = 0
            chunk += character
            units += width
        if chunk:
            result.append(chunk)
    return result


def _inline_object_width(element: ElementTree.Element, max_units: int) -> int:
    if element.tag == f"{{{HP_NS}}}ctrl":
        return 0
    if element.tag == f"{{{HP_NS}}}equation":
        size = element.find(f"{{{HP_NS}}}sz")
        margin = element.find(f"{{{HP_NS}}}outMargin")
        try:
            width = int(size.get("width", "0")) if size is not None else max_units
            if margin is not None:
                width += int(margin.get("left", "0")) + int(margin.get("right", "0"))
            return max(1, width)
        except ValueError:
            return max_units
    if element.tag == f"{{{HP_NS}}}pic":
        size = element.find(f"{{{HP_NS}}}curSz")
        try:
            return max(1, int(size.get("width", str(max_units)))) if size is not None else max_units
        except ValueError:
            return max_units
    return max_units


def _append_text(
    line: list[tuple[str | None, ElementTree.Element]],
    text: str,
    mark: str | None = None,
) -> None:
    if not text:
        return
    text_tag = f"{{{HP_NS}}}t"
    if line and line[-1][0] == mark and line[-1][1].tag == text_tag:
        line[-1][1].text = (line[-1][1].text or "") + text
        return
    node = ElementTree.Element(
        text_tag,
        {"{http://www.w3.org/XML/1998/namespace}space": "preserve"},
    )
    node.text = text
    line.append((mark, node))


def _split_paragraph_content(
    content_xml: str,
    available_width: int,
) -> list[list[tuple[str | None, ElementTree.Element]]]:
    """Expand trusted inline XML into editable single-line paragraph contents."""

    try:
        root = ElementTree.fromstring(
            f'<root xmlns:hp="{HP_NS}" xmlns:hc="{HC_NAMESPACE}" '
            f'xmlns:qjs="{_MARK_NAMESPACE}">{content_xml}</root>'
        )
    except ElementTree.ParseError as exc:
        raise HwpxRenderError(f"cannot inspect generated paragraph content: {exc}") from exc

    max_units = max(1200, int(available_width * _LINE_WRAP_WIDTH_RATIO))
    lines: list[list[tuple[str | None, ElementTree.Element]]] = [[]]
    used_units = 0

    def hard_break() -> None:
        nonlocal used_units
        lines.append([])
        used_units = 0

    def add_text(text: str, mark: str | None = None) -> None:
        nonlocal used_units
        for fragment in _text_fragments(text, max_units):
            width = _estimate_text_units(fragment)
            if lines[-1] and used_units + width > max_units:
                hard_break()
            _append_text(lines[-1], fragment, mark)
            used_units += width

    for child in root:
        if child.tag == f"{{{HP_NS}}}lineBreak":
            hard_break()
        elif child.tag == f"{{{HP_NS}}}t":
            add_text(child.text or "")
        elif child.tag == _MARK_TAG:
            marked_children = list(child)
            if (
                not (child.get("kind") == "underline" or re.fullmatch(r"document-style:\d+:\d+", child.get("kind", "")))
                or len(marked_children) != 1
                or marked_children[0].tag != f"{{{HP_NS}}}t"
            ):
                raise HwpxRenderError("generated marked text fragment is invalid")
            add_text(marked_children[0].text or "", child.get("kind"))
        else:
            width = _inline_object_width(child, max_units)
            if lines[-1] and used_units + width > max_units:
                hard_break()
            lines[-1].append((None, copy.deepcopy(child)))
            used_units += width
        if child.tail:
            add_text(child.tail)
    return lines


def _line_metrics(
    elements: list[tuple[str | None, ElementTree.Element]],
    available_width: int,
) -> tuple[int, int]:
    height, baseline = 1000, 850
    document_ascent, document_descent = 0, 0
    for _mark, element in elements:
        for equation in element.iter(f"{{{HP_NS}}}equation"):
            script = equation.find(f"{{{HP_NS}}}script")
            _width, equation_height, _equation_baseline, line_baseline = (
                _equation_geometry(script.text or "", available_width)
            )
            height = max(height, equation_height)
            baseline = max(baseline, line_baseline)
        if _mark and _mark.startswith('document-style:'):
            size = int(_mark.rsplit(':', 1)[1])
            document_ascent = max(document_ascent, round(size * .85))
            document_descent = max(document_descent, size - round(size * .85))
        for item in element.iter():
            ascent = item.get(f'{{{_MARK_NAMESPACE}}}ascent')
            descent = item.get(f'{{{_MARK_NAMESPACE}}}descent')
            if ascent is not None and descent is not None:
                document_ascent = max(document_ascent, int(ascent))
                document_descent = max(document_descent, int(descent))
    if document_ascent or document_descent:
        descent = max(height - baseline, document_descent)
        baseline = max(baseline, document_ascent)
        height = baseline + descent
    return height, baseline


class _Renderer:
    def __init__(self, kind: str, context: _TemplateContext) -> None:
        self.kind = kind
        self.context = context
        self.allocator = BuildIdAllocator.from_xml(
            (context.members["Contents/header.xml"], context.members["Contents/section0.xml"])
        )
        self.paragraphs: list[ElementTree.Element] = []
        self.preview: list[str] = []
        self.canonical: Counter[str] = Counter()
        self.binaries: dict[str, bytes] = {}
        self._asset_refs: dict[tuple[str, str], tuple[str, int, int]] = {}
        self._manifest_ids = set(context.manifest_ids)
        self._next_image = 1
        self._endnote_numbers: list[int] = []
        self.selector_texts: dict[str, list[str]] = {
            "content.answer": [],
            "content.explanation": [],
        }

    def _paragraph(
        self,
        content: str,
        preview: str,
        *,
        style: str = "paragraph",
        page_break: bool = False,
        target: list[ElementTree.Element] | None = None,
        available_width: int | None = None,
    ) -> ElementTree.Element:
        refs = self.context.styles[style]
        width = available_width or self.context.writable_width
        if width < 1:
            raise HwpxRenderError("paragraph has no writable width")
        destination = self.paragraphs if target is None else target
        paragraphs: list[ElementTree.Element] = []
        for index, elements in enumerate(_split_paragraph_content(content, width)):
            vertical_size, baseline = _line_metrics(elements, width)
            line = line_segment_xml(
                vertical_position=0,
                vertical_size=vertical_size,
                text_height=vertical_size,
                baseline=baseline,
                spacing=600,
                horizontal_size=width,
            )
            runs: list[ElementTree.Element] = []
            for mark, element in elements:
                for item in element.iter():
                    item.attrib.pop(f'{{{_MARK_NAMESPACE}}}ascent', None)
                    item.attrib.pop(f'{{{_MARK_NAMESPACE}}}descent', None)
                character_property_id = (
                    self.context.underline_character_property_ids[
                        refs.character_property_id
                    ]
                    if mark == "underline"
                    else refs.character_property_id
                )
                if mark and mark.startswith('document-style:'):
                    character_property_id = mark.split(':')[1]
                if not runs or runs[-1].get("charPrIDRef") != character_property_id:
                    runs.append(
                        ElementTree.Element(
                            f"{{{HP_NS}}}run",
                            {"charPrIDRef": character_property_id},
                        )
                    )
                runs[-1].append(element)
            if not runs:
                runs.append(
                    ElementTree.Element(
                        f"{{{HP_NS}}}run",
                        {"charPrIDRef": refs.character_property_id},
                    )
                )
            runs_xml = "".join(
                ElementTree.tostring(
                    run,
                    encoding="unicode",
                    short_empty_elements=True,
                )
                for run in runs
            )
            xml = paragraph_runs_xml(
                runs_xml,
                paragraph_id=self.allocator.next_object_id(),
                paragraph_property_id=refs.paragraph_property_id,
                style_id=refs.style_id,
                line_segment=line,
                page_break=page_break and index == 0,
            )
            paragraphs.append(_fragment(xml))
        destination.extend(paragraphs)
        if preview:
            self.preview.append(preview)
        return paragraphs[0]

    def _inlines(
        self,
        inlines: Any,
        path: str,
        *,
        prefix: str = "",
        allow_equation: bool = True,
        available_width: int | None = None,
    ) -> tuple[str, str, str]:
        if not isinstance(inlines, list) or not inlines:
            raise HwpxRenderError(f"{path} must be a non-empty inline array")
        xml = text_xml(prefix) if prefix else ""
        plain = prefix
        visible = prefix
        for index, inline in enumerate(inlines):
            here = f"{path}[{index}]"
            if not isinstance(inline, Mapping):
                raise HwpxRenderError(f"{here} must be an object")
            inline_type = inline.get("type")
            if inline_type == "text":
                marks = inline.get("marks")
                if marks is None:
                    marks = []
                if not isinstance(marks, list) or marks not in ([], ["underline"]):
                    raise HwpxRenderError(
                        f"{here}.marks contains unsupported renderer v0.6 formatting"
                    )
                text = inline.get("text")
                if not isinstance(text, str):
                    raise HwpxRenderError(f"{here}.text must be a string")
                for part in re.split(r"(\r\n|\r|\n)", text):
                    if part in {"\r\n", "\r", "\n"}:
                        xml += "<hp:lineBreak/>"
                    elif marks == ["underline"]:
                        xml += (
                            '<qjs:mark kind="underline">'
                            f"{text_xml(part)}</qjs:mark>"
                        )
                    else:
                        xml += text_xml(part)
                plain += text
                visible += text
            elif inline_type == "equation" and allow_equation:
                script = inline.get("hwp_script")
                if not isinstance(script, str) or not script.strip():
                    raise HwpxRenderError(f"{here}.hwp_script must be non-empty")
                width, height, equation_baseline, _line_baseline = _equation_geometry(
                    script, available_width or self.context.writable_width
                )
                xml += equation_xml(
                    script,
                    self.allocator,
                    width=width,
                    height=height,
                    baseline=equation_baseline,
                    base_unit=1100,
                    font="HYhwpEQ",
                    text_color="#000000",
                    horizontal_margin=56,
                )
                plain += str(inline.get("latex") or inline.get("source_text") or script)
                self.canonical["equation"] += 1
            elif inline_type == "line_break":
                xml += "<hp:lineBreak/>"
                plain += "\n"
                visible += "\n"
            else:
                raise HwpxRenderError(f"unsupported inline type at {here}: {inline_type!r}")
        return xml, plain, visible

    def _paragraph_block(
        self,
        block: Mapping[str, Any],
        path: str,
        *,
        prefix: str = "",
        style: str = "paragraph",
        target: list[ElementTree.Element] | None = None,
        available_width: int | None = None,
    ) -> str:
        self.canonical["block.paragraph"] += 1
        xml, plain, visible = self._inlines(
            block.get("inlines"),
            f"{path}.inlines",
            prefix=prefix,
            available_width=available_width,
        )
        self._paragraph(
            xml,
            plain,
            style=style,
            target=target,
            available_width=available_width,
        )
        if (
            self.kind == "explanation" and visible.strip()
        ):
            if ".content.explanation" in path:
                self.selector_texts["content.explanation"].append(visible)
            elif ".content.answer" in path:
                self.selector_texts["content.answer"].append(visible)
        return visible

    def _lineage_reaches(
        self,
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
            if (
                current.get("role") != "figure_regenerated"
                or current.get("section") not in {self.kind, "shared"}
                or not isinstance(current.get("generation"), Mapping)
            ):
                return False
            seen.add(current_id)
            parent_id = current.get("derived_from_asset_id")
            if not isinstance(parent_id, str) or parent_id not in assets:
                return False
            current = assets[parent_id]

    def _embed_png_asset(
        self,
        item: _LoadedItem,
        asset: Mapping[str, Any],
        path: str,
        *,
        available_width: int,
        available_height: int,
    ) -> tuple[str, int, int]:
        asset_id = asset.get("asset_id")
        if not isinstance(asset_id, str) or asset.get("state") != "ready":
            raise HwpxRenderError(f"{path} raster asset is not ready")
        if asset.get("media_type") != "image/png":
            raise HwpxRenderError(f"{path} raster asset must be image/png")
        cache_key = (item.item_id, asset_id)
        cached = self._asset_refs.get(cache_key)
        if cached:
            binary_id, width_px, height_px = cached
        else:
            raw_path = asset.get("path")
            if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path or ":" in raw_path:
                raise HwpxRenderError(f"{path} raster asset path is unsafe")
            relative = PurePosixPath(raw_path)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise HwpxRenderError(f"{path} raster asset path is unsafe")
            item_root = item.path.parent.resolve()
            asset_path = item_root.joinpath(*relative.parts).resolve()
            if not asset_path.is_relative_to(item_root) or not asset_path.is_file():
                raise HwpxRenderError(f"{path} raster asset file is missing or escapes its item")
            try:
                raw = asset_path.read_bytes()
            except OSError as exc:
                raise HwpxRenderError(f"cannot read raster asset {asset_path}: {exc}") from exc
            declared_sha = asset.get("sha256")
            if not isinstance(declared_sha, str) or _sha256_bytes(raw) != declared_sha.lower():
                raise HwpxRenderError(f"{path} raster asset SHA-256 mismatch")
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    actual_format = image.format
                    width_px, height_px = image.size
                    image.verify()
            except Exception as exc:
                raise HwpxRenderError(f"{path} raster asset is not a valid PNG: {exc}") from exc
            if actual_format != "PNG" or asset_path.suffix.lower() != ".png":
                raise HwpxRenderError(f"{path} raster asset MIME/extension mismatch")
            declared_width = asset.get("width_px")
            declared_height = asset.get("height_px")
            if (
                isinstance(declared_width, bool)
                or not isinstance(declared_width, int)
                or isinstance(declared_height, bool)
                or not isinstance(declared_height, int)
                or declared_width != width_px
                or declared_height != height_px
            ):
                raise HwpxRenderError(f"{path} raster asset dimensions mismatch")
            try:
                bmp = png_to_rgb_bmp(raw)
            except Exception as exc:
                raise HwpxRenderError(f"cannot convert PNG raster to RGB BMP: {exc}") from exc
            while True:
                binary_id = f"image{self._next_image}"
                self._next_image += 1
                if binary_id not in self._manifest_ids:
                    break
            self._manifest_ids.add(binary_id)
            self.binaries[binary_id] = bmp
            self._asset_refs[cache_key] = (binary_id, width_px, height_px)
        scale = min(
            HWPUNIT_PER_SOURCE_PIXEL,
            available_width / width_px,
            available_height / height_px,
        )
        return binary_id, max(1, round(width_px * scale)), max(1, round(height_px * scale))

    def _figure_asset(
        self,
        item: _LoadedItem,
        block: Mapping[str, Any],
        path: str,
        *,
        available_width: int,
        available_height: int,
    ) -> tuple[str, int, int]:
        resources = item.value.get("resources")
        values = resources.get("assets") if isinstance(resources, Mapping) else None
        if not isinstance(values, list):
            raise HwpxRenderError(f"{path}: resources.assets must be an array")
        assets: dict[str, Mapping[str, Any]] = {}
        for asset in values:
            if not isinstance(asset, Mapping) or not isinstance(asset.get("asset_id"), str):
                continue
            asset_id = str(asset["asset_id"])
            if asset_id in assets:
                raise HwpxRenderError(f"duplicate asset_id in {item.item_id}: {asset_id}")
            assets[asset_id] = asset
        asset_id = block.get("asset_id")
        original_id = block.get("original_asset_id")
        if not isinstance(asset_id, str) or asset_id not in assets:
            raise HwpxRenderError(f"{path}.asset_id does not identify a local asset")
        if not isinstance(original_id, str) or original_id not in assets:
            raise HwpxRenderError(f"{path}.original_asset_id does not identify a local asset")
        selected = assets[asset_id]
        original = assets[original_id]
        if selected.get("role") == "figure_generated" or original.get("role") == "figure_generated":
            if asset_id != original_id or selected.get("section") not in {self.kind, "shared"}:
                raise HwpxRenderError(f"{path} generated figure must reference itself in its section")
            if selected.get("state") != "ready":
                raise HwpxRenderError(f"{path} generated figure is not ready")
            return self._embed_png_asset(
                item,
                selected,
                path,
                available_width=available_width,
                available_height=available_height,
            )
        if original.get("role") != "figure_original":
            raise HwpxRenderError(f"{path}.original_asset_id is not a figure original")
        original_region = original.get("source_region")
        if (
            original.get("section") not in {self.kind, "shared"}
            or not isinstance(original_region, Mapping)
            or original.get("derived_from_asset_id")
            != original_region.get("source_asset_id")
        ):
            raise HwpxRenderError(f"{path} figure original has invalid source lineage")
        if selected.get("role") not in {"figure_original", "figure_regenerated"}:
            raise HwpxRenderError(f"{path} selected asset is not a v0.6 figure raster")
        if not self._lineage_reaches(selected, original_id, assets):
            raise HwpxRenderError(f"{path} selected asset is outside the figure lineage")
        if selected.get("section") not in {self.kind, "shared"}:
            raise HwpxRenderError(f"{path} selected figure belongs to another section")
        review = selected.get("review")
        selected_ready = (
            selected.get("state") == "ready"
            and isinstance(review, Mapping)
            and review.get("status") == "approved"
            and review.get("boundary_uncertain") is False
        )
        effective = selected if selected_ready else original
        return self._embed_png_asset(
            item,
            effective,
            path,
            available_width=available_width,
            available_height=available_height,
        )

    def _figure(
        self,
        item: _LoadedItem,
        block: Mapping[str, Any],
        path: str,
        *,
        target: list[ElementTree.Element] | None = None,
        available_width: int | None = None,
        available_height: int | None = None,
    ) -> None:
        writable_width = available_width or self.context.writable_width
        binary_id, width, height = self._figure_asset(
            item,
            block,
            path,
            available_width=writable_width,
            available_height=available_height or self.context.writable_height,
        )
        self.canonical["block.figure"] += 1
        self._paragraph(
            picture_xml(binary_id, self.allocator, width=width, height=height),
            "[그림]",
            target=target,
            available_width=writable_width,
        )
        caption = block.get("caption")
        if caption:
            xml, plain, _visible = self._inlines(
                caption,
                f"{path}.caption",
                allow_equation=False,
                available_width=writable_width,
            )
            self._paragraph(
                xml,
                plain,
                style="figure_caption",
                target=target,
                available_width=writable_width,
            )

    @staticmethod
    def _paragraphs_height(paragraphs: list[ElementTree.Element]) -> int:
        total = 0
        for paragraph in paragraphs:
            height = 1600
            for segment in paragraph.iter(f"{{{HP_NS}}}lineseg"):
                try:
                    height = max(
                        height,
                        int(segment.get("vertpos", "0"))
                        + int(segment.get("vertsize", "1000"))
                        + int(segment.get("spacing", "600")),
                    )
                except ValueError:
                    pass
            for size in paragraph.iter(f"{{{HP_NS}}}curSz"):
                try:
                    height = max(height, int(size.get("height", "0")) + 568)
                except ValueError:
                    pass
            for size in paragraph.iter(f"{{{HP_NS}}}sz"):
                parent_height = size.get("height")
                if parent_height:
                    try:
                        height = max(height, int(parent_height) + 568)
                    except ValueError:
                        pass
            total += height
        return max(total, 1600)

    def _table_paragraph(
        self,
        table: ElementTree.Element,
        *,
        target: list[ElementTree.Element] | None,
        available_width: int,
    ) -> None:
        paragraph = self._paragraph(
            "",
            "",
            target=target,
            available_width=available_width,
        )
        run = paragraph.find(f"{{{HP_NS}}}run")
        if run is None:
            raise HwpxRenderError("native table paragraph has no run")
        run.append(table)

    def _one_cell_table(
        self,
        paragraphs: list[ElementTree.Element],
        *,
        width: int,
        padding: int,
        border_fill_id: str | None = None,
    ) -> ElementTree.Element:
        hp = f"{{{HP_NS}}}"
        content_width = width - padding * 2
        if content_width < 1:
            raise HwpxRenderError("box has no writable cell width")
        if not paragraphs:
            self._paragraph("", "", target=paragraphs, available_width=content_width)
        height = self._paragraphs_height(paragraphs) + padding * 2
        border_fill_id = border_fill_id or self.context.neutral_border_fill_id
        table = ElementTree.Element(
            hp + "tbl",
            {
                "id": str(self.allocator.next_object_id()),
                "zOrder": str(self.allocator.next_z_order()),
                "numberingType": "TABLE",
                "textWrap": "TOP_AND_BOTTOM",
                "textFlow": "BOTH_SIDES",
                "lock": "0",
                "dropcapstyle": "None",
                "pageBreak": "NONE",
                "repeatHeader": "1",
                "rowCnt": "1",
                "colCnt": "1",
                "cellSpacing": "0",
                "borderFillIDRef": border_fill_id,
                "noAdjust": "0",
            },
        )
        ElementTree.SubElement(
            table,
            hp + "sz",
            {
                "width": str(width),
                "widthRelTo": "ABSOLUTE",
                "height": str(height),
                "heightRelTo": "ABSOLUTE",
                "protect": "0",
            },
        )
        ElementTree.SubElement(
            table,
            hp + "pos",
            {
                "treatAsChar": "1",
                "affectLSpacing": "0",
                "flowWithText": "1",
                "allowOverlap": "0",
                "holdAnchorAndSO": "0",
                "vertRelTo": "PARA",
                "horzRelTo": "PARA",
                "vertAlign": "TOP",
                "horzAlign": "LEFT",
                "vertOffset": "0",
                "horzOffset": "0",
            },
        )
        ElementTree.SubElement(
            table, hp + "outMargin", {"left": "0", "right": "0", "top": "284", "bottom": "284"}
        )
        ElementTree.SubElement(
            table,
            hp + "inMargin",
            {side: str(padding) for side in ("left", "right", "top", "bottom")},
        )
        row = ElementTree.SubElement(table, hp + "tr")
        cell = ElementTree.SubElement(
            row,
            hp + "tc",
            {
                "name": "",
                "header": "0",
                "hasMargin": "1",
                "protect": "0",
                "editable": "0",
                "dirty": "0",
                "borderFillIDRef": border_fill_id,
            },
        )
        sublist = ElementTree.SubElement(
            cell,
            hp + "subList",
            {
                "id": "",
                "textDirection": "HORIZONTAL",
                "lineWrap": "BREAK",
                "vertAlign": "CENTER",
                "linkListIDRef": "0",
                "linkListNextIDRef": "0",
                "textWidth": str(content_width),
                "textHeight": str(max(1, height - padding * 2)),
                "hasTextRef": "0",
                "hasNumRef": "0",
            },
        )
        sublist.extend(paragraphs)
        ElementTree.SubElement(cell, hp + "cellAddr", {"colAddr": "0", "rowAddr": "0"})
        ElementTree.SubElement(cell, hp + "cellSpan", {"colSpan": "1", "rowSpan": "1"})
        ElementTree.SubElement(cell, hp + "cellSz", {"width": str(width), "height": str(height)})
        ElementTree.SubElement(
            cell,
            hp + "cellMargin",
            {side: str(padding) for side in ("left", "right", "top", "bottom")},
        )
        return table

    def _paragraph_grid_table(
        self,
        cells: list[list[ElementTree.Element]],
        *,
        width: int,
        columns: int,
        padding: int,
        border_fill_id: str,
    ) -> ElementTree.Element:
        if columns < 1 or not cells:
            raise HwpxRenderError("choice grid needs cells and columns")
        hp = f"{{{HP_NS}}}"
        base_width, remainder = divmod(width, columns)
        column_widths = [
            base_width + (1 if index < remainder else 0) for index in range(columns)
        ]
        rows = (len(cells) + columns - 1) // columns
        rendered: list[list[list[ElementTree.Element]]] = []
        row_heights: list[int] = []
        for row_index in range(rows):
            row_cells: list[list[ElementTree.Element]] = []
            height = 1600
            for column_index, column_width in enumerate(column_widths):
                cell_index = row_index * columns + column_index
                paragraphs = cells[cell_index] if cell_index < len(cells) else []
                content_width = max(1, column_width - padding * 2)
                if not paragraphs:
                    self._paragraph(
                        "", "", target=paragraphs, available_width=content_width
                    )
                height = max(height, self._paragraphs_height(paragraphs) + padding * 2)
                row_cells.append(paragraphs)
            rendered.append(row_cells)
            row_heights.append(height)

        table = ElementTree.Element(
            hp + "tbl",
            {
                "id": str(self.allocator.next_object_id()),
                "zOrder": str(self.allocator.next_z_order()),
                "numberingType": "TABLE",
                "textWrap": "TOP_AND_BOTTOM",
                "textFlow": "BOTH_SIDES",
                "lock": "0",
                "dropcapstyle": "None",
                "pageBreak": "CELL",
                "repeatHeader": "0",
                "rowCnt": str(rows),
                "colCnt": str(columns),
                "cellSpacing": "0",
                "borderFillIDRef": border_fill_id,
                "noAdjust": "0",
            },
        )
        total_height = sum(row_heights)
        ElementTree.SubElement(
            table,
            hp + "sz",
            {
                "width": str(width),
                "widthRelTo": "ABSOLUTE",
                "height": str(total_height),
                "heightRelTo": "ABSOLUTE",
                "protect": "0",
            },
        )
        ElementTree.SubElement(
            table,
            hp + "pos",
            {
                "treatAsChar": "1",
                "affectLSpacing": "0",
                "flowWithText": "1",
                "allowOverlap": "0",
                "holdAnchorAndSO": "0",
                "vertRelTo": "PARA",
                "horzRelTo": "PARA",
                "vertAlign": "TOP",
                "horzAlign": "LEFT",
                "vertOffset": "0",
                "horzOffset": "0",
            },
        )
        ElementTree.SubElement(
            table,
            hp + "outMargin",
            {"left": "0", "right": "0", "top": "0", "bottom": "0"},
        )
        ElementTree.SubElement(
            table,
            hp + "inMargin",
            {"left": "0", "right": "0", "top": "150", "bottom": "150"},
        )
        for row_index, row_cells in enumerate(rendered):
            row = ElementTree.SubElement(table, hp + "tr")
            for column_index, paragraphs in enumerate(row_cells):
                cell_width = column_widths[column_index]
                cell = ElementTree.SubElement(
                    row,
                    hp + "tc",
                    {
                        "name": "",
                        "header": "0",
                        "hasMargin": "1",
                        "protect": "0",
                        "editable": "0",
                        "dirty": "0",
                        "borderFillIDRef": border_fill_id,
                    },
                )
                sublist = ElementTree.SubElement(
                    cell,
                    hp + "subList",
                    {
                        "id": "",
                        "textDirection": "HORIZONTAL",
                        "lineWrap": "BREAK",
                        "vertAlign": "CENTER",
                        "linkListIDRef": "0",
                        "linkListNextIDRef": "0",
                        "textWidth": str(max(1, cell_width - padding * 2)),
                        "textHeight": str(max(1, row_heights[row_index] - padding * 2)),
                        "hasTextRef": "0",
                        "hasNumRef": "0",
                    },
                )
                sublist.extend(paragraphs)
                ElementTree.SubElement(
                    cell,
                    hp + "cellAddr",
                    {"colAddr": str(column_index), "rowAddr": str(row_index)},
                )
                ElementTree.SubElement(
                    cell, hp + "cellSpan", {"colSpan": "1", "rowSpan": "1"}
                )
                ElementTree.SubElement(
                    cell,
                    hp + "cellSz",
                    {"width": str(cell_width), "height": str(row_heights[row_index])},
                )
                ElementTree.SubElement(
                    cell,
                    hp + "cellMargin",
                    {side: str(padding) for side in ("left", "right", "top", "bottom")},
                )
        return table

    @staticmethod
    def _reference_bogi_title(value: str) -> str:
        compact = re.sub(r"[\s\[\]<>]", "", value)
        return "<보기>" if compact == "보기" else value

    def _reference_bogi_table(
        self,
        title: str,
        paragraphs: list[ElementTree.Element],
        *,
        available_width: int,
    ) -> ElementTree.Element:
        """Build the Hancom-authored 4x5 title-gap <보기> table."""

        hp = f"{{{HP_NS}}}"
        table_width = max(1, available_width - round(2.1 * HWPUNIT_PER_MM))
        edge = min(round(1.67 * HWPUNIT_PER_MM), table_width // 10)
        title_width = min(round(17 * HWPUNIT_PER_MM), table_width // 3)
        wing, remainder = divmod(table_width - edge * 2 - title_width, 2)
        column_widths = [edge, wing, title_width, wing + remainder, edge]
        title_height = 1070
        padding = 141
        content_height = self._paragraphs_height(paragraphs) + padding * 2
        bottom_height = 284
        row_heights = [title_height, title_height, content_height, bottom_height]
        borders = self.context.bogi_border_fill_ids

        title_paragraphs: list[ElementTree.Element] = []
        title_text = self._reference_bogi_title(title)
        title_paragraph = self._paragraph(
            text_xml(title_text),
            title_text,
            target=title_paragraphs,
            available_width=max(1, title_width - 1020),
        )
        title_paragraph.set(
            "paraPrIDRef", self.context.centered_paragraph_property_id
        )

        table = ElementTree.Element(
            hp + "tbl",
            {
                "id": str(self.allocator.next_object_id()),
                "zOrder": str(self.allocator.next_z_order()),
                "numberingType": "TABLE",
                "textWrap": "TOP_AND_BOTTOM",
                "textFlow": "BOTH_SIDES",
                "lock": "0",
                "dropcapstyle": "None",
                "pageBreak": "NONE",
                "repeatHeader": "1",
                "rowCnt": "4",
                "colCnt": "5",
                "cellSpacing": "0",
                "borderFillIDRef": borders["all"],
                "noAdjust": "0",
            },
        )
        ElementTree.SubElement(
            table,
            hp + "sz",
            {
                "width": str(table_width),
                "widthRelTo": "ABSOLUTE",
                "height": str(sum(row_heights)),
                "heightRelTo": "ABSOLUTE",
                "protect": "0",
            },
        )
        ElementTree.SubElement(
            table,
            hp + "pos",
            {
                "treatAsChar": "1",
                "affectLSpacing": "0",
                "flowWithText": "1",
                "allowOverlap": "0",
                "holdAnchorAndSO": "0",
                "vertRelTo": "PARA",
                "horzRelTo": "PARA",
                "vertAlign": "TOP",
                "horzAlign": "LEFT",
                "vertOffset": "0",
                "horzOffset": "0",
            },
        )
        ElementTree.SubElement(
            table,
            hp + "outMargin",
            {"left": "141", "right": "141", "top": "141", "bottom": "141"},
        )
        ElementTree.SubElement(
            table,
            hp + "inMargin",
            {"left": "140", "right": "140", "top": "140", "bottom": "140"},
        )

        def empty_paragraphs(width: int) -> list[ElementTree.Element]:
            result: list[ElementTree.Element] = []
            self._paragraph("", "", target=result, available_width=max(1, width))
            return result

        def add_cell(
            row: ElementTree.Element,
            *,
            row_index: int,
            column: int,
            col_span: int,
            row_span: int,
            border: str,
            values: list[ElementTree.Element] | None = None,
            horizontal_margin: int = 141,
        ) -> None:
            width = sum(column_widths[column : column + col_span])
            height = sum(row_heights[row_index : row_index + row_span])
            cell_values = values or empty_paragraphs(width - horizontal_margin * 2)
            cell = ElementTree.SubElement(
                row,
                hp + "tc",
                {
                    "name": "",
                    "header": "0",
                    "hasMargin": "1",
                    "protect": "0",
                    "editable": "0",
                    "dirty": "0",
                    "borderFillIDRef": border,
                },
            )
            sublist = ElementTree.SubElement(
                cell,
                hp + "subList",
                {
                    "id": "",
                    "textDirection": "HORIZONTAL",
                    "lineWrap": "BREAK",
                    "vertAlign": "CENTER",
                    "linkListIDRef": "0",
                    "linkListNextIDRef": "0",
                    "textWidth": str(max(1, width - horizontal_margin * 2)),
                    "textHeight": str(max(1, height - padding * 2)),
                    "hasTextRef": "0",
                    "hasNumRef": "0",
                },
            )
            sublist.extend(cell_values)
            ElementTree.SubElement(
                cell,
                hp + "cellAddr",
                {"colAddr": str(column), "rowAddr": str(row_index)},
            )
            ElementTree.SubElement(
                cell,
                hp + "cellSpan",
                {"colSpan": str(col_span), "rowSpan": str(row_span)},
            )
            ElementTree.SubElement(
                cell,
                hp + "cellSz",
                {"width": str(width), "height": str(height)},
            )
            ElementTree.SubElement(
                cell,
                hp + "cellMargin",
                {
                    "left": str(horizontal_margin),
                    "right": str(horizontal_margin),
                    "top": str(padding),
                    "bottom": str(padding),
                },
            )

        row0 = ElementTree.SubElement(table, hp + "tr")
        add_cell(row0, row_index=0, column=0, col_span=2, row_span=1, border=borders["bottom"])
        add_cell(
            row0,
            row_index=0,
            column=2,
            col_span=1,
            row_span=2,
            border=borders["none"],
            values=title_paragraphs,
            horizontal_margin=510,
        )
        add_cell(row0, row_index=0, column=3, col_span=2, row_span=1, border=borders["bottom"])

        row1 = ElementTree.SubElement(table, hp + "tr")
        add_cell(row1, row_index=1, column=0, col_span=2, row_span=1, border=borders["left_top"])
        add_cell(row1, row_index=1, column=3, col_span=2, row_span=1, border=borders["right_top"])

        row2 = ElementTree.SubElement(table, hp + "tr")
        add_cell(row2, row_index=2, column=0, col_span=1, row_span=1, border=borders["left"], horizontal_margin=0)
        add_cell(
            row2,
            row_index=2,
            column=1,
            col_span=3,
            row_span=1,
            border=borders["none"],
            values=paragraphs,
        )
        add_cell(row2, row_index=2, column=4, col_span=1, row_span=1, border=borders["right"], horizontal_margin=0)

        row3 = ElementTree.SubElement(table, hp + "tr")
        add_cell(
            row3,
            row_index=3,
            column=0,
            col_span=5,
            row_span=1,
            border=borders["left_right_bottom"],
        )
        return table

    def _bogi(
        self,
        item: _LoadedItem,
        block: Mapping[str, Any],
        path: str,
        *,
        target: list[ElementTree.Element] | None,
        available_width: int,
        depth: int,
        ancestors: frozenset[int],
    ) -> None:
        self.canonical["block.bogi"] += 1
        content_width = available_width - round(6.5 * HWPUNIT_PER_MM)
        if content_width < 1:
            raise HwpxRenderError(f"{path} bogi has no writable content width")
        paragraphs: list[ElementTree.Element] = []
        title = block.get("title")
        if not isinstance(title, Mapping):
            raise HwpxRenderError(f"{path}.title must be an object")
        value = title.get("text") if title.get("visible") is True else "<보기>"
        if not isinstance(value, str):
            raise HwpxRenderError(f"{path}.title.text must be a string")
        items = block.get("items")
        if not isinstance(items, list) or not items:
            raise HwpxRenderError(f"{path}.items must be non-empty")
        for item_index, child_item in enumerate(items):
            item_path = f"{path}.items[{item_index}]"
            if not isinstance(child_item, Mapping):
                raise HwpxRenderError(f"{item_path} must be an object")
            label = child_item.get("label")
            children = child_item.get("blocks")
            if not isinstance(label, str) or not label:
                raise HwpxRenderError(f"{item_path}.label must be visible")
            if not isinstance(children, list) or not children:
                raise HwpxRenderError(f"{item_path}.blocks must be non-empty")
            first = children[0]
            if isinstance(first, Mapping) and first.get("type") == "paragraph":
                self._paragraph_block(
                    first,
                    f"{item_path}.blocks[0]",
                    prefix=f"{label if label.endswith(('.', ')')) else label + '.'} ",
                    target=paragraphs,
                    available_width=content_width,
                )
                children = children[1:]
            else:
                self._paragraph(
                    text_xml(label if label.endswith((".", ")")) else label + "."),
                    label if label.endswith((".", ")")) else label + ".",
                    target=paragraphs,
                    available_width=content_width,
                )
            self._render_blocks(
                item,
                children,
                f"{item_path}.blocks",
                target=paragraphs,
                available_width=content_width,
                depth=depth + 1,
                ancestors=ancestors,
            )
        table = self._reference_bogi_table(
            value,
            paragraphs,
            available_width=available_width,
        )
        self._table_paragraph(table, target=target, available_width=available_width)

    def _box(
        self,
        item: _LoadedItem,
        block: Mapping[str, Any],
        path: str,
        *,
        target: list[ElementTree.Element] | None,
        available_width: int,
        depth: int,
        ancestors: frozenset[int],
    ) -> None:
        self.canonical["block.box"] += 1
        padding = round(2.5 * HWPUNIT_PER_MM)
        title = block.get("title")
        title_value: str | None = None
        if title is not None:
            if not isinstance(title, Mapping):
                raise HwpxRenderError(f"{path}.title must be an object")
            if title.get("visible") is True:
                value = title.get("text")
                if not isinstance(value, str):
                    raise HwpxRenderError(f"{path}.title.text must be a string")
                title_value = value
        reference_bogi = bool(
            title_value
            and re.sub(r"[\s\[\]<>]", "", title_value) == "보기"
        )
        cell_width = available_width - (
            round(6.5 * HWPUNIT_PER_MM) if reference_bogi else padding * 2
        )
        if cell_width < 1:
            raise HwpxRenderError(f"{path} box has no writable content width")
        paragraphs: list[ElementTree.Element] = []
        if title_value is not None and not reference_bogi:
            self._paragraph(
                text_xml(title_value),
                title_value,
                target=paragraphs,
                available_width=cell_width,
            )
        self._render_blocks(
            item,
            block.get("blocks"),
            f"{path}.blocks",
            target=paragraphs,
            available_width=cell_width,
            depth=depth + 1,
            ancestors=ancestors,
        )
        table = (
            self._reference_bogi_table(
                title_value or "<보기>",
                paragraphs,
                available_width=available_width,
            )
            if reference_bogi
            else self._one_cell_table(
                paragraphs, width=available_width, padding=padding
            )
        )
        self._table_paragraph(table, target=target, available_width=available_width)

    def _table_layout(
        self,
        block: Mapping[str, Any],
        path: str,
    ) -> tuple[int, list[list[tuple[Mapping[str, Any], int, int, int]]]]:
        column_count = block.get("column_count")
        rows = block.get("rows")
        if (
            isinstance(column_count, bool)
            or not isinstance(column_count, int)
            or not 1 <= column_count <= 64
            or not isinstance(rows, list)
            or not rows
        ):
            raise HwpxRenderError(f"{path} has invalid table dimensions")
        active = [0] * column_count
        result: list[list[tuple[Mapping[str, Any], int, int, int]]] = []
        for row_index, row in enumerate(rows):
            if not isinstance(row, Mapping) or not isinstance(row.get("cells"), list):
                raise HwpxRenderError(f"{path}.rows[{row_index}].cells must be an array")
            covered = [value > 0 for value in active]
            updates: dict[int, int] = {}
            cursor = 0
            placed: list[tuple[Mapping[str, Any], int, int, int]] = []
            for cell_index, cell in enumerate(row["cells"]):
                cell_path = f"{path}.rows[{row_index}].cells[{cell_index}]"
                if not isinstance(cell, Mapping):
                    raise HwpxRenderError(f"{cell_path} must be an object")
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
                    or row_index + row_span > len(rows)
                    or cursor + col_span > column_count
                    or any(covered[cursor : cursor + col_span])
                ):
                    raise HwpxRenderError(f"{cell_path} has an invalid or overlapping span")
                for column in range(cursor, cursor + col_span):
                    covered[column] = True
                    if row_span > 1:
                        updates[column] = max(updates.get(column, 0), row_span - 1)
                placed.append((cell, cursor, col_span, row_span))
                cursor += col_span
            if not all(covered):
                raise HwpxRenderError(f"{path}.rows[{row_index}] does not cover the table grid")
            active = [max(0, value - 1) for value in active]
            for column, value in updates.items():
                active[column] = max(active[column], value)
            result.append(placed)
        if any(active):
            raise HwpxRenderError(f"{path} has a row span beyond its final row")
        return column_count, result

    def _table(
        self,
        item: _LoadedItem,
        block: Mapping[str, Any],
        path: str,
        *,
        target: list[ElementTree.Element] | None,
        available_width: int,
        depth: int,
        ancestors: frozenset[int],
    ) -> None:
        self.canonical["block.table"] += 1
        caption = block.get("caption")
        if caption:
            xml, plain, _visible = self._inlines(
                caption,
                f"{path}.caption",
                allow_equation=False,
                available_width=available_width,
            )
            self._paragraph(
                xml,
                plain,
                style="figure_caption",
                target=target,
                available_width=available_width,
            )
        column_count, layout = self._table_layout(block, path)
        base_width, remainder = divmod(available_width, column_count)
        column_widths = [base_width + (1 if index < remainder else 0) for index in range(column_count)]
        padding = round(1.5 * HWPUNIT_PER_MM)
        rendered_rows: list[list[dict[str, Any]]] = []
        row_heights = [1600] * len(layout)
        for row_index, cells in enumerate(layout):
            rendered_cells: list[dict[str, Any]] = []
            for cell_index, (cell, column, col_span, row_span) in enumerate(cells):
                width = sum(column_widths[column : column + col_span])
                content_width = width - padding * 2
                if content_width < 1:
                    raise HwpxRenderError(f"{path} table cell has no writable width")
                paragraphs: list[ElementTree.Element] = []
                blocks = cell.get("blocks")
                if not isinstance(blocks, list):
                    raise HwpxRenderError(
                        f"{path}.rows[{row_index}].cells[{cell_index}].blocks must be an array"
                    )
                self._render_blocks(
                    item,
                    blocks,
                    f"{path}.rows[{row_index}].cells[{cell_index}].blocks",
                    target=paragraphs,
                    available_width=content_width,
                    depth=depth + 1,
                    ancestors=ancestors,
                    allowed=frozenset({"paragraph", "figure"}),
                )
                if not paragraphs:
                    self._paragraph(
                        "", "", target=paragraphs, available_width=content_width
                    )
                desired = self._paragraphs_height(paragraphs) + padding * 2
                rendered_cells.append(
                    {
                        "source": cell,
                        "column": column,
                        "col_span": col_span,
                        "row_span": row_span,
                        "width": width,
                        "paragraphs": paragraphs,
                        "desired_height": desired,
                    }
                )
                if row_span == 1:
                    row_heights[row_index] = max(row_heights[row_index], desired)
            rendered_rows.append(rendered_cells)
        for row_index, cells in enumerate(rendered_rows):
            for cell in cells:
                span = cell["row_span"]
                current = sum(row_heights[row_index : row_index + span])
                if current < cell["desired_height"]:
                    row_heights[row_index + span - 1] += cell["desired_height"] - current

        hp = f"{{{HP_NS}}}"
        table = ElementTree.Element(
            hp + "tbl",
            {
                "id": str(self.allocator.next_object_id()),
                "zOrder": str(self.allocator.next_z_order()),
                "numberingType": "TABLE",
                "textWrap": "TOP_AND_BOTTOM",
                "textFlow": "BOTH_SIDES",
                "lock": "0",
                "dropcapstyle": "None",
                "pageBreak": "CELL",
                "repeatHeader": "1" if any(
                    cell["source"].get("role") == "header" for cell in rendered_rows[0]
                ) else "0",
                "rowCnt": str(len(rendered_rows)),
                "colCnt": str(column_count),
                "cellSpacing": "0",
                "borderFillIDRef": self.context.neutral_border_fill_id,
                "noAdjust": "0",
            },
        )
        total_height = sum(row_heights)
        ElementTree.SubElement(
            table,
            hp + "sz",
            {
                "width": str(available_width),
                "widthRelTo": "ABSOLUTE",
                "height": str(total_height),
                "heightRelTo": "ABSOLUTE",
                "protect": "0",
            },
        )
        ElementTree.SubElement(
            table,
            hp + "pos",
            {
                "treatAsChar": "1",
                "affectLSpacing": "0",
                "flowWithText": "1",
                "allowOverlap": "0",
                "holdAnchorAndSO": "0",
                "vertRelTo": "PARA",
                "horzRelTo": "PARA",
                "vertAlign": "TOP",
                "horzAlign": "LEFT",
                "vertOffset": "0",
                "horzOffset": "0",
            },
        )
        ElementTree.SubElement(
            table, hp + "outMargin", {"left": "0", "right": "0", "top": "284", "bottom": "284"}
        )
        ElementTree.SubElement(
            table, hp + "inMargin", {"left": "140", "right": "140", "top": "140", "bottom": "140"}
        )
        for row_index, cells in enumerate(rendered_rows):
            row = ElementTree.SubElement(table, hp + "tr")
            for cell in cells:
                is_header = cell["source"].get("role") == "header"
                node = ElementTree.SubElement(
                    row,
                    hp + "tc",
                    {
                        "name": "",
                        "header": "1" if is_header else "0",
                        "hasMargin": "1",
                        "protect": "0",
                        "editable": "0",
                        "dirty": "0",
                        "borderFillIDRef": (
                            self.context.neutral_header_fill_id
                            if is_header
                            else self.context.neutral_border_fill_id
                        ),
                    },
                )
                content_height = sum(row_heights[row_index : row_index + cell["row_span"]])
                sublist = ElementTree.SubElement(
                    node,
                    hp + "subList",
                    {
                        "id": "",
                        "textDirection": "HORIZONTAL",
                        "lineWrap": "BREAK",
                        "vertAlign": "CENTER",
                        "linkListIDRef": "0",
                        "linkListNextIDRef": "0",
                        "textWidth": str(max(1, cell["width"] - padding * 2)),
                        "textHeight": str(max(1, content_height - padding * 2)),
                        "hasTextRef": "0",
                        "hasNumRef": "0",
                    },
                )
                sublist.extend(cell["paragraphs"])
                ElementTree.SubElement(
                    node,
                    hp + "cellAddr",
                    {"colAddr": str(cell["column"]), "rowAddr": str(row_index)},
                )
                ElementTree.SubElement(
                    node,
                    hp + "cellSpan",
                    {"colSpan": str(cell["col_span"]), "rowSpan": str(cell["row_span"])},
                )
                ElementTree.SubElement(
                    node,
                    hp + "cellSz",
                    {"width": str(cell["width"]), "height": str(content_height)},
                )
                ElementTree.SubElement(
                    node,
                    hp + "cellMargin",
                    {side: str(padding) for side in ("left", "right", "top", "bottom")},
                )
        self._table_paragraph(table, target=target, available_width=available_width)

    @staticmethod
    def _choice_grid_eligible(items: Any) -> bool:
        if not isinstance(items, list) or not items:
            return False
        for child in items:
            if (
                not isinstance(child, Mapping)
                or not isinstance(child.get("blocks"), list)
                or len(child["blocks"]) != 1
                or not isinstance(child["blocks"][0], Mapping)
                or child["blocks"][0].get("type") != "paragraph"
            ):
                return False
            inlines = child["blocks"][0].get("inlines")
            if not isinstance(inlines, list) or not inlines:
                return False
            weight = 0
            for inline in inlines:
                if not isinstance(inline, Mapping):
                    return False
                if inline.get("type") == "text":
                    text = inline.get("text")
                    if not isinstance(text, str) or "\n" in text or "\r" in text:
                        return False
                    weight += len("".join(text.split()))
                elif inline.get("type") == "equation":
                    weight += 8
                else:
                    return False
            if weight > 36:
                return False
        return True

    def _container(
        self,
        item: _LoadedItem,
        block: Mapping[str, Any],
        path: str,
        block_type: str,
        *,
        target: list[ElementTree.Element] | None,
        available_width: int,
        depth: int,
        ancestors: frozenset[int],
    ) -> None:
        if block_type == "bogi":
            self._bogi(
                item,
                block,
                path,
                target=target,
                available_width=available_width,
                depth=depth,
                ancestors=ancestors,
            )
            return
        self.canonical[f"block.{block_type}"] += 1
        items = block.get("items")
        if not isinstance(items, list) or not items:
            raise HwpxRenderError(f"{path}.items must be non-empty")
        if block_type == "choices" and block.get("source_layout") in {
            "horizontal",
            "grid",
        } and self._choice_grid_eligible(items):
            plain_parts: list[str] = []
            visible_parts: list[str] = []
            cells: list[list[ElementTree.Element]] = []
            padding = 141
            grid_width = max(1, available_width - round(2.73 * HWPUNIT_PER_MM))
            cell_width = max(1, grid_width // 3 - padding * 2)
            for item_index, child_item in enumerate(items):
                label = child_item.get("label")
                if not isinstance(label, str) or not label:
                    raise HwpxRenderError(f"{path}.items[{item_index}].label must be visible")
                paragraph = child_item["blocks"][0]
                paragraph_path = f"{path}.items[{item_index}].blocks[0]"
                self.canonical["block.paragraph"] += 1
                xml, plain, visible = self._inlines(
                    paragraph.get("inlines"),
                    f"{paragraph_path}.inlines",
                    prefix=f"{label} ",
                    available_width=cell_width,
                )
                paragraphs: list[ElementTree.Element] = []
                self._paragraph(
                    xml,
                    plain,
                    style="choice",
                    target=paragraphs,
                    available_width=cell_width,
                )
                cells.append(paragraphs)
                plain_parts.append(plain)
                visible_parts.append(visible)
            table = self._paragraph_grid_table(
                cells,
                width=grid_width,
                columns=3,
                padding=padding,
                border_fill_id=self.context.choice_grid_border_fill_id,
            )
            self.canonical["layout.choice_grid"] += 1
            self._table_paragraph(
                table,
                target=target,
                available_width=available_width,
            )
            separator = "    "
            visible = separator.join(visible_parts)
            if self.kind == "explanation" and visible.strip():
                if ".content.explanation" in path:
                    self.selector_texts["content.explanation"].append(visible)
                elif ".content.answer" in path:
                    self.selector_texts["content.answer"].append(visible)
            return
        for item_index, child_item in enumerate(items):
            item_path = f"{path}.items[{item_index}]"
            if not isinstance(child_item, Mapping):
                raise HwpxRenderError(f"{item_path} must be an object")
            label = child_item.get("label")
            if not isinstance(label, str) or not label:
                raise HwpxRenderError(f"{item_path}.label must be visible")
            children = child_item.get("blocks")
            if not isinstance(children, list) or not children:
                raise HwpxRenderError(f"{item_path}.blocks must be non-empty")
            first = children[0]
            if isinstance(first, Mapping) and first.get("type") == "paragraph":
                self._paragraph_block(
                    first,
                    f"{item_path}.blocks[0]",
                    prefix=f"{label} ",
                    style="choice",
                    target=target,
                    available_width=available_width,
                )
                children = children[1:]
            else:
                self._paragraph(
                    text_xml(label),
                    label,
                    style="choice",
                    target=target,
                    available_width=available_width,
                )
            self._render_blocks(
                item,
                children,
                f"{item_path}.blocks",
                target=target,
                available_width=available_width,
                depth=depth + 1,
                ancestors=ancestors,
            )

    def _render_blocks(
        self,
        item: _LoadedItem,
        blocks: Any,
        path: str,
        *,
        target: list[ElementTree.Element] | None,
        available_width: int,
        depth: int,
        ancestors: frozenset[int],
        allowed: frozenset[str] | None = None,
    ) -> None:
        if depth > MAX_BLOCK_DEPTH:
            raise HwpxRenderError(f"{path} exceeds max block depth {MAX_BLOCK_DEPTH}")
        if not isinstance(blocks, list):
            raise HwpxRenderError(f"{path} must be a block array")
        permitted = allowed or frozenset(
            {"paragraph", "figure", "bogi", "choices", "box", "table"}
        )
        for index, block in enumerate(blocks):
            here = f"{path}[{index}]"
            if not isinstance(block, Mapping):
                raise HwpxRenderError(f"{here} must be an object")
            identity = id(block)
            if identity in ancestors:
                raise HwpxRenderError(f"{here} contains a cyclic block reference")
            block_type = block.get("type")
            if block_type not in permitted:
                raise HwpxRenderError(f"unsupported block at {here}: {block_type!r}")
            lineage = ancestors | {identity}
            if block_type == "paragraph":
                self._paragraph_block(
                    block,
                    here,
                    target=target,
                    available_width=available_width,
                )
            elif block_type == "figure":
                self._figure(
                    item,
                    block,
                    here,
                    target=target,
                    available_width=available_width,
                )
            elif block_type in {"bogi", "choices"}:
                self._container(
                    item,
                    block,
                    here,
                    str(block_type),
                    target=target,
                    available_width=available_width,
                    depth=depth,
                    ancestors=lineage,
                )
            elif block_type == "box":
                self._box(
                    item,
                    block,
                    here,
                    target=target,
                    available_width=available_width,
                    depth=depth,
                    ancestors=lineage,
                )
            else:
                self._table(
                    item,
                    block,
                    here,
                    target=target,
                    available_width=available_width,
                    depth=depth,
                    ancestors=lineage,
                )

    def _source_crop_item(
        self, item: _LoadedItem, ordinal: int
    ) -> ElementTree.Element:
        asset_id = item.source_question_asset_id
        resources = item.value.get("resources")
        values = resources.get("assets") if isinstance(resources, Mapping) else None
        if not isinstance(asset_id, str) or not isinstance(values, list):
            raise HwpxRenderError(f"item {item.item_id} has no planned question source crop")
        matches = [
            asset
            for asset in values
            if isinstance(asset, Mapping) and asset.get("asset_id") == asset_id
        ]
        if len(matches) != 1:
            raise HwpxRenderError(f"item {item.item_id} source crop identity is ambiguous")
        asset = matches[0]
        if asset.get("role") != "source_crop" or asset.get("section") not in {
            "question",
            "shared",
        }:
            raise HwpxRenderError(f"item {item.item_id} planned source crop is not a question crop")
        binary_id, width, height = self._embed_png_asset(
            item,
            asset,
            f"item[{item.item_id}].resources.assets[{asset_id}]",
            available_width=self.context.writable_width,
            available_height=self.context.writable_height,
        )
        self.canonical["block.figure"] += 1
        self.canonical["fallback.source_crop"] += 1
        return self._paragraph(
            picture_xml(binary_id, self.allocator, width=width, height=height),
            f"[원본 문항 {ordinal}]",
            page_break=ordinal > 1 and self.context.page_break_between_items,
            available_width=self.context.writable_width,
        )

    def question_item(
        self, item: _LoadedItem, ordinal: int
    ) -> ElementTree.Element:
        if item.question_render_mode == "source_crop":
            return self._source_crop_item(item, ordinal)
        if item.question_render_mode != "canonical":
            raise HwpxRenderError(
                f"item {item.item_id} has unknown question render mode: {item.question_render_mode!r}"
            )
        number = str(ordinal)
        marker = self._paragraph(
            text_xml(f"{number}."),
            f"{number}.",
            page_break=ordinal > 1 and self.context.page_break_between_items,
        )
        content = item.value.get("content")
        question = content.get("question") if isinstance(content, Mapping) else None
        blocks = question.get("blocks") if isinstance(question, Mapping) else None
        if not isinstance(blocks, list) or not blocks:
            raise HwpxRenderError(f"item {item.item_id} has no question blocks")
        self._render_blocks(
            item,
            blocks,
            f"item[{item.item_id}].content.question.blocks",
            target=None,
            available_width=self.context.writable_width,
            depth=0,
            ancestors=frozenset(),
        )
        return marker

    @staticmethod
    def _choice_labels(item: _LoadedItem) -> dict[str, str]:
        content = item.value.get("content")
        question = content.get("question") if isinstance(content, Mapping) else None
        blocks = question.get("blocks") if isinstance(question, Mapping) else None
        labels: dict[str, str] = {}
        if not isinstance(blocks, list):
            return labels

        def visit(values: Any, depth: int, ancestors: frozenset[int]) -> None:
            if depth > MAX_BLOCK_DEPTH:
                raise HwpxRenderError(
                    f"item {item.item_id} choices exceed max block depth {MAX_BLOCK_DEPTH}"
                )
            if not isinstance(values, list):
                return
            for block in values:
                if not isinstance(block, Mapping):
                    continue
                identity = id(block)
                if identity in ancestors:
                    raise HwpxRenderError(f"item {item.item_id} choices contain a cycle")
                lineage = ancestors | {identity}
                block_type = block.get("type")
                if block_type in {"bogi", "choices"}:
                    for child in block.get("items", []):
                        if not isinstance(child, Mapping):
                            continue
                        if block_type == "choices":
                            choice_id, label = child.get("choice_id"), child.get("label")
                            if (
                                not isinstance(choice_id, str)
                                or not isinstance(label, str)
                                or not label
                            ):
                                raise HwpxRenderError(
                                    f"item {item.item_id} has an invalid visible choice label"
                                )
                            if choice_id in labels:
                                raise HwpxRenderError(
                                    f"item {item.item_id} has duplicate choice_id: {choice_id}"
                                )
                            labels[choice_id] = label
                        visit(child.get("blocks"), depth + 1, lineage)
                elif block_type == "box":
                    visit(block.get("blocks"), depth + 1, lineage)
                elif block_type == "table":
                    for row in block.get("rows", []):
                        if isinstance(row, Mapping):
                            for cell in row.get("cells", []):
                                if isinstance(cell, Mapping):
                                    visit(cell.get("blocks"), depth + 1, lineage)

        visit(blocks, 0, frozenset())
        return labels

    @classmethod
    def _answer_choice_text(cls, item: _LoadedItem) -> str:
        content = item.value.get("content")
        answer = content.get("answer") if isinstance(content, Mapping) else None
        if not isinstance(answer, Mapping) or answer.get("kind") != "choice":
            raise HwpxRenderError(f"item {item.item_id} answer must be a saved choice answer")
        choice_ids = answer.get("choice_ids")
        if not isinstance(choice_ids, list) or not choice_ids:
            raise HwpxRenderError(f"item {item.item_id} answer has no saved choice IDs")
        labels = cls._choice_labels(item)
        try:
            visible = [labels[choice_id] for choice_id in choice_ids]
        except (KeyError, TypeError) as exc:
            raise HwpxRenderError(
                f"item {item.item_id} answer choice_id has no visible canonical label"
            ) from exc
        return ", ".join(visible)

    @classmethod
    def _answer_text(cls, item: _LoadedItem) -> str:
        return f"정답: {cls._answer_choice_text(item)}"

    def explanation_item(
        self,
        item: _LoadedItem,
        ordinal: int,
        *,
        include_answer: bool,
        include_explanation: bool,
    ) -> None:
        number = str(ordinal)
        self._paragraph(
            text_xml(f"{number}번"),
            f"{number}번",
            page_break=ordinal > 1 and self.context.page_break_between_items,
        )
        content = item.value.get("content")
        if not isinstance(content, Mapping):
            raise HwpxRenderError(f"item {item.item_id} has no content")
        if include_answer and item.answer_render_mode == "saved":
            answer = content.get("answer")
            if not isinstance(answer, Mapping):
                raise HwpxRenderError(f"item {item.item_id} has an invalid saved answer")
            if answer.get("kind") == "choice":
                answer_text = self._answer_text(item)
                self._paragraph(text_xml(answer_text), answer_text, style="choice")
                self.selector_texts["content.answer"].append(answer_text)
                self.canonical["answer.choice"] += 1
            elif answer.get("kind") == "content":
                self._paragraph(text_xml("정답:"), "정답:", style="choice")
                self.canonical["answer.content"] += 1
                self._render_blocks(
                    item,
                    answer.get("blocks"),
                    f"item[{item.item_id}].content.answer.blocks",
                    target=None,
                    available_width=self.context.writable_width,
                    depth=0,
                    ancestors=frozenset(),
                )
            else:
                raise HwpxRenderError(
                    f"item {item.item_id} answer kind is unsupported by renderer v0.6"
                )
        if include_explanation and item.explanation_render_mode == "saved":
            explanation = content.get("explanation")
            blocks = explanation.get("blocks") if isinstance(explanation, Mapping) else None
            if not isinstance(blocks, list) or not blocks:
                raise HwpxRenderError(f"item {item.item_id} has an invalid saved explanation")
            self._render_blocks(
                item,
                blocks,
                f"item[{item.item_id}].content.explanation.blocks",
                target=None,
                available_width=self.context.writable_width,
                depth=0,
                ancestors=frozenset(),
            )

    def endnote_item(
        self,
        item: _LoadedItem,
        ordinal: int,
        marker: ElementTree.Element,
        *,
        include_answer: bool,
        include_explanation: bool,
    ) -> None:
        """Attach one saved answer/explanation endnote to a question marker."""

        if ordinal != len(self._endnote_numbers) + 1:
            raise HwpxRenderError("native endnotes must be attached in 1..N order")
        if marker not in self.paragraphs:
            raise HwpxRenderError("native endnote marker is not an output paragraph")
        marker_run = marker.find(f"{{{HP_NS}}}run")
        if marker_run is None or marker_run.find(f".//{{{HP_NS}}}endNote") is not None:
            raise HwpxRenderError("native endnote marker is missing or already occupied")
        # In combined output the native endnote marker is the visible problem
        # number. Remove the separate ``1.`` text so Hancom does not show both.
        for text_node in list(marker_run.findall(f"{{{HP_NS}}}t")):
            marker_run.remove(text_node)
        content = item.value.get("content")
        if not isinstance(content, Mapping):
            raise HwpxRenderError(f"item {item.item_id} has no content")

        note_paragraphs: list[ElementTree.Element] = []
        answer = content.get("answer")
        answer_blocks: Any = None
        answer_value = ""
        if include_answer and item.answer_render_mode == "saved":
            if not isinstance(answer, Mapping):
                raise HwpxRenderError(f"item {item.item_id} has an invalid saved answer")
            if answer.get("kind") == "choice":
                answer_value = self._answer_choice_text(item)
                self.canonical["answer.choice"] += 1
            elif answer.get("kind") == "content":
                answer_blocks = answer.get("blocks")
                self.canonical["answer.content"] += 1
            else:
                raise HwpxRenderError(
                    f"item {item.item_id} answer kind is unsupported by renderer v0.6"
                )
        else:
            self.canonical["endnote.empty_answer"] += 1

        auto_number = (
            f'<hp:ctrl><hp:autoNum num="{ordinal}" numType="ENDNOTE">'
            '<hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" '
            'suffixChar=")" supscript="0"/></hp:autoNum></hp:ctrl>'
        )
        answer_label = f" [답]{f' {answer_value}' if answer_value else ''}"
        self._paragraph(
            auto_number + text_xml(answer_label),
            answer_label.strip(),
            style="choice",
            target=note_paragraphs,
            available_width=self.context.writable_width,
        )
        if answer_blocks is not None:
            self._render_blocks(
                item,
                answer_blocks,
                f"item[{item.item_id}].content.answer.blocks",
                target=note_paragraphs,
                available_width=self.context.writable_width,
                depth=0,
                ancestors=frozenset(),
            )

        self._paragraph(
            text_xml("[풀이]"),
            "[풀이]",
            style="choice",
            target=note_paragraphs,
            available_width=self.context.writable_width,
        )
        explanation = content.get("explanation")
        if include_explanation and item.explanation_render_mode == "saved":
            blocks = explanation.get("blocks") if isinstance(explanation, Mapping) else None
            if not isinstance(blocks, list) or not blocks:
                raise HwpxRenderError(f"item {item.item_id} has an invalid saved explanation")
            self._render_blocks(
                item,
                blocks,
                f"item[{item.item_id}].content.explanation.blocks",
                target=note_paragraphs,
                available_width=self.context.writable_width,
                depth=0,
                ancestors=frozenset(),
            )
        else:
            self.canonical["endnote.empty_explanation"] += 1

        control = ElementTree.Element(f"{{{HP_NS}}}ctrl")
        note = ElementTree.SubElement(
            control,
            f"{{{HP_NS}}}endNote",
            {
                "number": str(ordinal),
                "suffixChar": "41",
                "instId": str(self.allocator.next_instance_id()),
            },
        )
        sub_list = ElementTree.SubElement(
            note,
            f"{{{HP_NS}}}subList",
            {
                "id": "",
                "textDirection": "HORIZONTAL",
                "lineWrap": "BREAK",
                "vertAlign": "TOP",
                "linkListIDRef": "0",
                "linkListNextIDRef": "0",
                "textWidth": "0",
                "textHeight": "0",
                "hasTextRef": "0",
                "hasNumRef": "0",
            },
        )
        sub_list.extend(note_paragraphs)
        marker_run.insert(0, control)
        self._endnote_numbers.append(ordinal)
        self.canonical["endnotes"] += 1

    def finish(self) -> tuple[bytes, bytes, dict[str, bytes]]:
        if not self.paragraphs:
            raise HwpxRenderError(f"{self.kind} renderer produced no paragraphs")
        if self._endnote_numbers != list(range(1, len(self._endnote_numbers) + 1)):
            raise HwpxRenderError("native endnote numbering is not continuous")
        first_run = self.paragraphs[0].find(f"{{{HP_NS}}}run")
        if first_run is None:
            raise HwpxRenderError("first output paragraph has no run")
        section_properties = copy.deepcopy(self.context.section_properties)
        if self._endnote_numbers:
            if section_properties.find(f"{{{HP_NS}}}endNotePr") is not None:
                raise HwpxRenderError("template already contains native endnote properties")
            endnote_properties = _fragment(
                '<hp:endNotePr><hp:autoNumFormat type="DIGIT" userChar="" '
                'prefixChar="" suffixChar=")" supscript="0"/>'
                '<hp:noteLine length="-1" type="SOLID" width="0.12 mm" '
                'color="#000000"/><hp:noteSpacing betweenNotes="0" '
                'belowLine="567" aboveLine="850"/>'
                '<hp:numbering type="CONTINUOUS" newNum="1"/>'
                '<hp:placement place="END_OF_DOCUMENT" beneathText="0"/>'
                '</hp:endNotePr>'
            )
            children = list(section_properties)
            insert_at = next(
                (
                    index
                    for index, child in enumerate(children)
                    if child.tag == f"{{{HP_NS}}}pageBorderFill"
                ),
                len(children),
            )
            section_properties.insert(insert_at, endnote_properties)
        first_run.insert(0, section_properties)
        first_run.insert(1, copy.deepcopy(self.context.column_control))
        refs = self.context.styles["paragraph"]
        line = line_segment_xml(
            vertical_position=0,
            vertical_size=1000,
            text_height=1000,
            baseline=850,
            spacing=600,
            horizontal_size=self.context.page_writable_width,
        )
        outer_id = self.allocator.next_object_id()
        footer_id = self.allocator.next_object_id()
        inner_id = self.allocator.next_object_id()
        footer = _fragment(
            f'<hp:p id="{outer_id}" paraPrIDRef="{refs.paragraph_property_id}" '
            f'styleIDRef="{refs.style_id}" pageBreak="0" columnBreak="0" merged="0">'
            f'<hp:run charPrIDRef="{refs.character_property_id}"><hp:ctrl>'
            f'<hp:footer id="{footer_id}" applyPageType="BOTH">'
            '<hp:subList id="" textDirection="HORIZONTAL" lineWrap="BREAK" '
            'vertAlign="BOTTOM" linkListIDRef="0" linkListNextIDRef="0" '
            f'textWidth="{self.context.page_writable_width}" textHeight="1000" '
            'hasTextRef="0" hasNumRef="1">'
            f'<hp:p id="{inner_id}" paraPrIDRef="{self.context.centered_paragraph_property_id}" '
            f'styleIDRef="{refs.style_id}" pageBreak="0" columnBreak="0" merged="0">'
            f'<hp:run charPrIDRef="{refs.character_property_id}"><hp:ctrl>'
            '<hp:autoNum num="1" numType="PAGE"><hp:autoNumFormat type="DIGIT" '
            'userChar="" prefixChar="" suffixChar="" supscript="0"/>'
            '</hp:autoNum></hp:ctrl><hp:t/></hp:run>'
            f'{line}</hp:p></hp:subList></hp:footer></hp:ctrl></hp:run>{line}</hp:p>'
        )
        self.paragraphs.insert(1, footer)
        section = copy.deepcopy(self.context.section_root)
        section[:] = self.paragraphs
        section_bytes = ElementTree.tostring(
            section, encoding="utf-8", xml_declaration=True, short_empty_elements=True
        )
        additions = "".join(
            f'<opf:item id="{binary_id}" href="BinData/{binary_id}.bmp" '
            'media-type="image/bmp" isEmbeded="1"/>'
            for binary_id in sorted(self.binaries)
        ).encode("ascii")
        hpf = self.context.members["Contents/content.hpf"]
        marker = b"</opf:manifest>"
        if hpf.count(marker) != 1:
            raise HwpxRenderError("template content.hpf manifest boundary is ambiguous")
        hpf = hpf.replace(marker, additions + marker, 1)
        return section_bytes, hpf, dict(self.binaries)


def _zip_info(name: str, compression: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, _FIXED_ZIP_TIME)
    info.compress_type = compression
    info.create_system = 0
    info.external_attr = 0
    return info


def _package_bytes(
    context: _TemplateContext,
    section: bytes,
    hpf: bytes,
    preview: bytes,
    binaries: Mapping[str, bytes],
) -> bytes:
    members = dict(context.members)
    members["Contents/section0.xml"] = section
    members["Contents/content.hpf"] = hpf
    members["Preview/PrvText.txt"] = preview
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(_zip_info("mimetype", zipfile.ZIP_STORED), MIMETYPE)
        archive.writestr(
            _zip_info("version.xml", zipfile.ZIP_STORED), members["version.xml"]
        )
        archive.writestr(
            _zip_info("Contents/header.xml", zipfile.ZIP_DEFLATED),
            members["Contents/header.xml"],
        )
        for binary_id in sorted(binaries):
            data = binaries[binary_id]
            archive.writestr(
                _zip_info(f"BinData/{binary_id}.bmp", zipfile.ZIP_DEFLATED), data
            )
        for name in _BASE_PACKAGE_ORDER[3:]:
            archive.writestr(_zip_info(name, zipfile.ZIP_DEFLATED), members[name])
    return output.getvalue()


def _inspect_package(
    data: bytes,
    expected: Mapping[str, int],
    writable_width: int,
    *,
    allow_native_endnotes: bool = False,
) -> dict[str, int]:
    errors: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if not infos or infos[0].filename != "mimetype" or infos[0].compress_type != zipfile.ZIP_STORED:
                errors.append("mimetype is not first and stored")
            if len(infos) < 2 or infos[1].filename != "version.xml" or infos[1].compress_type != zipfile.ZIP_STORED:
                errors.append("version.xml is not second and stored")
            if archive.read("mimetype") != MIMETYPE:
                errors.append("invalid mimetype content")
            required = set(REQUIRED_HWPX_MEMBERS)
            if not required.issubset(names):
                errors.append("required package members are missing")
            extras = set(names).difference(required)
            if any(not re.fullmatch(r"BinData/image\d+\.bmp", name) for name in extras):
                errors.append("package contains a non-approved template member")
            lowered = "\n".join(names).lower()
            if ".svg" in lowered or "scripts/" in lowered or "endnote" in lowered:
                errors.append("package contains forbidden SVG/script/endnote members")
            section = ElementTree.fromstring(archive.read("Contents/section0.xml"))
            header = ElementTree.fromstring(archive.read("Contents/header.xml"))
            hpf = ElementTree.fromstring(archive.read("Contents/content.hpf"))
            manifest = hpf.find(f"{{{OPF_NS}}}manifest")
            manifest_items = {
                node.get("id"): node.get("href")
                for node in manifest.findall(f"{{{OPF_NS}}}item")
            } if manifest is not None else {}
            picture_refs = {
                node.get("binaryItemIDRef")
                for node in section.iter(f"{{{HC_NAMESPACE}}}img")
                if node.get("binaryItemIDRef")
            }
            binary_ids = {PurePosixPath(name).stem for name in extras}
            if picture_refs != binary_ids:
                errors.append("picture references do not match BinData members")
            if any(manifest_items.get(binary_id) != f"BinData/{binary_id}.bmp" for binary_id in binary_ids):
                errors.append("BinData members do not match content.hpf manifest")
            section_properties = section.findall(f".//{{{HP_NS}}}secPr")
            if len(section_properties) != 1:
                errors.append("output must preserve exactly one secPr")
            if len(section.findall(f".//{{{HP_NS}}}colPr")) != 1:
                errors.append("output must preserve exactly one colPr")
            top_level = section.findall(f"{{{HP_NS}}}p")
            if (
                not top_level
                or top_level[0].find(f".//{{{HP_NS}}}secPr") is None
                or top_level[0].find(f".//{{{HP_NS}}}colPr") is None
            ):
                errors.append("first output paragraph must own secPr and colPr")
            parents = {child: parent for parent in section.iter() for child in parent}
            footers = section.findall(f".//{{{HP_NS}}}footer")
            auto_numbers = section.findall(f".//{{{HP_NS}}}autoNum")
            page_numbers = [
                node for node in auto_numbers if node.get("numType") == "PAGE"
            ]
            endnote_auto_numbers = [
                node for node in auto_numbers if node.get("numType") == "ENDNOTE"
            ]
            if len(footers) != 1 or len(page_numbers) != 1:
                errors.append("output must contain exactly one page-number footer")
            elif (
                page_numbers[0].get("numType") != "PAGE"
                or (number_format := page_numbers[0].find(f"{{{HP_NS}}}autoNumFormat"))
                is None
                or number_format.get("type") != "DIGIT"
            ):
                errors.append("page-number footer has an invalid autoNum contract")
            if any(
                node.get("numType") not in {"PAGE", "ENDNOTE"}
                for node in auto_numbers
            ):
                errors.append("output contains an unsupported autoNum contract")

            endnote_properties = section.findall(f".//{{{HP_NS}}}endNotePr")
            endnotes = section.findall(f".//{{{HP_NS}}}endNote")
            named_endnote_elements = [
                node
                for node in section.iter()
                if "endnote" in node.tag.rsplit("}", 1)[-1].casefold()
            ]
            try:
                expected_endnotes = int(expected.get("endnotes", expected.get("endnote", 0)))
            except (TypeError, ValueError):
                expected_endnotes = -1
            if not allow_native_endnotes:
                if (
                    named_endnote_elements
                    or endnote_auto_numbers
                    or expected_endnotes != 0
                ):
                    errors.append("native endnotes are forbidden for this output")
            else:
                if expected_endnotes < 1:
                    errors.append("combined output must declare at least one native endnote")
                if any(
                    node.tag.rsplit("}", 1)[-1] not in {"endNotePr", "endNote"}
                    for node in named_endnote_elements
                ):
                    errors.append("combined output contains an unknown endnote element")
                if len(endnote_properties) != 1 or (
                    len(section_properties) == 1
                    and parents.get(endnote_properties[0]) is not section_properties[0]
                ):
                    errors.append("combined output must have one secPr-owned endNotePr")
                elif len(section_properties) == 1:
                    properties = endnote_properties[0]
                    number_format = properties.find(f"{{{HP_NS}}}autoNumFormat")
                    note_line = properties.find(f"{{{HP_NS}}}noteLine")
                    note_spacing = properties.find(f"{{{HP_NS}}}noteSpacing")
                    numbering = properties.find(f"{{{HP_NS}}}numbering")
                    placement = properties.find(f"{{{HP_NS}}}placement")
                    if (
                        number_format is None
                        or number_format.get("type") != "DIGIT"
                        or number_format.get("suffixChar") != ")"
                        or note_line is None
                        or note_line.get("type") != "SOLID"
                        or note_spacing is None
                        or numbering is None
                        or numbering.get("type") != "CONTINUOUS"
                        or numbering.get("newNum") != "1"
                        or placement is None
                        or placement.get("place") != "END_OF_DOCUMENT"
                        or placement.get("beneathText") != "0"
                    ):
                        errors.append("combined endNotePr contract is invalid")
                if len(endnotes) != expected_endnotes:
                    errors.append("native endnote count differs from the declared item count")
                if len(endnote_auto_numbers) != len(endnotes):
                    errors.append("native endnotes must each own one ENDNOTE autoNum")
                sub_list_attrs = {
                    "textDirection": "HORIZONTAL",
                    "lineWrap": "BREAK",
                    "vertAlign": "TOP",
                    "linkListIDRef": "0",
                    "linkListNextIDRef": "0",
                    "hasTextRef": "0",
                    "hasNumRef": "0",
                }
                for index, note in enumerate(endnotes, 1):
                    note_parent = parents.get(note)
                    note_run = parents.get(note_parent) if note_parent is not None else None
                    note_paragraph = parents.get(note_run) if note_run is not None else None
                    if (
                        note_parent is None
                        or note_parent.tag != f"{{{HP_NS}}}ctrl"
                        or note_run is None
                        or note_run.tag != f"{{{HP_NS}}}run"
                        or note_paragraph is None
                        or note_paragraph.tag != f"{{{HP_NS}}}p"
                    ):
                        errors.append(f"endnote[{index}] is not attached to a paragraph run")
                    instance_id = note.get("instId")
                    if (
                        note.get("number") != str(index)
                        or note.get("suffixChar") != "41"
                        or not isinstance(instance_id, str)
                        or not instance_id.isdigit()
                    ):
                        errors.append(f"endnote[{index}] numbering/instance contract is invalid")
                    sub_lists = note.findall(f"{{{HP_NS}}}subList")
                    if len(sub_lists) != 1:
                        errors.append(f"endnote[{index}] must contain exactly one subList")
                        continue
                    sub_list = sub_lists[0]
                    if any(
                        sub_list.get(attribute) != value
                        for attribute, value in sub_list_attrs.items()
                    ):
                        errors.append(f"endnote[{index}] subList contract is invalid")
                    note_auto_numbers = sub_list.findall(f".//{{{HP_NS}}}autoNum")
                    if len(note_auto_numbers) != 1:
                        errors.append(f"endnote[{index}] must contain one autoNum")
                    else:
                        auto_number = note_auto_numbers[0]
                        auto_format = auto_number.find(f"{{{HP_NS}}}autoNumFormat")
                        if (
                            auto_number.get("numType") != "ENDNOTE"
                            or auto_number.get("num") != str(index)
                            or auto_format is None
                            or auto_format.get("type") != "DIGIT"
                            or auto_format.get("suffixChar") != ")"
                        ):
                            errors.append(f"endnote[{index}] autoNum contract is invalid")
                    note_paragraphs = sub_list.findall(f"{{{HP_NS}}}p")
                    texts = [
                        "".join(
                            text.text or ""
                            for text in paragraph.iter(f"{{{HP_NS}}}t")
                        ).strip()
                        for paragraph in note_paragraphs
                    ]
                    solution_labels = [
                        position for position, value in enumerate(texts) if value == "[풀이]"
                    ]
                    if (
                        not texts
                        or not texts[0].startswith("[답]")
                        or len(solution_labels) != 1
                        or solution_labels[0] == 0
                    ):
                        errors.append(f"endnote[{index}] must order [답] before [풀이]")
            para_ids = {
                node.get("id") for node in header.iter(f"{{{HH_NS}}}paraPr")
            }
            char_ids = {
                node.get("id") for node in header.iter(f"{{{HH_NS}}}charPr")
            }
            char_properties = header.find(f".//{{{HH_NS}}}charProperties")
            if (
                char_properties is None
                or char_properties.get("itemCnt")
                != str(len(char_properties.findall(f"{{{HH_NS}}}charPr")))
            ):
                errors.append("header character property count is invalid")
            style_ids = {
                node.get("id") for node in header.iter(f"{{{HH_NS}}}style")
            }
            if any(
                paragraph.get("paraPrIDRef") not in para_ids
                or paragraph.get("styleIDRef") not in style_ids
                for paragraph in section.iter(f"{{{HP_NS}}}p")
            ) or any(
                run.get("charPrIDRef") not in char_ids
                for run in section.iter(f"{{{HP_NS}}}run")
            ):
                errors.append("output contains an unresolved style reference")
            identities = [
                value
                for node in section.iter()
                for attribute in ("id", "instid", "instId")
                if (value := node.get(attribute))
            ]
            z_orders = [
                value
                for node in section.iter()
                if (value := node.get("zOrder")) is not None
            ]
            if len(identities) != len(set(identities)):
                errors.append("output contains duplicate object id/instid values")
            if len(z_orders) != len(set(z_orders)):
                errors.append("output contains duplicate zOrder values")
            for table_index, table in enumerate(section.iter(f"{{{HP_NS}}}tbl")):
                try:
                    column_count = int(table.get("colCnt", "0"))
                    row_count = int(table.get("rowCnt", "0"))
                    table_width = int(
                        table.find(f"{{{HP_NS}}}sz").get("width", "0")
                    )
                except (AttributeError, TypeError, ValueError):
                    errors.append(f"table[{table_index}] has invalid dimensions")
                    continue
                rows = table.findall(f"{{{HP_NS}}}tr")
                if (
                    column_count < 1
                    or row_count != len(rows)
                    or not 1 <= table_width <= writable_width
                ):
                    errors.append(f"table[{table_index}] size/count contract is invalid")
                    continue
                active = [0] * column_count
                for row_index, row in enumerate(rows):
                    covered = [value > 0 for value in active]
                    updates: dict[int, int] = {}
                    cursor = 0
                    for cell in row.findall(f"{{{HP_NS}}}tc"):
                        while cursor < column_count and covered[cursor]:
                            cursor += 1
                        address = cell.find(f"{{{HP_NS}}}cellAddr")
                        span = cell.find(f"{{{HP_NS}}}cellSpan")
                        try:
                            col_addr = int(address.get("colAddr", "-1"))
                            row_addr = int(address.get("rowAddr", "-1"))
                            col_span = int(span.get("colSpan", "0"))
                            row_span = int(span.get("rowSpan", "0"))
                        except (AttributeError, TypeError, ValueError):
                            errors.append(f"table[{table_index}] has invalid cell metadata")
                            break
                        if (
                            (row_addr, col_addr) != (row_index, cursor)
                            or col_span < 1
                            or row_span < 1
                            or row_index + row_span > row_count
                            or cursor + col_span > column_count
                            or any(covered[cursor : cursor + col_span])
                        ):
                            errors.append(f"table[{table_index}] has an invalid cell grid/span")
                            break
                        for column in range(cursor, cursor + col_span):
                            covered[column] = True
                            if row_span > 1:
                                updates[column] = max(
                                    updates.get(column, 0), row_span - 1
                                )
                        cursor += col_span
                    if not all(covered):
                        errors.append(f"table[{table_index}] row[{row_index}] is incomplete")
                    active = [max(0, value - 1) for value in active]
                    for column, value in updates.items():
                        active[column] = max(active[column], value)
                if any(active):
                    errors.append(f"table[{table_index}] rowspan exceeds the final row")
            counts = {
                "paragraphs": len(section.findall(f".//{{{HP_NS}}}p")),
                "equations": len(section.findall(f".//{{{HP_NS}}}equation")),
                "pictures": len(section.findall(f".//{{{HP_NS}}}pic")),
                "tables": len(section.findall(f".//{{{HP_NS}}}tbl")),
                "bin_data_members": len(binary_ids),
                "endnotes": len(endnotes),
            }
            if counts["equations"] != int(expected.get("equation", 0)):
                errors.append("rendered equation count differs from consumed canonical count")
            if counts["pictures"] != int(expected.get("block.figure", 0)):
                errors.append("rendered figure count differs from consumed canonical count")
            expected_tables = int(expected.get("block.table", 0)) + int(
                expected.get("block.box", 0)
            ) + int(expected.get("block.bogi", 0)) + int(
                expected.get("layout.choice_grid", 0)
            )
            if counts["tables"] != expected_tables:
                errors.append("rendered native table count differs from semantic table layouts")
            if archive.testzip() is not None:
                errors.append("package has a bad CRC")
    except (KeyError, OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        errors.append(f"cannot inspect rendered package: {exc}")
        counts = {}
    if errors:
        raise HwpxRenderError("rendered HWPX failed strict checks: " + "; ".join(errors))
    return counts


def _render_one(
    output: OutputPlan,
    items: tuple[_LoadedItem, ...],
    project: Mapping[str, Any],
) -> _RenderedPackage:
    context = _load_template_context(output)
    if int(context.column_control.find(f"{{{HP_NS}}}colPr").get("colCount")) != 2:
        control = copy.deepcopy(context.column_control)
        column = control.find(f"{{{HP_NS}}}colPr")
        if column is None:
            raise HwpxRenderError("template column control is missing")
        gap = round(8 * 7200 / 25.4)
        column.set("colCount", "2")
        column.set("sameGap", str(gap))
        context = replace(
            context,
            column_control=control,
            writable_width=(context.page_writable_width - gap) // 2,
        )
    renderer = _Renderer(output.document_kind, context)
    export = project.get("export")
    if not isinstance(export, Mapping):
        raise HwpxRenderError("project export contract is missing")
    for ordinal, item in enumerate(items, 1):
        if output.document_kind == "question":
            renderer.question_item(item, ordinal)
        else:
            renderer.explanation_item(
                item,
                ordinal,
                include_answer=export.get("include_answers") is True,
                include_explanation=export.get("include_explanations") is True,
            )
    section, hpf, binaries = renderer.finish()
    data = _package_bytes(context, section, hpf, "\n".join(renderer.preview).encode("utf-8"), binaries)
    canonical_counts = dict(sorted(renderer.canonical.items()))
    renderer_counts = _inspect_package(
        data, canonical_counts, context.writable_width
    )
    selector_texts = {
        selector: tuple(sorted(set(texts)))
        for selector, texts in sorted(renderer.selector_texts.items())
        if texts
    }
    return _RenderedPackage(data, canonical_counts, renderer_counts, selector_texts)


def _commit_pair(outputs: tuple[tuple[Path, bytes], tuple[Path, bytes]]) -> None:
    paths = tuple(path.resolve() for path, _ in outputs)
    if paths[0] == paths[1] or paths[0].parent != paths[1].parent:
        raise HwpxRenderError("separate HWPX outputs must be distinct files in one directory")
    parent = paths[0].parent
    parent.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise HwpxRenderError(f"refusing to replace a non-regular output: {path}")
    stage = Path(tempfile.mkdtemp(prefix=".hwpx-build-", dir=parent))
    staged = (stage / "question.hwpx", stage / "explanation.hwpx")
    backups = (stage / "backup-question.hwpx", stage / "backup-explanation.hwpx")
    moved_backups: list[int] = []
    published: list[int] = []
    preserve_recovery = False
    try:
        for target, data, temporary in zip(paths, (outputs[0][1], outputs[1][1]), staged):
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        try:
            for index, (target, backup) in enumerate(zip(paths, backups)):
                if target.exists():
                    os.replace(target, backup)
                    moved_backups.append(index)
            for index, (temporary, target) in enumerate(zip(staged, paths)):
                os.replace(temporary, target)
                published.append(index)
        except OSError as exc:
            rollback_errors: list[str] = []
            for index in reversed(published):
                try:
                    if paths[index].exists():
                        paths[index].unlink()
                except OSError as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            for index in reversed(moved_backups):
                try:
                    if backups[index].exists():
                        os.replace(backups[index], paths[index])
                except OSError as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            preserve_recovery = bool(rollback_errors)
            detail = (
                f"; rollback errors: {rollback_errors}; recovery artifacts: {stage}"
                if rollback_errors
                else ""
            )
            raise HwpxRenderError(f"atomic HWPX publication failed: {exc}{detail}") from exc
    finally:
        if not preserve_recovery:
            for path in (*staged, *backups):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                stage.rmdir()
            except OSError:
                pass


def _validate_pair(
    plan: ExportPlan,
    receipt: dict[str, Any],
    rendered: Mapping[str, _RenderedPackage],
    forbidden_question_texts: Mapping[str, tuple[str, ...]],
) -> None:
    with tempfile.TemporaryDirectory(prefix="qjs-hwpx-render-") as temporary:
        root = Path(temporary)
        for output in plan.outputs:
            path = root / output.output_path.name
            path.write_bytes(rendered[output.document_kind].data)
            report = validate_hwpx_output(
                path,
                output.document_kind,
                forbidden_selectors=(
                    forbidden_question_texts
                    if output.document_kind == "question"
                    else None
                ),
                build_receipt=receipt,
                run_external_tools=False,
            )
            if not report["ok"]:
                codes = ", ".join(issue["code"] for issue in report["errors"])
                raise HwpxRenderError(
                    f"strict {output.document_kind} HWPX validation failed: {codes}"
                )
            entry = next(
                value
                for value in receipt["outputs"]
                if value["document_kind"] == output.document_kind
            )
            entry["validation"]["strict_report_sha256"] = report["report_sha256"]


def _fresh_plan(plan: ExportPlan) -> ExportPlan:
    if not isinstance(plan, ExportPlan):
        raise TypeError("render_export_plan requires an ExportPlan from preflight_export")
    if plan.question.output_path.parent != plan.explanation.output_path.parent:
        raise HwpxRenderError("preflight plan outputs must share one directory")
    for item_id in plan.question.item_ids:
        if (
            not isinstance(item_id, str)
            or not item_id
            or "\\" in item_id
            or ":" in item_id
            or PurePosixPath(item_id).name != item_id
            or item_id in {".", ".."}
        ):
            raise HwpxRenderError(f"unsafe canonical item ID in export plan: {item_id!r}")
    try:
        fresh = preflight_export(plan.project_path, plan.question.output_path.parent)
    except ExportPreflightError as exc:
        raise HwpxRenderError(f"export plan is stale: {exc}") from exc
    if plan.as_dict() != fresh.as_dict():
        raise HwpxRenderError("export plan is stale or was not produced by the current preflight")
    return fresh


def render_export_plan(plan: ExportPlan) -> dict[str, Any]:
    """Render and atomically publish the problem-only and explanation-only HWPX pair."""

    plan = _fresh_plan(plan)
    receipt = copy.deepcopy(dict(plan.receipt))
    receipt["column_count"] = 2
    project_expected_sha = str(receipt["project"]["sha256"])
    project = _json_bytes(
        _read_exact(plan.project_path, project_expected_sha, "project"), "project"
    )
    receipt_items = {
        entry["item_id"]: entry
        for entry in receipt["inputs"]["items"]
        if isinstance(entry, Mapping) and isinstance(entry.get("item_id"), str)
    }
    loaded: list[_LoadedItem] = []
    for item_id in plan.question.item_ids:
        entry = receipt_items.get(item_id)
        if not isinstance(entry, Mapping):
            raise HwpxRenderError(f"build receipt has no canonical item: {item_id}")
        items_root = (plan.project_path.parent / "items").resolve()
        item_path = (items_root / item_id / "question.json").resolve()
        if not item_path.is_relative_to(items_root):
            raise HwpxRenderError(f"canonical item path escapes items directory: {item_id}")
        value = _json_bytes(
            _read_exact(item_path, str(entry.get("sha256")), f"item {item_id}"),
            f"item {item_id}",
        )
        normalize_equations(value.get("content"))
        mode = entry.get("question_render_mode")
        source_asset_id = entry.get("source_question_asset_id")
        answer_mode = entry.get("answer_render_mode")
        explanation_mode = entry.get("explanation_render_mode")
        if mode not in {"canonical", "source_crop"} or (
            mode == "source_crop" and not isinstance(source_asset_id, str)
        ) or answer_mode not in {"saved", "omitted"} or explanation_mode not in {
            "saved",
            "omitted",
        }:
            raise HwpxRenderError(f"build receipt has an invalid question render mode: {item_id}")
        loaded.append(
            _LoadedItem(
                item_id,
                item_path,
                value,
                str(mode),
                source_asset_id if isinstance(source_asset_id, str) else None,
                str(answer_mode),
                str(explanation_mode),
            )
        )
    items = tuple(loaded)
    question = _render_one(plan.question, items, project)
    explanation = _render_one(plan.explanation, items, project)

    rendered = {"question": question, "explanation": explanation}
    for entry in receipt["outputs"]:
        kind = entry["document_kind"]
        package = rendered[kind]
        entry["sha256"] = _sha256_bytes(package.data)
        entry["size_bytes"] = len(package.data)
        entry["actual_counts"] = dict(package.canonical_counts)
        entry["renderer_counts"] = dict(package.renderer_counts)
        entry["validation"]["package"] = "passed"
    forbidden_question_texts = {
        selector: tuple(
            text
            for text in texts
            if selector == "content.answer"
            or len(
                re.findall(
                    r"[0-9A-Za-z가-힣]",
                    unicodedata.normalize("NFC", text),
                )
            )
            >= _LEAKAGE_MIN_SEMANTIC_CHARS
        )
        for selector, texts in explanation.selector_texts.items()
    }
    forbidden_question_texts = {
        selector: texts for selector, texts in forbidden_question_texts.items() if texts
    }
    question_entry = next(
        entry for entry in receipt["outputs"] if entry["document_kind"] == "question"
    )
    question_entry["forbidden_exact_text_sha256"] = {
        selector: [_sha256_bytes(text.encode("utf-8")) for text in texts]
        for selector, texts in sorted(forbidden_question_texts.items())
    }
    receipt["counts"] = {
        "input": {
            output.document_kind: dict(output.expected_counts) for output in plan.outputs
        },
        "output": {
            kind: {
                "canonical": dict(package.canonical_counts),
                "renderer": dict(package.renderer_counts),
            }
            for kind, package in rendered.items()
        },
    }
    receipt["status"] = "built"
    _validate_pair(plan, receipt, rendered, forbidden_question_texts)
    _commit_pair(
        (
            (plan.question.output_path, question.data),
            (plan.explanation.output_path, explanation.data),
        )
    )
    return receipt


build_hwpx_exports = render_export_plan
