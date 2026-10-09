"""Bounded, read-only batch progress projection. Never return raw worker text."""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Mapping

_TRIAGE = re.compile(r'\[triage (\d{1,9})/(\d{1,9})\] batch (\d{1,9}): ok')
_ANALYSIS = re.compile(r'\[(\d{1,9})/(\d{1,9})\] p\.(\d{1,9}): (ok|failed)\s+completed=(\d{1,9}) total=(\d{1,9}) failed=(\d{1,9})')
_TRIAGE_HEAD = re.compile(r'저비용 페이지 분류: (\d{1,9})쪽 / (\d{1,9})개 접촉시트')
_DETECT_HEAD = re.compile(r'Codex 검출: .*\((\d{1,9})쪽, workers=\d{1,3}\)')
_REPAIR_HEAD = re.compile(r'경계 교정 재사용 (\d{1,9})개 · 실행 ([\d, ]*)')
_CROPPED = re.compile(r'크롭 (\d{1,9})개, 검토 항목 (\d{1,9})개')
_VERIFY_HEAD = re.compile(r'VLM 크롭 검수: (\d{1,9})개')
_VERIFY = re.compile(r'\[verify (\d{1,9})/(\d{1,9})\] \S+: (pass|review|fail|cached|error)')

# Crop-engine progress (0-100) per phase: (start, end). Used to move the bar
# inside long phases instead of only at phase boundaries.
_PHASE_SPAN = {'triage': (6, 15), 'detect': (15, 50), 'repair': (50, 58),
               'crop': (58, 60), 'verify': (60, 92), 'repair2': (92, 95), 'verify2': (95, 99)}
_PHASE_LABEL = {'triage': '페이지 분류', 'detect': '문항 검출', 'repair': '경계 교정',
                'crop': '문항 이미지 크롭', 'verify': '크롭 검수', 'repair2': '검수 후 경계 교정',
                'verify2': '교정 문항 재검수'}


def crop_log_progress(lines: Any) -> dict[str, Any] | None:
    """Parse the crop worker log into the current phase and its exact counts.

    Returns None when no known phase line was seen. ``fraction`` is the
    crop-engine-wide progress (0-100) interpolated inside the current phase.
    """

    if not isinstance(lines, (list, tuple)):
        return None
    state: dict[str, Any] | None = None
    history: dict[str, dict[str, Any]] = {}
    repairs = verifies = 0
    for raw in lines:
        if not isinstance(raw, str):
            continue
        line = raw.strip()
        previous = state
        if match := _TRIAGE_HEAD.search(line):
            state = {'phase': 'triage', 'done': 0, 'total': int(match.group(2)), 'unit': '묶음'}
        elif match := _DETECT_HEAD.search(line):
            state = {'phase': 'detect', 'done': 0, 'total': int(match.group(1)), 'failed': 0, 'unit': '쪽'}
        elif match := _REPAIR_HEAD.search(line):
            repairs += 1
            pages = [part for part in match.group(2).replace(' ', '').split(',') if part]
            state = {'phase': 'repair' if repairs == 1 else 'repair2', 'done': 0,
                     'total': len(pages) or None, 'failed': 0, 'unit': '쪽'}
        elif match := _CROPPED.search(line):
            state = {'phase': 'crop', 'done': int(match.group(1)), 'total': int(match.group(1)),
                     'unit': '개', 'review_queue': int(match.group(2))}
        elif match := _VERIFY_HEAD.search(line):
            verifies += 1
            state = {'phase': 'verify' if verifies == 1 else 'verify2', 'done': 0,
                     'total': int(match.group(1)), 'unit': '문항',
                     'pass': 0, 'review': 0, 'fail': 0}
        elif state is None:
            continue
        elif state['phase'] == 'triage' and (match := _TRIAGE.search(line)):
            state['done'] = max(state['done'], int(match.group(1)))
        elif state['phase'] in ('detect', 'repair', 'repair2') and (match := _ANALYSIS.search(line)):
            state['done'] = max(state['done'], int(match.group(5)))
            state['total'] = int(match.group(6))
            state['failed'] = int(match.group(7))
        elif state['phase'] in ('verify', 'verify2') and (match := _VERIFY.search(line)):
            state['done'] = max(state['done'], int(match.group(1)))
            verdict = match.group(3)
            if verdict in ('pass', 'review', 'fail'):
                state[verdict] += 1
        if state is not previous and state is not None:
            history[state['phase']] = state
    if state is None:
        return None
    state['history'] = history
    start, end = _PHASE_SPAN[state['phase']]
    total = state.get('total')
    ratio = min(1.0, state['done'] / total) if isinstance(total, int) and total > 0 else 0.0
    state['label'] = _PHASE_LABEL[state['phase']]
    state['fraction'] = start + round((end - start) * ratio)
    return state


_STAGES = {'crop': '문항 추출', 'queued': '대기', 'starting_crop': '추출 준비',
           'polling_crop': '문항 추출', 'importing': '문항 등록', 'ocr': '문제·해설 전사',
           'solution': '해설 연결', 'solution_retry': '해설 연결',
           'solution_transcription': '해설 전사', 'solution_verification': '해설 대조',
           'solution_linking': '해설 연결', 'figure_generation': '그림 생성',
           'completed': '전체 완료', 'failed': '작업 실패', 'interrupted': '작업 중단',
           'cancelled': '작업 취소', '페이지 분류': '페이지 분류', 'Codex 의미 분석': 'Codex 의미 분석',
           '경계 교정': '경계 교정', '문항 이미지 크롭': '문항 이미지 크롭', '크롭 검수': '크롭 검수'}


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _summary_metrics(record: Mapping[str, Any], crop: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    summary = _mapping(record.get('summary'))
    metrics = []

    def add(key, label, value, total=None, unit='문항'):
        done = _count(value)
        if done is None:
            return
        total = _count(total)
        metrics.append({'key': key, 'label': label, 'done': done,
                        'total': total if total is None or done <= total else None, 'unit': unit})

    stored_crop = _mapping(summary.get('crop'))
    live_crop = _mapping(_mapping(crop).get('summary'))

    def crop_source(field: str) -> Mapping[str, Any]:
        if record.get('state') == 'polling_crop' and field in live_crop:
            return live_crop
        return stored_crop if field in stored_crop else live_crop

    for field, label in [('total', '추출 문항'), ('passed', '추출 통과'), ('review', '추출 기록'),
                         ('failed', '추출 실패'), ('skipped', '추출 건너뜀')]:
        source = crop_source(field)
        add('crop_' + field, label, source.get(field),
            None if field == 'total' else source.get('total'))
    add('registered', '등록 문항', stored_crop.get('registered', summary.get('registered')))
    for field, label in [('pages', '추출 처리 페이지'), ('crops', '생성 크롭')]:
        add('crop_' + field, label, crop_source(field).get(field), unit='쪽' if field == 'pages' else '개')
    for field, label in [('imported', '가져온 문항')]:
        add(field, label, summary.get(field))
    ocr = _mapping(summary.get('ocr'))
    for field, label in [('attempted', '전사 시도'), ('succeeded', '전사 성공'), ('failed', '전사 실패'),
                         ('question_succeeded', '문제 전사 성공'), ('question_failed', '문제 전사 실패'),
                         ('explanation_succeeded', '해설 전사 성공'), ('explanation_failed', '해설 전사 실패')]:
        add('ocr_' + field, label, ocr.get(field), ocr.get('total'), unit='전사 건')
    transcription = _mapping(summary.get('solution_transcription'))
    for field, label in [('total', '해설 전사 대상'), ('applied', '해설 전사 반영'),
                         ('skipped_current', '기존 해설 유지'), ('failed', '해설 전사 실패'), ('no_source', '해설 미연결')]:
        add('solution_transcription_' + field, label, transcription.get(field),
            None if field == 'total' else transcription.get('total'))
    counts = _mapping(summary.get('content_counts'))
    add('content_error_count', '저장 내용 불러오기 실패', summary.get('content_error_count'))
    for field, label in [('question_only', '문제만'), ('question_answer', '문제+정답'),
                         ('question_answer_explanation', '문제+정답+해설'),
                         ('question_explanation_without_answer', '문제+해설(정답 없음)'),
                         ('question_missing', '문제 없음')]:
        add('content_' + field, label, counts.get(field))
    connections = _mapping(summary.get('solution_connections'))
    for field, label in [('answers', '연결된 정답'), ('explanations', '연결된 해설'), ('none', '연결 없음')]:
        add('solution_connections_' + field, label, connections.get(field))
    return metrics


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', value):
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def _live_crop_metrics(live: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    label, phase = live['label'], live['phase']
    metric = {'key': 'live_' + phase, 'label': f'현재 단계 · {label}', 'done': live['done'],
              'total': live.get('total'), 'unit': live.get('unit', '건')}
    if live.get('failed'):
        metric['failed'] = live['failed']
    metrics = [metric]
    text = f"{label} {live['done']}/{live.get('total') if live.get('total') is not None else '?'}{live.get('unit', '')}"
    if phase in ('verify', 'verify2'):
        for verdict, name in (('pass', '통과'), ('fail', '실패')):
            metrics.append({'key': f'live_{phase}_{verdict}', 'label': f'{label} {name}',
                            'done': live[verdict], 'total': live.get('total'), 'unit': '문항'})
        text += f" · 통과 {live['pass']} · 실패 {live['fail']}"
    return metrics, [{'text': text}]


def _solution_metrics(counts: Mapping[str, Any]) -> list[dict[str, Any]]:
    labels = (('problem_hints', '해설 위치 단서 문항', '문항'), ('solution_pages', '해설 블록 검출 쪽', '쪽'),
              ('verifications', '문제·해설 대조 기록', '건'), ('crops', '해설 크롭', '개'))
    return [{'key': 'solution_' + key, 'label': label, 'done': counts[key], 'total': None, 'unit': unit}
            for key, label, unit in labels if _count(counts.get(key)) is not None]


_FAILED_STATES = {'failed', 'interrupted', 'cancelled'}


def _amount(done: Any, total: Any, unit: str) -> str:
    done, total = _count(done), _count(total)
    if done is None:
        return ''
    return f'{done}/{total}{unit}' if total is not None else f'{done}{unit}'


def build_flow(record: Mapping[str, Any], crop: Mapping[str, Any] | None,
               live: Mapping[str, Any] | None,
               solution_counts: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Ordered pipeline steps with done/current/pending/failed status and a short detail."""

    crop = _mapping(crop)
    request = _mapping(record.get('request'))
    summary = _mapping(record.get('summary'))
    history = _mapping(_mapping(live).get('history'))
    has_solution = bool(request.get('solution_name') or request.get('solution_upload')
                        or request.get('solution_path') or crop.get('solution'))
    steps: list[dict[str, Any]] = [
        {'key': 'triage', 'label': '페이지 분류'},
        {'key': 'detect', 'label': '문항 검출'},
        {'key': 'repair', 'label': '경계 교정'},
        {'key': 'verify', 'label': '크롭 검수'},
    ]
    if has_solution:
        steps.append({'key': 'solution', 'label': '해설 연결'})
    steps.append({'key': 'import', 'label': '문항 등록'})
    steps.append({'key': 'ocr', 'label': '문제·해설 전사'})
    if request.get('regenerate_figures') is True:
        steps.append({'key': 'figures', 'label': '그림 생성'})
    keys = [step['key'] for step in steps]

    state = record.get('state')
    stage = record.get('stage')
    current: str | None
    if state == 'completed':
        current = None
    elif state in ('queued', 'starting_crop'):
        current = 'triage'
    elif state == 'polling_crop' and stage in ('solution', 'solution_retry'):
        current = 'solution'
    elif state == 'polling_crop':
        phase = _mapping(live).get('phase')
        current = {'triage': 'triage', 'detect': 'detect', 'repair': 'repair'}.get(phase, 'verify' if phase else 'triage')
    elif state == 'importing':
        current = 'import'
    elif state in ('ocr', 'solution_transcription', 'solution_verification', 'solution_linking'):
        current = 'ocr'
    elif state == 'figure_generation':
        current = 'figures' if 'figures' in keys else 'ocr'
    elif state in _FAILED_STATES:
        ocr = _mapping(summary.get('ocr'))
        error_code = str(record.get('error_code') or '')
        if summary.get('figures') and 'figures' in keys and stage == 'figure_generation':
            current = 'figures'
        elif (ocr.get('total') is not None and not error_code.startswith('solution')) or stage == 'ocr':
            current = 'ocr'
        elif _mapping(summary.get('crop')).get('registered') is not None or stage == 'importing':
            current = 'import'
        elif stage in ('solution', 'solution_retry') or error_code.startswith('solution'):
            current = 'solution' if 'solution' in keys else 'verify'
        else:
            current = 'verify' if history.get('verify') else 'detect' if history.get('detect') else 'triage'
    else:
        current = None
    current_index = keys.index(current) if current in keys else len(keys)

    # Step details
    triage, detect = _mapping(history.get('triage')), _mapping(history.get('detect'))
    repair = _mapping(history.get('repair2') or history.get('repair'))
    verify = _mapping(history.get('verify2') or history.get('verify'))
    details: dict[str, str] = {}
    if triage:
        details['triage'] = _amount(triage.get('done'), triage.get('total'), '묶음')
    if detect:
        details['detect'] = _amount(detect.get('done'), detect.get('total'), '쪽') + (
            f" · 실패 {detect['failed']}쪽" if detect.get('failed') else '')
    if repair:
        details['repair'] = _amount(repair.get('done'), repair.get('total'), '쪽')
        if history.get('repair2') and history.get('repair'):
            details['repair'] = f"1차 {_amount(history['repair'].get('total'), None, '쪽')} · 2차 {details['repair']}"
    if verify:
        text = _amount(verify.get('done'), verify.get('total'), '문항')
        if history.get('verify2'):
            text = '재검수 ' + text
        details['verify'] = f"{text} · 통과 {verify.get('pass', 0)} · 실패 {verify.get('fail', 0)}"
    crop_summary = _mapping(crop.get('summary')) or _mapping(summary.get('crop'))
    if current_index > keys.index('verify') and crop_summary.get('crops') is not None and 'verify' not in details:
        details['verify'] = f"크롭 {crop_summary.get('crops')}개 · 통과 {crop_summary.get('passed', 0)} · 실패 {crop_summary.get('failed', 0)}"
    if 'solution' in keys:
        connections = _mapping(summary.get('solution_connections')) or _mapping(solution_counts)
        if all(_count(connections.get(key)) is not None for key in ('answers', 'explanations', 'none')):
            details['solution'] = f"연결된 정답 {connections['answers']} · 해설 {connections['explanations']} · 없음 {connections['none']}"
        elif solution_counts:
            details['solution'] = f"대조 {solution_counts.get('verifications', 0)}건 · 해설 크롭 {solution_counts.get('crops', 0)}개"
    imported = _mapping(summary.get('crop')).get('registered', summary.get('registered', summary.get('imported')))
    if _count(imported) is not None:
        details['import'] = f'{imported}문항'
    ocr = _mapping(summary.get('ocr'))
    transcription = _mapping(summary.get('solution_transcription'))
    targets = record.get('solution_targets')
    solution_total = solution_done = solution_success = solution_failed = 0
    if isinstance(targets, list) and targets:
        terminal = {'applied', 'skipped_current', 'generated_pending_apply',
                    'review_budget_exhausted', 'failed', 'no_source', 'cancelled'}
        solution_total = len(targets)
        solution_done = sum(_mapping(target).get('state') in terminal for target in targets)
        solution_success = sum(_mapping(target).get('state') in {'applied', 'skipped_current'} for target in targets)
        solution_failed = sum(_mapping(target).get('state') == 'failed' for target in targets)
    elif _count(transcription.get('total')) is not None:
        solution_total = transcription['total']
        solution_success = int(transcription.get('applied') or 0) + int(transcription.get('skipped_current') or 0)
        solution_failed = int(transcription.get('failed') or 0)
        solution_done = solution_success + solution_failed + sum(int(transcription.get(key) or 0)
            for key in ('no_source', 'cancelled', 'generated_pending_apply', 'review_budget_exhausted'))
    if solution_total or (_count(ocr.get('succeeded')) is not None and (ocr.get('total') or ocr.get('attempted'))):
        # Ordinary OCR and solution targets are separate durable work units; retries are not new targets.
        success = int(ocr.get('succeeded') or 0) + solution_success
        failures = int(ocr.get('failed') or 0) + solution_failed
        finished = int(ocr.get('succeeded') or 0) + int(ocr.get('failed') or 0) + solution_done
        total = (int(ocr['total']) + solution_total) if _count(ocr.get('total')) is not None else (solution_total or None)
        details['ocr'] = _amount(finished, total, '건') + f" · 성공 {success} · 실패 {failures}"
    figures = _mapping(summary.get('figures'))
    if 'figures' in keys and _count(figures.get('attempted')) is not None:
        details['figures'] = _amount(figures.get('attempted'), figures.get('eligible'), '개')

    failed = state in _FAILED_STATES
    flow = []
    for index, step in enumerate(steps):
        status = 'done' if index < current_index else 'pending'
        if index == current_index:
            status = 'failed' if failed else 'current'
        detail = details.get(step['key'], '') if status in ('done', 'current') else ''
        if status == 'failed':
            detail = {'interrupted': '앱 재시작으로 중단', 'cancelled': '취소됨'}.get(str(state), '이 단계에서 실패')
        flow.append({'key': step['key'], 'label': step['label'], 'status': status, 'detail': detail})
    return flow


def build_progress_detail(record: Mapping[str, Any], crop: Mapping[str, Any] | None = None,
                          *, observed_at: str | None = None, log_lines: Any = None,
                          solution_counts: Mapping[str, Any] | None = None) -> dict[str, Any]:
    crop = _mapping(crop)
    # A completed problem crop may still be linking solutions upstream.
    stage = (crop.get('stage') if record.get('state') == 'polling_crop' and crop
             and record.get('stage') not in ('solution', 'solution_retry')
             else record.get('stage'))
    stage = stage if isinstance(stage, str) else ''
    metrics = []
    events = []
    logs = crop.get('logs', []) if crop else []
    if stage == '페이지 분류' and isinstance(logs, list):
        for line in logs[-64:]:
            match = _TRIAGE.fullmatch(line.strip()) if isinstance(line, str) else None
            if match:
                done, total, batch = map(int, match.groups())
                if done == batch and done <= total:
                    metrics = [{'key': 'triage_batches', 'label': '페이지 분류 접촉시트',
                                'done': done, 'total': total, 'unit': '접촉시트 묶음'}]
                    events.append({'text': f'페이지 분류 접촉시트 {done}/{total}묶음 완료'})
    if stage == 'Codex 의미 분석' and isinstance(logs, list):
        for line in logs[-64:]:
            match = _ANALYSIS.fullmatch(line.strip()) if isinstance(line, str) else None
            if not match:
                continue
            index, header_total, page, status, done, total, failed = match.groups()
            index, header_total, page, done, total, failed = map(int, (index, header_total, page, done, total, failed))
            if header_total != total or index > total or done > total or failed > done or page < 1:
                continue
            metrics = [{'key': 'analysis_pages', 'label': '의미 분석 페이지', 'done': done,
                        'total': total, 'unit': '쪽', 'failed': failed}]
            outcome = '완료' if status == 'ok' else '실패'
            events.append({'text': f'{page}쪽 의미 분석 {outcome} · 처리 {done}/{total}쪽 · 실패 {failed}쪽'})
    live = crop_log_progress(log_lines) if record.get('state') == 'polling_crop' else None
    in_solution = record.get('stage') in ('solution', 'solution_retry')
    if live is not None and not in_solution:
        live_metrics, live_events = _live_crop_metrics(live)
        metrics = live_metrics + [m for m in metrics if m['key'] not in ('analysis_pages', 'triage_batches')]
        events = live_events + events
        stage = live['label']
    if in_solution and solution_counts:
        metrics = _solution_metrics(solution_counts) + metrics
        events.append({'text': f"원본 대조 {solution_counts.get('verifications', 0)}건 · 해설 크롭 {solution_counts.get('crops', 0)}개"})
    metrics.extend(m for m in _summary_metrics(record, crop) if m['done'] or m.get('total'))
    updated_at = _timestamp(record.get('updated_at'))
    if record.get('state') == 'polling_crop':
        updated_at = _timestamp(crop.get('updated_at')) or updated_at
    stage_label = _STAGES.get(stage, stage if stage in _PHASE_LABEL.values() else '진행 정보 확인')
    if stage == 'completed' and record.get('state') != 'completed':
        stage_label = '문항 추출 완료'
    if record.get('state') == 'completed':
        stage_label = '전체 완료'
    flow_live = live if live is not None else crop_log_progress(log_lines)
    return {'version': 1, 'updated_at': updated_at,
            'flow': build_flow(record, crop, flow_live, solution_counts),
            'observed_at': _timestamp(observed_at) or datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'stage_label': stage_label, 'metrics': metrics, 'recent_events': events[-8:],
            'availability': 'ready' if crop and metrics else 'partial',
            'notice': (('일부 상세 정보가 없어 저장된 Studio 집계를 표시합니다. ' if not crop else '')
                       + '기록 시각(updated_at)과 조회 시각(observed_at)은 다릅니다. '
                       '단계 완료는 전체 완료를 뜻하지 않습니다.') }
