# Public source snapshot verification — 2026-10-04

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
