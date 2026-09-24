import { layoutDocument } from './document-layout.js';

export async function printDocument() {
  window.documentPrint = { status: 'loading' };
  try {
    const response = await fetch('/print-data');
    if (!response.ok) throw Error('인쇄 데이터를 읽을 수 없습니다.');
    const { document: current, currentHash, questions, solutions } = await response.json();
    const rule = document.createElement('style');
    rule.textContent = `@page { size: ${Number(current.layout.width_mm)}mm ${Number(current.layout.height_mm)}mm; margin: 0; }`;
    document.head.append(rule);
    const receipt = await layoutDocument(current, document.getElementById('document-print-root'), undefined, { assetBase: '/print-assets', questions, solutions });
    receipt.identity.currentHash = currentHash;
    const outside = [...document.querySelectorAll('.document-layout-unit')].filter(unit => {
      const bounds = unit.getBoundingClientRect(), column = unit.parentElement.getBoundingClientRect();
      return bounds.left < column.left - .75 || bounds.right > column.right + .75 || bounds.bottom > column.bottom + .75;
    });
    if (outside.length) throw Error('인쇄 영역을 벗어난 문항이 있습니다.');
    window.documentPrint = { ...receipt, geometry: { outside: 0 } };
  } catch (error) {
    document.getElementById('document-print-root').replaceChildren();
    window.documentPrint = { status: 'blocked', code: error.code ?? 'document_layout_failed', nodeId: error.nodeId ?? null, message: error.message };
  }
}
