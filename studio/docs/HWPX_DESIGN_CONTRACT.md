# HWPX 디자인 계약

상태: `approved` (사용자 승인: 2026-08-25)  
적용 대상: 문제용 HWPX와 해설용 HWPX  
원칙: serialization engine과 시각 디자인을 분리하며 기존 workbook base의 헤더·열·색상·BinData를 상속하지 않는다.

## 페이지

- 용지: A4 세로
- 단: 1단
- 여백: 왼쪽/오른쪽 20 mm, 위/아래 18 mm
- 머리말/꼬리말: 없음
- 쪽 번호: 아래 가운데, 첫 페이지부터
- 문항은 canonical 순서로 연속 배치하고, 한 문항을 억지로 한 페이지에 고정하지 않는다.
- 문항 내부 block 앞뒤에서만 자연스러운 페이지 나눔을 허용하며 표·그림·보기 내부 분할은 피한다.

## 공통 계층

- 기본 글자: 한글 가독성을 우선한 보편 산세리프, 10 pt
- 문항 번호/해설 번호: 12 pt, 굵게
- 문제 stem·instruction: 10.5 pt
- 보기·선택지·표: 9.5~10 pt
- 해설 본문: 10 pt, 줄 간격 160%
- 수식: native HWP equation, 주변 본문 baseline과 정렬
- paragraph 간격은 role로 결정하며 임의 빈 문단으로 간격을 만들지 않는다.

## 문제 HWPX

- 포함: `content.question`의 저장된 renderable block(검수 상태는 receipt warning으로 보존)
- 제외: `content.answer`, `content.explanation`, review note, provider/job metadata
- 선택지는 canonical label과 source layout 순서를 보존한다.
- 승인·ready selected figure는 그대로 사용하고, selected가 미승인이면 동일 lineage의 ready original을 사용한다. AI candidate를 자동 선택하지 않는다.
- canonical 문제를 안전하게 렌더링할 수 없으면 ready 원본 문제 crop 하나를 전체-page inline raster로 사용한다.
- 한컴에서 검증되지 않은 transcription prompt가 box/table/equation native 구조를 만들면 OCR JSON은 보존하고 같은 source crop fallback을 사용한다. receipt에는 prompt version, 위험 feature와 `hancom_unverified_native_structure` 사유를 기록한다.

## 해설 HWPX

- 포함: 저장된 renderable `content.explanation`과 실제로 보이는 answer
- 제외: AI가 추론하거나 생성한 해설, endnote, solver 결과
- answer/explanation이 없거나 renderer 미지원이면 해당 부분만 생략하고 문항 번호와 receipt warning을 유지한다.
- 정답은 canonical ID를 새로 해석하지 않고 저장된 visible label/content만 사용한다.

## 컨테이너

- box: 얇은 중립 회색 테두리, 내부 여백 2.5 mm
- bogi: title이 visible일 때만 제목 표시, 항목 label과 본문 분리
- choices: 원본 순서 유지; 공간이 부족하면 행 단위 wrap, label과 본문 분리 금지
- table: native table, writable width 이내, 셀 padding 1.5 mm, header role만 옅은 회색
- 중첩 box→bogi/choices/table은 편집 가능한 native container로 flatten-render하고 raster fallback하지 않는다.

## 그림

- PNG 입력은 계획에서 선택한 ready original/approved variant/source crop만 사용하고 HWPX engine 요구 시 RGB BMP로 변환한다.
- aspect ratio 유지, writable width를 넘지 않음, 확대 시 원본 픽셀 한계를 넘지 않음
- floating 배치 없이 inline을 기본으로 한다.
- SVG를 package에 포함하지 않는다.

## 반복 요소와 금지 사항

- workbook 이름, 학원명, 로고, 워터마크, 정답률 badge를 새로 만들지 않는다.
- 기존 `header_area_template.xml`, base BinData, fixed column width를 복사하지 않는다.
- solver, endnote, AI 생성 해설을 포함하지 않는다.
- 입력 원본/template을 덮어쓰지 않는다.

## 승인된 결정

- 문제/해설은 같은 neutral template을 공유하고 design profile만 두 개로 분리한다.
- 위 A4/1단/여백/글자 계층을 사용한다.
- source mechanics 기준 root는 `C:\Users\USER\OneDrive\학원 자료 모음\exam-studio-multi`로 고정한다.

## Native ordered v0.2 직렬화 결정

- `paragraph`, `figure`, `bogi`, `choices`, `box`, `table`은 문제·정답·해설 컨테이너에서 canonical 순서로 재귀 렌더한다. 최대 block 깊이는 12이며 순환 참조는 오류로 중단한다.
- box는 얇은 중립 회색 테두리의 편집 가능한 1×1 native table로 만들고 내부 block을 셀 안 native 문단/객체로 유지한다.
- table은 `row_span`·`col_span`으로 occupancy grid를 검증한 뒤 native `cellAddr`·`cellSpan`으로 보존한다. 빈 셀도 편집 가능한 빈 문단을 가진다.
- `source_layout`이 horizontal/grid이고 모든 선택지가 단일 문단이면 label과 본문을 한 문단의 가로 흐름으로 보존한다. 그림·중첩 block이 있는 복합 선택지는 label을 먼저 둔 세로 native flow로 안전하게 전환한다.
- 명시적 `line_break`와 text 내부 개행은 native `hp:lineBreak`로 보존한다.
- 해설용 HWPX의 content 정답은 저장된 renderable block만 렌더하며 새 정답을 추론하지 않는다.
- `table_snapshot` 자체를 figure로 삽입하지 않는다. 문제 구조가 미지원이면 개별 candidate 대신 전체 ready 원본 문제 crop을 사용한다.
