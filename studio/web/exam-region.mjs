"use strict";

import { applyResize, clampBox, normalizeBox, screenToImage } from "./vendor/pnkmath/crop_geometry.mjs";
export { clampBox, normalizeBox, screenToImage };

const DRAFT_KEY = "astra-exam-draft";
const MODE_KEY = "astra-work-mode";
const $ = (id) => document.getElementById(id);
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
const round = (value) => Math.round(value * 1000) / 1000;

export function moveBox(box, dx, dy, handle = "move") {
  let [left, top, right, bottom] = box;
  if (handle === "move") {
    const width = right - left, height = bottom - top;
    left = clamp(left + dx, 0, 1 - width);
    top = clamp(top + dy, 0, 1 - height);
    right = left + width;
    bottom = top + height;
  } else {
    [left, top, right, bottom] = applyResize(handle, box, dx, dy);
    if (right - left < .005) right = Math.min(1, left + .005);
    if (bottom - top < .005) bottom = Math.min(1, top + .005);
  }
  return [left, top, right, bottom].map(round);
}

export function previousCropRevision(crop) {
  return crop?.previous_revision ?? null;
}

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

export function mountExamRegion() {
  const ui = Object.fromEntries([
    "examWorkspace", "examUploadForm", "examFile", "examDraftSelect", "examRefresh", "examMessage",
    "examDraftArea", "examPageChoices", "examSavePages", "examCleanEnabled", "examBulkStatus", "examPageTabs",
    "examPageEditor", "examPageTitle", "examSourceMode", "examClean", "examDetect", "examRetry",
    "examPageStatus", "examOriginal", "examComparison", "examWorkingCaption", "examZoom", "examZoomValue",
    "examSelectAll", "examSelectNone", "examDrawRegion", "examAddChoice", "examUndo", "examCanvas", "examCanvasImage",
    "examOverlay", "examRegions", "examRegionCount", "examConfirmAll", "examSaveGate",
    "examPublish", "examPublicationStatus", "examPublishedList"
  ].map((id) => [id, $(id)]));
  if (!ui.examWorkspace) return;
  const legacy = [$("batchForm"), document.querySelector(".recent-batches"), document.querySelector(".advanced-import")];
  const heading = $("batchTitle"), description = document.querySelector(".batch-heading p");
  const originalHeading = heading.textContent, originalDescription = description.textContent;
  const state = { draft: null, page: null, clean: null, proposal: null, selection: null,
    publication: null, publishPending: false, regionId: null, partIndex: 0, poll: null,
    busy: false, adding: false, drag: null, switchToCleaned: false, advancedRegionId: null, cropOpenRegionId: null,
    crop: null, cropRegionId: null, cropArea: null, candidateRevision: null, cropPending: null };
  const pageUrl = (suffix = "") => `/api/exam/drafts/${state.draft.draft_id}/pages/${state.page}${suffix}`;
  const cropUrl = (regionId, suffix = "") => pageUrl(`/regions/${encodeURIComponent(regionId)}/crop${suffix}`);
  const report = (message, error = false) => { ui.examMessage.textContent = message; ui.examMessage.dataset.state = error ? "error" : "ok"; };

  async function request(path, options = {}) {
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    let body = options.body;
    if (Object.hasOwn(options, "json")) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(options.json);
    }
    const response = await fetch(path, { method: options.method || "GET", credentials: "same-origin", headers, body });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.message || `요청 실패 (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function mode(next) {
    const exam = next === "exam";
    ui.examWorkspace.hidden = !exam;
    legacy.forEach((element) => { if (element) element.hidden = exam; });
    heading.textContent = exam ? "시험지·선택 영역" : originalHeading;
    description.textContent = exam ? "필기를 정리하고 필요한 문항 영역을 직접 확인합니다." : originalDescription;
    try { sessionStorage.setItem(MODE_KEY, next); } catch {}
    if (exam) listDrafts().catch((error) => report(error.message, true));
  }

  async function listDrafts() {
    const { drafts } = await request("/api/exam/drafts");
    ui.examDraftSelect.replaceChildren(new Option("초안을 선택하세요", ""));
    drafts.forEach((draft) => ui.examDraftSelect.add(new Option(`${draft.name} · ${draft.page_count}쪽`, draft.draft_id)));
    let saved = "";
    try { saved = localStorage.getItem(DRAFT_KEY) || ""; } catch {}
    if (saved && drafts.some((draft) => draft.draft_id === saved)) {
      ui.examDraftSelect.value = saved;
      if (!state.draft || state.draft.draft_id !== saved) await openDraft(saved);
    }
  }

  async function openDraft(id) {
    state.draft = await request(`/api/exam/drafts/${id}`);
    state.draft = await request(`/api/exam/drafts/${id}/clean`, { method: "POST" });
    try { localStorage.setItem(DRAFT_KEY, id); } catch {}
    ui.examDraftSelect.value = id;
    ui.examDraftArea.hidden = false;
    renderChoices();
    renderTabs();
    state.page = state.draft.selected_pages[0]?.number || null;
    if (state.page) await loadPage();
    report(state.draft.cleaning_all ? `${state.draft.page_count}쪽 필기 제거를 시작했습니다.`
      : `${state.draft.name} · ${state.draft.page_count}쪽 초안을 열었습니다.`);
  }

  function renderChoices() {
    ui.examPageChoices.replaceChildren();
    const chosen = new Map(state.draft.selected_pages.map((page) => [page.number, page.rotation]));
    for (let number = 1; number <= state.draft.page_count; number++) {
      const row = node("label", "exam-page-choice");
      const check = node("input"); check.type = "checkbox"; check.checked = chosen.has(number); check.dataset.page = String(number);
      const name = node("span", "", `${number}쪽`);
      const rotation = node("select"); rotation.dataset.rotation = String(number); rotation.setAttribute("aria-label", `${number}쪽 회전`);
      [0, 90, 180, 270].forEach((angle) => rotation.add(new Option(`${angle}°`, String(angle))));
      rotation.value = String(chosen.get(number) ?? 0);
      row.append(check, name, rotation);
      ui.examPageChoices.append(row);
    }
  }

  function renderTabs() {
    ui.examPageTabs.replaceChildren();
    state.draft.selected_pages.forEach((page) => {
      const button = node("button", "exam-page-tab", `${page.number}쪽`);
      button.type = "button"; button.dataset.page = String(page.number);
      button.setAttribute("role", "tab"); button.setAttribute("aria-selected", String(state.page === page.number));
      button.addEventListener("click", () => { state.page = page.number; loadPage().catch(showError); });
      ui.examPageTabs.append(button);
    });
  }

  async function savePages() {
    const pages = [...ui.examPageChoices.querySelectorAll("input[data-page]:checked")]
      .map((check) => ({ number: Number(check.dataset.page), rotation: Number(ui.examPageChoices.querySelector(`select[data-rotation="${check.dataset.page}"]`).value) }));
    if (!pages.length) { report("처리할 쪽을 한 개 이상 선택하세요.", true); return; }
    if (JSON.stringify(pages) === JSON.stringify(state.draft.selected_pages)) { report("쪽 선택과 회전이 이미 저장돼 있습니다."); return; }
    try {
      state.draft = await request(`/api/exam/drafts/${state.draft.draft_id}/pages`,
        { method: "PATCH", json: { expected_revision: state.draft.revision, pages } });
      state.page = pages.some((page) => page.number === state.page) ? state.page : pages[0].number;
      renderChoices(); renderTabs(); await loadPage();
      report("쪽 선택과 회전을 저장했습니다.");
    } catch (error) { await conflict(error); }
  }

  async function conflict(error) {
    report(error.status === 409 ? "다른 편집 내용이 먼저 저장됐습니다. 최신 상태를 다시 불러왔습니다." : error.message, true);
    if (error.status === 409 && state.draft) await openDraft(state.draft.draft_id);
  }
  const showError = (error) => report(error.message || "요청을 완료하지 못했습니다.", true);

  async function loadPage() {
    if (!state.draft || !state.page) return;
    if (state.draft.cleaning_all) state.draft = await request(`/api/exam/drafts/${state.draft.draft_id}`);
    const page = state.page;
    clearTimeout(state.poll);
    ui.examPageEditor.hidden = false;
    ui.examPageTitle.textContent = `${page}쪽`;
    renderTabs();
    const base = pageUrl();
    ui.examOriginal.src = `${base}/original`;
    state.clean = await request(`${base}/clean`);
    state.selection = await request(`${base}/selection`);
    state.proposal = await request(`${base}/proposals`).catch(() => null);
    state.publication = await request(`/api/exam/drafts/${state.draft.draft_id}/publication`).catch(() => null);
    if (state.page !== page) return;
    const originalMode = state.selection.source_mode === "original_unprocessed";
    const cleaningStatus = state.clean.cleaning_status || state.clean.status;
    ui.examBulkStatus.hidden = !state.draft.cleaning_all;
    ui.examBulkStatus.textContent = state.draft.cleaning_all
      ? `${state.draft.page_count}쪽 필기 제거 중입니다. 정리된 쪽부터 작업할 수 있습니다.` : "";
    ui.examSavePages.disabled = state.draft.cleaning_all;
    if (state.switchToCleaned && originalMode && cleaningStatus === "ready") {
      await request(`${base}/use-cleaned`, { method: "POST", json: { expected_revision: state.selection.revision } });
      state.switchToCleaned = false;
      await loadPage(); report("정리본으로 전환했습니다. 영역은 다시 확인하세요.");
      return;
    }
    if (state.switchToCleaned && originalMode && cleaningStatus === "failed") {
      state.switchToCleaned = false;
      report(`정리에 실패해 원본 모드를 유지합니다 (${state.clean.cleaning_error_code || state.clean.error_code || "원인 미상"}).`, true);
    }
    if (!state.switchToCleaned) ui.examCleanEnabled.checked = !originalMode;
    ui.examSourceMode.textContent = originalMode ? "원본 그대로 · 필기 제거 안 함" : state.clean.status === "ready" ? "정리본 사용" : "정리 전";
    ui.examSourceMode.dataset.mode = originalMode ? "original" : "cleaned";
    const image = originalMode ? `${base}/original` : state.clean.status === "ready" ? `${base}/cleaned` : "";
    ui.examWorkingCaption.textContent = originalMode ? "검출 입력: 원본 그대로" : state.clean.status === "ready" ? "검출 입력: 정리본" : "정리 전";
    ui.examComparison.hidden = !image;
    if (image) ui.examComparison.src = image;
    ui.examCanvasImage.src = image || `${base}/original`;
    ui.examCanvas.dataset.editable = String(Boolean(image));
    ui.examClean.textContent = originalMode ? state.switchToCleaned ? "정리본 전환 중…" :
      state.clean.cleaning_status === "ready" ? "정리본으로 전환" : "필기 제거 후 정리본 사용" :
      state.clean.status === "failed" ? "정리 다시 시도" :
      state.clean.status === "ready" ? "이 쪽 다시 정리" : "이 쪽 정리";
    ui.examClean.disabled = state.switchToCleaned || state.draft.cleaning_all;
    ui.examDetect.disabled = !image || state.clean.status === "running";
    ui.examPageStatus.textContent = state.switchToCleaned ? "페이지를 정리하는 중입니다. 완료 후 정리본으로 전환합니다." : statusText(state.clean, state.proposal, originalMode);
    ui.examRetry.hidden = state.clean.status !== "failed" && state.proposal?.status !== "failed";
    if (state.publishPending && state.publication?.items?.length &&
        state.publication.items.every((item) => ["succeeded", "failed"].includes(item.status)))
      state.publishPending = false;
    renderRegions();
    if (state.regionId) await refreshCrop(state.regionId);
    if (["running", "queued"].includes(state.clean.status) || ["running", "queued"].includes(state.proposal?.status)
        || state.switchToCleaned || state.publishPending || state.cropPending || state.draft.cleaning_all)
      state.poll = setTimeout(() => loadPage().catch(showError), 1500);
  }

  function statusText(clean, proposal, originalMode) {
    if (originalMode) return proposal?.status === "ready" ? "원본에서 제안 완료 · 영역 확인이 필요합니다." : "필기 제거를 건너뛰었습니다. 원본에서 영역을 찾을 수 있습니다.";
    if (clean.status === "failed") return clean.error_code === "codex_cli_unavailable"
      ? "Codex CLI 실행 파일을 찾지 못했습니다. 원본은 보존됐습니다."
      : clean.error_code === "configuration_required" ? "이미지 서비스 설정이 필요합니다. 설정 후 이 쪽만 다시 시도하세요."
      : `이 쪽 정리에 실패했습니다 (${clean.error_code || "원인 미상"}). 원본은 보존됐습니다.`;
    if (clean.status === "running") return "페이지 필기를 정리하는 중입니다…";
    if (clean.status !== "ready") return "쪽을 정한 뒤 정리를 시작하세요.";
    if (proposal?.status === "failed") return "영역 제안에 실패했습니다. 정리본을 확인하고 다시 시도하세요.";
    if (proposal?.status === "running") return "정리본에서 문항 영역을 찾는 중입니다…";
    return proposal?.status === "ready" ? "제안 영역을 원본과 비교하고 수정하세요." : "정리됐습니다. 영역 제안을 시작하세요.";
  }

  async function cleanPage() {
    if (state.busy) return;
    state.busy = true;
    try {
      const wantsCleaning = ui.examCleanEnabled.checked || state.selection.source_mode === "original_unprocessed";
      if (state.selection.source_mode === "original_unprocessed") ui.examCleanEnabled.checked = true;
      if (wantsCleaning && state.selection.source_mode === "original_unprocessed") {
        if (state.clean.cleaning_status === "ready") {
          await request(`${pageUrl()}/use-cleaned`, { method: "POST", json: { expected_revision: state.selection.revision } });
        } else {
          state.switchToCleaned = true;
          await request(`${pageUrl()}/clean`, { method: "POST" });
        }
      } else if (wantsCleaning) await request(`${pageUrl()}/clean`, { method: "POST" });
      else await request(`${pageUrl()}/skip-clean`, { method: "POST", json: { expected_revision: state.selection.revision } });
      await loadPage();
      if (!state.switchToCleaned) report(wantsCleaning ? "페이지 정리를 시작했습니다." : "원본 그대로 진행합니다. 필기 제거 상태가 아닙니다.");
    } catch (error) { state.switchToCleaned = false; await conflict(error); }
    finally { state.busy = false; }
  }

  async function detectPage() {
    if (state.busy) return;
    state.busy = true;
    try { await request(`${pageUrl()}/proposals`, { method: "POST" }); await loadPage(); report("영역 제안을 시작했습니다."); }
    catch (error) { await conflict(error); }
    finally { state.busy = false; }
  }

  async function edit(operation, quiet = false) {
    if (!state.selection || state.selection.status !== "ready") { report("먼저 페이지를 준비하세요.", true); return false; }
    try {
      state.selection = await request(`${pageUrl()}/selection`,
        { method: "PATCH", json: { expected_revision: state.selection.revision, operation } });
      renderRegions();
      if (state.regionId) await refreshCrop(state.regionId);
      if (!quiet) report("영역 편집을 저장했습니다.");
      return true;
    } catch (error) { await conflict(error); return false; }
  }

  function renderRegions() {
    const regions = state.selection?.regions || [];
    ui.examRegionCount.textContent = `${regions.length}개`;
    ui.examRegions.replaceChildren();
    const editable = state.selection?.status === "ready" && Boolean(state.selection.source_sha256);
    const selected = regions.filter((region) => region.selected);
    ui.examConfirmAll.disabled = !editable || !regions.some((region) => region.selected && !region.confirmed);
    const hasConfirmed = selected.some((region) => region.confirmed) || Boolean(state.publication?.items?.length);
    ui.examPublish.disabled = state.publishPending || !hasConfirmed || selected.some((region) => !region.confirmed);
    ui.examSaveGate.textContent = regions.some((region) => region.selected && !region.confirmed)
      ? "선택한 영역을 모두 확정해야 타이핑하고 저장할 수 있습니다."
      : hasConfirmed
        ? "확정한 모든 쪽의 문항을 한 번에 타이핑하고 저장합니다."
        : "저장할 영역을 선택하고 확정하세요.";
    if (!regions.length) ui.examRegions.append(node("p", "exam-empty", "제안된 문항이 없습니다. 영역을 직접 추가할 수 있습니다."));
    regions.forEach((region) => ui.examRegions.append(regionCard(region, editable)));
    renderPublication();
    drawOverlay();
  }

  async function refreshCrop(regionId) {
    const region = state.selection?.regions?.find((item) => item.region_id === regionId);
    if (!region?.selected || !region.confirmed) {
      state.crop = null; state.cropRegionId = null; state.cropPending = null; renderRegions(); return;
    }
    const page = state.page;
    const crop = await request(cropUrl(regionId, "/clean"));
    if (state.page !== page || state.regionId !== regionId) return;
    state.crop = crop; state.cropRegionId = regionId;
    if (state.cropPending?.regionId === regionId && crop.revision > state.cropPending.revision && crop.status !== "running") {
      state.cropPending = null;
    }
    const current = crop.candidates.filter((item) => item.base_sha256 === crop.base_sha256);
    if (!current.some((item) => item.revision === state.candidateRevision))
      state.candidateRevision = current.at(-1)?.revision ?? null;
    renderRegions();
  }

  function renderPublication() {
    ui.examPublishedList.replaceChildren();
    const items = state.publication?.items || [];
    if (state.publishPending) ui.examPublicationStatus.textContent = "확정 영역을 타이핑하는 중입니다. 성공한 문항부터 보존합니다.";
    else if (items.some((item) => item.status === "failed")) ui.examPublicationStatus.textContent = "일부 문항을 저장하지 못했습니다. 실패한 문항만 다시 시도할 수 있습니다.";
    else if (items.some((item) => item.status === "succeeded")) ui.examPublicationStatus.textContent = "저장된 문항은 기존 문제은행에서 열 수 있습니다.";
    else ui.examPublicationStatus.textContent = "";
    for (const item of items) {
      const region = state.selection?.regions?.find((value) => value.region_id === item.region_id);
      const row = node("div", "exam-published-row");
      row.append(node("span", "", `${region?.printed_number || region?.order || "문항"} · ${
        ({ pending: "대기", running: "타이핑 중", succeeded: "저장됨", failed: "실패" })[item.status] || item.status}`));
      if (item.status === "succeeded" && item.saved_item_id) {
        const link = node("a", "", "저장된 문항 열기");
        link.href = `/?view=saved&project=${encodeURIComponent(state.publication.project_id)}&item=${encodeURIComponent(item.saved_item_id)}&mode=read`;
        row.append(link);
      }
      ui.examPublishedList.append(row);
    }
  }

  function regionCard(region, editable) {
    const card = node("article", "exam-region-card");
    card.dataset.active = String(state.regionId === region.region_id);
    const header = node("div", "exam-region-head");
    const check = node("input"); check.type = "checkbox"; check.checked = Boolean(region.selected); check.disabled = !editable;
    check.setAttribute("aria-label", `${region.printed_number || region.order}번 영역 선택`);
    check.addEventListener("change", () => edit({ action: "select", region_id: region.region_id, value: check.checked }));
    const title = node("button", "exam-region-title", `문항 ${region.printed_number || region.order}`);
    title.type = "button"; title.addEventListener("click", () => {
      state.regionId = region.region_id; state.partIndex = 0; state.crop = null;
      state.cropArea = null; state.candidateRevision = null; state.advancedRegionId = null; state.cropOpenRegionId = null; renderRegions();
      refreshCrop(region.region_id).catch(showError);
    });
    const badge = node("span", "exam-region-badge", region.confirmed ? "확정" : region.selected ? "선택됨" : "미선택");
    header.append(check, title, badge); card.append(header);
    if (state.regionId !== region.region_id) return card;
    const fields = node("div", "exam-region-fields");
    const advanced = node("details", "exam-region-advanced");
    advanced.open = state.advancedRegionId === region.region_id;
    advanced.append(node("summary", "", "세부 조정"));
    advanced.addEventListener("toggle", () => {
      if (!advanced.isConnected) return;
      state.advancedRegionId = advanced.open ? region.region_id : null;
    });
    const advancedFields = node("div", "exam-region-fields");
    for (const [label, action, value, type] of [
      ["문항 번호", "number", region.printed_number || "", "text"],
      ["순서", "order", region.order || 1, "number"],
      ["묶음", "group", region.group_label || "", "text"]]) {
      const wrap = node("label", "", label), input = node("input"); input.type = type; input.value = value; input.disabled = !editable;
      if (type === "number") input.min = "1";
      let committed = String(value);
      const save = () => {
        if (input.value === committed) return;
        committed = input.value;
        edit({ action, region_id: region.region_id, value: type === "number" ? Number(input.value) : input.value || (action === "group" ? null : "") });
      };
      input.addEventListener("change", save);
      input.addEventListener("blur", save);
      input.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); save(); } });
      wrap.append(input); (action === "number" ? fields : advancedFields).append(wrap);
    }
    card.append(fields); advanced.append(advancedFields);
    const parts = node("div", "exam-parts");
    (region.parts || []).forEach((part, index) => {
      const row = node("div", "exam-part-row");
      const choose = node("button", "button", `영역 ${index + 1} · ${part.page_number}쪽`);
      choose.type = "button"; choose.dataset.active = String(state.partIndex === index);
      choose.addEventListener("click", () => { state.partIndex = index; renderRegions(); });
      const remove = node("button", "exam-small-action", "삭제"); remove.type = "button"; remove.disabled = !editable || region.parts.length <= 1;
      remove.addEventListener("click", () => edit({ action: "delete_part", region_id: region.region_id, part_index: index }));
      row.append(choose, remove); parts.append(row);
    });
    advanced.append(parts);
    const part = region.parts?.[state.partIndex];
    if (part) {
      const coords = node("div", "exam-coordinate-fields");
      const inputs = part.box.map((value, index) => {
        const wrap = node("label", "", ["왼쪽", "위", "오른쪽", "아래"][index]);
        const input = node("input"); input.type = "number"; input.min = "0"; input.max = "1"; input.step = "0.001"; input.value = value; input.disabled = !editable;
        wrap.append(input); coords.append(wrap); return input;
      });
      inputs.forEach((input) => input.addEventListener("change", () => edit({ action: "box", region_id: region.region_id,
        part_index: state.partIndex, box: inputs.map((field) => Number(field.value)) })));
      advanced.append(coords);
    }
    const actions = node("div", "exam-region-actions");
    const partPage = node("select"); partPage.setAttribute("aria-label", "추가할 부분의 쪽");
    state.draft.selected_pages.forEach((choice) => partPage.add(new Option(`${choice.number}쪽`, String(choice.number))));
    partPage.value = String(state.page);
    const addPart = node("button", "button", "부분 추가"); addPart.type = "button"; addPart.disabled = !editable;
    addPart.addEventListener("click", () => edit({ action: "add_part", region_id: region.region_id,
      part: { page_number: Number(partPage.value), role: "stem", box: [.1, .1, .9, .3] } }));
    const partActions = node("div", "exam-region-actions"); partActions.append(partPage, addPart);
    advanced.append(partActions);
    const confirm = node("button", "button", region.confirmed ? "확정 취소" : "이 영역 확정"); confirm.type = "button"; confirm.disabled = !editable || !region.selected;
    confirm.addEventListener("click", () => {
      state.advancedRegionId = null; state.cropOpenRegionId = null;
      edit({ action: "confirm", region_id: region.region_id, value: !region.confirmed });
    });
    const remove = node("button", "exam-delete", "영역 삭제"); remove.type = "button"; remove.disabled = !editable;
    remove.addEventListener("click", () => edit({ action: "delete", region_id: region.region_id }));
    actions.append(confirm, remove); card.append(actions);
    if (region.selected && region.confirmed) {
      const cleaning = node("details", "exam-crop-disclosure");
      cleaning.open = state.cropOpenRegionId === region.region_id;
      cleaning.append(node("summary", "", "필기가 남았을 때만 추가 제거"));
      if (cleaning.open) cleaning.append(cropWorkbench(region));
      cleaning.addEventListener("toggle", () => {
        if (!cleaning.isConnected) return;
        if (cleaning.open) {
          if (state.cropOpenRegionId === region.region_id) return;
          state.cropOpenRegionId = region.region_id;
          refreshCrop(region.region_id).catch(showError);
        } else if (state.cropOpenRegionId === region.region_id) state.cropOpenRegionId = null;
      });
      advanced.append(cleaning);
    }
    card.append(advanced);
    return card;
  }

  function cropWorkbench(region) {
    const workbench = node("section", "exam-crop-workbench");
    workbench.setAttribute("aria-label", `${region.printed_number || region.order}번 문항 필기 추가 제거`);
    const crop = state.cropRegionId === region.region_id ? state.crop : null;
    if (!crop) { workbench.append(node("p", "exam-help", "문항 이미지를 불러오는 중입니다…")); return workbench; }
    const url = cropUrl(region.region_id);
    const current = crop.candidates.filter((item) => item.base_sha256 === crop.base_sha256);
    const candidate = current.find((item) => item.revision === state.candidateRevision);
    const published = state.publication?.items?.some((item) => item.region_id === region.region_id && item.status === "succeeded");
    const inputLabel = crop.adopted_revision == null ? "페이지에서 자른 원본" : `추가 정리본 ${crop.adopted_revision}번`;
    workbench.append(node("h4", "", "이 문항 필기 추가 제거"));
    workbench.append(node("p", "exam-help", `타이핑 입력: ${inputLabel} · 상태 ${crop.status} · 수정 ${crop.revision}${crop.error_code ? ` · 오류 ${crop.error_code}` : ""}`));
    if (published) workbench.append(node("p", "exam-help", "이미 저장된 문항은 이 화면에서 바꾸지 않습니다."));
    const compare = node("div", "exam-crop-compare");
    const before = node("figure"); before.append(node("figcaption", "", "현재 사용 중인 crop · 영역 힌트"));
    const stage = node("div", "exam-crop-stage");
    const base = node("img"); base.src = `${url}/active?sha=${crop.active_sha256}`; base.alt = "현재 타이핑 입력으로 선택된 문항 이미지";
    const overlay = node("div", "exam-crop-overlay"); overlay.tabIndex = 0;
    overlay.setAttribute("role", "group");
    overlay.setAttribute("aria-label", "제거할 부분을 드래그하세요. 화살표로 이동, Shift와 화살표로 크기를 조정합니다.");
    const hint = node("div", "exam-crop-hint");
    if (state.cropArea) position(hint, state.cropArea); else hint.hidden = true;
    overlay.append(hint); stage.append(base, overlay); before.append(stage);
    if (crop.adopted_revision != null) {
      const original = node("details"), summary = node("summary", "", "처음 자른 이미지 보기");
      const image = node("img"); image.src = `${url}?sha=${crop.base_sha256}`;
      image.alt = "처음 확정한 문항 crop"; original.append(summary, image); before.append(original);
    }
    compare.append(before);
    const after = node("figure"); after.append(node("figcaption", "", "추가 정리 후보"));
    if (candidate) {
      const image = node("img"); image.src = `${url}/candidates/${candidate.revision}?sha=${candidate.sha256}`;
      image.alt = `${candidate.revision}번 정리 후보 이미지`; after.append(image);
    } else after.append(node("p", "exam-empty", "아직 정리 후보가 없습니다."));
    compare.append(after); workbench.append(compare);
    const help = node("p", "exam-help", "사각형은 지울 위치를 알려주는 힌트이며 픽셀 마스크는 아닙니다. 인쇄 내용은 원본과 비교하세요.");
    workbench.append(help);
    const coords = node("div", "exam-coordinate-fields");
    const inputs = ["왼쪽", "위", "오른쪽", "아래"].map((label, index) => {
      const wrap = node("label", "", label), input = node("input");
      input.type = "number"; input.min = "0"; input.max = "1"; input.step = "0.001";
      input.value = state.cropArea?.[index] ?? ""; input.placeholder = "0–1";
      wrap.append(input); coords.append(wrap); return input;
    });
    inputs.forEach((input) => input.addEventListener("change", () => {
      const values = inputs.map((field) => Number(field.value));
      if (!inputs.every((field) => field.value !== "") || !values.every(Number.isFinite)) return;
      state.cropArea = clampBox(normalizeBox(values)).map(round); renderRegions();
    }));
    workbench.append(coords);
    const point = (event) => {
      const rect = overlay.getBoundingClientRect();
      const [x, y] = screenToImage(event.clientX, event.clientY, rect, rect.width, rect.height);
      return [clamp(x / rect.width, 0, 1), clamp(y / rect.height, 0, 1)];
    };
    let dragging = null;
    overlay.addEventListener("pointerdown", (event) => {
      if (published) return;
      dragging = { origin: point(event), pointerId: event.pointerId };
      overlay.setPointerCapture(event.pointerId); event.preventDefault();
    });
    overlay.addEventListener("pointermove", (event) => {
      if (!dragging || dragging.pointerId !== event.pointerId) return;
      position(hint, clampBox(normalizeBox([...dragging.origin, ...point(event)]))); hint.hidden = false;
    });
    overlay.addEventListener("pointerup", (event) => {
      if (!dragging || dragging.pointerId !== event.pointerId) return;
      const origin = dragging.origin, end = point(event); dragging = null;
      const rect = overlay.getBoundingClientRect();
      if (Math.abs(end[0] - origin[0]) * rect.width < 5 || Math.abs(end[1] - origin[1]) * rect.height < 5) {
        renderRegions(); return;
      }
      state.cropArea = clampBox(normalizeBox([...origin, ...end])).map(round); renderRegions();
    });
    overlay.addEventListener("pointercancel", () => { dragging = null; renderRegions(); });
    overlay.addEventListener("keydown", (event) => {
      const delta = { ArrowLeft: [-.005, 0], ArrowRight: [.005, 0], ArrowUp: [0, -.005], ArrowDown: [0, .005] }[event.key];
      if (!delta || published) return;
      event.preventDefault();
      const handle = event.shiftKey ? ({ ArrowLeft: "w", ArrowRight: "e", ArrowUp: "n", ArrowDown: "s" })[event.key] : "move";
      state.cropArea = moveBox(state.cropArea || [.25, .25, .75, .75], ...delta, handle);
      renderRegions(); ui.examRegions.querySelector(".exam-crop-overlay")?.focus();
    });
    const actions = node("div", "exam-region-actions");
    const clear = node("button", "button", "영역 힌트 지우기"); clear.type = "button"; clear.disabled = !state.cropArea || published;
    clear.addEventListener("click", () => { state.cropArea = null; renderRegions(); });
    const clean = node("button", "button", crop.status === "running" || state.cropPending ? "정리 중…" : "이 문항 필기 추가 제거");
    clean.type = "button"; clean.disabled = published || crop.status === "running" || Boolean(state.cropPending);
    clean.addEventListener("click", async () => {
      if (state.cropArea && (state.cropArea[2] - state.cropArea[0] < .005 || state.cropArea[3] - state.cropArea[1] < .005)) {
        report("영역 힌트의 가로·세로 크기를 더 넓혀 주세요.", true); return;
      }
      try {
        await request(`${url}/clean`, { method: "POST", json: { expected_revision: crop.revision, area: state.cropArea } });
        state.cropPending = { regionId: region.region_id, revision: crop.revision };
        await refreshCrop(region.region_id); report("이 문항만 추가 정리를 시작했습니다.");
        state.poll = setTimeout(() => loadPage().catch(showError), 1500);
      } catch (error) { await conflict(error); }
    });
    const select = node("select"); select.setAttribute("aria-label", "확인할 정리 후보");
    current.forEach((item) => select.add(new Option(`후보 ${item.revision} · ${item.sha256.slice(0, 8)}`, String(item.revision))));
    select.value = String(state.candidateRevision ?? ""); select.disabled = !current.length;
    select.addEventListener("change", () => { state.candidateRevision = Number(select.value); renderRegions(); });
    const adopt = node("button", "button primary", "이 결과 사용"); adopt.type = "button";
    adopt.disabled = published || !candidate || crop.adopted_revision === candidate.revision || crop.status === "running";
    adopt.addEventListener("click", async () => {
      try {
        state.crop = await request(`${url}/adopt`, { method: "POST", json: { expected_revision: crop.revision, candidate_revision: candidate.revision } });
        renderRegions(); report("선택한 정리본을 타이핑 입력으로 지정했습니다. 타이핑은 아직 시작되지 않았습니다.");
      } catch (error) { await conflict(error); }
    });
    const restore = node("button", "button", "직전 이미지로 복원"); restore.type = "button";
    restore.disabled = published || crop.adopted_revision == null || crop.status === "running";
    restore.addEventListener("click", async () => {
      try {
        state.crop = await request(`${url}/restore`, { method: "POST", json: {
          expected_revision: crop.revision, candidate_revision: previousCropRevision(crop) } });
        renderRegions(); report("직전 문항 이미지를 타이핑 입력으로 복원했습니다.");
      } catch (error) { await conflict(error); }
    });
    actions.append(clear, clean, select, adopt, restore); workbench.append(actions);
    return workbench;
  }

  function drawOverlay() {
    ui.examOverlay.replaceChildren();
    const editable = ui.examCanvas.dataset.editable === "true" && state.selection?.status === "ready";
    if (!editable) return;
    (state.selection.regions || []).forEach((region) => (region.parts || []).forEach((part, index) => {
      if (part.page_number !== state.page) return;
      const box = node("div", "exam-box");
      box.dataset.region = region.region_id; box.dataset.part = String(index);
      box.dataset.active = String(state.regionId === region.region_id && state.partIndex === index);
      box.tabIndex = 0; box.setAttribute("role", "button");
      box.setAttribute("aria-label", `${region.printed_number || region.order}번 ${part.role} 영역. 화살표로 이동`);
      position(box, part.box);
      const label = node("span", "exam-box-label", `${region.printed_number || region.order}${region.parts.length > 1 ? ` · ${index + 1}` : ""}`);
      box.append(label);
      if (box.dataset.active === "true") for (const handle of ["nw", "n", "ne", "e", "se", "s", "sw", "w"]) {
        const grip = node("button", `exam-handle exam-handle-${handle}`); grip.type = "button"; grip.dataset.handle = handle;
        grip.setAttribute("aria-label", `${handle} 방향 크기 조절`); box.append(grip);
      }
      ui.examOverlay.append(box);
    }));
  }

  function position(element, box) {
    element.style.left = `${box[0] * 100}%`;
    element.style.top = `${box[1] * 100}%`;
    element.style.width = `${(box[2] - box[0]) * 100}%`;
    element.style.height = `${(box[3] - box[1]) * 100}%`;
  }

  function pointerPoint(event) {
    const rect = ui.examOverlay.getBoundingClientRect();
    const width = ui.examCanvasImage.naturalWidth || rect.width;
    const height = ui.examCanvasImage.naturalHeight || rect.height;
    const [x, y] = screenToImage(event.clientX, event.clientY, rect, width, height);
    return [clamp(x / width, 0, 1), clamp(y / height, 0, 1)];
  }

  function pointerDown(event) {
    if (ui.examCanvas.dataset.editable !== "true") return;
    const boxElement = event.target.closest(".exam-box");
    const point = pointerPoint(event);
    if (state.adding && !boxElement) {
      const preview = node("div", "exam-box exam-box-preview"); ui.examOverlay.append(preview);
      position(preview, [point[0], point[1], point[0], point[1]]);
      state.drag = { kind: "add", origin: point, preview, pointerId: event.pointerId };
    } else if (boxElement) {
      const region = state.selection.regions.find((item) => item.region_id === boxElement.dataset.region);
      const index = Number(boxElement.dataset.part);
      if (!region?.parts?.[index]) return;
      state.regionId = region.region_id; state.partIndex = index;
      state.drag = { kind: "edit", origin: point, original: [...region.parts[index].box],
        box: boxElement, handle: event.target.dataset.handle || "move", pointerId: event.pointerId };
      renderRegions();
      state.drag.box = ui.examOverlay.querySelector(`.exam-box[data-region="${region.region_id}"][data-part="${index}"]`);
    } else return;
    ui.examOverlay.setPointerCapture(event.pointerId);
    event.preventDefault();
  }

  function pointerMove(event) {
    if (!state.drag || state.drag.pointerId !== event.pointerId) return;
    const point = pointerPoint(event), dx = point[0] - state.drag.origin[0], dy = point[1] - state.drag.origin[1];
    if (state.drag.kind === "edit") position(state.drag.box, moveBox(state.drag.original, dx, dy, state.drag.handle));
    else position(state.drag.preview, clampBox([...state.drag.origin, ...point]));
  }

  function pointerUp(event) {
    if (!state.drag || state.drag.pointerId !== event.pointerId) return;
    const drag = state.drag; state.drag = null;
    const point = pointerPoint(event);
    if (drag.kind === "edit") {
      const box = moveBox(drag.original, point[0] - drag.origin[0], point[1] - drag.origin[1], drag.handle);
      edit({ action: "box", region_id: state.regionId, part_index: state.partIndex, box });
    } else {
      const rect = ui.examOverlay.getBoundingClientRect();
      if (Math.abs(point[0] - drag.origin[0]) * rect.width < 5 || Math.abs(point[1] - drag.origin[1]) * rect.height < 5) {
        drag.preview.remove(); report("영역은 가로·세로 5px 이상 드래그해 추가하세요.", true); return;
      }
      const box = clampBox([...drag.origin, ...point]).map(round);
      edit({ action: "add", parts: [{ page_number: state.page, role: "stem", box }] });
    }
  }

  ui.examUploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = ui.examFile.files?.[0]; if (!file) return;
    const type = file.type || ({ pdf: "application/pdf", png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", webp: "image/webp" })[file.name.split(".").pop().toLowerCase()];
    if (!type) { report("PDF, PNG, JPEG, WEBP 파일만 선택하세요.", true); return; }
    report("원본을 확인하고 저장하는 중입니다…");
    try {
      const draft = await request("/api/exam/drafts", { method: "POST", headers: { "Content-Type": type, "X-Upload-Name": encodeURIComponent(file.name) }, body: file });
      await listDrafts(); await openDraft(draft.draft_id);
    } catch (error) { showError(error); }
  });
  ui.examDraftSelect.addEventListener("change", () => { if (ui.examDraftSelect.value) openDraft(ui.examDraftSelect.value).catch(showError); });
  ui.examRefresh.addEventListener("click", () => listDrafts().catch(showError));
  ui.examSavePages.addEventListener("click", savePages);
  ui.examClean.addEventListener("click", cleanPage);
  ui.examDetect.addEventListener("click", detectPage);
  ui.examRetry.addEventListener("click", () => (state.clean?.status === "failed" ? cleanPage() : detectPage()));
  ui.examSelectAll.addEventListener("click", () => edit({ action: "select_all", value: true }));
  ui.examSelectNone.addEventListener("click", () => edit({ action: "select_all", value: false }));
  ui.examUndo.addEventListener("click", () => edit({ action: "undo" }));
  async function prepareManualRegion() {
    if (ui.examCanvas.dataset.editable === "true" && state.selection?.status === "ready") return true;
    if (!state.draft || !state.page || !state.selection) { report("먼저 시험지를 여세요.", true); return false; }
    if (ui.examCleanEnabled.checked) {
      ui.examAddChoice.showModal();
      const useOriginal = await new Promise((resolve) => ui.examAddChoice.addEventListener("close", () =>
        resolve(ui.examAddChoice.returnValue === "original"), { once: true }));
      if (!useOriginal) return false;
    }
    try {
      await request(`${pageUrl()}/skip-clean`, { method: "POST", json: { expected_revision: state.selection.revision } });
      await loadPage();
      return ui.examCanvas.dataset.editable === "true" && state.selection?.status === "ready";
    } catch (error) { await conflict(error); return false; }
  }
  ui.examDrawRegion.addEventListener("click", async () => {
    if (!state.adding && !await prepareManualRegion()) return;
    state.adding = !state.adding;
    ui.examDrawRegion.setAttribute("aria-pressed", String(state.adding));
    ui.examDrawRegion.textContent = state.adding ? "드래그 종료" : "드래그로 추가";
    ui.examOverlay.dataset.adding = String(state.adding);
    report(state.adding ? "이미지의 빈 곳에서 새 영역을 드래그하세요." : "드래그 모드를 종료했습니다.");
  });
  ui.examConfirmAll.addEventListener("click", async () => {
    for (const region of state.selection.regions.filter((item) => item.selected && !item.confirmed)) {
      if (!await edit({ action: "confirm", region_id: region.region_id, value: true }, true)) return;
    }
    report("선택한 영역을 확정했습니다. 문항 저장은 아직 시작되지 않았습니다.");
  });
  ui.examPublish.addEventListener("click", async () => {
    if (ui.examPublish.disabled || state.busy) return;
    state.busy = true;
    try {
      await request(`/api/exam/drafts/${state.draft.draft_id}/publication`, { method: "POST" });
      state.publishPending = true;
      await loadPage();
      report("확정한 문항의 타이핑·저장을 시작했습니다.");
    } catch (error) { state.publishPending = false; await conflict(error); }
    finally { state.busy = false; }
  });
  ui.examZoom.addEventListener("input", () => { const zoom = Number(ui.examZoom.value); ui.examCanvas.style.width = `${zoom}%`; ui.examZoomValue.value = `${zoom}%`; });
  ui.examOverlay.addEventListener("pointerdown", pointerDown);
  ui.examOverlay.addEventListener("pointermove", pointerMove);
  ui.examOverlay.addEventListener("pointerup", pointerUp);
  ui.examOverlay.addEventListener("pointercancel", () => { state.drag = null; drawOverlay(); });
  ui.examCanvas.addEventListener("keydown", (event) => {
    const delta = { ArrowLeft: [-.005, 0], ArrowRight: [.005, 0], ArrowUp: [0, -.005], ArrowDown: [0, .005] }[event.key];
    if (!delta || !state.regionId) return;
    const region = state.selection.regions.find((item) => item.region_id === state.regionId);
    const part = region?.parts?.[state.partIndex]; if (!part) return;
    event.preventDefault();
    const handle = event.shiftKey ? ({ ArrowLeft: "w", ArrowRight: "e", ArrowUp: "n", ArrowDown: "s" })[event.key] : "move";
    edit({ action: "box", region_id: state.regionId, part_index: state.partIndex, box: moveBox(part.box, ...delta, handle) });
  });
  document.querySelectorAll('input[name="workMode"]').forEach((radio) => radio.addEventListener("change", () => mode(radio.value)));
  let initialMode = "batch";
  try { initialMode = sessionStorage.getItem(MODE_KEY) || "batch"; } catch {}
  const selectedMode = document.querySelector(`input[name="workMode"][value="${initialMode}"]`);
  if (selectedMode) selectedMode.checked = true;
  mode(initialMode);
}
