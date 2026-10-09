"""User-triggered blind reviews. Immutable job-owned ledger; canonical files are read-only.

Queue snapshots retain the selected version. A reviewer receives only the typed
question and verified image copies in a dedicated cwd. Answers are compared after
the call, and results never approve, correct, or rewrite a canonical package.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Mapping

from . import item_generation as ig
from .item_generation_identity import (digest, safe_path, image_bytes, image_sandbox,
    draft_identity, contributors, independence, model_family, receipt_model)
from .item_registry import item_code
from .model import load_json_object, utc_now, _path_lock, validate_id, ModelError
from .trash import Trash

MODELS = {'claude': 'claude-opus-5-5', 'codex': 'gpt-6-astra'}


def canonical_path(workspace: Path, draft: Mapping) -> Path | None:
    if not draft.get('confirmed_item_id'):
        return None
    project = validate_id(draft.get('confirmed_project_id'), field='project_id')
    item = validate_id(draft['confirmed_item_id'], field='item_id')
    return safe_path(workspace, f'{project}/items/{item}/question.json')


def _walk(value):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def canonical_answer(package: Mapping) -> str:
    answer = (package.get('content') or {}).get('answer')
    if not isinstance(answer, Mapping):
        raise ig.ItemGenerationError('canonical_answer_missing')
    if answer.get('kind') == 'choice':
        ids = answer.get('choice_ids') or []
        if len(ids) != 1:
            raise ig.ItemGenerationError('canonical_answer_ambiguous')
        for block in _walk((package.get('content') or {}).get('question')):
            if block.get('type') == 'choices':
                for row in block.get('items') or []:
                    if row.get('choice_id') == ids[0] and row.get('label'):
                        return str(row['label'])
        raise ig.ItemGenerationError('canonical_choice_missing')
    if answer.get('kind') == 'content':
        text = ig._blocks_text(answer.get('blocks'))
        if text.strip():
            return text
    raise ig.ItemGenerationError('canonical_answer_missing')


def _validate_visible_blocks(blocks) -> None:
    """Reject content the current typed renderer cannot show, rather than omit it."""
    if not isinstance(blocks, list):
        raise ig.ItemGenerationError('review_content_unrenderable')
    for block in blocks:
        if not isinstance(block, Mapping) or block.get('type') not in {'paragraph', 'figure', 'table', 'bogi', 'choices', 'box'}:
            raise ig.ItemGenerationError('review_content_unrenderable')
        kind = block['type']
        if kind == 'paragraph':
            for inline in block.get('inlines') or []:
                if not isinstance(inline, Mapping) or inline.get('type') not in {'text', 'equation', 'line_break'}:
                    raise ig.ItemGenerationError('review_content_unrenderable')
                if inline['type'] == 'equation' and not (inline.get('latex') or inline.get('source_text')):
                    raise ig.ItemGenerationError('review_equation_unrenderable')
        elif kind == 'box':
            _validate_visible_blocks(block.get('blocks'))
        elif kind in {'bogi', 'choices'}:
            for item in block.get('items') or []:
                _validate_visible_blocks(item.get('blocks'))
        elif kind == 'table':
            for row in block.get('rows') or []:
                for cell in row.get('cells') or []:
                    _validate_visible_blocks(cell.get('blocks'))


def snapshot(workspace: Path | str, job: Mapping, draft: Mapping) -> dict:
    """Snapshot current typed content, answer and assets; return bytes only to the parent."""
    from .item_generation_pipeline import problem_text
    workspace = Path(workspace).resolve()
    scope = ig.load_scope(job['request']['scope'])
    unit = ig.resolve_unit(job['request']['scope'], job['request']['major'], job['request']['middle'])
    context = {'scope': job['request']['scope'], 'curriculum': scope.get('curriculum'),
        'allowed_concepts': unit['concepts'], 'excluded_notes': scope.get('excluded_notes') or [],
        'major': unit['major'], 'middle': unit['middle']}
    images, asset_refs = [], []
    path = canonical_path(workspace, draft)
    if path:
        Trash(workspace).require_active(draft['confirmed_project_id'], draft['confirmed_item_id'])
        data = path.read_bytes()
        package = json.loads(data.decode('utf-8-sig'))
        if package.get('item_id') != draft['confirmed_item_id']:
            raise ig.ItemGenerationError('canonical_identity_mismatch')
        generation = (package.get('provenance') or {}).get('generation') or {}
        if generation.get('job_id') != job['job_id'] or generation.get('draft_no') != draft['no']:
            raise ig.ItemGenerationError('canonical_generation_mismatch')
        question = copy.deepcopy((package.get('content') or {}).get('question') or {})
        if not question.get('blocks'):
            raise ig.ItemGenerationError('canonical_question_missing')
        _validate_visible_blocks(question['blocks'])
        assets = {a['asset_id']: a for a in (package.get('resources') or {}).get('assets') or [] if isinstance(a, Mapping)}
        for block in _walk(question):
            if block.get('type') == 'figure':
                selected = block.get('asset_id')  # matches the actual reading renderer
                asset = assets.get(selected)
                if not asset or asset.get('section') != 'question' or asset.get('state') != 'ready':
                    raise ig.ItemGenerationError('review_figure_not_ready')
                image, sha = image_bytes(path.parent, asset.get('path') or '', asset.get('sha256'))
                images.append(image)
                asset_refs.append({'sha256': sha, 'asset_id': selected})
        # Figure alt metadata is not the visible problem text and can contain a
        # revealing source filename. Review the actual pixels, never that hint.
        visible = copy.deepcopy(question)
        for block in _walk(visible):
            if block.get('type') == 'figure':
                block['alt_text'] = '첨부 이미지'
        problem = f"### 문항 {draft['no']}\n" + ig.blocks_text_for_prompt(visible['blocks'])
        answer = canonical_answer(package)
        author = generation.get('generator') or {}
        authors = list(author.get('contributors') or contributors(draft))
        if author.get('model_confirmed') is False:
            authors = ['unknown']
        if draft.get('confirmed_content_sha256') and (digest(question) != draft['confirmed_content_sha256']
                or digest((package.get('content') or {}).get('answer')) != draft.get('confirmed_answer_sha256')):
            # Canonical edits have their own workflow revision. Without a new
            # authorship receipt, keep the original contributors and explicitly
            # retain the unknown editing contribution rather than forge an AI author.
            authors = sorted(set(authors + ['unknown']))
        solution = ig.blocks_text_for_prompt(((package.get('content') or {}).get('explanation') or {}).get('blocks'))
        identity = {'source_revision': (package.get('workflow') or {}).get('revision'),
            'source_sha256': hashlib.sha256(data).hexdigest(), 'content_sha256': digest(question),
            'answer_sha256': digest((package.get('content') or {}).get('answer')),
            'assets_sha256': digest(asset_refs), 'scope_sha256': digest(context)}
    else:
        base = ig.job_dir(workspace, job['job_id'])
        identity = draft_identity(draft, base, allow_legacy=True)
        identity.update(source_sha256=digest({k: draft.get(k) for k in ('stem', 'instruction', 'table', 'bogi', 'choices',
            'answer', 'solution', 'figure', 'contributors', 'generator_model', 'model_confirmed', 'content_revision')}
            | {'figure': {k: (draft.get('figure') or {}).get(k) for k in ('file', 'sha256', 'spec', 'alt_text', 'method')}}),
            scope_sha256=digest(context))
        f = draft.get('figure')
        if isinstance(f, Mapping):
            if not f.get('file'):
                raise ig.ItemGenerationError('review_figure_missing')
            # Bind the second read to this snapshot's first observation, also
            # catching replacement between identity computation and pixel copy.
            image, sha = image_bytes(base, f['file'], identity['asset_shas'][0])
            images.append(image)
            asset_refs.append({'sha256': sha, 'observation':
                'stored-sha256' if f.get('sha256') else 'legacy-first-observation'})
        problem = problem_text(draft)
        answer = draft.get('answer')
        authors = contributors(draft)
        solution = str(draft.get('solution') or '')
    if not isinstance(answer, str) or not answer.strip():
        raise ig.ItemGenerationError('review_answer_missing')
    return {'no': draft['no'], 'job_id': job['job_id'], 'item_code': draft.get('item_code') or item_code(job['job_id'], draft['no']),
        'item_id': draft.get('confirmed_item_id'), 'project_id': draft.get('confirmed_project_id'),
        'identity': identity, 'context': context, 'authors': authors, 'problem': problem,
        'answer': answer, 'solution': solution, 'images': images, 'asset_refs': asset_refs}


def records(workspace: Path | str, job_id: str) -> list[dict]:
    base = ig.job_dir(workspace, job_id)
    return [load_json_object(safe_path(base, str(p.relative_to(base))))
            for p in sorted((base / 'reviews').glob('*.json'))]


def enqueue(workspace: Path | str, job_id: str, numbers: list[int], model: str, expected_revision: int) -> dict:
    from .item_generation_pipeline import check_expected
    if not isinstance(model, str) or model not in MODELS:
        raise ig.ItemGenerationError('review_model_invalid')
    if type(expected_revision) is not int or expected_revision < 1:
        raise ig.ItemGenerationError('expected_revision_required')
    if not isinstance(numbers, list) or not numbers or any(type(n) is not int or n < 1 for n in numbers) or len(set(numbers)) != len(numbers):
        raise ig.ItemGenerationError('review_numbers_invalid')
    workspace = Path(workspace).resolve()
    def change(job):
        check_expected(job, expected_revision)
        if job.get('status') in {'queued', 'running', 'paused'} or (job.get('work_action') == 'review'
                and (job.get('review_request') or {}).get('status') in {'queued', 'running', 'paused'}):
            raise ig.ItemGenerationConflict('이미 진행 중인 검토 또는 작업입니다. 남은 검토를 재개하세요.')
        by_no = {d['no']: d for d in job.get('drafts') or []}
        if any(n not in by_no or by_no[n].get('discarded') for n in numbers):
            raise ig.ItemGenerationError('review_draft_invalid')
        request_id = uuid.uuid4().hex
        selected = []
        for no in numbers:
            snap = snapshot(workspace, job, by_no[no])
            copies = []
            for i, data in enumerate(snap.pop('images'), 1):
                relative = f'review-inputs/{request_id}/{no}/image-{i:02d}.png'
                path = safe_path(ig.job_dir(workspace, job_id), relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('xb') as output:
                    output.write(data)
                copies.append({'path': relative, 'sha256': hashlib.sha256(data).hexdigest()})
            snap['image_copies'] = copies
            snap['entry_id'] = f'{request_id}-{no}'
            selected.append(snap)
        effort = ig.claude_effort_for_scope(job['scope']) if model == 'claude' else ig.codex_effort_for_scope(job['scope'])
        job['review_request'] = {'request_id': request_id, 'status': 'queued', 'model': model,
            'requested_model': MODELS[model], 'effort': effort, 'expected_revision': expected_revision,
            'at': utc_now(), 'selected': selected, 'completed_entry_ids': [], 'return_status': job.get('status', 'ready')}
        job['work_action'] = 'review'
        job['status'] = 'queued'
        job['progress'] = {'step': 'review', 'message': f'선택 {len(numbers)}문항 블라인드 검토 대기', 'at': utc_now()}
    return ig.update_job(workspace, job_id, change)


def _is_current(workspace: Path, job: Mapping, selected: Mapping) -> bool:
    target = next((d for d in job.get('drafts') or [] if d['no'] == selected['no']), None)
    if not target or target.get('discarded'):
        return False
    try:
        now = snapshot(workspace, job, target)
        return now['identity'] == selected['identity'] and now['item_id'] == selected.get('item_id')
    except (ig.ItemGenerationError, ModelError, OSError, ValueError):
        return False


def _append(workspace: Path, job_id: str, selected: Mapping, record: dict) -> dict:
    """Canonical CAS is a read check under its existing lock; never a canonical write."""
    with ig._LOCK:
        job = ig.load_job(workspace, job_id)
        target = next((d for d in job['drafts'] if d['no'] == selected['no']), None)
        path = canonical_path(workspace, target) if target else None
        def persist():
            same_request = any(s.get('entry_id') == record['entry_id'] for s in (job.get('review_request') or {}).get('selected') or [])
            record['stale'] = not same_request or not _is_current(workspace, ig.load_job(workspace, job_id), selected)
            record['at'] = utc_now()
            base = ig.job_dir(workspace, job_id) / 'reviews'
            base.mkdir(exist_ok=True)
            existing = records(workspace, job_id)
            if any(r['entry_id'] == record['entry_id'] for r in existing):
                return job
            record['sequence'] = len(existing) + 1
            # Unique immutable record, atomic serialization. No truncation or replacement.
            destination = safe_path(base, f"{record['sequence']:08d}-{record['entry_id']}.json")
            if destination.exists():
                raise ig.ItemGenerationConflict('review_record_exists')
            ig.atomic_write_json(destination, record)
            if not same_request:
                return job
            return ig.update_job(workspace, job_id, lambda j: j['review_request']['completed_entry_ids'].append(record['entry_id']))
        if path:
            with _path_lock(path):
                return persist()
        return persist()


def run_reviews(workspace: Path, job: Mapping, runners) -> dict:
    from .item_generation_pipeline import (VERIFY_PROMPT, ProviderQuotaExhausted, ProviderFailure,
        _result_map, normalize_answer)
    request = job.get('review_request') or {}
    if not request.get('selected'):
        raise ig.ItemGenerationError('review_request_missing')
    base = ig.job_dir(workspace, job['job_id'])
    for selected in request['selected']:
        # A crash between ledger append and job checkpoint never repeats a paid call.
        if selected['entry_id'] in {r['entry_id'] for r in records(workspace, job['job_id'])}:
            continue
        ig.update_job(workspace, job['job_id'], lambda j: j['review_request'].update(status='running'))
        record = {k: copy.deepcopy(selected.get(k)) for k in ('entry_id', 'no', 'job_id', 'item_code', 'item_id',
            'project_id', 'identity', 'context', 'authors', 'asset_refs')}
        record.update(requested_model=request['requested_model'], requested_effort=request['effort'],
                      actual_model=None, actual_effort=None, review_kind='unknown', status='blocked',
                      original_answer=selected['answer'], original_solution=selected.get('solution'), blind=True)
        if not _is_current(workspace, ig.load_job(workspace, job['job_id']), selected):
            record['error_category'] = 'selected_snapshot_stale'
            _append(workspace, job['job_id'], selected, record)
            continue
        try:
            usage = {}
            images = [image_bytes(base, c['path'], c['sha256'])[0] for c in selected.get('image_copies') or []]
            context = selected['context']
            grade, _, subject = context['scope'].partition('-')
            prompt = VERIFY_PROMPT.format(grade=grade, subject=subject, problems=selected['problem'])
            prompt += '\n교육과정·허용 범위·제외 범위: ' + json.dumps(context, ensure_ascii=False)
            with image_sandbox(base / '.scratch', images) as (sandbox, copies):
                if request['model'] == 'claude':
                    if copies:
                        prompt += '\nRead로 다음 이미지만 확인: ' + '\n'.join(str(p) for p in copies)
                    text, usage = runners.claude(prompt, effort=request['effort'], add_dirs=[sandbox])
                else:
                    text, usage = runners.codex(prompt, effort=request['effort'], images=copies, workdir=sandbox)
                result = _result_map(text, {selected['no']})[selected['no']]
            actual = receipt_model(usage)
            confirmed = usage.get('model_confirmed', bool(actual))
            kind = independence(selected['authors'], actual, confirmed=confirmed)
            record.update(actual_model=usage.get('actual_model'),
                cli_selected_model=usage.get('cli_selected_model'), cli_selected_effort=usage.get('cli_selected_effort'),
                model_evidence=usage.get('model_evidence'), model_confirmed=confirmed,
                actual_effort=usage.get('effort') or request['effort'], usage=usage,
                review_kind=kind, verifier_answer=result['answer'], verifier_reasoning=result['reasoning'],
                verifier_issue=result.get('issue'), verifier_bogi=result.get('bogi_judgement'),
                answer_agreement=normalize_answer(result['answer']) == normalize_answer(selected['answer']))
            if not confirmed or not actual:
                record.update(status='blocked', error_category='actual_model_unknown')
            elif actual != request['requested_model']:
                record.update(status='blocked', error_category='actual_model_mismatch', review_kind='unknown')
            else:
                record['status'] = 'item_issue' if result.get('issue') else 'agreement' if record['answer_agreement'] else 'conflict'
        except ProviderQuotaExhausted:
            # Intentionally no fallback: a requested Claude review must stay Claude.
            ig.update_job(workspace, job['job_id'], lambda j: j['review_request'].update(status='paused'))
            raise
        except Exception as exc:
            blocked = isinstance(exc, ProviderFailure) and exc.category in {
                'cli_receipt_unknown', 'cli_model_mismatch', 'cli_effort_mismatch', 'actual_model_mismatch'}
            if isinstance(exc, ProviderFailure):
                usage = exc.usage or usage
            record.update(status='blocked' if blocked else 'error', usage=usage,
                **{k: usage[k] for k in ('cli_selected_model', 'cli_selected_effort', 'model_evidence') if k in usage},
                error_category=exc.category if isinstance(exc, ProviderFailure)
                          else str(exc) if isinstance(exc, ig.ItemGenerationError) else 'review_failed')
        _append(workspace, job['job_id'], selected, record)
    def finish(j):
        if (j.get('review_request') or {}).get('request_id') != request.get('request_id'):
            return
        j['review_request']['status'] = 'done'
        j['status'] = request.get('return_status') if request.get('return_status') in {'ready', 'partial', 'failed'} else 'partial'
        j['error'] = None
        j['progress'] = {'step': 'review_done', 'message': '선택 문항 검토 완료. 항목별 판정과 이력을 확인하세요.', 'at': utc_now()}
    return ig.update_job(workspace, job['job_id'], finish)


def enrich_job(workspace: Path | str, job: Mapping) -> dict:
    """GET projection only. Historical review records and canonical bytes never change."""
    workspace = Path(workspace).resolve()
    view = copy.deepcopy(job)
    history = records(workspace, job['job_id'])
    for draft in view.get('drafts') or []:
        draft.setdefault('item_code', item_code(job['job_id'], draft['no']))
        entries = [copy.deepcopy(r) for r in history if r['no'] == draft['no']]
        for entry in entries:
            entry['current'] = not entry.get('stale') and _is_current(workspace, job, entry)
        draft['review_history'] = entries
        state = 'not_run'
        if entries:
            latest = entries[-1]
            state = ('stale' if not latest['current'] else latest['status'] if latest['status'] in {'blocked', 'error', 'conflict', 'item_issue'}
                else latest['review_kind'])
        draft['cross_review'] = {'status': state}
        path = canonical_path(workspace, draft)
        if path:
            try:
                current = snapshot(workspace, job, draft)
                package = load_json_object(path)
                draft['canonical_current'] = {'question_text': current['problem'], 'answer': current['answer'],
                    'solution': current['solution'], 'revision': current['identity']['source_revision'], 'authors': current['authors'],
                    'figures': [f"/api/projects/{draft['confirmed_project_id']}/items/{draft['confirmed_item_id']}/assets/{a['asset_id']}"
                                for a in current['asset_refs']]}
            except (ig.ItemGenerationError, ModelError, OSError, ValueError) as exc:
                draft['canonical_current'] = {'error': '현재 저장 문항을 읽을 수 없습니다.'}
        if not entries:
            v = draft.get('verification') or {}
            try:
                if path:
                    initial_current = bool(v.get('identity')) and not draft['canonical_current'].get('error') \
                        and current['identity']['source_revision'] == draft.get('confirmed_revision', 1) \
                        and current['identity']['content_sha256'] == draft.get('confirmed_content_sha256') \
                        and current['identity']['answer_sha256'] == draft.get('confirmed_answer_sha256') \
                        and [a['sha256'] for a in current['asset_refs']] == v['identity'].get('asset_shas', [])
                else:
                    initial_current = bool(v.get('identity')) and v['identity'] == draft_identity(draft, ig.job_dir(workspace, job['job_id']))
            except ig.ItemGenerationError:
                initial_current = False
            actual = receipt_model(v) or (v.get('model') if 'actual_model' not in v else None)
            authors = current['authors'] if path and not draft['canonical_current'].get('error') else contributors(draft)
            kind = independence(authors, actual, confirmed=v.get('model_confirmed', bool(actual)))
            if initial_current and not draft.get('edited_by_user') and v.get('status') == 'verified' and kind == 'cross_check':
                draft['cross_review'] = {'status': 'cross_check', 'source': 'initial_blind_validation'}
            elif kind == 'self_check':
                draft['cross_review']['initial_kind'] = 'self_check'
            if v.get('identity') and v.get('status') == 'verified' and not initial_current:
                draft['cross_review']['status'] = 'stale'
            if v.get('stale'):
                draft['cross_review']['status'] = 'stale'
        request = job.get('review_request') or {}
        selected = next((s for s in request.get('selected') or [] if s['no'] == draft['no']), None)
        if selected and request.get('status') in {'queued', 'running', 'paused'} \
                and selected['entry_id'] not in {r['entry_id'] for r in history}:
            draft['cross_review']['status'] = 'paused' if job.get('status') == 'paused' else request['status']
    view['projection_sha256'] = digest(view)
    return view


def canonical_review_state(workspace: Path | str, package: Mapping) -> dict | None:
    generation = (package.get('provenance') or {}).get('generation') or {}
    if not generation.get('job_id') or not generation.get('draft_no'):
        return None
    try:
        job = ig.load_job(workspace, generation['job_id'])
        view = enrich_job(workspace, job)
        draft = next(d for d in view['drafts'] if d['no'] == generation['draft_no'] and d.get('confirmed_item_id') == package.get('item_id'))
        return {'cross_review': draft['cross_review'], 'review_history': draft['review_history']}
    except (ig.ItemGenerationError, ModelError, OSError, StopIteration, ValueError):
        return {'cross_review': {'status': 'not_run'}, 'review_history': []}
