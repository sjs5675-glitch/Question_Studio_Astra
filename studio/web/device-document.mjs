// Presentation-only state: no document/session model changes.
export function deriveDocumentView({ mobile = false, selected = false, documentKey = '', previous, requestedMode } = {}) {
  const continuing = previous?.mobile === mobile && previous?.documentKey === documentKey && previous?.selected === selected;
  const mode = !selected ? 'unselected' : !mobile ? 'editing'
    : requestedMode === 'editing' || requestedMode === 'reading' ? requestedMode
      : continuing ? previous.mode : 'reading';
  return { mobile, selected, documentKey, mode, libraryOpen: !selected };
}

export function deriveRevisionIndicator(status = '', previous) {
  const match = /^(저장하지 않은 변경|저장됨)\s*·\s*r(\d+)/.exec(status);
  if (!match) return previous ?? { dirty: null, revision: null, kind: 'unknown', text: '문서를 선택해 주세요.' };
  const dirty = match[1] === '저장하지 않은 변경';
  return { dirty, revision: match[2], kind: dirty ? 'unsaved' : 'saved',
    text: dirty ? `미저장 편집 · 기준 저장본 r${match[2]} · 출력 파일과 다를 수 있음` : `저장됨 · r${match[2]}` };
}

const editableBeforeReading = new WeakMap();
export function setEditorReadingMode(editor, reading) {
  if (!editor || editor.isDestroyed || typeof editor.setEditable !== 'function') return;
  if (reading) {
    if (!editableBeforeReading.has(editor)) editableBeforeReading.set(editor, editor.isEditable);
    if (editor.isEditable) editor.setEditable(false, false);
  } else if (editableBeforeReading.has(editor)) {
    const editable = editableBeforeReading.get(editor);
    editableBeforeReading.delete(editor);
    if (editor.isEditable !== editable) editor.setEditable(editable, false);
  }
}

export function blocksReadingEvent(event, reading) {
  if (!reading) return false;
  if (['click', 'focus', 'beforeinput', 'paste', 'cut', 'drop', 'dragstart'].includes(event.type)) return true;
  if (event.type !== 'keydown') return false;
  return ['Enter', ' ', 'Backspace', 'Delete'].includes(event.key)
    || Boolean((event.ctrlKey || event.metaKey) && ['z', 'y', 'x', 'v'].includes(event.key?.toLowerCase()));
}

const mountedDocuments = new WeakMap();
export function initDeviceDocument(doc = globalThis.document, view = doc?.defaultView) {
  if (!doc || !view) return null;
  if (mountedDocuments.has(doc)) return mountedDocuments.get(doc);
  const byId = id => doc.getElementById(id);
  const app = byId('document-app'), workspace = byId('document-workspace'), flow = byId('document-flow');
  const modeButton = byId('device-document-mode'), status = byId('document-status');
  const indicator = byId('device-document-revision'), title = byId('device-document-title');
  const library = byId('device-document-library');
  const panels = ['output', 'format', 'properties'].map(name => byId(`device-document-${name}`));
  if (!app || !workspace || !flow || !modeButton || !status || !indicator || !title || !library || panels.some(panel => !panel)) return null;
  const media = view.matchMedia('(max-width: 760px)');
  let state, revision, queued = false, disposed = false, readingAfterComposition = false;
  const listeners = [];
  const listen = (target, type, handler, capture = false) => {
    target.addEventListener(type, handler, capture);
    listeners.push(() => target.removeEventListener(type, handler, capture));
  };
  const text = (element, value) => { if (element.textContent !== value) element.textContent = value; };
  const attr = (element, name, value) => { if (element.getAttribute(name) !== value) element.setAttribute(name, value); };
  const reading = () => state?.mode === 'reading';
  function sync(requestedMode) {
    if (disposed) return;
    const selected = !workspace.hidden;
    const documentKey = selected ? new URL(view.location.href).searchParams.get('id') ?? '' : '';
    const next = deriveDocumentView({ mobile: media.matches, selected, documentKey, previous: state,
      requestedMode: requestedMode ?? (readingAfterComposition ? 'reading' : undefined) });
    if (next.mode === 'reading' && view.documentEditor?.active?.()?.view?.composing) {
      next.mode = 'editing'; readingAfterComposition = true;
    } else readingAfterComposition = false;
    const changedDocument = !state || state.documentKey !== next.documentKey || state.selected !== next.selected;
    const changedDevice = !state || state.mobile !== next.mobile;
    if (changedDocument) { library.open = next.libraryOpen; revision = undefined; }
    if (changedDocument || changedDevice) for (const panel of panels) panel.open = !next.mobile;
    state = next;
    if (app.dataset.documentMode !== state.mode) app.dataset.documentMode = state.mode;
    if (app.dataset.documentSelected !== String(selected)) app.dataset.documentSelected = String(selected);
    revision = deriveRevisionIndicator(selected ? status.textContent : '', revision);
    text(indicator, revision.text);
    if (indicator.dataset.kind !== revision.kind) indicator.dataset.kind = revision.kind;
    text(title, byId('document-title').value || '제목 없는 문서');
    text(modeButton, reading() ? '편집' : '읽기로 전환');
    attr(modeButton, 'aria-pressed', String(state.mode === 'editing'));
    const disabled = !selected || workspace.inert;
    if (modeButton.disabled !== disabled) modeButton.disabled = disabled;
    attr(flow, 'aria-label', reading() ? '현재 문서 본문 · 읽기 모드' : '현재 문서 본문');
    setEditorReadingMode(view.documentEditor?.active?.(), reading());
  }
  function schedule() {
    if (queued || disposed) return;
    queued = true;
    queueMicrotask(() => { queued = false; sync(); });
  }
  function changeMode(mode, focus = false) {
    if (!state.selected || workspace.inert || view.documentEditor?.active?.()?.view?.composing) return;
    sync(mode);
    if (focus && mode === 'editing') {
      const active = view.documentEditor?.active?.();
      if (active?.commands?.focus) active.commands.focus();
      else (flow.querySelector('.document-paragraph') ?? flow).focus({ preventScroll: true });
    }
  }
  listen(modeButton, 'click', () => changeMode(reading() ? 'editing' : 'reading', reading()));
  // Opening an editing panel is an explicit edit action, not a content write.
  for (const panel of panels.slice(1)) listen(panel, 'toggle', () => {
    if (panel.open && media.matches && reading()) changeMode('editing');
  });
  const guard = event => {
    if (!blocksReadingEvent(event, reading())) return;
    event.stopImmediatePropagation();
    // Preserve native text selection and harmless focus while blocking the editor's activation listeners.
    if (!['click', 'focus'].includes(event.type) || event.target.closest?.('button, a')) event.preventDefault();
  };
  for (const type of ['click', 'focus', 'keydown', 'beforeinput', 'paste', 'cut', 'drop', 'dragstart']) listen(flow, type, guard, true);
  listen(doc, 'keydown', event => {
    if (reading() && (event.ctrlKey || event.metaKey) && ['z', 'y'].includes(event.key.toLowerCase()) && !event.target.closest?.('dialog')) {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }, true);
  listen(workspace, 'input', schedule);
  listen(workspace, 'compositionend', schedule);
  listen(media, 'change', schedule);
  // Observe only model-owned signals, never the presentation elements we update.
  const observer = new view.MutationObserver(schedule);
  observer.observe(workspace, { attributes: true, attributeFilter: ['hidden', 'inert'] });
  observer.observe(status, { childList: true, characterData: true, subtree: true });
  observer.observe(flow, { childList: true, subtree: true });
  const controller = { sync, state: () => ({ ...state }), dispose() {
    disposed = true; observer.disconnect(); listeners.forEach(remove => remove());
    setEditorReadingMode(view.documentEditor?.active?.(), false);
    mountedDocuments.delete(doc);
  } };
  mountedDocuments.set(doc, controller);
  sync();
  return controller;
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => initDeviceDocument(), { once: true });
  else initDeviceDocument();
}
