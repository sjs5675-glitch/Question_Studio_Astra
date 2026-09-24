# 문제-답·해설 연결 패치 계획

## 목표

기존 문제 crop 결과를 유지하면서, 사용자가 지정한 `정답과해설 PDF` 한 개를 색인해 각 문제에 인쇄된 답과 상세 해설 이미지를 정확하게 연결한다. 자동 연결 근거가 부족하면 잘못 연결하지 않고 별도 검토 상태로 남긴다.

## 기본 범위

- 입력 기본형: 문제 PDF 1개 + 정답과해설 PDF 1개
- PDF만 처리한다.
- 별도 빠른정답 PDF는 초기 범위에서 제외한다.
- 정답과해설 PDF 내부의 빠른 정답 영역과 상세 해설 영역은 서로 다른 role로 색인한다.
- 기존 `manifest.json`, crop 이미지, ensemble 결과를 다시 만들지 않는다.
- 연결 결과는 `run_dir/solutions/link_manifest.json` sidecar에 저장한다.
- 문제 crop 상태와 해설 연결 상태를 분리한다.
- 원본 문제·해설 PDF는 수정하지 않는다.

## 절대 규칙

1. 표시 문제 번호 하나만으로 자동 연결하지 않는다.
2. 문제 귀퉁이의 `해설 20쪽`은 기본적으로 단일 페이지가 아니라 해설 탐색 시작점이다.
3. 단일 쪽 안내의 종료점은 다음 문제 범위·다음 단원·다음 numbering scope가 시작되는 경계로 결정한다.
4. `pdf_index`(0-based), `pdf_page`(1-based), `printed_page`(책 인쇄쪽)를 별도 필드로 저장한다.
5. 표지·삽지·부록·쪽수 재시작 때문에 전역 page offset 하나를 가정하지 않는다.
6. 문제 번호가 1번으로 돌아오면 단원·코너·출현 순서를 포함한 새 numbering scope를 만든다.
7. 빠른 답과 상세 해설은 별도 asset으로 연결한다. 상세 해설에 답이 반복되는 것은 정상이다.
8. 답과 상세 해설 양쪽의 명시 답이 충돌하면 어느 한쪽도 자동 확정하지 않고 `conflict`로 둔다.
9. 자동 연결은 유일 후보 + 범위 일치 + 번호/소문항 일치 + 단조 순서 + 독립 검증을 모두 통과해야 한다.
10. 불확실한 연결은 `review`, 후보 없음은 `unmatched`, 충돌은 `conflict`로 기록한다.
11. 모든 문제는 `verified|review|unmatched|conflict` 중 정확히 하나의 연결 상태를 가진다.
12. 해설 연결 실패가 문제 crop의 PASS/REVIEW/FAIL을 변경하지 않는다.

## 해설 범위 모델

```json
{
  "raw": "정답과 해설 20쪽",
  "mode": "start",
  "printed_start": 20,
  "printed_end": null,
  "bbox": [760, 20, 980, 85],
  "scope": "page_or_section",
  "confidence": 0.98
}
```

- 동일 시작 안내가 연속 문제 페이지에 반복되면 하나의 anchor scope로 합친다.
- 안내가 한 번만 나타나면 다음 anchor·단원·코너 경계 전까지 같은 scope에 전파한다.
- 해설 인쇄 20쪽에서 시작해 21~23쪽으로 이어지는 경우, 24쪽에서 다음 범위가 시작하면 20~23쪽을 후보 구간으로 삼는다.
- 후보 구간 안에서도 각 문제에는 자신의 answer/detail block만 연결한다.
- 해설 하나가 페이지를 넘으면 여러 `parts[]`를 가진 단일 asset으로 합친다.

## 문항 연결 키

```text
book/run identity
+ solution printed-page anchor
+ chapter/section/corner path
+ normalized parent number
+ subquestion label
+ numbering-scope occurrence
+ monotonic local order
+ stem/figure-label fingerprint
```

## 저장 구조

```text
<leaf-run>/solutions/
  link_manifest.json
  page_map.json
  detections/
  crops/
    answer-*.png
    explanation-*.png
  logs/
```

`link_manifest.json` 최소 구조:

```json
{
  "schema_version": 1,
  "problem": {"source_sha256": "...", "page_map": []},
  "solution": {"source_sha256": "...", "page_map": []},
  "assets": {},
  "items": {
    "p0010-q2": {
      "status": "verified",
      "answer_ids": ["answer-001"],
      "explanation_ids": ["explanation-001"],
      "solution_printed_pages": [20, 21],
      "confidence": 0.97,
      "evidence": [],
      "review_reasons": []
    }
  }
}
```

## 처리 흐름

```text
문제 run
  -> 문제 인쇄쪽·귀퉁이 해설 시작점 추출
  -> numbering scope 구성

정답과해설 PDF
  -> 인쇄쪽수 <-> PDF index 구간 지도
  -> 페이지/블록을 quick_answer | detailed_solution | mixed로 색인
  -> 문항별 answer/detail bbox와 continuation 추출

연결
  -> anchor 범위 hard gate
  -> 단원/번호/소문항 후보 제한
  -> 구간 전체 단조 순서 정렬
  -> stem/그림 라벨 fingerprint 확인
  -> Codex 제안 + Gemini 검증, 불일치만 Qwen
  -> verified/review/unmatched/conflict 저장
```

## DPI 정책

- 비전 overview: 200 DPI + 기존 확대 타일 유지
- 기본 문제·해설 출력: 300 DPI
- 고화질 문제·해설 출력: 450 DPI
- 600 DPI는 초기 범위에서 제외한다.
- 스캔 PDF는 원본 유효 해상도 이상으로 확대해도 새 정보가 생기지 않으므로 300 DPI를 기본 유지한다.
- 앱 옵션은 `기본 300 DPI`와 `고화질 450 DPI` 두 개만 제공한다.

## 고2 선택과목 파일럿

현재 실물 회귀 범위는 학생용 문제 PDF와 별도 정답과해설 PDF 쌍으로만 제한한다.

### A. 셀파 물리학 - 이미지형 스캔

- 문제: `고2 선택과목/셀파/물리학/셀파 과탐_(22개정)물리학_문제편(학생용).pdf`
- 해설: `고2 선택과목/셀파/물리학/셀파 과탐_(22개정)물리학_정답과해설.pdf`
- 검증 내용:
  - 양쪽 PDF 모두 텍스트 레이어가 없는 스캔
  - 쪽수·범위·문항 번호를 비전으로만 연결

### B. 메가 N제 - 단원별 문제 조각 + 전체 해설

- 문제: `고2 선택과목/메가 N제/*단원 문제만*.pdf`
- 해설: `고2 선택과목/메가 N제/메가 N제_물리학 576제_해설.pdf`
- 검증 내용:
  - 잘라낸 문제 PDF의 PDF index와 원래 인쇄쪽수 불일치
  - 단원별 번호 초기화와 하나의 전체 해설 PDF 공유

## 구현 단계

### 0. 계획·현 상태 고정

- [x] 본 계획 문서 생성
- [x] 현재 테스트·앱 상태 기준선 기록
- [x] 고2 선택과목 PDF pair 목록을 테스트 fixture 메타데이터로 정리

### 1. 순수 연결 코어

- [x] `solution_link.py` 생성
- [x] page label/range 정규화 함수
- [x] numbering scope 생성 함수
- [x] start anchor를 다음 경계까지 확장하는 함수
- [x] quick/detail role 분리 모델
- [x] 단조 sequence alignment 및 candidate 결정 함수
- [x] sidecar read/write 및 cache key
- [x] 순수 함수 단위 테스트

### 2. AI 검출 계약

- [x] 문제 귀퉁이 anchor 검출 prompt/schema
- [x] 해설 인쇄쪽·범위·블록 검출 prompt/schema
- [x] 문제-해설 동일성 검증 prompt/schema
- [x] 텍스트 레이어 우선, 이미지형만 VLM fallback
- [x] Codex/Gemini/Qwen 결과 검증 및 fail-closed 처리

### 3. 해설 렌더·crop

- [x] 정답과해설 PDF page map 생성
- [x] answer/detail block crop
- [x] 다중 페이지 explanation parts 합성
- [x] 300/450 DPI 출력
- [x] asset SHA와 provenance 저장

### 4. 로컬 API

- [x] 앱 작업 옵션에 `solution_source`와 `crop_dpi` 추가
- [x] `POST /api/jobs/{id}/solutions`
- [x] `GET /api/jobs/{id}/solution-asset?id=...`
- [x] 기존 `/results` 응답에 verified/review 연결 병합
- [x] 해설 처리 상태를 문제 작업 상태와 별도 관리
- [x] 경로 이탈 차단 및 cache 재사용 테스트

### 5. 웹 UI

- [x] `정답과해설 PDF` 입력 1개
- [x] `기본 300 DPI / 고화질 450 DPI` 선택
- [x] 완료 작업의 `해설 PDF 연결` 버튼
- [x] 문제 카드의 `답·해설 보기` 버튼
- [x] 데스크톱 문제/답/해설 동시 보기
- [x] 모바일 문제/답/해설 탭
- [x] 연결 REVIEW 후보 선택·수동 확정
- [x] 연결 상태가 문제 crop verdict와 섞이지 않도록 표시

### 6. 파일럿·회귀

- [x] 실물 회귀 입력을 학생용 문제 PDF + 별도 정답과해설 PDF로 제한
- [x] 단일 시작 20쪽 -> 실제 20~23쪽 연속 구간 fixture
- [x] 번호 연속·번호 초기화 fixture
- [x] 동일 번호가 여러 단원에 반복되는 fixture
- [x] 대문항/소문항 fixture
- [x] 빠른 답과 상세 해설의 답 충돌 fixture
- [x] 셀파 스캔형 표본
- [x] 메가 N제 단원 조각 표본
- [x] 전체 Python 테스트
- [x] TypeScript/lint/build
- [x] 앱 E2E 시각 검증

## 자동 연결 승인 조건

- candidate가 anchor 시작 구간과 같은 page-map segment에 있음
- hierarchy/numbering scope 일치
- parent number와 sublabel 일치
- section 안에서 순서 역전 없음
- stem 또는 핵심 도형 라벨 fingerprint 일치
- 후보가 유일하고 runner-up과 충분히 분리됨
- 독립 검증 모델이 동일 문항으로 판정
- quick/detail 양쪽에 명시 답이 있으면 정규화 결과 일치

위 조건 중 하나라도 깨지면 자동 연결하지 않는다.

## 완료 기준

- 문제와 답·해설을 한 화면에서 함께 볼 수 있다.
- 문제 번호가 반복돼도 다른 단원 해설이 연결되지 않는다.
- `해설 20쪽` 하나로 실제 20~23쪽에 퍼진 해설을 연결할 수 있다.
- 정답과 상세 해설이 같은 PDF의 앞/뒤 또는 같은 페이지에 있어도 분리된다.
- 모든 문항에 연결 상태가 존재하고 silent drop이 없다.
- 골든셋 auto-link precision 목표 99.5% 이상, cross-section false link 0건.
- 문제 crop 300 DPI 기본이 유지되고 450 DPI 고화질 옵션이 작동한다.

## 2026-08-24 재개방 회귀 - 학생용 해설/빠른답

사용자 실사용 화면에서 다음 결함을 확인하여 완료 상태를 다시 연다.

- [x] 최종 상세 해설 crop이 첫 줄·문항 번호·마지막 줄을 모두 포함하고 다음 문항을 포함하지 않음
- [x] 빠른답 crop이 대상 문제 번호와 실제 답을 함께 보이며 다른 행 번호를 보이지 않음
- [x] 해설 bbox를 Codex 단독 좌표가 아니라 독립 AI 2개 이상이 각각 제안하고 의미 합의함
- [x] 최종 저장된 answer/detail PNG 자체를 서로 다른 검수 AI가 완전성·번호·답 가시성 기준으로 검사함
- [x] 검수 대상 이미지와 최종 저장 이미지 SHA가 동일함
- [x] 빠른답 번호·답을 읽지 못하면 VERIFIED가 아니라 REVIEW로 남음
- [x] 첨부 화면 회귀 fixture 및 실제 PDF 재실행
- [x] Python 전체 테스트와 웹 type/lint/build
- [x] 앱 데스크톱/모바일 시각 재검증

## 진행 기록

### 2026-08-24 자이스토리 신규 형식 회귀

- [x] 문제 12~15·18쪽에 개념 설명, 빈칸/OX, 객관식, 서술형·단답형을 의도적으로 혼합
- [x] 개념 12쪽과 빈칸/OX 13쪽은 crop 0개, 실제 A01~A08·A17~A20만 12개 검출
- [x] 문제 `A13`과 해설 `scope A + parent 13`을 scope 증거가 있을 때만 같은 번호로 정규화
- [x] 해설 앞 1~8쪽의 전권 공용 빠른정답과 상세 anchor 직전 4쪽을 분리된 허용 구간으로 처리
- [x] 빠른정답 초기 page-map은 빈 blocks로 작게 확인하고 선택 문항만 별도 locator로 추출
- [x] 명시 해설 범위의 끝쪽을 확인하고 동일 오프셋의 중간 인쇄쪽을 보간
- [x] 같은 영문 numbering prefix의 연속 문항은 페이지별 제목 문구가 달라도 같은 numbering scope로 유지
- [x] 열 좌표가 어긋난 A20에 실패 PNG와 실제 column split을 제공해 재교정하고 PASS 확인
- [x] footer/해설 쪽수 안내가 문제 crop에 유입되지 않도록 하단 확장 제한
- [x] Python 전체 회귀 147개 통과
- [x] 앱 최종 재실행에서 12개 문제와 빠른답·상세해설 연결 상태 확인

- 2026-08-23: 계획 생성. 사용자 요구와 고2 선택과목의 학생용 문제·정답과해설 표본을 반영함.
- 2026-08-23: 기준선 기록 - Python 48개 테스트 통과, TypeScript/lint/build 통과, 기존 셀파 run은 문제 crop 160/160 PASS. 해설 연결 패치는 기존 run 결과를 변경하지 않는 sidecar 방식으로 시작함.
- 2026-08-24: 해설 anchor·block·link verification 프롬프트 3개와 strict schema 3개 추가. 단일 쪽=start, raw printed label 보존, quick/detail/mixed, 번호 reset/range/continuation, identity-only fail-closed 계약 검증 완료.
- 2026-08-24: 순수 연결 코어와 fail-closed DP 정렬, sidecar/cache/CLI 구현 완료. 백엔드 API·300/450 DPI 전달·asset 경로 차단, 웹 입력·동시 보기·모바일 탭 구현. 중간 기준 Python 74개 및 웹 type/lint/build 통과. 실제 AI·렌더 adapter와 파일럿은 진행 중.
- 2026-08-24: `tests/fixtures/solution_link_cases.json`에 셀파 스캔형, 메가N제 단원 조각형, 단일 시작 20쪽→20~23쪽 synthetic 회귀 메타데이터를 고정함.
- 2026-08-24: REVIEW/conflict 후보 asset을 API에 노출하고, 문제·답·해설 비교 모달에서 역할별 후보를 선택해 수동 확정하는 경로를 추가함. 후보 목록 밖 asset, 역할이 다른 asset, 경로 이탈은 확정할 수 없으며 문제 crop verdict는 그대로 유지함.
- 2026-08-24: 실물 회귀 입력을 학생용 문제 PDF와 별도 정답과해설 PDF 쌍으로 제한함.
- 2026-08-24: 300→450 DPI 재실행에서 동일 asset이 170×67→254×100 픽셀로 증가하고 PDF SHA+DPI별 파일이 분리되는 것을 확인함. 해설 PDF 교체 시 렌더 캐시 분리, 누락 검증 자동 재시도, 전체 Python 102개 및 웹 type/lint/build 통과.
- 2026-08-24: 로컬 앱을 재시작해 Gemini/Qwen 키 자동 상속, 결과 11개, 각 카드의 `답·해설 보기`, 데스크톱 3열 비교, 390px 모바일 문제/답/해설 탭 전환을 브라우저에서 확인함. 오류 overlay 및 console error 0건.
- 2026-08-24: 텍스트 레이어가 전혀 없는 셀파 문제 005·007쪽 001~010을 실제 실행해, 빠른답 68쪽 + 상세 70~71쪽(009 continuation 포함)을 10/10 VERIFIED로 연결함. `개념편` 66쪽의 같은 번호는 원본 `문제편` variant와 달라 후보에서 제외했고, 001~101 밀집표는 대상 10개만 2차 locator로 개별 crop함.
- 2026-08-24: 메가N제 단원 조각 인쇄 099쪽의 508~512를 전체 해설 40~41쪽에 대조함. 상세 해설 범위와 block은 찾았으나 밀집 빠른답의 개별 위치에 Gemini/Qwen 불일치가 남아 5건 모두 REVIEW로 보존함. 이 표본으로 검증 미리보기와 최종 저장 crop을 동일하게 만드는 안전 게이트 및 실제 후보 수동 검토 UI를 확인함.
- 2026-08-24: 해설 경계를 Codex+Gemini 독립 제안, 의미 단위 합의, 불일치 시 Qwen 보강으로 변경함. provider별 x/y 전치·축약 JSON을 정규화하고, mixed/coarse/fine 빠른답은 번호·소문항·명시 답 단위로 합의함.
- 2026-08-24: 최종 300 DPI PNG를 Gemini+Qwen이 모두 검사하고, 번호·답·첫 줄·마지막 줄·이웃/다음 문항 부재를 hard gate로 적용함. 검수와 최종 렌더가 동일 geometry/SHA를 사용하며, 실패 문항만 Codex+Gemini 표적 경계 복구 후 다시 이중 검수함.
- 2026-08-27: 검토 후보 ID만 남고 실제 crop asset이 빠져 downstream import가 중단되는 계약 불일치를 수정함. 새 manifest는 `review_only|auto_verified|manual_verified` 선택 상태를 명시하고 모든 검토 후보를 materialize한다. 자기모순 검증 응답은 같은 공급자에게 1회 교정하며 실패도 캐시한다. Codex/Gemini 중 한쪽의 유효한 복구 bbox도 후보군에 넣되 최종 Gemini+Qwen hard gate는 유지하고, 밀집 빠른정답표 탐색을 상·하·좌·우로 확장했다.
- 2026-08-24: 학생용 셀파 스캔 표본 001~010을 답 10개+상세해설 10개로 10/10 VERIFIED, 학생용 메가N제 단원 조각 508~512를 실제 빠른답 표 5개+40~41쪽 상세해설 5개로 5/5 VERIFIED. 선택 자산 SHA 대조 0건 불일치. Qwen 월 100달러 차단 장치 아래 누적 추정 사용액 약 1.37달러에서 완료함.
- 2026-08-24: 최신 코드로 앱 재시작 후 최근 작업에 학생용 셀파·메가N제만 노출되고 제외 대상 0건임을 확인함. 메가 509 데스크톱 3열 문제/빠른답/상세해설, 셀파 004의 390px 모바일 문제/답/해설 탭 전환, PASS 5·10/REVIEW 0/FAIL 0 및 console error 0건을 브라우저에서 검증함. 결과 화면 요약은 작업 시작 당시 summary가 아니라 최신 결과 summary를 우선 표시하도록 수정함.
- 2026-08-24: 자이스토리 문제 12~15·18쪽을 앱으로 재실행. 개념 설명 12쪽과 빈칸/OX 13쪽은 0개로 제외하고 A01~A08·A17~A20을 대문항 12개로 생성해 12/12 PASS. 해설 앞 빠른정답 2쪽과 상세 4~7·12~13쪽을 12/12 VERIFIED로 연결하여 빠른답 12개+상세해설 12개, 최종 asset SHA 불일치 0건을 확인함. Python 148개 테스트 통과, Qwen 누적 추정 $1.78/$100에서 완료함.
