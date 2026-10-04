import { layoutDocument } from './document-layout.js';

export function mountLayoutControls(host, options) {
  let generation = 0, controller, receipt;
  const fields = new Map(); const target = document.getElementById('document-preview'); const status = document.getElementById('document-layout-error');
  function group(title, open = false) {
    const details = document.createElement('details'); details.className = 'inspector-group'; details.open = open;
    const summary = document.createElement('summary'); summary.textContent = title;
    const body = document.createElement('div'); body.className = 'inspector-fields';
    details.append(summary, body); host.append(details); return body;
  }
  const pageFields = group('출력 내용 · 용지', true);
  const spacingFields = group('여백 · 간격', true);
  const labelFields = group('안내 · 머리말 · 쪽 번호');
  const breakFields = group('문항 나눔');
  const figureFields = group('그림 크기');
  function fieldGroup(path) {
    if (path.includes('margins_mm') || ['layout.column_gap_mm', 'layout.question_gap_mm'].includes(path)) return spacingFields;
    if (['layout.instructions', 'layout.unit', 'layout.type', 'layout.header', 'layout.footer', 'layout.page_start', 'layout.cover_number_visible'].includes(path)) return labelFields;
    return pageFields;
  }
  const specifications = [
    ['layout.template', '지면 종류', ['mock_exam|모의고사', 'workbook|문제집']], ['output_role', '출력 내용', ['question_only|문제만', 'explanation_only|답과 해설만', 'combined_end_section|문제 뒤 답과 해설']],
    ['layout.paper', '용지', ['A4|A4 · 210×297mm', 'B4|B4 · 250×353mm', 'custom|사용자 지정']], ['layout.first_page', '첫 쪽', ['title_questions|제목과 문제', 'cover|표지 전용']],
    ['layout.width_mm', '용지 너비(mm)', 'number', 100, 500], ['layout.height_mm', '용지 높이(mm)', 'number', 100, 700],
    ...['top|위', 'right|오른쪽', 'bottom|아래', 'left|왼쪽'].map(value => { const [key, label] = value.split('|'); return [`layout.margins_mm.${key}`, `${label} 여백(mm)`, 'number', 0, 100]; }),
    ['layout.column_gap_mm', '단 사이(mm)', 'number', 0, 100], ['layout.question_gap_mm', '문항 사이(mm)', 'number', 0, 100],
    ['layout.instructions', '안내 문구', 'textarea'], ['layout.unit', '단원', 'text'], ['layout.type', '문항 유형', 'text'],
    ['layout.header', '머리말', 'text'], ['layout.footer', '꼬리말', 'text'], ['layout.page_start', '시작 쪽 번호', 'number', 1, 9999], ['layout.cover_number_visible', '표지 쪽 번호 표시', 'checkbox'],
  ];
  function set(document, path, value) { const keys = path.split('.'); let owner = document; for (const key of keys.slice(0, -1)) owner = owner[key]; owner[keys.at(-1)] = value; }
  function read(document, path) { return path.split('.').reduce((value, key) => value[key], document); }
  for (const [path, label, kind, min, max] of specifications) {
    const wrapper = document.createElement('label'); wrapper.textContent = label;
    const control = document.createElement(Array.isArray(kind) ? 'select' : kind === 'textarea' ? 'textarea' : 'input');
    control.setAttribute('aria-label', label); control.dataset.layoutField = path;
    if (Array.isArray(kind)) for (const entry of kind) { const [value, text] = entry.split('|'); const option = document.createElement('option'); option.value = value; option.textContent = text; control.append(option); }
    else { if (kind !== 'textarea') control.type = kind; control.maxLength = 16384; if (kind === 'number') { control.min = min; control.max = max; control.step = path === 'layout.page_start' ? '1' : '.5'; } }
    control.addEventListener('change', () => {
      const value = kind === 'checkbox' ? control.checked : kind === 'number' ? Number(control.value) : control.value;
      if (kind === 'number' && (!Number.isFinite(value) || value < min || value > max)) { status.textContent = '지면 치수의 허용 범위를 확인해 주세요.'; return; }
      options.change(document => { set(document, path, value); if (path === 'layout.paper' && value !== 'custom') { const size = value === 'A4' ? [210, 297] : [250, 353]; document.layout.width_mm = size[0]; document.layout.height_mm = size[1]; } });
    });
    if (Array.isArray(kind) || kind === 'textarea' || ['layout.header', 'layout.footer'].includes(path)) wrapper.classList.add('field-wide');
    if (kind === 'checkbox') wrapper.classList.add('checkbox-field');
    wrapper.append(control); fieldGroup(path).append(wrapper); fields.set(path, { wrapper, control, kind });
  }
  function select(label, parent) { const wrapper = document.createElement('label'); wrapper.className = 'field-wide'; wrapper.textContent = label; const control = document.createElement('select'); control.setAttribute('aria-label', label); wrapper.append(control); parent.append(wrapper); return control; }
  function button(label, action, parent) { const control = document.createElement('button'); control.type = 'button'; control.textContent = label; control.addEventListener('click', action); parent.append(control); return control; }
  const breaks = select('나눔 위치 문항', breakFields);
  button('선택 문항 앞 새 단 전환', () => { if (breaks.value) options.change(document => { const ids = document.layout.break_before; document.layout.break_before = ids.includes(breaks.value) ? ids.filter(id => id !== breaks.value) : [...ids, breaks.value]; }); }, breakFields);
  button('공통 지문 반복 배치 초기화', () => options.change(document => { document.layout.context_repeats = []; }), breakFields);
  const breakNote = document.createElement('p'); breakNote.className = 'document-help'; breakFields.append(breakNote);
  const figures = select('크기 조정 그림', figureFields);
  const figureDescription = document.createElement('p'); figureDescription.className = 'figure-description'; figureFields.append(figureDescription);
  for (const [key, label, max] of [['width_mm', '그림 너비(mm)', 400], ['height_mm', '그림 높이(mm) · 비우면 자동', 600]]) {
    const wrapper = document.createElement('label'); wrapper.textContent = label; const input = document.createElement('input'); input.type = 'number'; input.min = 1; input.max = max; input.setAttribute('aria-label', label);
    input.addEventListener('change', () => { if (!figures.value) return; const value = input.value === '' && key === 'height_mm' ? null : Number(input.value); if (value !== null && (!Number.isFinite(value) || value < 1 || value > max)) return;
      options.change(document => { document.content.nodes.find(node => node.id === figures.value).payload[key] = value; }); });
    wrapper.append(input); figureFields.append(wrapper); fields.set(`figure.${key}`, { control: input });
  }
  const aspect = document.createElement('input'); aspect.type = 'checkbox'; aspect.setAttribute('aria-label', '그림 비율 유지');
  const aspectLabel = document.createElement('label'); aspectLabel.textContent = '그림 비율 유지'; aspectLabel.className = 'checkbox-field'; aspectLabel.append(aspect); figureFields.append(aspectLabel);
  aspect.addEventListener('change', () => { if (figures.value) options.change(document => { document.content.nodes.find(node => node.id === figures.value).payload.keep_aspect = aspect.checked; }); });
  function figureValues(current) { const node = current.content.nodes.find(node => node.id === figures.value); for (const key of ['width_mm', 'height_mm']) fields.get(`figure.${key}`).control.value = node?.payload[key] ?? ''; aspect.checked = node?.payload.keep_aspect ?? true; figureDescription.textContent = node?.payload.alt_text ?? '선택한 그림이 없습니다.'; }
  figures.addEventListener('change', () => figureValues(options.snapshot()));
  function invalidate() { generation++; controller?.abort(); receipt = null; target.replaceChildren(); status.replaceChildren(); }
  async function build() {
    if (!options.beforePreview()) return;
    invalidate(); const version = generation; controller = new AbortController(); status.textContent = '현재 글꼴·그림·수식을 확인하고 지면을 계산하는 중입니다.';
    const current = options.snapshot();
    try {
      const result = await layoutDocument(current, target, controller.signal);
      if (generation !== version) return;
      receipt = result; status.textContent = `${result.pages.length}쪽 미리보기 · 문항은 단마다 최대 2개 · 저장 후 PDF를 만들 수 있습니다.`;
      target.scrollIntoView({ block: 'start' });
    } catch (error) {
      if (generation !== version) return;
      target.replaceChildren(); receipt = { status: 'blocked', code: error.code ?? 'layout_failed', nodeId: error.nodeId, measured: error.measured, available: error.available, proposal: error.proposal };
      status.textContent = `${error.code ?? error.message}${error.code === '글꼴 준비 실패' ? ` (${error.measured.font})` : ''}. ${error.code === '글꼴 준비 실패' ? '글꼴 파일과 연결을 확인한 뒤 문서를 다시 열거나 글꼴을 변경하세요.' : '용지·여백·글자·그림 크기·간격·나눔을 조정한 뒤 다시 계산하세요.'}`;
      const details = document.createElement('details'); const summary = document.createElement('summary'); summary.textContent = '위치와 실측 정보'; details.append(summary, document.createTextNode(JSON.stringify(receipt))); status.append(details);
      if (error.proposal) { const proposal = error.proposal; const confirm = document.createElement('button'); confirm.type = 'button'; confirm.textContent = `${proposal.page}쪽 ${proposal.column}단에 공통 지문 반복 허용`;
        confirm.addEventListener('click', () => { options.change(document => { document.layout.context_repeats = document.layout.context_repeats.filter(item => item.before_question_id !== proposal.before_question_id); document.layout.context_repeats.push(proposal); }); build(); }); status.append(confirm); }
    }
  }
  document.getElementById('document-preview-button').addEventListener('click', build);
  function sync(current) {
    for (const [path, field] of fields) { if (path.startsWith('figure.')) continue; if (field.kind === 'checkbox') field.control.checked = read(current, path); else field.control.value = read(current, path);
      if (['layout.width_mm', 'layout.height_mm'].includes(path)) field.wrapper.hidden = current.layout.paper !== 'custom'; }
    for (const [select, nodes, label] of [[breaks, current.content.nodes.filter(node => current.content.roots.includes(node.id) && node.kind === 'question'), node => `문항 ${node.payload.label}`],
      [figures, current.content.nodes.filter(node => node.kind === 'figure'), (node, index) => `그림 ${index + 1}`]]) {
      const selected = select.value; select.replaceChildren(); for (const [index, node] of nodes.entries()) { const option = document.createElement('option'); option.value = node.id; option.textContent = label(node, index); select.append(option); }
      if (nodes.some(node => node.id === selected)) select.value = selected;
    }
    breakNote.textContent = `새 단 ${current.layout.break_before.length}곳 · 승인한 공통 지문 반복 ${current.layout.context_repeats.length}곳`;
    figureValues(current);
  }
  window.documentLayout = { receipt: () => receipt ? structuredClone(receipt) : null };
  return { sync, invalidate };
}
