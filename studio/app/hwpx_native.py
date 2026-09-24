"""Small, build-local HWPX XML primitives.

The returned fragments use the standard ``hp`` and ``hc`` prefixes.  Callers
insert them into a template section where those prefixes are already bound.
Document styles and writable geometry stay caller-owned.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


HP_NAMESPACE = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HC_NAMESPACE = "http://www.hancom.co.kr/hwpml/2011/core"
XML_NAMESPACES = {"hp": HP_NAMESPACE, "hc": HC_NAMESPACE}

_IDENTITY_ATTRIBUTE = re.compile(
    rb"(?:^|\s)(?:id|instid)\s*=\s*(['\"])(\d+)\1", re.IGNORECASE
)
_Z_ORDER_ATTRIBUTE = re.compile(rb"(?:^|\s)zOrder\s*=\s*(['\"])(\d+)\1")


def escape_xml(value: object | None) -> str:
    """Escape text or an attribute value while preserving Korean characters."""

    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def text_xml(text: object | None) -> str:
    """Return one whitespace-preserving native text node."""

    return f'<hp:t xml:space="preserve">{escape_xml(text)}</hp:t>'


def _xml_bytes(value: str | bytes) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")


@dataclass
class BuildIdAllocator:
    """Allocate object identities and z-orders after a template's maxima."""

    _next_identity: int
    _next_z_order: int

    @classmethod
    def from_xml(
        cls,
        documents: str | bytes | Iterable[str | bytes] | Mapping[object, str | bytes],
    ) -> "BuildIdAllocator":
        if isinstance(documents, Mapping):
            values: Iterable[str | bytes] = documents.values()
        elif isinstance(documents, (str, bytes)):
            values = (documents,)
        else:
            values = documents

        max_identity = 0
        max_z_order = -1
        for document in values:
            raw = _xml_bytes(document)
            max_identity = max(
                [max_identity]
                + [int(match.group(2)) for match in _IDENTITY_ATTRIBUTE.finditer(raw)]
            )
            max_z_order = max(
                [max_z_order]
                + [int(match.group(2)) for match in _Z_ORDER_ATTRIBUTE.finditer(raw)]
            )
        return cls(max_identity + 1, max_z_order + 1)

    def next_object_id(self) -> int:
        value = self._next_identity
        self._next_identity += 1
        return value

    def next_instance_id(self) -> int:
        return self.next_object_id()

    def next_z_order(self) -> int:
        value = self._next_z_order
        self._next_z_order += 1
        return value


def _integer(name: str, value: int, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def line_segment_xml(
    *,
    vertical_position: int,
    vertical_size: int,
    text_height: int,
    baseline: int,
    spacing: int,
    horizontal_size: int,
) -> str:
    """Emit one deterministic line-segment cache array."""

    values = {
        "vertical_position": _integer("vertical_position", vertical_position),
        "vertical_size": _integer("vertical_size", vertical_size, minimum=1),
        "text_height": _integer("text_height", text_height, minimum=1),
        "baseline": _integer("baseline", baseline),
        "spacing": _integer("spacing", spacing),
        "horizontal_size": _integer("horizontal_size", horizontal_size, minimum=1),
    }
    return (
        '<hp:linesegarray><hp:lineseg textpos="0" '
        f'vertpos="{values["vertical_position"]}" '
        f'vertsize="{values["vertical_size"]}" '
        f'textheight="{values["text_height"]}" '
        f'baseline="{values["baseline"]}" spacing="{values["spacing"]}" '
        f'horzpos="0" horzsize="{values["horizontal_size"]}" '
        'flags="393216"/></hp:linesegarray>'
    )


def paragraph_xml(
    content_xml: str,
    *,
    paragraph_id: int | str,
    paragraph_property_id: int | str,
    character_property_id: int | str,
    style_id: int | str,
    line_segment: str,
    page_break: bool = False,
    column_break: bool = False,
) -> str:
    """Wrap trusted inline XML in a native paragraph."""

    if not isinstance(content_xml, str) or not isinstance(line_segment, str):
        raise TypeError("content_xml and line_segment must be strings")
    run = (
        f'<hp:run charPrIDRef="{escape_xml(character_property_id)}">'
        f"{content_xml}</hp:run>"
        if content_xml
        else f'<hp:run charPrIDRef="{escape_xml(character_property_id)}"/>'
    )
    return paragraph_runs_xml(
        run,
        paragraph_id=paragraph_id,
        paragraph_property_id=paragraph_property_id,
        style_id=style_id,
        line_segment=line_segment,
        page_break=page_break,
        column_break=column_break,
    )


def paragraph_runs_xml(
    runs_xml: str,
    *,
    paragraph_id: int | str,
    paragraph_property_id: int | str,
    style_id: int | str,
    line_segment: str,
    page_break: bool = False,
    column_break: bool = False,
) -> str:
    """Wrap trusted native run XML in a paragraph without nesting runs."""

    if not isinstance(runs_xml, str) or not isinstance(line_segment, str):
        raise TypeError("runs_xml and line_segment must be strings")
    return (
        f'<hp:p id="{escape_xml(paragraph_id)}" '
        f'paraPrIDRef="{escape_xml(paragraph_property_id)}" '
        f'styleIDRef="{escape_xml(style_id)}" '
        f'pageBreak="{int(page_break)}" columnBreak="{int(column_break)}" merged="0">'
        f"{runs_xml}{line_segment}</hp:p>"
    )


def empty_paragraph_xml(
    *,
    paragraph_id: int | str,
    paragraph_property_id: int | str,
    character_property_id: int | str,
    style_id: int | str,
    line_segment: str,
) -> str:
    return paragraph_xml(
        "",
        paragraph_id=paragraph_id,
        paragraph_property_id=paragraph_property_id,
        character_property_id=character_property_id,
        style_id=style_id,
        line_segment=line_segment,
    )


def page_break_xml(
    *,
    paragraph_id: int | str,
    paragraph_property_id: int | str,
    character_property_id: int | str,
    style_id: int | str,
    line_segment: str,
) -> str:
    return paragraph_xml(
        "",
        paragraph_id=paragraph_id,
        paragraph_property_id=paragraph_property_id,
        character_property_id=character_property_id,
        style_id=style_id,
        line_segment=line_segment,
        page_break=True,
    )


def equation_xml(
    hwp_script: str,
    allocator: BuildIdAllocator,
    *,
    width: int,
    height: int,
    baseline: int,
    base_unit: int,
    font: str,
    text_color: str,
    horizontal_margin: int,
    treat_as_character: bool = True,
) -> str:
    """Emit an inline native equation from canonical ``hwp_script``."""

    if not isinstance(hwp_script, str) or not hwp_script.strip():
        raise ValueError("hwp_script must be non-empty")
    object_id = allocator.next_object_id()
    z_order = allocator.next_z_order()
    width = _integer("width", width, minimum=1)
    height = _integer("height", height, minimum=1)
    baseline = _integer("baseline", baseline)
    base_unit = _integer("base_unit", base_unit, minimum=1)
    horizontal_margin = _integer("horizontal_margin", horizontal_margin)
    return (
        f'<hp:equation id="{object_id}" zOrder="{z_order}" numberingType="EQUATION" '
        'textWrap="TOP_AND_BOTTOM" textFlow="BOTH_SIDES" lock="0" '
        'dropcapstyle="None" version="Equation Version 60" '
        f'baseLine="{baseline}" textColor="{escape_xml(text_color)}" '
        f'baseUnit="{base_unit}" lineMode="CHAR" font="{escape_xml(font)}">'
        f'<hp:sz width="{width}" widthRelTo="ABSOLUTE" height="{height}" '
        'heightRelTo="ABSOLUTE" protect="0"/>'
        f'<hp:pos treatAsChar="{int(treat_as_character)}" affectLSpacing="0" '
        'flowWithText="1" allowOverlap="0" holdAnchorAndSO="0" '
        'vertRelTo="PARA" horzRelTo="PARA" vertAlign="TOP" horzAlign="LEFT" '
        'vertOffset="0" horzOffset="0"/>'
        f'<hp:outMargin left="{horizontal_margin}" right="{horizontal_margin}" '
        'top="0" bottom="0"/>'
        '<hp:shapeComment>수식입니다.</hp:shapeComment>'
        f'<hp:script>{escape_xml(hwp_script)}</hp:script></hp:equation>'
    )


def picture_xml(
    binary_item_id: str,
    allocator: BuildIdAllocator,
    *,
    width: int,
    height: int,
    original_width: int | None = None,
    original_height: int | None = None,
) -> str:
    """Emit an inline raster picture referencing one manifest binary item."""

    if not isinstance(binary_item_id, str) or not binary_item_id.strip():
        raise ValueError("binary_item_id must be non-empty")
    if binary_item_id.lower().endswith(".svg"):
        raise ValueError("SVG binary items are not supported")
    width = _integer("width", width, minimum=1)
    height = _integer("height", height, minimum=1)
    original_width = _integer(
        "original_width", original_width if original_width is not None else width, minimum=1
    )
    original_height = _integer(
        "original_height", original_height if original_height is not None else height, minimum=1
    )
    object_id = allocator.next_object_id()
    instance_id = allocator.next_instance_id()
    z_order = allocator.next_z_order()
    ref = escape_xml(binary_item_id)
    return (
        f'<hp:pic id="{object_id}" zOrder="{z_order}" numberingType="PICTURE" '
        'textWrap="TOP_AND_BOTTOM" textFlow="BOTH_SIDES" lock="0" '
        f'dropcapstyle="None" href="" groupLevel="0" instid="{instance_id}" reverse="0">'
        '<hp:offset x="0" y="0"/>'
        f'<hp:orgSz width="{original_width}" height="{original_height}"/>'
        f'<hp:curSz width="{width}" height="{height}"/>'
        '<hp:flip horizontal="0" vertical="0"/>'
        f'<hp:rotationInfo angle="0" centerX="{width // 2}" centerY="{height // 2}" '
        'rotateimage="1"/>'
        '<hp:renderingInfo>'
        '<hc:transMatrix e1="1" e2="0" e3="0" e4="0" e5="1" e6="0"/>'
        '<hc:scaMatrix e1="1" e2="0" e3="0" e4="0" e5="1" e6="0"/>'
        '<hc:rotMatrix e1="1" e2="0" e3="0" e4="0" e5="1" e6="0"/>'
        '</hp:renderingInfo>'
        f'<hp:imgRect><hc:pt0 x="0" y="0"/><hc:pt1 x="{width}" y="0"/>'
        f'<hc:pt2 x="{width}" y="{height}"/><hc:pt3 x="0" y="{height}"/></hp:imgRect>'
        f'<hp:imgClip left="0" right="{width}" top="0" bottom="{height}"/>'
        '<hp:inMargin left="0" right="0" top="0" bottom="0"/>'
        f'<hp:imgDim dimwidth="{width}" dimheight="{height}"/>'
        f'<hc:img binaryItemIDRef="{ref}" bright="0" contrast="0" '
        'effect="REAL_PIC" alpha="0"/><hp:effects/>'
        f'<hp:sz width="{width}" widthRelTo="ABSOLUTE" height="{height}" '
        'heightRelTo="ABSOLUTE" protect="0"/>'
        '<hp:pos treatAsChar="1" affectLSpacing="0" flowWithText="1" '
        'allowOverlap="0" holdAnchorAndSO="0" vertRelTo="PARA" horzRelTo="PARA" '
        'vertAlign="TOP" horzAlign="LEFT" vertOffset="0" horzOffset="0"/>'
        '<hp:outMargin left="0" right="0" top="0" bottom="0"/>'
        '<hp:shapeComment>그림입니다.</hp:shapeComment></hp:pic>'
    )


def png_to_rgb_bmp(source: Path | str | bytes) -> bytes:
    """Convert one PNG path or byte string to an uncompressed RGB BMP."""

    from PIL import Image

    stream: Path | io.BytesIO
    stream = io.BytesIO(source) if isinstance(source, bytes) else Path(source)
    with Image.open(stream) as image:
        if image.format != "PNG":
            raise ValueError("source must contain PNG image data")
        converted = image.convert("RGB")
        output = io.BytesIO()
        converted.save(output, format="BMP")
        return output.getvalue()
