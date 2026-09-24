import assert from 'node:assert/strict';

const { paragraphProjection, applyParagraphProjection } = await import('../document-projection.js');
const paragraph = { id: 'p', kind: 'paragraph', children: ['t', 'e', 'b'], payload: { role: 'body' }, provenance: null };
const text = { id: 't', kind: 'text', children: [], payload: { text: '  한글 A  ', marks: ['bold'] }, provenance: null };
const equation = { id: 'e', kind: 'equation', children: [], payload: { latex: '\\frac{1}{2}', authority: 'ordinary_tex' }, provenance: null };
const line = { id: 'b', kind: 'line_break', children: [], payload: {}, provenance: null };
const document = { content: { roots: ['q'], nodes: [{ id: 'q', kind: 'question', children: ['p'], payload: {}, provenance: null }, paragraph, text, equation, line] }, styles: { defaults: {}, node_overrides: { t: { font_size_pt: 22 } } } };
const projection = paragraphProjection(document, 'p');
assert.equal(projection.content[0].content[0].text, '  한글 A  ');
assert.deepEqual(applyParagraphProjection(document, 'p', projection), document);
const changed = structuredClone(projection);
changed.content[0].content[0].text = '  한글 B  ';
const current = applyParagraphProjection(document, 'p', changed);
assert.equal(current.content.nodes.find(node => node.id === 't').payload.text, '  한글 B  ');
assert.equal(current.styles.node_overrides.t.font_size_pt, 22);
assert.deepEqual(current.content.nodes.find(node => node.id === 'e'), equation);
assert.equal(document.content.nodes.find(node => node.id === 't').payload.text, '  한글 A  ');
console.log('PASS: exact spaces, marks, equation, stable ID, style and immutable source projection');
