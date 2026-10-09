/** Device-specific presentation only. Canonical writes stay in app.js. */
export const MOBILE_QUERY = '(max-width: 760px)';

export function createDeviceState(href, mobile = false, visibleView = null) {
  const route = deviceRoute(href, visibleView);
  return { ...route, mobile, more: false,
    itemDetail: route.view === 'saved' && Boolean(route.itemId || route.readOnly),
    setDetail: route.view === 'sets' && Boolean(route.setId) };
}

/** These transitions cannot save, approve, process or discard canonical drafts. */
export function deviceTransition(state, action) {
  switch (action.type) {
    case 'resize': return { ...state, mobile: Boolean(action.mobile), more: false };
    case 'create': return { ...state, create: state.view === 'work' && Boolean(action.open) };
    case 'more': return { ...state, more: Boolean(action.open) };
    case 'open-item': return { ...state, itemDetail: true, itemId: action.itemId ?? state.itemId, projectId: action.projectId ?? state.projectId };
    case 'back-items': return { ...state, itemDetail: false };
    case 'open-set': return { ...state, setDetail: true, setId: action.setId ?? state.setId };
    case 'back-sets': return { ...state, setDetail: false };
    default: return state;
  }
}

/** Touch only presentation nodes; never recreate or populate input/editor content. */
export function renderDevicePresentation(doc, state) {
  const get = (id) => doc.getElementById(id);
  const hide = (id, hidden) => { const node = get(id); if (node) node.hidden = hidden; };
  const changedDevice = doc.body.dataset.deviceMobile !== String(state.mobile);
  doc.body.dataset.deviceMobile = String(state.mobile);
  doc.body.dataset.deviceView = state.view;
  doc.body.dataset.deviceCreate = String(state.create);
  hide('deviceCreatePanel', !state.create);
  hide('deviceDashboard', state.create);
  hide('deviceRecentBatches', state.create);
  hide('deviceItemsBack', !state.mobile || !state.itemDetail);
  hide('deviceSetsBack', !state.mobile || !state.setDetail);
  hide('deviceMorePanel', !state.more || !state.mobile);
  get('deviceNewPdf')?.setAttribute('aria-expanded', String(state.create));
  get('deviceMore')?.setAttribute('aria-expanded', String(state.more && state.mobile));
  if (get('savedView')) get('savedView').dataset.deviceDetail = String(state.itemDetail);
  if (get('setsView')) get('setsView').dataset.deviceDetail = String(state.setDetail);
  if (changedDevice) for (const id of ['deviceProjectsDisclosure', 'deviceItemFilters', 'deviceSetOutput']) {
    const node = get(id);
    if (node) node.open = !state.mobile;
  }
  for (const link of doc.querySelectorAll('[data-device-view]')) {
    link.setAttribute('aria-current', link.dataset.deviceView === state.view ? 'page' : 'false');
  }
}

export function deviceDestinations(links) {
  return [...links].map((link) => ({ href: link.getAttribute?.('href') || link.href, label: link.textContent.trim() })).concat([
    { href: '/?view=work&create=1', label: '새 PDF 등록' },
    { href: '/?view=work&create=1&workmode=exam', label: '시험지·선택 영역' },
    { href: '/?view=work&create=1#deviceImport', label: '기존 작업 폴더 가져오기' }
  ]);
}

export function batchAttemptPresentation(status, progress) {
  const terminal = ['failed', 'interrupted', 'cancelled'].includes(status);
  return { phase: terminal ? '처리 시도 종료' : null, percent: `${terminal ? '시도 ' : ''}${progress}%` };
}

const NAV_GROUPS = [
  ['작업', ['workNav']], ['문항 탐색·검수', ['savedNav', 'bankNav']],
  ['AI 생성', ['generateNav']], ['문제지·문서', ['setsNav', 'documentNav']], ['관리·기록', ['learningNav']]
];

/** Mount once on either page, retaining canonical anchor nodes and their IDs. */
export function initializeDeviceShell(doc = document, win = window) {
  if (doc.body.dataset.deviceShell === 'ready') return;
  const nav = doc.querySelector('.workspace-nav');
  if (!nav) return;
  doc.body.dataset.deviceShell = 'ready';
  const get = (id) => doc.getElementById(id);
  const make = (tag, text, className) => {
    const node = doc.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const workLabel = get('workNav')?.querySelector('span');
  if (workLabel) workLabel.textContent = '작업 현황';
  doc.querySelector('.app-brand')?.setAttribute('aria-label', 'Forest of Q 작업 현황');
  const destinations = deviceDestinations(nav.querySelectorAll('a'));
  for (const [label, ids] of NAV_GROUPS) {
    const group = make('div', '', 'device-nav-group');
    group.append(make('p', label, 'device-nav-label'));
    for (const id of ids) { const link = get(id); if (link) group.append(link); }
    nav.append(group);
  }
  const bottom = make('nav', '', 'device-bottom-nav');
  bottom.setAttribute('aria-label', '모바일 주요 메뉴');
  for (const [label, view, glyph] of [['홈', 'work', '⌂'], ['문항', 'saved', '▤'], ['문제지', 'sets', '▱']]) {
    const link = make('a');
    link.href = `/?view=${view}`;
    link.dataset.deviceView = view;
    const icon = make('span', glyph, 'device-bottom-icon');
    icon.setAttribute('aria-hidden', 'true');
    link.append(icon, make('span', label));
    bottom.append(link);
  }
  const more = make('button');
  more.id = 'deviceMore'; more.type = 'button';
  more.setAttribute('aria-expanded', 'false'); more.setAttribute('aria-controls', 'deviceMorePanel');
  const moreIcon = make('span', '···', 'device-bottom-icon'); moreIcon.setAttribute('aria-hidden', 'true');
  more.append(moreIcon, make('span', '더보기')); bottom.append(more);
  const panel = make('nav', '', 'device-more-panel'); panel.id = 'deviceMorePanel'; panel.hidden = true;
  panel.setAttribute('aria-labelledby', 'deviceMoreTitle');
  const panelHeading = make('div', '', 'device-more-heading');
  const title = make('h2', '모든 기능'); title.id = 'deviceMoreTitle';
  const close = make('button', '닫기', 'button'); close.type = 'button';
  panelHeading.append(title, close); panel.append(panelHeading);
  for (const entry of destinations) {
    const link = make('a', entry.label);
    link.href = entry.href;
    link.dataset.deviceView = deviceRoute(entry.href).view;
    panel.append(link);
  }
  doc.body.append(panel, bottom);
  const media = win.matchMedia(MOBILE_QUERY);
  const visibleView = ['saved', 'bank', 'sets', 'learning', 'work'].find((view) => get(`${view}View`) && !get(`${view}View`).hidden);
  let state = createDeviceState(win.location.href, media.matches, visibleView);
  let listScroll = 0;
  const render = () => renderDevicePresentation(doc, state);
  const transition = (action) => { state = deviceTransition(state, action); render(); };
  const focus = (id) => get(id)?.focus({ preventScroll: true });
  const writeLocalUrl = (changes) => {
    const url = new URL(win.location.href);
    for (const [key, value] of Object.entries(changes)) {
      if (value == null) url.searchParams.delete(key); else url.searchParams.set(key, value);
    }
    win.history.replaceState(win.history.state, '', url);
  };
  const closeMore = () => { transition({ type: 'more', open: false }); more.focus({ preventScroll: true }); };
  more.addEventListener('click', () => transition({ type: 'more', open: !state.more }));
  close.addEventListener('click', closeMore);
  doc.addEventListener('keydown', (event) => { if (event.key === 'Escape' && state.more) { closeMore(); event.preventDefault(); } });
  doc.addEventListener('click', (event) => {
    if (state.more && !panel.contains(event.target) && !bottom.contains(event.target)) transition({ type: 'more', open: false });
  });
  get('deviceNewPdf')?.addEventListener('click', () => {
    transition({ type: 'create', open: true }); writeLocalUrl({ create: '1' });
    win.scrollTo(0, 0); focus('deviceCreateTitle');
  });
  get('deviceCreateBack')?.addEventListener('click', () => {
    transition({ type: 'create', open: false }); writeLocalUrl({ create: null });
    win.scrollTo(0, 0); focus('deviceNewPdf');
  });
  get('deviceItemsBack')?.addEventListener('click', () => {
    if (state.readOnly) { win.location.assign('/?view=bank'); return; }
    transition({ type: 'back-items' }); writeLocalUrl({ item: null });
    win.scrollTo(0, listScroll);
    get('itemList')?.querySelector('[aria-current="true"]')?.focus({ preventScroll: true });
  });
  get('deviceSetsBack')?.addEventListener('click', () => {
    transition({ type: 'back-sets' }); writeLocalUrl({ set: null });
    win.scrollTo(0, listScroll);
    get('questionSetList')?.querySelector('[aria-current="true"]')?.focus({ preventScroll: true });
  });
  doc.addEventListener('astra:item-opened', (event) => {
    if (state.view !== 'saved' || state.readOnly) return;
    if (!state.itemDetail) listScroll = win.scrollY;
    transition({ type: 'open-item', ...event.detail });
    writeLocalUrl({ project: state.projectId, item: state.itemId });
    if (state.mobile) { win.scrollTo(0, 0); focus('deviceItemsBack'); }
  });
  doc.addEventListener('astra:set-opened', (event) => {
    if (state.view !== 'sets') return;
    if (!state.setDetail) listScroll = win.scrollY;
    transition({ type: 'open-set', ...event.detail });
    writeLocalUrl({ set: state.setId });
    if (state.mobile) { win.scrollTo(0, 0); focus('deviceSetsBack'); }
  });
  media.addEventListener('change', () => {
    transition({ type: 'resize', mobile: media.matches });
    if (state.mobile && (state.itemDetail || state.setDetail)) win.scrollTo(0, 0);
  });
  win.addEventListener('popstate', () => {
    const route = deviceRoute(win.location.href);
    // Local history presentation only; canonical selection is never discarded.
    if (route.view === state.view) { state = { ...state, create: route.create, more: false }; render(); }
  });
  doc.addEventListener('change', (event) => { if (event.target.name === 'workMode') render(); });
  render();
  const params = new URL(win.location.href).searchParams;
  if (state.create && params.get('workmode') === 'exam') {
    const radio = doc.querySelector('input[name="workMode"][value="exam"]');
    if (radio) { radio.checked = true; radio.dispatchEvent(new win.Event('change', { bubbles: true })); }
  }
  if (state.create && win.location.hash === '#deviceImport') {
    const disclosure = get('deviceImport'); if (disclosure) disclosure.open = true;
  }
  return { get state() { return state; } };
}

if (typeof document !== 'undefined' && typeof window !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => initializeDeviceShell(), { once: true });
  else queueMicrotask(() => initializeDeviceShell());
}

export function deviceRoute(href, visibleView = null) {
  const url = new URL(href, 'http://astra.local');
  const params = url.searchParams;
  const requested = params.get('view');
  const view = /(?:^|\/)document\.html$/.test(url.pathname) ? 'document'
    : ['saved', 'bank', 'sets', 'learning', 'work'].includes(requested) ? requested
    : params.has('project') ? 'saved' : visibleView || 'work';
  return { view, create: view === 'work' && params.get('create') === '1',
    readOnly: view === 'saved' && params.get('mode') === 'read',
    itemId: params.get('item'), projectId: params.get('project'), setId: params.get('set') };
}
