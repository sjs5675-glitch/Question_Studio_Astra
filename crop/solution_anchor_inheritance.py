from dataclasses import replace
from typing import Mapping, Sequence

from solution_link import ProblemBoundary, ProblemIndex, ProblemItem


def inherit_missing_anchors(
    index: ProblemIndex,
    source_pages: Mapping[str, int],
    boundaries: Sequence[ProblemBoundary] = (),
) -> ProblemIndex:
    """Inherit the nearest original span on a monotone run; keep its page window."""
    ordered = sorted(index.items, key=lambda item: item.sequence)
    pages = {page.pdf_index: page for page in index.page_map}
    runs: dict[str, int] = {}
    run = 0
    previous: ProblemItem | None = None
    for problem in ordered:
        page = pages.get(source_pages[problem.item_id])
        previous_page = pages.get(source_pages[previous.item_id]) if previous else None
        continues = (
            previous is not None and page is not None and previous_page is not None
            and page.segment_id == previous_page.segment_id
            and problem.parent_number.isdigit() and previous.parent_number.isdigit()
            and (
                int(problem.parent_number) > int(previous.parent_number)
                or problem.parent_number == previous.parent_number
                and problem.owner_id is not None and problem.owner_id == previous.owner_id
            )
            and not any(previous.sequence < boundary.sequence <= problem.sequence
                        for boundary in boundaries)
        )
        if not continues:
            run += 1
        runs[problem.item_id] = run
        previous = problem
    donors = [
        problem for problem in ordered
        if problem.anchor is not None and problem.anchor.resolved
        and problem.anchor.page_range.end is not None
        and problem.anchor.confidence >= 0.9
        and problem.anchor.segment_id != "ambiguous"
        and source_pages[problem.item_id] in pages
    ]
    result: list[ProblemItem] = []
    for problem in ordered:
        target_page = source_pages[problem.item_id]
        if problem.anchor is not None or not problem.crop_is_search_eligible:
            result.append(problem)
            continue
        candidates = [
            donor for donor in donors
            if runs[donor.item_id] == runs[problem.item_id]
            and source_pages[donor.item_id] != target_page
        ]
        if candidates:
            distance = min(abs(target_page - source_pages[donor.item_id]) for donor in candidates)
            nearest = [donor for donor in candidates
                       if abs(target_page - source_pages[donor.item_id]) == distance]
            windows = {
                (donor.anchor.segment_id, donor.anchor.reference_document,
                 donor.anchor.page_range.start, donor.anchor.page_range.end)
                for donor in nearest if donor.anchor is not None
            }
            if len(windows) == 1:
                donor = min(nearest, key=lambda value: source_pages[value.item_id])
                span = donor.anchor
                if span is not None:
                    reason = f"anchor_inherited_from_page_{source_pages[donor.item_id] + 1}"
                    problem = replace(problem, anchor=replace(
                        span, item_ids=(problem.item_id,), scope_id=problem.scope_id,
                        review_reasons=tuple(dict.fromkeys((*span.review_reasons, reason))),
                    ))
        result.append(problem)
    return replace(index, items=tuple(result))
