from dataclasses import dataclass
from typing import TypeAlias


BoundingBox: TypeAlias = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class AnswerCell:
    entry_id: str
    bbox: BoundingBox | None


def isolated_answer_cell_bbox(
    block_bbox: BoundingBox,
    entries: tuple[AnswerCell, ...],
    target_entry_id: str,
) -> BoundingBox | None:
    if not _valid_bbox(block_bbox):
        return None
    targets = tuple(entry for entry in entries if entry.entry_id == target_entry_id)
    if len(targets) != 1 or any(entry.bbox is None for entry in entries):
        return None
    target = targets[0].bbox
    if target is None or not _inside(target, block_bbox):
        return None
    peers = tuple(entry.bbox for entry in entries if entry is not targets[0])
    if any(
        peer is None
        or not _inside(peer, block_bbox)
        or _overlaps(target, peer)
        for peer in peers
    ):
        return None
    return target


def _valid_bbox(value: BoundingBox) -> bool:
    left, top, right, bottom = value
    return 0 <= left < right <= 1000 and 0 <= top < bottom <= 1000


def _inside(value: BoundingBox, container: BoundingBox) -> bool:
    if not _valid_bbox(value):
        return False
    left, top, right, bottom = value
    outer_left, outer_top, outer_right, outer_bottom = container
    return (
        outer_left <= left < right <= outer_right
        and outer_top <= top < bottom <= outer_bottom
    )


def _overlaps(left: BoundingBox, right: BoundingBox) -> bool:
    return (
        max(left[0], right[0]) < min(left[2], right[2])
        and max(left[1], right[1]) < min(left[3], right[3])
    )
