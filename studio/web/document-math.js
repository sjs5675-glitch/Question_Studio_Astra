import { MathfieldElement } from 'mathlive';
import { validateTex, validateLiteral } from './document-math-policy.js';

MathfieldElement.soundsDirectory = null;
MathfieldElement.openUrl = () => {};
const palette = [
  ['분수', '\\frac{#0}{#?}'], ['지수', '#0^{#?}'], ['아래첨자', '#0_{#?}'], ['근호', '\\sqrt{#0}'],
  ['그리스 문자', '\\alpha'], ['벡터', '\\overrightarrow{#0}'], ['윗줄', '\\overline{#0}'],
  ['절댓값', '\\left|#0\\right|'], ['이중 막대', '\\left\\Vert#0\\right\\Vert'],
  ['단위', '\\mathrm{m/s}'], ['행렬', '\\begin{pmatrix}1&2\\\\3&4\\end{pmatrix}'], ['화학식', '\\mathrm{H}_{2}\\mathrm{O}'], ['반응 화살표', '\\rightarrow'],
];
function button(label, action) { const element = document.createElement('button'); element.type = 'button'; element.textContent = label; element.addEventListener('click', action); return element; }

export async function editEquation(options) {
  const { node, validate, commit, finished, source, style = {} } = options;
  MathfieldElement.fontsDirectory = '/dist/document/mathlive-fonts';
  const original = structuredClone(node.payload);
  const dialog = document.createElement('dialog'); dialog.className = 'document-math-dialog';
  const title = document.createElement('h2'); title.textContent = '수식 편집';
  const note = document.createElement('p'); note.textContent = '적용 전까지 문서에는 반영되지 않습니다.';
  const rawOriginal = document.createElement('details'); const summary = document.createElement('summary'); summary.textContent = '보존된 현재 표현';
  const rawValues = document.createElement('pre'); rawValues.textContent = JSON.stringify({ current: original, imported: source }, null, 2); rawOriginal.append(summary, rawValues);
  const size = document.createElement('input'); size.type = 'number'; size.min = '.5'; size.max = '3'; size.step = '.1'; size.value = String(style.math_size ?? 1); size.setAttribute('aria-label', '수식 크기 배율');
  const variant = document.createElement('select'); variant.setAttribute('aria-label', '수식 굵기');
  for (const [value, label] of [['normal', '수식 보통'], ['bold', '수식 굵게']]) { const option = document.createElement('option'); option.value = value; option.textContent = label; variant.append(option); } variant.value = style.math_variant ?? 'normal';
  const mode = document.createElement('select'); mode.setAttribute('aria-label', '수식 입력 방식');
  for (const [value, label] of [['visual', '수식 입력기'], ['raw', 'Raw TeX · 직렬화 없이 적용'], ['literal', '일반 문자로 명시']]) {
    const option = document.createElement('option'); option.value = value; option.textContent = label;
    option.disabled = value === 'literal' && ['native_hwp_only', 'manual_disagreement'].includes(original.authority); mode.append(option);
  }
  const resolution = document.createElement('input'); resolution.type = 'checkbox'; resolution.id = 'document-math-resolve';
  const resolutionLabel = document.createElement('label'); resolutionLabel.htmlFor = resolution.id; resolutionLabel.append(resolution, ' 현재 문서의 표현을 명시적으로 확정');
  const host = document.createElement('div'); host.className = 'document-math-host';
  const raw = document.createElement('textarea'); raw.maxLength = 16384; raw.setAttribute('aria-label', 'Raw 수식 또는 일반 문자'); raw.rows = 4;
  const controls = document.createElement('div'); controls.className = 'document-toolbar';
  const preview = document.createElement('div'); preview.className = 'document-math-preview'; preview.setAttribute('aria-label', '수식 초안 미리보기');
  const status = document.createElement('p'); status.setAttribute('role', 'status');
  const actions = document.createElement('div'); actions.className = 'document-toolbar';
  let field; let ready = false; let baseline = ''; let mountedRaw = ''; let previousMode = ''; let alive = true; let applying = false;
  const controller = new AbortController();
  const initial = original.latex ?? '';
  const requiresResolution = !['ordinary_tex', 'plain_literal'].includes(original.authority);
  function close() { if (!alive) return; alive = false; controller.abort(); dialog.close(); dialog.remove(); finished(); }
  function visualValue() { if (!ready || !field.isConnected) throw Error('수식 입력기가 준비되지 않았습니다.'); const serialized = field.getValue('latex'); return serialized === baseline ? mountedRaw : serialized; }
  function value() { return mode.value === 'visual' ? visualValue() : raw.value; }
  function draw() {
    try {
      const candidate = value();
      if (mode.value === 'literal') { validateLiteral(candidate); preview.textContent = candidate; }
      else preview.innerHTML = validateTex(candidate, original.display);
      status.textContent = mode.value === 'visual' && field.errors.length ? '이 수식은 Raw TeX 방식으로 편집해 주세요.' : '초안 미리보기 · 아직 적용하지 않음';
    } catch (error) { preview.textContent = ''; status.textContent = error.message; }
  }
  async function mount() {
    if (raw.value.trim()) validateTex(raw.value, original.display);
    if (field && ready) {
      field.setValue(raw.value, { silenceNotifications: true }); baseline = field.getValue('latex'); mountedRaw = raw.value;
      if (field.errors.length) throw Error('MathLive 미지원: Raw TeX로 편집해 주세요.');
      host.hidden = false; return;
    }
    field = new MathfieldElement(); field.setAttribute('aria-label', '수식 초안');
    field.mathVirtualKeyboardPolicy = 'manual'; field.smartMode = false; field.smartFence = false; field.smartSuperscript = false; field.removeExtraneousParentheses = false;
    const mounted = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('수식 입력기 준비 시간이 초과되었습니다.')), 10000);
      field.addEventListener('mount', () => { clearTimeout(timer); resolve(); }, { once: true });
      controller.signal.addEventListener('abort', () => { clearTimeout(timer); reject(Error('수식 편집이 취소되었습니다.')); }, { once: true });
    });
    field.addEventListener('input', draw);
    field.addEventListener('paste', event => {
      event.preventDefault();
      try { const pasted = event.clipboardData.getData('text/plain'); validateTex(pasted, original.display); field.insert(pasted); }
      catch (error) { status.textContent = error.message; }
    });
    host.append(field); await mounted;
    if (!alive || !field.isConnected) return;
    field.inlineShortcuts = {};
    field.setValue(raw.value, { silenceNotifications: true }); baseline = field.getValue('latex'); mountedRaw = raw.value; ready = true;
    if (field.errors.length) { mode.value = 'raw'; host.hidden = true; raw.hidden = false; status.textContent = 'MathLive 미지원: 원문을 그대로 보존하는 Raw TeX 편집을 사용합니다.'; }
  }
  for (const [label, latex] of palette) controls.append(button(label, () => {
    if (!ready || mode.value !== 'visual') { status.textContent = '수식 입력기 방식에서 팔레트를 사용해 주세요.'; return; }
    field.focus(); field.insert(latex); draw();
  }));
  actions.append(button('안쪽 실행 취소', () => { if (ready && mode.value === 'visual') field.executeCommand('undo'); draw(); }),
    button('안쪽 다시 실행', () => { if (ready && mode.value === 'visual') field.executeCommand('redo'); draw(); }), button('취소', close));
  const apply = button('적용', async () => {
    if (applying) return;
    try {
      const candidate = value();
      const unchanged = mode.value === 'literal' ? candidate === original.plain_text : candidate === initial;
      const scale = Number(size.value);
      if (!Number.isFinite(scale) || scale < .5 || scale > 3) throw Error('수식 크기는 0.5~3배로 지정해 주세요.');
      const changedStyle = scale !== (style.math_size ?? 1) || variant.value !== (style.math_variant ?? 'normal');
      const nextStyle = changedStyle ? { ...style, math_size: scale, math_variant: variant.value } : style;
      if (unchanged && !resolution.checked) { if (changedStyle) commit(original, nextStyle); close(); return; }
      if (requiresResolution && !resolution.checked) throw Error('현재 문서의 표현을 명시적으로 확정해 주세요.');
      if (mode.value === 'visual' && field.errors.length) throw Error('Raw TeX 방식으로 편집해 주세요.');
      if (mode.value === 'literal') validateLiteral(candidate); else validateTex(candidate, original.display);
      applying = true; apply.disabled = true;
      const payload = await validate({ mode: mode.value === 'literal' ? 'literal' : 'tex', value: candidate, display: original.display }, controller.signal);
      if (alive) { commit(payload, nextStyle); close(); }
    } catch (error) { if (alive) status.textContent = error.message; }
    finally { applying = false; apply.disabled = false; }
  }); actions.append(apply);
  mode.addEventListener('change', async () => {
    if (previousMode === 'visual' && ready) raw.value = visualValue();
    host.hidden = mode.value !== 'visual'; raw.hidden = mode.value === 'visual';
    try { if (mode.value === 'visual') await mount(); draw(); } catch (error) { status.textContent = error.message; mode.value = 'raw'; raw.hidden = false; host.hidden = true; }
    previousMode = mode.value;
  });
  raw.addEventListener('input', draw);
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  dialog.addEventListener('keydown', event => { if (event.key === 'Escape' && !event.isComposing) { event.preventDefault(); event.stopPropagation(); close(); } }, true);
  dialog.addEventListener('keydown', event => event.stopPropagation());
  function setting(label, control) {
    const wrapper = document.createElement('label'); wrapper.className = 'math-setting';
    const text = document.createElement('span'); text.textContent = label; wrapper.append(text, control); return wrapper;
  }
  const styleControls = document.createElement('div'); styleControls.className = 'document-math-options';
  styleControls.append(setting('수식 크기 배율', size), setting('수식 굵기', variant));
  dialog.append(title, note, rawOriginal, setting('수식 입력 방식', mode), resolutionLabel, styleControls, host, raw, controls, preview, status, actions);
  document.body.append(dialog); dialog.showModal();
  raw.value = initial;
  mode.value = ['plain_only', 'plain_literal'].includes(original.authority) ? 'literal' : 'visual';
  if (mode.value === 'literal') raw.value = original.plain_text ?? '';
  if (mode.value === 'visual') {
    try { await mount(); } catch (error) { mode.value = 'raw'; status.textContent = error.message; }
  }
  host.hidden = mode.value !== 'visual'; raw.hidden = mode.value === 'visual';
  previousMode = mode.value;
  draw();
  return { dialog, close };
}
