const marks = new Set(['bold', 'italic', 'underline', 'superscript', 'subscript']);
const clone = value => structuredClone(value);

export function paragraphProjection(document, paragraphId) {
  const nodes = new Map(document.content.nodes.map(node => [node.id, node]));
  const paragraph = nodes.get(paragraphId);
  if (paragraph?.kind !== 'paragraph') throw Error('편집할 문단이 없습니다.');
  const attrs = node => ({ original: clone(node), style: clone(document.styles.node_overrides[node.id] ?? {}) });
  const content = paragraph.children.map(id => {
    const node = nodes.get(id);
    switch (node.kind) {
      case 'text': return { type: 'text', text: node.payload.text, marks: [
        { type: 'identity', attrs: attrs(node) }, ...node.payload.marks.map(type => ({ type }))] };
      case 'equation': return { type: 'equation', attrs: attrs(node) };
      case 'line_break': return { type: 'hardBreak', attrs: attrs(node) };
      default: throw Error(`문단 안에 올 수 없는 형식: ${node.kind}`);
    }
  }).filter(node => node.type !== 'text' || node.text.length);
  return { type: 'doc', content: [{ type: 'paragraph', attrs: attrs(paragraph), content }] };
}

export function applyParagraphProjection(document, paragraphId, projection) {
  const result = clone(document);
  const original = result.content.nodes.find(node => node.id === paragraphId);
  if (!original || projection.type !== 'doc') throw Error('문단 투영 경계가 올바르지 않습니다.');
  const removed = new Set([paragraphId, ...original.children]);
  const existing = new Map(result.content.nodes.map(node => [node.id, node]));
  const used = new Set();
  const replacements = new Map();
  function materialize(kind, attributes, payload) {
    const source = attributes?.original;
    const retain = source && removed.has(source.id) && !used.has(source.id) && existing.get(source.id)?.kind === kind;
    const local = source && !existing.has(source.id) && !used.has(source.id);
    const id = retain || local ? source.id : crypto.randomUUID();
    used.add(id);
    const node = { id, kind, children: [], payload: clone(payload), provenance: retain ? clone(existing.get(id).provenance) : null };
    replacements.set(id, node);
    const style = attributes?.style ?? (source ? result.styles.node_overrides[source.id] : null);
    if (style && Object.keys(style).length) result.styles.node_overrides[id] = clone(style);
    return node;
  }
  const paragraphs = projection.content.map(block => {
    if (block.type !== 'paragraph') throw Error('문단만 편집할 수 있습니다.');
    const paragraph = materialize('paragraph', block.attrs, block.attrs?.original?.payload ?? { role: 'body' });
    for (const inline of block.content ?? []) {
      let node;
      switch (inline.type) {
        case 'text': {
          const identity = inline.marks?.find(mark => mark.type === 'identity');
          const selectedStyle = inline.marks?.find(mark => mark.type === 'textStyle');
          const attributes = clone(identity?.attrs ?? {});
          if (selectedStyle) attributes.style = { ...attributes.style, ...selectedStyle.attrs.style };
          node = materialize('text', attributes, { text: inline.text, marks: (inline.marks ?? []).filter(mark => marks.has(mark.type)).map(mark => mark.type) });
          break;
        }
        case 'equation': node = materialize('equation', inline.attrs, inline.attrs.original.payload); break;
        case 'hardBreak': node = materialize('line_break', inline.attrs, {}); break;
        default: throw Error(`지원하지 않는 인라인 형식: ${inline.type}`);
      }
      paragraph.children.push(node.id);
    }
    return paragraph.id;
  });
  const parent = result.content.nodes.find(node => node.children.includes(paragraphId));
  if (!parent) throw Error('문단의 소유자가 없습니다.');
  parent.children.splice(parent.children.indexOf(paragraphId), 1, ...paragraphs);
  result.content.nodes = result.content.nodes.flatMap(node => {
    if (!removed.has(node.id)) return [node];
    const replacement = replacements.get(node.id);
    replacements.delete(node.id);
    return replacement ? [replacement] : [];
  });
  result.content.nodes.push(...replacements.values());
  const live = new Set(result.content.nodes.map(node => node.id));
  result.styles.node_overrides = Object.fromEntries(Object.entries(result.styles.node_overrides).filter(([id]) => live.has(id)));
  return result;
}
