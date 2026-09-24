"""Current source evidence for Crop-owned automatic link decisions."""
from __future__ import annotations

import json
import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from .contracts import AstraError, Json, JsonMap, MODEL, POLICY_VERSION, digest, effort_for, json_object, check_schema

QUALITY_POLICY: Final = 'astra-link-quality-v1'
ROOT: Final = Path(__file__).resolve().parents[1]


def canonical(value: Json) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def identity(value: Json) -> str:
    return digest(canonical(value))


def mapping(value: Json) -> JsonMap:
    if not isinstance(value, dict):
        raise AstraError('quality_receipt_malformed')
    return value


def file_record(path: Path) -> JsonMap:
    resolved = path.resolve(strict=True)
    return {'path': str(resolved), 'sha256': digest(resolved.read_bytes())}


def check_file(value: Json, label: str) -> Path:
    record = mapping(value)
    if not isinstance(record.get('path'), str) or not isinstance(record.get('sha256'), str):
        raise AstraError('quality_receipt_malformed')
    path = Path(record['path'])
    if not path.is_file() or digest(path.read_bytes()) != record['sha256']:
        raise AstraError(f'quality_current_{label}_changed')
    return path.resolve()


def pointer(leaf: Path, unit_id: str, asset_id: str) -> Path:
    return leaf / 'solutions/quality' / (identity([unit_id, asset_id]) + '.json')


def source_path(leaf: Path, value: Json) -> Path:
    if not isinstance(value, str) or not value:
        raise AstraError('quality_source_path_missing')
    path = Path(value)
    return (path if path.is_absolute() else leaf / path).resolve(strict=True)


def manifest_input(leaf: Path, unit_id: str) -> JsonMap:
    manifest = json_object((leaf / 'manifest.json').read_text(encoding='utf-8'))
    items = manifest.get('items')
    if not isinstance(items, list):
        raise AstraError('quality_manifest_malformed')
    matches = [item for item in items if isinstance(item, dict) and item.get('unit_id') == unit_id]
    if len(matches) != 1:
        raise AstraError('quality_manifest_identity_ambiguous')
    declared = matches[0].get('output_sha256')
    if not isinstance(declared, str) or re.fullmatch(r'[0-9a-fA-F]{64}', declared) is None:
        raise AstraError('quality_declared_problem_crop_missing_or_invalid')
    if digest(source_path(leaf, matches[0].get('output_file')).read_bytes()) != declared.casefold():
        raise AstraError('quality_declared_problem_crop_mismatch')
    return {'source_pdf': manifest.get('source_pdf'), 'source_sha256': manifest.get('source_sha256'), 'item': matches[0]}


def normalize_link_result(raw: JsonMap, problem: JsonMap, asset: JsonMap,
                          *, scope: bool = False) -> JsonMap:
    result = copy.deepcopy(raw)
    questions = result.get('questions')
    if isinstance(questions, list) and len(questions) == 1 and isinstance(questions[0], dict):
        result = questions[0]
    if not scope and problem.get('subquestion_label') is None and asset.get('subquestion_label') is None and result.get('subquestion_match') in {None, 'unknown'}:
        result['subquestion_match'] = 'not_applicable'
    return result


def check_result_lineage(wrapper: JsonMap, target: JsonMap, stage: str,
                         runtime: JsonMap, result: JsonMap) -> None:
    text = mapping(wrapper.get('meta')).get('response_text')
    if not isinstance(text, str) or digest(text) != runtime.get('output_sha256'):
        raise AstraError('quality_raw_response_hash_mismatch')
    raw = json_object(text)
    if raw != wrapper.get('result'):
        raise AstraError('quality_raw_result_changed')
    expected = normalize_link_result(raw, mapping(target.get('problem')),
                                     mapping(target.get('asset')), scope=stage == 'solution-scope-verify')
    if expected != result:
        raise AstraError('quality_raw_result_lineage_mismatch')


def make_receipt(*, leaf: Path, target: JsonMap, files: JsonMap, template: Path,
                 schema: Path, prompt_sha256: str, stage: str, runtime: JsonMap,
                 result: JsonMap, raw: Path) -> JsonMap:
    """Issue only from a completed current invocation, never from legacy passes."""
    expected = {'requested_model': MODEL, 'selected_model': MODEL,
                'requested_effort': effort_for(stage), 'selected_effort': effort_for(stage),
                'policy_version': POLICY_VERSION, 'stage': stage,
                'prompt_sha256': prompt_sha256,
                'schema_sha256': digest(json.dumps(json_object(schema.read_text(encoding='utf-8')), sort_keys=True, ensure_ascii=False)),
                'status': 'succeeded', 'terminal_completed': True}
    if any(runtime.get(key) != value for key, value in expected.items()):
        raise AstraError('quality_runtime_contract_mismatch')
    images = files.get('inference_images')
    if not isinstance(images, list) or runtime.get('image_sha256') != [mapping(image).get('sha256') for image in images]:
        raise AstraError('quality_runtime_images_mismatch')
    raw_wrapper = json_object(raw.read_text(encoding='utf-8'))
    check_result_lineage(raw_wrapper, target, stage, runtime, result)
    raw_meta = mapping(raw_wrapper.get('meta'))
    actual_prompt = raw_meta.get('request_prompt')
    marker = 'CURRENT_EVIDENCE_TARGET_SHA256=' + identity(target)
    if not isinstance(actual_prompt, str) or digest(actual_prompt) != prompt_sha256 or marker not in actual_prompt.splitlines():
        raise AstraError('quality_invocation_target_mismatch')
    receipt: JsonMap = {'policy': QUALITY_POLICY, 'target': target, 'target_id': identity(target),
                        'files': files, 'template': file_record(template), 'schema': file_record(schema),
                        'prompt_sha256': prompt_sha256, 'stage': stage, 'runtime': runtime,
                        'result': result, 'raw': file_record(raw)}
    receipt['receipt_id'] = identity(receipt)
    return receipt


def _read_current_lineage(receipt: Json, leaf: Path, *, problem: JsonMap | None = None,
                          asset: JsonMap | None = None, conflict: bool = False,
                          corroborating: JsonMap | None = None) -> tuple[bool, JsonMap, JsonMap, JsonMap]:
    """Re-read current bytes and the authoritative pair head at every decision."""
    try:
        value = mapping(receipt)
        if value.get('policy') != QUALITY_POLICY:
            raise AstraError('quality_receipt_missing_or_obsolete')
        body = {key: entry for key, entry in value.items() if key != 'receipt_id'}
        if value.get('receipt_id') != identity(body):
            raise AstraError('quality_receipt_digest_mismatch')
        target, files, runtime, result = (mapping(value.get(key)) for key in ('target', 'files', 'runtime', 'result'))
        sources = mapping(target.get('sources'))
        if sources != {key: files.get(key) for key in ('problem_pdf', 'problem_crop', 'solution_document', 'inference_images')}:
            raise AstraError('quality_source_binding_mismatch')
        bound_problem, bound_asset = mapping(target.get('problem')), mapping(target.get('asset'))
        unit_id, asset_id = bound_problem.get('item_id'), bound_asset.get('asset_id')
        if not isinstance(unit_id, str) or not isinstance(asset_id, str):
            raise AstraError('quality_receipt_malformed')
        if value.get('target_id') != identity(target):
            raise AstraError('quality_target_digest_mismatch')
        if problem is not None and problem != bound_problem:
            raise AstraError('quality_current_problem_identity_changed')
        if asset is not None and asset != bound_asset:
            raise AstraError('quality_current_asset_geometry_or_role_changed')
        if conflict and (corroborating is None or target.get('corroborating') != corroborating):
            raise AstraError('quality_corroborating_target_changed')
        current = manifest_input(leaf, unit_id)
        if current != target.get('manifest'):
            raise AstraError('quality_current_manifest_changed')
        source = source_path(leaf, current.get('source_pdf'))
        if check_file(files.get('problem_pdf'), 'problem_pdf') != source or digest(source.read_bytes()) != current.get('source_sha256'):
            raise AstraError('quality_current_problem_source_mismatch')
        item = mapping(current.get('item'))
        crop_path = source_path(leaf, item.get('output_file'))
        if not crop_path.is_relative_to(leaf.resolve()) or check_file(files.get('problem_crop'), 'problem_crop') != crop_path:
            raise AstraError('quality_current_problem_crop_mismatch')
        for label in ('solution_document', 'solution_pdf', 'solution_crop', 'raw_result'):
            check_file(files.get(label), label)
        images = files.get('inference_images')
        if not isinstance(images, list) or not images:
            raise AstraError('quality_receipt_malformed')
        if mapping(images[0]).get('sha256') != mapping(files.get('problem_crop')).get('sha256') or len(images) < 2 or images[1] != files.get('solution_crop'):
            raise AstraError('quality_current_inference_target_mismatch')
        for image in images:
            check_file(image, 'inference_image')
        template = check_file(value.get('template'), 'prompt')
        schema = check_file(value.get('schema'), 'schema')
        stage = value.get('stage')
        if stage not in {'solution-verify', 'solution-verify-correction', 'solution-scope-verify'}:
            raise AstraError('quality_stage_invalid')
        is_scope = stage == 'solution-scope-verify'
        if template != ROOT / ('crop/prompts/verify_scope_pair.md' if is_scope else 'crop/prompts/verify_solution_link.md') or schema != ROOT / ('crop/scope_pair_verification.schema.json' if is_scope else 'crop/solution_link_verification.schema.json'):
            raise AstraError('quality_current_contract_path_mismatch')
        expected = {'requested_model': MODEL, 'selected_model': MODEL, 'policy_version': POLICY_VERSION,
                    'requested_effort': effort_for(stage), 'selected_effort': effort_for(stage),
                    'stage': stage, 'prompt_sha256': value.get('prompt_sha256'),
                    'schema_sha256': digest(json.dumps(json_object(schema.read_text(encoding='utf-8')), sort_keys=True, ensure_ascii=False)),
                    'image_sha256': [mapping(image).get('sha256') for image in images],
                    'status': 'succeeded', 'terminal_completed': True}
        if any(runtime.get(key) != entry for key, entry in expected.items()):
            raise AstraError('quality_current_runtime_contract_mismatch')
        raw_path = check_file(value.get('raw'), 'raw_invocation')
        raw_wrapper = json_object(raw_path.read_text(encoding='utf-8'))
        raw_meta = mapping(raw_wrapper.get('meta'))
        request_prompt = raw_meta.get('request_prompt')
        if not isinstance(request_prompt, str) or digest(request_prompt) != value.get('prompt_sha256') or ('CURRENT_EVIDENCE_TARGET_SHA256=' + identity(target)) not in request_prompt.splitlines():
            raise AstraError('quality_invocation_target_mismatch')
        if raw_meta.get('runtime_receipt') != runtime:
            raise AstraError('quality_runtime_receipt_changed')
        check_result_lineage(raw_wrapper, target, stage, runtime, result)
        result_path = check_file(files.get('raw_result'), 'raw_result')
        if json_object(result_path.read_text(encoding='utf-8')).get('result') != result:
            raise AstraError('quality_current_result_changed')
        head = json_object(pointer(leaf, unit_id, asset_id).read_text(encoding='utf-8'))
        if head != value:
            raise AstraError('quality_current_pair_evidence_changed')
        check_schema(result, json_object(schema.read_text(encoding='utf-8')))
        if is_scope and (result.get('problem_scope_id') != bound_problem.get('scope_id') or 'episode:' + str(result.get('solution_scope_id')) != bound_asset.get('scope_id')):
            raise AstraError('quality_result_scope_mismatch')
        return is_scope, bound_problem, bound_asset, result
    except AstraError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise AstraError('quality_receipt_malformed_or_unavailable') from exc


def validate_receipt(receipt: Json, leaf: Path, *, problem: JsonMap | None = None,
                     asset: JsonMap | None = None, conflict: bool = False, min_confidence: float = .9,
                     corroborating: JsonMap | None = None) -> str | None:
    try:
        is_scope, bound_problem, bound_asset, result = _read_current_lineage(receipt, leaf, problem=problem, asset=asset, conflict=conflict, corroborating=corroborating)
        unit_id, asset_id = bound_problem['item_id'], bound_asset['asset_id']
        optional = ('subquestion_match',) + (() if is_scope else ('range_or_group_match',))
        if not is_scope and bound_asset.get('role') == 'quick_answer':
            optional += ('hierarchy_scope_match', 'fingerprint_match')
        if any(result.get(field) not in {'match', 'not_applicable'} for field in optional):
            raise AstraError('quality_identity_inconclusive')
        if result.get('question_id') != unit_id or result.get('candidate_ids') != [asset_id] or result.get('candidate_role') != bound_asset.get('role'):
            raise AstraError('quality_result_identity_mismatch')
        confidence = result.get('confidence')
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not min_confidence <= confidence <= 1:
            raise AstraError('quality_confidence_below_threshold')
        if result.get('decision') != ('conflict' if conflict else 'verified') or result.get('same_question') != 'yes' or result.get('explicit_answer_conflict') is not conflict:
            raise AstraError('quality_current_result_not_approved')
        fields = ('candidate_crop_complete', 'target_identifier_visible', 'first_line_complete', 'last_line_complete', 'neighbor_entry_absent', 'next_question_absent')
        if any(result.get(field) is not True for field in fields):
            raise AstraError('quality_crop_incomplete')
        required = ('parent_number_match', 'monotonic_order_match', 'scope_episode_match' if is_scope else 'anchor_segment_match')
        if (bound_asset.get('role') != 'quick_answer' or is_scope
                or (bound_problem.get('anchor') is None and bound_asset.get('expanded_source_binding') is not None)):
            required += ('hierarchy_scope_match', 'fingerprint_match')
        if any(result.get(field) != 'match' for field in required) or result.get('review_reasons') not in ([], None):
            raise AstraError('quality_identity_inconclusive')
        if result.get('target_answer_visible') not in ({'yes'} if bound_asset.get('role') == 'quick_answer' else {'yes', 'not_applicable'}):
            raise AstraError('quality_answer_visibility_inconclusive')
        return None
    except AstraError as exc:
        return exc.code
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return 'quality_receipt_malformed_or_unavailable'


@dataclass(frozen=True, slots=True)
class NonapprovalEvidence:
    kind: Literal['wrong_link', 'review', 'low_confidence']
    same_item: bool
    explicit_answer_conflict: bool
    confidence: float


def read_nonapproval_receipt(receipt: Json, leaf: Path, *, problem: JsonMap,
                            asset: JsonMap, corroborating: JsonMap | None = None) -> NonapprovalEvidence:
    if not leaf.resolve().is_relative_to(ROOT):
        raise AstraError('quality_evidence_root_outside_workspace')
    scope, _, _, result = _read_current_lineage(receipt, leaf, problem=problem, asset=asset)
    if result.get('question_id') != problem.get('item_id') or result.get('candidate_ids') != [asset.get('asset_id')] or result.get('candidate_role') != asset.get('role'):
        raise AstraError('quality_result_identity_mismatch')
    if mapping(mapping(receipt).get('target')).get('corroborating') != corroborating:
        raise AstraError('quality_corroborating_target_changed')
    decision, same, conflict, confidence = result.get('decision'), result.get('same_question'), result.get('explicit_answer_conflict'), result.get('confidence')
    if type(confidence) not in (float, int) or not 0 <= confidence <= 1 or type(conflict) is not bool:
        raise AstraError('quality_nonapproval_incoherent')
    if decision == 'wrong_link' and same == 'no' and conflict is False:
        return NonapprovalEvidence('wrong_link', False, False, float(confidence))
    if decision == 'review' and same in {'yes', 'no', 'uncertain'} and conflict is False:
        return NonapprovalEvidence('review', False, False, float(confidence))
    if decision not in {'verified', 'conflict'} or same != 'yes' or conflict != (decision == 'conflict') or confidence >= .9 or (conflict and corroborating is None):
        raise AstraError('quality_nonapproval_incoherent')
    optional = ('subquestion_match',) + (() if scope else ('range_or_group_match',))
    required = ('parent_number_match', 'monotonic_order_match', 'scope_episode_match' if scope else 'anchor_segment_match')
    if asset.get('role') != 'quick_answer' or scope: required += ('hierarchy_scope_match', 'fingerprint_match')
    else: optional += ('hierarchy_scope_match', 'fingerprint_match')
    flags = ('candidate_crop_complete', 'target_identifier_visible', 'first_line_complete', 'last_line_complete', 'neighbor_entry_absent', 'next_question_absent')
    if any(result.get(field) is not True for field in flags) or any(result.get(field) != 'match' for field in required) or any(result.get(field) not in {'match', 'not_applicable'} for field in optional) or result.get('review_reasons') not in ([], None) or result.get('target_answer_visible') not in ({'yes'} if asset.get('role') == 'quick_answer' else {'yes', 'not_applicable'}) or (scope and problem.get('subquestion_label') is not None and result.get('subquestion_match') != 'match'):
        raise AstraError('quality_nonapproval_incoherent')
    return NonapprovalEvidence('low_confidence', decision == 'verified', conflict, float(confidence))
