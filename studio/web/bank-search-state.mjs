const PAGE_SIZES = new Set([20, 50, 100]);
const INTERNAL_CODE = /AS1-[a-f\d]{64}/i;
const FILTER_CATEGORIES = new Set(["subject", "unit", "source", "difficulty", "problemTypes", "keywords", "features", "figureStatus"]);

export function createBankSearchState(value = {}) {
  const tabs = Array.isArray(value.tabs)
    ? value.tabs.filter((tab) => tab && typeof tab === "object" && !Array.isArray(tab)).map(normalizeTab)
    : [];
  const safeTabs = tabs.length ? tabs : [createTab("검색 1")];
  return {
    activeTabId: safeTabs.some((tab) => tab.id === value.activeTabId) ? value.activeTabId : safeTabs[0].id,
    tabs: safeTabs
  };
}

export function createBankRequestSequence() {
  let sequence = 0;
  return { begin: () => ++sequence, isCurrent: (request) => request === sequence };
}

export function bankGroupIsExpanded(expansionByTab, tabId, groupKey) {
  return expansionByTab.get(tabId)?.has(groupKey) === true;
}

export function setBankGroupExpanded(expansionByTab, tabId, groupKey, expanded) {
  const groups = expansionByTab.get(tabId) ?? new Set();
  if (expanded) groups.add(groupKey);
  else groups.delete(groupKey);
  expansionByTab.set(tabId, groups);
}

function createTab(label) {
  return { id: crypto.randomUUID(), label, query: "", numberQuery: "", filters: {}, sort: "original", page: 1, pageSize: 20, scrollY: 0 };
}

function normalizeTab(tab, index) {
  return {
    ...createTab(`검색 ${index + 1}`),
    ...tab,
    id: typeof tab?.id === "string" && tab.id ? tab.id : crypto.randomUUID(),
    query: typeof tab?.query === "string" ? tab.query : "",
    numberQuery: typeof tab?.numberQuery === "string" ? tab.numberQuery : "",
    filters: Object.fromEntries(Object.entries(tab?.filters && typeof tab.filters === "object" ? tab.filters : {})
      .filter(([key]) => FILTER_CATEGORIES.has(key))
      .map(([key, values]) => [key, Array.isArray(values) ? values.filter((value) => typeof value === "string") : []])),
    sort: ["original", "latest", "number"].includes(tab?.sort) ? tab.sort : "original",
    page: Number.isInteger(tab?.page) && tab.page > 0 ? tab.page : 1,
    pageSize: PAGE_SIZES.has(tab?.pageSize) ? tab.pageSize : 20,
    scrollY: Number.isFinite(tab?.scrollY) && tab.scrollY >= 0 ? tab.scrollY : 0
  };
}

export function activeBankSearch(state) {
  return state.tabs.find((tab) => tab.id === state.activeTabId) ?? state.tabs[0];
}

export function updateBankSearch(state, patch) {
  const active = activeBankSearch(state);
  state.tabs = state.tabs.map((tab) => tab.id === active.id ? { ...tab, ...patch } : tab);
  return activeBankSearch(state);
}

export function newBankSearch(state) {
  const tab = createTab(`검색 ${state.tabs.length + 1}`);
  state.tabs = [...state.tabs, tab];
  state.activeTabId = tab.id;
  return tab;
}

export function selectBankSearch(state, id) {
  if (state.tabs.some((tab) => tab.id === id)) state.activeTabId = id;
  return activeBankSearch(state);
}

export function closeBankSearch(state, id) {
  if (state.tabs.length <= 1) return activeBankSearch(state);
  const index = state.tabs.findIndex((tab) => tab.id === id);
  if (index < 0) return activeBankSearch(state);
  state.tabs.splice(index, 1);
  if (state.activeTabId === id) state.activeTabId = state.tabs[Math.min(index, state.tabs.length - 1)].id;
  return activeBankSearch(state);
}

export function filterAndSortBankItems(items, search) {
  const query = search.query.trim().toLocaleLowerCase("ko-KR");
  const numberQuery = search.numberQuery.trim().toLocaleLowerCase("ko-KR");
  const matches = items.filter((item) => {
    const text = String(item.searchText ?? "").toLocaleLowerCase("ko-KR");
    const number = String(item.printedNumber ?? "").toLocaleLowerCase("ko-KR");
    if (query && !text.includes(query)) return false;
    if (numberQuery && !number.includes(numberQuery)) return false;
    return Object.entries(search.filters).every(([category, selected]) => {
      if (!selected?.length) return true;
      const values = item.categories?.[category] ?? [];
      return selected.includes("__unclassified") ? !values.length || selected.some((value) => values.includes(value))
        : selected.some((value) => values.includes(value));
    });
  });
  return matches.sort((left, right) => {
    if (search.sort === "latest") {
      const dateOrder = String(right.updatedAt ?? "").localeCompare(String(left.updatedAt ?? ""));
      if (dateOrder) return dateOrder;
    } else if (search.sort === "number") {
      const numberOrder = String(left.printedNumber ?? "").localeCompare(String(right.printedNumber ?? ""), "ko-KR", { numeric: true });
      if (numberOrder) return numberOrder;
    } else {
      const originalOrder = left.originalIndex - right.originalIndex;
      if (originalOrder) return originalOrder;
    }
    return String(left.refKey).localeCompare(String(right.refKey));
  });
}

export function paginateBankItems(items, page, pageSize) {
  const size = PAGE_SIZES.has(pageSize) ? pageSize : 20;
  const pageCount = Math.max(1, Math.ceil(items.length / size));
  const currentPage = Math.min(Math.max(1, Number.isInteger(page) ? page : 1), pageCount);
  const start = items.length ? (currentPage - 1) * size : 0;
  return { items: items.slice(start, start + size), page: currentPage, pageCount, pageSize: size, start, end: Math.min(start + size, items.length), total: items.length };
}

export function loadBankSearchState(storage, key) {
  try {
    const value = JSON.parse(storage.getItem(key) ?? "null");
    return createBankSearchState(value?.version === 1 ? value : {});
  } catch {
    return createBankSearchState();
  }
}

export function saveBankSearchState(storage, key, state) {
  const clean = createBankSearchState(state);
  clean.tabs = clean.tabs.map((tab) => ({
    ...tab,
    query: INTERNAL_CODE.test(tab.query) ? "" : tab.query,
    numberQuery: INTERNAL_CODE.test(tab.numberQuery) ? "" : tab.numberQuery
  }));
  try { storage.setItem(key, JSON.stringify({ version: 1, ...clean })); } catch { /* Storage can be disabled or full. */ }
}
