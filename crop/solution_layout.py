from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PageLayoutObservation:
    page_index: int
    stream_id: str
    heading_signatures: tuple[tuple[str, ...], ...]
    first_number: tuple[int, ...] | None
    last_number: tuple[int, ...] | None
    reset_detected: bool


def running_furniture_tokens(
    observations: tuple[PageLayoutObservation, ...],
) -> frozenset[tuple[str, tuple[str, ...]]]:
    pages_by_heading: dict[tuple[str, tuple[str, ...]], set[int]] = defaultdict(set)
    continuations_by_heading: dict[tuple[str, tuple[str, ...]], int] = defaultdict(int)
    previous_by_stream: dict[str, PageLayoutObservation] = {}
    seen_by_stream: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    furniture_start_evidence: set[tuple[str, tuple[str, ...]]] = set()
    for observation in sorted(
        observations, key=lambda item: (item.stream_id, item.page_index)
    ):
        previous = previous_by_stream.get(observation.stream_id)
        continuing = bool(
            previous is not None
            and observation.page_index == previous.page_index + 1
            and not observation.reset_detected
            and observation.first_number is not None
            and previous.last_number is not None
            and observation.first_number > previous.last_number
        )
        headings = set(observation.heading_signatures)
        seen = seen_by_stream[observation.stream_id]
        for heading in headings:
            key = (observation.stream_id, heading)
            pages_by_heading[key].add(observation.page_index)
            if continuing:
                continuations_by_heading[key] += 1
            if not seen or (headings - {heading}) & seen:
                furniture_start_evidence.add(key)
        seen.update(headings)
        previous_by_stream[observation.stream_id] = observation
    return frozenset(
        key
        for key, pages in pages_by_heading.items()
        if (
            len(pages) >= 3
            and continuations_by_heading[key] >= 2
            and key in furniture_start_evidence
        )
    )
