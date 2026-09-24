export class DocumentSession {
  constructor(document) { this.current = structuredClone(document); this.undoStack = []; this.redoStack = []; this.saved = JSON.stringify(document); }
  commit(document) {
    if (JSON.stringify(document) === JSON.stringify(this.current)) return false;
    this.undoStack.push(this.current); if (this.undoStack.length > 100) this.undoStack.shift();
    this.current = structuredClone(document); this.redoStack = []; return true;
  }
  change(action) { const next = structuredClone(this.current); action(next); return this.commit(next); }
  undo() { if (!this.undoStack.length) return; this.redoStack.push(this.current); this.current = this.undoStack.pop(); }
  redo() { if (!this.redoStack.length) return; this.undoStack.push(this.current); this.current = this.redoStack.pop(); }
  get dirty() { return JSON.stringify(this.current) !== this.saved; }
  savedDocument(document) {
    for (const frame of [...this.undoStack, ...this.redoStack]) { frame.revision = document.revision; frame.updated_at = document.updated_at; }
    this.current = structuredClone(document); this.saved = JSON.stringify(document);
  }
  remove(id) {
    return this.change(document => {
      const nodes = new Map(document.content.nodes.map(node => [node.id, node]));
      if (!['question', 'figure'].includes(nodes.get(id)?.kind)) throw Error('문항 또는 그림을 선택해 주세요.');
      const removed = new Set();
      function subtree(nodeId) { removed.add(nodeId); for (const child of nodes.get(nodeId).children) subtree(child); }
      subtree(id);
      const oldGroups = document.groups;
      document.groups = oldGroups.map(group => ({ ...group, child_question_ids: group.child_question_ids.filter(child => !removed.has(child)) })).filter(group => group.child_question_ids.length);
      const retainedContext = new Set(document.groups.flatMap(group => group.common_node_ids));
      for (const group of oldGroups) for (const common of group.common_node_ids) if (!retainedContext.has(common)) subtree(common);
      document.content.roots = document.content.roots.filter(nodeId => !removed.has(nodeId));
      document.content.nodes = document.content.nodes.filter(node => !removed.has(node.id));
      for (const node of document.content.nodes) node.children = node.children.filter(child => !removed.has(child));
      document.styles.node_overrides = Object.fromEntries(Object.entries(document.styles.node_overrides).filter(([nodeId]) => !removed.has(nodeId)));
      document.layout.break_before = document.layout.break_before.filter(nodeId => !removed.has(nodeId));
      document.layout.context_repeats = document.layout.context_repeats.filter(repeat => !removed.has(repeat.before_question_id));
    });
  }
  move(id, directionOrTarget) {
    return this.change(document => {
      const byId = new Map(document.content.nodes.map(node => [node.id, node]));
      const source = byId.get(id);
      if (!source || !['question', 'figure'].includes(source.kind)) throw Error('문항 또는 그림만 이동할 수 있습니다.');
      const siblings = document.content.roots.includes(id) ? document.content.roots : document.content.nodes.find(node => node.children.includes(id))?.children;
      const index = siblings?.indexOf(id);
      if (index == null || index < 0) throw Error('이동할 노드의 소유자가 없습니다.');
      if (typeof directionOrTarget === 'number') {
        const target = index + directionOrTarget;
        if (target < 0 || target >= siblings.length) return;
        siblings.splice(index, 1); siblings.splice(target, 0, id);
      } else {
        const target = byId.get(directionOrTarget);
        if (!target || target.kind !== source.kind || target.id === id) return;
        const destination = document.content.roots.includes(target.id) ? document.content.roots : document.content.nodes.find(node => node.children.includes(target.id))?.children;
        if (!destination) return;
        siblings.splice(index, 1); destination.splice(destination.indexOf(target.id), 0, id);
      }
    });
  }
}
