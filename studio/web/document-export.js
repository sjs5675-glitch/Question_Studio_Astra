export function mountPdfExport(options) {
  const controls = ['pdf', 'hwpx', 'zip'].map(format => mountExport(options, format));
  return { sync: () => controls.forEach(control => control.sync()) };
}

function mountExport(options, format) {
  const label = format === 'zip' ? '문서 백업 ZIP (정답·원본 포함)' : format.toUpperCase();
  const resource = format === 'pdf' ? 'exports' : 'artifacts';
  const build = document.getElementById(`document-${format}-download`), cancel = document.getElementById(`document-${format}-cancel`), download = document.getElementById(`document-${format}-file`);
  const notes = document.createElement('details'); notes.className = 'document-help'; notes.hidden = true;
  notes.style.cssText = 'flex-basis:100%;min-width:0;overflow-wrap:anywhere';
  if (format === 'hwpx') download.parentElement.append(notes);
  let running = null;
  let visibleOwner = '';
  let generation = 0;
  function link(receipt) {
    download.href = receipt.download; download.textContent = `${label} 내려받기 · 저장본 r${receipt.revision}`; download.hidden = false;
    if (format === 'hwpx') {
      const summary = document.createElement('summary'); summary.textContent = `HWPX 출력 기록 · ${receipt.warnings?.length ?? 0}개 제한`;
      const native = document.createElement('p'); native.textContent = `한컴 실제 열기: 미검증${receipt.completeness === 'source_only' ? ' · 미해결 수식은 소스 전용으로 보존된 불완전 백업입니다.' : ''}`;
      notes.replaceChildren(summary, native);
      for (const warning of receipt.warnings ?? []) { const entry = document.createElement('p'); entry.textContent = `${warning.node_id ? `${warning.node_id} · ` : ''}${warning.control ?? warning.code}: ${warning.message ?? '현재 수식 이미지로 보존되며 이미지 자체는 편집할 수 없습니다.'}${warning.requested != null ? ` · 요청값 ${JSON.stringify(warning.requested)}` : ''}`; notes.append(entry); }
      notes.hidden = false;
    }
  }
  function sync() {
    build.disabled = !options.allowed() || Boolean(running);
    const current = options.current(), owner = current ? `${current.document_id}/${current.output_role}` : '';
    if (visibleOwner === owner) return;
    visibleOwner = owner; const ticket = ++generation; download.hidden = true; notes.hidden = true;
    if (current) options.api(`/api/documents/${current.document_id}/${resource}`).then(result => {
      if (visibleOwner !== owner || ticket !== generation) return;
      const receipt = result.exports.find(item => item.output_role === current.output_role && (format === 'pdf' || item.format === format));
      if (receipt) link(receipt);
    }).catch(error => { if (visibleOwner === owner && ticket === generation) options.status(error.message); });
  }
  build.addEventListener('click', async () => {
    if (!options.allowed() || running) return;
    const current = options.current(), owner = current.document_id, revision = current.revision;
    const ticket = ++generation;
    running = { owner, revision }; sync();
    options.status(`저장된 문서의 ${label}를 만드는 중입니다.`);
    try {
      const started = await options.api(`/api/documents/${owner}/exports`, { method: 'POST', body: JSON.stringify({ expected_revision: revision, format, output_role: current.output_role }) });
      running.buildId = started.build_id; cancel.hidden = false;
      let receipt = started;
      while (receipt.status === 'building') {
        await new Promise(resolve => setTimeout(resolve, 400));
        receipt = await options.api(`/api/documents/${owner}/${resource}/${started.build_id}`);
      }
      if (receipt.status !== 'ready') throw Error(receipt.message ?? receipt.error);
      if (options.current()?.document_id !== owner || ticket !== generation) return;
      if (options.current().output_role === receipt.output_role) link(receipt);
      options.status(`${label}${format === 'pdf' ? ` ${receipt.pages}쪽` : ''} 준비됨 · 저장본 r${receipt.revision}${receipt.completeness === 'source_only' ? ' · 미해결 수식: 소스 전용 백업' : ''}${receipt.warnings?.length ? ` · 출력 제한 ${receipt.warnings.length}건 (출력 기록 확인)` : ''}${options.current().revision !== revision ? ' · 현재 문서와 다른 저장본입니다.' : ''}`);
    } catch (error) { if (options.current()?.document_id === owner && ticket === generation) { options.status(`${error.message} · 이전 ${label}는 유지됩니다.`); if (download.hidden) visibleOwner = ''; } }
    finally { running = null; cancel.hidden = true; sync(); }
  });
  cancel.addEventListener('click', async () => {
    if (!running?.buildId) return;
    try { await options.api(`/api/documents/${running.owner}/${resource}/${running.buildId}/cancel`, { method: 'POST', body: '{}' }); options.status(`${label} 취소 중입니다. 종료가 끝날 때까지 기다려 주세요.`); }
    catch (error) { options.status(error.message); }
  });
  return { sync };
}
