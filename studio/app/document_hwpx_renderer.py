from __future__ import annotations

import copy
import hashlib
import io
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image, __version__ as PILLOW_VERSION

from .document_hwpx_styles import DocumentStyles
from .document_storage import confined, immutable
from .document_types import Document, DocumentError, Json, JsonMap, mapping, sequence, text
from .hwpx_native import picture_xml
from .hwpx_renderer import HP_NS, _LoadedItem, _Renderer, _TemplateContext, _fragment


class CurrentRenderer(_Renderer):
    def __init__(self, context: _TemplateContext, current: Document, stage: Path) -> None:
        super().__init__('question', context)
        self.style_book = DocumentStyles(context)
        self.current = current
        self.stage = stage
        self.paragraph_style = current['styles']['defaults']
        self.current_item = _LoadedItem(current['document_id'], stage / 'document.json', {}, 'canonical', None, 'saved', 'saved')
        self.image_receipts: list[JsonMap] = []

    def styled_text(self, value: str, style: JsonMap | None = None) -> str:
        return self.style_book.text(value, style or self.current['styles']['defaults'])

    def _render_blocks(self, item: _LoadedItem, blocks: Json, path: str, *, target: list[ET.Element] | None,
                       available_width: int, depth: int, ancestors: frozenset[int], allowed: frozenset[str] | None = None) -> None:
        if allowed == frozenset({'paragraph', 'figure'}):
            allowed = frozenset({'paragraph', 'figure', 'box', 'table'})
        super()._render_blocks(item, blocks, path, target=target, available_width=available_width, depth=depth, ancestors=ancestors, allowed=allowed)

    def _inlines(self, inlines: Json, path: str, *, prefix: str = '', allow_equation: bool = True, available_width: int | None = None) -> tuple[str, str, str]:
        xml = self.styled_text(prefix) if prefix else ''
        plain = prefix
        visible = prefix
        for raw in sequence(inlines, path):
            inline = mapping(raw, path)
            style = mapping(inline.get('document_style', self.paragraph_style), 'style')
            if inline['type'] == 'text':
                value = text(inline['text'], 'text')
                for part in re.split(r'(\r\n|\r|\n)', value):
                    xml += '<hp:lineBreak/>' if part in {'\r\n', '\r', '\n'} else self.styled_text(part, style)
                plain += value
                visible += value
            elif inline['type'] == 'document_formula_image':
                asset = mapping(inline['asset'], 'formula image')
                width, height = int(inline['width']), int(inline['height'])
                if width > (available_width or self.context.writable_width) or height > self.context.writable_height:
                    raise DocumentError('document_formula_overflow', f"Current formula image exceeds HWPX area: {inline['node_id']}")
                binary, _, _ = self._embed_png_asset(self.current_item, asset, path, available_width=width, available_height=height)
                ascent = int(inline['ascent'])
                picture = picture_xml(binary, self.allocator, width=width, height=height)
                xml += picture.replace('<hp:pic ', f'<hp:pic qjs:ascent="{ascent}" qjs:descent="{height - ascent}" ', 1)
                self.canonical['block.figure'] += 1
                self.image_receipts.append({'node_id': inline['node_id'], 'kind': 'current_formula', 'png_sha256': asset['sha256'], 'bmp_sha256': hashlib.sha256(self.binaries[binary]).hexdigest(), 'binary_id': binary, 'width': width, 'height': height, 'ascent': ascent})
                plain += text(inline['latex'], 'latex')
            else:
                part, text_value, shown = super()._inlines([inline], path, allow_equation=allow_equation, available_width=available_width)
                xml += part
                plain += text_value
                visible += shown
        return xml, plain, visible

    def _paragraph(self, content: str, preview: str, *, style: str = 'paragraph', page_break: bool = False, target: list[ET.Element] | None = None, available_width: int | None = None) -> ET.Element:
        destination = self.paragraphs if target is None else target
        before = len(destination)
        result = super()._paragraph(content, preview, style=style, page_break=page_break, target=target, available_width=available_width)
        reference = self.style_book.paragraph(self.paragraph_style)
        for paragraph in destination[before:]:
            paragraph.set('paraPrIDRef', reference)
            for line in paragraph.iter(f'{{{HP_NS}}}lineseg'):
                height = int(line.get('vertsize', '1000'))
                line.set('spacing', str(round(height * (float(self.paragraph_style.get('line_spacing', 1.6)) - 1))))
        return result

    def _paragraph_block(self, block: JsonMap, path: str, *, prefix: str = '', style: str = 'paragraph', target: list[ET.Element] | None = None, available_width: int | None = None) -> str:
        old = self.paragraph_style
        self.paragraph_style = mapping(block.get('document_style', self.current['styles']['defaults']), 'paragraph style')
        try:
            return super()._paragraph_block(block, path, prefix=prefix, style=style, target=target, available_width=available_width)
        finally:
            self.paragraph_style = old

    def _figure(self, item: _LoadedItem, block: JsonMap, path: str, *, target: list[ET.Element] | None = None, available_width: int | None = None, available_height: int | None = None) -> None:
        identifier = text(block['asset_id'], 'asset_id')
        source = next(asset for asset in self.current['assets'] if asset['id'] == identifier)
        raw = confined(self.stage, source['relative_path']).read_bytes()
        with Image.open(io.BytesIO(raw)) as image:
            source_mode = image.mode
            background_policy = 'preserve_opaque_rgb'
            if image.mode in {'RGBA', 'LA'} or 'transparency' in image.info:
                background_policy = 'alpha_composite_white'
                background = Image.new('RGBA', image.size, 'white')
                background.alpha_composite(image.convert('RGBA'))
                picture = background.convert('RGB')
            else:
                picture = image.convert('RGB')
            pixels = picture.size
            output = io.BytesIO()
            picture.save(output, format='PNG')
        png = output.getvalue()
        digest = hashlib.sha256(png).hexdigest()
        relative = f'render-assets/{digest}.png'
        if not confined(self.stage, relative).exists():
            immutable(confined(self.stage, relative), png)
        width = round(float(block.get('width_mm') or pixels[0] * 25.4 / 96) * 7200 / 25.4)
        height = round(float(block.get('height_mm') or width * pixels[1] / pixels[0] * 25.4 / 7200) * 7200 / 25.4)
        if width > (available_width or self.context.writable_width) or height > (available_height or self.context.writable_height):
            raise DocumentError('document_figure_overflow', f"Current figure exceeds HWPX area: {block['node_id']}")
        if block.get('keep_aspect') and block.get('width_mm') and block.get('height_mm'):
            fit = min(width / pixels[0], height / pixels[1])
            width, height = round(pixels[0] * fit), round(pixels[1] * fit)
        asset = {'asset_id': digest, 'state': 'ready', 'media_type': 'image/png', 'path': relative, 'sha256': digest, 'width_px': pixels[0], 'height_px': pixels[1]}
        binary, _, _ = self._embed_png_asset(self.current_item, asset, path, available_width=width, available_height=height)
        self.canonical['block.figure'] += 1
        self._paragraph(picture_xml(binary, self.allocator, width=width, height=height), '[그림]', target=target, available_width=available_width)
        self.image_receipts.append({'node_id': block['node_id'], 'kind': 'owned_figure', 'source_sha256': source['sha256'], 'png_sha256': digest, 'bmp_sha256': hashlib.sha256(self.binaries[binary]).hexdigest(), 'binary_id': binary, 'width': width, 'height': height,
            'background_policy': background_policy, 'source_mode': source_mode, 'image_converter': f'Pillow/{PILLOW_VERSION} PNG/RGB/BMP'})

    def finish(self):
        section, hpf, binaries = super().finish()
        self.context.members['Contents/header.xml'] = self.style_book.bytes()
        return section, hpf, binaries
