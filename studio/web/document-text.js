import { Editor, Extension, Node, Mark } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import { Plugin } from '@tiptap/pm/state';
import { paragraphProjection, applyParagraphProjection } from './document-projection.js';
import { applyStyle, renderEquation } from './document-render.js';

const attributes = () => ({ original: { default: null }, style: { default: {} } });
function styleAttribute(style) { const span = document.createElement('span'); applyStyle(span, style); return span.getAttribute('style') ?? ''; }
const Identity = Mark.create({ name: 'identity', inclusive: true, addAttributes: attributes,
  renderHTML: ({ mark }) => ['span', { style: styleAttribute(mark.attrs.style) }, 0] });
const TextStyle = Mark.create({ name: 'textStyle', addAttributes: () => ({ style: { default: {} } }),
  renderHTML: ({ mark }) => ['span', { style: styleAttribute(mark.attrs.style) }, 0] });
const Paragraph = Node.create({ name: 'paragraph', group: 'block', content: 'inline*', addAttributes: attributes,
  parseHTML: () => [{ tag: 'p' }], renderHTML: ({ node }) => ['p', { style: styleAttribute(node.attrs.style) }, 0] });
const HardBreak = Node.create({ name: 'hardBreak', inline: true, group: 'inline', selectable: false, addAttributes: attributes,
  parseHTML: () => [{ tag: 'br' }], renderHTML: () => ['br'],
  addKeyboardShortcuts() { return { 'Shift-Enter': () => this.editor.commands.insertContent({ type: 'hardBreak' }) }; } });
const scriptMark = (name, tag) => Mark.create({ name, excludes: 'superscript subscript', parseHTML: () => [{ tag }], renderHTML: () => [tag, 0] });

export function mountTextEditor(options) {
  const { current, id, element, changed, math, error } = options;
  let editor;
  let endingComposition = false;
  const Equation = Node.create({ name: 'equation', inline: true, group: 'inline', atom: true, selectable: true,
    addAttributes: attributes, renderHTML: () => ['span', { 'data-math-atom': '' }],
    addNodeView() { return ({ node }) => {
      const dom = document.createElement('span'); renderEquation(dom, node.attrs.original);
      dom.contentEditable = 'false'; dom.tabIndex = 0; dom.setAttribute('role', 'button'); dom.setAttribute('aria-label', '수식 편집');
      dom.addEventListener('click', event => { event.stopPropagation(); math(node.attrs.original.id); });
      dom.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); math(node.attrs.original.id); } });
      return { dom };
    }; } });
  const IdentityNormalizer = Extension.create({ name: 'identityNormalizer',
    addProseMirrorPlugins() { return [new Plugin({ appendTransaction(transactions, oldState, state) {
      if (editor?.view.composing || !transactions.some(transaction => transaction.docChanged || transaction.getMeta('normalize'))) return null;
      const used = new Set(); const tr = state.tr;
      state.doc.descendants((node, position) => {
        if (node.isText) {
          const identity = node.marks.find(mark => mark.type.name === 'identity');
          const original = identity?.attrs.original;
          if (original && !used.has(original.id)) { used.add(original.id); return; }
          const fresh = { id: crypto.randomUUID(), kind: 'text', children: [], payload: {}, provenance: null };
          used.add(fresh.id);
          tr.addMark(position, position + node.nodeSize, state.schema.marks.identity.create({ original: fresh, style: identity?.attrs.style ?? {} }));
        } else if (node.type.name !== 'doc') {
          const original = node.attrs.original;
          if (original && !used.has(original.id)) { used.add(original.id); return; }
          if (node.type.name === 'equation') throw Error('수식 소유권이 중복되었습니다.');
          const fresh = { id: crypto.randomUUID(), kind: node.type.name === 'hardBreak' ? 'line_break' : 'paragraph', children: [], payload: node.type.name === 'paragraph' ? { role: 'body' } : {}, provenance: null };
          used.add(fresh.id);
          tr.setNodeMarkup(position, undefined, { ...node.attrs, original: fresh });
        }
      });
      return tr.docChanged ? tr : null;
    } })]; } });
  function publish() {
    if (editor.view.composing) return;
    try { changed(applyParagraphProjection(current, id, editor.getJSON())); }
    catch (failure) { error(failure.message); }
  }
  element.replaceChildren();
  editor = new Editor({ element, extensions: [StarterKit.configure({ paragraph: false, hardBreak: false, undoRedo: false,
    blockquote: false, bulletList: false, orderedList: false, listItem: false, listKeymap: false, heading: false, horizontalRule: false,
    codeBlock: false, code: false, link: false, strike: false, trailingNode: false }), Paragraph, HardBreak, Identity, TextStyle,
    scriptMark('superscript', 'sup'), scriptMark('subscript', 'sub'), Equation, IdentityNormalizer],
    content: paragraphProjection(current, id),
    editorProps: { attributes: { 'aria-label': '본문 편집', spellcheck: 'false' },
      handlePaste(view, event) {
        const html = event.clipboardData?.getData('text/html');
        if (!html) return false;
        const parsed = new DOMParser().parseFromString(html, 'text/html');
        const safe = new Set(['P', 'BR', 'STRONG', 'B', 'EM', 'I', 'U', 'SUP', 'SUB', 'SPAN', 'DIV']);
        if ([...parsed.body.querySelectorAll('*')].some(node => !safe.has(node.tagName) || [...node.attributes].some(attribute => /^(on|src$|href$)/i.test(attribute.name))) || parsed.head.children.length) {
          event.preventDefault(); error('스크립트·외부 주소·임의 스타일이 포함된 붙여넣기를 거부했습니다.'); return true;
        }
        return false;
      },
      transformPastedHTML(html) {
        const parsed = new DOMParser().parseFromString(html, 'text/html');
        for (const node of parsed.body.querySelectorAll('*')) for (const attribute of [...node.attributes]) node.removeAttribute(attribute.name);
        return parsed.body.innerHTML;
      },
      handleDOMEvents: { compositionend() {
        endingComposition = true;
        queueMicrotask(() => { if (!editor.isDestroyed) { editor.view.dispatch(editor.state.tr.setMeta('normalize', true)); publish(); } endingComposition = false; });
        return false;
      } } },
    onUpdate: () => { if (!endingComposition) publish(); },
  });
  editor.commands.focus('end');
  return editor;
}
