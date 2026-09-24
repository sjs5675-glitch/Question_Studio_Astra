# 과학 문제집 문항 크롭 파이프라인

한국 중·고등 과학 문제집 PDF를 페이지 단위로 훑고, 여러 비전 AI가 문항 계층과 정규화 좌표를 독립적으로 판단하면 로컬 코드가 300 DPI 원본에서 문항 이미지를 결정론적으로 만드는 파이프라인입니다.

AI가 이미지를 직접 편집하지 않습니다. AI는 `문항 의미 + 관계 + bbox`만 판단하고, 렌더·회전·패딩·조각 결합·파일 저장·해시·검증은 로컬 코드가 담당합니다.

## 현재 범위

- 우선 대상: `C:\Users\서재성\Documents\과학 문제집\고등\2022 개정`
- 해당 폴더: PDF 77개, 7.12 GiB, 10,272쪽
- 전체 고등·중등: PDF 583개, 정상적으로 읽힌 567개/67,275쪽
- 이미지 전용·텍스트형·혼합형·암호화 플래그 PDF를 모두 PDFium으로 렌더
- `SUPER GWATAM`은 사용자 지시로 기본 필터와 골든셋에서 제외
- `OX & 빈칸 채우기`, `초성 Quiz` 같은 개념 확인 드릴은 제외
- `.hwpx`/`.hwp`는 현재 범위 밖이며 PDF만 처리

`1등급 만들기` 폴더의 PDF는 문제 원본이 아니라 `바른답 알찬풀이`뿐이라 해설 분류 음성 표본으로만 사용합니다.

## 가장 쉬운 사용법: 로컬 앱

이 프로젝트는 웹사이트처럼 보이지만, 실제로는 이 PC의 PDF·Codex CLI와 (full line에서) Gemini·Qwen API에 접근할 수 있으므로 **로컬 앱**으로 실행합니다. `codex_only` link run은 로컬 Codex 단일-provider 경로를 사용하고 Gemini·Qwen 호출을 하지 않습니다. 외부 서버에 문제집 전체를 업로드하지 않고, 분석에 필요한 페이지 이미지만 선택한 AI에 전달합니다.

최초 한 번만 설치합니다.

```powershell
cd C:\Question_Crop
uv sync
cd web
npm install
```

AI 로그인 상태를 확인합니다.

```powershell
codex login status
```

앱을 시작하고 중지하는 명령은 각각 하나입니다.

```powershell
cd C:\Question_Crop
.\start_app.ps1

# 사용을 마친 뒤
.\stop_app.ps1
```

브라우저에서 `http://localhost:3000`을 열고 문제 PDF를 놓거나, `고등\2022 개정` 같은 로컬 폴더 경로를 입력합니다. 페이지 입력은 `all`, `36`, `10-20`, `3,7,12-15` 형식을 지원합니다. 정답과해설 PDF는 처음부터 함께 선택하거나, 문제 crop 완료 후 결과 화면에서 따로 연결할 수 있습니다.

출력 해상도는 `기본 300 DPI`와 `고화질 450 DPI` 중 하나를 선택합니다. 450 DPI는 글자와 도표를 더 크게 저장하지만 처리 시간과 파일 크기도 증가합니다.

### Gemini 연결

Gemini는 Codex와 독립적으로 모든 대상 페이지를 분석합니다. Google AI Studio에서 키를 만든 뒤 프로젝트 루트에 `.env.local`을 만들고 다음 줄을 넣습니다.

```text
GEMINI_API_KEY=발급받은_키
```

### QwenCloud 연결

Qwen은 Codex와 Gemini가 불일치한 페이지만 `qwen3.7-plus`로 재판정합니다.

```text
DASHSCOPE_API_KEY=발급받은_키
QWEN_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
QWEN_MODEL=qwen3.7-plus
QWEN_MONTHLY_BUDGET_USD=100
```

`.env.example`을 복사해 시작할 수 있습니다. 키는 Git에 포함되지 않으며, 앱을 재시작해야 상태가 반영됩니다.

## 처리 방식과 여러 AI가 합의하는 방식

`provider_profile`은 OCR quality profile과 별개의 processing-line 선택자입니다. Studio 새 작업 또는 `solution_link.py link --provider-profile {full,codex_only}`로 고르며, 생략하면 `full`입니다.

- `full` (기본값/legacy)은 현재 Codex→Gemini 독립 제안, 불일치 시 Qwen 보강, 이중 검수 흐름을 유지합니다.
- `codex_only`는 해설 연결의 단일-provider Codex 경로만 사용합니다. 경계 consensus는 항상 `REVIEW`/`review_only`로 남고 자동 `verified`가 되지 않습니다. 수동 확인 후 Phase 3 fixed-edge 전파는 계속 허용됩니다.

provider별 raw proposal은 기존 `raw/<provider>/` 경로를 두 line이 안전하게 공유합니다. consensus, scope index, cache provenance는 profile별로 분리되며, codex-only 결과는 예를 들어 `solutions/detections/solution-blocks/consensus/codex_only/`와 `solutions/provider_profiles/codex_only/alignment_index.json` 아래에 기록되고 manifest provenance에 `provider_profile`과 해당 경로·SHA가 남습니다.

1. **Codex A**가 문항 번호·유형·공통자료·부모/소문항 관계와 bbox를 제안하고 실제 crop을 만듭니다.
2. **Gemini B**가 Codex 결과를 보지 않은 채 같은 페이지와 확대 타일을 독립 분석합니다.
3. 페이지 전체가 아니라 **문항별로** 번호·유형·공통자료·부모/소문항 관계를 맞춥니다. 한 문항의 차이는 같은 페이지의 정상 문항으로 전파하지 않습니다.
4. 관계가 같고 실제 영역이 의미 있게 겹치면 같은 내용으로 봅니다. 좌표 네 숫자는 일치할 필요가 없으며, 경계 차이는 진단에 남기고 실제 crop 검수가 PASS일 때만 승인합니다.
5. Codex와 Gemini 사이에 내용 합의가 없는 문항이 있을 때만 **Qwen C**를 호출합니다. 무관한 추가 검출 때문에 페이지 전체를 재판정하지 않습니다.
6. Gemini와 Qwen만 같고 Codex crop이 다르면 잘못된 이미지를 PASS하지 않고 `REVIEW`로 둡니다. 대체 좌표로 다시 crop하기 전에는 자동 승인하지 않습니다.
7. 마지막으로 실제 crop 이미지를 별도 비전 검수가 다시 확인합니다.

즉, 합의는 글자열만 비교하는 방식도, 좌표의 완전 일치를 요구하는 방식도 아닙니다. **문항의 의미 관계 + 실제 포함 영역**을 함께 비교합니다.

보조 AI가 문항을 놓쳤지만 반대 경계를 제안하지 않았고, 완성된 crop 자체가 고신뢰 PASS이면 이는 불일치가 아니라 `model_abstention` 주의로 남깁니다. 대상 문항과 무관한 작은 장식·불완전한 주변 조각 역시 crop을 REVIEW로 막지 않고 `cosmetic_fragment` 주의로만 표시합니다.

## 처리 흐름

```text
PDF 감사/필터
  → 8쪽 접촉시트 triage + 시각적 회전 판정
  → 문제 페이지 전체 이미지 + 겹침 2×2 확대 타일
  → Codex 문항 계층/bbox JSON
  → 300 DPI 로컬 crop + 공통자료/부모문맥 합성
  → 픽셀 경계·열 경계·계층 중첩 검사
  → 고난도 bbox 교정 패스
  → 독립 VLM 완결성 검수
  → (full) Gemini 독립 경계 제안
  → (full) Codex×Gemini 합의, 불일치 페이지만 Qwen 재판정
  → manifest + review queue
```

전체 페이지는 한 장당 200 DPI overview와 네 개의 겹침 확대 타일로 봅니다. Codex CLI 이미지 입력은 내부적으로 축소될 수 있으므로, 전체 300 DPI 페이지만 한 장 보내는 것보다 타일을 함께 보내는 편이 작은 글자와 선택지 경계에 안정적입니다.

## 문항 모델

단일 사각형만으로는 다음 사례를 처리할 수 없어 `parts[]` 관계형 구조를 사용합니다.

- 독립 객관식·단답형·서술형
- `[01~02]` 공통자료와 여러 자문항
- 대문항 아래 `(1)`, `(2)` 소문항
- 한 문항이 열을 넘어 이어지는 비연속 fragment
- 이전/다음 PDF 페이지의 자료나 continuation
- 문제별 그림·표·보기·답안란·힌트 사이드카

`figure_or_table`은 그림의 선·도형만 뜻하지 않습니다. 그림 내부의 문자·화살표·축·단위·범례와, 패널에 시각적으로 붙은 `(가)`, `(나)`, `(A)`, `(B)` 식별표까지 하나의 시각 단위로 봅니다. 떨어진 패널은 각각의 식별표를 포함한 여러 개의 tight bbox로 저장하며, 본문 속 "그림 (가)는" 문장이나 별도 답을 요구하는 `(가)/(나)` 소문항은 그림 캡션으로 취급하지 않습니다.

출력 단위는 다음과 같습니다.

- `atomic` 기본: 각 소문항에 부모 stem과 공통자료를 붙여 독립적으로 풀 수 있게 생성
- `main`: 대문항과 모든 소문항을 한 이미지로 생성
- `both`: 위 두 종류 모두 생성
- `--emit-groups`: 공통자료 묶음 전체 이미지도 추가

기본은 답안 공간과 힌트 사이드카를 제외합니다. 워크시트 원형이 필요하면 `--answer-space include --sidecars include`를 명시합니다.

## 설치

Python 3.11 이상과 로그인된 Codex CLI가 필요합니다.

```powershell
cd C:\Question_Crop
uv sync
codex login status
```

의존성은 두 개뿐입니다.

- `pypdfium2`: PDF 렌더링
- `Pillow`: 타일·크롭·합성·픽셀 QA

## 안전한 시작 순서

### 1. 코퍼스 감사

모델 호출 없이 PDF 개수, 페이지 수, 텍스트 레이어, 오류 파일을 기록합니다.

```powershell
uv run python question_crop.py audit `
  "C:\Users\서재성\Documents\과학 문제집\고등\2022 개정" `
  --out output\audit_high_2022.json `
  --workers 4
```

### 2. 한 PDF의 지정 페이지만 파일럿

```powershell
uv run python question_crop.py run `
  "C:\Users\서재성\Documents\과학 문제집\고등\2022 개정\오투 통합과학1 (25년)\오투 통합과학1 본문 (학생용).pdf" `
  --out runs\pilot `
  --pages 36 `
  --triage always `
  --model gpt-5.6-sol `
  --workers 1 `
  --unit both `
  --verify all
```

### 3. 층화 골든 페이지 회귀

`benchmarks/high22_stress.json`에는 오투·셀파·엔픽·BON·마더텅·완자·기출픽·하이탑의 공통자료, 소문항, 스캔 왜곡, 회전, 해설 오탐 표본이 들어 있습니다. SUPER GWATAM은 포함하지 않습니다.

처음에는 2건만 실행합니다.

```powershell
uv run python question_crop.py benchmark benchmarks\high22_stress.json `
  --out runs\high22_regression `
  --max-cases 2 `
  --model gpt-5.6-sol `
  --workers 2 `
  --verify all
```

특정 사례만 실행할 수도 있습니다.

```powershell
uv run python question_crop.py benchmark benchmarks\high22_stress.json `
  --case-regex "bon1|hightop1" `
  --out runs\targeted_regression `
  --model gpt-5.6-sol
```

`question_crop.py benchmark`에는 provider profile 옵션이 없습니다. 해설 연결 CLI의 선택 문법은 `solution_link.py link --provider-profile {full,codex_only}`이며 `full`이 기존 동작을 보존하는 기본값입니다. `codex_only`를 선택해도 provider raw cache는 기존 경로를 공유하고, consensus/scope/cache provenance만 full과 분리됩니다.

### 4. 디렉터리 전체 실행

```powershell
uv run python question_crop.py run `
  "C:\Users\서재성\Documents\과학 문제집\고등\2022 개정" `
  --out runs\high22 `
  --model gpt-5.6-sol `
  --workers 2 `
  --unit atomic `
  --exclude-regex "SUPER\s*GWATAM"
```

디렉터리 처리의 기본 파일명 제외 정규식은 비어 있습니다. `정답표시없음`처럼 문제 PDF인데 파일명에 `정답`이 들어간 사례를 놓치지 않기 위해서입니다. 문제/해설 여부는 페이지 이미지를 보는 triage가 판단합니다. `SUPER GWATAM`만 현재 사용자 코퍼스의 실행 옵션과 로컬 앱에서 명시적으로 제외합니다.

파일명이 불명확한 합본은 접촉시트 triage가 페이지별로 다시 거릅니다. 처음부터 전체를 돌리지 말고 `--max-docs 2 --max-pages-per-doc 16`으로 호출량을 확인하는 것을 권장합니다.

## 주요 옵션

```text
--triage auto|always|never       문제/개념/해설/드릴 선분류
--tiles quadrants|none           겹침 확대 타일
--context-pages auto|0|1         인접 페이지 자료/continuation
--unit atomic|main|both          최종 문항 단위
--answer-space exclude|include   답안선·답안상자
--sidecars exclude|include       Tip/Key Word 패널
--crop-dpi 300|450              문제 crop 출력 해상도
--repair-attempts 1              경계 실패 고난도 교정
--verify all|review|none         독립 VLM 최종 검수
--solution-mask none|white       실험적 정답 마스킹
```

`solution-mask=white`는 실험 옵션입니다. 인라인 정답 bbox가 본문을 잘못 가리킬 수 있으므로 기본값은 `none`입니다. 문제와 정답이 같은 페이지는 `problem_with_inline_solution`로 격리하고 VLM 검수에 통과한 crop만 승인합니다. 가능하면 학생용 무정답 PDF를 우선합니다.

문제 PDF에서는 선택지의 색·폰트·인쇄 농도 차이만으로 정답 노출을 판정하지 않습니다. 명시적인 체크·동그라미·선택 표시, `정답` 라벨, 풀이·해설 또는 채점 근거가 실제로 보일 때만 정답 증거로 처리합니다.

완료 후 특정 페이지만 다시 교정할 때는 `question_crop.py repair --run-dir <leaf-run> --pages 20,34`처럼 지정할 수 있습니다. 선택 교정은 실패한 owner 문항만 교체하고 이미 PASS한 같은 페이지의 형제 문항 좌표는 보존하며, 기존 repair 기록 다음 번호로 이력을 남깁니다.

### 빠른 답·상세 해설 연결

해설 연결은 기존 문제 crop과 ensemble 결과를 다시 만들지 않습니다. 기본 입력은 `문제 PDF 1개 + 정답과해설 PDF 1개`이며, 정답과해설 PDF 안의 빠른 답과 상세 풀이를 서로 다른 asset으로 색인해 별도 sidecar로 연결합니다. 별도 빠른정답 PDF는 필수가 아닙니다.

```powershell
.\.venv\Scripts\python.exe solution_link.py link `
  --run-dir "runs\<leaf-run>" `
  --solution "정답과해설.pdf" `
  --crop-dpi 300 `
  --workers 2 `
  --model gpt-5.6-sol
```

`정답친해 20쪽` 같은 단일 표시는 20쪽 한 장으로 단정하지 않고 다음 anchor 전까지의 시작점으로 해석합니다. 선택한 문제 범위에 다음 anchor가 없을 때는 우선 20~23쪽의 제한된 후보 창만 확인하며, 번호·단원/코너·소문항·순서와 문항 이미지를 다시 대조합니다. 상세 해설 시작점보다 앞에 빠른정답 표가 몰려 있는 형식은 같은 page-map segment의 앞 4쪽까지 별도 탐색합니다. 001~101처럼 조밀한 정답표는 대상 번호만 다시 위치 지정해 개별 bbox를 만듭니다. 해설 경계는 Codex와 Gemini가 독립 제안하고 의미 단위로 합의하며, 불일치할 때 Qwen을 보강 워커로 사용합니다. 최종 answer/detail PNG는 저장될 300/450 DPI 이미지 자체를 Gemini와 Qwen이 모두 검사합니다. 번호·실제 답·첫 줄·마지막 줄·이웃/다음 문항 부재 중 하나라도 충족하지 못하면 `REVIEW`이며, 두 AI가 문항에는 동의하고 경계만 실패한 항목만 표적 재크롭합니다.

해설 PDF 앞부분 1~8쪽에 전권 공용 `빠른 정답/정답표`가 있는 형식도 별도 허용 구간으로 탐색합니다. 이 구간은 표지가 실제로 보이고 같은 page-map segment이며 단원·번호·순서가 유일하게 맞을 때만 허용하고, 상세 anchor 직전 4쪽 범위와 하나의 넓은 구간으로 합치지 않습니다. `A13`처럼 문제 번호에 단원 접두가 붙고 해설에는 `A 단원 + 13`으로 나뉘어 적힌 경우에는 보이는 numbering scope가 접두와 일치할 때만 `13`으로 정규화합니다. 명시 범위 `4~7p`는 시작뿐 아니라 끝쪽도 확인하고 두 끝의 물리/인쇄 오프셋이 일치할 때 중간 page-map을 채웁니다.

개념 설명 페이지와 빈칸·O/X 개념체크는 학생이 풀 독립 문항 crop 대상에서 제외할 수 있습니다. 실제 문제와 같은 실행 범위에 섞여 있어도 페이지별 의미 판정 후 문제만 남깁니다. 최종 VLM이 잘린 crop을 발견하면 다음 교정은 clean page·기존 overlay와 함께 실패한 최종 PNG 및 실제 열 경계를 보고 실패 owner만 다시 잡습니다. footer나 `정답 및 해설 n~mp` 영역은 검출된 ignored footer보다 위에서 crop 확장을 멈춥니다.

앱에서는 문제 crop의 `PASS/REVIEW/FAIL`과 해설 연결의 `verified/review/unmatched/conflict`를 별도로 표시합니다. 자동 확정 근거가 부족한 `review/conflict`는 문제·답 후보·해설 후보를 함께 본 뒤 사용자가 수동 확정할 수 있습니다. 새 manifest는 모든 검토 후보를 실제 `review_only` crop으로 저장하며, 이 후보는 수동 확인 전에는 자동 확정 자산으로 사용할 수 없습니다. 해설 연결이 실패해도 완료된 문제 crop과 판정은 그대로 유지됩니다. 같은 PDF·DPI·AI 계약은 캐시하지만, 독립 검증이 누락된 REVIEW는 일시적인 API 오류가 굳지 않도록 다음 실행에서 자동 재시도합니다. 검증 응답이 schema 또는 자체 의미와 모순되면 같은 공급자에게 한 번만 교정을 요청하고 성공·실패 결과를 모두 캐시합니다. 300/450 DPI와 해설 PDF SHA는 출력 파일명과 provenance에 포함되어 서로 잘못 재사용되지 않습니다.

## 결과 구조

```text
runs/<book_id>/
  triage.json
  pages/                 # overview와 확대 타일
  detections/            # 페이지별 구조화 JSON
    history/             # 교정 전 JSON
    repair/              # 교정 결과
  overlays/              # 사람이 확인할 bbox 오버레이
  crops/                 # 최종 PNG/JPG
  verification/          # crop별 독립 VLM 판정
  solutions/             # 빠른 답·상세 해설 연결 sidecar와 crop
    link_manifest.json   # 문항별 연결 상태와 등록 asset
    page_map.json        # PDF index·PDF page·인쇄쪽 대응
    crops/               # 빠른 답·상세 해설 이미지
  logs/                  # Codex JSONL 이벤트와 stderr
  manifest.json          # 원본 해시, 좌표, 계층, 출력 해시
  review_queue.json      # 자동 승인 불가 항목
  run_state.json         # 재개 상태와 회전 보정
```

`manifest.json`의 페이지 번호는 모두 1-based입니다. 검출 JSON의 `page_offset`만 현재 페이지 기준 `-1/0/+1`입니다.

같은 원본·프롬프트·스키마·모델 설정의 성공 JSON은 캐시되어 재실행 시 건너뜁니다. 교정 전 결과도 `detections/history`에 보존됩니다.

## 품질 게이트

모델의 자체 confidence는 승인 기준으로 쓰지 않습니다. 실제 파일럿에서 confidence 0.99인데도 긴 선택지 하단이나 우측 문장이 잘린 사례가 있었습니다.

최종 승인은 다음을 모두 만족해야 합니다.

1. JSON Schema와 좌표 범위 검증 통과
2. 다른 문항 bbox와 과도한 겹침 없음
3. 부모 본문과 형제 소문항 영역 분리
4. 렌더 잉크가 crop 경계에 닿지 않거나 독립 VLM이 완전함을 확인
5. 공통자료·그림·표·보기·①~⑤ 선택지 포함
6. 다른 문항·정답·해설 유입 없음
7. `review_queue.json`이 비어 있음

현재 대표 파일럿 결과입니다. BON·하이탑·완자·마더텅·오투는 최신 결정론적 crop 단계로 다시 만든 22개 이미지를 사람이 시각 점검해 22/22 통과했고, 셀파 앱 E2E 표본은 최신 crop을 별도 VLM이 다시 PASS했습니다.

- 오투1 p36: 공통자료 포함 6/6 통과
- BON1 p27: 좌우 독립 공통자료 4/4 통과
- 마더텅2 p240: 페이지 전체 대형 문항 교정 후 1/1 통과
- 하이탑1 p254: 대문항 2 + atomic 소문항 4, 6/6 통과
- 완자2 스캔 p169: `[07~08]` 포함 5/5 통과
- 셀파2 p36: 스캔형 단일 문항 1/1 통과
- 엔픽 p60: 숨은 180° 회전과 빈칸/OX 드릴 제외 성공, 인라인 정답 때문에 자동 승인 보류
- 1등급 만들기 p30: solution으로 분류, 상세 검출 0건

이는 대표 페이지 검증 결과이지 전체 67,275쪽의 정확도 보장은 아닙니다. 실제 전체 실행에서는 두 AI 합의가 없거나 경계 검사가 남은 항목을 자동 PASS하지 않고 검토 큐에 둡니다. 전체 투입 전 골든셋을 계속 늘리고 문항 recall·정답 유입률을 수치화하는 것이 안전합니다.

## 워커 운영 권장

- triage: 8쪽 접촉시트, `low`
- 상세 bbox: 페이지별 독립 `codex exec`, `medium`
- 경계 교정: 실패 페이지만 `high`
- 최종 crop 검수: 문항별 독립 이미지, `medium`
- 동시 워커: 처음 2개, 429/지연을 확인한 뒤 최대 4개
- 페이지 배치는 Codex 내부 멀티에이전트보다 외부 독립 프로세스가 캐시·재시도·비용 추적에 유리

CLI는 `--ignore-user-config`, `--sandbox read-only`, `--ephemeral`, `--output-schema`, `--output-last-message`를 사용합니다. 이미지 경로가 prompt를 삼키지 않도록 stdin prompt를 첫 positional로 둡니다.

## 채택·보류한 오픈소스

- 채택: [OpenAI Codex](https://github.com/openai/codex) - 이미지 입력과 구조화 출력 워커
- 채택: [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) - 가벼운 PDFium 렌더러
- 조건부: [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) - 오프라인 OCR/사전 라벨이 필요해질 때만
- 초기 제외: [DocLayout-YOLO](https://github.com/opendatalab/DocLayout-YOLO) - 문제 계층 특화가 아니고 PyTorch/가중치 및 AGPL 부담이 큼

Codex 비대화식 구조화 출력은 [OpenAI 공식 문서](https://learn.chatgpt.com/docs/non-interactive-mode)를 기준으로 구성했습니다.

## 핵심 파일

| 파일 | 쉽게 말하면 | 역할 |
|---|---|---|
| `start_app.ps1` | 앱 켜기 | 로컬 API와 웹 화면을 숨은 프로세스로 시작하고 브라우저를 엽니다. |
| `stop_app.ps1` | 앱 끄기 | 이 프로젝트가 시작한 Python/웹 프로세스를 중지합니다. |
| `app_server.py` | 화면과 엔진 사이의 접수창구 | PDF 업로드, 작업 대기열, 진행 로그, 결과 이미지 제공, CLI 실행을 담당합니다. |
| `web/app/page.tsx` | 사용자가 누르는 화면 | 파일 선택, 옵션, 진행률, PASS/REVIEW/FAIL 필터, crop 확대를 구현합니다. |
| `web/app/globals.css` | 화면 디자인 | 데스크톱·모바일 레이아웃, 색상, 카드, 진행 상태를 담당합니다. |
| `web/app/layout.tsx` | 앱의 표지 | 한국어 문서 설정, 제목과 설명을 지정합니다. |
| `question_crop.py` | 실제 절단 엔진 | PDF 렌더, 회전, 타일, Codex 검출, 좌표 교정, 300 DPI crop, 검수를 수행합니다. |
| `ensemble.py` | 합의 심판 | Codex와 Gemini 제안을 비교하고, 불일치할 때만 Qwen을 호출합니다. |
| `solution_link.py` | 답·해설 연결 엔진 | 문제 귀퉁이의 해설 쪽수와 번호 범위를 읽고 빠른 답·상세 해설을 별도 crop으로 연결합니다. |
| `solution_runtime.py` | 실제 해설 분석 워커 | PDF 쪽수 지도, Codex+Gemini 독립 경계 합의, Qwen 보강, 실패 문항 표적 재크롭, Gemini+Qwen 최종 PNG 이중검수와 300/450 DPI 렌더를 수행합니다. |
| `prompts/triage_pages.md` | 페이지 분류 지시서 | 문제·개념·해설·제외 드릴과 회전을 구분합니다. |
| `prompts/detect_questions.md` | Codex 경계 지시서 | 공통자료, 대문항, 소문항, 그림·표·보기의 의미와 bbox를 찾게 합니다. |
| `prompts/propose_boundaries.md` | Gemini/Qwen 독립 지시서 | 다른 모델의 답을 보지 않고 같은 구조를 다시 제안하게 합니다. |
| `prompts/verify_crop.md` | 최종 품질 지시서 | 잘림, 다른 문항·정답 혼입, 필수 자료 누락을 검사합니다. |
| `*.schema.json` | AI 답안 양식 | 모델이 항상 파싱 가능한 JSON으로 답하도록 강제합니다. |
| `benchmarks/high22_stress.json` | 어려운 실전 시험지 목록 | 출판사별 규칙이 아니라 다양한 레이아웃 회귀 표본을 보관합니다. |
| `tests/` | 자동 안전검사 | 좌표 합의, 공통자료/소문항, 페이지 번호, crop 조합을 빠르게 검사합니다. |
| `.env.example` | 비밀키 견본 | 실제 키 없이 Gemini·Qwen 환경변수 이름만 보여줍니다. |
