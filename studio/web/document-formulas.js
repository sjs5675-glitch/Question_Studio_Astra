import { renderDocument } from './document-render.js';
import { layoutHash } from './document-layout.js';

export async function renderFormulas() {
  window.documentFormulas = { status: 'loading' };
  try {
    const { document: current, equationIds } = await (await fetch('/print-data')).json();
    const root = document.getElementById('document-formulas-root'); root.style.background = 'white';
    root.style.width = `${(current.layout.width_mm - current.layout.margins_mm.left - current.layout.margins_mm.right - current.layout.column_gap_mm) / 2}mm`;
    renderDocument(root, current, { assetBase: '/print-assets' });
    for (const id of equationIds) {
      const node = current.content.nodes.find(node => node.id === id);
      if (!node || node.payload.authority !== 'ordinary_tex') throw Error('현재 TeX 수식을 확인해 주세요.');
      const formula = root.querySelector(`[data-node-id="${id}"]`);
      if (formula.querySelector('.document-unresolved')) throw Error(`수식을 렌더링할 수 없습니다: ${id}`);
      const baseline = document.createElement('span'); baseline.dataset.baselineFor = id; baseline.style.cssText = 'display:inline-block;width:0;height:0;padding:0';
      formula.after(baseline);
    }
    await document.fonts.ready;
    const needed = new Map();
    for (const id of equationIds) {
      const walker = document.createTreeWalker(root.querySelector(`[data-node-id="${id}"]`), NodeFilter.SHOW_TEXT); let node;
      while ((node = walker.nextNode())) {
      if (!node.textContent || node.parentElement.closest('.katex-mathml')) continue;
      const style = getComputedStyle(node.parentElement);
      const descriptor = `${style.fontStyle} ${style.fontWeight} 16px ${style.fontFamily.split(',')[0]}`;
      needed.set(descriptor, (needed.get(descriptor) ?? '') + node.textContent);
      }
    }
    const fonts = [];
    for (const [descriptor, text] of needed) {
      let faces;
      try { faces = await document.fonts.load(descriptor, text); }
      catch (error) { throw new Error(`필수 수식 글꼴을 불러오지 못했습니다: ${descriptor}. 로컬 문서 글꼴 파일을 확인하고 다시 빌드해 주세요.`, { cause: error }); }
      if (!faces.length || faces.some(face => face.status !== 'loaded')) throw Error(`글꼴 준비 실패: ${descriptor}`);
      fonts.push({ descriptor, textHash: await layoutHash(text), faces: faces.map(face => ({ family: face.family, status: face.status, weight: face.weight, unicodeRange: face.unicodeRange })) });
    }
    await document.fonts.ready;
    await Promise.all([...root.querySelectorAll('img')].map(image => image.decode()));
    const formulas = equationIds.map(id => {
      const box = root.querySelector(`[data-node-id="${id}"]`).getBoundingClientRect();
      const baseline = root.querySelector(`[data-baseline-for="${id}"]`).getBoundingClientRect().top - box.top;
      if (box.width <= 0 || box.height <= 0 || baseline < 0 || baseline > box.height) throw Error(`수식 크기 또는 기준선 오류: ${id}`);
      return { node_id: id, css_width: box.width, css_height: box.height, baseline_css: baseline };
    });
    window.documentFormulas = { status: 'ready', fonts, formulas };
  } catch (error) { window.documentFormulas = { status: 'blocked', message: error.message, diagnostic: error.cause ? { name: error.cause.name, message: error.cause.message } : null }; }
}
