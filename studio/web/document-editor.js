import { DocumentSession } from './document-state.js';
import { renderDocument } from './document-render.js';
import { mountTextEditor } from './document-text.js';
import { editEquation } from './document-math.js';
import { mountLayoutControls } from './document-layout-controls.js';
import { mountPdfExport } from './document-export.js';

const $ = id => document.getElementById(id);
async function api(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', ...options, headers: { 'Content-Type': 'application/json', ...options.headers } });
  const result = await response.json();
  if (!response.ok) throw Error(`${response.status}: ${result.message ?? result.error}`);
  return result;
}

export async function startDocumentEditor() {
  let session; let active; let activeId; let mathOpen = false; let saving = false; let loadVersion = 0;
  const status = message => { $('document-status').textContent = message; };
  function closeText() { if (active) { active.destroy(); active = null; activeId = null; } }
  function updateStatus() {
    loadVersion++;
    layoutControls.invalidate();
    $('document-workspace').inert = saving;
    $('document-list').inert = saving;
    for (const control of document.querySelectorAll('#document-workspace button, #document-workspace input, #document-workspace select, #document-list button')) control.disabled = saving;
    $('document-save').disabled = !session || saving;
    $('document-undo').disabled = !session?.undoStack.length;
    $('document-redo').disabled = !session?.redoStack.length;
    pdfExport.sync();
    if (session) status(`${session.dirty ? '저장하지 않은 변경' : '저장됨'} · r${session.current.revision}${session.dirty ? '' : ' · PDF를 만들 수 있습니다.'}`);
  }
  function render() {
    if (!session) return;
    closeText();
    renderDocument($('document-flow'), session.current, { paragraph: openParagraph, math: openMath,
      move: (id, direction) => { session.move(id, direction); render(); }, drop: (id, target) => { session.move(id, target); render(); }, remove: id => { session.remove(id); render(); } });
    $('document-title').value = session.current.layout.title;
    $('document-font').value = session.current.styles.defaults.font_family;
    $('document-font-size').value = session.current.styles.defaults.font_size_pt;
    $('document-line-spacing').value = session.current.styles.defaults.line_spacing;
    layoutControls.sync(session.current);
    updateStatus();
  }
  function openParagraph(id) {
    if (mathOpen || saving || activeId === id) return;
    loadVersion++;
    if (active) render();
    const element = $('document-flow').querySelector(`[data-node-id="${CSS.escape(id)}"]`);
    activeId = id;
    active = mountTextEditor({ current: structuredClone(session.current), id, element,
      changed: document => { session.commit(document); updateStatus(); }, math: openMath, error: status });
  }
  async function openMath(id) {
    if (mathOpen || saving) return;
    closeText(); render();
    const owner = session; const ticket = {}; mathOpen = ticket;
    const node = owner.current.content.nodes.find(node => node.id === id);
    const current = () => session === owner && mathOpen === ticket && owner.current.content.nodes.some(node => node.id === id);
    const finished = () => { if (mathOpen !== ticket) return; mathOpen = false; render(); $('document-flow').querySelector(`[data-node-id="${CSS.escape(id)}"]`)?.focus(); };
    try {
      const imported = node.provenance ? await api(`/api/documents/${owner.current.document_id}/math/${id}/source`) : { source: null };
      if (!current()) { finished(); return; }
      await editEquation({ node, source: imported.source, style: owner.current.styles.node_overrides[id] ?? {},
        validate: async (body, signal) => {
          if (!current()) throw Error('수식 편집 문서가 변경되었습니다. 다시 열어 주세요.');
          const result = await api(`/api/documents/${owner.current.document_id}/math/validate`, { method: 'POST', body: JSON.stringify(body), signal });
          if (!current()) throw Error('수식 편집 문서가 변경되었습니다. 다시 열어 주세요.');
          return result.equation;
        },
        commit: (payload, style) => { if (current()) owner.change(document => { document.content.nodes.find(node => node.id === id).payload = payload; if (Object.keys(style).length) document.styles.node_overrides[id] = style; }); },
        finished,
      });
    } catch (error) { if (mathOpen === ticket) { mathOpen = false; status(error.message); } }
  }
  async function refreshList() {
    const result = await api('/api/documents'); $('document-list').replaceChildren();
    for (const item of result.documents) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'document-list-item';
      button.textContent = `${item.title || '제목 없는 문서'} · r${item.revision}`;
      button.addEventListener('click', () => load(item.document_id)); $('document-list').append(button);
    }
    if (!result.documents.length) $('document-list').textContent = '문제지에서 문서를 만들어 주세요.';
  }
  async function load(id) {
    if (saving || mathOpen) { status('현재 저장 또는 수식 편집을 마친 뒤 문서를 열어 주세요.'); return; }
    const version = ++loadVersion; const owner = session;
    if (session?.dirty && !confirm('저장하지 않은 변경을 버리고 문서를 열까요?')) return;
    try {
      const result = await api(`/api/documents/${encodeURIComponent(id)}`);
      if (version !== loadVersion || session !== owner || saving || mathOpen) return;
      closeText(); session = new DocumentSession(result.document); $('document-workspace').hidden = false;
      history.replaceState(null, '', `/document.html?id=${encodeURIComponent(id)}`); render();
    } catch (error) { if (version === loadVersion && session === owner) status(error.message); }
  }
  $('document-workspace').addEventListener('input', () => { loadVersion++; });
  const layoutControls = mountLayoutControls($('document-layout-controls'), {
    snapshot: () => structuredClone(session.current),
    beforePreview: () => { if (!session || saving || mathOpen) return false; closeText(); return true; },
    change: action => { if (!session || saving || mathOpen) return; closeText(); session.change(action); render(); },
  });
  const pdfExport = mountPdfExport({ api, status, current: () => session?.current, allowed: () => Boolean(session && !session.dirty && !saving && !mathOpen && !active) });
  $('document-workspace').addEventListener('input', () => layoutControls.invalidate());
  $('document-save').addEventListener('click', async () => {
    if (!session || saving || mathOpen) return;
    closeText(); saving = true; render();
    try {
      const result = await api(`/api/documents/${session.current.document_id}`, { method: 'PATCH', body: JSON.stringify({ expected_revision: session.current.revision, document: session.current }) });
      session.savedDocument(result.document); await refreshList();
      saving = false; render();
    } catch (error) { saving = false; updateStatus(); status(`${error.message} · 초안은 그대로 보존됩니다.`); }
  });
  function historyAction(action) { if (!session || mathOpen || saving) return; closeText(); session[action](); render(); }
  $('document-undo').addEventListener('click', () => historyAction('undo'));
  $('document-redo').addEventListener('click', () => historyAction('redo'));
  document.addEventListener('keydown', event => {
    if (event.isComposing || active?.view.composing) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z' && !mathOpen) { event.preventDefault(); historyAction(event.shiftKey ? 'redo' : 'undo'); }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'y' && !mathOpen) { event.preventDefault(); historyAction('redo'); }
  });
  for (const button of document.querySelectorAll('[data-mark]')) {
    button.addEventListener('mousedown', event => event.preventDefault());
    button.addEventListener('click', () => active?.chain().focus().toggleMark(button.dataset.mark).run());
  }
  function styleChange(key, value) {
    if (!session || saving) return;
    if (active && !active.state.selection.empty && ['font_family', 'font_size_pt'].includes(key)) active.chain().focus().setMark('textStyle', { style: { [key]: value } }).run();
    else if (active) {
      const position = active.state.selection.$from.before(1); const node = active.state.doc.nodeAt(position);
      active.view.dispatch(active.state.tr.setNodeMarkup(position, undefined, { ...node.attrs, style: { ...node.attrs.style, [key]: value } }));
    } else { session.change(document => { document.styles.defaults[key] = value; }); render(); }
  }
  $('document-font').addEventListener('change', event => styleChange('font_family', event.target.value));
  for (const [id, key, min, max] of [['document-font-size', 'font_size_pt', 6, 72], ['document-line-spacing', 'line_spacing', 1, 3], ['document-paragraph-spacing', 'paragraph_spacing_mm', 0, 30]]) {
    $(id).addEventListener('change', event => { const value = Number(event.target.value); if (Number.isFinite(value) && value >= min && value <= max) styleChange(key, value); else status('서식 범위를 확인해 주세요.'); });
  }
  $('document-title').addEventListener('change', event => { closeText(); session.change(document => { document.layout.title = event.target.value; }); render(); });
  $('document-add-math').addEventListener('mousedown', event => event.preventDefault());
  $('document-add-math').addEventListener('click', () => {
    if (!session) return;
    if (!active) openParagraph(session.current.content.nodes.find(node => node.kind === 'paragraph').id);
    active.commands.insertContent({ type: 'equation', attrs: { original: { id: crypto.randomUUID(), kind: 'equation', children: [], provenance: null,
      payload: { authority: 'ordinary_tex', latex: 'x', hwp_script: null, plain_text: null, display: 'inline', validation: 'valid', derivation: null } }, style: {} } });
  });
  $('document-refresh-list').addEventListener('click', () => refreshList().catch(error => status(error.message)));
  window.addEventListener('beforeunload', event => { if (session?.dirty) { event.preventDefault(); event.returnValue = ''; } });
  window.documentEditor = { snapshot: () => session ? structuredClone(session.current) : null, history: () => ({ undo: session?.undoStack.length, redo: session?.redoStack.length }), active: () => active };
  try { await refreshList(); const id = new URL(location.href).searchParams.get('id'); if (id) await load(id); }
  catch (error) { status(`${error.message} · Studio에서 다시 연결해 주세요.`); }
}
