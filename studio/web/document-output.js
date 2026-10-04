export function answerLabels(current) {
  const nodes = new Map(current.content.nodes.map(node => [node.id, node]));
  const labels = new Map();
  for (const root of current.content.roots) {
    const choices = new Map(); const answers = [];
    function visit(id, solution = false) {
      const node = nodes.get(id); const inside = solution || ['answer', 'explanation'].includes(node.kind);
      if (node.kind === 'choice' && !inside) choices.set(node.payload.choice_id, node.payload.label);
      if (node.kind === 'answer') answers.push(node);
      for (const child of node.children) visit(child, inside);
    }
    visit(root);
    for (const node of answers) {
      const payload = node.payload;
      const text = payload.kind === 'choice' ? payload.choice_ids.map(id => choices.get(id) ?? '확인 필요').join(', ')
        : payload.value === true ? '참' : payload.value === false ? '거짓' : payload.text ?? '';
      labels.set(node.id, text);
    }
  }
  return labels;
}

export function outputDocument(current, section) {
  const labels = answerLabels(current);
  const nodes = new Map(current.content.nodes.map(node => [node.id, node]));
  const kept = [];
  const selected = current.content.roots.filter(id => nodes.get(id).kind === 'question' && nodes.get(id).payload.selected);
  function copy(id, solution = false) {
    const node = nodes.get(id); const inside = solution || ['answer', 'explanation'].includes(node.kind);
    if (section === 'questions' && inside) return null;
    const children = node.children.map(child => copy(child, inside)).filter(Boolean);
    if (section === 'solutions' && !inside && node.kind !== 'question') return null;
    const payload = structuredClone(node.payload);
    if (node.kind === 'question') payload.label = String(selected.indexOf(id) + 1);
    if (node.kind === 'answer') { payload.text = labels.get(id); payload.choice_ids = []; payload.label ??= payload.part_id; }
    kept.push({ id, kind: node.kind, children, payload, provenance: null });
    return id;
  }
  const common = new Set(current.groups.filter(group => group.child_question_ids.some(id => selected.includes(id))).flatMap(group => group.common_node_ids));
  const roots = current.content.roots.filter(id => selected.includes(id) || section === 'questions' && common.has(id)).map(id => copy(id)).filter(Boolean);
  const ids = new Set(kept.map(node => node.id));
  const assetIds = new Set(kept.filter(node => node.kind === 'figure').map(node => node.payload.asset_id));
  return { document_id: current.document_id, revision: current.revision, output_role: section, content: { roots, nodes: kept },
    groups: section === 'questions' ? current.groups.map(group => ({ ...group, child_question_ids: group.child_question_ids.filter(id => selected.includes(id)) })).filter(group => group.child_question_ids.length) : [],
    styles: { defaults: structuredClone(current.styles.defaults), node_overrides: Object.fromEntries(Object.entries(current.styles.node_overrides).filter(([id]) => ids.has(id))) },
    layout: structuredClone(current.layout), assets: current.assets.filter(asset => assetIds.has(asset.id)).map(asset => structuredClone(asset)) };
}

export function solutionUnits(view) {
  const nodes = new Map(view.content.nodes.map(node => [node.id, node])); const result = [];
  for (const id of view.content.roots) {
    const question = nodes.get(id);
    for (const child of question.children) {
      const node = nodes.get(child);
      if (!node.children.length) result.push({ key: child, questionId: id, label: question.payload.label, roots: [child], overrides: [] });
      else for (const block of node.children) result.push({ key: `${child}/${block}`, questionId: id, label: question.payload.label, roots: [child], overrides: [{ ...node, children: [block] }] });
    }
    if (!question.children.length) result.push({ key: `${id}/missing`, questionId: id, label: question.payload.label, roots: [], overrides: [], missing: true });
  }
  return result;
}

export function outputViews(current) {
  const questions = current.output_role === 'explanation_only' ? null : outputDocument(current, 'questions');
  const solutions = current.output_role === 'question_only' ? null : outputDocument(current, 'solutions');
  const views = [questions, solutions].filter(Boolean), nodes = new Map();
  for (const view of views) for (const node of view.content.nodes) {
    const previous = nodes.get(node.id);
    nodes.set(node.id, previous ? { ...node, children: [...new Set([...previous.children, ...node.children])] } : node);
  }
  return { questions, solutions, document: { ...current,
    content: { roots: [...new Set(views.flatMap(view => view.content.roots))], nodes: [...nodes.values()] },
    assets: [...new Map(views.flatMap(view => view.assets).map(asset => [asset.id, asset])).values()], groups: questions?.groups ?? [] } };
}
