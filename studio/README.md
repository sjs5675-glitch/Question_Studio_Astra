# Question JSON Studio

> **현재 운영 안내 (2026-10-06 갱신)** — 이 문서 아래 내용은 옛 Question JSON Studio 기준이라 포트 8875, 외부 `C:/Question_Crop` 경로 등 지금과 다른 내용이 있습니다. 그대로 실행하지 마세요.
> - 시작: 프로젝트 루트의 `Open-Astra.cmd` (또는 `powershell -NoProfile -File Open-Astra.ps1`), 상태만 확인: `Open-Astra.ps1 -CheckOnly`
> - 종료: `studio/stop_app.ps1 -OwnerId preview11` — 먼저 같은 PowerShell에서 `ASTRA_RUNTIME`, `ASTRA_PORTS_FILE`, `ASTRA_WORKSPACE`를 위 운영 경로로 지정해야 합니다(지정하지 않으면 운영 서버를 찾지 못하고 아무것도 멈추지 않습니다).
> - 운영 작업공간·런타임: `.runtime/t12s-cf76f8c9/{workspace,runtime}`, 포트는 `.runtime/t12s-cf76f8c9/ports.json` (Studio 현재 8876)
> - Crop 엔진: 이 저장소의 `crop/` (외부 `C:/Question_Crop` 아님)
> - 프로젝트 개요는 루트 `README.md`를 우선합니다.

문제 PDF와 해설 PDF를 입력하면 로컬 `Question_Crop` 엔진으로 문항을 나누고 연결한 뒤, 안전한 후보 탐색을 모두 수행한 문제·해설 OCR과 canonical JSON 저장을 한 번에 수행하는 로컬 앱입니다. `full`에서는 그림 재생성 후보도 저장하며, `codex_only`에서는 원본 PDF/그림을 보존합니다. 기존 `Question_Crop` 결과는 수정하지 않습니다.

## 현재 구현 상태

현재 실제로 동작하는 범위는 다음과 같습니다.

- 상단 메뉴를 `새 작업`과 `저장된 문항`으로 분리합니다. 새 작업은 PDF batch와 진행률만, 저장된 문항은 프로젝트·문항 조회와 검수만 표시합니다.
- 상단 일괄 작업에는 이 기기의 문제 PDF·해설 PDF를 직접 선택하고 선택적인 쪽 범위(`all`, `3-12`, `3-12,20`)만 입력합니다. 서버에 이미 있는 PDF 경로 방식은 고급 항목으로 유지하며 workers·DPI·OCR/그림 모델 등 기술 설정은 내부 고품질 기본값으로 고정됩니다.
- **전체 자동 처리 시작**을 누르면 숨은 `Question_Crop` loopback backend를 필요할 때 실행하고 `PDF crop/검증 → 문제·해설 연결 → 최선 후보 재판정 → 모든 crop 등록 → OCR → 문항별 JSON 저장 → (full이면) 그림 후보 재생성`을 비동기로 수행합니다. PASS/REVIEW/FAIL은 품질 경고로 보존하되, 실제 crop 파일이 있는 문항은 모두 등록하고 자동 OCR합니다. OCR이 실패해도 원본 crop 등록은 유지됩니다.
- 해설 연결은 `기존 verified → 저장된 review 후보 → 독립 검증 재집계 → 정확한 범위·번호의 2모델 consensus → 빠른 정답 2모델 합의`를 순서대로 모두 확인합니다. 형제 소문항이 섞일 수 있는 block crop은 버리고, 그래도 확정되지 않은 안전한 후보는 `저신뢰 · 검토 필요`, 상세 후보가 없으면 `연결 실패`로 기록합니다. 오래된 run에서 검토 후보 ID의 crop이 빠져 있어도 문항 import를 중단하지 않고 `해설 검토 필요` 원인과 upstream ID를 보존합니다.
- `verified`와 `review_only` 상세 해설 crop을 모두 자동 OCR합니다. 상세 해설 crop이 없고 빠른정답 crop만 있으면 그 crop을 답 전사용 fallback으로 사용하되 링크 상태를 자동 승격하지 않습니다. 원본 해설 PDF가 SHA-256과 일치하면 새 consensus crop은 450 DPI로 다시 렌더링하고, 원본이 없을 때만 기존 200 DPI 증거 렌더를 사용합니다. 자동 해설 OCR은 이미 저장된 사용자 정답을 덮어쓰지 않습니다.
- 최근 작업 UI는 현재 단계, 진행률, 저장 문항 수와 미완료 항목만 2초마다 갱신합니다. 모델명·원격 호출·비용 같은 기술 정보는 기본 화면에 노출하지 않습니다.
- 일괄 job은 `workspace/.batch_jobs/<job-id>/job.json`에 원자 저장됩니다. 실행 중 취소는 현재 외부 호출이 끝난 다음 안전한 단계 경계에서 확정되고, 앱 재시작 시 active job은 `interrupted`로 저장됩니다. 최근 작업 카드에서 7일 안에 명시적으로 재개하면 기존 크롭·프로젝트·OCR·그림 캐시와 남은 그림 예산을 재사용합니다.
- `manifest.json`이 있는 기존 leaf run을 읽기 전용으로 import합니다. 문제/해설 crop을 새 `workspace`로 복사하고 SHA-256, 상대경로, provenance를 저장합니다.
- 같은 source manifest는 UUIDv5/fingerprint로 중복 import하지 않으며, staging 디렉터리를 완성한 뒤 새 프로젝트로 승격합니다.
- 브라우저에서 문제/해설 원본 위 OCR bbox를 대조·교정하고 block 순서를 바꿀 수 있습니다. `full` 그림은 원본 보존형 재크롭, Gemini 재생성, 자산 승인·거절, 승인된 동일 계보 variant 선택을 지원하며 `codex_only`는 재생성을 지원하지 않습니다.
- 저장 화면은 문제·해설 입력 완료 여부와 저장된 정답을 먼저 보여 줍니다. 문제/해설 탭 하나로 원본 이미지, 타이핑 결과, 전체 입력 내용을 함께 전환하며, 전체 결과는 문장·선택지·보기·표를 펼친 읽기 화면으로 표시합니다. 순서 이동과 블록 구조는 `순서·구조 편집`에 접어 두고 내부 ID·버전·JSON은 기술 정보에만 남깁니다.
- 문항별 `tags`에는 2022 개정 과목·학년, 간단 단원, 3단계 난이도, 문제 유형, 핵심어, 그림·그래프·표·수식 여부와 시각자료 의존도를 저장합니다. 현재 물리학 단원은 `힘과 에너지 / 전기와 자기 / 빛과 물질`로 고정하며 A–P 책 번호 규칙을 우선 적용하고 Gemini가 난이도·유형·핵심어와 모의고사형 단원을 보완합니다. 낮은 신뢰도도 저장하며 태그만 수정할 때 기존 문항 승인과 HWPX 내용은 유지됩니다.
- `문제은행`은 모든 프로젝트의 문항을 복사 없이 한 목록으로 합치고 기존 태그로 검색합니다. 문항을 `문제지 만들기`에 담아 드래그 또는 위·아래 버튼으로 순서를 바꾸고 제외할 수 있으며, 문제지 JSON은 revision CAS로 저장됩니다. 출력 번호는 원본을 바꾸지 않고 선택 순서대로 1번부터 부여합니다.
- `H19(1)·H19(2)`처럼 공통 지문·그림을 쓰는 소문항은 문제은행에서 하나의 연속 세트로 표시됩니다. 개별 문항 또는 세트 전체를 담을 수 있고, 여러 형제를 선택하면 순서와 인접성을 자동 보정해 세트 단위로 이동합니다. 한 문항만 출력하면 공통 내용을 포함하고, 여러 형제를 출력하면 공통 지문·그림은 첫 문항에만 한 번 넣습니다.
- 동일·유사 문제는 전체 문제 내용을 정규화한 fingerprint로 찾아 `중복 가능` 경고만 표시합니다. 자동 삭제·병합하지 않으며 사용자가 그대로 문제지에 담을 수 있습니다.
- 선택 문제지는 `문제·해설 따로` 또는 `한 파일·미주 해설` 중 하나로 HWPX를 생성합니다. 문제은행 출력은 A4 2단·8mm 단 간격·검은 단 구분선과 좌우 13mm·상하 8mm 여백을 사용합니다. 한 파일 모드는 문제 번호마다 실제 native HWPX 미주를 연결하고, 미주에는 저장된 `[답]`과 `[풀이]`만 넣습니다. `<보기>`는 샘플과 같은 4행×5열 제목 틈 표, 짧은 객관식은 무테 3열 표로 만들며 긴 선택지는 세로로 배치합니다. 사용자 템플릿 참조 경계는 마련했지만 현재는 승인된 clean 문제은행 기본 템플릿만 지원하며, non-null custom 참조는 조용히 무시하지 않고 차단합니다.
- 선택한 프로젝트는 **프로젝트 ZIP 내보내기**로 canonical JSON과 참조 이미지·승인 profile/template만 manifest와 함께 백업할 수 있습니다. provider job/raw 응답과 임시 파일은 포함하지 않습니다.
- OCR provider processing line은 작업마다 선택합니다. `full`은 기존 Gemini/OpenAI vision REST adapter와 structured-output schema를 그대로 사용하고, `codex_only`는 로컬 `codex exec`만 사용해 immutable candidate job을 만듭니다. `codex_only` batch는 profile 상태를 작업 카드에 표시하며 Gemini·Qwen·OpenAI app-provider 호출이 0회이고, 그림 재생성은 거부/비활성화하고 원본 PDF를 보존합니다.
- candidate는 canonical `question.json`을 무조건 덮어쓰지 않습니다. 자동 batch와 사용자의 candidate 적용 모두 base revision을 확인한 뒤 CAS 저장합니다. 결과는 즉시 등록·출력 가능하고, 사용 중 문제가 발견되면 JSON 교정·검증·확인을 선택적으로 수행합니다. 일반 저장도 schema/의미 검증을 통과해야 기록됩니다.
- 일반 저장이나 candidate/그림 variant 적용 후 기존 승인은 무효화됩니다. 승인은 현재 revision과 approval digest를 결합하며, 미완료 coverage·열린 issue·미전사 해설 source·미승인 선택 그림·파일/SHA 불일치가 있으면 거부됩니다.
- vendored Exam Studio equation core를 통해 LaTeX→HWP script 변환을 호출할 수 있으며, 일부 과학 명령은 사람 검수 경고를 냅니다.
- 저장된 문항 화면의 **문제·해설 HWPX 만들기**를 한 번 누르면 `question-json-studio.native_ordered.v0.6` 엔진이 문제 파일과 해설 파일을 생성합니다. `paragraph`, `figure`, `bogi`, `choices`, `box`, `table`을 저장된 순서로 재귀 렌더링하고 native HWP 수식, 밑줄 문자 모양, RGB BMP 그림을 삽입합니다.
- HWPX build는 입력·profile·template·engine fingerprint를 사전 검사하고, 두 파일을 strict validation한 뒤 한 쌍으로만 게시합니다. 같은 입력은 결정론적 결과를 재사용하며 다운로드 URL은 build fingerprint와 파일 SHA에 결합됩니다. profile/template/engine 값이 `null`인 기본 프로젝트는 승인된 neutral 기본값을 자동 준비하고, 사용자가 지정한 custom 값은 덮어쓰지 않습니다.

HWPX 출력에는 solver, 정답 추론, AI 해설 생성, 별도 endnote package member, 기존 `resources/hwpx_base`를 사용하지 않습니다. native `endNote/endNotePr`은 combined 문제지에서만 strict contract에 따라 허용됩니다. `source_only`·`needs_review`·미승인 block은 승인 상태를 바꾸지 않고 출력하며 receipt에 경고를 남깁니다. canonical 문제를 안전하게 렌더링할 수 없으면 SHA가 맞는 ready 원본 문제 crop으로 문항 전체를 대체하고, 없는 답·풀이는 미주 라벨을 비워 둔 채 추론하지 않습니다. 안전하지 않거나 누락·변조된 실제 입력 파일과 유효하지 않은 계약만 build를 차단합니다.

## 설치와 실행

Python 3.11 이상을 권장합니다. 수식 bridge를 사용할 때는 Node.js가 필요하지만 별도 `npm install`은 필요하지 않습니다.

```powershell
cd C:\Question_JSON_Studio
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\start_app.ps1
```

앱은 `127.0.0.1:8875`에만 바인딩됩니다. 이 PC의 권장 Tailscale 주소는 `http://aidestkop:10001`이며 그대로 북마크할 수 있습니다. 해당 short Host는 Tailscale ACL을 통과한 동일 출처 요청만 허용합니다. 전송은 tailnet WireGuard 안에서만 이루어지며 인터넷 공개용 Funnel은 사용하지 않습니다. 긴 HTTPS token 주소 `:10000`은 호환용으로 함께 유지합니다.

다른 PC에서 같은 구성을 만들 때는 Tailscale 로그인 후 한 번만 실행합니다.

```powershell
tailscale serve --bg --yes --https=10000 http://127.0.0.1:8875
tailscale serve --bg --yes --http=10001 http://127.0.0.1:8875
.\stop_app.ps1
.\start_app.ps1
```

짧은 주소를 끄려면 `tailscale serve --http=10001 off`를 실행합니다. 앱 종료:

```powershell
.\stop_app.ps1
```

자세한 운영 절차와 provider 오류 처리는 [`docs/OPERATIONS.md`](docs/OPERATIONS.md)를 참고하세요.

## Gemini/OpenAI 설정과 호출 조건

키는 파일이나 workspace JSON에 넣지 말고, 앱을 시작하는 PowerShell 세션의 환경변수로 설정합니다. `.env.example`은 이름을 보여 주는 참고 파일일 뿐 앱이 자동으로 읽지 않습니다.

`start_app.ps1`은 Process 값이 없을 때 Windows 사용자 환경변수의 `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `OPENAI_API_KEY`를 값 출력 없이 상속합니다. 기존 Question_Crop에서 사용자 환경변수로 설정한 Gemini 키를 그대로 재사용할 수 있습니다.

```powershell
$env:GEMINI_API_KEY = "..."       # 우선 사용
# $env:GOOGLE_API_KEY = "..."     # Gemini fallback
$env:OPENAI_API_KEY = "..."
.\start_app.ps1
```

OCR quality profile과 provider processing line(`provider_profile`)은 별개입니다. quality profile은 `full`에서 사용할 OCR model을 고르고, provider processing line은 작업 전체의 호출 경로를 고릅니다. provider profile은 작업에 고정되며 전환하려면 새 작업을 만들어야 합니다. 별도 model override 환경변수는 없습니다.

| provider_profile | 처리 경로 |
|---|---|
| `full` (기본값/legacy) | 기존 Gemini/OpenAI OCR adapter와 현재의 해설 연결·그림 재생성 흐름 |
| `codex_only` | 로컬 `codex exec` OCR; profile 상태 표시; Gemini·Qwen·OpenAI app-provider 호출 0회; 그림 재생성 거부/비활성화; 원본 PDF 보존 |

| UI profile | provider/model |
|---|---|
| 균형 | `gemini-3.7-flash` |
| 저비용 | `gemini-3.5-flash-lite` |
| 고품질 검수 | `gpt-5.6-sol` |

`full` 자동 흐름은 crop `450 DPI`, 사용 가능한 최고 OCR quality profile, `gemini-3-pro-image` 2K 그림 후보를 내부 기본값으로 사용합니다. `codex_only`는 같은 OCR quality 선택과 관계없이 로컬 Codex 경로를 사용하고 그림 재생성을 하지 않습니다. 사용자가 기술 옵션을 고르지 않도록 UI에서 제거했으며 입력 이미지·프롬프트·thinking 토큰 비용과 실제 청구액은 provider 사용량에 따라 달라집니다.

표의 값은 앱의 현재 기본 설정입니다. 실제 model 접근 가능 여부와 비용은 provider 계정 설정에 따릅니다. 이미 실행 중인 서버에 새 키를 반영하려면 `stop_app.ps1`로 종료한 뒤 다시 시작합니다.

호출 안전 경계:

- `full` adapter 키가 없으면 provider 요청 전에 실패하므로 원격 호출은 0회입니다. `codex_only`는 API key 대신 로컬 Codex CLI 로그인 상태를 사용합니다.
- 키가 있어도 앱 열기, job 목록 조회, 기존 결과 import, 저장, 검증, 승인만으로는 crop engine이나 provider를 호출하지 않습니다.
- 일괄 흐름은 사용자가 **전체 자동 처리 시작**을 눌렀을 때만 `Question_Crop` engine과 선택 provider를 호출합니다. `full`에서만 그림 자동 재생성이 기본 ON이며, `codex_only`에서는 그림 재생성이 비활성화되고 요청도 거부됩니다. 단건 재처리는 **현재 탭 OCR**, **AI 재생성 실행**, **AI 자동 태깅**에서만 호출합니다. **전체 자동 태깅**은 아직 캐시되지 않은 문항만 순서대로 처리합니다.
- `full` 단건 OCR의 최초 cache miss는 선택한 provider에 HTTP POST 1회를 보내고 자동 retry하지 않습니다. `codex_only` cache miss는 로컬 `codex exec`로 처리하며 429/rate-limit/usage-limit에 한해 최대 3회 bounded retry합니다. 일괄 OCR도 각 대상 crop을 동일한 immutable job 규칙으로 처리합니다.
- OCR은 같은 이미지·prompt·schema·model·revision의 완료 job을 재사용합니다. 그림 cache는 같은 프로젝트의 같은 문항·block에 한정되므로 PDF부터 새 batch/프로젝트로 다시 실행하면 다시 과금될 수 있습니다.
- OCR 전송 내용은 문제/해설 source crop, transcription prompt, JSON schema입니다. `full` 그림 재생성은 추출 원본 또는 승인된 local crop과 원본 보존형 redraw prompt를 전송하며, `codex_only`는 그림 재생성을 하지 않습니다. solver·정답 추론 지시는 포함되지 않습니다.
- `full` HTTP adapter timeout은 120초이고, `codex_only`의 각 Codex CLI 시도 timeout은 600초입니다. API key는 저장하지 않습니다. provider raw 응답·prompt·schema·candidate는 감사 가능한 immutable `jobs/transcription/<job-id>/`에 저장되지만 canonical `question.json`에는 승인 적용 결과와 전사 provenance만 들어갑니다.

`OPENAI_API_KEY` 환경변수 사용 방식은 [OpenAI 공식 Developer quickstart](https://developers.openai.com/api/docs/quickstart)와 같습니다.

## 기본 작업 흐름

1. 문제 PDF와 해설 PDF를 브라우저에서 각각 선택합니다. 서버 PC에 이미 있는 파일은 고급 항목의 allowlist 경로를 사용할 수 있습니다.
2. 전체가 아니면 처리 범위만 입력하고 **전체 자동 처리 시작**을 누릅니다.
3. 이후 추가 클릭 없이 crop, 연결, import, 문제·해설 OCR, JSON 저장, 그리고 `full`일 때만 그림 후보 재생성이 순차 실행됩니다.
4. crop 파일이 만들어진 모든 문항은 저신뢰 여부와 무관하게 등록됩니다. OCR 실패 문항도 원본 crop으로 남습니다.
5. 바로 JSON/HWPX를 사용할 수 있습니다. 사용 중 문제가 보일 때만 문제/해설 원본과 OCR JSON을 교정하고 **검증 → 확인 완료**를 선택적으로 수행합니다.
6. OCR 재시도나 후보 비교가 필요하면 문항 화면의 **현재 탭 OCR**, **미전사 문제·해설 자동 OCR**, **후보 적용**을 사용합니다.
7. **전체 자동 태깅**을 누르면 과목·단원·난이도·유형·시각자료 태그가 추가됩니다. 목록 검색은 단원·유형·핵심어까지 포함하며 그림·그래프 문항만 걸러볼 수 있습니다.
8. 저장된 문항 화면에서 **문제·해설 HWPX 만들기**를 누르면 문제·해설 다운로드 버튼이 표시됩니다. 실제로 빠진 정답·해설이나 연결 확인 항목만 함께 안내합니다.

기존 leaf run을 직접 가져오는 기능은 상단의 **고급: 기존 결과 가져오기**에 남아 있습니다. 브라우저 PDF는 파일당 512MiB, workspace 전체 4GiB, 업로드 후 최소 여유 공간 256MiB 제한을 적용합니다. 완료되었거나 프로젝트까지 생성된 작업의 업로드 원본은 안전하게 회수하고, 프로젝트 생성 전 중단 작업은 7일간 재개할 수 있습니다.

## HWPX 생성

- 엔진 ID: `question-json-studio.native_ordered.v0.6`
- 계약 버전: `0.3.1`
- 계약 fingerprint: `4a99bf4a35a28d65188e66a2d40223f25ce1dfaf58b1439e0d9f0d833f23db96`
- API: `POST /api/projects/{id}/hwpx/build` with `{}`

Preflight는 workflow 값을 거짓 승인하지 않고 readiness를 경고로 기록하며, design profile과 clean template SHA-256, engine 계약, 실제로 렌더할 파일의 경로·SHA, 지원 구조를 검사합니다. 한컴에서 아직 검증되지 않은 transcription prompt가 box/table/equation native 구조를 만들면 canonical JSON은 보존하고 ready 원본 문제 crop으로 문항 전체를 호환성 격리합니다. 같은 `item_order`에서 해설은 저장된 renderable `content.answer + content.explanation`만 사용하며 없는 내용이나 미지원 부분은 추론하지 않고 생략합니다.

완성된 두 package는 구조·manifest·content count를 strict validation하고, 둘 중 하나라도 실패하면 새 산출물을 게시하지 않습니다. 문제의 미지원 구조는 ready 원본 문제 crop이 있으면 문항 단위 raster fallback을 사용하고, 미지원 answer/explanation 부분은 receipt 경고와 함께 생략합니다. 원본 crop마저 없거나 누락·변조된 경우만 차단합니다. 복합 선택지의 가로 배치는 세로 fallback을 사용합니다.

## 검증과 테스트

```powershell
.\.venv\Scripts\python scripts\validate_package.py examples\question-package.example.json
.\.venv\Scripts\python scripts\validate_project.py examples\project.example.json
.\.venv\Scripts\python -m unittest discover -s tests -v
```

실제 문항의 이미지 파일·SHA까지 확인:

```powershell
.\.venv\Scripts\python scripts\validate_package.py workspace\<project-id>\items\<item-id>\question.json --check-files
```

전체 자동 테스트는 334개를 실행해 성공했고 환경 의존 테스트 3개는 skip됐습니다. 격리된 B05 sample clone의 historical v0.2 build fingerprint는 `97221962ff63a8592a591240055875584c10d61874320115a0cf43d5e8ac23c8`입니다. 문제 파일은 82,202 bytes, SHA-256 `c3df1c40a3e34f7261393f93e951cf66c5fb9d1344afc856dc19d7f745f48abc`, 해설 파일은 11,264 bytes, SHA-256 `b6f04b5aef84e87cbd9964c0a5fdb2b69c577faa0c2b36cdeea1f1115b032f06`입니다. 번들 validator/inspector와 선택한 source-root validator가 모두 통과했고, 한컴 한글 2024에서 문제·해설 각 1쪽 direct-open, 그림·수식 7개·가로 선택지·쪽 번호를 확인했습니다. 한컴 보안 설정은 변경하지 않았습니다.

실제 원본 `project-65793b56-ce59-542c-a19b-3781ea06b521`의 누락 3문항 OCR까지 완료해 38/38문항에 문제 JSON이 저장됐습니다. all-canonical 문제 HWPX는 세 validator를 통과했지만 한컴에서 빈 문서 화면과 함께 응답 불가 상태가 됐습니다. 차이는 `transcribe-exam-crop-v3`의 B38/K06/K08 세 문항이었고, 수식·XML 단독 원인보다 새 native 구조 조합의 한컴 호환성 문제로 판정했습니다. v0.3.1은 OCR JSON을 보존한 채 이 세 문항만 ready 원본 crop으로 격리합니다.

이전 실제 build fingerprint `b2386e17327fc65c56410cb9b4bbda1f4b5b63e8386c02bbbd045e3633c978f9`는 해설 연결 보강 전 기준선입니다.

2026-08-25 해설 재판정 후 실제 프로젝트는 문제 OCR 38/38, 상세 해설 OCR 36/38, visible answer 36/38입니다. 연결 상태는 `자동 확정 23 / 저신뢰·검토 필요 13 / 연결 실패 2`이며 모든 비확정 문항에 5단계 시도가 저장됩니다. B38·B39는 상세 해설을 찾지 못했지만 빠른 정답 `⑤`에 대한 Codex+Gemini 98% 합의를 별도 근거로 보존합니다. 새 consensus crop 12개는 원본 PDF에서 450 DPI로 생성했고 기존 검증 crop 4개는 300 DPI 원본을 재사용했습니다. H19(1)·H20(1)은 target-aware OCR로 형제 `(2)` 답안·채점 기준을 제외하고 각 `(1)` 정답만 저장합니다.

현재 build fingerprint는 `0840bf799587c4bec0cf6b2d712039f5917c6147b7dca537db2d66792428c1de`입니다. 문제 HWPX는 11,940,628 bytes/SHA-256 `bb86ee541440fbb226de354083a0b107c484b6ae07593ee499f1393aa05d8962`, 해설 HWPX는 2,148,414 bytes/SHA-256 `9f305d79c4e6ea029fdf83bf0ed38480af9604debd7be59dbad71afda05d9e28`입니다. 두 파일은 앱 strict 검사와 HWPX 전용 validate/inspect를 통과했고 텍스트 재추출도 확인했습니다. 문제 파일은 한컴에서 직접 열었던 기존 결과와 동일 SHA입니다. 새 해설 파일의 한컴 시각 검증은 기존 한컴 세션 응답 불가 때문에 pending입니다.

## 주요 파일

- [`docs/ANALYSIS_AND_DESIGN.md`](docs/ANALYSIS_AND_DESIGN.md): 목표 아키텍처와 현재 구현 상태
- [`docs/QUESTION_BANK_PLAN.md`](docs/QUESTION_BANK_PLAN.md): 통합 문제은행, 문항 선택·순서 변경, 선택 HWPX 구현 계획
- [`docs/REAL_RUN_REPORT_2026-08-24.md`](docs/REAL_RUN_REPORT_2026-08-24.md): 실제 문제·해설 PDF 20쪽 end-to-end 실행 결과
- [`docs/SOURCE_MANIFEST.json`](docs/SOURCE_MANIFEST.json): upstream commit, adapted/vendored 파일, 라이선스 provenance
- [`schemas/question-package.schema.json`](schemas/question-package.schema.json): canonical 문항 계약
- [`schemas/project.schema.json`](schemas/project.schema.json): 프로젝트·출력 준비 계약
- [`schemas/design-profile.schema.json`](schemas/design-profile.schema.json): 승인 HWPX template/design 계약
- [`app/importer.py`](app/importer.py): 읽기 전용 leaf run import
- [`app/solution_reconciliation.py`](app/solution_reconciliation.py): 기존 증거를 끝까지 재집계하는 해설 후보 판정, 450 DPI 안전 재크롭, 상태·시도 근거 저장
- [`app/batch.py`](app/batch.py): PDF 쌍, 숨은 crop backend, 등록된 문제·해설 OCR을 잇는 persistent batch coordinator
- [`app/transcription.py`](app/transcription.py): provider adapter, immutable candidate, CAS apply
- [`app/tagging.py`](app/tagging.py): 규칙 우선 feature 판정, Gemini 구조화 태깅, 사용자 수정 보존
- [`app/question_bank.py`](app/question_bank.py): 전역 문항 조회와 문제지 ordered reference CAS 저장
- [`app/question_set_hwpx.py`](app/question_set_hwpx.py): 선택 문항 재번호, separate/combined HWPX build와 artifact 검증
- [`prompts/tag_exam_question.md`](prompts/tag_exam_question.md): 정답 생성 없이 닫힌 분류표만 사용하는 태깅 prompt
- [`app/figure_generation.py`](app/figure_generation.py): Gemini 그림 재생성, immutable job, 비용 없는 cache 재적용
- [`app/figure_recrop.py`](app/figure_recrop.py): 원본 보존형 figure bbox 재크롭
- [`app/review_gates.py`](app/review_gates.py): OCR·수식·자산·그림 승인 차단 규칙
- [`app/project_archive.py`](app/project_archive.py): 결정론적 JSON+이미지 ZIP과 무결성 manifest
- [`app/provider_http.py`](app/provider_http.py): bounded JSON POST transport
- [`app/model.py`](app/model.py): revision CAS와 승인 digest
- [`app/hwpx_export.py`](app/hwpx_export.py): HWPX engine 계약, optimistic readiness 경고·safety preflight와 두 출력 계획
- [`app/hwpx_native.py`](app/hwpx_native.py): native paragraph/equation/picture XML과 build-local ID
- [`app/hwpx_template.py`](app/hwpx_template.py): 승인된 neutral clean HWPX template의 결정론적 builder
- [`app/hwpx_renderer.py`](app/hwpx_renderer.py): 문제·해설 native ordered renderer와 atomic pair publication
- [`app/hwpx_validation.py`](app/hwpx_validation.py): package/content strict validation
- [`app/hwpx_artifacts.py`](app/hwpx_artifacts.py): neutral 기본 profile/template/engine 준비, content-bound build cache와 다운로드 artifact
- [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md): 제3자 출처와 배포 주의사항
