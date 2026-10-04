---
name: Astra 문항 작업실
description: A quiet Korean publishing workspace for collecting, editing and printing questions.
colors:
  paper: "#f5f4f0"
  surface: "#fff"
  inset: "#f0f2ef"
  rail: "#eeefea"
  ink: "#25312d"
  secondary: "#596860"
  rule: "#dce2dc"
  rule-strong: "#b9c6be"
  accent: "#17634e"
  accent-hover: "#104c3b"
  accent-soft: "#e5f0e9"
  focus: "#176d9d"
  danger: "#a33232"
  warning: "#875416"
typography:
  title:
    fontSize: "1.75rem"
    letterSpacing: "-.025em"
  section:
    fontSize: "1rem"
    letterSpacing: "-.025em"
  body:
    fontFamily: "Pretendard, Noto Sans KR, Malgun Gothic, system-ui, sans-serif"
    fontSize: ".875rem"
    lineHeight: 1.55
  label:
    fontSize: ".8125rem"
  small:
    fontSize: ".75rem"
rounded:
  control: "6px"
  panel: "12px"
spacing:
  s1: "4px"
  s2: "8px"
  s3: "12px"
  s4: "16px"
  s6: "24px"
  s8: "32px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "8px 12px"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
  button-neutral:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "8px 12px"
  field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "8px 12px"
  navigation-active:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.accent-hover}"
    rounded: "{rounded.control}"
    padding: "12px"
  tag:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.accent-hover}"
    rounded: "{rounded.control}"
    padding: "3px 7px"
  panel:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.panel}"
    padding: "16px"
---

# Design System: Astra 문항 작업실

## Overview

**Creative North Star: "The Korean Publishing Desk"**

A quiet Korean publishing workspace: persistent navigation, clearly bounded working areas, readable source material and task-grouped controls. Its identity comes from paper, ink, exercise books, manuscript corrections and a teacher's editing desk. Operate mode keeps the content and the next useful action visible.

The user's confirmed scope is the entire application: source import, exam-region selection, saved questions and solutions, question bank, worksheet assembly, analysis examples and document editing, including their pickers and dialogs. PRODUCT.md owns product constraints; this document records the implemented visual system.

**Key Characteristics:**

- Paper surfaces, dark ink and restrained green action states.
- Korean labels with readable wrapping and stable alignment.
- Shared navigation and controls across all seven views.
- Editable content and output preview remain distinct surfaces.

## Colors

Warm paper and muted green establish the workspace; neutral surfaces keep original question images and mathematical content legible.

### Primary

Accent marks primary actions. Accent-hover deepens their hover state; accent-soft marks the current destination and selected content.

### Neutral

Paper is the application background, surface is the working panel, inset separates supporting controls, and rail identifies global navigation. Ink is primary text; secondary is supporting text. Both runtime muted and subtle aliases resolve to secondary after the final contrast override. Rule separates surfaces; rule-strong defines controls.

### Semantic states

Focus supplies the keyboard outline. Danger identifies destructive actions; warning indicates a condition requiring attention. Existing status-specific badges retain their inherited semantic colors.

## Typography

**Interface Font:** Pretendard, Noto Sans KR, Malgun Gothic, system-ui, sans-serif, inherited from the base stylesheet. Paper content retains its selected font; equations retain their existing rendering.

The frontmatter records the reusable scale. Title is for page headings; section for working-area headings; body for interface content; label for controls; small for supporting metadata. Local item titles and document title inputs use 1.25rem. Brand lettering and summary numbers are local treatments, not additional global scale steps.

Headings use compact tracking and balanced wrapping. Korean paragraphs, labels and summaries keep words together while allowing long identifiers to wrap. Interface numerals use tabular spacing.

## Layout

Tokens live on the application body in web/workspace.css; document-specific adaptations live in web/workspace-document.css. These stylesheets are loaded by index.html and document.html. Print/output templates keep their existing styling.

At 1180px and above, the global rail is fixed at 208px. Below that threshold, a compact brand row precedes horizontally scrollable navigation. Main import, bank and analysis containers cap at 1600px with 32px padding; document content caps at the same width with 24px padding. At 600px and below, primary content padding is 16px.

| Surface | Desktop composition | Responsive behavior |
| --- | --- | --- |
| Source import | Upload panel and task-grouped options | Fields and controls reflow; 16px panel padding on small screens |
| Exam-region selection | Flexible source canvas with 304px region panel | Single column at 900px and below |
| Saved questions and solutions | 264px project/item navigation and main editor | Navigation above content at 900px; one navigation column at 600px |
| Question bank | 224px classification and flexible cards | Classification narrows to 200px below 1180px, moves above cards at 900px, uses one column at 600px |
| Worksheet assembly | 232px set navigation; ordered questions beside 280px output controls | Editor becomes one column below 1180px; set navigation moves above at 900px |
| Analysis examples | Summary and example cards | Summary becomes two columns at 600px |
| Document editing | Fluid paper canvas with 304px inspector | Inspector narrows to 280px below 1180px; moves above canvas at 900px, with two task-group columns; groups stack at 600px |

Bounded lists and source panes preserve their local scrolling. Mobile navigation overflow is intentional and contained; the whole page must not scroll horizontally.

## Elevation & Depth

Surface shifts and hairline boundaries define the workspace. The shared shadow is reserved for the paper canvas and overlays: 0 8px 24px rgb(37 49 45 / 7%). Ordinary cards and buttons remain flat. The sidecar records the shadow token.

## Shapes

Controls have gently curved corners using control radius; panels and dialogs use panel radius. Strong rules outline fields; lighter rules separate content. Native disclosure triangles signal expandable task groups. Count badges retain their compact pill shape, while bank tags use control corners.

## Components

### Buttons

Primary and neutral buttons use label-size semibold text, shared control corners and 8px 12px padding. They have a 40px minimum height; shared buttons become 44px at 600px and below. Neutral hover uses inset with an accent border; primary hover deepens the fill. Press moves enabled buttons down 1px; disabled buttons use half opacity and a not-allowed cursor. Destructive text remains distinct.

### Inputs / Fields

Fields use white surfaces, strong borders and the control radius, with a 40px minimum height. Native selects remain native. Checkbox and radio controls are 18px. Document toolbar fields are denser at 36px on desktop and 44px on small screens. Field labels remain visible; placeholders use secondary text.

All buttons, anchors, inputs, selects, textareas and summaries receive a visible 2px focus outline with a 3px offset.

### Navigation

Named destinations use matching SVG line icons, 44px minimum targets and active text/background. Desktop destinations stack vertically; smaller screens scroll horizontally. The current destination is exposed through aria-current.

### Chips

Bank tags use small text, accent-soft fill and accent-hover text, with compact padding. Existing semantic generation/review statuses retain their own colors. Count badges use inset fill.

### Cards / Containers

Bank cards and inspector panels use surface backgrounds, light boundaries, panel corners and 16px padding. Import and analysis panels use 24px padding where space permits. Cards do not require ornamental shadows.

### Disclosures and inspector groups

Native details/summary preserve keyboard interaction. Saved-item advanced controls remain discoverable under named summaries. Document inspector groups are output/paper, margins/spacing, instructions/header/page number, question breaks and figure size; the first two open initially. Group fields use two columns, with wide controls spanning both.

### Paper canvas and overlays

The existing editable document DOM remains the paper canvas. Its padding steps from 32px to 24px below 1180px and 16px at 600px; its minimum height is 600px. The separate preview remains two-column output. Pickers, read dialogs and the math dialog use shared panel corners and overlay depth. Equation options use labeled native fields.

Motion is limited to existing state feedback. Reduced-motion mode removes transition duration and smooth scrolling.

## Do's and Don'ts

### Do:

- **Do** use the shared application tokens and native semantic controls.
- **Do** preserve Korean labels, long-name wrapping and visible keyboard focus.
- **Do** group controls by task and keep advanced actions discoverable.
- **Do** keep original question images, equations and editable paper content readable.
- **Do** verify all seven views plus their populated editors and dialogs at desktop, tablet and mobile widths.

### Don't:

- **Don't** apply workspace layout rules to print or export templates.
- **Don't** turn compact mobile navigation into whole-page horizontal overflow.
- **Don't** introduce a column-count selector; output remains two columns with continuous question numbering.
- **Don't** replace the shared editing draft, existing IDs or control listeners as a visual redesign shortcut.

Recorded validation: .omo/ui-rebuild/round3/report.json contains all seven views at 1440, 768 and 375px, no page-width overflow, no reported errors, and a two-column preview. Mobile off-viewport element entries are inside the scrollable navigation. extra-report.json adds exam editor, picker, expanded inspector, math dialog, saved editor and populated analysis at those widths with no reported overflow/errors. Exam and analysis records explicitly use fixtures. These reports support the recorded states; they are not deployment or exhaustive export verification.
