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
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .item_generation import (
    CLAUDE_DEFAULT_EFFORT,
    CHOICE_LABELS,
    ItemGenerationError,
    analyses_for_unit,
    auto_checks,
    blocks_text_for_prompt,
    claude_effort_for_scope,
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
from . import item_registry as registry
from .model import utc_now
from .item_generation_identity import digest, draft_identity, contributors, independence, safe_path, image_bytes, image_sandbox, receipt_model

CLAUDE_BIN = os.environ.get("ASTRA_ITEMGEN_CLAUDE_BIN", "C:/Users/ExampleUser/.local/bin/claude.exe")
CODEX_BIN = os.environ.get("ASTRA_ITEMGEN_CODEX_BIN", "C:/Users/ExampleUser/AppData/Local/Programs/nodejs/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe")
CLAUDE_MODEL = "claude-opus-5-5"
GENERATE_EFFORT = CLAUDE_DEFAULT_EFFORT  # default; authoring selects effort by scope
CHECK_EFFORT = "medium"  # figure/body comparison is a reading check, not authoring
CODEX_MODEL = "gpt-6-astra"
LAB_PYTHON = os.environ.get("ASTRA_ITEMGEN_LAB_PYTHON", "C:/Question_Item_Lab/.venv/Scripts/python.exe")
BATCH = 5


# --------------------------------------------------------------------------- runners

class ProviderFailure(ItemGenerationError):
    """Safe provider category, never the raw CLI dump or credentials."""

    def __init__(self, provider: str, category: str, usage: dict | None = None):
        self.provider, self.category, self.usage = provider, category, usage or {}
        super().__init__(f"{provider}: {category}")


class ProviderQuotaExhausted(ProviderFailure):
    def __init__(self, provider: str, usage: dict | None = None):
        super().__init__(provider, "subscription_usage_exhausted", usage)


def is_quota_exhaustion(value: Any) -> bool:
    if isinstance(value, Mapping):
        # Only explicit subscription usage categories, not rate_limit_error/HTTP 429.
        if value.get("type") in {"usage_limit_reached", "usage_limit_exceeded", "subscription_usage_limit"} \
                or value.get("code") in {"usage_limit_reached", "usage_limit_exceeded", "subscription_usage_limit"}:
            return True
        return any(is_quota_exhaustion(value.get(k)) for k in ("error", "errors", "result", "message"))
    if isinstance(value, list):
        return any(is_quota_exhaustion(v) for v in value)
    text = str(value or "")
    return bool(re.search(r"you(?:'|’|\u0027)ve hit your (?:(?:weekly|daily|session) )?(?:usage )?limit\b[\s\S]{0,300}\bresets?\b", text, re.I)
                or re.search(r"\byou have reached your usage limit\b", text, re.I))


def _provider_error(provider: str, value: Any, usage: dict | None = None) -> ProviderFailure:
    if is_quota_exhaustion(value):
        return ProviderQuotaExhausted(provider, usage)
    text = json.dumps(value, ensure_ascii=False).lower()
    category = "cli_failure"
    for pattern, name in (("unsupported model|model.*not.*supported", "unsupported_model"),
                          ("auth|unauthorized|credential", "authentication"),
                          ("429|rate.?limit", "rate_limited"), ("overload|capacity_unavailable", "overloaded"),
                          ("timeout|timed out", "timeout"), ("network|connection", "network"),
                          ("policy|denied", "policy_denied")):
        if re.search(pattern, text):
            category = name
            break
    return ProviderFailure(provider, category, usage)


def _subprocess(provider: str, cmd: list[str], **kwargs):
    try:
        return subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise ProviderFailure(provider, "timeout") from exc
    except OSError as exc:
        raise ProviderFailure(provider, "cli_unavailable") from exc

def _json_object(text: str) -> Any:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ItemGenerationError("모델 응답에서 JSON을 찾지 못했습니다.")
    return json.loads(text[start:end + 1])


def _scratch_dir(prefix: str, parent: Path | None = None) -> Path:
    # Python 3.13 mkdtemp applies an owner-only ACL on Windows that the Codex
    # sandbox account cannot read; a plain mkdir inherits normal permissions.
    base = parent or Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{prefix}-{os.getpid()}-{threading.get_ident()}-{os.urandom(5).hex()}"
    os.mkdir(path)
    return path


def real_claude(prompt: str, *, effort: str, add_dirs: list[Path]) -> tuple[str, dict[str, Any]]:
    cmd = [CLAUDE_BIN, "-p", "--model", CLAUDE_MODEL, "--effort", effort, "--output-format", "json",
           "--restricted", "--safe-mode", "--permission-mode", "dontAsk", "--tools", "Read",
           "--allowedTools", "Read(./image-*.png)" if len(add_dirs) == 1 and add_dirs[0].name.startswith('blind-') else "Read",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disallowedTools", "mcp__*",
           "--no-session-persistence"]
    for directory in add_dirs:
        cmd += ["--add-dir", str(directory)]
    started = time.time()
    proc = _subprocess("claude", cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          cwd=str(add_dirs[0] if add_dirs else lab_root()), timeout=1800)
    try:
        envelope = json.loads(proc.stdout)
        if not isinstance(envelope, dict):
            envelope = {}
    except (ValueError, TypeError):
        envelope = {}
    model_usage = envelope.get("modelUsage") or {}
    actual = envelope.get("model") or (next(iter(model_usage)) if len(model_usage) == 1 else None)
    usage = {"model": actual or CLAUDE_MODEL, "requested_model": CLAUDE_MODEL, "actual_model": actual,
             "model_confirmed": bool(actual), "effort": effort, "usage": envelope.get("usage"),
             "model_usage": model_usage,
             "duration_s": round(time.time() - started, 1)}
    if proc.returncode != 0 or envelope.get("is_error") or is_quota_exhaustion(envelope):
        raise _provider_error("claude", [envelope, proc.stdout, proc.stderr], usage)
    if not envelope or not isinstance(envelope.get("result"), str):
        raise ProviderFailure("claude", "invalid_envelope", usage)
    return str(envelope.get("result") or ""), usage


def _codex_startup_receipt(stderr: str) -> dict:
    # The installed CLI's stderr starts with its version and delimited config.
    # Never search for model/effort in the user prompt or assistant's text.
    stderr = (stderr or '').replace('\r\n', '\n')
    match = re.match(r'\AOpenAI Codex v[0-9]+\.[0-9]+\.[0-9]+[^\r\n]*\r?\n--------\r?\n'
                     r'(?P<header>(?:(?!--------|user$|assistant$)[^\r\n]*\r?\n)*)--------\r?\n', stderr or '')
    if not match:
        return {}
    config = match['header']
    if any(line.strip() in {'user', 'assistant'} for line in config.splitlines()):
        return {}
    models = re.findall(r'^model: ([^\r\n]+)$', config, re.M)
    efforts = re.findall(r'^reasoning effort: ([^\r\n]+)$', config, re.M)
    if len(models) != 1 or len(efforts) != 1:
        return {}
    receipt = {'cli_selected_model': models[0].strip(), 'cli_selected_effort': efforts[0].strip(),
               'model_evidence': 'cli_startup_receipt'}
    footer = re.search(r'(?:^|\n)tokens used\r?\n([0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)\s*\Z', stderr)
    if footer:
        receipt['usage'] = {'total_tokens': int(footer[1].replace(',', ''))}
    return receipt


def real_codex(prompt: str, *, effort: str, images: list[Path], write: bool = False,
               workdir: Path | None = None, timeout: int = 1800) -> tuple[str, dict[str, Any]]:
    own = workdir is None
    workdir = workdir or _scratch_dir("astra-itemgen-codex")
    try:
        last = workdir / f"_itemgen-{uuid.uuid4().hex}.txt"
        cmd = [CODEX_BIN, "exec", "-m", CODEX_MODEL, "-c", f'model_reasoning_effort="{effort}"',
               "-s", "workspace-write" if write else "read-only", "--skip-git-repo-check", "--ephemeral",
               "-C", str(workdir), "-o", str(last)]
        if not write:
            # Read-only shell sandbox is not a confidentiality boundary. Text /
            # blind reviewers need no tools: attach pixels directly and disable
            # ambient instructions, memory, plugins and file/command capabilities.
            cmd += ['--ignore-user-config', '-c', 'project_doc_max_bytes=0', '-c', 'web_search="disabled"',
                    '-c', 'approval_policy="never"', '-c', 'mcp_servers={}', '-c', 'plugins={}',
                    '-c', 'features.skip_host_skill_discovery=true']
            for feature in ('shell_tool', 'unified_exec', 'view_image', 'apps', 'multi_agent', 'multi_agent_v2',
                'hooks', 'memories', 'goals', 'shell_snapshot', 'plugins', 'remote_plugin', 'skill_search',
                'browser_use', 'computer_use', 'code_mode', 'code_mode_host', 'image_generation'):
                cmd += ['--disable', feature]
        for image in images:
            cmd += ["-i", str(image)]
        cmd.append("-")
        started = time.time()
        proc = _subprocess("codex", cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              cwd=str(workdir), timeout=timeout)
        events = []
        for line in (proc.stdout or "").splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            except ValueError:
                pass
        receipt = _codex_startup_receipt(proc.stderr)
        selected = receipt.get('cli_selected_model')
        usage = {"model": selected or CODEX_MODEL, "requested_model": CODEX_MODEL, "actual_model": None,
                 "model_confirmed": False, "effort": effort, "usage": None, **receipt,
                 "duration_s": round(time.time() - started, 1)}
        # Structured failures remain errors even in the text protocol; JSONL
        # model fields are not a receipt on this entry point. No paid retry.
        cli_prefix = re.split(r'(?m)^(?:user|assistant)\r?$', proc.stderr or '', maxsplit=1)[0]
        text_error = re.search(r'(?im)^\s*(?:error|fatal)\s*:', cli_prefix + '\n' + (proc.stdout or ''))
        if proc.returncode != 0 or text_error or any(e.get("type") in {"error", "turn.failed"} or e.get('is_error') for e in events) \
                or is_quota_exhaustion([events, proc.stdout, proc.stderr]):
            raise _provider_error("codex", [events, proc.stdout, proc.stderr], usage)
        text = last.read_text(encoding="utf-8") if last.exists() else ""
        if not text.strip():
            raise ProviderFailure("codex", "missing_output", usage)
        if is_quota_exhaustion(text):
            raise ProviderQuotaExhausted('codex', usage)
        if not selected or not receipt.get('cli_selected_effort'):
            raise ProviderFailure('codex', 'cli_receipt_unknown', usage)
        if selected != CODEX_MODEL:
            raise ProviderFailure('codex', 'cli_model_mismatch', usage)
        if receipt['cli_selected_effort'] != effort:
            raise ProviderFailure('codex', 'cli_effort_mismatch', usage)
        usage['model_confirmed'] = True
        return text, usage
    finally:
        if 'last' in locals() and last.is_file():
            last.unlink()
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

REPLACE_TASK = """
[대체 생성] 앞서 만든 문항 일부가 이미 있는 문항과 겹쳐 제외되었다. 위 지시의 문항 수는 무시하고,
아래 [이미 만든 문항] 목록과 상황·자료·풀이 구조가 모두 다른 새 문항을 정확히 {n}개만 만든다.
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
    lines = [f"### 문항 {draft['no']}", str(draft.get("stem") or "")]
    table = draft.get("table")
    if isinstance(table, Mapping):
        lines.append("[표]")
        lines.append("| " + " | ".join(map(str, table.get("columns") or [])) + " |")
        lines += ["| " + " | ".join(map(str, row)) + " |" for row in table.get("rows") or []]
    figure = draft.get("figure")
    if isinstance(figure, Mapping) and figure.get("file"):
        lines.append("[그림: 첨부 이미지]")
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
    if not result or not isinstance(result.get('answer'), str) or not result['answer'].strip():
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


def is_duplicate(draft: Mapping[str, Any]) -> bool:
    return (draft.get("duplicate_check") or {}).get("status") == "duplicate"


def _job_index(workspace: Path, job: Mapping[str, Any], *, skip_no: int | None = None) -> list[dict[str, Any]]:
    """Registry index: bank + other jobs + this job's kept drafts (except ``skip_no``)."""

    index = registry.build_index(workspace, exclude_job=str(job["job_id"]))
    index += [registry.draft_entry(job, d) for d in job.get("drafts") or []
              if d.get("no") != skip_no and not d.get("confirmed_item_id") and not d.get("discarded") and not is_duplicate(d)]
    return index


def _registry_check(job: Mapping[str, Any], draft: dict[str, Any], index: list[dict[str, Any]]) -> dict[str, Any]:
    """Give the draft its stable code and fingerprint and compare it with the index."""

    draft["item_code"] = draft.get("item_code") or registry.item_code(job["job_id"], draft["no"])
    entry = registry.draft_entry(job, draft)
    draft["fingerprint"] = entry["fingerprint"]
    draft["duplicate_check"] = {**registry.check(entry, index), "at": utc_now()}
    return entry


def _progress(workspace: Path, job_id: str, step: str, message: str, **extra: Any) -> dict[str, Any]:
    def change(job: dict[str, Any]) -> None:
        job["progress"] = {"step": step, "message": message, "at": utc_now()}
        job.update(extra)
    return update_job(workspace, job_id, change)


def _clean_draft(raw: Mapping[str, Any], kind: str) -> dict[str, Any]:
    fields = {'kind', 'source_item_id', 'source_printed_number', 'reference_item_ids', 'variant_type', 'difficulty',
              'format', 'scope_concepts', 'concepts', 'stem', 'table', 'figure', 'instruction', 'bogi', 'choices',
              'answer', 'solution', 'design_intent', 'distractor_intents', 'unique_answer_rationale', 'calc_checks'}
    draft = {k: v for k, v in dict(raw).items() if k in fields}
    draft.setdefault("kind", kind)
    if isinstance(draft.get("figure"), Mapping):
        draft["figure"] = {k: v for k, v in draft["figure"].items() if k in {"method", "alt_text", "spec"}}
    elif draft.get("figure") is not None:
        draft["figure"] = None
    return draft


def _author_call(workspace: Path, job: Mapping[str, Any], runners: Runners, prompt: str,
                 *, step: str, effort: str, images: list[Path] | None = None,
                 add_dirs: list[Path] | None = None) -> tuple[str, dict]:
    current = load_job(workspace, job["job_id"])
    if not current.get("claude_exhausted"):
        try:
            text, usage = runners.claude(prompt, effort=effort, add_dirs=add_dirs or [workspace])
            usage = {**usage, "model": usage.get("actual_model") or usage.get("model") or CLAUDE_MODEL,
                          "requested_model": CLAUDE_MODEL, "effort": effort}
            if receipt_model(usage) and receipt_model(usage) != CLAUDE_MODEL:
                raise ProviderFailure('claude', 'actual_model_mismatch', usage)
            update_job(workspace, job['job_id'], lambda j: j.setdefault('usage', []).append({'step': step, **usage}))
            return text, usage
        except ProviderQuotaExhausted as exc:
            if exc.provider != "claude":
                raise
            replacement = CHECK_EFFORT if step == "figure_check" else codex_effort_for_scope(job["request"]["scope"])
            def fallback(j):
                j["claude_exhausted"] = True
                j.setdefault("fallback_transitions", []).append({
                    "at": utc_now(), "category": exc.category, "reason": "Claude subscription usage exhausted",
                    "step": step, "original_model": CLAUDE_MODEL, "original_effort": effort,
                    "replacement_model": CODEX_MODEL, "replacement_effort": replacement})
                j.setdefault("usage", []).append({"step": step, "error_category": exc.category, **exc.usage})
            update_job(workspace, job["job_id"], fallback)
    effort = CHECK_EFFORT if step == "figure_check" else codex_effort_for_scope(job["request"]["scope"])
    text, usage = runners.codex(prompt, effort=effort, images=images or [])
    usage = {**usage, "model": usage.get("actual_model") or usage.get("model") or CODEX_MODEL,
                  "requested_model": CODEX_MODEL, "effort": effort}
    if receipt_model(usage) and receipt_model(usage) != CODEX_MODEL:
        raise ProviderFailure('codex', 'actual_model_mismatch', usage)
    update_job(workspace, job['job_id'], lambda j: j.setdefault('usage', []).append({'step': step, **usage}))
    return text, usage


def _authorship(usage: Mapping[str, Any]) -> dict:
    model = receipt_model(usage) or usage.get("model") or "unknown"
    return {"generator_model": model, "generator_effort": usage.get("effort"),
            "requested_model": usage.get("requested_model"), "actual_model": usage.get("actual_model"),
            **{k: usage[k] for k in ('cli_selected_model', 'cli_selected_effort', 'model_evidence') if k in usage},
            "model_confirmed": usage.get("model_confirmed", bool(usage.get("actual_model"))),
            "contributors": [model if usage.get("model_confirmed", bool(usage.get("actual_model"))) else "unknown"]}


def _validate_items(text: str, kind: str, maximum: int) -> list[dict]:
    payload = _json_object(text)
    items = payload.get("items") if isinstance(payload, Mapping) else None
    if not isinstance(items, list) or not items or len(items) > maximum:
        raise ItemGenerationError("invalid_items_count")
    cleaned = []
    for raw in items:
        if not isinstance(raw, Mapping) or raw.get("kind") != kind:
            raise ItemGenerationError("invalid_item_kind")
        item = _clean_draft(raw, kind)
        errors = draft_format_errors(item)
        if errors:
            raise ItemGenerationError("invalid_item_payload: " + errors[0])
        cleaned.append(item)
    return cleaned


def _ensure_plan(workspace: Path, job: Mapping[str, Any], root: Path) -> dict:
    if job.get("generation_plan"):
        return dict(job)
    prompts = build_prompts(job, root)
    if sum(n for _, _, n in prompts) != int(job["request"]["variants"]) + int(job["request"]["new"]):
        raise ItemGenerationError("source_count_short: not enough variant sources")
    slots = []
    consumed: set[int] = set()
    for number, (kind, prompt, expected) in enumerate(prompts):
        sources = re.findall(r"### 원문항 item_id=(\S+)", prompt) if kind == "variant" else []
        completed = []
        for draft in job["drafts"]:
            if draft["no"] in consumed or draft.get("kind") != kind or draft.get("replacement_of"):
                continue
            if sources and draft.get("source_item_id") not in sources:
                continue
            if len(completed) < expected:
                consumed.add(draft["no"])
                completed.append(draft["no"])
        slots.append({"slot": number, "kind": kind, "prompt": prompt, "expected": expected,
                      "source_ids": sources, "completed_nos": completed, "replacement": False})
    # Unmapped historical drafts are never removed or regenerated.
    if any(not d.get("replacement_of") and d["no"] not in consumed for d in job["drafts"]):
        raise ItemGenerationError("legacy_plan_conflict: saved drafts exceed or differ from request")
    return update_job(workspace, job["job_id"], lambda j: j.update(generation_plan=slots))


def step_generate(workspace: Path, job: Mapping[str, Any], runners: Runners, root: Path) -> None:
    job = _ensure_plan(workspace, job, root)
    unit = resolve_unit(job["request"]["scope"], job["request"]["major"], job["request"]["middle"], root)
    corpus, printed = _corpus(root)
    _, guide_sha = guides_text(job["request"]["scope"].partition("-")[2], root)
    effort = claude_effort_for_scope(job["request"]["scope"])
    slot_number = 0
    while True:
        current = load_job(workspace, job["job_id"])
        if slot_number >= len(current["generation_plan"]):
            break
        slot = current["generation_plan"][slot_number]
        missing = slot["expected"] - len(slot["completed_nos"])
        if missing <= 0:
            slot_number += 1
            continue
        prompt = slot["prompt"]
        pending_sources = list(slot.get("source_ids") or [])
        if pending_sources:
            by_no = {d["no"]: d for d in current["drafts"]}
            done_sources = {by_no[n].get("source_item_id") for n in slot["completed_nos"]}
            pending_sources = [s for s in pending_sources if s not in done_sources]
            # Whitelist only missing source sections; old completed variant content is not re-requested.
            sections = re.split(r"(?=### 원문항 item_id=)", prompt)
            prompt = sections[0] + "".join(s for s in sections[1:]
                if re.match(r"### 원문항 item_id=(\S+)", s).group(1) in pending_sources)
        prompt += f"\n[REQUEST_COUNT={missing}]\n앞 문항 수 지시 대신 남은 {missing}개만 출력한다. kind={slot['kind']}."
        if pending_sources:
            prompt += "\nsource_item_id는 다음 목록에서 각각 한 번만: " + json.dumps(pending_sources)
        prompt += registry.avoid_section(_job_index(workspace, current),
            scope=job["request"]["scope"], middle=job["request"]["middle"])
        _progress(workspace, job["job_id"], "generate", f"문항 생성 중 (slot {slot_number + 1}, 남은 {missing}개)")
        images = []
        for source in pending_sources:
            for asset in (lab_inputs(root).get(source) or {}).get("assets") or []:
                if isinstance(asset, Mapping) and asset.get("abs_path"):
                    images.append(Path(asset["abs_path"]))
        text, usage = _author_call(workspace, job, runners, prompt, step="generate", effort=effort, images=images)
        items = _validate_items(text, slot["kind"], missing)
        if pending_sources:
            lineage = [d.get("source_item_id") for d in items]
            if len(set(lineage)) != len(lineage) or not set(lineage) <= set(pending_sources):
                raise ItemGenerationError("invalid_variant_lineage")
        def save(j):
            plan = j["generation_plan"][slot_number]
            index = _job_index(workspace, j)
            duplicates = []
            for raw in items:
                draft = {**raw, **_authorship(usage)}
                draft["no"] = max((d["no"] for d in j["drafts"]), default=0) + 1
                draft["content_revision"] = 1
                draft["verification"] = {"status": "pending"}
                if slot.get("replacement"):
                    original_no = slot["replacement_of"][len(plan["completed_nos"])]
                    draft["replacement_of"] = original_no
                    next(d for d in j["drafts"] if d["no"] == original_no)["replaced_by"] = draft["no"]
                _recheck(draft, unit["concepts"], corpus, printed)
                entry = _registry_check(j, draft, index)
                if is_duplicate(draft):
                    duplicates.append(draft["no"])
                else:
                    index.append(entry)
                j["drafts"].append(draft)
                plan["completed_nos"].append(draft["no"])
            if duplicates and slot["kind"] == "new" and not slot.get("replacement"):
                j["generation_plan"].append({"slot": len(j["generation_plan"]), "kind": "new",
                    "prompt": slot["prompt"] + REPLACE_TASK.format(n=len(duplicates)), "expected": len(duplicates),
                    "source_ids": [], "completed_nos": [], "replacement": True, "replacement_of": duplicates})
            plan['last_receipt'] = {'model': usage['model'], 'effort': usage['effort'], 'at': utc_now(),
                'retry': slot.get('replacement', False), 'duplicates': len(duplicates)}
            j["guide_sha256"] = guide_sha
            # This is original job metadata only; draft/version authorship is authoritative.
            j.setdefault("generator_model", usage["model"])
            j.setdefault("generator_effort", usage["effort"])
        update_job(workspace, job["job_id"], save)
        if len(items) < missing:
            raise ItemGenerationError(f"generation_output_short: expected {missing}, received {len(items)}")
        slot_number += 1


def _figure_attempt(workspace: Path, job_id: str, draft: Mapping[str, Any], method: str, runners: Runners) -> Path:
    spec = json.dumps((draft.get("figure") or {}).get("spec") or {}, ensure_ascii=False, indent=1)
    stem = problem_text({**draft, "figure": None})
    workdir = _scratch_dir("astra-itemgen-figure", job_dir(workspace, job_id) / '.scratch')
    try:
        prompt = (FIGURE_CODE_PROMPT.format(spec=spec, stem=stem, python=LAB_PYTHON) if method == "code"
                  else FIGURE_IMAGE_PROMPT.format(spec=spec, stem=stem))
        _, usage = runners.codex(prompt, effort="medium", images=[], write=True, workdir=workdir, timeout=900)
        update_job(workspace, job_id, lambda j: j.setdefault('usage', []).append({'step': 'figure', 'no': draft['no'], **usage}))
        output = safe_path(workdir, 'output.png')
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
    todo = [d for d in job["drafts"] if not d.get("discarded") and not is_duplicate(d)
            and isinstance(d.get("figure"), Mapping) and not d["figure"].get("file") and not d.get("confirmed_item_id")]
    for index, draft in enumerate(todo, 1):
        _progress(workspace, job["job_id"], "figure", f"그림 만드는 중 ({index}/{len(todo)}, {draft['no']}번)")
        preferred = draft["figure"].get("method") if draft["figure"].get("method") in {"code", "codex_image"} else "code"
        order = [preferred]
        result, errors = None, []
        for method in order:
            try:
                result = (_figure_attempt(workspace, job["job_id"], draft, method, runners), method)
                break
            except ProviderQuotaExhausted:
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{method}: {exc}")

        def change(current: dict[str, Any], no=draft["no"], result=result, errors=errors) -> None:
            target = next(d for d in current["drafts"] if d["no"] == no)
            if result:
                target["figure"]["file"] = str(result[0].relative_to(job_dir(workspace, current["job_id"]))).replace("\\", "/")
                target["figure"]["method"] = result[1]
                from .model import sha256_file
                target['figure']['sha256'] = sha256_file(result[0])
                target["figure"]["check"] = {"status": "pending"}
            target["figure"]["errors"] = errors
            current["usage"].append({"step": "figure", "no": no, "ok": bool(result), "errors": errors})
        update_job(workspace, job["job_id"], change)
        if result is None:
            raise ItemGenerationError('figure_failed: ' + ' / '.join(errors))


def _stage_current(draft: Mapping[str, Any], base: Path, record: Mapping[str, Any]) -> bool:
    identity = record.get("identity")
    if not identity:
        # Legacy receipts remain historical. Preserve untouched drafts during
        # another draft's revision; explicit retry marks these stages pending.
        return not draft.get('content_revision') and not draft.get('edited_by_user') \
            and not (draft.get('figure') or {}).get('hash_observation')
    try:
        return identity == draft_identity(draft, base)
    except ItemGenerationError:
        return False


def _result_map(text: str, numbers: set[int], *, figure: bool = False) -> dict:
    payload = _json_object(text)
    rows = payload.get("results") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ItemGenerationError("invalid_results")
    result = {}
    for row in rows:
        if not isinstance(row, Mapping) or type(row.get("no")) is not int or row["no"] not in numbers or row["no"] in result:
            raise ItemGenerationError("invalid_result_number")
        if figure:
            if type(row.get("ok")) is not bool or not isinstance(row.get("issues"), list):
                raise ItemGenerationError("invalid_figure_result")
        elif not isinstance(row.get("answer"), str) or not row["answer"].strip() or not isinstance(row.get("reasoning"), str):
            raise ItemGenerationError("invalid_blind_result")
        result[row["no"]] = dict(row)
    if set(result) != numbers:
        raise ItemGenerationError("missing_results")
    return result


def _draft_images(drafts: list[dict], base: Path) -> list[bytes]:
    images = []
    for d in drafts:
        f = d.get("figure")
        if isinstance(f, Mapping) and f.get("file"):
            data, _ = image_bytes(base, f["file"], f.get("sha256"))
            images.append(data)
    return images


def step_figure_check(workspace: Path, job: Mapping[str, Any], runners: Runners) -> None:
    base = job_dir(workspace, job["job_id"])
    todo = [d for d in job["drafts"] if not d.get("discarded") and not is_duplicate(d)
            and not d.get("confirmed_item_id") and isinstance(d.get("figure"), Mapping) and d["figure"].get("file")
            and ((d["figure"].get("check") or {}).get("status") in (None, "pending", "error")
                 or not _stage_current(d, base, d["figure"].get("check") or {}))]
    # One receipt per image comparison lets quota resume only unfinished work.
    for draft in todo:
        _progress(workspace, job["job_id"], "figure_check", f"그림·본문 대조 중 ({draft['no']}번)")
        identity = draft_identity(draft, base)
        with image_sandbox(base / ".scratch", _draft_images([draft], base)) as (sandbox, images):
            entries = (f"[그림 파일] {images[0]}\n[그림 명세] "
                + json.dumps(draft["figure"].get("spec"), ensure_ascii=False)
                + "\n" + problem_text({**draft, "figure": None}))
            text, usage = _author_call(workspace, job, runners, FIGURE_CHECK_PROMPT.format(entries=entries),
                step="figure_check", effort=CHECK_EFFORT, images=images, add_dirs=[sandbox])
            result = _result_map(text, {draft["no"]}, figure=True)[draft["no"]]
        def save(j):
            target = next(d for d in j["drafts"] if d["no"] == draft["no"])
            if draft_identity(target, base) != identity:
                raise ItemGenerationError("figure_check_stale")
            target["figure"]["check"] = {"status": "ok" if result["ok"] else "mismatch",
                "issues": [str(x) for x in result["issues"]], "model": usage["model"],
                "requested_model": usage.get("requested_model"), "actual_model": usage.get("actual_model"),
                **{k: usage[k] for k in ('cli_selected_model', 'cli_selected_effort', 'model_evidence', 'model_confirmed') if k in usage},
                "effort": CHECK_EFFORT, "at": utc_now(), "identity": identity}
            target.setdefault("auto_checks", {})["figure_ok"] = result["ok"]
        update_job(workspace, job["job_id"], save)


def step_verify(workspace: Path, job: Mapping[str, Any], runners: Runners) -> None:
    base = job_dir(workspace, job["job_id"])
    todo = [d for d in job["drafts"] if not d.get("discarded") and not d.get("confirmed_item_id") and not is_duplicate(d)
            and ((d.get("verification") or {}).get("status") in (None, "pending", "error", "not_solved")
                 or not _stage_current(d, base, d.get("verification") or {}))
            and not (d.get("auto_checks") or {}).get("format_errors")]
    scope = job["request"]["scope"]
    grade, _, subject = scope.partition("-")
    effort = codex_effort_for_scope(scope)
    exclusions = load_scope(scope).get("excluded_notes") or []
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        _progress(workspace, job["job_id"], "verify", f"Codex 블라인드 검산 중 ({start + len(chunk)}/{len(todo)}, {effort})")
        identities = {d["no"]: draft_identity(d, base) for d in chunk}
        prompt = VERIFY_PROMPT.format(grade=grade, subject=subject, problems="\n\n".join(problem_text(d) for d in chunk))
        prompt += "\n범위 제외: " + json.dumps(exclusions, ensure_ascii=False)
        with image_sandbox(base / ".scratch", _draft_images(chunk, base)) as (sandbox, images):
            text, usage = runners.codex(prompt, effort=effort, images=images, workdir=sandbox)
            update_job(workspace, job['job_id'], lambda j: j.setdefault('usage', []).append({'step': 'verify', 'nos': [d['no'] for d in chunk], **usage}))
            results = _result_map(text, {d["no"] for d in chunk})
        def save(j):
            for target in j["drafts"]:
                if target["no"] not in results:
                    continue
                if draft_identity(target, base) != identities[target["no"]]:
                    raise ItemGenerationError("verification_stale")
                result = results[target["no"]]
                actual = receipt_model(usage)
                model = actual or usage.get("model") or CODEX_MODEL
                target["verification"] = {"status": verification_status(target, result),
                    "model": model, "requested_model": CODEX_MODEL, "actual_model": usage.get('actual_model'),
                    **{k: usage[k] for k in ('cli_selected_model', 'cli_selected_effort', 'model_evidence') if k in usage},
                    "model_confirmed": usage.get("model_confirmed", bool(actual)), "effort": effort,
                    "at": utc_now(), "blind": True, "identity": identities[target["no"]],
                    "review_kind": independence(contributors(target), actual,
                        confirmed=usage.get("model_confirmed", bool(actual))),
                    "verifier_answer": result.get("answer"), "verifier_reasoning": result.get("reasoning"),
                    "verifier_issue": result.get("issue"), "verifier_bogi": result.get("bogi_judgement")}
                if actual and actual != CODEX_MODEL:
                    target['verification'].update(status='error', review_kind='unknown', error='actual_model_mismatch')
        update_job(workspace, job["job_id"], save)


def step_revise(workspace: Path, job: Mapping[str, Any], runners: Runners, root: Path) -> None:
    todo = [d for d in job["drafts"] if d.get("revise_request") and not d.get("confirmed_item_id")]
    if not todo:
        return
    effort = claude_effort_for_scope(job["request"]["scope"])
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
            text, usage = _author_call(workspace, job, runners, prompt, step='revise', effort=effort,
                images=[job_dir(workspace, job['job_id']) / draft['figure']['file']]
                if isinstance(draft.get('figure'), Mapping) and draft['figure'].get('file') else [])
            revised = _validate_items(text, draft['kind'], 1)[0]
            if revised.get('source_item_id') != draft.get('source_item_id'):
                raise ItemGenerationError('revision_lineage_changed')
        except ProviderQuotaExhausted:
            raise
        except Exception as exc:
            update_job(workspace, job['job_id'], lambda j: next(d for d in j['drafts'] if d['no'] == draft['no']).update(revise_error=str(exc)[:200]))
            raise

        def change(current: dict[str, Any], no=draft["no"], revised=revised, usage=usage) -> None:
            target = next(d for d in current["drafts"] if d["no"] == no)
            request = target.get("revise_request")
            history = target.get("history") or []
            history.append({"at": utc_now(), "request": request,
                            "previous": {k: v for k, v in target.items() if k not in {'history', 'revise_request'}}})
            authors = sorted(set(contributors(target) + _authorship(usage)['contributors']))
            keep = {"no": no, "history": history, **_authorship(usage), "contributors": authors,
                    "content_revision": int(target.get('content_revision', 0)) + 1,
                    "item_code": target.get("item_code"), 'replacement_of': target.get('replacement_of')}
            previous_verification = target.get('verification')
            old_figure = target.get("figure") if isinstance(target.get("figure"), Mapping) else None
            target.clear()
            target.update(revised)
            target.update(keep)
            if isinstance(target.get("figure"), Mapping) and old_figure and old_figure.get("file") and \
                    target["figure"].get("spec") == old_figure.get("spec"):
                target["figure"] = dict(old_figure)
                target['figure']['check'] = {'status': 'pending', 'previous': old_figure.get('check')}
            target["verification"] = {"status": "pending", 'stale': bool(previous_verification), 'previous': previous_verification}
            _recheck(target, unit["concepts"], corpus, printed)
            _registry_check(current, target, _job_index(workspace, current, skip_no=no))
        update_job(workspace, job["job_id"], change)


def run_job(workspace: Path | str, job_id: str, runners: Runners | None = None, root: Path | None = None) -> dict[str, Any]:
    workspace = Path(workspace).resolve()
    runners = runners or Runners()
    root = root or lab_root()
    if load_job(workspace, job_id).get('status') == 'paused':
        return load_job(workspace, job_id)

    def start(job: dict[str, Any]) -> None:
        from .item_generation import ItemGenerationConflict
        if job.get('status') == 'running':
            raise ItemGenerationConflict('작업이 진행 중입니다.')
        job["status"] = "running"
        job["error"] = None
    job = update_job(workspace, job_id, start)
    try:
        if job.get('work_action') == 'review':
            from .item_generation_reviews import run_reviews
            return run_reviews(workspace, job, runners)
        if job.get("route") == "quick" and job.get('work_action', 'generate') == 'generate':
            step_generate(workspace, load_job(workspace, job_id), runners, root)
        step_revise(workspace, load_job(workspace, job_id), runners, root)
        step_figures(workspace, load_job(workspace, job_id), runners)
        step_figure_check(workspace, load_job(workspace, job_id), runners)
        step_verify(workspace, load_job(workspace, job_id), runners)
    except ProviderQuotaExhausted as exc:
        def pause(current):
            current['status'] = 'paused'
            current['pause'] = {'provider': exc.provider, 'category': exc.category, 'at': utc_now(),
                                'stage': current.get('progress', {}).get('step')}
            current['codex_exhausted'] = exc.provider == 'codex'
            current.setdefault('usage', []).append({'step': current['pause']['stage'],
                'error_category': exc.category, **exc.usage})
            current['error'] = str(exc)
            current['progress'] = {'step': 'paused', 'message': '구독 사용량 한도에 도달했습니다. 사용자 재개가 필요합니다.', 'at': utc_now()}
        return update_job(workspace, job_id, pause)
    except Exception as exc:  # noqa: BLE001 - recorded on the job
        def fail(current: dict[str, Any]) -> None:
            if isinstance(exc, ProviderFailure) and exc.usage:
                current.setdefault('usage', []).append({'step': current.get('progress', {}).get('step'),
                    **exc.usage, 'error_category': exc.category})
            current["status"] = "partial" if current["drafts"] else "failed"
            current["error"] = str(exc)[:800]
            current["progress"] = {"step": "failed", "message": str(exc)[:200], "at": utc_now()}
        return update_job(workspace, job_id, fail)

    def finish(current: dict[str, Any]) -> None:
        if current.get('route') == 'quick':
            plan = current.get('generation_plan')
            complete = (all(len(s['completed_nos']) == s['expected'] for s in plan) if plan else
                all(sum(d.get('kind') == kind and not d.get('replacement_of') for d in current['drafts']) == current['request'][key]
                    for kind, key in (('variant', 'variants'), ('new', 'new'))))
            if not complete:
                current['status'] = 'partial'
                current['error'] = 'generation_count_incomplete'
                return
        active = [d for d in current['drafts'] if not d.get('discarded') and not is_duplicate(d)
                  and not d.get('confirmed_item_id')]
        if any(d.get('revise_request') or (d.get('verification') or {}).get('status') in {None, 'pending', 'error', 'not_solved'}
               or (isinstance(d.get('figure'), Mapping) and (not d['figure'].get('file')
                   or (d['figure'].get('check') or {}).get('status') in {None, 'pending', 'error'})) for d in active):
            current['status'] = 'partial'
            current['error'] = 'stage_incomplete'
            return
        current["status"] = "ready"
        counts: dict[str, int] = {}
        for draft in current["drafts"]:
            status = "duplicate" if is_duplicate(draft) else (draft.get("verification") or {}).get("status", "pending")
            counts[status] = counts.get(status, 0) + 1
        labels = {"duplicate": "중복 제외", "verified": "검증 통과", "answer_conflict": "정답 불일치", "needs_review": "검토 필요", "item_issue": "문항 문제 지적",
                  "not_solved": "검증 응답 없음", "error": "검증 실패", "pending": "검증 전"}
        current["progress"] = {"step": "done", "message": "완료: " + ", ".join(f"{labels.get(k, k)} {v}" for k, v in counts.items()),
                               "at": utc_now()}
    return update_job(workspace, job_id, finish)


# --------------------------------------------------------------------------- draft operations

def check_expected(job: Mapping[str, Any], expected_revision: int | None) -> None:
    from .item_generation import ItemGenerationConflict
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision != job.get('revision')):
        raise ItemGenerationConflict('생성 작업 버전이 바뀌었습니다. 새로 고치세요.')


def resume_job(workspace: Path | str, job_id: str, *, expected_revision: int | None = None) -> dict:
    from .item_generation import ItemGenerationConflict
    def change(job):
        check_expected(job, expected_revision)
        if job.get('status') in {'queued', 'running'}:
            raise ItemGenerationConflict('이미 진행 중인 작업입니다.')
        if job.get('work_action') != 'review':
            _observe_legacy_figures(job, job_dir(workspace, job_id))
            for d in job.get('drafts') or []:
                if d.get('confirmed_item_id') or d.get('discarded'):
                    continue
                v = d.get('verification') or {}
                if not v.get('identity'):
                    d['verification'] = {'status': 'pending', 'previous': v}
                f = d.get('figure')
                if isinstance(f, dict) and not (f.get('check') or {}).get('identity'):
                    f['check'] = {'status': 'pending', 'previous': f.get('check')}
        job['status'] = 'queued'
        job.pop('pause', None)
        job.pop('codex_exhausted', None)
        job['error'] = None
        if job.get('work_action') != 'review' and not any(d.get('revise_request') for d in job.get('drafts') or []):
            job['work_action'] = 'generate' if job.get('route') == 'quick' else 'verify'
        job['progress'] = {'step': 'queued', 'message': '사용자가 남은 작업 재개', 'at': utc_now()}
    return update_job(workspace, job_id, change)


def _observe_legacy_figures(job: dict, base: Path) -> None:
    """Explicit user update only; old checks are never promoted to new receipts."""
    for draft in job.get('drafts') or []:
        figure = draft.get('figure')
        if draft.get('confirmed_item_id') or draft.get('discarded') or not isinstance(figure, dict) or not figure.get('file'):
            continue
        _, sha = image_bytes(base, figure['file'], figure.get('sha256'), allow_legacy=True)
        if not figure.get('sha256'):
            figure.update(sha256=sha, hash_observation={'kind': 'legacy-first-observation', 'sha256': sha, 'at': utc_now()})

def request_revision(workspace: Path | str, job_id: str, no: int, instruction: str, *, expected_revision: int | None = None) -> dict[str, Any]:
    instruction = str(instruction or "").strip()
    if not instruction:
        raise ItemGenerationError("수정 요청을 입력하세요.")

    def change(job: dict[str, Any]) -> None:
        check_expected(job, expected_revision)
        if job.get("status") in {"queued", "running", "paused"}:
            from .item_generation import ItemGenerationConflict
            raise ItemGenerationConflict("작업이 진행 중이거나 일시정지되었습니다. 남은 단계를 재개하세요.")
        draft = next((d for d in job["drafts"] if d["no"] == int(no)), None)
        if draft is None or draft.get("confirmed_item_id"):
            raise ItemGenerationError("수정할 수 없는 초안입니다.")
        draft["revise_request"] = instruction[:2000]
        _observe_legacy_figures({'drafts': [draft]}, job_dir(workspace, job_id))
        job['work_action'] = 'revise'
        draft.pop("revise_error", None)
        job["status"] = "queued"
        job["progress"] = {"step": "queued", "message": f"{no}번 수정 대기", "at": utc_now()}
    return update_job(workspace, job_id, change)


EDITABLE = {"stem", "instruction", "table", "bogi", "choices", "answer", "solution", "design_intent",
            "distractor_intents", "unique_answer_rationale", "difficulty", "format", "scope_concepts", "concepts"}


def edit_draft(workspace: Path | str, job_id: str, no: int, fields: Mapping[str, Any], root: Path | None = None, *, expected_revision: int | None = None) -> dict[str, Any]:
    unexpected = set(fields) - EDITABLE
    if unexpected:
        raise ItemGenerationError(f"고칠 수 없는 항목: {', '.join(sorted(unexpected))}")
    root = root or lab_root()
    corpus, printed = _corpus(root)

    def change(job: dict[str, Any]) -> None:
        check_expected(job, expected_revision)
        if job.get("status") in {"queued", "running", "paused"}:
            from .item_generation import ItemGenerationConflict
            raise ItemGenerationConflict("작업이 진행 중이거나 일시정지되었습니다. 남은 단계를 재개하세요.")
        unit = resolve_unit(job["request"]["scope"], job["request"]["major"], job["request"]["middle"], root)
        draft = next((d for d in job["drafts"] if d["no"] == int(no)), None)
        if draft is None or draft.get("confirmed_item_id"):
            raise ItemGenerationError("고칠 수 없는 초안입니다.")
        candidate = {**draft, **dict(fields)}
        errors = draft_format_errors(candidate)
        if errors:
            raise ItemGenerationError(errors[0])
        _observe_legacy_figures({'drafts': [draft]}, job_dir(workspace, job_id))
        previous_verification = draft.get('verification')
        history = draft.get("history") or []
        history.append({"at": utc_now(), "request": "직접 편집", "previous": {k: v for k, v in draft.items() if k != "history"}})
        draft.update(dict(fields))
        draft["history"] = history
        draft['contributors'] = sorted(set(contributors(draft) + ['human']))
        draft['content_revision'] = int(draft.get('content_revision', 0)) + 1
        draft["edited_by_user"] = True
        job['work_action'] = 'verify'
        draft["verification"] = {"status": "pending", 'stale': bool(previous_verification), 'previous': previous_verification}
        if isinstance(draft.get('figure'), Mapping):
            draft['figure']['check'] = {'status': 'pending', 'previous': draft['figure'].get('check')}
        _recheck(draft, unit["concepts"], corpus, printed)
        _registry_check(job, draft, _job_index(Path(workspace).resolve(), job, skip_no=draft["no"]))
        job["status"] = "queued"
        job["progress"] = {"step": "queued", "message": f"{no}번 편집 후 재검증 대기", "at": utc_now()}
    return update_job(workspace, job_id, change)


def set_discarded(workspace: Path | str, job_id: str, no: int, discarded: bool, *, expected_revision: int | None = None) -> dict[str, Any]:
    def change(job: dict[str, Any]) -> None:
        check_expected(job, expected_revision)
        if job.get('status') in {'queued', 'running'}:
            from .item_generation import ItemGenerationConflict
            raise ItemGenerationConflict('작업이 진행 중입니다.')
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

    workspace = Path(workspace).resolve()

    def change(current: dict[str, Any]) -> None:
        index = _job_index(workspace, current)
        for draft in cleaned:
            draft["no"] = len(current["drafts"]) + 1
            draft["generator_model"] = "item-analyst (hermes bot)"
            draft["generator_effort"] = None
            draft["verification"] = {"status": "pending"}
            _recheck(draft, unit["concepts"], corpus, printed)
            entry = _registry_check(current, draft, index)
            if not is_duplicate(draft):
                index.append(entry)
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
                    job["status"] = "paused"
                    job["progress"] = {"step": "paused", "message": "중단된 작업입니다. 사용자 재개가 필요합니다.", "at": utc_now()}
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
