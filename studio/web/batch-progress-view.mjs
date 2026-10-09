"use strict";

function formatTime(value) {
  const date = typeof value === "string" ? new Date(value) : null;
  return date && Number.isFinite(date.getTime())
    ? new Intl.DateTimeFormat("ko-KR", {dateStyle:"short", timeStyle:"medium"}).format(date)
    : "기록 없음";
}

function count(value) {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function metricsOf(detail) {
  const rows = Array.isArray(detail?.metrics) ? detail.metrics : [];
  return rows.filter(m=>m && count(m.done) !== null && typeof m.label === "string"
    && !String(m.key || "").startsWith("solution_linking_")
    && !/^live_verify2?_review$/.test(String(m.key || ""))).slice(0,48).map(m=>{
    const total = count(m.total), done = count(m.done), unit = typeof m.unit === "string" ? m.unit : "건";
    return {key:m.key, label:m.key === "crop_review" ? "추출 기록" : m.label,
      value:total === null ? `처리 ${done}${unit} · 전체 수 미확정` : `${done} / ${total} ${unit}`,
      failures:count(m.failed) > 0 ? `실패 ${m.failed}` : ""};
  });
}

const FLOW_STATUS = {done: "완료", current: "진행 중", pending: "대기", failed: "중단"};

function flowOf(detail) {
  const rows = Array.isArray(detail?.flow) ? detail.flow : [];
  return rows.filter(step => step && typeof step.label === "string" && Object.hasOwn(FLOW_STATUS, step.status))
    .slice(0, 12).map(step => ({key: String(step.key || ""), label: step.label, status: step.status,
      statusLabel: FLOW_STATUS[step.status], detail: typeof step.detail === "string"
        ? step.key === "solution" && /검토|검증|충돌|완료 \d+ · 일부/.test(step.detail)
          ? "연결 결과 집계 중" : step.detail.replace(/ · 검토 \d+/g, "") : ""}));
}

export function progressPresentation(job, detail, now = Date.now()) {
  const start = Date.parse(job?.created_at);
  const end = Date.parse(job?.finished_at);
  const seconds = Number.isFinite(start) ? Math.max(0, Math.floor(((Number.isFinite(end) ? end : now) - start) / 1000)) : null;
  const elapsed = seconds === null ? "시작 시각 기록 없음" : seconds < 60 ? `${seconds}초` : `${Math.floor(seconds / 60)}분 ${seconds % 60}초`;
  return {
    stage: detail?.stage_label === "해설 검증" ? "해설 대조" : detail?.stage_label || "상세 단계 확인 중",
    flow: flowOf(detail),
    metrics: metricsOf(detail),
    notice:detail?.availability === "partial" ? "일부 단계의 상세 기록은 확인할 수 없습니다. 확인된 수치만 표시합니다."
      : detail?.availability === "unavailable" ? "상세 기록을 확인하지 못했습니다. 기존 작업 요약은 유지됩니다." : "",
    events: (Array.isArray(detail?.recent_events) ? detail.recent_events : []).filter(e=>typeof e?.text === "string")
      .filter(e=>!/완료 \d+ · 일부|해설 연결 (검증|검토)/.test(e.text)).slice(-8).map(e=>e.text.replace(/ · 검토 \d+/g, "")),
    elapsed, recordTime:formatTime(detail?.updated_at), observedTime:formatTime(detail?.observed_at),
    overallNote:"전체 진행률은 단계별 가중치입니다. 실제 처리량은 아래 단계별 수치를 확인하세요.",
    approvalNote:"전사 완료 후 바로 사용하고, 필요한 문항만 수정할 수 있습니다."
  };
}
