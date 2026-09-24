from __future__ import annotations

import copy
import json
from xml.etree import ElementTree as ET

from .document_types import JsonMap
from .hwpx_export import HH_NS
from .hwpx_native import HC_NAMESPACE, text_xml
from .hwpx_renderer import _TemplateContext, _xml_bytes_with_original_namespaces


class DocumentStyles:
    def __init__(self, context: _TemplateContext) -> None:
        self.original = context.members['Contents/header.xml']
        self.header = ET.fromstring(self.original)
        self.characters = self.header.find(f'.//{{{HH_NS}}}charProperties')
        self.paragraphs = self.header.find(f'.//{{{HH_NS}}}paraProperties')
        self.base_character = next(node for node in self.characters if node.get('id') == context.styles['paragraph'].character_property_id)
        self.base_paragraph = next(node for node in self.paragraphs if node.get('id') == context.styles['paragraph'].paragraph_property_id)
        self.char_cache: dict[str, str] = {}
        self.para_cache: dict[str, str] = {}

    def character(self, style: JsonMap) -> tuple[str, int]:
        key = json.dumps(style, sort_keys=True)
        size = round(float(style.get('font_size_pt', 10)) * 100)
        if key in self.char_cache:
            return self.char_cache[key], size
        node = copy.deepcopy(self.base_character)
        identifier = str(max(int(item.get('id', '0')) for item in self.characters) + 1)
        node.set('id', identifier)
        node.set('height', str(size))
        for tag, field in [('bold', 'bold'), ('italic', 'italic'), ('underline', 'underline'), ('supscript', 'superscript'), ('subscript', 'subscript')]:
            for old in list(node.findall(f'{{{HH_NS}}}{tag}')):
                node.remove(old)
            if style.get(field):
                attrs = {'type': 'BOTTOM', 'shape': 'SOLID', 'color': '#000000'} if tag == 'underline' else {}
                ET.SubElement(node, f'{{{HH_NS}}}{tag}', attrs)
        family = style.get('font_family')
        if family:
            names = {'HANGUL': 'hangul', 'LATIN': 'latin', 'HANJA': 'hanja', 'JAPANESE': 'japanese', 'OTHER': 'other', 'SYMBOL': 'symbol', 'USER': 'user'}
            reference = node.find(f'{{{HH_NS}}}fontRef')
            for face in self.header.findall(f'.//{{{HH_NS}}}fontface'):
                slot = names.get(face.get('lang', ''))
                if slot is None:
                    continue
                found = next((font for font in face if font.get('face') == family), None)
                if found is None:
                    found = copy.deepcopy(face[0])
                    found.set('id', str(max(int(font.get('id', '0')) for font in face) + 1))
                    found.set('face', str(family))
                    found.set('isEmbedded', '0')
                    face.append(found)
                    face.set('fontCnt', str(len(face)))
                reference.set(slot, found.get('id'))
        self.characters.append(node)
        self.characters.set('itemCnt', str(len(self.characters)))
        self.char_cache[key] = identifier
        return identifier, size

    def paragraph(self, style: JsonMap) -> str:
        key = json.dumps({name: style.get(name) for name in ('align', 'line_spacing', 'paragraph_spacing_mm')}, sort_keys=True)
        if key in self.para_cache:
            return self.para_cache[key]
        node = copy.deepcopy(self.base_paragraph)
        identifier = str(max(int(item.get('id', '0')) for item in self.paragraphs) + 1)
        node.set('id', identifier)
        if style.get('align'):
            for align in node.iter(f'{{{HH_NS}}}align'):
                align.set('horizontal', str(style['align']).upper())
        for spacing in node.iter(f'{{{HH_NS}}}lineSpacing'):
            spacing.set('type', 'PERCENT')
            spacing.set('value', str(round(float(style.get('line_spacing', 1.6)) * 100)))
        for after in node.iter(f'{{{HC_NAMESPACE}}}next'):
            after.set('value', str(round(float(style.get('paragraph_spacing_mm', 3)) * 7200 / 25.4)))
        self.paragraphs.append(node)
        self.paragraphs.set('itemCnt', str(len(self.paragraphs)))
        self.para_cache[key] = identifier
        return identifier

    def text(self, value: str, style: JsonMap) -> str:
        identifier, size = self.character(style)
        return f'<qjs:mark kind="document-style:{identifier}:{size}">{text_xml(value)}</qjs:mark>'

    def bytes(self) -> bytes:
        return _xml_bytes_with_original_namespaces(self.header, self.original)
