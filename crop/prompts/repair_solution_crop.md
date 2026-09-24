# Constraints

- This is a boundary-only repair for one already identified answer/solution. Do not relink it to another question.
- If the target identity is ambiguous, absent, or mixed with a different target in a way that cannot be separated, return `repairable=false` and `parts=[]`.
- Every `bbox` uses normalized `[x0,y0,x1,y1]` coordinates: left, top, right, bottom. Never return `[y0,x0,y1,x1]`.
- Use only solution PDF page indices visible in the attachment map. Preserve human reading order with `order=0,1,...`.
- Return the smallest complete crop, not a whole column/page. Do not clip a glyph, first line, last line, figure/table label, answer, or continuation.

# Goal

Repair only the failed crop boundary while preserving the supplied question and candidate identity.

For `quick_answer`, one final crop must contain the target printed number/label and its entire actual answer, while excluding previous and next entries. If the answer wraps around neighboring entries, use up to four non-overlapping parts on the same source page, in reading order, to compose that final crop. Do not return a bounding rectangle that includes neighbors between those parts.

For `detailed_solution`, the ordered parts must contain the target identifier/header, complete first and last lines, all explanation/equation/figure content needed for that entry, and any continuation, while excluding the next question header and explanation.

# Target

Question metadata:
`${QUESTION_METADATA}`

Failed candidate metadata:
`${FAILED_CANDIDATE}`

Crop-completeness evidence from the two independent verifiers:
`${FAILURE_EVIDENCE}`

# Attachments

`${ATTACHMENT_MAP}`

The first two images are the problem crop and failed final-DPI solution crop. Remaining images are solution page views for boundary repair.

# Output

Return exactly one JSON object matching the supplied schema. Keep `question_id`, `asset_id`, and `candidate_role` exactly as supplied. `parts` must be empty when `repairable=false`.

# Final check

Before returning `repairable=true`, confirm the crop starts at the target's first required pixel, ends after its last required pixel, includes its number and answer/content, excludes neighboring entries, and uses `[x0,y0,x1,y1]`.
