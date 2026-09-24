'use client';

/* eslint-disable @next/next/no-img-element -- local diagnostic images have unknown dimensions and stay on localhost */

import { ChangeEvent, DragEvent, FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';

const API = 'http://127.0.0.1:8765';

type Engine = { available: boolean; label: string; detail?: string; installed?: boolean; authenticated?: boolean };
type Health = { status: string; engines: Record<string, Engine> };
type Summary = { pages: number; crops: number; passed: number; review: number; failed: number };
type SolutionState = { state: string; progress: number; message: string; source?: string; source_name?: string; crop_dpi: number };
type SolutionAsset = { id: string; kind?: string; confidence?: number; pdf_pages: number[]; printed_pages: number[]; image_url: string };
type LinkRoleStatus = 'verified' | 'review' | 'unmatched' | 'conflict';
type OverallLinkStatus = 'complete' | 'partial' | 'review' | 'unmatched' | 'conflict';
type SolutionLinks = {
  status: 'verified' | 'review' | 'unmatched' | 'conflict'; answers: SolutionAsset[]; explanations: SolutionAsset[]; candidates: SolutionAsset[];
  answer_status?: LinkRoleStatus; explanation_status?: LinkRoleStatus; overall_status?: OverallLinkStatus;
  solution_printed_pages: number[]; confidence?: number; evidence: unknown[]; review_reasons: string[];
};
type Job = {
  id: string; source: string; source_name: string; state: string; stage: string; progress: number;
  message: string; created_at: string; updated_at: string; logs: string[]; summary: Summary; solution?: SolutionState;
};
type ResultItem = {
  id: string; number: string; kind: string; answer_type: string; source_pages: number[];
  confidence: number; verdict: 'pass' | 'review' | 'fail'; reasons: string[]; warnings?: string[];
  solutions?: SolutionLinks | null; image_url: string;
};
type ResultDocument = { name: string; items: ResultItem[]; overlays: string[]; ensemble?: { status?: string } };
type Results = { documents: ResultDocument[]; summary: Summary };

const stages = [
  ['01', '페이지 분류', '문제·개념·해설과 회전 방향을 찾습니다.'],
  ['02', '독립 분석', 'Codex와 Gemini가 서로 보지 않고 경계를 제안합니다.'],
  ['03', '합의·검수', '합의된 crop만 통과시키고 Qwen이 불일치를 재판정합니다.'],
];

const kindLabels: Record<string, string> = {
  question: '독립 문항', main_question: '대문항', subquestion: '소문항', shared_group_bundle: '공통자료 묶음',
};

function formatTime(value?: string) {
  if (!value) return '';
  return new Intl.DateTimeFormat('ko-KR', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
}

function formatReason(reason: string) {
  if (reason.includes('boundary_agreement:codex+gemini')) return 'Codex와 Gemini가 같은 문항 내용에 합의했습니다.';
  if (reason.includes('boundary_agreement:codex+qwen')) return 'Codex와 Qwen이 같은 문항 내용에 합의했습니다.';
  if (reason.includes('boundary_agreement:gemini+qwen')) return 'Gemini와 Qwen이 같은 문항 내용에 합의했습니다.';
  if (reason.includes('fewer_than_two_valid_proposals')) return '두 개 이상의 독립 AI 판정을 받지 못했습니다.';
  if (reason.includes('no_two_independent_proposals_agree')) return '두 AI가 같은 문항 경계에 합의하지 못했습니다.';
  if (reason.includes('requires_rebuild')) return 'Gemini와 Qwen은 합의했지만 기존 Codex crop을 다시 만들어야 합니다.';
  if (reason.includes('gemini_disabled')) return 'Gemini 재판정이 꺼져 있습니다.';
  if (reason.includes('qwen_error')) return 'QwenCloud 호출 상태를 확인해주세요.';
  if (reason.includes('qwen_disabled')) return 'Qwen 재판정이 꺼져 있습니다.';
  return reason;
}

function formatWarning(warning: string) {
  if (warning === 'cosmetic_fragment') return '사용에 영향 없는 장식·주변 조각';
  if (warning === 'model_abstention') return '보조 AI가 경계 판단을 유보함';
  return warning;
}

const linkLabels: Record<OverallLinkStatus, string> = {
  complete: '답·해설 완료', partial: '일부 연결', review: '연결 검토', unmatched: '연결 없음', conflict: '답 충돌',
};

function overallLinkStatus(links: SolutionLinks): OverallLinkStatus {
  if (links.overall_status) return links.overall_status;
  if (links.answers.length && links.explanations.length) return 'complete';
  if (links.answers.length || links.explanations.length) return 'partial';
  return links.status === 'verified' ? 'partial' : links.status;
}

function overallStatusClass(status: OverallLinkStatus) {
  return status === 'complete' ? 'verified' : status === 'partial' ? 'review' : status;
}

function solutionActionLabel(links: SolutionLinks) {
  if (links.answers.length && links.explanations.length) return '답·해설 보기';
  if (links.answers.length) return '답 보기';
  if (links.explanations.length) return '해설 보기';
  return '후보 검토';
}

export default function Home() {
  const [health, setHealth] = useState<Health | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [activeJob, setActiveJob] = useState<Job | null>(null);
  const [results, setResults] = useState<Results | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [solutionFile, setSolutionFile] = useState<File | null>(null);
  const [source, setSource] = useState('');
  const [pages, setPages] = useState('all');
  const [unit, setUnit] = useState('atomic');
  const [workers, setWorkers] = useState('2');
  const [cropDpi, setCropDpi] = useState<'300' | '450'>('300');
  const [ensemble, setEnsemble] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [attachingSolution, setAttachingSolution] = useState(false);
  const [error, setError] = useState('');
  const [filter, setFilter] = useState<'all' | 'pass' | 'review' | 'fail'>('all');
  const [view, setView] = useState<'crops' | 'overlays'>('crops');
  const [selectedImage, setSelectedImage] = useState<{ src: string; title: string } | null>(null);
  const [compareItem, setCompareItem] = useState<(ResultItem & { document: string; docIndex: number }) | null>(null);
  const [compareTab, setCompareTab] = useState<'question' | 'answer' | 'explanation'>('question');
  const [selectedAnswerId, setSelectedAnswerId] = useState('');
  const [selectedExplanationId, setSelectedExplanationId] = useState('');
  const [confirmingSolution, setConfirmingSolution] = useState(false);
  const latestJobRequest = useRef(0);

  const fetchHealth = useCallback(async () => {
    try {
      const response = await fetch(`${API}/api/health`);
      if (!response.ok) throw new Error('로컬 엔진 응답이 없습니다.');
      setHealth(await response.json() as Health);
    } catch {
      setHealth(null);
    }
  }, []);

  const fetchJobs = useCallback(async () => {
    try {
      const response = await fetch(`${API}/api/jobs`);
      if (!response.ok) return;
      const data = await response.json() as { jobs?: Job[] };
      setJobs(data.jobs ?? []);
    } catch { /* local API can be starting */ }
  }, []);

  const loadJob = useCallback(async (jobId: string) => {
    const requestId = ++latestJobRequest.current;
    setResults(null);
    try {
      const response = await fetch(`${API}/api/jobs/${jobId}`);
      if (!response.ok || requestId !== latestJobRequest.current) return;
      const job = await response.json() as Job;
      if (requestId !== latestJobRequest.current) return;
      setActiveJob(job);
      if (job.state === 'completed') {
        const resultResponse = await fetch(`${API}/api/jobs/${jobId}/results`);
        if (resultResponse.ok) {
          const nextResults = await resultResponse.json() as Results;
          if (requestId === latestJobRequest.current) setResults(nextResults);
        }
      }
    } catch { /* polling resumes when the local API is available */ }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void fetchHealth();
      void fetchJobs();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [fetchHealth, fetchJobs]);
  useEffect(() => {
    if (!activeJob) return;
    const mainBusy = ['queued', 'running'].includes(activeJob.state);
    const solutionBusy = ['queued', 'indexing', 'linking'].includes(activeJob.solution?.state ?? '');
    if (!mainBusy && !solutionBusy) return;
    const timer = window.setInterval(() => { loadJob(activeJob.id); fetchJobs(); }, 1800);
    return () => window.clearInterval(timer);
  }, [activeJob, loadJob, fetchJobs]);
  const allItems = useMemo(() => results?.documents.flatMap((document, docIndex) => document.items.map((item) => ({ ...item, document: document.name, docIndex }))) ?? [], [results]);
  const filteredItems = useMemo(() => allItems.filter((item) => filter === 'all' || item.verdict === filter), [allItems, filter]);
  const overlayItems = useMemo(() => results?.documents.flatMap((document) => document.overlays.map((src, index) => ({ src, title: `${document.name} · 오버레이 ${index + 1}` }))) ?? [], [results]);

  function selectPdf(nextFile: File | null) {
    if (nextFile && nextFile.type !== 'application/pdf' && !nextFile.name.toLowerCase().endsWith('.pdf')) {
      setError('PDF 파일만 선택할 수 있습니다.');
      return;
    }
    setError('');
    setFile(nextFile);
    if (nextFile) setSource('');
  }

  function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    selectPdf(event.target.files?.[0] ?? null);
  }

  function chooseSolutionFile(event: ChangeEvent<HTMLInputElement>) {
    const nextFile = event.target.files?.[0] ?? null;
    if (nextFile && nextFile.type !== 'application/pdf' && !nextFile.name.toLowerCase().endsWith('.pdf')) {
      setError('정답과해설도 PDF 파일만 선택할 수 있습니다.');
      return;
    }
    setError('');
    setSolutionFile(nextFile);
  }

  function dropFile(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    selectPdf(event.dataTransfer.files?.[0] ?? null);
  }

  async function uploadSelectedFile(selected: File) {
    const response = await fetch(`${API}/api/uploads?filename=${encodeURIComponent(selected.name)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/pdf' }, body: selected,
    });
    const data = await response.json() as { path?: string; error?: string };
    if (!response.ok) throw new Error(data.error || '파일 업로드에 실패했습니다.');
    if (!data.path) throw new Error('업로드 경로를 받지 못했습니다.');
    return data.path;
  }

  const geminiAvailable = Boolean(health?.engines.gemini?.available);
  const qwenAvailable = Boolean(health?.engines.qwen?.available);
  const ensembleAvailable = geminiAvailable && qwenAvailable;
  const ensembleEnabled = ensembleAvailable && ensemble;
  const pipelineReady = Boolean(health?.engines.codex?.available);

  async function submit(event: FormEvent) {
    event.preventDefault();
    ++latestJobRequest.current;
    setError(''); setSubmitting(true); setResults(null);
    try {
      if (!file && !source.trim()) throw new Error('PDF 파일 또는 로컬 경로를 입력해주세요.');
      const [resolvedSource, resolvedSolution] = await Promise.all([
        file ? uploadSelectedFile(file) : Promise.resolve(source.trim()),
        solutionFile ? uploadSelectedFile(solutionFile) : Promise.resolve(undefined),
      ]);
      const response = await fetch(`${API}/api/jobs`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: resolvedSource, solution_source: resolvedSolution, crop_dpi: Number(cropDpi), pages, unit, workers: Number(workers), ensemble: ensembleEnabled, gemini: ensembleEnabled, qwen: ensembleEnabled, verify: 'all' }),
      });
      const data = await response.json() as Job & { error?: string };
      if (!response.ok) throw new Error(data.error || '작업을 시작하지 못했습니다.');
      setActiveJob(data); setJobs((current) => [data, ...current]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '알 수 없는 오류가 발생했습니다.');
    } finally { setSubmitting(false); }
  }

  async function attachSolution() {
    if (!activeJob || activeJob.state !== 'completed' || !solutionFile) return;
    setError(''); setAttachingSolution(true);
    try {
      const resolvedSolution = await uploadSelectedFile(solutionFile);
      const response = await fetch(`${API}/api/jobs/${activeJob.id}/solutions`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: resolvedSolution, crop_dpi: Number(cropDpi), force: false }),
      });
      const data = await response.json() as Job & { error?: string };
      if (!response.ok) throw new Error(data.error || '해설 연결을 시작하지 못했습니다.');
      setActiveJob(data);
      await loadJob(activeJob.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '해설 연결 중 오류가 발생했습니다.');
    } finally { setAttachingSolution(false); }
  }

  function openComparison(item: ResultItem & { document: string; docIndex: number }) {
    const candidates = item.solutions?.candidates ?? [];
    const answerCandidates = candidates.filter((asset) => ['quick_answer', 'mixed'].includes(asset.kind ?? ''));
    const explanationCandidates = candidates.filter((asset) => ['detailed_solution', 'mixed'].includes(asset.kind ?? ''));
    setSelectedAnswerId(answerCandidates.length === 1 ? answerCandidates[0].id : '');
    setSelectedExplanationId(explanationCandidates.length === 1 ? explanationCandidates[0].id : '');
    setCompareItem(item);
    setCompareTab('question');
  }

  async function confirmSolution() {
    if (!activeJob || !compareItem || !compareItem.solutions || (!selectedAnswerId && !selectedExplanationId)) return;
    setError(''); setConfirmingSolution(true);
    try {
      const response = await fetch(`${API}/api/jobs/${activeJob.id}/solutions/confirm`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          doc: compareItem.docIndex,
          unit_id: compareItem.id,
          answer_ids: selectedAnswerId ? [selectedAnswerId] : [],
          explanation_ids: selectedExplanationId ? [selectedExplanationId] : [],
        }),
      });
      const data = await response.json() as Job & { error?: string };
      if (!response.ok) throw new Error(data.error || '해설 후보를 확정하지 못했습니다.');
      setActiveJob(data);
      setCompareItem(null);
      await loadJob(data.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '해설 후보 확정 중 오류가 발생했습니다.');
    } finally { setConfirmingSolution(false); }
  }

  const compareCandidates = compareItem?.solutions?.candidates ?? [];
  const compareAnswerCandidates = compareCandidates.filter((asset) => ['quick_answer', 'mixed'].includes(asset.kind ?? ''));
  const compareExplanationCandidates = compareCandidates.filter((asset) => ['detailed_solution', 'mixed'].includes(asset.kind ?? ''));
  const compareOverallStatus = compareItem?.solutions ? overallLinkStatus(compareItem.solutions) : 'unmatched';
  const compareLinkedIds = new Set([
    ...(compareItem?.solutions?.answers ?? []).map((asset) => asset.id),
    ...(compareItem?.solutions?.explanations ?? []).map((asset) => asset.id),
  ]);
  const compareAnswerVerified = compareItem?.solutions?.answer_status === 'verified'
    || Boolean(compareItem?.solutions?.answers.length);
  const compareExplanationVerified = compareItem?.solutions?.explanation_status === 'verified'
    || Boolean(compareItem?.solutions?.explanations.length);
  const compareHasPendingCandidate = compareCandidates.some((asset) =>
    !compareLinkedIds.has(asset.id) && (
      (!compareAnswerVerified && ['quick_answer', 'mixed'].includes(asset.kind ?? ''))
      || (!compareExplanationVerified && ['detailed_solution', 'mixed'].includes(asset.kind ?? ''))
    ));
  const compareCanConfirm = ['review', 'conflict'].includes(compareItem?.solutions?.status ?? '')
    || (compareOverallStatus === 'partial' && compareHasPendingCandidate);

  const engineEntries = health ? Object.entries(health.engines) : [];
  const summary = results?.summary ?? activeJob?.summary ?? { pages: 0, crops: 0, passed: 0, review: 0, failed: 0 };

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">Q</span><div><strong>문항 크롭 스튜디오</strong><span>Science workbook crop console</span></div></div>
        <div className={`local-badge ${health ? '' : 'offline'}`}><i /> {health ? '로컬 엔진 준비됨' : '로컬 API 연결 대기'}</div>
      </header>

      <section className="hero compact">
        <div><p className="eyebrow">QUESTION CROP / MULTI-AI CONSENSUS</p><h1>문제집을 넣으면,<br />풀 수 있는 문항 단위로.</h1></div>
        <div className="hero-side">
          <p className="hero-copy">출판사 규칙 없이 의미와 구조를 분석합니다. 두 AI가 독립 판정하고, 불일치할 때만 세 번째 AI가 재검토합니다.</p>
          <div className="engine-row">
            {engineEntries.map(([key, engine]) => <span key={key} className={engine.available ? 'engine on' : 'engine off'}><i /> {engine.label}{!engine.available && ['gemini', 'qwen'].includes(key) ? ' · 키 필요' : ''}</span>)}
          </div>
        </div>
      </section>

      <section className="workspace-grid live">
        <aside className="panel source-panel">
          <div className="panel-heading"><span>새 작업</span><small>PDF SOURCE</small></div>
          <form onSubmit={submit}>
            <label className={`drop-zone ${file ? 'has-file' : ''}`} onDragOver={(event) => event.preventDefault()} onDrop={dropFile}>
              <span className="drop-icon">{file ? '✓' : '＋'}</span>
              <strong>{file ? file.name : 'PDF를 이곳에 놓으세요'}</strong>
              <span>{file ? `${(file.size / 1024 / 1024).toFixed(1)} MB` : '또는 눌러서 파일 선택'}</span>
              <input type="file" accept="application/pdf" onChange={chooseFile} />
            </label>
            <div className="divider"><span>또는 로컬 경로</span></div>
            <label className="field-label" htmlFor="source-path">파일·폴더 경로</label>
            <input id="source-path" className="text-input" value={source} onChange={(event) => { setSource(event.target.value); if (event.target.value) setFile(null); }} placeholder="C:\문서\과학 문제집" />
            <label className={`solution-picker ${solutionFile ? 'has-file' : ''}`}>
              <span>정답과해설 PDF <small>선택</small></span>
              <strong>{solutionFile?.name ?? '파일 선택'}</strong>
              <input type="file" accept="application/pdf" onChange={chooseSolutionFile} />
            </label>
            <div className="field-row">
              <div><label className="field-label" htmlFor="pages">페이지</label><input id="pages" className="text-input" value={pages} onChange={(event) => setPages(event.target.value)} /></div>
              <div><label className="field-label" htmlFor="unit">출력 단위</label><select id="unit" className="text-input" value={unit} onChange={(event) => setUnit(event.target.value)}><option value="atomic">소문항별</option><option value="main">대문항 전체</option><option value="both">둘 다</option></select></div>
            </div>
            <div className="field-row option-row">
              <div><label className="field-label" htmlFor="workers">병렬 작업</label><select id="workers" className="text-input" value={workers} onChange={(event) => setWorkers(event.target.value)}><option value="1">1개</option><option value="2">2개</option><option value="3">3개</option><option value="4">4개</option></select></div>
              <div><label className="field-label" htmlFor="crop-dpi">출력 품질</label><select id="crop-dpi" className="text-input" value={cropDpi} onChange={(event) => setCropDpi(event.target.value as '300' | '450')}><option value="300">기본 300 DPI</option><option value="450">고화질 450 DPI</option></select></div>
            </div>
            <div className="toggle-stack standalone"><label className={!ensembleAvailable ? 'disabled' : ''}><input type="checkbox" checked={ensembleEnabled} disabled={!ensembleAvailable} onChange={(event) => setEnsemble(event.target.checked)} /><span /> Gemini + Qwen 합의</label></div>
            {error && <p className="form-error" role="alert">{error}</p>}
            <button className="primary-button" type="submit" disabled={submitting || !pipelineReady}>{submitting ? '업로드 중…' : '분석 시작'}</button>
            <p className="privacy-note">파일은 이 PC 안에서 처리되고, 선택한 AI에 페이지만 전송됩니다.</p>
          </form>

          {jobs.length > 0 && <div className="recent-jobs"><div className="section-label">최근 작업</div>{jobs.slice(0, 4).map((job) => <button key={job.id} type="button" className={activeJob?.id === job.id ? 'recent active' : 'recent'} onClick={() => loadJob(job.id)}><span className={`state-dot ${job.state}`} /><div><strong>{job.source_name}</strong><small>{formatTime(job.created_at)} · {job.stage}</small></div></button>)}</div>}
          {activeJob?.state === 'completed' && <div className="solution-job-card">
            <div><span className={`state-dot ${activeJob.solution?.state === 'completed' ? 'completed' : activeJob.solution?.state === 'failed' ? 'failed' : ['queued', 'indexing', 'linking'].includes(activeJob.solution?.state ?? '') ? 'running' : 'idle'}`} /><div><strong>답·해설 연결</strong><small>{activeJob.solution?.message ?? '정답과해설 PDF를 선택할 수 있습니다.'}</small></div></div>
            <button type="button" onClick={attachSolution} disabled={!solutionFile || attachingSolution || ['queued', 'indexing', 'linking'].includes(activeJob.solution?.state ?? '')}>{attachingSolution ? '연결 중…' : activeJob.solution?.state === 'completed' ? '다시 연결' : '해설 PDF 연결'}</button>
          </div>}
        </aside>

        <section className="panel flow-panel live-flow">
          <div className="panel-heading"><span>{results ? '결과 갤러리' : '분석 흐름'}</span><small>{activeJob?.state?.toUpperCase() ?? 'READY'}</small></div>

          {!activeJob && <div className="empty-preview"><div className="page-stack"><i /><i /><i /></div><strong>아직 실행한 작업이 없습니다</strong><p>왼쪽에서 PDF를 선택하면 페이지 분석 상태와 결과 crop이 여기에 표시됩니다.</p></div>}

          {activeJob && !results && <div className="running-view">
            <div className="running-head"><div className={`spinner-ring ${['failed', 'interrupted'].includes(activeJob.state) ? 'stopped' : ''}`}><span>{activeJob.progress}%</span></div><div><p className="eyebrow">{['failed', 'interrupted'].includes(activeJob.state) ? 'PROCESS STOPPED' : 'PIPELINE RUNNING'}</p><h2>{activeJob.stage}</h2><p>{activeJob.message}</p></div></div>
            <div className="progress-track"><i style={{ width: `${activeJob.progress}%` }} /></div>
            <div className="stage-rail">{stages.map(([number, title, description], index) => <article key={number} className={activeJob.progress >= [18, 35, 68][index] ? 'done' : ''}><span>{number}</span><div><strong>{title}</strong><p>{description}</p></div></article>)}</div>
            <div className="log-view">{(activeJob.logs ?? []).slice(-9).map((line, index) => <code key={`${index}-${line}`}>{line}</code>)}</div>
          </div>}

          {results && <>
            <div className="gallery-toolbar">
              <div className="segmented"><button type="button" className={view === 'crops' ? 'selected' : ''} onClick={() => setView('crops')}>문항 crop</button><button type="button" className={view === 'overlays' ? 'selected' : ''} onClick={() => setView('overlays')}>페이지 오버레이</button></div>
              {view === 'crops' && <div className="filter-row">{(['all', 'pass', 'review', 'fail'] as const).map((value) => <button type="button" key={value} className={filter === value ? `filter ${value} selected` : `filter ${value}`} onClick={() => setFilter(value)}>{value === 'all' ? `전체 ${allItems.length}` : `${value.toUpperCase()} ${allItems.filter((item) => item.verdict === value).length}`}</button>)}</div>}
            </div>
            {view === 'crops' ? <div className="crop-grid">{filteredItems.map((item) => {
              const links = item.solutions;
              const overallStatus = links ? overallLinkStatus(links) : 'unmatched';
              const hasLinkedAssets = Boolean(links?.answers.length || links?.explanations.length);
              const hasCandidates = Boolean(links?.candidates?.length);
              return <article key={`${item.document}-${item.id}`} className={`crop-card ${item.verdict}`}>
                <button type="button" className="image-button" onClick={() => setSelectedImage({ src: `${API}${item.image_url}`, title: `${item.number} · ${item.document}` })}><img src={`${API}${item.image_url}`} alt={`${item.number} 문항 crop`} loading="lazy" /></button>
                <div className="crop-info">
                  <div><span className={`verdict ${item.verdict}`}>{item.verdict}</span><strong>{item.number}</strong></div>
                  <small>{kindLabels[item.kind] ?? item.kind} · p.{item.source_pages.join(', ')}</small>
                  {item.reasons?.[0] && <p>{formatReason(item.reasons[0])}</p>}
                  {item.warnings?.length ? <div className="warning-list">{item.warnings.map((warning) => <span key={warning}>{formatWarning(warning)}</span>)}</div> : null}
                  {links && <div className="solution-actions"><span className={`link-status ${overallStatusClass(overallStatus)}`}>{linkLabels[overallStatus]}</span>{(hasLinkedAssets || hasCandidates) && <button type="button" onClick={() => openComparison(item)}>{solutionActionLabel(links)}</button>}</div>}
                  {links?.review_reasons?.[0] && <p className="solution-reason">{links.review_reasons[0]}</p>}
                </div>
              </article>;
            })}</div> : <div className="overlay-grid">{overlayItems.map((item) => <button type="button" key={item.src} onClick={() => setSelectedImage({ src: `${API}${item.src}`, title: item.title })}><img src={`${API}${item.src}`} alt={item.title} loading="lazy" /><span>{item.title}</span></button>)}</div>}
            {view === 'crops' && filteredItems.length === 0 && <div className="no-results">선택한 상태의 문항이 없습니다.</div>}
          </>}
        </section>

        <aside className="panel summary-panel">
          <div className="panel-heading"><span>결과 요약</span><small>SESSION</small></div>
          <dl className="metrics"><div><dt>처리 페이지</dt><dd>{summary.pages || '—'}</dd></div><div><dt>검출 문항</dt><dd>{summary.crops || '—'}</dd></div><div><dt>자동 통과</dt><dd className="pass-text">{summary.passed || '—'}</dd></div><div><dt>검토·실패</dt><dd className={summary.review + summary.failed ? 'warn-text' : ''}>{summary.review + summary.failed || '—'}</dd></div></dl>
          <div className="quality-card"><span>품질 게이트</span><strong>{ensembleAvailable ? 'Codex × Gemini + Qwen' : 'Codex 단독 준비'}</strong><p>{ensembleAvailable ? 'Codex와 Gemini가 합의하면 통과하고, 불일치는 Qwen이 재판정합니다.' : 'Gemini와 Qwen 키를 연결하면 다중 AI 합의가 활성화됩니다.'}</p></div>
          <div className="engine-details">{engineEntries.map(([key, engine]) => <div key={key}><span className={engine.available ? 'state-dot completed' : 'state-dot failed'} /><div><strong>{engine.label}</strong><small>{engine.detail ?? (engine.available ? '사용 가능' : '사용할 수 없음')}</small></div></div>)}</div>
        </aside>
      </section>

      {selectedImage && <div className="modal" role="dialog" aria-modal="true" aria-label={selectedImage.title} onClick={() => setSelectedImage(null)}><button type="button" aria-label="닫기" onClick={() => setSelectedImage(null)}>×</button><figure onClick={(event) => event.stopPropagation()}><img src={selectedImage.src} alt={selectedImage.title} /><figcaption>{selectedImage.title}</figcaption></figure></div>}
      {compareItem && <div className="modal compare-modal" role="dialog" aria-modal="true" aria-label={`${compareItem.number} 답과 해설`} onClick={() => setCompareItem(null)}>
        <button type="button" aria-label="닫기" onClick={() => setCompareItem(null)}>×</button>
        <section onClick={(event) => event.stopPropagation()}>
          <header><div><strong>{compareItem.number} · 문제와 답·해설</strong><span>{compareItem.document}</span></div><span className={`link-status ${overallStatusClass(compareOverallStatus)}`}>{linkLabels[compareOverallStatus]}</span></header>
          <div className="compare-tabs">{(['question', 'answer', 'explanation'] as const).map((tab) => <button type="button" key={tab} className={compareTab === tab ? 'active' : ''} onClick={() => setCompareTab(tab)}>{tab === 'question' ? '문제' : tab === 'answer' ? '답' : '해설'}</button>)}</div>
          <div className="compare-grid">
            <article className="compare-pane" data-active={compareTab === 'question'}><h3>문제</h3><img src={`${API}${compareItem.image_url}`} alt={`${compareItem.number} 문제`} /></article>
            <article className="compare-pane" data-active={compareTab === 'answer'}><h3>빠른 답</h3>{compareItem.solutions?.answers.length ? compareItem.solutions.answers.map((asset) => <figure key={asset.id}><img src={`${API}${asset.image_url}`} alt={`${compareItem.number} 답`} /><figcaption>{asset.printed_pages.length ? `해설 인쇄 ${asset.printed_pages.join(', ')}쪽` : '빠른 답'}</figcaption></figure>) : compareAnswerCandidates.length ? compareAnswerCandidates.map((asset) => <label key={asset.id} className={`candidate-option ${selectedAnswerId === asset.id ? 'selected' : ''}`}><input type="radio" name="answer-candidate" checked={selectedAnswerId === asset.id} onChange={() => setSelectedAnswerId(asset.id)} /><img src={`${API}${asset.image_url}`} alt={`${compareItem.number} 답 후보`} /><span>{asset.printed_pages.length ? `답 후보 · 해설 인쇄 ${asset.printed_pages.join(', ')}쪽` : '답 후보'}</span></label>) : <p>연결된 빠른 답이 없습니다.</p>}</article>
            <article className="compare-pane" data-active={compareTab === 'explanation'}><h3>상세 해설</h3>{compareItem.solutions?.explanations.length ? compareItem.solutions.explanations.map((asset) => <figure key={asset.id}><img src={`${API}${asset.image_url}`} alt={`${compareItem.number} 상세 해설`} /><figcaption>{asset.printed_pages.length ? `해설 인쇄 ${asset.printed_pages.join(', ')}쪽` : '상세 해설'}</figcaption></figure>) : compareExplanationCandidates.length ? compareExplanationCandidates.map((asset) => <label key={asset.id} className={`candidate-option ${selectedExplanationId === asset.id ? 'selected' : ''}`}><input type="radio" name="explanation-candidate" checked={selectedExplanationId === asset.id} onChange={() => setSelectedExplanationId(asset.id)} /><img src={`${API}${asset.image_url}`} alt={`${compareItem.number} 해설 후보`} /><span>{asset.printed_pages.length ? `해설 후보 · 인쇄 ${asset.printed_pages.join(', ')}쪽` : '해설 후보'}</span></label>) : <p>연결된 상세 해설이 없습니다.</p>}</article>
          </div>
          {compareCanConfirm && compareCandidates.length > 0 && <footer className="candidate-confirm"><p>문제와 같은 항목인지 확인한 뒤 선택한 답·해설을 확정하세요.</p><button type="button" onClick={confirmSolution} disabled={confirmingSolution || (!selectedAnswerId && !selectedExplanationId)}>{confirmingSolution ? '확정 중…' : '선택한 후보 확정'}</button></footer>}
        </section>
      </div>}
    </main>
  );
}
