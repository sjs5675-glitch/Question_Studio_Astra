# Public source snapshot verification

## 2026-10-09 — 문항 생성 후속 기능 운영 반영

- 최신 운영 소스 기준으로 문항 코드·규칙 기반 중복 검사, 정확한 물리 과목의 max 정책, Claude 구독 소진 대체·저장·재개, 버전/그림 SHA에 결박된 사후 검토·재검토를 선택 병합했습니다. 오래된 개발 폴더 전체를 복사하지 않았습니다.
- 관리자 직접 실행: 문항 생성 관련 **81개 회귀 모두 통과**. 주변 문제은행·읽기·PDF 회귀 15개 중 14개 통과, 변경 전과 같은 `test_bank_scans_every_item_and_searches_summary`의 `answer_saved` 기대 실패 1개가 남았습니다. 새로운 회귀 실패는 없었습니다.
- 공개 clone에서도 운영 소스 경로를 PYTHONPATH에서 제외하고 새 시험 런타임을 지정해 동일 **81개 회귀를 직접 실행, 모두 통과**했습니다. 공개 대상 1,401개 파일 민감자료 검사에서 탐지 0건, Python 158개 AST 파싱 통과. 검사는 알려진 인증값 대조와 휴리스틱이며 모든 비밀의 부재를 보증하지 않습니다. 이번에 갱신한 동작 소스는 검증된 16개이며 기존 라이선스 고지는 그대로 유지했습니다.
- 실제 Edge 격리 화면에서 선택/모델/필터/교차 검토/재검토/이력 보존을 조작하고 데스크톱·모바일을 렌더링했습니다. 이 UI 시험은 가상 문항과 가짜 모델 runner를 사용합니다.
- 별도 실제 구독 CLI 시험에서는 합성 신규 1문항에 대해 **Claude 주간 한도 → Codex 대체 출제 → 정답을 숨긴 Codex 자체 검산 → 격리 문제은행 저장**이 성공했습니다. 이후 Claude 사후 검토는 실제 한도 대기로 멈췄고 canonical SHA가 유지됐습니다. 이 단건의 Codex CLI 보고 토큰 합계는 24,191이며 사전 연결 진단·개발 에이전트 사용량은 포함하지 않습니다.
- Codex 모델 식별은 신뢰할 수 있는 CLI 시작 기록으로 확인합니다. `cli_selected_model/effort`, `model_evidence=cli_startup_receipt`를 보존하며 제공자가 모델 메타데이터를 주지 않으면 `actual_model=null`을 유지합니다. 모델의 자기진술을 확인 증거로 쓰지 않습니다.
- 기존 그림의 SHA가 과거 기록에 없으면 사용자 검토 요청 시 현재 바이트를 새 스냅샷으로 동결합니다. 이미 기록된 SHA와 다르면 차단하며 과거 검토 이력을 위조하지 않습니다.
- 사용자가 승인한 운영 반영: 활성 배치·생성·해설 작업이 없는 것을 확인하고 소스 16개만 반영, owned Studio 재시작. 인증 health, 검증본과 운영 파일 SHA 일치, 기존 생성 자료 33개 파일 SHA 보존, 실제 운영 문항 생성 화면·API의 max/high 정책·기존 문항 코드·새 검토 UI를 확인했습니다. 운영 시험 문항 생성/저장은 하지 않았습니다.
- 브라우저의 실제 사용자 프로필은 잠겨 있어 닫거나 변경하지 않았습니다. 정식 런처의 인증을 별도 임시 Edge에서 사용했고 인증값은 증거·공개 소스에 포함하지 않았습니다.
- 제한: Claude는 실제 주간 한도 상태라 성공하는 실제 Claude 교차 검토는 미검증입니다. 가짜 runner로 성공·충돌·편집 중 버전 변경·중단/재개를 검증했습니다. 새 PC 전체 설치, 전체 앱 테스트, 실제 물리 문항 max 품질 비교, 한컴 직접 열기는 이번 검증 범위가 아닙니다.
- 변경 전 보존 태그: `backup/pre-itemgen-fallback-20261009`. 아래 기록은 해당 시점의 역사적 결과이며 이번 결과와 합산하지 않습니다.

## 2026-10-09 — 변경 전 소스 보존

- 운영 소스에서 허용된 소스·스키마·프롬프트·필수 번들을 별도 clone으로 복사했습니다. 운영 Git 이력을 업로드하지 않았습니다.
- 내보낸 파일 1,386개, Python AST 파싱 151개 통과. 공급자 키 형식·개인 키·인증 URL·알려진 현재 세션 인증값 대조에서 탐지 0건(휴리스틱 검사).
- 개인 이름과 개인 Tailscale 호스트는 예시 값으로 치환했습니다. 원본 자료·운영 문항·DB·로그·실행환경·작업 결과는 제외했습니다.
- 이 커밋은 기능 변경 전 복원 지점이며 이번 갱신에서 전체 회귀·실제 AI 호출·새 PC 설치를 검증한 것은 아닙니다. 아래 결과는 10월 4일 당시의 기록입니다.

## 2026-10-04 — 이전 검증 기록

현재 소스를 운영 작업 트리가 아닌 별도 Git clone에 복사하여 검증했습니다. 원본 PDF·운영 문제은행·학생 자료·운영 Git 이력·인증자료·실행 결과·보류 중인 UI 후보는 포함하지 않습니다.

## 실제 실행 결과

- Python 회귀 테스트 **35/35 PASS**:
  - `tests.test_homework_local`: 11
  - `tests.test_answer_only_transcription_state`: 10
  - `tests.test_question_bank`: 9
  - `tests.test_question_bank_server`: 5
- 최초 실행에서는 정답-only 테스트가 참조하는 `test_transcription` helper가 사본에 없어 1개 오류를 확인했습니다. 가상 자료 helper를 함께 포함하고 테스트 경로를 보완한 뒤 동일 35개 테스트가 모두 통과했습니다. 운영 전사나 실제 제공자 호출은 하지 않았습니다.
- Node `studio/web/scripts/document-answer-association.test.mjs`: **4/4 PASS**.
- `node --check`: `studio/web/app.js`, `bank-search-state.mjs`, `device-shell.mjs`, `exam-region.mjs` 모두 통과.
- 공개 사본의 `launch_astra.py --help` 실제 실행 성공.
- Git index에 올라간 Python 파일 **136개 AST 파싱 성공**.

## 격리 HTTP 검사

공개 사본의 `create_server()`로 새 임시 작업공간과 임의 loopback 포트를 사용했습니다. 검사 후 임시 서버를 정상 종료했습니다.

- 인증된 `/api/health`, `/api/bank/items`: HTTP 200 / JSON 파싱 성공.
- 미인증 `/api/bank/items`: HTTP 401. 인증을 해제하지 않았습니다.
- 아래 8개 응답은 HTTP 200이며 사본 파일 SHA-256과 모두 일치:
  - `index.html`, `app.js`, `styles.css`, `document.html`, `device-shell.mjs`
  - `dist/document/document.js`, `dist/document/manifest.json`
  - 실제 번들의 KaTeX AMS woff2 글꼴 1개
- 운영 API 쓰기 **0**, 운영 서버 재시작/변경 **0**.

## 공개 범위와 민감자료 검사

- 허용한 소스·스키마·프롬프트·가상 예제·필수 번들/글꼴만 내보냈습니다.
- `.runtime`, `.omo`, 가상환경, 실제 `.env`, DB, PDF/이미지 원본, 작업공간, 작업·전사 산출물과 로그의 Git index 포함을 검사했습니다.
- 공급자/GitHub/Google/Telegram 키 형식, 개인 키, 인증값 포함 URL, 개인 이름/Tailscale 호스트 및 **현재 운영 세션 토큰과의 실제 일치 여부**를 검사했습니다. 탐지된 민감자료는 없었습니다. 검사는 휴리스틱과 알려진 값 대조이며 모든 종류의 비밀정보가 없음을 수학적으로 보증하지 않습니다.
- 별도 검토에 표시된 `studio/tests/test_question_bank_server.py`의 상수는 가상 HTTP 테스트에서만 사용하는 `question-bank-server-token`입니다. 운영 인증값이 아닙니다.
- 원저작자 고지·MIT/OFL 등 각 구성요소의 라이선스 파일을 보존합니다. PNKmath는 게시자가 **공개 GitHub 재배포까지 원저작자 허가를 받았다고 확인**한 범위이며 전체 저장소를 임의로 MIT로 재라이선스하지 않습니다.

## 공개 사본 차이와 제한

- 개인 이름과 개인 Tailscale 호스트의 예시 값 치환, 공개 README/ignore/고지/검증 문서가 운영 사본과 다릅니다. 동작 코드를 임의로 재설계하지 않았습니다.
- 일부 원본 Markdown의 줄바꿈용 공백, vendored/bundled 소스와 기존 CSS의 공백으로 `git diff --check` 경고가 발생했습니다. 원본·라이선스·문자열 내용을 불필요하게 바꾸지 않기 위해 보존했습니다.
- 실제 브라우저의 화면 렌더링/상호작용, 실제 휴대폰, 원본 OCR, 한컴에서의 출력 열기, 새 PC에서의 전체 설치는 이번 공개 갱신에서 검증하지 않았습니다.
- 이 문서는 실행한 범위의 로컬 검증 기록입니다. GitHub Actions 통과나 서비스 배포 완료를 뜻하지 않습니다.
