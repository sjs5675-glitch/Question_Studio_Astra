"""AI-written explanations for items that have a confirmed answer but no explanation.

The confirmed answer is authority: models only write the reasoning that leads to it and may
refuse (answer_suspect / insufficient). Nothing is saved when every writer refuses.

Plans (user decision 2026-10-06):
- middle school: Claude sonnet alone (friendly style).
- high school 통합과학1/2: Codex (gpt-6-astra, high) and Claude opus compete; Claude opus
  judges blind (A/B without authorship) because Codex picked its own text 11/11 in trials.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .tagging import _project_context, _section_text, _walk
from .model import utc_now

CODEX_BIN = os.environ.get(
    "ASTRA_EXPLAIN_CODEX_BIN", "C:/Users/ExampleUser/AppData/Local/Programs/nodejs/codex.cmd"
)
CLAUDE_BIN = os.environ.get("ASTRA_EXPLAIN_CLAUDE_BIN", "C:/Users/ExampleUser/.local/bin/claude.exe")
TIMEOUT_SECONDS = 600


class ExplanationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Writer:
    provider: str  # "codex" | "claude"
    model: str
    effort: str | None = None


@dataclass(frozen=True)
class Plan:
    name: str
    writers: tuple[Writer, ...]
    judge: Writer | None = None


MIDDLE_PLAN = Plan("middle-claude", (Writer("claude", "sonnet"),))
HIGH_INTEGRATED_PLAN = Plan(
    "high-integrated-competition",
    (Writer("codex", "gpt-6-astra", "high"), Writer("claude", "opus")),
    judge=Writer("claude", "opus"),
)


def plan_for(project: Mapping[str, Any], document: Mapping[str, Any]) -> Plan:
    subject, school_level, _grade = _project_context(project, document)
    if school_level == "middle":
        return MIDDLE_PLAN
    if subject in {"통합과학1", "통합과학2"}:
        return HIGH_INTEGRATED_PLAN
    raise ExplanationError(f"이 과목({subject})은 아직 AI 해설 설정이 없습니다.")


def _content(document: Mapping[str, Any]) -> Mapping[str, Any]:
    content = document.get("content")
    return content if isinstance(content, Mapping) else {}


def _choice_labels(question: Any) -> dict[str, str]:
    labels: dict[str, str] = {}
    for node in _walk(question):
        if isinstance(node.get("choice_id"), str) and isinstance(node.get("label"), str):
            labels[node["choice_id"]] = node["label"]
    return labels


def answer_text(document: Mapping[str, Any]) -> str:
    content = _content(document)
    answer = content.get("answer")
    if not isinstance(answer, Mapping):
        return ""
    kind = answer.get("kind")
    if kind == "choice":
        labels = _choice_labels(content.get("question"))
        return ", ".join(labels.get(str(cid), str(cid)) for cid in answer.get("choice_ids") or [])
    if kind == "content":
        return _section_text({"blocks": answer.get("blocks") or []}, limit=4000)
    if kind == "true_false":
        return ", ".join(
            f"{row.get('statement_id')}: {'O' if row.get('value') else 'X'}"
            for row in answer.get("items") or []
            if isinstance(row, Mapping)
        )
    if kind == "composite":
        parts = []
        for row in answer.get("items") or []:
            if isinstance(row, Mapping):
                sub = answer_text({"content": {"question": content.get("question"), "answer": row.get("answer")}})
                parts.append(f"{row.get('label')}: {sub}")
        return "\n".join(parts)
    return ""


WRITE_PROMPT = """너는 한국 {level} 과학 문항의 해설을 쓰는 교사다. 아래 문항의 **정답은 확정되어 있다**. 정답을 바꾸지 말고, 그 정답이 나오는 풀이 과정을 학생이 이해할 수 있게 한국어로 써라.
- 첨부된 문항 이미지(그림 포함)가 있으면 반드시 확인하라.
- {style}
- 선택형이면 정답 선지가 맞는 이유와 주요 오답이 틀린 이유를 쓴다.
- 문제·그림에 없는 수치나 단위를 지어내지 마라.
- 문제 정보만으로 이 정답이 나올 수 없거나(정답 오류 의심) 그림 정보가 부족하면 해설을 억지로 만들지 말고 verdict를 그에 맞게 설정하라.
출력은 JSON 하나만: {{"verdict":"ok|answer_suspect|insufficient","explanation":"해설(문단은 줄바꿈으로 구분)","note":"verdict가 ok가 아닐 때 이유"}}

[문항 이미지 파일] {image}
[문항 글]
{question}

[확정 정답]
{answer}
"""

JUDGE_PROMPT = """두 해설 중 학생에게 더 나은 것을 골라라. 기준 순서: 확정 정답과 일치 → 과학적 정확성 → 그림·문제에 없는 내용을 지어내지 않음 → 그림 정보를 올바르게 사용 → 이해하기 쉬움. 둘 다 부적합하면 NONE.
출력은 JSON 하나만: {{"choice":"A|B|NONE","reason":"짧게"}}

[문항 글]
{question}
[확정 정답]
{answer}
[해설 A]
{a}
[해설 B]
{b}
"""


SUPPLEMENT_PROMPT = """너는 한국 {level} 과학 문항의 해설을 보완하는 교사다. 아래 문항의 **정답은 확정**되어 있고, 원본 해설이 이미 있다. 원본 해설은 고치거나 다시 쓰지 말고, **빠진 부분만** 덧붙일 문단을 한국어로 써라.
- 첨부된 문항 이미지(그림 포함)가 있으면 반드시 확인하라.
- 보충할 만한 것: 정답 근거의 빠진 단계, 그림·그래프 해석, 선택형의 오답 이유, 학생이 헷갈리는 개념. {style}
- 원본 해설에 이미 있는 내용을 반복하지 마라. 문제·그림에 없는 수치나 단위를 지어내지 마라.
- 원본 해설만으로 충분하면 verdict를 sufficient로 하고 supplement는 비워라.
- 원본 해설이 확정 정답과 맞지 않거나 정답 오류가 의심되면 answer_suspect, 그림 정보가 부족하면 insufficient.
출력은 JSON 하나만: {{"verdict":"add|sufficient|answer_suspect|insufficient","supplement":"덧붙일 문단(줄바꿈으로 구분)","note":"이유 한 줄"}}

[문항 이미지 파일] {image}
[문항 글]
{question}

[확정 정답]
{answer}

[원본 해설]
{existing}
"""

SUPPLEMENT_JUDGE_PROMPT = """원본 해설에 덧붙일 보충 문단 두 개 중 학생에게 더 도움이 되는 것을 골라라. 기준 순서: 확정 정답과 일치 → 과학적 정확성 → 원본 해설과 중복되지 않음 → 문제·그림에 없는 내용을 지어내지 않음 → 이해하기 쉬움. 둘 다 부적합하면 NONE.
출력은 JSON 하나만: {{"choice":"A|B|NONE","reason":"짧게"}}

[문항 글]
{question}
[확정 정답]
{answer}
[원본 해설]
{existing}
[보충 A]
{a}
[보충 B]
{b}
"""

SUPPLEMENT_LABEL = "[AI 보충] "


def parse_json_object(raw: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        raise ExplanationError("AI 응답에서 JSON을 찾지 못했습니다.")
    try:
        value = json.loads(match.group(0))
    except ValueError as exc:
        raise ExplanationError("AI 응답 JSON을 해석하지 못했습니다.") from exc
    if not isinstance(value, dict):
        raise ExplanationError("AI 응답은 객체여야 합니다.")
    return value


Runner = Callable[[Writer, str, Path | None, Path], tuple[str, str | None]]


def cli_runner(writer: Writer, prompt: str, image: Path | None, workdir: Path) -> tuple[str, str | None]:
    """Run a subscription CLI. Returns (raw text, actual model id if reported)."""
    if writer.provider == "codex":
        cmd = [CODEX_BIN, "exec", "--skip-git-repo-check", "-s", "read-only", "-C", str(workdir), "-m", writer.model]
        if writer.effort:
            cmd += ["-c", f"model_reasoning_effort={writer.effort}"]
        if image:
            cmd += ["-i", str(image)]
        cmd += ["-"]
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT_SECONDS)
        if proc.returncode != 0 and "{" not in (proc.stdout or ""):
            raise ExplanationError(f"Codex 실행 실패(exit {proc.returncode})")
        return proc.stdout, writer.model
    if writer.provider == "claude":
        cmd = [CLAUDE_BIN, "-p", "--output-format", "json", "--model", writer.model, "--allowedTools", "Read", "--add-dir", str(workdir)]
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT_SECONDS, cwd=str(workdir))
        try:
            envelope = json.loads(proc.stdout)
        except ValueError as exc:
            tail = (proc.stdout + proc.stderr)[-200:]
            raise ExplanationError(f"Claude 실행 실패: {tail}") from exc
        if envelope.get("is_error"):
            raise ExplanationError(f"Claude 오류: {str(envelope.get('result'))[:200]}")
        models = list((envelope.get("modelUsage") or {}).keys())
        return str(envelope.get("result") or ""), (models[0] if models else writer.model)
    raise ExplanationError(f"알 수 없는 AI 제공자: {writer.provider}")


@dataclass
class Draft:
    writer: Writer
    model: str | None
    verdict: str
    explanation: str
    note: str = ""
    error: str | None = None


@dataclass
class Result:
    plan: str
    chosen: Draft | None
    drafts: list[Draft] = field(default_factory=list)
    judge: dict[str, Any] | None = None
    refusals: list[str] = field(default_factory=list)


def _write(
    writer: Writer, prompt: str, image: Path | None, workdir: Path, runner: Runner, *, supplement: bool = False
) -> Draft:
    try:
        raw, model = runner(writer, prompt, image, workdir)
        value = parse_json_object(raw)
    except (ExplanationError, subprocess.TimeoutExpired, OSError) as exc:
        return Draft(writer, None, "error", "", error=str(exc))
    verdict = value.get("verdict")
    text = value.get("supplement" if supplement else "explanation")
    allowed = {"add", "sufficient", "answer_suspect", "insufficient"} if supplement else {"ok", "answer_suspect", "insufficient"}
    if verdict not in allowed:
        return Draft(writer, model, "error", "", error="verdict 값이 올바르지 않습니다.")
    if supplement and verdict == "add":
        verdict = "ok"
    if verdict == "ok" and (not isinstance(text, str) or not text.strip()):
        return Draft(writer, model, "error", "", error="해설이 비어 있습니다.")
    return Draft(writer, model, str(verdict), str(text or "").strip(), str(value.get("note") or ""))


def write_explanation(
    project: Mapping[str, Any],
    document: Mapping[str, Any],
    *,
    question_image: Path | None,
    runner: Runner = cli_runner,
    supplement: bool = False,
) -> Result:
    plan = plan_for(project, document)
    content = _content(document)
    question = _section_text(content.get("question"), limit=14000)
    answer = answer_text(document)
    if not question.strip() or not answer.strip():
        raise ExplanationError("문제 글과 확정 정답이 있어야 AI 해설을 쓸 수 있습니다.")
    existing = _section_text(content.get("explanation"), limit=8000) if supplement else ""
    if supplement and not existing.strip():
        raise ExplanationError("보충할 기존 해설이 없습니다.")
    _subject, school_level, _grade = _project_context(project, document)
    level = "중학교" if school_level == "middle" else "고등학교"
    style = (
        "친절하게 쓰되, 학생이 자주 헷갈리는 부분이 있으면 짧게 짚어 준다."
        if plan is MIDDLE_PLAN
        else "근거를 그림·자료에서 읽어 단계적으로 제시하고, 흔한 오답이 있으면 짚어 준다."
    )
    with tempfile.TemporaryDirectory(prefix="astra-explain-") as tmp:
        workdir = Path(tmp)
        image = None
        if question_image and question_image.is_file():
            image = workdir / "question.png"
            shutil.copy2(question_image, image)
        template = SUPPLEMENT_PROMPT if supplement else WRITE_PROMPT
        prompt = template.format(level=level, style=style, image=str(image) if image else "(없음)", question=question, answer=answer, existing=existing)
        drafts = [_write(writer, prompt, image, workdir, runner, supplement=supplement) for writer in plan.writers]
        result = Result(plan.name, None, drafts)
        result.refusals = [f"{d.writer.provider}: {d.verdict} {d.note}".strip() for d in drafts if d.verdict in {"answer_suspect", "insufficient"}]
        if result.refusals:
            return result  # any refusal blocks saving (possible answer typo / missing figure info)
        usable = [d for d in drafts if d.verdict == "ok"]
        if not usable:
            return result
        if len(usable) == 1 or plan.judge is None:
            result.chosen = usable[0]
            return result
        # Blind judge with deterministic order so authorship cannot be inferred from position.
        flip = int(hashlib.sha256(str(document.get("item_id")).encode()).hexdigest(), 16) % 2 == 1
        first, second = (usable[1], usable[0]) if flip else (usable[0], usable[1])
        try:
            judge_template = SUPPLEMENT_JUDGE_PROMPT if supplement else JUDGE_PROMPT
            raw, model = runner(plan.judge, judge_template.format(question=question, answer=answer, existing=existing, a=first.explanation, b=second.explanation), None, workdir)
            verdict = parse_json_object(raw)
        except (ExplanationError, subprocess.TimeoutExpired, OSError) as exc:
            result.judge = {"error": str(exc)}
            return result
        choice = verdict.get("choice")
        result.judge = {"model": model, "choice": choice, "reason": str(verdict.get("reason") or "")[:500]}
        if choice == "A":
            result.chosen = first
        elif choice == "B":
            result.chosen = second
        return result


def explanation_blocks(item_id: str, text: str, *, prefix: str = "ai-expl", label: str = "") -> list[dict[str, Any]]:
    paragraphs = [line.strip() for line in re.split(r"\n+", text) if line.strip()]
    if label and paragraphs:
        paragraphs[0] = label + paragraphs[0]
    digest = hashlib.sha256(f"{item_id}\n{text}".encode("utf-8")).hexdigest()[:20]
    return [
        {
            "block_id": f"{prefix}-{digest}-{index}",
            "type": "paragraph",
            "role": "explanation",
            "inlines": [{"type": "text", "text": paragraph, "source_text": None}],
            "review_status": "unreviewed",
        }
        for index, paragraph in enumerate(paragraphs, 1)
    ]


def explanation_section(item_id: str, text: str) -> dict[str, Any]:
    return {"blocks": explanation_blocks(item_id, text)}


def supplement_blocks(item_id: str, text: str) -> list[dict[str, Any]]:
    return explanation_blocks(item_id, text, prefix="ai-supp", label=SUPPLEMENT_LABEL)


def provenance_record(
    result: Result, answer: str, *, mode: str = "write", block_ids: list[str] | None = None
) -> dict[str, Any]:
    chosen = result.chosen
    assert chosen is not None
    return {
        "mode": mode,
        "block_ids": list(block_ids or []),
        "plan": result.plan,
        "provider": chosen.writer.provider,
        "model": chosen.model or chosen.writer.model,
        "candidates": [
            {"provider": d.writer.provider, "model": d.model or d.writer.model, "verdict": d.verdict}
            for d in result.drafts
        ],
        "judge": ({k: v for k, v in result.judge.items() if k in {"model", "choice", "reason"}} if result.judge else None),
        "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "created_at": utc_now(),
    }
