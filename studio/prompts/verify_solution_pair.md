Compare image 0 (the actual question crop) with image 1 (the currently selected solution crop).
The OCR text is supplementary evidence; the images are authoritative. Determine whether the solution
actually belongs to this question by comparing visible subject matter, numbers, formulas, figures,
conditions and answer. Cite the image index and concrete visible details in evidence. A matching printed
number alone never proves a match. Missing page references, headings, or answers mean unknown, not
mismatch. A visible number recognition error may be source_number_issue only with specific evidence of
matching body content. Check clipped formulas and intrusion from adjacent questions. Never invent a
solution or change the canonical question number. Set crop_repair to null unless image 1 visibly clips
text, a formula, or a diagram at exactly one edge while the question and solution content match and
there is no neighboring question content. If so, name that edge and quote the visible fragment touching
it in visible_evidence; image_index must be 1. Do not infer missing words, guess a box size, or request
repair based only on OCR. Return only the requested JSON.
