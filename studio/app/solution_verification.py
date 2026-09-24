from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

from PIL import Image

from astra_cli import runtime
from astra_cli.quality import identity
from astra_cli.quality_studio import PROMPT, SCHEMA, _current, make_receipt, validate_receipt, verdict
from .model import atomic_write_json, load_json_object, save_json_cas


def _ink_at_edge(path: Path, edge: str) -> bool:
    with Image.open(path) as opened:
        gray = opened.convert('L')
        width, height = gray.size
        band = max(3, round((height if edge in {'top', 'bottom'} else width) * 0.015))
        boxes = {
            'top': (0, 0, width, band), 'bottom': (0, height - band, width, height),
            'left': (0, 0, band, height), 'right': (width - band, 0, width, height),
        }
        strip = gray.crop(boxes[edge])
        return sum(pixel < 170 for pixel in strip.getdata()) >= max(12, strip.width * strip.height // 1000)


def repair_selected_crop(question_path: Path, outcome: dict[str, Any], import_roots: tuple[Path, ...]) -> dict[str, Any] | None:
    from .solution_sources import select_source

    result = outcome.get('result')
    suggestion = result.get('crop_repair') if isinstance(result, dict) else None
    if (outcome.get('state') != 'review' or not isinstance(suggestion, dict)
        or result.get('crop_complete') is not False or result.get('neighbor_content_absent') is not True
        or result.get('same_question') != 'match' or result.get('content_match') != 'match'
        or result.get('answer_consistency') == 'conflict'
        or float(result.get('confidence') or 0) < 0.9
        or suggestion.get('image_index') != 1 or suggestion.get('edge') not in {'top', 'bottom', 'left', 'right'}
        or float(suggestion.get('confidence') or 0) < 0.9
        or not isinstance(suggestion.get('visible_evidence'), str) or len(suggestion['visible_evidence'].strip()) < 2):
        return None
    item_root = question_path.parent
    document = load_json_object(question_path)
    selection = document.get('provenance', {}).get('solution_selection')
    if not isinstance(selection, dict) or selection.get('origin') != 'auto_candidate':
        return None
    if validate_receipt(document, item_root, require_verified=False) is not None:
        return None
    link = next((entry for entry in document['provenance'].get('solution_links', [])
                 if isinstance(entry, dict) and entry.get('source_asset_id') == selection.get('source_asset_id')), None)
    if (not isinstance(link, dict) or link.get('source_origin') == 'uploaded_image'
        or 'auto_crop_unverified' in link.get('review_reasons', [])
        or not isinstance(link.get('parts'), list) or len(link['parts']) != 1):
        return None
    part = link['parts'][0]
    box = part.get('bbox') if isinstance(part, dict) else None
    if (not isinstance(box, dict) or box.get('unit') != 'ratio' or box.get('origin') != 'top-left'
        or any(type(box.get(key)) not in {int, float} for key in ('left', 'top', 'right', 'bottom'))
        or type(part.get('pdf_page')) is not int or not isinstance(link.get('document_sha256'), str)):
        return None
    bounds = {key: float(box[key]) for key in ('left', 'top', 'right', 'bottom')}
    if not (0 <= bounds['left'] < bounds['right'] <= 1 and 0 <= bounds['top'] < bounds['bottom'] <= 1):
        return None
    edge = suggestion['edge']
    previous = bounds[edge]
    bounds[edge] = max(0, previous - 0.025) if edge in {'top', 'left'} else min(1, previous + 0.025)
    if abs(bounds[edge] - previous) < 0.005:
        return None
    target = _current(document, item_root)
    if not _ink_at_edge(Path(target['images'][1]['path']), edge):
        return None
    receipt_sha = outcome.get('receipt', {}).get('sha256')
    if not isinstance(receipt_sha, str):
        return None
    operation_id = 'auto-crop-' + hashlib.sha256(
        f"{document['item_id']}:{selection['revision']}:{receipt_sha}:{edge}".encode()).hexdigest()[:24]
    payload = {'operation_id': operation_id, 'expected_revision': document['workflow']['revision'],
               'expected_selection_revision': selection['revision'],
               'source': {'kind': 'crop', 'base_source_asset_id': selection['source_asset_id'],
                          'parts': [{'page_ref': f"solution-page-{link['document_sha256']}-{part['pdf_page']}",
                                     'bbox': {'unit': 'ratio', 'origin': 'top-left', **bounds}}]}}
    return select_source(question_path, payload, import_roots=import_roots, auto_crop=True)


def alternative_assets(document: dict[str, Any], selected_asset_id: str) -> list[str]:
    selection = document.get('provenance', {}).get('solution_selection', {})
    if selection.get('origin') != 'auto_candidate':
        return []
    ready = {asset.get('asset_id') for asset in document.get('resources', {}).get('assets', [])
             if isinstance(asset, dict) and asset.get('section') == 'explanation'
             and asset.get('role') == 'source_crop' and asset.get('state', 'ready') == 'ready'}
    links = [link for link in document.get('provenance', {}).get('solution_links', [])
             if isinstance(link, dict) and link.get('source_asset_id') in ready
             and link.get('source_asset_id') != selected_asset_id
             and link.get('kind') in {'detailed_solution', 'mixed'}
             and not any('rejected' in str(reason) or 'excluded' in str(reason)
                         for reason in link.get('review_reasons', []))]
    links.sort(key=lambda link: (-float(link.get('confidence') or 0), str(link['source_asset_id'])))
    return list(dict.fromkeys(link['source_asset_id'] for link in links))[:2]


def can_try_alternative(target: dict[str, Any]) -> bool:
    return int(target.get('ocr_candidates') or 1) < 3 and int(target.get('ocr_calls') or 0) < 3


def mark_pair_mismatch(question_path: Path) -> None:
    document = load_json_object(question_path)
    selection = document.get('provenance', {}).get('solution_selection', {})
    if selection.get('origin') != 'auto_candidate':
        return
    for link in document.get('provenance', {}).get('solution_links', []):
        if link.get('source_asset_id') != selection.get('source_asset_id'):
            continue
        reasons = link.setdefault('review_reasons', [])
        if 'pair_mismatch_rejected' not in reasons:
            reasons.append('pair_mismatch_rejected')
            link['status'] = 'review'
            save_json_cas(question_path, document, expected_revision=document['workflow']['revision'], preserve_approval=True)
        return


def verify_selected(question_path: Path) -> dict[str, Any]:
    item_root = question_path.parent
    document = load_json_object(question_path)
    selection = document.get('provenance', {}).get('solution_selection')
    if not isinstance(selection, dict):
        return {'state': 'review', 'reason': 'selection_missing'}
    if selection.get('verification_receipt') and validate_receipt(document, item_root, require_verified=False) is None:
        receipt_path = item_root / selection['verification_receipt']['path']
        saved = load_json_object(receipt_path)
        return {'state': saved['verdict'], 'reason': 'current_receipt',
                'receipt': selection['verification_receipt'], 'result': saved.get('result')}
    target = _current(document, item_root)
    schema = json.loads(SCHEMA.read_text(encoding='utf-8'))
    prompt = (PROMPT.read_text(encoding='utf-8') + '\n'
              + 'CURRENT_EVIDENCE_TARGET_SHA256=' + identity(target) + '\n'
              + 'Item ID: ' + target['item_id'] + '\nSelected asset ID: ' + target['selected_asset_id'] + '\n'
              + 'Question OCR and solution OCR are supplementary only:\n'
              + json.dumps({'question': document.get('content', {}).get('question'),
                            'answer': document.get('content', {}).get('answer'),
                            'explanation': document.get('content', {}).get('explanation')}, ensure_ascii=False))
    images = tuple(Path(image['path']) for image in target['images'])
    response = runtime.run(stage='solution-verify', prompt=prompt, schema=schema, images=images)
    receipt = make_receipt(document, item_root, prompt, response.text, response.value, response.receipt)
    receipt_dir = item_root / 'jobs' / 'solution_verification' / str(selection['revision']) / identity(target)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    (receipt_dir / 'request.txt').write_text(prompt, encoding='utf-8')
    (receipt_dir / 'response.json').write_text(response.text, encoding='utf-8')
    receipt_path = receipt_dir / 'receipt.json'
    atomic_write_json(receipt_path, receipt)
    current = load_json_object(question_path)
    if _current(current, item_root) != target:
        return {'state': 'review', 'reason': 'selection_changed'}
    current_selection = current['provenance']['solution_selection']
    current_selection['verification_receipt'] = {
        'path': str(receipt_path.relative_to(item_root)).replace('\\', '/'),
        'sha256': hashlib.sha256(receipt_path.read_bytes()).hexdigest()}
    state = verdict(response.value)
    if state == 'verified' and validate_receipt(current, item_root) is None:
        for link in current['provenance'].get('solution_links', []):
            if link.get('source_asset_id') == target['selected_asset_id']:
                link['status'] = 'verified'
                link['confidence'] = response.value['confidence']
                break
    save_json_cas(question_path, current, expected_revision=current['workflow']['revision'], preserve_approval=True)
    return {'state': state, 'reason': 'pair_verified' if state == 'verified' else 'pair_' + state,
            'receipt': current_selection['verification_receipt'], 'result': response.value}
