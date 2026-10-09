"""Explicit project information (publisher / target / subject / semester).

AI tagging and AI explanations need the school level, grade and subject of a
project. Historically these were guessed from the project name; storing them
explicitly lets the user rename projects freely.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# target -> (school_level, grade)
TARGETS: dict[str, tuple[str, str]] = {
    "중1": ("middle", "1"),
    "중2": ("middle", "2"),
    "중3": ("middle", "3"),
    "고1": ("high", "1"),
    "고2이상": ("high", "2"),
}
SUBJECTS_BY_TARGET: dict[str, tuple[str, ...]] = {
    "중1": ("과학",),
    "중2": ("과학",),
    "중3": ("과학",),
    "고1": ("통합과학1", "통합과학2"),
    "고2이상": ("물리학", "역학과 에너지", "전자기와 양자"),
}
SEMESTERS = ("1학기", "2학기")
INFO_FIELDS = ("title", "publisher", "target", "subject", "semester")


class ProjectInfoError(ValueError):
    pass


def _text(value: Any, field: str, *, limit: int, required: bool) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ProjectInfoError(f"{field}을(를) 입력해 주세요.")
        return None
    if not isinstance(value, str):
        raise ProjectInfoError(f"{field}은(는) 문자열이어야 합니다.")
    text = " ".join(value.split())
    if len(text) > limit or re.search(r"[\x00-\x1f\x7f-\x9f]", text):
        raise ProjectInfoError(f"{field}은(는) {limit}자 이하로 입력해 주세요.")
    return text


def target_of(metadata: Mapping[str, Any]) -> str | None:
    level, grade = metadata.get("school_level"), str(metadata.get("grade") or "")
    for target, pair in TARGETS.items():
        if pair == (level, grade):
            return target
    if level == "high" and grade == "3":
        return "고2이상"
    return None


def normalize_info(values: Mapping[str, Any], *, require_title: bool) -> dict[str, Any]:
    """Validate user input. Returns only the keys that were supplied."""

    unknown = set(values) - set(INFO_FIELDS)
    if unknown:
        raise ProjectInfoError("지원하지 않는 프로젝트 정보 항목이 있습니다.")
    result: dict[str, Any] = {}
    if "title" in values or require_title:
        result["title"] = _text(values.get("title"), "프로젝트 이름", limit=200, required=True)
    if "publisher" in values:
        result["publisher"] = _text(values.get("publisher"), "출판사", limit=100, required=False)
    if "target" in values or "subject" in values:
        target = values.get("target")
        subject = values.get("subject")
        if target is None and subject is None:
            result["target"] = None
            result["subject"] = None
        else:
            if target not in TARGETS:
                raise ProjectInfoError("대상 학년을 선택해 주세요.")
            if subject not in SUBJECTS_BY_TARGET[target]:
                raise ProjectInfoError(f"{target} 과목을 선택해 주세요.")
            result["target"] = target
            result["subject"] = subject
    if "semester" in values:
        semester = values.get("semester")
        if semester in (None, "", "전체"):
            semester = None
        elif semester not in SEMESTERS:
            raise ProjectInfoError("학기는 전체, 1학기, 2학기 중에서 선택해 주세요.")
        result["semester"] = semester
    return result


def apply_info(metadata: dict[str, Any], info: Mapping[str, Any]) -> None:
    """Write normalized info into project metadata (schema field names)."""

    if "title" in info:
        metadata["filename_base"] = info["title"]
        metadata["book_title"] = info["title"]
    if "publisher" in info:
        metadata["publisher"] = info["publisher"]
    if "target" in info:
        if info["target"] is None:
            metadata["school_level"] = None
            metadata["grade"] = None
            metadata["subject"] = None
        else:
            level, grade = TARGETS[info["target"]]
            metadata["school_level"] = level
            metadata["grade"] = grade
            metadata["subject"] = info["subject"]
    if "semester" in info:
        metadata["semester"] = info["semester"]


def semester_number(metadata: Mapping[str, Any]) -> int | None:
    value = str(metadata.get("semester") or "")
    if value.startswith("1"):
        return 1
    if value.startswith("2"):
        return 2
    return None
