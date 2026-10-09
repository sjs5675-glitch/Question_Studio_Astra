"""Build the approved neutral A4 HWPX serialization template.

The upstream ``exam-studio-multi`` tree is a mechanics source, not a visual
template.  This builder copies only its allowed package resources, replaces
the workbook style names with stable neutral renderer hooks, and writes a new
empty one-section package with deterministic ZIP metadata.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Mapping
from xml.etree import ElementTree

from .hwpx_export import MIMETYPE, validate_clean_hwpx_template


TEMPLATE_ID = "neutral-a4-v1"
DEFAULT_SOURCE_ROOT = Path(
    r"C:\Users\ExampleUser\OneDrive\학원 자료 모음\exam-studio-multi"
)
DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parents[1] / "templates" / f"{TEMPLATE_ID}.hwpx"

PAGE_WIDTH = 59_528
PAGE_HEIGHT = 84_189
MARGIN_LEFT = 5_669
MARGIN_RIGHT = 5_669
MARGIN_TOP = 5_102
MARGIN_BOTTOM = 5_102
WRITABLE_WIDTH = PAGE_WIDTH - MARGIN_LEFT - MARGIN_RIGHT

HANGUL_HEAD_NS = "http://www.hancom.co.kr/hwpml/2011/head"
HANGUL_PARAGRAPH_NS = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HANGUL_SECTION_NS = "http://www.hancom.co.kr/hwpml/2011/section"
HPF_NS = "http://www.idpf.org/2007/opf/"

STYLE_NAMES = (
    "neutral-paragraph",
    "neutral-equation",
    "neutral-choice",
    "neutral-figure-caption",
)

PACKAGE_ORDER = (
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

_ENGINE_FILES = (
    "assemble.py",
    "equation.py",
    "shapes.py",
    "ids.py",
    "resources/hwpx_base",
)
_SOURCE_RESOURCES = {
    "mimetype": "mimetype",
    "version.xml": "version.xml",
    "Contents/header.xml": "Contents/header.xml",
    "settings.xml": "settings.xml",
    "META-INF/container.rdf": "META-INF/container.rdf",
    "Contents/content.hpf": "content_hpf_template.xml",
    "META-INF/container.xml": "META-INF/container.xml",
    "META-INF/manifest.xml": "META-INF/manifest.xml",
}

# A generated, neutral 1 x 1 white preview.  The workbook preview is not reused.
_BLANK_PREVIEW_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR42mP4//8/AAX+Av4zEpUUAAAAAElFTkSuQmCC"
)

_SECTION_XML = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<hs:sec xmlns:ha="http://www.hancom.co.kr/hwpml/2011/app" xmlns:hp="{HANGUL_PARAGRAPH_NS}" xmlns:hp10="http://www.hancom.co.kr/hwpml/2016/paragraph" xmlns:hs="{HANGUL_SECTION_NS}" xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core" xmlns:hh="{HANGUL_HEAD_NS}" xmlns:hhs="http://www.hancom.co.kr/hwpml/2011/history" xmlns:hm="http://www.hancom.co.kr/hwpml/2011/master-page" xmlns:hpf="http://www.hancom.co.kr/schema/2011/hpf" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="{HPF_NS}" xmlns:ooxmlchart="http://www.hancom.co.kr/hwpml/2016/ooxmlchart" xmlns:hwpunitchar="http://www.hancom.co.kr/hwpml/2016/HwpUnitChar" xmlns:epub="http://www.idpf.org/2007/ops" xmlns:config="urn:oasis:names:tc:opendocument:xmlns:config:1.0"><hp:p id="0" paraPrIDRef="0" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0"><hp:run charPrIDRef="1"><hp:secPr id="" textDirection="HORIZONTAL" spaceColumns="0" tabStop="8000" tabStopVal="4000" tabStopUnit="HWPUNIT" outlineShapeIDRef="1" memoShapeIDRef="1" textVerticalWidthHead="0" masterPageCnt="0"><hp:grid lineGrid="0" charGrid="0" wonggojiFormat="0"/><hp:startNum pageStartsOn="BOTH" page="0" pic="0" tbl="0" equation="0"/><hp:visibility hideFirstHeader="0" hideFirstFooter="0" hideFirstMasterPage="0" border="SHOW_ALL" fill="SHOW_ALL" hideFirstPageNum="0" hideFirstEmptyLine="0" showLineNumber="0"/><hp:lineNumberShape restartType="0" countBy="0" distance="0" startNumber="0"/><hp:pagePr landscape="WIDELY" width="{PAGE_WIDTH}" height="{PAGE_HEIGHT}" gutterType="LEFT_ONLY"><hp:margin header="0" footer="0" gutter="0" left="{MARGIN_LEFT}" right="{MARGIN_RIGHT}" top="{MARGIN_TOP}" bottom="{MARGIN_BOTTOM}"/></hp:pagePr><hp:pageBorderFill type="BOTH" borderFillIDRef="1" textBorder="PAPER" headerInside="0" footerInside="0" fillArea="PAPER"><hp:offset left="0" right="0" top="0" bottom="0"/></hp:pageBorderFill></hp:secPr><hp:ctrl><hp:colPr id="" type="NEWSPAPER" layout="LEFT" colCount="1" sameSz="1" sameGap="0"/></hp:ctrl></hp:run><hp:linesegarray><hp:lineseg textpos="0" vertpos="0" vertsize="1000" textheight="1000" baseline="850" spacing="600" horzpos="0" horzsize="{WRITABLE_WIDTH}" flags="393216"/></hp:linesegarray></hp:p></hs:sec>'''.encode(
    "utf-8"
)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_base(source_root: Path) -> Path:
    missing = [name for name in _ENGINE_FILES if not (source_root / name).exists()]
    if missing:
        raise FileNotFoundError(
            f"incomplete exam-studio-multi root {source_root}: missing {missing}"
        )
    return source_root / "resources" / "hwpx_base"


def _neutral_header(source: bytes) -> bytes:
    text = source.decode("utf-8")
    normal = re.search(r'<hh:style id="0"[^>]*/>', text)
    if normal is None:
        raise ValueError("source header.xml has no Normal style")
    styles = [normal.group(0)]
    for style_id, name in enumerate(STYLE_NAMES, 1):
        styles.append(
            f'<hh:style id="{style_id}" type="PARA" name="{name}" '
            f'engName="{name}" paraPrIDRef="0" charPrIDRef="1" '
            'nextStyleIDRef="0" langID="1042" lockForm="0"/>'
        )
    replacement = f'<hh:styles itemCnt="{len(styles)}">{"".join(styles)}</hh:styles>'
    text, count = re.subn(
        r"<hh:styles\b[^>]*>.*?</hh:styles>", replacement, text, count=1
    )
    if count != 1:
        raise ValueError("source header.xml has no unique styles container")
    return text.encode("utf-8")


def _neutral_settings(source: bytes) -> bytes:
    text = source.decode("utf-8")
    text, count = re.subn(
        r'<ha:CaretPosition\b[^>]*/>',
        '<ha:CaretPosition listIDRef="0" paraIDRef="0" pos="0"/>',
        text,
        count=1,
    )
    if count != 1:
        raise ValueError("source settings.xml has no unique CaretPosition")
    return text.encode("utf-8")


def _neutral_hpf(source: bytes) -> bytes:
    text = source.decode("utf-8")
    text = text.replace("{{MODIFIED_DATE}}", "1970-01-01T00:00:00Z")
    text = text.replace("{{EXTRA_IMAGES}}", "")
    text = re.sub(
        r'<opf:item\b[^>]*href="(?:BinData/[^\"]+|Contents/masterpage0\.xml)"[^>]*/>',
        "",
        text,
    )
    text = text.replace('content="text"', 'content=""')
    text = re.sub(
        r'(<opf:meta name="CreatedDate"[^>]*>)[^<]*(</opf:meta>)',
        r"\g<1>1970-01-01T00:00:00Z\g<2>",
        text,
    )
    if "{{" in text or "BinData/" in text or "masterpage" in text.lower():
        raise ValueError("source content HPF could not be cleaned safely")
    return text.encode("utf-8")


def _members(source_root: Path) -> tuple[dict[str, bytes], dict[str, str]]:
    base = _source_base(source_root)
    source_bytes = {
        member: (base / relative).read_bytes()
        for member, relative in _SOURCE_RESOURCES.items()
    }
    if source_bytes["mimetype"] != MIMETYPE:
        raise ValueError("source mimetype is not application/hwp+zip")
    members = dict(source_bytes)
    members["Contents/header.xml"] = _neutral_header(source_bytes["Contents/header.xml"])
    members["Contents/content.hpf"] = _neutral_hpf(source_bytes["Contents/content.hpf"])
    members["settings.xml"] = _neutral_settings(source_bytes["settings.xml"])
    members["Contents/section0.xml"] = _SECTION_XML
    members["Preview/PrvText.txt"] = b""
    members["Preview/PrvImage.png"] = _BLANK_PREVIEW_PNG
    return members, {
        _SOURCE_RESOURCES[name]: _sha256_bytes(data)
        for name, data in sorted(source_bytes.items())
    }


def _zip_info(name: str, compression: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = compression
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def validate_neutral_a4_template(path: Path | str) -> tuple[str, ...]:
    """Return clean-template and neutral-design contract violations."""

    path = Path(path)
    _, clean_errors = validate_clean_hwpx_template(path)
    errors = list(clean_errors)
    if errors or not zipfile.is_zipfile(path):
        return tuple(dict.fromkeys(errors))

    with zipfile.ZipFile(path) as archive:
        names = tuple(info.filename for info in archive.infolist())
        if names != PACKAGE_ORDER:
            errors.append(f"neutral template ZIP order must be {PACKAGE_ORDER!r}")
        if archive.read("Preview/PrvText.txt"):
            errors.append("neutral template preview text must be empty")
        if archive.read("Preview/PrvImage.png") != _BLANK_PREVIEW_PNG:
            errors.append("neutral template must use the generated blank preview")

        header = ElementTree.fromstring(archive.read("Contents/header.xml"))
        section = ElementTree.fromstring(archive.read("Contents/section0.xml"))
        hpf = ElementTree.fromstring(archive.read("Contents/content.hpf"))
        if header.tag != f"{{{HANGUL_HEAD_NS}}}head":
            errors.append("header.xml must use the real Hancom head namespace")
        if section.tag != f"{{{HANGUL_SECTION_NS}}}sec":
            errors.append("section0.xml must use the real Hancom section namespace")

        para_ids = {
            element.attrib.get("id")
            for element in header.iter()
            if _local_name(element.tag) == "paraPr"
        }
        char_ids = {
            element.attrib.get("id")
            for element in header.iter()
            if _local_name(element.tag) == "charPr"
        }
        styles = {
            element.attrib.get("name"): element
            for element in header.iter()
            if _local_name(element.tag) == "style"
        }
        for name in STYLE_NAMES:
            style = styles.get(name)
            if style is None:
                errors.append(f"neutral named style is missing: {name}")
            elif style.attrib.get("paraPrIDRef") not in para_ids:
                errors.append(f"neutral style has an invalid paraPrIDRef: {name}")
            elif style.attrib.get("charPrIDRef") not in char_ids:
                errors.append(f"neutral style has an invalid charPrIDRef: {name}")

        paragraphs = [
            element for element in section if element.tag == f"{{{HANGUL_PARAGRAPH_NS}}}p"
        ]
        if len(paragraphs) != 1:
            errors.append("neutral section must contain exactly one direct paragraph")
        else:
            paragraph = paragraphs[0]
            style_ids = {
                element.attrib.get("id")
                for element in header.iter()
                if _local_name(element.tag) == "style"
            }
            if paragraph.attrib.get("paraPrIDRef") not in para_ids:
                errors.append("empty paragraph has an invalid paraPrIDRef")
            if paragraph.attrib.get("styleIDRef") not in style_ids:
                errors.append("empty paragraph has an invalid styleIDRef")
            runs = [
                element
                for element in paragraph
                if element.tag == f"{{{HANGUL_PARAGRAPH_NS}}}run"
            ]
            if not runs or any(run.attrib.get("charPrIDRef") not in char_ids for run in runs):
                errors.append("empty paragraph needs runs with valid charPrIDRef values")

        local_names = [_local_name(element.tag) for element in section.iter()]
        forbidden_elements = {
            "tbl",
            "pic",
            "img",
            "equation",
            "endNote",
            "endNotePr",
            "header",
            "footer",
            "pageNum",
        }
        found_forbidden = sorted(forbidden_elements.intersection(local_names))
        if found_forbidden:
            errors.append(f"neutral section contains forbidden content: {found_forbidden}")
        if any(
            element.text and element.text.strip()
            for element in section.iter()
            if _local_name(element.tag) == "t"
        ):
            errors.append("neutral section contains visible text")
        required_structure = {"secPr", "linesegarray", "lineseg", "colPr"}
        missing_structure = sorted(required_structure.difference(local_names))
        if missing_structure:
            errors.append(f"neutral paragraph structure is incomplete: {missing_structure}")

        page = next(
            (element for element in section.iter() if _local_name(element.tag) == "pagePr"),
            None,
        )
        margin = next(
            (element for element in section.iter() if _local_name(element.tag) == "margin"),
            None,
        )
        column = next(
            (element for element in section.iter() if _local_name(element.tag) == "colPr"),
            None,
        )
        expected_page = {
            "landscape": "WIDELY",
            "width": str(PAGE_WIDTH),
            "height": str(PAGE_HEIGHT),
        }
        expected_margin = {
            "left": str(MARGIN_LEFT),
            "right": str(MARGIN_RIGHT),
            "top": str(MARGIN_TOP),
            "bottom": str(MARGIN_BOTTOM),
            "gutter": "0",
        }
        if page is None or any(page.attrib.get(key) != value for key, value in expected_page.items()):
            errors.append("neutral template page must be A4 portrait")
        if margin is None or any(
            margin.attrib.get(key) != value for key, value in expected_margin.items()
        ):
            errors.append("neutral template margins must be 20/20/18/18 mm")
        if column is None or column.attrib.get("colCount") != "1":
            errors.append("neutral template must use one column")

        manifest_hrefs = {
            element.attrib.get("href")
            for element in hpf.iter()
            if _local_name(element.tag) == "item"
        }
        if manifest_hrefs != {
            "Contents/header.xml",
            "Contents/section0.xml",
            "settings.xml",
        }:
            errors.append("content.hpf must reference only header, section0, and settings")
        lowered_members = "\n".join(names).lower()
        if any(token in lowered_members for token in ("bindata/", ".svg", "scripts/", "macro")):
            errors.append("neutral template contains a forbidden package member")
    return tuple(dict.fromkeys(errors))


def _manifest(template_path: Path, source_hashes: Mapping[str, str]) -> dict[str, object]:
    data = template_path.read_bytes()
    return {
        "schema_version": "0.1.0",
        "template_id": TEMPLATE_ID,
        "file": template_path.name,
        "sha256": _sha256_bytes(data),
        "size_bytes": len(data),
        "source": {
            "mechanics": "exam-studio-multi",
            "resources": dict(source_hashes),
            "copied_visible_body": False,
            "copied_base_bindata": False,
        },
        "design": {
            "paper": "A4",
            "orientation": "portrait",
            "columns": 1,
            "margins_mm": {"left": 20, "right": 20, "top": 18, "bottom": 18},
            "named_styles": list(STYLE_NAMES),
        },
        "policy": {
            "empty_sections": 1,
            "bindata": False,
            "svg": False,
            "endnotes": False,
            "macros": False,
            "solver": False,
            "page_numbering_added_by_renderer": True,
        },
    }


def build_neutral_a4_template(
    output_path: Path | str = DEFAULT_OUTPUT_PATH,
    *,
    source_root: Path | str = DEFAULT_SOURCE_ROOT,
    manifest_path: Path | str | None = None,
) -> dict[str, object]:
    """Atomically build, validate, and optionally describe the clean template."""

    output_path = Path(output_path)
    source_root = Path(source_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    members, source_hashes = _members(source_root)

    handle = tempfile.NamedTemporaryFile(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".hwpx",
        delete=False,
    )
    temporary_path = Path(handle.name)
    handle.close()
    try:
        with zipfile.ZipFile(
            temporary_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for name in PACKAGE_ORDER:
                compression = zipfile.ZIP_STORED if name in {
                    "mimetype",
                    "version.xml",
                    "Preview/PrvImage.png",
                } else zipfile.ZIP_DEFLATED
                archive.writestr(_zip_info(name, compression), members[name])
        errors = validate_neutral_a4_template(temporary_path)
        if errors:
            raise ValueError("invalid neutral HWPX template:\n- " + "\n- ".join(errors))
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    manifest = _manifest(output_path, source_hashes)
    if manifest_path is not None:
        manifest_path = Path(manifest_path)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    manifest_path = args.manifest or args.output.with_suffix(".manifest.json")
    manifest = build_neutral_a4_template(
        args.output, source_root=args.source_root, manifest_path=manifest_path
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
