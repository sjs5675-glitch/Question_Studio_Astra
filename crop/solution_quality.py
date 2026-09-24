"""Adapt current Crop targets to the shared persisted quality boundary."""
from __future__ import annotations

from pathlib import Path

from astra_cli import AstraError
from astra_cli.contracts import JsonMap, json_object
from astra_cli import quality
from solution_link import LinkContext, ProblemItem, SolutionAsset


def target_for(leaf: Path, problem: ProblemItem, asset: SolutionAsset,
               rendered: SolutionAsset, corroborating: SolutionAsset | None = None,
               *, solution_path: Path, images: list[Path]) -> JsonMap:
    manifest = quality.manifest_input(leaf, problem.item_id)
    return {'manifest': manifest,
            'sources': {'problem_pdf': quality.file_record(quality.source_path(leaf, manifest.get('source_pdf'))),
                        'problem_crop': quality.file_record(quality.source_path(leaf, quality.mapping(manifest.get('item')).get('output_file'))),
                        'solution_document': quality.file_record(solution_path),
                        'inference_images': [quality.file_record(image) for image in images]},
            'problem': problem.signature(), 'asset': asset.signature(),
            'rendered_asset': rendered.signature(),
            'corroborating': corroborating.signature() if corroborating is not None else None}


def issue_pair(context: LinkContext, problem: ProblemItem, asset: SolutionAsset,
               rendered: SolutionAsset, images: list[Path], output: Path,
               template: Path, schema: Path, result: JsonMap,
               corroborating: SolutionAsset | None = None) -> JsonMap:
    correction = output.with_name(output.stem + '.correction.json')
    if correction.is_file() and json_object(correction.read_text(encoding='utf-8')).get('result') == result:
        output = correction
    normalized = json_object(output.read_text(encoding='utf-8'))
    if normalized.get('result') != result:
        raise AstraError('quality_current_result_mismatch')
    raw = output.with_name(output.stem + '.raw.json')
    invocation = json_object(raw.read_text(encoding='utf-8'))
    runtime = quality.mapping(quality.mapping(invocation.get('meta')).get('runtime_receipt'))
    meta = quality.mapping(normalized.get('meta'))
    prompt_sha = meta.get('prompt_sha256')
    stage = runtime.get('stage')
    if not isinstance(prompt_sha, str) or not isinstance(stage, str):
        raise AstraError('quality_runtime_provenance_missing')
    manifest = quality.manifest_input(context.run_dir, problem.item_id)
    source = quality.source_path(context.run_dir, manifest.get('source_pdf'))
    item = quality.mapping(manifest.get('item'))
    files: JsonMap = {
        'problem_pdf': quality.file_record(source),
        'problem_crop': quality.file_record(quality.source_path(context.run_dir, item.get('output_file'))),
        'solution_document': quality.file_record(context.solution_path),
        'solution_pdf': quality.file_record(source if asset.source_document == 'problem' else context.solution_path),
        'solution_crop': quality.file_record(images[1]),
        'raw_result': quality.file_record(output),
        'inference_images': [quality.file_record(image) for image in images],
    }
    target = target_for(context.run_dir, problem, asset, rendered, corroborating, solution_path=context.solution_path, images=images)
    return quality.make_receipt(leaf=context.run_dir, target=target, files=files,
                                template=template, schema=schema, prompt_sha256=prompt_sha,
                                stage=stage, runtime=runtime, result=result, raw=raw)
