"""Validate Crop decisions against the currently materialized import surface."""
from __future__ import annotations

from pathlib import Path

from .contracts import AstraError, JsonMap
from .quality_reads import read_json
from .quality import QUALITY_POLICY, check_file, identity, mapping, source_path, validate_receipt


def decision_body(link: JsonMap) -> JsonMap:
    return {key: value for key, value in link.items() if key != 'quality_decision'}


def decision_reason(leaf: Path, manifest: JsonMap, unit_id: str) -> str | None:
    try:
        link = mapping(mapping(manifest.get('items')).get(unit_id))
        if link.get('quality_decision') is None:
            return 'quality_final_decision_missing'
        decision = mapping(link.get('quality_decision'))
        if decision.get('policy') != QUALITY_POLICY or decision.get('decision_sha256') != identity(decision_body(link)):
            return 'quality_final_decision_missing_or_changed'
        ids = list(link.get('answer_ids') or []) + list(link.get('explanation_ids') or [])
        receipts = mapping(decision.get('receipts'))
        if link.get('status') != 'verified' or not ids or set(ids) != set(receipts):
            return 'quality_final_decision_not_verified'
        assets = mapping(manifest.get('assets'))
        for asset_id in ids:
            if not isinstance(asset_id, str):
                raise AstraError('quality_receipt_malformed')
            receipt = mapping(receipts.get(asset_id))
            reason = validate_receipt(receipt, leaf)
            if reason is not None:
                return reason
            target = mapping(receipt.get('target'))
            files = mapping(receipt.get('files'))
            signature = mapping(target.get('rendered_asset'))
            asset = mapping(assets.get(asset_id))
            if mapping(target.get('problem')).get('item_id') != unit_id or signature.get('asset_id') != asset_id:
                return 'quality_manifest_owner_mismatch'
            if asset.get('quality_signature') != signature or asset.get('kind') != signature.get('role'):
                return 'quality_manifest_geometry_or_role_changed'
            parts = signature.get('parts')
            if not isinstance(parts, list):
                raise AstraError('quality_receipt_malformed')
            expected_parts = [{**mapping(mapping(part).get('page')), 'bbox': mapping(part).get('bbox')} for part in sorted(parts, key=lambda part: mapping(part).get('order', 0))]
            if asset.get('parts') != expected_parts:
                return 'quality_manifest_parts_changed'
            relative = asset.get('output_file')
            if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
                return 'quality_manifest_asset_path_invalid'
            output = source_path(leaf / 'solutions', relative)
            if not output.is_relative_to((leaf / 'solutions').resolve()):
                return 'quality_manifest_asset_path_invalid'
            if check_file(files.get('solution_crop'), 'solution_crop') != output or asset.get('output_sha256') != mapping(files.get('solution_crop')).get('sha256'):
                return 'quality_manifest_crop_changed'
            solution = mapping(manifest.get('solution'))
            if check_file(files.get('solution_document'), 'solution_document') != source_path(leaf, solution.get('source_pdf')) or solution.get('source_sha256') != mapping(files.get('solution_document')).get('sha256'):
                return 'quality_manifest_solution_source_changed'
            source_role = signature.get('source_document', 'solution')
            expected_source = files.get('problem_pdf') if source_role == 'problem' else files.get('solution_document')
            if files.get('solution_pdf') != expected_source or asset.get('source_sha256') != mapping(expected_source).get('sha256') or asset.get('source_document', 'solution') != source_role:
                return 'quality_manifest_source_role_changed'
            if mapping(manifest.get('problem')).get('source_sha256') != mapping(files.get('problem_pdf')).get('sha256'):
                return 'quality_manifest_problem_source_changed'
            corroborating = target.get('corroborating')
            if corroborating is not None:
                detail_id = mapping(corroborating).get('asset_id')
                if not isinstance(detail_id, str) or detail_id not in receipts or mapping(mapping(receipts.get(detail_id)).get('target')).get('asset') != corroborating:
                    return 'quality_corroborating_target_changed'
        return None
    except AstraError as exc:
        return exc.code
    except (OSError, ValueError, TypeError, KeyError):
        return 'quality_final_decision_missing_or_malformed'


def stored_reason(document: JsonMap, item_root: Path | None = None) -> str | None:
    try:
        provenance = mapping(document.get('provenance'))
        selection = provenance.get('solution_selection')
        if isinstance(selection, dict) and selection.get('verification_receipt') is not None:
            if item_root is None:
                return 'quality_studio_item_root_missing'
            from .quality_studio import validate_receipt as validate_studio_receipt
            return validate_studio_receipt(document, item_root)
        snapshot = mapping(provenance.get('raw_snapshot'))
        link = mapping(snapshot.get('link_item'))
        origin = mapping(link.get('quality_origin'))
        leaf_value, unit_id = origin.get('leaf'), origin.get('unit_id')
        if not isinstance(leaf_value, str) or not isinstance(unit_id, str):
            return 'quality_import_origin_missing'
        leaf = Path(leaf_value)
        current = read_json(leaf / 'solutions/link_manifest.json')
        current_link = mapping(mapping(current.get('items')).get(unit_id))
        if origin.get('decision_sha256') != identity(current_link):
            return 'quality_current_import_decision_changed'
        reason = decision_reason(leaf, current, unit_id)
        if reason is not None:
            return reason
        links = provenance.get('solution_links')
        if not isinstance(links, list):
            return 'quality_import_links_malformed'
        ids = set(current_link.get('answer_ids') or []) | set(current_link.get('explanation_ids') or [])
        if any(mapping(entry).get('external_asset_id') not in ids for entry in links if mapping(entry).get('status') == 'verified'):
            return 'quality_import_asset_identity_changed'
        recorded_root = origin.get('item_root')
        if not isinstance(recorded_root, str):
            return 'quality_import_root_missing'
        current_root = item_root.resolve() if item_root is not None else Path(recorded_root).resolve()
        if current_root != Path(recorded_root).resolve():
            return 'quality_import_root_changed'
        local = mapping(origin.get('local_sources'))
        assets = mapping(document.get('resources')).get('assets')
        if not isinstance(assets, list):
            return 'quality_import_assets_malformed'
        for entry in links:
            saved = mapping(entry)
            if saved.get('status') != 'verified':
                continue
            external_id, source_id = saved.get('external_asset_id'), saved.get('source_asset_id')
            current_asset = mapping(mapping(current.get('assets')).get(str(external_id)))
            selected_assets = [mapping(asset) for asset in assets if mapping(asset).get('asset_id') == source_id]
            if len(selected_assets) != 1 or selected_assets[0].get('sha256') != current_asset.get('output_sha256'):
                return 'quality_import_crop_identity_changed'
            if saved.get('kind') != current_asset.get('kind'):
                return 'quality_import_role_changed'
            parts = current_asset.get('parts')
            expected_parts = [{**mapping(part), 'render_dpi': current_asset.get('dpi', 300),
                'bbox': {'unit': 'ratio', 'origin': 'top-left', **{
                    name: coordinate / 1000 for name, coordinate in
                    zip(('left', 'top', 'right', 'bottom'), mapping(part)['bbox'], strict=True)}}}
                for part in parts] if isinstance(parts, list) else []
            if saved.get('parts') != expected_parts:
                return 'quality_import_parts_changed'
            relative = selected_assets[0].get('path')
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or '..' in Path(relative).parts or ':' in relative:
                return 'quality_import_path_invalid'
            actual_path = source_path(current_root, relative)
            if not actual_path.is_relative_to(current_root) or check_file(local.get(str(source_id)), 'imported_source') != actual_path:
                return 'quality_import_path_changed'
            if mapping(local.get(str(source_id))).get('sha256') != selected_assets[0].get('sha256'):
                return 'quality_import_crop_identity_changed'
        return None
    except AstraError as exc:
        return exc.code
    except (OSError, ValueError, TypeError, KeyError):
        return 'quality_import_origin_missing_or_unavailable'


def bind_import_origin(document: JsonMap, leaf: Path, unit_id: str,
                       link: JsonMap, item_root: Path) -> None:
    provenance = mapping(document.get('provenance'))
    snapshot = mapping(provenance.get('raw_snapshot'))
    snapshot_link = mapping(snapshot.get('link_item'))
    links = provenance.get('solution_links')
    assets = mapping(document.get('resources')).get('assets')
    if not isinstance(links, list) or not isinstance(assets, list):
        raise AstraError('quality_import_links_malformed')
    selected = {mapping(entry).get('source_asset_id') for entry in links}
    local = {str(mapping(asset).get('asset_id')): {
                'path': str(item_root / str(mapping(asset).get('path'))),
                'sha256': mapping(asset).get('sha256')}
             for asset in assets if mapping(asset).get('asset_id') in selected}
    snapshot_link['quality_origin'] = {'leaf': str(leaf), 'unit_id': unit_id,
        'decision_sha256': identity(link), 'item_root': str(item_root.resolve()), 'local_sources': local}
