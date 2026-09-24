import { Editor } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import { MathfieldElement } from 'mathlive';
import katex from 'katex';
import 'katex/dist/katex.min.css';
import '@fontsource/noto-sans-kr/400.css';
import '@fontsource/noto-serif-kr/400.css';
import '@fontsource/noto-sans-kr/700.css';
import '@fontsource/noto-serif-kr/700.css';

if (document.querySelector('#document-formulas-root')) {
  import('./document-formulas.js').then(module => module.renderFormulas());
} else if (document.querySelector('#document-print-root')) {
  import('./document-print.js').then(module => module.printDocument());
} else if (document.querySelector('#document-app')) {
  import('./document-editor.js').then(module => module.startDocumentEditor());
} else {
MathfieldElement.fontsDirectory = '/assets/mathlive-fonts';
MathfieldElement.soundsDirectory = null;
MathfieldElement.openUrl = () => {};
const examples = [
  { name: '분수', latex: '\\frac{1}{2}' },
  { name: '근호', latex: '\\sqrt{3}' },
  { name: '벡터', latex: '\\vec{v}' },
  { name: '행렬', latex: '\\begin{pmatrix}1&2\\\\3&4\\end{pmatrix}' },
];
const state = { mounted: false, committed: examples.map(example => example.latex), applies: 0, errors: [] };
const bodyEditor = new Editor({ element: document.querySelector('#body-editor'), extensions: [StarterKit],
  content: { type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text: '한글 수식 출력 검증. 물체의 속도와 힘을 구하시오.' }] }] } });
const field = new MathfieldElement();
field.setAttribute('aria-label', '수식 초안');
field.mathVirtualKeyboardPolicy = 'manual';
field.smartMode = false;
field.smartFence = false;
field.smartSuperscript = false;
field.removeExtraneousParentheses = false;
let active = 0;
let original = state.committed[0];
let baseline = '';
const options = { throwOnError: true, trust: false, strict: 'error', maxExpand: 1000, maxSize: 20, displayMode: true };
function render() {
  document.querySelectorAll('[data-formula]').forEach((element, index) => katex.render(state.committed[index], element, { ...options, macros: {} }));
  document.querySelector('#print-body').textContent = bodyEditor.getText({ blockSeparator: '\n' });
  document.querySelector('#status').textContent = `적용 ${state.applies}회 · 인쇄에는 확정된 수식만 사용합니다.`;
}
function open(index) {
  active = index;
  original = state.committed[index];
  field.setValue(original, { silenceNotifications: true });
  baseline = field.getValue('latex');
  document.querySelector('#draft-name').textContent = `${examples[index].name} 초안`;
}
field.addEventListener('mount', () => { field.inlineShortcuts = {}; state.mounted = true; open(0); render(); });
document.querySelector('#math-editor').append(field);
for (const [index, example] of examples.entries()) {
  const button = document.createElement('button');
  button.textContent = example.name;
  button.type = 'button';
  button.addEventListener('click', () => open(index));
  document.querySelector('#palette').append(button);
}
document.querySelector('#apply').addEventListener('click', () => {
  try {
    if (!state.mounted || !field.isConnected) throw Error('수식 편집기가 준비되지 않았습니다.');
    const candidate = field.getValue('latex');
    if (candidate.length > 16384 || field.errors.length || candidate.includes('\\placeholder')) throw Error('수식을 완성해 주세요.');
    katex.renderToString(candidate, { ...options, macros: {} });
    state.committed[active] = candidate === baseline ? original : candidate;
    state.applies++;
    render();
  } catch (error) { state.errors.push(String(error)); document.querySelector('#status').textContent = String(error); }
});
document.querySelector('#cancel').addEventListener('click', () => open(active));
bodyEditor.on('update', render);
window.documentPilot = { state, snapshot: () => ({ ...state, body: bodyEditor.getJSON(), mathErrors: field.errors, mountedValue: field.getValue('latex') }) };
}
