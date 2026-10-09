"""Sequential worker for AI item generation: generate → figure → figure check → Codex verify.

Every external model call goes through :class:`Runners` so tests can inject fakes.
The server never executes model-written code itself: code-drawn figures are
produced by Codex inside its own sandbox, and the app only accepts the PNG.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .item_generation import (
    CHOICE_LABELS,
    ItemGenerationError,
    analyses_for_unit,
    auto_checks,
    blocks_text_for_prompt,
    codex_effort_for_scope,
    draft_format_errors,
    grams,
    guides_text,
    job_dir,
    jobs_root,
    lab_inputs,
    lab_root,
    list_jobs,
    load_job,
    load_scope,
    resolve_unit,
    update_job,
    _blocks_text,
    _pattern_path,
)
from .model import utc_now

CLAUDE_BIN = os.environ.get("ASTRA_ITEMGEN_CLAUDE_BIN", "C:/Users/ExampleUser/.local/bin/claude.exe")
CODEX_BIN = os.environ.get("ASTRA_ITEMGEN_CODEX_BIN", "C:/Users/ExampleUser/AppData/Local/Programs/nodejs/codex.cmd")
CLAUDE_MODEL = "claude-opus-5-5"
GENERATE_EFFORT = "high"  # user decision: generation = Opus 5.5 high
CHECK_EFFORT = "medium"  # figure/body comparison is a reading check, not authoring
CODEX_MODEL = "gpt-6-astra"
LAB_PYTHON = os.environ.get("ASTRA_ITEMGEN_LAB_PYTHON", "C:/Question_Item_Lab/.venv/Scripts/python.exe")
BATCH = 5


# --------------------------------------------------------------------------- runners

def _json_object(text: str) -> Any:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ItemGenerationError("모델 응답에서 JSON을 찾지 못했습니다.")
    return json.loads(text[start:end + 1])


def _scratch_dir(prefix: str) -> Path:
    # Python 3.13 mkdtemp applies an owner-only ACL on Windows that the Codex
    # sandbox account cannot read; a plain mkdir inherits normal permissions.
    path = Path(tempfile.gettempdir()) / f"{prefix}-{os.getpid()}-{threading.get_ident()}-{os.urandom(5).hex()}"
    os.mkdir(path)
    return path


def real_claude(prompt: str, *, effort: str, add_dirs: list[Path]) -> tuple[str, dict[str, Any]]:
    cmd = [CLAUDE_BIN, "-p", "--model", CLAUDE_MODEL, "--effort", effort, "--output-format", "json",
           "--tools", "Read", "--allowedTools", "Read", "--no-session-persistence"]
    for directory in add_dirs:
        cmd += ["--add-dir", str(directory)]
    started = time.time()
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          cwd=str(lab_root()), timeout=1800)
    if proc.returncode != 0:
        raise ItemGenerationError(f"Claude 실행 실패(rc={proc.returncode}): {(proc.stderr or proc.stdout)[-400:]}")
    envelope = json.loads(proc.stdout)
    if envelope.get("is_error"):
        raise ItemGenerationError(f"Claude 오류: {str(envelope.get('result'))[:400]}")
    usage = {"model": CLAUDE_MODEL, "effort": effort, "usage": envelope.get("usage"),
             "duration_s": round(time.time() - started, 1)}
    return str(envelope.get("result") or ""), usage


def real_codex(prompt: str, *, effort: str, images: list[Path], write: bool = False,
               workdir: Path | None = None, timeout: int = 1800) -> tuple[str, dict[str, Any]]:
    own = workdir is None
    workdir = workdir or _scratch_dir("astra-itemgen-codex")
    try:
        last = workdir / "_last.txt"
        cmd = [CODEX_BIN, "exec", "-m", CODEX_MODEL, "-c", f'model_reasoning_effort="{effort}"',
               "-s", "workspace-write" if write else "read-only", "--skip-git-repo-check", "--ephemeral",
               "-C", str(workdir), "-o", str(last)]
        for image in images:
            cmd += ["-i", str(image)]
        cmd.append("-")
        started = time.time()
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              cwd=str(workdir), timeout=timeout)
        if proc.returncode != 0 and not last.exists():
            raise ItemGenerationError(f"Codex 실행 실패(rc={proc.returncode}): {(proc.stderr or proc.stdout)[-400:]}")
        text = last.read_text(encoding="utf-8") if last.exists() else ""
        return text, {"model": CODEX_MODEL, "effort": effort, "duration_s": round(time.time() - started, 1)}
    finally:
        if own:
            shutil.rmtree(workdir, ignore_errors=True)


@dataclass
class Runners:
    claude: Callable[..., tuple[str, dict[str, Any]]] = real_claude
    codex: Callable[..., tuple[str, dict[str, Any]]] = real_codex


# --------------------------------------------------------------------------- prompts

COMMON = """너는 {grade} {subject}({curriculum}) 문항 출제자다. 결과는 학생용 문제지와 교사용 정답·해설로 쓰인다.

[학년 범위 — 반드시 지킨다]
- 허용 개념(이 단원): {unit_concepts}
- 범위 밖으로 정한 내용: {excluded}
- 각 문항의 scope_concepts에는 위 허용 개념 목록의 문자열을 **그대로 복사**해 사용한 것만 적는다. 목록에 없는 개념이 필요하면 그 문항을 만들지 않는다.

[출제 지침서]
{guides}

[공통 규칙]
- 정답이 하나로만 정해지게 만든다. 보기(ㄱㄴㄷ)형은 각 보기의 참·거짓이 근거로 확정돼야 한다.
- 자료는 글·표·그림으로 제시할 수 있다. {figure_rule}
- 수치는 계산이 학년 범위를 넘지 않게 한다. 수치 계산이 정답을 정하면 calc_checks에 계산식(사칙연산만)과 기대값을 적는다.
- 원문항 문장·수치를 그대로 옮기지 않는다.
- 해설(solution)은 학생이 읽는 풀이다: 자료 해석 → 핵심 개념 적용 → 각 보기/선지 판단 순서로 쓴다.
- design_intent는 교사용 '출제 의도'다: purpose(평가 목적·성취기준 연결), assessed_ability, structure_rationale,
  misconceptions_targeted(겨냥한 오개념과 그것을 드러내는 장치), difficulty_design, basis(참고한 분석 근거).
- 한국어, 문제지 문체(“~이다.”, “~은?”).
{free_request}
[출력] JSON 객체 하나만 출력(코드펜스·설명 금지): {{"items": [ ... ]}}
각 item:
{{
 "kind": "variant" | "new",
 "source_item_id": "변형이면 원문항 item_id, 신규면 null",
 "source_printed_number": "변형이면 원문항 번호, 신규면 null",
 "reference_item_ids": ["신규일 때 참고한 분석 문항 item_id들"],
 "variant_type": ["수치 변경"|"조건 변경"|"자료 변경"|"발문 반전"|"보기 재구성"|"맥락 변경" ...],
 "difficulty": "상|중|하",
 "format": "보기(ㄱㄴㄷ)형|단일 선택형|단답형|서술형",
 "scope_concepts": ["허용 개념 목록에서 그대로 복사"],
 "concepts": ["세부 개념 짧게"],
 "stem": "자료 설명 문장",
 "table": {{"columns": [...], "rows": [[...], ...]}} 또는 null,
 "figure": null 또는 {{"method": "code"|"codex_image", "alt_text": "그림 설명 한 문장",
            "spec": {{"type": "graph|diagram|map|circuit|illustration", "description": "무엇을 그리는지",
                      "axes": {{"x": {{"label": "", "unit": "", "ticks": []}}, "y": {{...}}}} 또는 null,
                      "data": "그래프·모식도에 들어갈 수치·점·곡선을 정확히", "labels": ["그림에 들어갈 글자"],
                      "layout": "배치·크기", "must_match": ["본문·정답 근거와 반드시 일치해야 하는 사항"]}}}},
 "instruction": "발문",
 "bogi": {{"ㄱ": "...", "ㄴ": "...", "ㄷ": "..."}} 또는 null,
 "choices": {{"①": "...", "②": "...", "③": "...", "④": "...", "⑤": "..."}} 또는 null,
 "answer": "① 같은 선지 기호, 단답형은 답, 서술형은 예시 답안",
 "solution": "학생용 풀이",
 "design_intent": {{"purpose": "...", "assessed_ability": "...", "structure_rationale": "...",
                   "misconceptions_targeted": ["..."], "difficulty_design": "...", "basis": "..."}},
 "distractor_intents": {{"오답 보기·선지": "노리는 오개념/실수"}},
 "unique_answer_rationale": "정답이 하나로 정해지는 근거",
 "calc_checks": [{{"expression": "6+6", "expected": 12, "meaning": "생성물 계수 합"}}] 또는 []
}}
"""

FIGURE_RULES = {
    "auto": "그림이 문항을 더 좋게 만들면 figure 명세를 쓴다. 그래프·모식도·회로·지도는 method=\"code\"(코드로 정확히 작도), 사실적 일러스트만 method=\"codex_image\". 그림 속 수치·라벨은 본문과 정확히 일치해야 한다.",
    "use": "각 문항에 그림을 하나씩 넣는다(figure 필수). 그래프·모식도·회로·지도는 method=\"code\", 사실적 일러스트만 method=\"codex_image\". 그림 속 수치·라벨은 본문과 정확히 일치해야 한다.",
    "none": "그림은 쓰지 않는다(figure는 null). 필요하면 표나 글 자료로 바꾼다.",
}

VARIANT_TASK = """
[작업] 아래 원문항 각각에 대해 변형 문항을 정확히 1개씩 만든다(kind="variant", 총 {n}개).
- 원문항의 '묻는 것'과 핵심 아이디어는 유지하고, 자료·조건·수치·발문·보기 구성 중 둘 이상을 바꾼다.
- 원문항 분석의 오답 유도·혼동 장치를 참고해 오답 선지를 설계한다.
- 원문항에 [그림 파일]이 있으면 Read 도구로 열어 확인할 수 있다.
- 목표 난이도: {difficulty}{fmt}

{sources}
"""

NEW_TASK = """
[작업] 특정 원문항을 바꾸지 말고 새 문항 {n}개를 만든다(kind="new").
- 아래 단원 패턴 문서와 문항 분석 요약에서 이 문제집이 쓰는 발문 형식·보기 문장 형식·오답 유도·혼동 장치를 익히고, 새 상황·새 자료에 적용한다.
- 기존 문항과 자료·상황·보기 문장이 겹치지 않게 한다. reference_item_ids에 참고한 분석 문항을 적는다.
- {n}개가 서로 다른 세부 주제나 다른 오답 장치를 다루게 한다.{details}
- 목표 난이도: {difficulty}{fmt}

[단원 패턴 문서]
{pattern}

[문항 분석 요약]
{digests}
"""

REVISE_TASK = """
[작업] 아래 초안 문항 1개를 사용자 요청대로 고쳐 kind·source_item_id는 유지한 같은 형식의 item 1개로 다시 낸다(items 길이 1).
- 고치지 말라고 하지 않은 부분도 정답 유일성·범위·출제 의도가 맞도록 함께 점검한다.
[사용자 요청]
{instruction}
[현재 초안]
{draft}
"""

VERIFY_PROMPT = """다음은 {grade} {subject} 시험 문항이다. 각 문항을 직접 풀어라. 파일을 만들거나 명령을 실행할 필요는 없다.

규칙:
- 선택형은 answer에 선지 기호(①~⑤)만 적는다. 단답형은 답만, 서술형은 핵심 답안을 짧게 적는다.
- 보기(ㄱㄴㄷ)형은 각 보기의 참/거짓을 bogi_judgement에 적는다.
- 그림이 첨부된 문항은 그림을 보고 푼다.
- 정답이 둘 이상이거나, 정답이 없거나, 자료가 모순되거나, 조건이 부족해 정답이 정해지지 않으면 issue에 구체적으로 적는다. 문제가 없으면 issue는 null.
- {grade} 교육과정 범위를 넘는 지식이 필요하면 issue에 적는다.

출력: JSON 객체 하나만(설명·코드펜스 금지)
{{"results": [{{"no": 1, "answer": "③", "bogi_judgement": {{"ㄱ": true}} 또는 null, "reasoning": "핵심 풀이 2~3문장", "issue": null}}]}}

{problems}
"""

FIGURE_CODE_PROMPT = """교과서·문제집 문항에 들어갈 그림을 **파이썬 코드로 작도**해 PNG로 저장하라.

[그림 명세]
{spec}

[문항 본문 — 그림은 이 내용과 정확히 일치해야 한다]
{stem}

규칙:
- 현재 폴더에 figure.py를 만들고 `{python}` 로 실행해 현재 폴더에 output.png 하나를 만든다(matplotlib 사용 가능, 한글 글꼴 "Malgun Gothic").
- 흰 배경, 흑백 인쇄에도 구분되는 선·무늬, 교재 풍의 간결한 그림. 해상도 200dpi 안팎, 가로 1400px 이하.
- 그림 속 수치·눈금·라벨은 명세와 본문 그대로. 정답을 그림에 직접 적지 않는다.
- 다른 파일은 만들거나 바꾸지 않는다. 완료하면 DONE 한 단어만 답한다.
"""

FIGURE_IMAGE_PROMPT = """교과서·문제집 문항에 들어갈 그림을 만들어라.

[그림 명세]
{spec}

[문항 본문 — 그림은 이 내용과 일치해야 한다]
{stem}

규칙:
- 내장 이미지 생성 도구로 깔끔한 교재 풍 그림을 만든다(흰 배경, 과한 장식 없음). 그림 속 글자는 명세의 라벨만, 정답은 적지 않는다.
- 현재 폴더에 output.png 하나만 저장하고 DONE 한 단어만 답한다.
"""

FIGURE_CHECK_PROMPT = """다음 문항 초안들의 그림 파일을 Read 도구로 열어, 그림이 그림 명세와 문항 본문(수치·라벨·축·관계)과 일치하는지 판정하라.
정답이 그림에 직접 적혀 있거나, 수치·라벨이 본문과 다르거나, 그림만 보고 답을 정할 수 없게 그려졌으면 문제로 적는다.

출력: JSON 객체 하나만 {{"results": [{{"no": 1, "ok": true, "issues": []}}]}}

{entries}
"""


def blocks_digest(row: Mapping[str, Any]) -> str:
    return json.dumps({
        "item_id": row["item_id"], "번호": row.get("printed_number"),
        "세부": (row.get("unit") or {}).get("detail_topic"), "난이도": (row.get("difficulty") or {}).get("level"),
        "형식": (row.get("structure") or {}).get("format"), "발문": (row.get("stem") or {}).get("type"),
        "아이디어": row.get("idea"), "보기형식": [c.get("pattern") for c in row.get("choice_form") or []],
        "오답유도": [d.get("intent") for d in row.get("distractors") or []],
        "혼동장치": row.get("confusion_devices"),
    }, ensure_ascii=False)


def _render_source(row: Mapping[str, Any], unit_path: str) -> str:
    lines = [f"### 원문항 item_id={row['item_id']} 번호={row.get('printed_number')} ({unit_path})",
             blocks_text_for_prompt((row.get("question") or {}).get("blocks"))]
    answer = row.get("answer")
    if answer:
        lines.append("[정답(Astra 저장값)] " + json.dumps(answer, ensure_ascii=False)[:400])
    for asset in row.get("assets") or []:
        if isinstance(asset, Mapping) and asset.get("abs_path"):
            lines.append(f"[그림 파일] {asset['abs_path']}")
    return "\n".join(lines)


def build_prompts(job: Mapping[str, Any], root: Path | None = None) -> list[tuple[str, str, int]]:
    """Return [(kind, prompt, expected_count)] for the job request."""

    root = root or lab_root()
    request = job["request"]
    scope = load_scope(request["scope"], root)
    unit = resolve_unit(request["scope"], request["major"], request["middle"], root)
    grade, _, subject = request["scope"].partition("-")
    guides, _ = guides_text(subject, root)
    levels = [x.strip() for x in str(request.get("difficulty") or "").split(",") if x.strip()]
    analyses = analyses_for_unit(unit["major"], unit["middle"], root)
    inputs = lab_inputs(root)
    details = request.get("details") or []
    if details:
        focused = [a for a in analyses if (a.get("unit") or {}).get("detail_topic") in details]
        analyses = focused or analyses
    pool = [a for a in analyses if (a.get("difficulty") or {}).get("level") in levels
            and not (a.get("answer_check") or {}).get("answer_doubt")] or analyses
    rng = random.Random(job["job_id"])
    free = request.get("free_request")
    head = COMMON.format(grade=grade, subject=subject, curriculum=scope.get("curriculum"),
                         unit_concepts=json.dumps(unit["concepts"], ensure_ascii=False),
                         excluded=" / ".join(scope.get("excluded_notes") or []),
                         guides=guides or "(지침서 없음)",
                         figure_rule=FIGURE_RULES[request.get("figure") or "auto"],
                         free_request=f"\n[사용자 요청 — 범위·정답 유일성 규칙 안에서 반영]\n{free}\n" if free else "")
    fmt = f"\n- 형식: {request['format']}" if request.get("format") else ""
    prompts: list[tuple[str, str, int]] = []
    if request.get("variants"):
        ids = list(request.get("source_ids") or [])[: request["variants"]]
        if len(ids) < request["variants"]:
            rest = [a["item_id"] for a in pool if a["item_id"] not in ids]
            ids += rng.sample(rest, min(request["variants"] - len(ids), len(rest)))
        by_id = {a["item_id"]: a for a in analyses_for_unit(unit["major"], unit["middle"], root)}
        for start in range(0, len(ids), BATCH):
            chunk = ids[start:start + BATCH]
            sources = []
            for item_id in chunk:
                row = inputs.get(item_id)
                if row is None:
                    raise ItemGenerationError(f"원문항 학습 데이터가 없습니다: {item_id}")
                analysis = by_id.get(item_id)
                sources.append(_render_source(row, f"{unit['major']} > {unit['middle']}")
                               + "\n[분석] " + (blocks_digest(analysis) if analysis else "(없음)"))
            prompts.append(("variant", head + VARIANT_TASK.format(n=len(chunk), difficulty=request["difficulty"], fmt=fmt,
                                                                  sources="\n\n".join(sources)), len(chunk)))
    if request.get("new"):
        pattern_file = _pattern_path(root, subject, unit)
        pattern = pattern_file.read_text(encoding="utf-8") if pattern_file.is_file() else "(패턴 문서 없음)"
        sample = rng.sample(pool, min(25, len(pool)))
        detail_line = f"\n- 세부 주제: {', '.join(details)}" if details else ""
        left = int(request["new"])
        while left > 0:
            n = min(BATCH, left)
            prompts.append(("new", head + NEW_TASK.format(n=n, difficulty=request["difficulty"], fmt=fmt, details=detail_line,
                                                          pattern=pattern, digests="\n".join(blocks_digest(x) for x in sample)), n))
            left -= n
    return prompts


def problem_text(draft: Mapping[str, Any]) -> str:
    lines = [f"### 문항 {draft['no']} ({draft.get('format')})", str(draft.get("stem") or "")]
    table = draft.get("table")
    if isinstance(table, Mapping):
        lines.append("[표]")
        lines.append("| " + " | ".join(map(str, table.get("columns") or [])) + " |")
        lines += ["| " + " | ".join(map(str, row)) + " |" for row in table.get("rows") or []]
    figure = draft.get("figure")
    if isinstance(figure, Mapping) and figure.get("file"):
        lines.append(f"[그림: 첨부 이미지 {Path(str(figure['file'])).name}]")
    lines.append(str(draft.get("instruction") or ""))
    if draft.get("bogi"):
        lines.append("[보기]")
        lines += [f"{k}. {v}" for k, v in draft["bogi"].items()]
    if draft.get("choices"):
        lines += [f"{k} {v}" for k, v in draft["choices"].items()]
    return "\n".join(lines)


_CIRCLED = {label: str(index + 1) for index, label in enumerate(CHOICE_LABELS)}


def normalize_answer(value: Any) -> str:
    text = str(value or "").strip()
    for label, digit in _CIRCLED.items():
        text = text.replace(label, digit)
    return re.sub(r"[\s,.·()\[\]번]", "", text)


def verification_status(draft: Mapping[str, Any], result: Mapping[str, Any] | None) -> str:
    if not result:
        return "not_solved"
    if result.get("issue"):
        return "item_issue"
    if normalize_answer(result.get("answer")) == normalize_answer(draft.get("answer")):
        return "verified"
    return "answer_conflict" if draft.get("choices") else "needs_review"


# --------------------------------------------------------------------------- job steps

def _corpus(root: Path) -> tuple[dict[str, set[str]], dict[str, Any]]:
    inputs = lab_inputs(root)
    corpus = {item_id: grams(_blocks_text((row.get("question") or {}).get("blocks"))) for item_id, row in inputs.items()}
    printed = {item_id: row.get("printed_number") for item_id, row in inputs.items()}
    return corpus, printed


def _recheck(draft: dict[str, Any], unit_concepts: list[str], corpus, printed) -> None:
    draft["auto_checks"] = auto_checks(draft, allowed_concepts=unit_concepts, corpus=corpus, printed=printed)


def _progress(workspace: Path, job_id: str, step: str, message: str, **extra: Any) -> dict[str, Any]:
    def change(job: dict[str, Any]) -> None:
        job["progress"] = {"step": step, "message": message, "at": utc_now()}
        job.update(extra)
    return update_job(workspace, job_id, change)


def _clean_draft(raw: Mapping[str, Any], kind: str) -> dict[str, Any]:
    draft = {k: v for k, v in dict(raw).items() if k not in {"no", "auto_checks", "verification", "confirmed_item_id"}}
    draft.setdefault("kind", kind)
    if isinstance(draft.get("figure"), Mapping):
        draft["figure"] = {k: v for k, v in draft["figure"].items() if k in {"method", "alt_text", "spec"}}
    elif draft.get("figure") is not None:
        draft["figure"] = None
    return draft


def step_generate(workspace: Path, job: Mapping[str, Any], runners: Runners, root: Path) -> None:
    prompts = build_prompts(job, root)
    unit = resolve_unit(job["request"]["scope"], job["request"]["major"], job["request"]["middle"], root)
    corpus, printed = _corpus(root)
    _, guide_sha = guides_text(job["request"]["scope"].partition("-")[2], root)
    for index, (kind, prompt, expected) in enumerate(prompts, 1):
        _progress(workspace, job["job_id"], "generate", f"문항 생성 중 ({index}/{len(prompts)}, {kind})")
        last_error = None
        for attempt in (1, 2):
            try:
                text, usage = runners.claude(prompt, effort=GENERATE_EFFORT, add_dirs=[workspace])
                items = _json_object(text).get("items")
                if not isinstance(items, list) or not items:
                    raise ItemGenerationError("생성 결과에 items가 없습니다.")
                break
            except Exception as exc:  # noqa: BLE001 - recorded on the job
                last_error, items, usage = exc, None, None
        if items is None:
            raise ItemGenerationError(f"문항 생성 실패: {last_error}")

        def change(current: dict[str, Any]) -> None:
            for raw in items[:expected]:
                draft = _clean_draft(raw, kind)
                draft["no"] = len(current["drafts"]) + 1
                draft["generator_model"] = CLAUDE_MODEL
                draft["generator_effort"] = GENERATE_EFFORT
                draft["verification"] = {"status": "pending"}
                _recheck(draft, unit["concepts"], corpus, printed)
                current["drafts"].append(draft)
            current["usage"].append({"step": "generate", "kind": kind, **(usage or {})})
            current["guide_sha256"] = guide_sha
            current["generator_model"] = CLAUDE_MODEL
            current["generator_effort"] = GENERATE_EFFORT
        update_job(workspace, job["job_id"], change)


def _figure_attempt(workspace: Path, job_id: str, draft: Mapping[str, Any], method: str, runners: Runners) -> Path:
    spec = json.dumps((draft.get("figure") or {}).get("spec") or {}, ensure_ascii=False, indent=1)
    stem = problem_text({**draft, "figure": None})
    workdir = _scratch_dir("astra-itemgen-figure")
    try:
        prompt = (FIGURE_CODE_PROMPT.format(spec=spec, stem=stem, python=LAB_PYTHON) if method == "code"
                  else FIGURE_IMAGE_PROMPT.format(spec=spec, stem=stem))
        runners.codex(prompt, effort="medium", images=[], write=True, workdir=workdir, timeout=900)
        output = workdir / "output.png"
        if output.is_symlink() or not output.is_file() or output.stat().st_size < 200:
            raise ItemGenerationError("그림 파일이 만들어지지 않았습니다.")
        from PIL import Image

        with Image.open(output) as opened:
            opened.verify()
        target_dir = job_dir(workspace, job_id) / "figures"
        target_dir.mkdir(exist_ok=True)
        target = target_dir / f"d{int(draft['no'])}-{method}.png"
        shutil.copyfile(output, target)
        code = workdir / "figure.py"
        if code.is_file():
            shutil.copyfile(code, target_dir / f"d{int(draft['no'])}-figure.py")
        return target
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def step_figures(workspace: Path, job: Mapping[str, Any], runners: Runners) -> None:
    todo = [d for d in job["drafts"] if not d.get("discarded") and isinstance(d.get("figure"), Mapping)
            and not d["figure"].get("file") and not d.get("confirmed_item_id")]
    for index, draft in enumerate(todo, 1):
        _progress(workspace, job["job_id"], "figure", f"그림 만드는 중 ({index}/{len(todo)}, {draft['no']}번)")
        preferred = draft["figure"].get("method") if draft["figure"].get("method") in {"code", "codex_image"} else "code"
        order = [preferred] + [m for m in ("code", "codex_image") if m != preferred]
        result, errors = None, []
        for method in order:
            try:
                result = (_figure_attempt(workspace, job["job_id"], draft, method, runners), method)
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{method}: {exc}")

        def change(current: dict[str, Any], no=draft["no"], result=result, errors=errors) -> None:
            target = next(d for d in current["drafts"] if d["no"] == no)
            if result:
                target["figure"]["file"] = str(result[0].relative_to(job_dir(workspace, current["job_id"]))).replace("\\", "/")
                target["figure"]["method"] = result[1]
                target["figure"]["check"] = {"status": "pending"}
            target["figure"]["errors"] = errors
            current["usage"].append({"step": "figure", "no": no, "ok": bool(result), "errors": errors})
        update_job(workspace, job["job_id"], change)


def step_figure_check(workspace: Path, job: Mapping[str, Any], runners: Runners) -> None:
    todo = [d for d in job["drafts"] if not d.get("discarded") and isinstance(d.get("figure"), Mapping)
            and d["figure"].get("file") and (d["figure"].get("check") or {}).get("status") in (None, "pending")]
    if not todo:
        return
    _progress(workspace, job["job_id"], "figure_check", f"그림·본문 대조 중 ({len(todo)}개)")
    base = job_dir(workspace, job["job_id"])
    entries = "\n\n".join(
        f"### 문항 {d['no']}\n[그림 파일] {base / d['figure']['file']}\n[그림 명세] "
        + json.dumps(d["figure"].get("spec"), ensure_ascii=False) + "\n" + problem_text({**d, "figure": None})
        for d in todo)
    try:
        text, usage = runners.claude(FIGURE_CHECK_PROMPT.format(entries=entries), effort=CHECK_EFFORT, add_dirs=[base])
        results = {int(r["no"]): r for r in _json_object(text).get("results") or [] if str(r.get("no", "")).isdigit()}
        error = None
    except Exception as exc:  # noqa: BLE001
        results, usage, error = {}, None, str(exc)

    def change(current: dict[str, Any]) -> None:
        for draft in current["drafts"]:
            if draft["no"] not in {d["no"] for d in todo}:
                continue
            result = results.get(draft["no"])
            if error or not result:
                draft["figure"]["check"] = {"status": "error", "error": error or "판정 결과 없음", "at": utc_now()}
            else:
                draft["figure"]["check"] = {"status": "ok" if result.get("ok") else "mismatch",
                                            "issues": [str(x) for x in result.get("issues") or []],
                                            "model": CLAUDE_MODEL, "effort": CHECK_EFFORT, "at": utc_now()}
            draft.setdefault("auto_checks", {})["figure_ok"] = draft["figure"]["check"]["status"] == "ok"
        current["usage"].append({"step": "figure_check", **(usage or {}), "error": error})
    update_job(workspace, job["job_id"], change)


def step_verify(workspace: Path, job: Mapping[str, Any], runners: Runners) -> None:
    todo = [d for d in job["drafts"] if not d.get("discarded") and not d.get("confirmed_item_id")
            and (d.get("verification") or {}).get("status") in (None, "pending", "error")
            and not (d.get("auto_checks") or {}).get("format_errors")]
    scope = job["request"]["scope"]
    grade, _, subject = scope.partition("-")
    effort = codex_effort_for_scope(scope)
    base = job_dir(workspace, job["job_id"])
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        _progress(workspace, job["job_id"], "verify", f"Codex 독립 검증 중 ({start + len(chunk)}/{len(todo)}, {effort})")
        images = [base / d["figure"]["file"] for d in chunk if isinstance(d.get("figure"), Mapping) and d["figure"].get("file")]
        prompt = VERIFY_PROMPT.format(grade=grade, subject=subject, problems="\n\n".join(problem_text(d) for d in chunk))
        try:
            text, usage = runners.codex(prompt, effort=effort, images=images)
            results = {int(r["no"]): r for r in _json_object(text).get("results") or [] if str(r.get("no", "")).isdigit()}
            error = None
        except Exception as exc:  # noqa: BLE001
            results, usage, error = {}, None, str(exc)

        def change(current: dict[str, Any], chunk=chunk, results=results, error=error, usage=usage) -> None:
            numbers = {d["no"] for d in chunk}
            for draft in current["drafts"]:
                if draft["no"] not in numbers:
                    continue
                result = results.get(draft["no"])
                record = {"model": CODEX_MODEL, "effort": effort, "at": utc_now(), "blind": True}
                if error:
                    record.update(status="error", error=error[:600])
                else:
                    record.update(status=verification_status(draft, result),
                                  verifier_answer=(result or {}).get("answer"),
                                  verifier_reasoning=(result or {}).get("reasoning"),
                                  verifier_issue=(result or {}).get("issue"),
                                  verifier_bogi=(result or {}).get("bogi_judgement"))
                draft["verification"] = record
            current["usage"].append({"step": "verify", "nos": sorted(numbers), **(usage or {}), "error": error})
        update_job(workspace, job["job_id"], change)


def step_revise(workspace: Path, job: Mapping[str, Any], runners: Runners, root: Path) -> None:
    todo = [d for d in job["drafts"] if d.get("revise_request") and not d.get("confirmed_item_id")]
    if not todo:
        return
    unit = resolve_unit(job["request"]["scope"], job["request"]["major"], job["request"]["middle"], root)
    corpus, printed = _corpus(root)
    head = build_prompts({**job, "request": {**job["request"], "variants": 0, "new": 0}}, root)  # validates inputs
    del head
    scope = load_scope(job["request"]["scope"], root)
    grade, _, subject = job["request"]["scope"].partition("-")
    guides, _ = guides_text(subject, root)
    common = COMMON.format(grade=grade, subject=subject, curriculum=scope.get("curriculum"),
                           unit_concepts=json.dumps(unit["concepts"], ensure_ascii=False),
                           excluded=" / ".join(scope.get("excluded_notes") or []), guides=guides or "(지침서 없음)",
                           figure_rule=FIGURE_RULES[job["request"].get("figure") or "auto"], free_request="")
    for draft in todo:
        _progress(workspace, job["job_id"], "revise", f"{draft['no']}번 수정 중")
        visible = {k: v for k, v in draft.items() if k not in {"auto_checks", "verification", "revise_request", "history",
                                                               "generator_model", "generator_effort"}}
        prompt = common + REVISE_TASK.format(instruction=draft["revise_request"],
                                             draft=json.dumps(visible, ensure_ascii=False, indent=1))
        try:
            text, usage = runners.claude(prompt, effort=GENERATE_EFFORT, add_dirs=[workspace])
            items = _json_object(text).get("items") or []
            revised = _clean_draft(items[0], draft["kind"]) if items else None
            error = None if revised else "수정 결과가 비어 있습니다."
        except Exception as exc:  # noqa: BLE001
            revised, usage, error = None, None, str(exc)

        def change(current: dict[str, Any], no=draft["no"], revised=revised, error=error, usage=usage) -> None:
            target = next(d for d in current["drafts"] if d["no"] == no)
            request = target.pop("revise_request", None)
            if revised is None:
                target["revise_error"] = error
                return
            history = target.get("history") or []
            history.append({"at": utc_now(), "request": request,
                            "previous": {k: v for k, v in target.items() if k != "history"}})
            keep = {"no": no, "history": history[-5:], "generator_model": CLAUDE_MODEL, "generator_effort": GENERATE_EFFORT}
            old_figure = target.get("figure") if isinstance(target.get("figure"), Mapping) else None
            target.clear()
            target.update(revised)
            target.update(keep)
            if isinstance(target.get("figure"), Mapping) and old_figure and old_figure.get("file") and \
                    target["figure"].get("spec") == old_figure.get("spec"):
                target["figure"] = dict(old_figure)
            target["verification"] = {"status": "pending"}
            _recheck(target, unit["concepts"], corpus, printed)
            current["usage"].append({"step": "revise", "no": no, **(usage or {})})
        update_job(workspace, job["job_id"], change)


def run_job(workspace: Path | str, job_id: str, runners: Runners | None = None, root: Path | None = None) -> dict[str, Any]:
    workspace = Path(workspace).resolve()
    runners = runners or Runners()
    root = root or lab_root()

    def start(job: dict[str, Any]) -> None:
        job["status"] = "running"
        job["error"] = None
    job = update_job(workspace, job_id, start)
    try:
        if job.get("route") == "quick" and not job["drafts"]:
            step_generate(workspace, load_job(workspace, job_id), runners, root)
        step_revise(workspace, load_job(workspace, job_id), runners, root)
        step_figures(workspace, load_job(workspace, job_id), runners)
        step_figure_check(workspace, load_job(workspace, job_id), runners)
        step_verify(workspace, load_job(workspace, job_id), runners)
    except Exception as exc:  # noqa: BLE001 - recorded on the job
        def fail(current: dict[str, Any]) -> None:
            current["status"] = "partial" if current["drafts"] else "failed"
            current["error"] = str(exc)[:800]
            current["progress"] = {"step": "failed", "message": str(exc)[:200], "at": utc_now()}
        return update_job(workspace, job_id, fail)

    def finish(current: dict[str, Any]) -> None:
        current["status"] = "ready"
        counts: dict[str, int] = {}
        for draft in current["drafts"]:
            status = (draft.get("verification") or {}).get("status", "pending")
            counts[status] = counts.get(status, 0) + 1
        labels = {"verified": "검증 통과", "answer_conflict": "정답 불일치", "needs_review": "검토 필요", "item_issue": "문항 문제 지적",
                  "not_solved": "검증 응답 없음", "error": "검증 실패", "pending": "검증 전"}
        current["progress"] = {"step": "done", "message": "완료: " + ", ".join(f"{labels.get(k, k)} {v}" for k, v in counts.items()),
                               "at": utc_now()}
    return update_job(workspace, job_id, finish)


# --------------------------------------------------------------------------- draft operations

def request_revision(workspace: Path | str, job_id: str, no: int, instruction: str) -> dict[str, Any]:
    instruction = str(instruction or "").strip()
    if not instruction:
        raise ItemGenerationError("수정 요청을 입력하세요.")

    def change(job: dict[str, Any]) -> None:
        if job.get("status") in {"queued", "running"}:
            raise ItemGenerationError("작업이 진행 중입니다. 끝난 뒤 수정하세요.")
        draft = next((d for d in job["drafts"] if d["no"] == int(no)), None)
        if draft is None or draft.get("confirmed_item_id"):
            raise ItemGenerationError("수정할 수 없는 초안입니다.")
        draft["revise_request"] = instruction[:2000]
        draft.pop("revise_error", None)
        job["status"] = "queued"
        job["progress"] = {"step": "queued", "message": f"{no}번 수정 대기", "at": utc_now()}
    return update_job(workspace, job_id, change)


EDITABLE = {"stem", "instruction", "table", "bogi", "choices", "answer", "solution", "design_intent",
            "distractor_intents", "unique_answer_rationale", "difficulty", "format", "scope_concepts", "concepts"}


def edit_draft(workspace: Path | str, job_id: str, no: int, fields: Mapping[str, Any], root: Path | None = None) -> dict[str, Any]:
    unexpected = set(fields) - EDITABLE
    if unexpected:
        raise ItemGenerationError(f"고칠 수 없는 항목: {', '.join(sorted(unexpected))}")
    root = root or lab_root()
    corpus, printed = _corpus(root)

    def change(job: dict[str, Any]) -> None:
        if job.get("status") in {"queued", "running"}:
            raise ItemGenerationError("작업이 진행 중입니다. 끝난 뒤 고치세요.")
        unit = resolve_unit(job["request"]["scope"], job["request"]["major"], job["request"]["middle"], root)
        draft = next((d for d in job["drafts"] if d["no"] == int(no)), None)
        if draft is None or draft.get("confirmed_item_id"):
            raise ItemGenerationError("고칠 수 없는 초안입니다.")
        candidate = {**draft, **dict(fields)}
        errors = draft_format_errors(candidate)
        if errors:
            raise ItemGenerationError(errors[0])
        history = draft.get("history") or []
        history.append({"at": utc_now(), "request": "직접 편집", "previous": {k: v for k, v in draft.items() if k != "history"}})
        draft.update(dict(fields))
        draft["history"] = history[-5:]
        draft["edited_by_user"] = True
        draft["verification"] = {"status": "pending"}
        _recheck(draft, unit["concepts"], corpus, printed)
        job["status"] = "queued"
        job["progress"] = {"step": "queued", "message": f"{no}번 편집 후 재검증 대기", "at": utc_now()}
    return update_job(workspace, job_id, change)


def set_discarded(workspace: Path | str, job_id: str, no: int, discarded: bool) -> dict[str, Any]:
    def change(job: dict[str, Any]) -> None:
        draft = next((d for d in job["drafts"] if d["no"] == int(no)), None)
        if draft is None or draft.get("confirmed_item_id"):
            raise ItemGenerationError("바꿀 수 없는 초안입니다.")
        draft["discarded"] = bool(discarded)
    return update_job(workspace, job_id, change)


def import_bot_drafts(workspace: Path | str, request: Mapping[str, Any], drafts: list[Any], *,
                      bot_session_id: str | None, root: Path | None = None) -> dict[str, Any]:
    """Create a bot-route job from drafts the bot wrote; format errors reject the import."""

    from .item_generation import create_job

    root = root or lab_root()
    if not isinstance(drafts, list) or not drafts:
        raise ItemGenerationError("가져올 초안이 없습니다.")
    if len(drafts) > 10:
        raise ItemGenerationError("한 번에 10문항까지 가져올 수 있습니다.")
    cleaned = []
    for index, raw in enumerate(drafts, 1):
        draft = _clean_draft(raw if isinstance(raw, Mapping) else {}, str((raw or {}).get("kind") or "new"))
        errors = draft_format_errors(draft)
        if errors:
            raise ItemGenerationError(f"봇 초안 {index}번 형식 오류: {errors[0]}")
        cleaned.append(draft)
    unit = resolve_unit(request["scope"], request["major"], request["middle"], root)
    corpus, printed = _corpus(root)
    job = create_job(workspace, request, route="bot", bot_session_id=bot_session_id)
    _, guide_sha = guides_text(request["scope"].partition("-")[2], root)

    def change(current: dict[str, Any]) -> None:
        for draft in cleaned:
            draft["no"] = len(current["drafts"]) + 1
            draft["generator_model"] = "item-analyst (hermes bot)"
            draft["generator_effort"] = None
            draft["verification"] = {"status": "pending"}
            _recheck(draft, unit["concepts"], corpus, printed)
            current["drafts"].append(draft)
        current["guide_sha256"] = guide_sha
        current["generator_model"] = "item-analyst (hermes bot)"
    return update_job(workspace, job["job_id"], change)


# --------------------------------------------------------------------------- worker

class GenerationWorker:
    """One background thread that processes queued generation jobs in order."""

    def __init__(self, workspace_root: Path, *, runners: Runners | None = None, autostart: bool = True) -> None:
        self.workspace = Path(workspace_root).resolve()
        self.runners = runners or Runners()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._recover()
        if autostart:
            self.start()

    def _recover(self) -> None:
        for summary in list_jobs(self.workspace, limit=500):
            if summary.get("status") == "running":
                def change(job: dict[str, Any]) -> None:
                    job["status"] = "queued"
                    job["progress"] = {"step": "queued", "message": "서버 재시작으로 다시 대기", "at": utc_now()}
                try:
                    update_job(self.workspace, str(summary["job_id"]), change)
                except Exception:  # noqa: BLE001
                    pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="item-generation", daemon=True)
        self._thread.start()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _next(self) -> str | None:
        queued = [j for j in list_jobs(self.workspace, limit=500) if j.get("status") == "queued"]
        queued.sort(key=lambda j: str(j.get("updated_at") or ""))
        return str(queued[0]["job_id"]) if queued else None

    def run_once(self) -> bool:
        job_id = self._next()
        if job_id is None:
            return False
        run_job(self.workspace, job_id, self.runners)
        return True

    def _loop(self) -> None:
        while True:
            try:
                if self.run_once():
                    continue
            except Exception:  # noqa: BLE001 - keep the worker alive
                time.sleep(2)
            self._wake.wait(30)
            self._wake.clear()
