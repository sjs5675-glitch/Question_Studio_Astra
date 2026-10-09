"use strict";

// '문항 생성' 메뉴: 주제 선택 → 생성(빠른 생성 또는 봇과 상의) → 그림·검사·Codex 검증 → 검토 → 확정(문제은행 저장).

const API = "/api/item-generation";
const STATUS_LABEL = {
  queued: "대기 중", running: "진행 중", ready: "검토 대기", partial: "일부 완료", paused: "사용자 재개 대기", failed: "실패", cancelled: "취소됨",
};
const VERIFY_LABEL = {
  verified: ["Codex 정답 일치", "ok"], answer_conflict: ["Codex 정답 불일치", "bad"], item_issue: ["Codex 문항 문제 지적", "bad"],
  needs_review: ["답 표현 확인 필요", "warn"], not_solved: ["Codex 미풀이", "warn"], error: ["검증 오류", "bad"], pending: ["검증 대기", "wait"],
};
const KIND_LABEL = { variant: "변형", new: "신규" };
const REVIEW_LABEL = {
  not_run: ["교차 검토 미실시", "wait"], self_check: ["같은 모델 자체 검산 · 교차 검토 미실시", "warn"],
  cross_check: ["교차 검토 · 답 일치", "ok"], mixed: ["혼합 작성 · 독립 교차 검토 아님", "warn"],
  unknown: ["작성·검토 모델 확인 불가", "warn"], stale: ["이전 내용의 검토 · 다시 검토 필요", "warn"],
  conflict: ["검토 답 불일치", "bad"], item_issue: ["검토에서 문항 문제 지적", "bad"],
  blocked: ["검토 차단 · 모델/입력 확인 필요", "warn"], error: ["검토 오류", "bad"],
  queued: ["검토 대기", "wait"], running: ["검토 진행 중", "wait"], paused: ["검토 일시정지 · 사용자 재개 필요", "warn"],
};

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== "" && text !== null && text !== undefined) element.textContent = String(text);
  return element;
}

function button(text, className = "button", onClick = null) {
  const element = node("button", className, text);
  element.type = "button";
  if (onClick) element.addEventListener("click", onClick);
  return element;
}

async function call(path, options = {}) {
  const init = { method: options.method || "GET", credentials: "same-origin", headers: { Accept: "application/json" } };
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(`${API}${path}`, init);
  const raw = await response.text();
  let body = null;
  try { body = raw ? JSON.parse(raw) : null; } catch { body = null; }
  if (!response.ok) throw new Error(body?.message || `요청 실패 (${response.status})`);
  return body;
}

const q = (params) => "?" + new URLSearchParams(params).toString();

function renderMath(container) {
  if (!window.katex) return;
  container.querySelectorAll("[data-math-text]").forEach((element) => {
    const text = element.textContent;
    if (!text.includes("$")) return;
    element.textContent = "";
    text.split(/(\$[^$]+\$)/).forEach((part) => {
      if (/^\$[^$]+\$$/.test(part)) {
        const span = node("span");
        try { window.katex.render(part.slice(1, -1), span, { throwOnError: false }); } catch { span.textContent = part; }
        element.append(span);
      } else if (part) {
        element.append(document.createTextNode(part));
      }
    });
  });
}

function mathText(tag, className, text) {
  const element = node(tag, className, text);
  element.dataset.mathText = "1";
  return element;
}

export function mountItemGeneration(root) {
  if (!root || root.dataset.mounted === "1") return;
  root.dataset.mounted = "1";
  const state = {
    scopes: [], tree: null, scope: "", major: "", middle: "", details: new Set(), sources: [], sourceIds: new Set(),
    jobs: [], job: null, selected: new Set(), pollTimer: null, chat: null, chatTimer: null, showAllDetails: false, openMore: new Set(),
    reviewSelected: new Set(), reviewFilter: "all", reviewModel: null, openHistory: new Set(), reviseText: new Map(),
    selectionInitialized: false,
  };

  const layout = node("div", "gen-layout");
  const left = node("section", "gen-panel gen-setup");
  const right = node("section", "gen-panel gen-result");
  layout.append(left, right);
  root.replaceChildren(layout);
  const status = node("p", "gen-status");
  status.setAttribute("role", "status");
  root.prepend(status);
  const say = (text, tone = "") => { status.textContent = text || ""; status.dataset.tone = tone; };

  // ---------------------------------------------------------------- setup form
  const scopeSelect = node("select");
  const majorSelect = node("select");
  const middleSelect = node("select");
  const search = node("input");
  search.type = "search";
  search.placeholder = "주제 검색 (예: 산화 환원, 엘니뇨)";
  const searchResults = node("ul", "gen-search-results");
  const detailBox = node("div", "gen-chips");
  const conceptBox = node("p", "gen-concepts");
  const variants = Object.assign(node("input"), { type: "number", min: 0, max: 50, value: 2 });
  const fresh = Object.assign(node("input"), { type: "number", min: 0, max: 50, value: 2 });
  const difficulty = node("select");
  ["중,상", "상", "중", "하,중", "하"].forEach((v) => difficulty.append(Object.assign(node("option", "", v), { value: v })));
  const format = node("select");
  [["", "자동"], ["보기(ㄱㄴㄷ)형", "보기(ㄱㄴㄷ)형"], ["단일 선택형", "단일 선택형"], ["단답형", "단답형"], ["서술형", "서술형"]]
    .forEach(([v, t]) => format.append(Object.assign(node("option", "", t), { value: v })));
  const figure = node("select");
  [["auto", "필요하면 그림 넣기"], ["use", "문항마다 그림 넣기"], ["none", "그림 없이"]]
    .forEach(([v, t]) => figure.append(Object.assign(node("option", "", t), { value: v })));
  const freeRequest = node("textarea");
  freeRequest.rows = 3;
  freeRequest.placeholder = "자유 요청 (예: 실험 상황을 바꿔서, 그래프 해석 위주로)";
  const sourceBox = node("div", "gen-sources");
  const estimate = node("p", "gen-estimate");
  const startButton = button("문항 생성 시작", "button primary");
  const chatButton = button("봇과 상의하기", "button");

  const field = (label, control, hint = "") => {
    const wrap = node("label", "gen-field");
    wrap.append(node("span", "gen-label", label), control);
    if (hint) wrap.append(node("small", "gen-hint", hint));
    return wrap;
  };
  const row = (...children) => { const r = node("div", "gen-row"); r.append(...children); return r; };

  left.append(
    node("h3", "", "1. 주제 고르기"),
    field("과목", scopeSelect),
    row(field("대단원", majorSelect), field("중단원", middleSelect)),
    field("주제 검색", search), searchResults,
    node("p", "gen-label", "세부 주제 (여러 개 고르기, 안 고르면 단원 전체)"), detailBox,
    conceptBox,
    node("h3", "", "2. 만들 문항"),
    row(field("변형 문항 수", variants), field("신규 문항 수", fresh)),
    node("p", "gen-hint", "변형·신규 합계 최대 50문항. 5문항씩 나눠 처리하며, 문항 수와 그림 수가 늘면 시간·토큰 사용량도 증가합니다."),
    row(field("난이도", difficulty), field("형식", format)),
    field("그림", figure),
    field("자유 요청", freeRequest),
    node("details", "gen-source-details"),
    estimate,
    row(startButton, chatButton),
  );
  const sourceDetails = left.querySelector(".gen-source-details");
  sourceDetails.append(node("summary", "", "변형할 원문항 직접 고르기 (안 고르면 자동)"), sourceBox);

  function fillSelect(select, values, current) {
    select.replaceChildren(...values.map((v) => Object.assign(node("option", "", v), { value: v })));
    if (values.includes(current)) select.value = current;
  }

  function currentMiddle() {
    const major = state.tree?.majors.find((m) => m.major === state.major);
    return major?.middles.find((m) => m.middle === state.middle) || null;
  }

  function renderTopic() {
    const majors = state.tree?.majors || [];
    fillSelect(majorSelect, majors.map((m) => m.major), state.major);
    state.major = majorSelect.value;
    const middles = majors.find((m) => m.major === state.major)?.middles || [];
    fillSelect(middleSelect, middles.map((m) => m.middle), state.middle);
    state.middle = middleSelect.value;
    const middle = currentMiddle();
    const allDetails = middle?.details || [];
    const shown = state.showAllDetails ? allDetails : allDetails.filter((d, i) => i < 12 || state.details.has(d.name));
    detailBox.replaceChildren(...shown.map((d) => {
      const chip = button(`${d.name} (${d.count})`, "gen-chip");
      chip.setAttribute("aria-pressed", state.details.has(d.name) ? "true" : "false");
      chip.addEventListener("click", () => {
        if (state.details.has(d.name)) state.details.delete(d.name); else state.details.add(d.name);
        renderTopic();
        refreshEstimate();
      });
      return chip;
    }));
    if (allDetails.length > shown.length || state.showAllDetails) {
      detailBox.append(button(state.showAllDetails ? "접기" : `더 보기 (${allDetails.length - shown.length}개)`, "gen-link", () => {
        state.showAllDetails = !state.showAllDetails;
        renderTopic();
      }));
    }
    if (middle && !middle.details.length) detailBox.append(node("span", "gen-hint", "분석된 세부 주제가 없습니다."));
    conceptBox.textContent = middle
      ? `허용 개념 ${middle.concepts.length}개 · 분석 문항 ${middle.analysis_count}개 · 패턴 문서 ${middle.pattern_available ? "있음" : "없음"} · Codex 검증 ${state.tree.codex_effort}`
      : "";
  }

  async function loadSources() {
    sourceBox.replaceChildren(node("p", "gen-hint", "불러오는 중…"));
    try {
      const data = await call(`/sources${q({ major: state.major, middle: state.middle })}`);
      state.sources = data.items || [];
    } catch (error) { sourceBox.replaceChildren(node("p", "gen-hint", error.message)); return; }
    state.sourceIds = new Set([...state.sourceIds].filter((id) => state.sources.some((s) => s.item_id === id)));
    const list = node("ul", "gen-source-list");
    state.sources.forEach((source) => {
      const item = node("li");
      const box = Object.assign(node("input"), { type: "checkbox", checked: state.sourceIds.has(source.item_id) });
      box.disabled = source.answer_doubt;
      box.addEventListener("change", () => { box.checked ? state.sourceIds.add(source.item_id) : state.sourceIds.delete(source.item_id); });
      const label = node("label");
      label.append(box, node("strong", "", `${source.printed_number ?? "?"}번`),
        node("span", "gen-badge", source.difficulty || "-"), source.has_figure ? node("span", "gen-badge", "그림") : "",
        source.answer_doubt ? node("span", "gen-badge bad", "정답 의심") : "",
        node("span", "gen-hint", ` ${source.detail_topic || ""}`));
      item.append(label, mathText("p", "gen-source-preview", source.preview));
      list.append(item);
    });
    sourceBox.replaceChildren(list);
    renderMath(sourceBox);
  }

  function requestBody() {
    return {
      scope: state.scope, major: state.major, middle: state.middle, details: [...state.details],
      variants: Number(variants.value || 0), new: Number(fresh.value || 0), difficulty: difficulty.value,
      format: format.value || null, figure: figure.value, free_request: freeRequest.value.trim() || null,
      source_ids: [...state.sourceIds],
    };
  }

  let estimateTimer = null;
  function refreshEstimate() {
    window.clearTimeout(estimateTimer);
    estimateTimer = window.setTimeout(async () => {
      try {
        const data = await call("/estimate", { method: "POST", body: requestBody() });
        const e = data.estimate;
        estimate.textContent = `예상 호출: 생성(Claude Opus ${e.claude_effort}) ${e.claude_generate}회 · 그림 최대 ${e.figure_codex_max}회 · 그림 대조 최대 ${e.figure_check_claude_max}회 · Codex 검증(${e.codex_effort}) ${e.codex_verify}회`;
        estimate.dataset.tone = "";
        startButton.disabled = false;
      } catch (error) {
        estimate.textContent = error.message;
        estimate.dataset.tone = "bad";
        startButton.disabled = true;
      }
    }, 250);
  }

  scopeSelect.addEventListener("change", async () => { state.scope = scopeSelect.value; await loadTree(); });
  majorSelect.addEventListener("change", () => { state.major = majorSelect.value; state.middle = ""; state.showAllDetails = false; state.details.clear(); state.sourceIds.clear(); renderTopic(); loadSources(); refreshEstimate(); });
  middleSelect.addEventListener("change", () => { state.middle = middleSelect.value; state.details.clear(); state.sourceIds.clear(); renderTopic(); loadSources(); refreshEstimate(); });
  [variants, fresh, difficulty, format, figure].forEach((control) => control.addEventListener("change", refreshEstimate));
  let searchTimer = null;
  search.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(async () => {
      if (!search.value.trim()) { searchResults.replaceChildren(); return; }
      const data = await call(`/topics${q({ scope: state.scope, q: search.value })}`).catch(() => ({ results: [] }));
      searchResults.replaceChildren(...data.results.slice(0, 12).map((hit) => {
        const item = node("li");
        item.append(button(`${hit.major} › ${hit.middle}${hit.kind === "middle" ? "" : ` › ${hit.text}`}`, "gen-link", () => {
          state.major = hit.major; state.middle = hit.middle; state.details.clear(); state.sourceIds.clear();
          if (hit.kind === "detail") state.details.add(hit.text);
          search.value = ""; searchResults.replaceChildren();
          renderTopic(); loadSources(); refreshEstimate();
        }));
        return item;
      }));
      if (!data.results.length) searchResults.append(node("li", "gen-hint", "찾은 주제가 없습니다."));
    }, 250);
  });

  startButton.addEventListener("click", async () => {
    startButton.disabled = true;
    try {
      const job = await call("/jobs", { method: "POST", body: requestBody() });
      say(`생성 작업을 시작했습니다 (${job.job_id}).`, "ok");
      await loadJobs();
      openJob(job.job_id);
    } catch (error) { say(error.message, "bad"); }
    finally { startButton.disabled = false; }
  });
  chatButton.addEventListener("click", () => openChat(null));

  async function loadTree() {
    state.tree = await call(`/topics${q({ scope: state.scope })}`);
    state.major = state.tree.majors[0]?.major || "";
    state.middle = "";
    state.details.clear();
    state.sourceIds.clear();
    renderTopic();
    await loadSources();
    refreshEstimate();
  }

  // ---------------------------------------------------------------- jobs & drafts
  const jobBar = node("div", "gen-jobbar");
  const jobSelect = node("select");
  const refreshButton = button("새로고침", "button small", () => loadJobs().then(() => state.job && openJob(state.job.job_id)));
  jobBar.append(field("생성 작업", jobSelect), refreshButton);
  const jobBody = node("div", "gen-job");
  right.append(node("h3", "", "3. 검토하고 확정하기"), jobBar, jobBody);
  jobSelect.addEventListener("change", () => jobSelect.value && openJob(jobSelect.value));

  async function loadJobs() {
    const data = await call("/jobs");
    state.jobs = data.jobs || [];
    jobSelect.replaceChildren(Object.assign(node("option", "", state.jobs.length ? "작업 고르기" : "아직 작업이 없습니다"), { value: "" }),
      ...state.jobs.map((job) => Object.assign(node("option", "",
        `${job.created_at?.slice(5, 16).replace("T", " ") || ""} · ${job.unit?.middle || ""} · ${STATUS_LABEL[job.status] || job.status} · ${job.draft_count}문항${job.confirmed_count ? ` (저장 ${job.confirmed_count})` : ""}${job.route === "bot" ? " · 봇" : ""}`),
      { value: job.job_id })));
    if (state.job) jobSelect.value = state.job.job_id;
  }

  async function openJob(jobId) {
    window.clearTimeout(state.pollTimer);
    const previous = state.job;
    try { state.job = await call(`/jobs/${encodeURIComponent(jobId)}`); }
    catch (error) { say(error.message, "bad"); return; }
    jobSelect.value = jobId;
    if (previous?.job_id !== jobId) {
      state.selected.clear(); state.reviewSelected.clear(); state.reviewModel = null; state.reviewFilter = "all";
      state.selectionInitialized = false;
    }
    const busy = ["queued", "running"].includes(state.job.status);
    if (!busy && !state.selectionInitialized) {
      state.job.drafts.forEach((d) => {
        if (!d.discarded && !d.confirmed_item_id && d.duplicate_check?.status !== "duplicate"
          && d.verification?.status === "verified" && d.auto_checks?.passed) state.selected.add(d.no);
      });
      state.selectionInitialized = true;
    }
    // Skip the rebuild when a poll returns the same saved job: keeps scroll, open sections and typed text.
    const unchanged = previous && previous.job_id === state.job.job_id && previous.revision === state.job.revision
      && previous.projection_sha256 === state.job.projection_sha256
      && previous.updated_at === state.job.updated_at && jobBody.childElementCount;
    if (!unchanged) renderJob();
    if (busy) state.pollTimer = window.setTimeout(() => openJob(jobId), 4000);
    else if (state.jobs.find((j) => j.job_id === jobId)?.status !== state.job.status) loadJobs();
  }

  function badge(text, tone = "") { return node("span", `gen-badge ${tone}`, text); }

  function renderDraft(draft) {
    const job = state.job;
    const card = node("article", "gen-draft");
    if (draft.discarded) card.classList.add("discarded");
    const head = node("header", "gen-draft-head");
    const pick = Object.assign(node("input"), { type: "checkbox", checked: state.selected.has(draft.no) });
    const duplicate = draft.duplicate_check?.status === "duplicate";
    pick.disabled = Boolean(draft.discarded || draft.confirmed_item_id || duplicate || ["queued", "running"].includes(job.status));
    pick.setAttribute("aria-label", `${draft.no}번 확정 선택`);
    pick.addEventListener("change", () => { pick.checked ? state.selected.add(draft.no) : state.selected.delete(draft.no); renderConfirmBar(); });
    const [vText, vTone] = VERIFY_LABEL[draft.verification?.status || "pending"] || [draft.verification?.status, "warn"];
    const code = draft.item_code ? node("code", "gen-item-code", draft.item_code) : null;
    head.append(pick, node("strong", "", `${draft.no}번`), ...(code ? [code] : []), badge(KIND_LABEL[draft.kind] || draft.kind, "kind"),
      badge(draft.difficulty || "-"), badge(draft.format || "-"), ...(duplicate ? [] : [badge(vText, vTone)]));
    const reviewPick = Object.assign(node("input"), { type: "checkbox", checked: state.reviewSelected.has(draft.no) });
    reviewPick.disabled = Boolean(draft.discarded || ["queued", "running"].includes(job.status));
    reviewPick.setAttribute("aria-label", `${draft.no}번 검토 선택`);
    reviewPick.addEventListener("change", () => {
      reviewPick.checked ? state.reviewSelected.add(draft.no) : state.reviewSelected.delete(draft.no);
      state.reviewModel = null; renderReviewBar();
    });
    const reviewLabel = node("label", "gen-review-pick");
    reviewLabel.append(reviewPick, " 검토 선택");
    head.append(reviewLabel);
    const cross = draft.cross_review || { status: "not_run" };
    const labelKey = cross.status === "not_run" && cross.initial_kind === "self_check" ? "self_check" : cross.status;
    const [reviewText, reviewTone] = REVIEW_LABEL[labelKey] || REVIEW_LABEL.unknown;
    head.append(badge(reviewText, reviewTone));
    if (draft.model_confirmed === false || draft.verification?.model_confirmed === false) {
      head.append(node("span", "gen-hint", "모델 확인 기록 없음 · 교차 검토 판정 불가"));
    }
    head.append(node("span", "gen-hint", `작성: ${(draft.canonical_current?.authors || draft.contributors || [draft.generator_model || "미상"])
      .map((m) => m === "unknown" ? "미상" : m === "human" ? "직접 편집" : m).join(" / ")}`));
    const dup = draft.duplicate_check;
    if (dup?.status === "duplicate" || dup?.status === "similar") {
      const near = (dup.matches || []).map((m) => `${m.code}(${(m.reasons || []).join(", ")})`).join(" / ");
      const label = dup.status === "duplicate" ? "중복 — 제외됨" : "비슷한 문항 있음";
      const tag = badge(label, dup.status === "duplicate" ? "bad" : "warn");
      tag.title = near;
      head.append(tag);
    }
    const checks = draft.auto_checks || {};
    if (checks.scope_ok === false) head.append(badge(`범위 밖 개념: ${(checks.scope_unlisted || []).join(", ")}`, "bad"));
    if (checks.calc_ok === false) head.append(badge("계산 재확인 불일치", "bad"));
    if (checks.calc_ok === true) head.append(badge("계산 재확인 일치", "ok"));
    if (checks.near_copy) head.append(badge("원문항과 너무 비슷함", "bad"));
    if (checks.too_similar_for_new) head.append(badge("기존 문항과 비슷함", "bad"));
    if (draft.figure?.check) head.append(badge(draft.figure.check.status === "ok" ? "그림·본문 일치" : draft.figure.check.status === "mismatch" ? "그림·본문 불일치" : "그림 대조 확인 필요",
      draft.figure.check.status === "ok" ? "ok" : "bad"));
    if (draft.confirmed_item_id) head.append(badge("문제은행 저장됨", "ok"));
    if (draft.discarded) head.append(badge("버림", "wait"));
    card.append(head);

    const body = node("div", "gen-draft-body");
    if (draft.canonical_current) {
      const current = draft.canonical_current;
      body.append(node("p", "gen-hint", `현재 문제은행 문항 · 버전 ${current.revision ?? "확인 불가"}`));
      if (current.error) body.append(node("p", "gen-issue", current.error));
      else {
        body.append(mathText("p", "gen-canonical-text", current.question_text));
        (current.figures || []).forEach((url) => {
          const image = node("img", "gen-figure"); image.alt = "현재 저장 문항의 그림"; image.src = url; body.append(image);
        });
        body.append(mathText("p", "gen-answer", `현재 정답 ${current.answer}`));
        body.append(mathText("p", "gen-canonical-text", current.solution));
      }
    } else {
    if (draft.kind === "variant" && draft.source_printed_number) body.append(node("p", "gen-hint", `원문항: ${draft.source_printed_number}번 · 변형: ${(draft.variant_type || []).join(", ")}`));
    body.append(mathText("p", "gen-stem", draft.stem));
    if (draft.table?.columns) {
      const table = node("table", "gen-table");
      const tr = node("tr");
      draft.table.columns.forEach((c) => tr.append(mathText("th", "", c)));
      table.append(tr);
      (draft.table.rows || []).forEach((r) => { const row = node("tr"); r.forEach((c) => row.append(mathText("td", "", c))); table.append(row); });
      body.append(table);
    }
    if (draft.figure) {
      if (draft.figure.file) {
        const img = node("img", "gen-figure");
        img.alt = draft.figure.alt_text || "생성 그림";
        img.src = `${API}/jobs/${encodeURIComponent(job.job_id)}/drafts/${draft.no}/figure?t=${encodeURIComponent(job.updated_at || "")}`;
        body.append(img);
      } else {
        body.append(node("p", "gen-hint", draft.figure.errors?.length ? `그림 생성 실패: ${draft.figure.errors.join(" / ")}` : `그림 예정: ${draft.figure.alt_text || ""}`));
      }
      (draft.figure.check?.issues || []).forEach((issue) => body.append(node("p", "gen-issue", `그림 대조: ${issue}`)));
    }
    body.append(mathText("p", "gen-instruction", draft.instruction));
    if (draft.bogi) {
      const box = node("div", "gen-bogi");
      box.append(node("span", "gen-label", "<보기>"));
      Object.entries(draft.bogi).forEach(([k, v]) => box.append(mathText("p", "", `${k}. ${v}`)));
      body.append(box);
    }
    if (draft.choices) {
      const list = node("div", "gen-choices");
      Object.entries(draft.choices).forEach(([k, v]) => list.append(mathText("span", "", `${k} ${v}`)));
      body.append(list);
    }
    const answer = node("p", "gen-answer");
    answer.append(node("strong", "", "정답 "), node("span", "", draft.answer),
      draft.verification?.verifier_answer ? node("span", "gen-hint", ` · Codex 답 ${draft.verification.verifier_answer}`) : "");
    body.append(answer);
    if (draft.verification?.verifier_issue) body.append(node("p", "gen-issue", `Codex 지적: ${draft.verification.verifier_issue}`));
    const more = node("details", "gen-more");
    // Polling re-renders the cards every few seconds; keep what the user opened.
    const openKey = `${job.job_id}:${draft.no}`;
    more.open = state.openMore.has(openKey);
    more.addEventListener("toggle", () => { if (more.open) state.openMore.add(openKey); else state.openMore.delete(openKey); });
    more.append(node("summary", "", "해설 · 출제 의도 · Codex 풀이"));
    more.append(node("h4", "", "해설"), mathText("p", "", draft.solution));
    const intent = draft.design_intent || {};
    more.append(node("h4", "", "출제 의도(교사용)"));
    [["평가 목적", intent.purpose], ["측정 능력", intent.assessed_ability], ["구성 이유", intent.structure_rationale],
      ["겨냥한 오개념", (intent.misconceptions_targeted || []).join(" / ")], ["난이도 설계", intent.difficulty_design], ["설계 근거", intent.basis]]
      .forEach(([k, v]) => v && more.append(mathText("p", "", `${k}: ${v}`)));
    if (draft.verification?.verifier_reasoning) more.append(node("h4", "", draft.verification.review_kind === "cross_check" ? "Codex 블라인드 교차 풀이" : "Codex 블라인드 검산 풀이"), mathText("p", "", draft.verification.verifier_reasoning));
    if (checks.calc_results?.length) more.append(node("h4", "", "계산 재확인"), ...checks.calc_results.map((c) => node("p", c.ok ? "" : "gen-issue", `${c.expression} = ${c.computed ?? c.error} (기대 ${c.expected}) ${c.ok ? "✓" : "✗"}`)));
    body.append(more);
    }
    const history = node("details", "gen-more gen-review-history");
    const historyKey = `${job.job_id}:${draft.no}:history`;
    history.open = state.openHistory.has(historyKey);
    history.addEventListener("toggle", () => { history.open ? state.openHistory.add(historyKey) : state.openHistory.delete(historyKey); });
    history.append(node("summary", "", `검토 전체 이력 (${draft.review_history?.length || 0})`));
    if (!draft.review_history?.length) history.append(node("p", "gen-hint", "사용자가 요청한 추가 검토 이력이 없습니다."));
    (draft.review_history || []).forEach((entry) => {
      const section = node("section", "gen-review-record");
      const kind = REVIEW_LABEL[entry.review_kind]?.[0] || "모델 확인 불가";
      section.append(node("strong", "", `${entry.sequence}회 · ${entry.at || ""} · ${entry.actual_model || entry.cli_selected_model || "미확인"} ${entry.actual_effort || entry.requested_effort || ""}`),
        node("p", "gen-hint", `${entry.status} · ${kind}${entry.current === false || entry.stale ? " · 이전 내용(stale)" : ""} · 원본 버전 ${entry.identity?.source_revision ?? "미상"}`),
        mathText("p", "", `원래 정답: ${entry.original_answer || "미상"} · 검토 답: ${entry.verifier_answer || "응답 없음"}`),
        mathText("p", "", `원래 해설: ${entry.original_solution || "없음"}`),
        mathText("p", "", `검토 풀이: ${entry.verifier_reasoning || "없음"}`));
      if (entry.model_evidence === "cli_startup_receipt") section.append(node("p", "gen-hint", "모델 확인 근거: CLI 시작 기록"));
      if (entry.status === "blocked") section.append(node("p", "gen-hint", "모델 확인 기록이 없거나 요청한 모델·추론 강도와 달라 검토 판정을 차단했습니다."));
      if (entry.verifier_issue || entry.error_category) section.append(node("p", "gen-issue", entry.verifier_issue || entry.error_category));
      history.append(section);
    });
    body.append(history);
    card.append(body);

    if (!draft.confirmed_item_id && !["queued", "running", "paused"].includes(job.status)) {
      const actions = node("div", "gen-draft-actions");
      const revise = node("input");
      const reviseKey = `${job.job_id}:${draft.no}`;
      revise.value = state.reviseText.get(reviseKey) || "";
      revise.addEventListener("input", () => state.reviseText.set(reviseKey, revise.value));
      revise.placeholder = "수정 요청 (예: ㄷ 보기를 더 헷갈리게)";
      actions.append(revise,
        button("AI에게 수정 요청", "button small", async () => {
          if (!revise.value.trim()) return;
          try { await call(`/jobs/${encodeURIComponent(job.job_id)}/drafts/${draft.no}/revise`, { method: "POST", body: { instruction: revise.value, expected_revision: job.revision } }); state.reviseText.delete(reviseKey); say(`${draft.no}번 수정을 요청했습니다. 수정 후 다시 검증합니다.`, "ok"); openJob(job.job_id); }
          catch (error) { say(error.message, "bad"); }
        }),
        button("직접 편집", "button small", () => openEditor(draft)),
        button("지침에 추가", "button small", async () => {
          const rule = (revise.value.trim() || window.prompt("다음 생성부터 항상 지킬 지침을 한 줄로 적어 주세요.") || "").trim();
          if (!rule) return;
          try { const r = await call("/guides", { method: "POST", body: { rule } }); say(`지침서에 추가했습니다: ${r.added}`, "ok"); }
          catch (error) { say(error.message, "bad"); }
        }),
        button(draft.discarded ? "되살리기" : "버리기", "button small", async () => {
          try { await call(`/jobs/${encodeURIComponent(job.job_id)}/drafts/${draft.no}/discard`, { method: "POST", body: { discarded: !draft.discarded, expected_revision: job.revision } }); state.selected.delete(draft.no); openJob(job.job_id); }
          catch (error) { say(error.message, "bad"); }
        }));
      if (draft.revise_error) actions.append(node("p", "gen-issue", `수정 실패: ${draft.revise_error}`));
      card.append(actions);
    }
    return card;
  }

  function openEditor(draft) {
    const dialog = node("dialog", "gen-editor");
    const fields = ["stem", "instruction", "answer", "solution"];
    const inputs = {};
    dialog.append(node("h3", "", `${draft.no}번 직접 편집`));
    fields.forEach((key) => {
      const area = node("textarea");
      area.rows = key === "solution" || key === "stem" ? 5 : 2;
      area.value = draft[key] || "";
      inputs[key] = area;
      dialog.append(field({ stem: "자료 설명", instruction: "발문", answer: "정답", solution: "해설" }[key], area));
    });
    for (const key of ["bogi", "choices"]) {
      if (!draft[key]) continue;
      const area = node("textarea");
      area.rows = 5;
      area.value = Object.entries(draft[key]).map(([k, v]) => `${k} ${v}`).join("\n");
      inputs[key] = area;
      dialog.append(field(key === "bogi" ? "보기 (줄마다 'ㄱ 내용')" : "선지 (줄마다 '① 내용')", area));
    }
    const message = node("p", "gen-issue");
    const save = button("저장 후 다시 검증", "button primary", async () => {
      const body = {};
      for (const key of fields) if (inputs[key].value !== (draft[key] || "")) body[key] = inputs[key].value;
      for (const key of ["bogi", "choices"]) {
        if (!inputs[key]) continue;
        const parsed = {};
        inputs[key].value.split("\n").map((line) => line.trim()).filter(Boolean).forEach((line) => {
          const [label, ...rest] = line.split(/\s+/);
          parsed[label.replace(/\.$/, "")] = rest.join(" ");
        });
        if (JSON.stringify(parsed) !== JSON.stringify(draft[key])) body[key] = parsed;
      }
      if (!Object.keys(body).length) { dialog.close(); return; }
      try {
        await call(`/jobs/${encodeURIComponent(state.job.job_id)}/drafts/${draft.no}/edit`, { method: "POST", body: { fields: body, expected_revision: state.job.revision } });
        dialog.close(); say(`${draft.no}번을 고쳤습니다. Codex가 다시 검증합니다.`, "ok"); openJob(state.job.job_id);
      } catch (error) { message.textContent = error.message; }
    });
    dialog.append(message, row(save, button("닫기", "button", () => dialog.close())));
    dialog.addEventListener("close", () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
  }

  const confirmBar = node("div", "gen-confirm");
  function renderConfirmBar() {
    const job = state.job;
    confirmBar.replaceChildren();
    if (!job || ["queued", "running"].includes(job.status)) return;
    const chosen = [...state.selected].filter((no) => job.drafts.some((d) => d.no === no && !d.confirmed_item_id && !d.discarded && d.duplicate_check?.status !== "duplicate"));
    const risky = chosen.filter((no) => { const d = job.drafts.find((x) => x.no === no); return d.verification?.status !== "verified" || !d.auto_checks?.passed; });
    const intent = Object.assign(node("input"), { type: "checkbox", checked: true });
    const intentLabel = node("label", "gen-hint");
    intentLabel.append(intent, " 출제 의도를 해설에 함께 저장");
    const go = button(`선택한 ${chosen.length}문항 문제은행에 저장`, "button primary", async () => {
      if (risky.length && !window.confirm(`${risky.join(", ")}번은 검증이 일치하지 않거나 자동 검사에 걸렸습니다. 그래도 저장할까요?`)) return;
      go.disabled = true;
      try {
        const result = await call(`/jobs/${encodeURIComponent(job.job_id)}/confirm`, { method: "POST", body: { numbers: chosen, include_intent: intent.checked, expected_revision: job.revision } });
        say(`${result.results.length}문항을 문제은행에 저장했습니다. 문제은행에서 '변형'·'신규' 배지로 볼 수 있습니다.`, "ok");
        state.selected.clear();
        await loadJobs();
        openJob(job.job_id);
      } catch (error) { say(error.message, "bad"); go.disabled = false; }
    });
    go.disabled = !chosen.length;
    const bank = Object.assign(node("a", "button", "문제은행에서 보기"), { href: "/?view=bank" });
    confirmBar.append(intentLabel, go, bank);
    if (risky.length) confirmBar.append(node("p", "gen-issue", `주의: ${risky.join(", ")}번은 검증 불일치 또는 자동 검사 문제가 있습니다.`));
  }

  const reviewBar = node("div", "gen-review-bar");
  function renderReviewBar() {
    const job = state.job;
    reviewBar.replaceChildren();
    if (!job) return;
    const selected = job.drafts.filter((d) => state.reviewSelected.has(d.no) && !d.discarded);
    const families = new Set(selected.flatMap((d) => d.canonical_current?.authors || d.contributors || [d.actual_model || d.generator_model || "unknown"])
      .map((m) => m.startsWith("claude-") ? "claude" : m.startsWith("gpt-") ? "codex" : "unknown").filter((m) => m !== "unknown"));
    const recommended = families.size === 1 && families.has("claude") ? "codex" : "claude";
    const reviewer = node("select"); reviewer.setAttribute("aria-label", "검토 모델");
    for (const [value, label] of [["claude", "Claude Opus 5.5"], ["codex", "Codex gpt-6-astra"]]) {
      reviewer.append(Object.assign(node("option", "", `${label}${value === recommended ? " (권장)" : ""}`), { value }));
    }
    reviewer.value = state.reviewModel || recommended;
    reviewer.addEventListener("change", () => { state.reviewModel = reviewer.value; });
    const busy = ["queued", "running"].includes(job.status) || (job.work_action === "review" && job.review_request?.status === "paused");
    const submit = button(`선택 ${selected.length}문항 교차 검토 / 재검토`, "button", async () => {
      submit.disabled = true;
      try {
        await call(`/jobs/${encodeURIComponent(job.job_id)}/reviews`, { method: "POST", body: {
          numbers: selected.map((d) => d.no), model: reviewer.value, expected_revision: job.revision,
        } });
        say("선택한 현재 버전의 블라인드 검토를 요청했습니다. 정답은 검토 후 비교합니다.", "ok");
        await openJob(job.job_id);
      } catch (error) { say(error.message, "bad"); submit.disabled = false; }
    });
    submit.disabled = busy || !selected.length; reviewer.disabled = busy;
    const filter = node("select"); filter.setAttribute("aria-label", "검토 상태 필터");
    for (const [value, label] of [["all", "검토 상태 전체"], ["not_run", "교차 검토 미실시"], ["self_check", "자체 검산"],
      ["cross_check", "교차 검토 일치"], ["stale", "이전 내용의 검토"], ["conflict", "답 불일치"], ["error", "오류·차단"]]) {
      filter.append(Object.assign(node("option", "", label), { value }));
    }
    filter.value = state.reviewFilter;
    filter.addEventListener("change", () => { state.reviewFilter = filter.value; renderJob(); });
    reviewBar.append(field("검토 모델", reviewer), submit, field("표시", filter), node("p", "gen-hint",
      `권장: ${recommended === "claude" ? "Claude" : "Codex"}. 같은 모델 계열의 검토는 자체 검산으로 표시합니다. 작성 모델 미상·혼합 작성은 독립 검토로 표시하지 않습니다. 추가 교차 검토는 저장의 필수 조건이 아닙니다.`));
  }

  function renderJob() {
    const job = state.job;
    if (!job) { jobBody.replaceChildren(node("p", "gen-hint", "왼쪽에서 주제를 고르고 생성하거나, 위에서 이전 작업을 여세요.")); return; }
    const info = node("div", "gen-job-info");
    info.append(node("strong", "", `${job.unit.major} › ${job.unit.middle}`),
      badge(STATUS_LABEL[job.status] || job.status, job.status === "failed" ? "bad" : job.status === "ready" ? "ok" : "wait"),
      node("span", "gen-hint", ` ${job.progress?.message || ""}`));
    if (job.error) info.append(node("p", "gen-issue", job.error));
    if (job.fallback_transitions?.length) info.append(node("p", "gen-hint", "Claude 사용량 한도에 도달해 이 작업의 남은 작성·수정·그림 대조를 Codex로 전환했습니다."));
    if (["failed", "partial", "paused"].includes(job.status)) info.append(button(job.work_action === "review" ? "남은 검토 사용자 재개" : "남은 단계 사용자 재개", "button small", async () => {
      try { await call(`/jobs/${encodeURIComponent(job.job_id)}/retry`, { method: "POST", body: { expected_revision: job.revision } }); openJob(job.job_id); } catch (error) { say(error.message, "bad"); }
    }));
    const list = node("div", "gen-drafts");
    const displayed = job.drafts.filter((d) => {
      const cross = d.cross_review || { status: "not_run" };
      const key = cross.status === "not_run" && cross.initial_kind === "self_check" ? "self_check" : cross.status;
      return state.reviewFilter === "all" || key === state.reviewFilter
        || (state.reviewFilter === "not_run" && key === "self_check")
        || (state.reviewFilter === "error" && ["error", "blocked"].includes(key));
    });
    displayed.forEach((draft) => list.append(renderDraft(draft)));
    if (job.drafts.length && !displayed.length) list.append(node("p", "gen-hint", "이 검토 상태에 해당하는 문항이 없습니다."));
    if (!job.drafts.length) list.append(node("p", "gen-hint", ["queued", "running"].includes(job.status) ? "문항을 만드는 중입니다. 화면은 자동으로 새로 고쳐집니다." : "초안이 없습니다."));
    jobBody.replaceChildren(info, reviewBar, list, confirmBar);
    renderReviewBar();
    renderConfirmBar();
    renderMath(jobBody);
  }

  // ---------------------------------------------------------------- bot chat
  const chatPanel = node("section", "gen-panel gen-chat");
  chatPanel.hidden = true;
  root.append(chatPanel);

  async function openChat(chatId) {
    window.clearTimeout(state.chatTimer);
    chatPanel.hidden = false;
    if (!chatId) { state.chat = null; renderChat(); chatPanel.scrollIntoView({ behavior: "smooth" }); return; }
    try { state.chat = await call(`/chats/${encodeURIComponent(chatId)}`); }
    catch (error) { say(error.message, "bad"); return; }
    renderChat();
    if (state.chat.status === "thinking") state.chatTimer = window.setTimeout(() => openChat(chatId), 3000);
  }

  function renderChat() {
    const chat = state.chat;
    const head = node("header", "gen-chat-head");
    head.append(node("h3", "", "봇과 상의하기 (item-analyst)"),
      node("p", "gen-hint", "봇은 파일 읽기와 이 대화 전용 폴더 쓰기만 할 수 있습니다. 한 번 답할 때 약 5~6만 토큰을 씁니다. 대화는 기록됩니다."),
      button("닫기", "button small", () => { chatPanel.hidden = true; window.clearTimeout(state.chatTimer); }));
    const log = node("div", "gen-chat-log");
    if (!chat) log.append(node("p", "gen-hint", `대상: ${state.major} › ${state.middle}. 원하는 문항을 설명하면 봇이 학습한 분석을 바탕으로 함께 설계합니다. 다 정해지면 '초안 저장해줘'라고 말한 뒤 '초안 가져오기'를 누르세요.`));
    (chat?.turns || []).forEach((turn) => {
      const bubble = mathText("div", `gen-bubble ${turn.role}`, turn.text);
      log.append(bubble);
    });
    if (chat?.status === "thinking") log.append(node("p", "gen-hint", "봇이 답하는 중…"));
    const input = node("textarea");
    input.rows = 3;
    input.placeholder = "봇에게 보낼 메시지";
    // The panel redraws every 3 s while the bot answers; keep what the user is typing.
    input.value = chatPanel.querySelector("textarea")?.value || "";
    const send = button("보내기", "button primary", async () => {
      const message = input.value.trim();
      if (!message) return;
      send.disabled = true;
      try {
        state.chat = chat
          ? await call(`/chats/${encodeURIComponent(chat.chat_id)}/messages`, { method: "POST", body: { message } })
          : await call("/chats", { method: "POST", body: { scope: state.scope, major: state.major, middle: state.middle, message } });
        input.value = "";
        openChat(state.chat.chat_id);
      } catch (error) { say(error.message, "bad"); send.disabled = false; }
    });
    const importButton = button("초안 가져오기 → 검증", "button", async () => {
      try {
        const job = await call(`/chats/${encodeURIComponent(chat.chat_id)}/import`, { method: "POST", body: {} });
        say(`봇 초안 ${job.drafts.length}문항을 가져왔습니다. 그림·검사·Codex 검증을 진행합니다.`, "ok");
        await loadJobs();
        openJob(job.job_id);
      } catch (error) { say(error.message, "bad"); }
    });
    importButton.disabled = !chat || chat.status === "thinking";
    send.disabled = chat?.status === "thinking";
    chatPanel.replaceChildren(head, log, input, row(send, importButton));
    renderMath(log);
    log.scrollTop = log.scrollHeight;
  }

  // ---------------------------------------------------------------- start
  (async () => {
    try {
      const data = await call("/scopes");
      state.scopes = data.scopes || [];
      fillSelect(scopeSelect, state.scopes.map((s) => s.scope), "고1-통합과학2");
      state.scope = scopeSelect.value;
      if (!state.scope) { say("학년 범위 파일이 없습니다(C:/Question_Item_Lab/scope).", "bad"); return; }
      await loadTree();
      await loadJobs();
      renderJob();
      const recent = state.jobs.find((j) => ["queued", "running"].includes(j.status)) || state.jobs[0];
      if (recent) openJob(recent.job_id);
    } catch (error) { say(error.message, "bad"); }
  })();
}
