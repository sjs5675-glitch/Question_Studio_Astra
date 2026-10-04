import assert from 'node:assert/strict';
import test from 'node:test';
import { outputDocument } from '../document-output.js';

function fixture() {
  const answer = (id, part_id, value) => ({ id, kind: 'answer', children: [], provenance: null,
    payload: { kind: 'true_false', label: null, text: null, choice_ids: [], value, part_id } });
  return { document_id: 'document', revision: 1, output_role: 'explanation_only', groups: [], assets: [],
    styles: { defaults: {}, node_overrides: {} }, layout: {}, content: { roots: ['question'], nodes: [
      { id: 'question', kind: 'question', children: ['stem', 'answers'], payload: { label: '1', selected: true }, provenance: null },
      { id: 'stem', kind: 'paragraph', children: ['text'], payload: { role: 'body' }, provenance: null },
      { id: 'text', kind: 'text', children: [], payload: { text: 'STATEMENT_A then STATEMENT_B', marks: [] }, provenance: null },
      { id: 'answers', kind: 'answer', children: ['answer-b', 'answer-a'], provenance: null,
        payload: { kind: 'true_false', label: null, text: null, choice_ids: [], value: null, part_id: null } },
      answer('answer-b', 'STATEMENT_B', false), answer('answer-a', 'STATEMENT_A', true),
    ] } };
}
const parts = view => view.content.nodes.filter(node => node.kind === 'answer' && node.payload.part_id);

test('true/false verdicts keep source identifiers when answer and question order differ', () => {
  const current = fixture();
  const before = structuredClone(current);
  const view = outputDocument(current, 'solutions');
  assert.deepEqual(parts(view).map(node => [node.payload.label, node.payload.text]), [['STATEMENT_B', '거짓'], ['STATEMENT_A', '참']]);
  assert.deepEqual(current, before);
});

test('explicit printed and intentionally empty labels take precedence over source identifiers', () => {
  const current = fixture();
  const answer = current.content.nodes.find(node => node.id === 'answer-b');
  answer.payload.label = '표시 ㄴ';
  assert.equal(parts(outputDocument(current, 'solutions'))[0].payload.label, '표시 ㄴ');
  answer.payload.label = '';
  assert.equal(parts(outputDocument(current, 'solutions'))[0].payload.label, '');
});

test('question-only output excludes true/false verdicts without dropping the statement text', () => {
  const view = outputDocument(fixture(), 'questions');
  assert.equal(view.content.nodes.some(node => node.kind === 'answer'), false);
  assert.equal(view.content.nodes.find(node => node.id === 'text').payload.text, 'STATEMENT_A then STATEMENT_B');
});

test('question output numbers follow selected root order without changing source labels', () => {
  const current = fixture();
  const first = current.content.nodes.find(node => node.id === 'question');
  first.payload.label = '492';
  const second = structuredClone(first); second.id = 'question-2'; second.payload.label = '7'; second.children = [];
  current.content.nodes.push(second); current.content.roots = ['question-2', 'question'];
  const view = outputDocument(current, 'questions');
  const questions = view.content.nodes.filter(node => node.kind === 'question');
  assert.deepEqual(questions.map(node => [node.id, node.payload.label]), [['question-2', '1'], ['question', '2']]);
  assert.equal(first.payload.label, '492');
  assert.equal(second.payload.label, '7');
});
