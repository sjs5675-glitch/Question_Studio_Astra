import { applyStyle, renderDocument } from './document-render.js';
import { outputDocument, solutionUnits } from './document-output.js';

const mm = 96 / 25.4;
const rounded = value => Math.round(value * 1000) / 1000;
export async function layoutHash(value) {
  const bytes = typeof value === 'string' ? new TextEncoder().encode(value) : value instanceof ArrayBuffer ? value : new TextEncoder().encode(JSON.stringify(value));
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(value => value.toString(16).padStart(2, '0')).join('');
}
export class LayoutError extends Error {
  constructor(code, id, measured, available, proposal = null) {
    super(`${code} · ${id} · 용지·여백·글자 크기·그림 크기·간격·나눔을 조정하세요.`);
    this.code = code; this.nodeId = id; this.measured = measured; this.available = available; this.proposal = proposal;
  }
}
function element(tag, className, text) { const node = document.createElement(tag); node.className = className; if (text != null) node.textContent = text; return node; }

export async function layoutDocument(current, target, signal, resources = {}) {
  const layout = current.layout, margins = layout.margins_mm;
  const width = (layout.width_mm - margins.left - margins.right - layout.column_gap_mm) * mm / 2;
  const usable = (layout.height_mm - margins.top - margins.bottom) * mm;
  if (width <= 0 || usable <= 0) throw new LayoutError('본문 영역 없음', 'layout', { width, height: usable }, {});
  const questions = current.output_role === 'explanation_only' ? null : resources.questions ?? outputDocument(current, 'questions');
  const solutions = current.output_role === 'question_only' ? null : resources.solutions ?? outputDocument(current, 'solutions');
  const views = [questions, solutions].filter(Boolean);
  const viewNodes = new Map(views.map(view => [view, new Map(view.content.nodes.map(node => [node.id, node]))]));
  const measure = element('div', 'document-print document-measure');
  applyStyle(measure, current.styles.defaults); measure.style.margin = '0';
  measure.dataset.template = layout.template; document.body.append(measure);
  const pages = [], assignments = [], repeated = [], fonts = [], usedRepeats = new Set(), seenContexts = new Set();
  const gap = layout.question_gap_mm * mm;
  let activePage, column = 0, used = 0, count = 0, contexts = new Set(), fontsReady = false;
  const deadline = AbortSignal.timeout(60000); const abort = signal ? AbortSignal.any([signal, deadline]) : deadline;
  const wait = promise => Promise.race([promise, new Promise((_, reject) => abort.addEventListener('abort', () => reject(abort.reason), { once: true }))]);
  function makePage(cover = false) {
    abort.throwIfAborted(); if (pages.length >= 200) throw new LayoutError('200쪽 한도', 'layout', { pages: pages.length + 1 }, { pages: 200 });
    const index = pages.length + 1; const page = element('section', 'document-layout-page');
    page.dataset.page = String(index); page.dataset.cover = String(cover); page.style.width = `${layout.width_mm}mm`; page.style.height = `${layout.height_mm}mm`;
    page.style.padding = `${margins.top}mm ${margins.right}mm ${margins.bottom}mm ${margins.left}mm`;
    const header = element('header', 'document-page-header', layout.header);
    const title = element('div', `document-page-title${cover ? ' document-cover-title' : ''}`);
    if (index === 1) {
      title.append(element('h1', '', layout.title), element('p', 'document-page-instructions', layout.instructions));
      if (layout.template === 'workbook') title.append(element('p', 'document-unit-title', [layout.unit, layout.type].filter(Boolean).join(' · ')));
    }
    const columns = element('div', 'document-layout-columns'); columns.style.columnGap = `${layout.column_gap_mm}mm`;
    for (const side of [1, 2]) { const col = element('div', 'document-layout-column'); col.dataset.column = String(side); col.style.gap = `${gap}px`; columns.append(col); }
    const footer = element('footer', 'document-page-footer'); footer.append(element('span', '', layout.footer));
    footer.append(element('span', 'document-page-number', cover && !layout.cover_number_visible ? '' : String(layout.page_start + index - 1)));
    page.append(header, title, columns, footer); measure.append(page); if (cover) columns.hidden = true;
    const chrome = header.getBoundingClientRect().height + title.getBoundingClientRect().height + footer.getBoundingClientRect().height;
    if (fontsReady && chrome >= usable) throw new LayoutError('제목·머리말 높이 초과', 'layout.title', { height: rounded(chrome) }, { height: rounded(usable) });
    const record = { element: page, columns: [...columns.children], index, cover, height: cover ? 0 : columns.getBoundingClientRect().height };
    pages.push(record); return record;
  }
  function advance(pageBreak = false) {
    if (pageBreak || column === 1) { activePage = makePage(); column = 0; } else column++;
    used = 0; count = 0; contexts = new Set();
  }
  function unit(view, roots, key, questionId, section, overrides = [], label = '') {
    const outer = element('div', 'document-layout-unit'); outer.dataset.unit = key; outer.dataset.questionId = questionId ?? ''; outer.dataset.section = section; outer.style.width = `${width}px`;
    if (label) outer.append(element('p', 'document-solution-label', label));
    const body = element('div', 'document-layout-content'); outer.append(body);
    const changed = new Map(overrides.map(node => [node.id, node])); const local = [];
    function collect(id) { const node = changed.get(id) ?? viewNodes.get(view).get(id); local.push(node); for (const child of node.children) collect(child); }
    for (const id of roots) collect(id);
    renderDocument(body, { ...view, content: { roots, nodes: local } }, { assetBase: resources.assetBase, compactPrint: true });
    body.style.margin = '0'; measure.append(outer);
    return { outer, roots, key, questionId, section, height: 0 };
  }
  function checkSize(item) {
    const bounds = item.outer.getBoundingClientRect(); item.height = bounds.height;
    for (const node of item.outer.querySelectorAll('img, .katex')) {
      const box = node.getBoundingClientRect();
      if (box.height > activePage.height + .75) throw new LayoutError('그림·수식 높이 초과', node.closest('[data-node-id]')?.dataset.nodeId ?? item.key,
        { height: rounded(box.height), width: rounded(box.width) }, { height: rounded(activePage.height), width: rounded(width) });
    }
    for (const node of item.outer.querySelectorAll('[data-node-id], .katex, img')) {
      const box = node.getBoundingClientRect();
      // Inline glyph bounds can overhang their wrapping box by a few pixels.
      const tolerance = node.dataset.kind === 'text' ? 4 : .75;
      if (box.right > bounds.left + width + tolerance || box.left < bounds.left - tolerance) throw new LayoutError('가로 넘침', node.closest('[data-node-id]')?.dataset.nodeId ?? item.key,
        { width: rounded(box.width), height: rounded(box.height) }, { width: rounded(width), height: rounded(activePage.height) });
    }
  }
  function place(item, numbered = false) {
    abort.throwIfAborted(); const itemGap = item.section === 'solutions' ? .5 * mm : gap;
    const needed = item.height + (used ? itemGap : 0);
    if (needed > activePage.height - used + .5 || numbered && count >= 2) throw new LayoutError('문항 높이 초과', item.questionId ?? item.key,
      { height: rounded(item.height), width: rounded(width) }, { height: rounded(activePage.height), width: rounded(width) });
    activePage.columns[column].append(item.outer); item.outer.style.width = '100%'; used += needed; if (numbered) count++;
    assignments.push({ page: activePage.index, column: column + 1, key: item.key, questionId: item.questionId, section: item.section,
      numbered, roots: item.roots, height: rounded(item.height), yEnd: rounded(used) });
  }
  try {
    await wait(document.fonts.ready); abort.throwIfAborted();
    if (layout.first_page === 'cover') makePage(true);
    activePage = makePage();
    const qNodes = questions ? new Map(questions.content.nodes.map(node => [node.id, node])) : new Map();
    const questionIds = questions?.content.roots.filter(id => qNodes.get(id).kind === 'question') ?? [];
    if (questionIds.length > 800) throw new LayoutError('200쪽 한도', 'content', { questions: questionIds.length }, { maximumQuestions: 800 });
    const questionItems = questionIds.map(id => unit(questions, [id], id, id, 'questions'));
    const contextItems = new Map((questions?.groups.flatMap(group => group.common_node_ids) ?? []).map(id => [id, unit(questions, [id], id, null, 'context')]));
    const seenSolutionQuestions = new Set();
    const solutionItems = solutions ? solutionUnits(solutions).map(item => {
      const label = seenSolutionQuestions.has(item.questionId) ? ''
        : `문항 ${item.label} · 답과 해설${item.missing ? ' · 등록된 내용 없음' : ''}`;
      seenSolutionQuestions.add(item.questionId);
      return unit(solutions, item.roots, item.key, item.questionId, 'solutions', item.overrides, label);
    }) : [];
    const requiredFonts = new Map(); const walker = document.createTreeWalker(measure, NodeFilter.SHOW_TEXT); let textNode;
    while ((textNode = walker.nextNode())) {
      const parent = textNode.parentElement; if (!textNode.textContent || parent.closest('.katex-mathml')) continue;
      const style = getComputedStyle(parent); if (style.display === 'none' || style.visibility === 'hidden') continue;
      const range = document.createRange(); range.selectNodeContents(textNode); if (![...range.getClientRects()].some(rect => rect.height)) continue;
      const descriptor = `${style.fontStyle} ${style.fontWeight} 16px ${style.fontFamily.split(',')[0]}`;
      const needed = requiredFonts.get(descriptor) ?? { text: '', ids: new Set() }; needed.text += textNode.textContent;
      needed.ids.add(parent.closest('[data-node-id]')?.dataset.nodeId ?? (parent.closest('header') ? 'layout.header' : parent.closest('footer') ? 'layout.footer' : 'layout.title'));
      requiredFonts.set(descriptor, needed);
    }
    for (const [descriptor, needed] of requiredFonts) {
      let loaded;
      try { loaded = await wait(document.fonts.load(descriptor, needed.text)); }
      catch (error) { if (abort.aborted) throw error; throw new LayoutError('글꼴 준비 실패', [...needed.ids][0], { font: descriptor }, {}); }
      if (!loaded.length || loaded.some(face => face.status !== 'loaded')) throw new LayoutError('글꼴 준비 실패', [...needed.ids][0], { font: descriptor }, {});
      fonts.push({ descriptor, nodeIds: [...needed.ids], textHash: await layoutHash(needed.text), faces: loaded.map(face => ({ family: face.family, style: face.style, weight: face.weight, unicodeRange: face.unicodeRange, status: face.status })) });
    }
    await wait(document.fonts.ready); await wait(Promise.all([...measure.querySelectorAll('img')].map(image => image.decode())));
    fontsReady = true;
    for (const page of pages) {
      const chrome = [...page.element.children].filter(node => !node.classList.contains('document-layout-columns')).reduce((total, node) => total + node.getBoundingClientRect().height, 0);
      if (chrome >= usable) throw new LayoutError('제목·머리말 높이 초과', 'layout.title', { height: rounded(chrome) }, { height: rounded(usable) });
      page.height = page.cover ? 0 : page.columns[0].parentElement.getBoundingClientRect().height;
    }
    if (measure.querySelector('.document-unresolved')) { const node = measure.querySelector('.document-unresolved').closest('[data-node-id]'); throw new LayoutError('수식 확인 필요', node.dataset.nodeId, {}, {}); }
    const assets = [...new Map(views.flatMap(view => view.assets).map(asset => [asset.id, asset])).values()];
    for (const asset of assets) {
      const response = await fetch(`${resources.assetBase ?? `/api/documents/${current.document_id}/assets`}/${asset.id}`, { signal: abort });
      if (!response.ok || await layoutHash(await response.arrayBuffer()) !== asset.sha256) throw new LayoutError('그림 무결성 오류', asset.id, {}, {});
    }
    for (const item of [...questionItems, ...contextItems.values(), ...solutionItems]) checkSize(item);
    for (let index = 0; index < questionItems.length; index++) {
      const item = questionItems[index]; const group = questions.groups.find(group => group.child_question_ids.includes(item.questionId));
      if (layout.break_before.includes(item.questionId) && used) advance();
      if (count >= 2) advance();
      const missing = () => (group?.common_node_ids ?? []).filter(id => !contexts.has(id));
      const contextHeight = () => missing().reduce((total, id) => total + contextItems.get(id).height + gap, 0);
      let needed = item.height + contextHeight() + (used ? gap : 0);
      const next = questionItems[index + 1];
      if (group && missing().length && next && group.child_question_ids.includes(next.questionId) && !layout.break_before.includes(next.questionId)) {
        const pair = item.height + contextHeight() + gap + next.height;
        if (pair <= activePage.height && used && (count + 2 > 2 || pair + gap > activePage.height - used)) advance();
      }
      needed = item.height + contextHeight() + (used ? gap : 0);
      if (needed > activePage.height - used + .5 && used) advance();
      needed = item.height + contextHeight() + (used ? gap : 0);
      if (needed > activePage.height + .5) throw new LayoutError('문항·공통 지문 높이 초과', item.questionId, { height: rounded(needed) }, { height: rounded(activePage.height) });
      const repeatIds = missing().filter(id => seenContexts.has(id));
      if (repeatIds.length) {
        const proposal = { group_id: group.id, page: activePage.index, column: column + 1, before_question_id: item.questionId };
        const found = layout.context_repeats.findIndex(record => ['group_id', 'page', 'column', 'before_question_id'].every(key => record[key] === proposal[key]));
        if (found < 0) throw new LayoutError('공통 지문 반복 승인 필요', item.questionId, { contextIds: repeatIds }, { page: proposal.page, column: proposal.column }, proposal);
        usedRepeats.add(found); repeated.push({ ...proposal, contextIds: repeatIds });
      }
      for (const id of missing()) { const original = contextItems.get(id); const copy = { ...original, outer: original.outer.cloneNode(true), key: `${id}@${activePage.index}:${column + 1}` }; place(copy); contexts.add(id); seenContexts.add(id); }
      place(item, true);
    }
    if (questions && usedRepeats.size !== layout.context_repeats.length) throw new LayoutError('이전 반복 배치 재확인 필요', 'layout.context_repeats', {}, {});
    for (const item of solutionItems) { if (used && item.height + .5 * mm > activePage.height - used) advance(); place(item); }
    const manifestResponse = await fetch('/dist/document/manifest.json', { signal: abort }); const manifestText = await manifestResponse.text();
    const manifest = JSON.parse(manifestText); const css = await (await fetch('/document.css', { signal: abort })).text();
    const roleContent = views.map(view => ({ role: view.output_role, content: view.content, assets: view.assets, groups: view.groups }));
    const identity = { documentId: current.document_id, revision: current.revision, currentHash: await layoutHash(current), outputHash: await layoutHash(roleContent),
      role: current.output_role, layoutHash: await layoutHash({ layout, styles: current.styles }), assetHashes: assets.map(asset => asset.sha256),
      snapshotHashes: current.imports.map(item => item.package_sha256), manifestHash: await layoutHash(manifestText), bundleHash: manifest.files.find(file => file.path === 'document.js').sha256,
      cssHash: await layoutHash(css), fontsHash: await layoutHash(fonts), renderer: 'document-render/layout-v1', assignmentsHash: await layoutHash(assignments) };
    abort.throwIfAborted(); target.replaceChildren(...pages.map(page => page.element)); target.classList.add('document-print'); applyStyle(target, current.styles.defaults); target.style.margin = '0'; target.dataset.template = layout.template;
    return { status: 'ready', identity, fonts, assignments, repeated, pages: pages.map(page => ({ page: page.index, cover: page.cover, availableHeight: rounded(page.height) })),
      selectedQuestionIds: current.content.roots.filter(id => { const node = current.content.nodes.find(node => node.id === id); return node?.kind === 'question' && node.payload.selected; }),
      outputNodeIds: [...new Set(views.flatMap(view => view.content.nodes.map(node => node.id)))], renderedNodeIds: [...new Set([...target.querySelectorAll('[data-node-id]')].map(node => node.dataset.nodeId))], roleContent };
  } finally { measure.remove(); }
}
