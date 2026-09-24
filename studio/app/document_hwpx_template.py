from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .document_storage import immutable
from .document_types import Document, mapping
from .hwpx_export import HP_NS, OutputPlan
from .hwpx_renderer import _load_template_context, _xml_bytes_with_original_namespaces
from .question_set_hwpx import _problem_bank_profile, _problem_bank_template_bytes


def document_template(current: Document, stage: Path):
    original = _problem_bank_template_bytes()
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        infos = archive.infolist()
        members = {item.filename: archive.read(item.filename) for item in infos}
    section = ET.fromstring(members['Contents/section0.xml'])
    page = section.find(f'.//{{{HP_NS}}}pagePr')
    margins = page.find(f'{{{HP_NS}}}margin')
    columns = section.find(f'.//{{{HP_NS}}}colPr')
    layout = current['layout']
    unit = lambda value: str(round(float(value) * 7200 / 25.4))
    page.set('width', unit(layout['width_mm']))
    page.set('height', unit(layout['height_mm']))
    for key, value in mapping(layout['margins_mm'], 'margins').items():
        margins.set(key, unit(value))
    margins.set('header', '0')
    margins.set('footer', '0')
    columns.set('sameGap', unit(layout['column_gap_mm']))
    members['Contents/section0.xml'] = _xml_bytes_with_original_namespaces(section, members['Contents/section0.xml'])
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for info in infos:
            archive.writestr(info, members[info.filename])
    template = stage / 'template.hwpx'
    immutable(template, output.getvalue())
    profile = _problem_bank_profile('question')
    profile['design']['page'].update(width_mm=layout['width_mm'], height_mm=layout['height_mm'], margins_mm=layout['margins_mm'])
    profile['design']['columns']['gap_mm'] = layout['column_gap_mm']
    template_hash = hashlib.sha256(output.getvalue()).hexdigest()
    profile['template'].update(path='template.hwpx', sha256=template_hash)
    profile_path = stage / 'profile.json'
    profile_data = json.dumps(profile, ensure_ascii=False).encode('utf-8')
    immutable(profile_path, profile_data)
    plan = OutputPlan('question', stage / 'output.hwpx', profile_path, hashlib.sha256(profile_data).hexdigest(), template,
                      template_hash, (), ('document.current',), {})
    return _load_template_context(plan)
