import { validateTex } from './document-math-policy.js';
import { answerLabels } from './document-output.js';

export function applyStyle(element, style) {
  if (style.font_family) element.style.fontFamily = style.font_family;
  if (style.font_size_pt) element.style.fontSize = `${style.font_size_pt}pt`;
  if (style.line_spacing) element.style.lineHeight = String(style.line_spacing);
  if (style.paragraph_spacing_mm != null) element.style.marginBottom = `${style.paragraph_spacing_mm}mm`;
  if (style.align) element.style.textAlign = style.align;
  if (style.bold) element.style.fontWeight = '700';
  if (style.italic) element.style.fontStyle = 'italic';
  if (style.underline) element.style.textDecoration = 'underline';
  if (style.math_size) element.style.fontSize = `${style.math_size}em`;
  if (style.math_variant) element.dataset.mathVariant = style.math_variant;
}

export function renderEquation(element, node) {
  element.replaceChildren();
  element.classList.add('document-equation');
  element.dataset.nodeId = node.id;
  const payload = node.payload;
  let state = payload.authority;
  if (payload.authority === 'plain_literal') {
    element.textContent = payload.plain_text;
    return;
  }
  if (payload.authority === 'ordinary_tex') {
    try {
      // Only the fixed, untrusted-input KaTeX renderer may create math markup.
      element.innerHTML = validateTex(payload.latex, payload.display);
      return;
    } catch (error) { state = 'unsupported_tex'; element.title = error.message; }
  }
  const label = document.createElement('span');
  label.className = 'document-unresolved';
  label.textContent = `${state} · ${node.id.slice(0, 8)} · 확인 필요`;
  const raw = document.createElement('code');
  raw.textContent = payload.latex ?? payload.plain_text ?? payload.hwp_script ?? '';
  element.append(label, raw);
}

function shortChoiceGrid(node, nodes) {
  if (!["horizontal", "grid"].includes(node.payload.source_layout)) return false;
  return node.children.every((id) => {
    const choice = nodes.get(id);
    if (choice?.kind !== "choice" || choice.children.length !== 1) return false;
    const paragraph = nodes.get(choice.children[0]);
    if (paragraph?.kind !== "paragraph") return false;
    let weight = 0;
    for (const inlineId of paragraph.children) {
      const inline = nodes.get(inlineId);
      if (inline?.kind === "equation") weight += 8;
      else if (inline?.kind === "text" && !/[\r\n]/.test(inline.payload.text)) {
        weight += inline.payload.text.replace(/\s/g, "").length;
      } else return false;
    }
    return weight <= 36;
  });
}

export function renderDocument(target, current, handlers = {}) {
  const nodes = new Map(current.content.nodes.map(node => [node.id, node]));
  const answers = answerLabels(current);
  target.replaceChildren();
  const { paragraph_spacing_mm, ...defaults } = current.styles.defaults;
  applyStyle(target, defaults);
  function render(id, parentKind) {
    const node = nodes.get(id);
    const tag = node.kind === 'paragraph' ? (parentKind === 'table' ? 'caption' : 'p') : ({ text: 'span', line_break: 'br', table: 'table', row: 'tr', cell: 'td', equation: 'span' }[node.kind] ?? 'div');
    const element = document.createElement(tag);
    element.dataset.nodeId = id;
    element.dataset.kind = node.kind;
    if (node.kind === 'box') {
      element.dataset.boxRole = node.payload.role;
      if (node.payload.role === 'choices' && shortChoiceGrid(node, nodes)) element.dataset.choiceGrid = 'true';
    }
    element.className = `document-node document-${node.kind}`;
    if (node.kind === 'paragraph') applyStyle(element, { paragraph_spacing_mm });
    applyStyle(element, current.styles.node_overrides[id] ?? {});
    switch (node.kind) {
      case 'text':
        element.textContent = node.payload.text;
        for (const mark of node.payload.marks) element.classList.add(`mark-${mark}`);
        break;
      case 'equation':
        renderEquation(element, node);
        if (handlers.math) { element.setAttribute('role', 'button'); element.tabIndex = 0; }
        element.setAttribute('aria-label', `수식 편집 ${id.slice(0, 8)}`);
        element.addEventListener('click', event => { event.stopPropagation(); handlers.math?.(id); });
        element.addEventListener('keydown', event => { if (['Enter', ' '].includes(event.key)) { event.preventDefault(); handlers.math?.(id); } });
        break;
      case 'figure': {
        const image = document.createElement('img');
        image.src = `${handlers.assetBase ?? `/api/documents/${current.document_id}/assets`}/${node.payload.asset_id}`;
        image.alt = node.payload.alt_text;
        if (node.payload.width_mm) image.style.width = `${node.payload.width_mm}mm`;
        if (node.payload.height_mm) image.style.height = `${node.payload.height_mm}mm`;
        image.style.objectFit = node.payload.keep_aspect ? 'contain' : 'fill';
        element.append(image);
        break;
      }
      case 'question': case 'choice': case 'box': case 'answer': {
        const label = document.createElement('span'); label.className = 'document-label';
        label.textContent = node.kind === 'question' ? (handlers.compactPrint ? `${node.payload.label}.` : `문항 ${node.payload.label}`) : node.kind === 'box' ? (node.payload.title_visible ? node.payload.title : '') : node.kind === 'answer' ? `답 ${node.payload.label ?? node.payload.part_id ?? ''} ${node.payload.text ?? answers.get(id) ?? ''}` : node.payload.label;
        element.append(label); break;
      }
      case 'table': element.setAttribute('role', 'table'); break;
      case 'row': element.setAttribute('role', 'row'); break;
      case 'cell': element.rowSpan = node.payload.row_span; element.colSpan = node.payload.col_span; break;
      case 'paragraph':
        if (handlers.paragraph) { element.tabIndex = 0; element.setAttribute('aria-label', '문단 편집'); }
        element.addEventListener('focus', () => handlers.paragraph?.(id, element));
        element.addEventListener('click', () => handlers.paragraph?.(id, element));
        break;
      case 'explanation': element.setAttribute('aria-label', '해설'); break;
      case 'line_break': break;
      default: throw Error(`알 수 없는 문서 형식: ${node.kind}`);
    }
    for (const child of node.children) element.append(render(child, node.kind));
    if (handlers.move && ['question', 'figure'].includes(node.kind)) {
      const controls = document.createElement('span'); controls.className = 'document-move'; controls.contentEditable = 'false';
      for (const [direction, label] of [[-1, '위로'], [1, '아래로']]) {
        const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
        button.setAttribute('aria-label', `${node.kind} ${label}`);
        button.addEventListener('click', () => handlers.move(id, direction)); controls.append(button);
      }
      element.append(controls); element.draggable = true;
      const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '삭제'; remove.setAttribute('aria-label', `${node.kind} 삭제`);
      remove.addEventListener('click', () => handlers.remove?.(id)); controls.append(remove);
      element.addEventListener('dragstart', event => { event.dataTransfer.setData('application/x-document-node', id); });
      element.addEventListener('dragover', event => event.preventDefault());
      element.addEventListener('drop', event => { event.preventDefault(); handlers.drop?.(event.dataTransfer.getData('application/x-document-node'), id); });
    }
    return element;
  }
  for (const id of current.content.roots) target.append(render(id));
}
