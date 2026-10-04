"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import type { CropBox } from "@/lib/cropper/types";
import {
  clampBox,
  imageToScreen,
  normalizeBox,
  screenToImage,
} from "@/lib/cropper/coords";

// ──────────────────────────────────────────────
// Resize handle directions
// ──────────────────────────────────────────────
type HandleDir =
  | "n"
  | "ne"
  | "e"
  | "se"
  | "s"
  | "sw"
  | "w"
  | "nw";

// ──────────────────────────────────────────────
// Drag state
// ──────────────────────────────────────────────
type DragState =
  | { mode: "none" }
  | {
      mode: "create";
      startImgX: number;
      startImgY: number;
    }
  | {
      mode: "move";
      boxId: string;
      startImgX: number;
      startImgY: number;
      origBox: Pick<CropBox, "x" | "y" | "w" | "h">;
    }
  | {
      mode: "resize";
      dir: HandleDir;
      boxId: string;
      startImgX: number;
      startImgY: number;
      origBox: Pick<CropBox, "x" | "y" | "w" | "h">;
    };

/** Local preview of in-progress create drag — never written to parent state until mouseup. */
interface PendingCreate {
  startImgX: number;
  startImgY: number;
  currentImgX: number;
  currentImgY: number;
}

// ──────────────────────────────────────────────
// Props
// ──────────────────────────────────────────────
export interface CropBoxLayerProps {
  boxes: CropBox[];
  selectedBoxId: string | null;
  /** display size of the canvas (CSS px) */
  displayWidth: number;
  displayHeight: number;
  /** image pixel dimensions (for coord conversion) */
  imageWidth: number;
  imageHeight: number;
  onBoxesChange: (boxes: CropBox[]) => void;
  onSelectBox: (id: string | null) => void;
}

const MIN_BOX_SIZE = 5; // pixels in image-space
const HANDLE_RADIUS = 6;

// ──────────────────────────────────────────────
// Helper: apply resize drag to origBox
// ──────────────────────────────────────────────
function applyResize(
  dir: HandleDir,
  origBox: Pick<CropBox, "x" | "y" | "w" | "h">,
  dx: number,
  dy: number
): { x: number; y: number; w: number; h: number } {
  let { x, y, w, h } = origBox;
  if (dir.includes("n")) {
    y += dy;
    h -= dy;
  }
  if (dir.includes("s")) {
    h += dy;
  }
  if (dir.includes("w")) {
    x += dx;
    w -= dx;
  }
  if (dir.includes("e")) {
    w += dx;
  }
  return { x, y, w, h };
}

// ──────────────────────────────────────────────
// Component
// ──────────────────────────────────────────────
export function CropBoxLayer({
  boxes,
  selectedBoxId,
  displayWidth,
  displayHeight,
  imageWidth,
  imageHeight,
  onBoxesChange,
  onSelectBox,
}: CropBoxLayerProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const dragRef = useRef<DragState>({ mode: "none" });
  // We keep a local boxes ref for mousemove (avoids stale closure)
  const boxesRef = useRef<CropBox[]>(boxes);
  useEffect(() => {
    boxesRef.current = boxes;
  }, [boxes]);

  // Local preview state — only used for the in-progress create drag.
  // Parent's boxes state is NOT touched until mouseup commits the final box.
  const [pendingCreate, setPendingCreate] = useState<PendingCreate | null>(
    null
  );

  // Stable refs for parent callbacks. The window-mousemove/mouseup listeners
  // are attached inside useEffect with deps [displayWidth,...] and would
  // otherwise capture stale `onBoxesChange` / `onSelectBox` closures, which
  // close over stale `pageIndex` / `currentPage` and route every newly created
  // box to page 0 regardless of the page the user is on.
  const onBoxesChangeRef = useRef(onBoxesChange);
  const onSelectBoxRef = useRef(onSelectBox);
  useEffect(() => {
    onBoxesChangeRef.current = onBoxesChange;
    onSelectBoxRef.current = onSelectBox;
  });

  const viewport = {
    displayWidth,
    displayHeight,
    imageWidth,
    imageHeight,
  };

  // ── coordinate helpers ──
  const toImg = useCallback(
    (sx: number, sy: number) => screenToImage(sx, sy, viewport),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [displayWidth, displayHeight, imageWidth, imageHeight]
  );
  const toScreen = useCallback(
    (ix: number, iy: number) => imageToScreen(ix, iy, viewport),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [displayWidth, displayHeight, imageWidth, imageHeight]
  );

  // ── relative mouse position inside SVG ──
  function svgRelative(e: React.MouseEvent | MouseEvent): {
    x: number;
    y: number;
  } {
    const rect = svgRef.current!.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  // ── onMouseDown on SVG (background / box / handle) ──
  function handleMouseDown(e: React.MouseEvent<SVGSVGElement>) {
    if (e.button !== 0) return;
    e.preventDefault();

    const target = e.target as SVGElement;
    const rel = svgRelative(e);

    // -- delete button on selected box?
    const deleteBoxId = target.dataset["deleteboxid"];
    if (deleteBoxId) {
      onBoxesChange(boxesRef.current.filter((b) => b.id !== deleteBoxId));
      onSelectBox(null);
      return;
    }

    // -- resize handle?
    const handleDir = target.dataset["handle"] as HandleDir | undefined;
    const boxId = target.dataset["boxid"];

    if (handleDir && boxId) {
      const box = boxesRef.current.find((b) => b.id === boxId);
      if (!box) return;
      const img = toImg(rel.x, rel.y);
      dragRef.current = {
        mode: "resize",
        dir: handleDir,
        boxId,
        startImgX: img.x,
        startImgY: img.y,
        origBox: { x: box.x, y: box.y, w: box.w, h: box.h },
      };
      return;
    }

    // -- box body (data-boxid set on rect)?
    if (boxId) {
      const box = boxesRef.current.find((b) => b.id === boxId);
      if (!box) return;
      onSelectBox(boxId);
      const img = toImg(rel.x, rel.y);
      dragRef.current = {
        mode: "move",
        boxId,
        startImgX: img.x,
        startImgY: img.y,
        origBox: { x: box.x, y: box.y, w: box.w, h: box.h },
      };
      return;
    }

    // -- background → create (local preview only; parent state untouched until mouseup)
    onSelectBox(null);
    const img = toImg(rel.x, rel.y);
    dragRef.current = {
      mode: "create",
      startImgX: img.x,
      startImgY: img.y,
    };
    setPendingCreate({
      startImgX: img.x,
      startImgY: img.y,
      currentImgX: img.x,
      currentImgY: img.y,
    });
  }

  // ── global mousemove / mouseup (attached to window) ──
  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      const drag = dragRef.current;
      if (drag.mode === "none") return;
      if (!svgRef.current) return;

      const rel = svgRelative(e);
      const img = screenToImage(rel.x, rel.y, viewport);

      if (drag.mode === "create") {
        // Update local preview only — parent state untouched.
        setPendingCreate((prev) =>
          prev
            ? { ...prev, currentImgX: img.x, currentImgY: img.y }
            : prev
        );
      } else if (drag.mode === "move") {
        const dx = img.x - drag.startImgX;
        const dy = img.y - drag.startImgY;
        const updated = boxesRef.current.map((b) => {
          if (b.id !== drag.boxId) return b;
          const clamped = clampBox(
            {
              x: drag.origBox.x + dx,
              y: drag.origBox.y + dy,
              w: drag.origBox.w,
              h: drag.origBox.h,
            },
            imageWidth,
            imageHeight
          );
          return { ...b, ...clamped };
        });
        onBoxesChangeRef.current(updated);
      } else if (drag.mode === "resize") {
        const dx = img.x - drag.startImgX;
        const dy = img.y - drag.startImgY;
        const raw = applyResize(drag.dir, drag.origBox, dx, dy);
        const normalized = normalizeBox(raw);
        const clamped = clampBox(normalized, imageWidth, imageHeight);
        const updated = boxesRef.current.map((b) => {
          if (b.id !== drag.boxId) return b;
          return { ...b, ...clamped };
        });
        onBoxesChangeRef.current(updated);
      }
    }

    function onMouseUp(e: MouseEvent) {
      const drag = dragRef.current;
      if (drag.mode === "none") return;

      if (drag.mode === "create") {
        // Derive final box dimensions from the mouseup event directly.
        // We deliberately do NOT read pending preview state here — that ref is
        // updated via useEffect (async) and can be stale on fast drags.
        if (svgRef.current) {
          const rel = svgRelative(e);
          const img = screenToImage(rel.x, rel.y, viewport);
          const raw = normalizeBox({
            x: drag.startImgX,
            y: drag.startImgY,
            w: img.x - drag.startImgX,
            h: img.y - drag.startImgY,
          });
          if (raw.w >= MIN_BOX_SIZE && raw.h >= MIN_BOX_SIZE) {
            const clamped = clampBox(raw, imageWidth, imageHeight);
            const newBox: CropBox = {
              id: crypto.randomUUID(),
              page: 0, // parent overrides via PdfPageCanvas
              x: clamped.x,
              y: clamped.y,
              w: clamped.w,
              h: clamped.h,
              number: boxesRef.current.length + 1, // parent autoNumber overrides
            };
            onBoxesChangeRef.current([...boxesRef.current, newBox]);
            onSelectBoxRef.current(newBox.id);
          } else {
            onSelectBoxRef.current(null);
          }
        }
        setPendingCreate(null);
      } else if (drag.mode === "resize") {
        const boxes = boxesRef.current;
        const box = boxes.find((b) => b.id === drag.boxId);
        if (box) {
          const normalized = normalizeBox(box);
          if (normalized.w < MIN_BOX_SIZE || normalized.h < MIN_BOX_SIZE) {
            onBoxesChangeRef.current(boxes.filter((b) => b.id !== drag.boxId));
            onSelectBoxRef.current(null);
          } else {
            onBoxesChangeRef.current(
              boxes.map((b) =>
                b.id === drag.boxId ? { ...b, ...normalized } : b
              )
            );
          }
        }
      }

      dragRef.current = { mode: "none" };
    }

    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [displayWidth, displayHeight, imageWidth, imageHeight]);

  // ── keyboard: Delete / Backspace ──
  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Delete" || e.key === "Backspace") {
      if (selectedBoxId) {
        onBoxesChange(boxes.filter((b) => b.id !== selectedBoxId));
        onSelectBox(null);
        e.preventDefault();
      }
    }
  }

  // ── render ──
  return (
    <svg
      ref={svgRef}
      width={displayWidth}
      height={displayHeight}
      style={{
        position: "absolute",
        top: 0,
        left: 0,
        cursor: "crosshair",
        userSelect: "none",
      }}
      tabIndex={0}
      onMouseDown={handleMouseDown}
      onKeyDown={handleKeyDown}
    >
      {/* In-progress create preview (dashed outline, no parent state) */}
      {pendingCreate &&
        (() => {
          const norm = normalizeBox({
            x: pendingCreate.startImgX,
            y: pendingCreate.startImgY,
            w: pendingCreate.currentImgX - pendingCreate.startImgX,
            h: pendingCreate.currentImgY - pendingCreate.startImgY,
          });
          const sc = toScreen(norm.x, norm.y);
          const scW = (norm.w / imageWidth) * displayWidth;
          const scH = (norm.h / imageHeight) * displayHeight;
          return (
            <rect
              x={sc.x}
              y={sc.y}
              width={scW}
              height={scH}
              fill="rgba(0,0,255,0.08)"
              stroke="#2563eb"
              strokeWidth={2}
              strokeDasharray="4 2"
              pointerEvents="none"
            />
          );
        })()}

      {boxes.map((box) => {
        const isSelected = box.id === selectedBoxId;
        // normalise before rendering (may be mid-drag with negative w/h)
        const norm = normalizeBox(box);
        const sc = toScreen(norm.x, norm.y);
        const scW = (norm.w / imageWidth) * displayWidth;
        const scH = (norm.h / imageHeight) * displayHeight;

        // Handle positions (screen coords, relative to box top-left)
        const handles: { dir: HandleDir; cx: number; cy: number }[] = [
          { dir: "nw", cx: sc.x, cy: sc.y },
          { dir: "n", cx: sc.x + scW / 2, cy: sc.y },
          { dir: "ne", cx: sc.x + scW, cy: sc.y },
          { dir: "e", cx: sc.x + scW, cy: sc.y + scH / 2 },
          { dir: "se", cx: sc.x + scW, cy: sc.y + scH },
          { dir: "s", cx: sc.x + scW / 2, cy: sc.y + scH },
          { dir: "sw", cx: sc.x, cy: sc.y + scH },
          { dir: "w", cx: sc.x, cy: sc.y + scH / 2 },
        ];

        const labelText = String(box.number);
        const fontSize = 12;
        const labelPad = 3;
        const labelW = labelText.length * (fontSize * 0.65) + labelPad * 2;
        const labelH = fontSize + labelPad * 2;

        return (
          <g key={box.id}>
            {/* Box body */}
            <rect
              x={sc.x}
              y={sc.y}
              width={scW}
              height={scH}
              fill="rgba(0,0,255,0.08)"
              stroke={isSelected ? "#2563eb" : "#3b82f6"}
              strokeWidth={isSelected ? 3 : 2}
              data-boxid={box.id}
              style={{ cursor: "move" }}
            />

            {/* Number label background */}
            <rect
              x={sc.x}
              y={sc.y - labelH}
              width={labelW}
              height={labelH}
              fill="#2563eb"
              pointerEvents="none"
            />
            {/* Number label text */}
            <text
              x={sc.x + labelPad}
              y={sc.y - labelPad}
              fontSize={fontSize}
              fill="white"
              fontFamily="sans-serif"
              fontWeight="bold"
              pointerEvents="none"
            >
              {labelText}
            </text>

            {/* Delete button (only when selected) — outside top-right, offset
                so it doesn't overlap the NE resize handle */}
            {isSelected && (
              <g style={{ cursor: "pointer" }}>
                <circle
                  cx={sc.x + scW + 12}
                  cy={sc.y - 12}
                  r={9}
                  fill="#dc2626"
                  stroke="white"
                  strokeWidth={1.5}
                  data-deleteboxid={box.id}
                />
                <text
                  x={sc.x + scW + 12}
                  y={sc.y - 11}
                  fontSize={13}
                  fill="white"
                  fontFamily="sans-serif"
                  fontWeight="bold"
                  textAnchor="middle"
                  dominantBaseline="central"
                  pointerEvents="none"
                >
                  ×
                </text>
              </g>
            )}

            {/* Resize handles (only when selected) */}
            {isSelected &&
              handles.map(({ dir, cx, cy }) => (
                <circle
                  key={dir}
                  cx={cx}
                  cy={cy}
                  r={HANDLE_RADIUS}
                  fill="white"
                  stroke="#2563eb"
                  strokeWidth={2}
                  data-handle={dir}
                  data-boxid={box.id}
                  style={{
                    cursor: resizeCursor(dir),
                  }}
                />
              ))}
          </g>
        );
      })}
    </svg>
  );
}

function resizeCursor(dir: HandleDir): string {
  const map: Record<HandleDir, string> = {
    n: "n-resize",
    ne: "ne-resize",
    e: "e-resize",
    se: "se-resize",
    s: "s-resize",
    sw: "sw-resize",
    w: "w-resize",
    nw: "nw-resize",
  };
  return map[dir];
}
