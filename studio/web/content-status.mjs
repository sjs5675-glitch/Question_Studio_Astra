export const CONTENT_LABELS = {
  question_only: "문제만",
  question_answer: "문제+정답",
  question_answer_explanation: "문제+정답+해설",
  question_explanation_without_answer: "문제+해설(정답 없음)",
  question_missing: "문제 없음"
};

function storedValuePresent(value) {
  if (typeof value === "string") return value.trim().length > 0;
  if (Array.isArray(value)) return value.some(storedValuePresent);
  if (!value || typeof value !== "object") return false;
  return ["text", "latex", "hwp_script", "plain_text", "asset_id"]
    .some((key) => typeof value[key] === "string" && value[key].trim().length > 0)
    || ["inlines", "blocks", "items", "rows", "cells"].some((key) => storedValuePresent(value[key]));
}

function answerPresent(answer) {
  if (!answer || typeof answer !== "object") return false;
  switch (answer.kind) {
    case "choice": return Array.isArray(answer.choice_ids) && answer.choice_ids.some((id) => typeof id === "string" && id.trim().length > 0);
    case "content": return storedValuePresent(answer.blocks);
    case "true_false": return Array.isArray(answer.items) && answer.items.some((item) => typeof item?.value === "boolean");
    case "composite": return Array.isArray(answer.items) && answer.items.some((item) => answerPresent(item?.answer));
    default: return false;
  }
}

export function savedContentStatus(value) {
  const canonical = value?.content_status ?? value?.package?.content_status;
  if (canonical && Object.hasOwn(CONTENT_LABELS, canonical.status)) return canonical;
  const content = value?.content ?? value?.package?.content;
  if (!content) return { status: "unavailable", label: "저장 내용 불러오기 실패", input_complete: false };
  const question = storedValuePresent(content.question);
  const answer = answerPresent(content.answer);
  const explanation = storedValuePresent(content.explanation);
  const status = !question ? "question_missing" : answer
    ? explanation ? "question_answer_explanation" : "question_answer"
    : explanation ? "question_explanation_without_answer" : "question_only";
  return { status, label: CONTENT_LABELS[status], has_question: question, has_answer: answer,
    has_explanation: explanation, input_complete: question && answer };
}

export function savedContentCounts(items) {
  const counts = Object.fromEntries(Object.keys(CONTENT_LABELS).map((key) => [key, 0]));
  for (const item of items) {
    const status = savedContentStatus(item).status;
    if (Object.hasOwn(counts, status)) counts[status] += 1;
  }
  return counts;
}

export function savedContentCountsText(counts) {
  const keys = Object.keys(CONTENT_LABELS).filter((key) => key !== "question_missing" || Number(counts?.[key]) > 0);
  return keys.map((key) => `${CONTENT_LABELS[key]} ${Math.max(0, Number(counts?.[key]) || 0)}`).join(" · ");
}

export async function withSavedContentStatus(items, readItem) {
  const result = items.slice();
  for (let start = 0; start < result.length; start += 8) {
    await Promise.all(result.slice(start, start + 8).map(async (item, offset) => {
      if (item?.content_status || item?.content) return;
      try {
        const detail = await readItem(item);
        result[start + offset] = { ...item, content_status: savedContentStatus(detail) };
      } catch {
        result[start + offset] = { ...item, content_status: { status: "unavailable", label: "저장 내용 불러오기 실패", input_complete: false } };
      }
    }));
  }
  return result;
}
