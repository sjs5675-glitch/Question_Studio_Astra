"""Only validated page counts and approved error codes cross the public boundary."""
from __future__ import annotations

from typing import NotRequired, TypedDict

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


class PageFailure(TypedDict):
    stage: str
    failed_pages: list[int]
    completed: int
    total: int
    error_code: str
    preserved: NotRequired[dict[str, int | bool]]


def parse_page_failure(value: JsonValue) -> PageFailure | None:
    if not isinstance(value, dict) or value.get('stage') not in {'detection', 'boundary_repair'}:
        return None
    stage = value['stage']
    pages, completed, total = value.get('failed_pages'), value.get('completed'), value.get('total')
    code = value.get('error_code')
    allowed_codes = ({'output_path_rejected', 'page_detection_failed'} if stage == 'detection'
                     else {'boundary_repair_failed'})
    if code not in allowed_codes:
        return None
    if not isinstance(pages, list) or not pages or any(type(page) is not int or page < 1 for page in pages):
        return None
    if len(set(pages)) != len(pages) or type(completed) is not int or type(total) is not int:
        return None
    if completed < 0 or total < 1 or completed + len(pages) != total:
        return None
    result: PageFailure = {'stage': stage, 'failed_pages': sorted(pages), 'completed': completed,
                           'total': total, 'error_code': code}
    if stage == 'boundary_repair':
        preserved = value.get('preserved')
        if not isinstance(preserved, dict) or set(preserved) != {
            'base_detections', 'repair_results', 'preliminary_crops', 'registered'
        }:
            return None
        if any(type(preserved.get(field)) is not int or preserved[field] < 0 for field in (
            'base_detections', 'repair_results', 'preliminary_crops'
        )) or preserved.get('registered') is not False:
            return None
        result['preserved'] = {field: preserved[field] for field in (
            'base_detections', 'repair_results', 'preliminary_crops', 'registered'
        )}
    return result


def page_failure_message(failure: PageFailure) -> str:
    pages = ', '.join(map(str, failure['failed_pages']))
    if failure['stage'] == 'boundary_repair':
        preserved = failure['preserved']
        return (f"경계 교정 실패 · {pages}쪽 / {failure['total']}쪽 중 {failure['completed']}쪽 교정 결과 보존 / "
                f"기본 분석 {preserved['base_detections']}쪽·초기 크롭 {preserved['preliminary_crops']}개 보존 / 문제은행 등록 전")
    action = ('저장 경로 확인이 필요합니다.' if failure['error_code'] == 'output_path_rejected'
              else '작업 로그를 확인해 주세요.')
    return f"{pages}쪽 분석 실패. {failure['total']}쪽 중 {failure['completed']}쪽 결과 보존. {action}"
