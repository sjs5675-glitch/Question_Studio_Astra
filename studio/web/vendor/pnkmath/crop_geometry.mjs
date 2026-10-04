// Adapted from PNKmath/ngd-studio coords.ts and CropBoxLayer.tsx at
// cafa7e70eca2b0bea5ff85ec160987c6f2f471b8. Originals are preserved in
// studio/vendor/pnkmath/upstream/. Astra stores [left, top, right, bottom] in 0..1.

const limit = (value, min, max) => Math.max(min, Math.min(max, value));

export function screenToImage(clientX, clientY, rect, imageWidth, imageHeight) {
  return [(clientX - rect.left) * imageWidth / rect.width,
    (clientY - rect.top) * imageHeight / rect.height];
}

export function normalizeBox([left, top, right, bottom]) {
  return [Math.min(left, right), Math.min(top, bottom),
    Math.max(left, right), Math.max(top, bottom)];
}

export function clampBox(box) {
  return normalizeBox(box.map((value) => limit(value, 0, 1)));
}

export function applyResize(direction, original, dx, dy) {
  let [left, top, right, bottom] = original;
  if (direction.includes("n")) top += dy;
  if (direction.includes("s")) bottom += dy;
  if (direction.includes("w")) left += dx;
  if (direction.includes("e")) right += dx;
  return clampBox([left, top, right, bottom]);
}
