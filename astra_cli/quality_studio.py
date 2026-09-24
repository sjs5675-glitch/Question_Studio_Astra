from __future__ import annotations

import json
from pathlib import Path

from .contracts import AstraError, MODEL, POLICY_VERSION, digest, effort_for, json_object, check_schema
from .quality import identity

ROOT = Path(__file__).resolve().parents[1]
PROMPT = ROOT / 'studio/prompts/verify_solution_pair.md'
SCHEMA = ROOT / 'studio/schemas/solution-pair-verification.schema.json'
POLICY = 'astra-studio-pair-v1'


def _asset(document: dict, asset_id: str) -> dict | None:
    return next((a for a in document.get('resources', {}).get('assets', [])
                 if isinstance(a, dict) and a.get('asset_id') == asset_id), None)


def _current(document: dict, item_root: Path) -> dict:
    selection = document['provenance']['solution_selection']
    question = _asset(document, 'source-question')
    selected = _asset(document, selection['source_asset_id'])
    if not question or not selected or selected.get('sha256') != selection.get('source_sha256'):
        raise ValueError('current_source_changed')
    images = []
    for asset in (question, selected):
        relative = Path(asset['path'])
        if relative.is_absolute() or '..' in relative.parts or ':' in str(relative):
            raise ValueError('source_path_invalid')
        path = (item_root / relative).resolve()
        if not path.is_relative_to(item_root.resolve()) or not path.is_file() or digest(path.read_bytes()) != asset['sha256']:
            raise ValueError('current_source_changed')
        images.append({'asset_id': asset['asset_id'], 'sha256': asset['sha256'], 'path': str(path)})
    selected_link = next((link for link in document.get('provenance', {}).get('solution_links', [])
                          if isinstance(link, dict) and link.get('source_asset_id') == selected['asset_id']), {})
    sources = selected_link.get('image_sources') or selection.get('image_sources') or [
        {'asset_id': selected['asset_id'], 'sha256': selected['sha256'], 'order': 0}]
    return {'item_id': document['item_id'], 'selection_revision': selection['revision'],
            'selected_asset_id': selection['source_asset_id'], 'images': images,
            'image_sources': sources}


def verdict(result: dict) -> str:
    if result.get('same_question') == 'mismatch' or result.get('content_match') == 'mismatch' or result.get('number_consistency') == 'mismatch' or result.get('answer_consistency') == 'conflict':
        return 'mismatch'
    if (result.get('same_question') == result.get('content_match') == 'match'
            and result.get('number_consistency') in {'match', 'source_number_issue', 'unknown'}
            and result.get('crop_complete') is True and result.get('neighbor_content_absent') is True
            and isinstance(result.get('confidence'), (int, float)) and result['confidence'] >= 0.9
            and result.get('evidence')):
        return 'verified'
    return 'review'


def make_receipt(document: dict, item_root: Path, prompt: str, raw: str, result: dict, runtime: dict) -> dict:
    target = _current(document, item_root)
    schema = json_object(SCHEMA.read_text(encoding='utf-8'))
    check_schema(result, schema)
    expected = {'requested_model': MODEL, 'selected_model': MODEL, 'requested_effort': effort_for('solution-verify'),
                'selected_effort': effort_for('solution-verify'), 'policy_version': POLICY_VERSION,
                'stage': 'solution-verify', 'status': 'succeeded', 'terminal_completed': True,
                'prompt_sha256': digest(prompt),
                'schema_sha256': digest(json.dumps(schema, sort_keys=True, ensure_ascii=False)),
                'image_sha256': [i['sha256'] for i in target['images']], 'output_sha256': digest(raw)}
    if any(runtime.get(k) != v for k, v in expected.items()):
        raise ValueError('runtime_contract_mismatch')
    if ('CURRENT_EVIDENCE_TARGET_SHA256=' + identity(target)) not in prompt.splitlines():
        raise ValueError('invocation_target_mismatch')
    if json_object(raw) != result or result.get('item_id') != target['item_id'] or result.get('selected_asset_id') != target['selected_asset_id']:
        raise ValueError('result_lineage_mismatch')
    body = {'policy': POLICY, 'target': target, 'prompt_sha256': digest(prompt),
            'template_sha256': digest(PROMPT.read_bytes()), 'schema_sha256': digest(SCHEMA.read_bytes()),
            'runtime': runtime, 'raw_sha256': digest(raw), 'result': result, 'verdict': verdict(result)}
    return {**body, 'receipt_id': identity(body)}


def validate_receipt(document: dict, item_root: Path, *, require_verified: bool = True) -> str | None:
    try:
        pointer = document['provenance']['solution_selection']['verification_receipt']
        relative = Path(pointer['path'])
        if relative.is_absolute() or '..' in relative.parts or ':' in str(relative):
            return 'quality_studio_receipt_path_invalid'
        path = (item_root / relative).resolve()
        target = _current(document, item_root)
        expected_relative = Path('jobs') / 'solution_verification' / str(target['selection_revision']) / identity(target) / 'receipt.json'
        if relative != expected_relative:
            return 'quality_studio_receipt_path_invalid'
        if not path.is_relative_to(item_root.resolve()) or digest(path.read_bytes()) != pointer['sha256']:
            return 'quality_studio_receipt_changed'
        receipt = json_object(path.read_text(encoding='utf-8'))
        if receipt.get('policy') != POLICY or receipt.get('receipt_id') != identity({k: v for k, v in receipt.items() if k != 'receipt_id'}):
            return 'quality_studio_receipt_invalid'
        if receipt.get('target') != _current(document, item_root):
            return 'quality_studio_selection_changed'
        if receipt.get('template_sha256') != digest(PROMPT.read_bytes()) or receipt.get('schema_sha256') != digest(SCHEMA.read_bytes()):
            return 'quality_studio_contract_changed'
        prompt = path.with_name('request.txt').read_text(encoding='utf-8')
        raw = path.with_name('response.json').read_text(encoding='utf-8')
        if receipt.get('prompt_sha256') != digest(prompt) or receipt.get('raw_sha256') != digest(raw):
            return 'quality_studio_response_changed'
        rebuilt = make_receipt(document, item_root, prompt, raw, receipt['result'], receipt['runtime'])
        if rebuilt != receipt:
            return 'quality_studio_receipt_invalid'
        return None if not require_verified or receipt.get('verdict') == 'verified' else 'quality_studio_not_verified'
    except (AstraError, KeyError, TypeError, ValueError, OSError):
        return 'quality_studio_receipt_invalid'
