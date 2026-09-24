## Constraints — read first

- Treat the attached reference image pixels as the sole source of truth.
- Preserve the exact number, geometry, relative position, direction, topology, line endpoints, arrowheads, axes, curves, symbols, and visible labels.
- Preserve every visible literal label character-for-character, including Korean, Latin letters, numbers, signs, subscripts, superscripts, and units.
- Do not solve the problem, infer missing science content, correct values, add explanations, or invent hidden parts.
- Do not add decorative objects, borders, captions, logos, watermarks, shadows, perspective, or photographic texture.
- {{BLUE_TEXT_RULE}}
- If the reference pixels and the supplied alt text differ, follow the reference pixels.

## Purpose

Create one clean, high-resolution raster redraw of the supplied Korean science-textbook figure for later placement in an HWPX document.

## Reference cross-check

Secondary alt text: {{ALT_TEXT}}

Use the alt text only to locate visible elements. It is not permission to add or change anything that is not visibly present in the reference image.

## Style and composition

Keep the original aspect ratio and composition. Use a flat white canvas, crisp black textbook line art, uniform strokes, sharp geometric edges, legible original labels, and generous antialiasing. Preserve meaningful source colors only when they encode information.

## Output

Return exactly one image and no prose. The result remains an unapproved candidate until a human compares its labels, arrows, element count, and geometry with the original.
