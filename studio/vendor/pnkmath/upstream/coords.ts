import type { CropBox, PdfRotation } from "./types";

interface Viewport {
  displayWidth: number;
  displayHeight: number;
  imageWidth: number;
  imageHeight: number;
}

/** 외부 입력 회전값을 cropper가 지원하는 90도 단위로 정규화 */
export function normalizePdfRotation(rotation: number): PdfRotation {
  const normalized = ((Math.round(rotation / 90) * 90) % 360 + 360) % 360;
  return normalized as PdfRotation;
}

/** 회전이 실제 렌더 이미지에 적용된 뒤의 픽셀 치수 */
export function getRotatedImageSize(args: {
  width: number;
  height: number;
  rotation: number;
}): { width: number; height: number } {
  const rotation = normalizePdfRotation(args.rotation);
  if (rotation === 90 || rotation === 270) {
    return { width: args.height, height: args.width };
  }
  return { width: args.width, height: args.height };
}

/** 화면 좌표 (clientX, clientY 기준 컨테이너 상대) → 이미지 픽셀 좌표 */
export function screenToImage(
  screenX: number,
  screenY: number,
  viewport: Viewport
): { x: number; y: number } {
  const scaleX = viewport.imageWidth / viewport.displayWidth;
  const scaleY = viewport.imageHeight / viewport.displayHeight;
  return {
    x: screenX * scaleX,
    y: screenY * scaleY,
  };
}

/** 이미지 픽셀 좌표 → 화면 좌표 */
export function imageToScreen(
  imgX: number,
  imgY: number,
  viewport: Viewport
): { x: number; y: number } {
  const scaleX = viewport.displayWidth / viewport.imageWidth;
  const scaleY = viewport.displayHeight / viewport.imageHeight;
  return {
    x: imgX * scaleX,
    y: imgY * scaleY,
  };
}

/** w/h가 음수인 박스 → 정규화 (드래그 방향 무관하게 양수) */
export function normalizeBox(box: {
  x: number;
  y: number;
  w: number;
  h: number;
}): { x: number; y: number; w: number; h: number } {
  return {
    x: box.w >= 0 ? box.x : box.x + box.w,
    y: box.h >= 0 ? box.y : box.y + box.h,
    w: Math.abs(box.w),
    h: Math.abs(box.h),
  };
}

/** 박스를 페이지 경계에 클램프 */
export function clampBox(
  box: { x: number; y: number; w: number; h: number },
  pageWidth: number,
  pageHeight: number
): { x: number; y: number; w: number; h: number } {
  const x = Math.max(0, Math.min(box.x, pageWidth));
  const y = Math.max(0, Math.min(box.y, pageHeight));
  const x2 = Math.max(0, Math.min(box.x + box.w, pageWidth));
  const y2 = Math.max(0, Math.min(box.y + box.h, pageHeight));
  return { x, y, w: x2 - x, h: y2 - y };
}

/**
 * 박스 배열의 현재 순서를 그대로 따라 1부터 번호 부여 (생성순/사용자 정렬순).
 * 위치(y,x) 기반 정렬은 하지 않는다 — 새 박스는 mouseup 시점에 배열 끝에
 * append되고, 박스 리스트 drag-and-drop 재정렬이 그대로 번호에 반영된다.
 */
export function autoNumber(boxes: CropBox[]): CropBox[] {
  return boxes.map((box, i) => ({ ...box, number: i + 1 }));
}

/**
 * 박스의 x 좌표를 이미지 폭을 기준으로 수평 미러링한다.
 *
 * 합성 순서: rotation 먼저 → flip.
 * `imageWidth`에는 `getRotatedImageSize` 결과의 width를 그대로 넘긴다.
 *
 * round-trip 보장: mirrorBoxX(mirrorBoxX(box, w), w) deepEquals box
 */
export function mirrorBoxX(
  box: { x: number; y: number; w: number; h: number },
  imageWidth: number
): { x: number; y: number; w: number; h: number } {
  return {
    x: imageWidth - box.x - box.w,
    y: box.y,
    w: box.w,
    h: box.h,
  };
}

/**
 * Gemini Vision의 1000×1000 정규화 bbox를 이미지 픽셀 좌표계 CropBox로 변환.
 * bbox 형식: [y_min, x_min, y_max, x_max] (0-1000, Gemini native 좌표계).
 */
export function normalizedBboxToCropBox(args: {
  bbox: [number, number, number, number];
  pageIndex: number;
  imageWidth: number;
  imageHeight: number;
  number: number;
  kind?: "regular" | "essay";
  id?: string;
}): CropBox {
  const [y_min, x_min, y_max, x_max] = args.bbox;
  const x = Math.round((x_min / 1000) * args.imageWidth);
  const y = Math.round((y_min / 1000) * args.imageHeight);
  const w = Math.round(((x_max - x_min) / 1000) * args.imageWidth);
  const h = Math.round(((y_max - y_min) / 1000) * args.imageHeight);
  const clamped = clampBox({ x, y, w, h }, args.imageWidth, args.imageHeight);
  return {
    id: args.id ?? crypto.randomUUID(),
    page: args.pageIndex,
    x: clamped.x,
    y: clamped.y,
    w: clamped.w,
    h: clamped.h,
    number: args.number,
    kind: args.kind,
  };
}
