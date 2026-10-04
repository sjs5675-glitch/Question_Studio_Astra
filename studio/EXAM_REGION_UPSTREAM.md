# 시험지 영역 기능의 PNKmath 참고 기록

참고 저장소: `PNKmath/ngd-studio`, 고정 커밋 [`cafa7e70eca2b0bea5ff85ec160987c6f2f471b8`](https://github.com/PNKmath/ngd-studio/commit/cafa7e70eca2b0bea5ff85ec160987c6f2f471b8). 아래 파일의 원문을 2026-09-25에 직접 확인했다. 사용자가 밝힌 개발자 사용 허락에 따라 필요한 원본 파일 네 개를 `studio/vendor/pnkmath/upstream/`에 그대로 보존하고, 아래의 adapted 파일을 실제 런타임에서 호출한다. 원본에 있는 고지는 그대로 보존했다.

| 원문 | Astra에서 채택·적응한 부분 | 제외한 부분 |
| --- | --- | --- |
| [`image_cleaner.py`](https://raw.githubusercontent.com/PNKmath/ngd-studio/cafa7e70eca2b0bea5ff85ec160987c6f2f471b8/image_cleaner.py) | 원문의 `process_question` source → provider → decode → cleaned write 흐름을 `studio/vendor/pnkmath/image_cleaner.py`에 수정 이식했다. `exam_cleaning.py`가 페이지 선행 타일마다 실제 호출한다. 원본/정리본 분리는 유지한다. | 실패나 `--no-clean` 때 원본을 정리본 경로에 복사하는 fallback, 질문별 CLI 후처리. |
| [`image_provider_adapter.py`](https://raw.githubusercontent.com/PNKmath/ngd-studio/cafa7e70eca2b0bea5ff85ec160987c6f2f471b8/image_provider_adapter.py) | 원문의 제공자 `clean_image` 계약을 `studio/vendor/pnkmath/image_provider_adapter.py`에 수정 이식했다. 현재 런타임은 `CodexCliImageProvider`를 사용하며 Astra CLI 계약과 생성 이미지 검증 경로에 연결된다. 초기 Gemini 채택 설명은 현재 구현과 다르다. | 별도 Google SDK 클라이언트, 초기 Gemini 어댑터와 자체 3회 재시도 경로. |
| [`coords.ts`](https://raw.githubusercontent.com/PNKmath/ngd-studio/cafa7e70eca2b0bea5ff85ec160987c6f2f471b8/ngd-studio/lib/cropper/coords.ts), [`CropBoxLayer.tsx`](https://raw.githubusercontent.com/PNKmath/ngd-studio/cafa7e70eca2b0bea5ff85ec160987c6f2f471b8/ngd-studio/components/cropper/CropBoxLayer.tsx) | 원문의 `screenToImage`, `normalizeBox`, `clampBox`, `applyResize`를 `studio/web/vendor/pnkmath/crop_geometry.mjs`로 이식해 `exam-region.mjs`에서 실제 import한다. 8핸들, 역방향 드래그, pointerup 확정은 Astra의 0~1 좌표·저장 CAS에 맞췄다. | React/SVG 구성, 화면 픽셀을 저장 좌표로 사용, 드래그 중 지속 저장. CSS 비율 오버레이가 `imageToScreen`과 같은 렌더 역할을 해 별도 함수는 두지 않았다. |
| [`CropperWorkspace.tsx`](https://raw.githubusercontent.com/PNKmath/ngd-studio/cafa7e70eca2b0bea5ff85ec160987c6f2f471b8/ngd-studio/components/cropper/CropperWorkspace.tsx) | 페이지별 박스와 읽기 순서·선택 상태를 분리하는 흐름을 참고했다. Astra는 서버 초안에 revision과 원본/정리본 SHA를 저장한다. | 브라우저 `localStorage`만을 정본으로 쓰는 방식, PDF 경로를 직접 전달하는 API. |
| [`auto-crop/route.ts`](https://raw.githubusercontent.com/PNKmath/ngd-studio/cafa7e70eca2b0bea5ff85ec160987c6f2f471b8/ngd-studio/app/api/auto-crop/route.ts), [`gemini_crop.py`](https://raw.githubusercontent.com/PNKmath/ngd-studio/cafa7e70eca2b0bea5ff85ec160987c6f2f471b8/workspaces/crop/gemini_crop.py) | 입력 이미지에 AI가 영역을 제안하고, 사용자가 수정한 뒤 확정하는 순서만 참고했다. 검출 자체는 Astra 기존 Question_Crop 스키마·프롬프트·CLI를 재사용한다. | 새 Gemini 검출 엔진, 원본 PDF 직접 검출, 경로 문자열을 받는 Next API. PNK Gemini bbox는 `[y_min,x_min,y_max,x_max]`이고 Astra Question_Crop은 `[x0,y0,x1,y1]`이므로 좌표를 혼용하지 않는다. |
| [`cleanerRunner.ts`](https://raw.githubusercontent.com/PNKmath/ngd-studio/cafa7e70eca2b0bea5ff85ec160987c6f2f471b8/ngd-studio/server/stages/cleanerRunner.ts) | 페이지별 성공·실패 상태와 재시도 구분을 참고했다. | 해설·생성 파이프라인과 원본 복사 `--no-clean` 실행. |

## 원본과 adapted 차이 확인

원본은 `studio/vendor/pnkmath/upstream/`에 있고 런타임 수정본은 `studio/vendor/pnkmath/` 및 `studio/web/vendor/pnkmath/`에 있다. 각 파일의 차이는 다음 명령으로 재현할 수 있다.

```text
git diff --no-index studio/vendor/pnkmath/upstream/image_cleaner.py studio/vendor/pnkmath/image_cleaner.py
git diff --no-index studio/vendor/pnkmath/upstream/image_provider_adapter.py studio/vendor/pnkmath/image_provider_adapter.py
git diff --no-index studio/vendor/pnkmath/upstream/coords.ts studio/web/vendor/pnkmath/crop_geometry.mjs
git diff --no-index studio/vendor/pnkmath/upstream/CropBoxLayer.tsx studio/web/vendor/pnkmath/crop_geometry.mjs
```

**라이선스 상태:** 고정 커밋의 트리에서 `LICENSE`, `COPYING`, `NOTICE` 파일을 찾지 못했고 GitHub 저장소 메타데이터의 `license` 값은 `null`이었다. [`phase-07-license-readme.md`](https://raw.githubusercontent.com/PNKmath/ngd-studio/cafa7e70eca2b0bea5ff85ec160987c6f2f471b8/docs/planning/exam-oss-fork-stage1/phase-07-license-readme.md)는 MIT 라이선스 작성 *계획*이며 상태가 `pending`이다. 게시자는 원저작자에게 받은 허가에 이 원본·수정본을 포함한 공개 GitHub 재배포도 포함된다고 확인했다. 이 기록은 저장소에 MIT 라이선스가 확인됐거나 제3자에게 무제한 재배포를 허가한다고 주장하지 않는다.
