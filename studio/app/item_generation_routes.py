"""Authenticated HTTP routes for the '문항 생성' menu (/api/item-generation/...)."""
from __future__ import annotations

from http import HTTPStatus
from typing import Any

from .item_generation import (
    ItemGenerationConflict,
    ItemGenerationError,
    confirm_drafts,
    create_job,
    estimate_calls,
    item_analysis,
    job_dir,
    lab_inputs,
    list_jobs,
    list_scopes,
    load_job,
    search_topics,
    topic_tree,
    validate_request,
    analyses_for_unit,
    blocks_text_for_prompt,
)
from . import item_generation_pipeline as pipeline


def _int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        from .server import ApiError
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} 값이 올바르지 않습니다.")


def _source_items(major: str, middle: str) -> list[dict[str, Any]]:
    inputs = lab_inputs()
    rows = []
    for analysis in analyses_for_unit(major, middle):
        row = inputs.get(analysis["item_id"]) or {}
        rows.append({
            "item_id": analysis["item_id"],
            "printed_number": analysis.get("printed_number"),
            "detail_topic": (analysis.get("unit") or {}).get("detail_topic"),
            "difficulty": (analysis.get("difficulty") or {}).get("level"),
            "format": (analysis.get("structure") or {}).get("format"),
            "has_figure": bool(row.get("assets")),
            "answer_doubt": bool((analysis.get("answer_check") or {}).get("answer_doubt")),
            "preview": blocks_text_for_prompt((row.get("question") or {}).get("blocks"))[:220],
        })
    return rows


def handle_item_generation_api(handler: Any, method: str, parts: list[str], query: dict[str, str]) -> None:
    from .server import ApiError

    app = handler.app
    root = app.config.workspace_root
    try:
        rest = parts[2:]
        if rest == ["scopes"] and method == "GET":
            handler._send_json(HTTPStatus.OK, {"scopes": list_scopes()})
            return
        if rest == ["topics"] and method == "GET":
            scope = query.get("scope", "")
            if query.get("q"):
                handler._send_json(HTTPStatus.OK, {"results": search_topics(scope, query["q"])})
            else:
                handler._send_json(HTTPStatus.OK, topic_tree(scope))
            return
        if rest == ["sources"] and method == "GET":
            handler._send_json(HTTPStatus.OK, {"items": _source_items(query.get("major", ""), query.get("middle", ""))})
            return
        if len(rest) == 2 and rest[0] == "analysis" and method == "GET":
            analysis = item_analysis(rest[1])
            if analysis is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "분석 자료가 없습니다.")
            handler._send_json(HTTPStatus.OK, analysis)
            return
        if rest == ["guides"]:
            from .item_generation import append_guide_rule, guides_text
            if method == "GET":
                text, sha = guides_text(query.get("subject", ""))
                handler._send_json(HTTPStatus.OK, {"text": text, "sha256": sha})
                return
            if method == "POST":
                handler._send_json(HTTPStatus.OK, append_guide_rule(handler._read_json().get("rule", "")))
                return
        if rest == ["estimate"] and method == "POST":
            request = validate_request(handler._read_json())
            handler._send_json(HTTPStatus.OK, {"request": request, "estimate": estimate_calls(request)})
            return
        if rest == ["jobs"]:
            if method == "GET":
                handler._send_json(HTTPStatus.OK, {"jobs": list_jobs(root)})
                return
            if method == "POST":
                request = validate_request(handler._read_json())
                job = create_job(root, request)
                app.item_generation_worker.wake()
                handler._send_json(HTTPStatus.CREATED, job)
                return
        if len(rest) >= 2 and rest[0] == "jobs":
            job_id = rest[1]
            if len(rest) == 2 and method == "GET":
                from .item_generation_reviews import enrich_job
                handler._send_json(HTTPStatus.OK, enrich_job(root, load_job(root, job_id)))
                return
            if len(rest) == 3 and rest[2] == 'reviews':
                from .item_generation_reviews import enqueue, records
                if method == 'GET':
                    handler._send_json(HTTPStatus.OK, {'records': records(root, job_id)})
                    return
                if method == 'POST':
                    body = handler._read_json()
                    job = enqueue(root, job_id, body.get('numbers'), body.get('model'), body.get('expected_revision'))
                    app.item_generation_worker.wake()
                    handler._send_json(HTTPStatus.ACCEPTED, job)
                    return
            if len(rest) == 3 and rest[2] == "confirm" and method == "POST":
                body = handler._read_json()
                numbers = body.get("numbers")
                if not isinstance(numbers, list):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "numbers 목록이 필요합니다.")
                result = confirm_drafts(root, job_id, [_int(n, "numbers") for n in numbers],
                                        include_intent=body.get("include_intent", True) is not False,
                                        expected_revision=body.get('expected_revision'))
                handler._send_json(HTTPStatus.OK, result)
                return
            if len(rest) == 3 and rest[2] == "retry" and method == "POST":
                job = pipeline.resume_job(root, job_id, expected_revision=handler._read_json().get('expected_revision'))
                app.item_generation_worker.wake()
                handler._send_json(HTTPStatus.ACCEPTED, job)
                return
            if len(rest) == 5 and rest[2] == "drafts":
                no = _int(rest[3], "no")
                action = rest[4]
                if action == "figure" and method == "GET":
                    job = load_job(root, job_id)
                    draft = next((d for d in job["drafts"] if d["no"] == no), None)
                    figure = (draft or {}).get("figure") or {}
                    if not figure.get("file"):
                        raise ApiError(HTTPStatus.NOT_FOUND, "그림이 없습니다.")
                    base = job_dir(root, job_id).resolve()
                    from .item_generation_identity import safe_path
                    path = safe_path(base, figure['file'])
                    if base not in path.parents or path.is_symlink() or not path.is_file():
                        raise ApiError(HTTPStatus.NOT_FOUND, "그림이 없습니다.")
                    handler._send_file(path, "image/png", cache="no-cache")
                    return
                if method == "POST" and action == "revise":
                    body = handler._read_json()
                    job = pipeline.request_revision(root, job_id, no, body.get('instruction', ''), expected_revision=body.get('expected_revision'))
                    app.item_generation_worker.wake()
                    handler._send_json(HTTPStatus.ACCEPTED, job)
                    return
                if method == "POST" and action == "edit":
                    body = handler._read_json()
                    fields = body.get("fields")
                    if not isinstance(fields, dict):
                        raise ApiError(HTTPStatus.BAD_REQUEST, "fields 객체가 필요합니다.")
                    job = pipeline.edit_draft(root, job_id, no, fields, expected_revision=body.get('expected_revision'))
                    app.item_generation_worker.wake()
                    handler._send_json(HTTPStatus.ACCEPTED, job)
                    return
                if method == "POST" and action == "discard":
                    body = handler._read_json()
                    job = pipeline.set_discarded(root, job_id, no, bool(body.get('discarded', True)), expected_revision=body.get('expected_revision'))
                    handler._send_json(HTTPStatus.OK, job)
                    return
        if rest == ["chats"]:
            if method == "GET":
                from .item_generation_bot import list_chats
                handler._send_json(HTTPStatus.OK, {"chats": list_chats(root)})
                return
            if method == "POST":
                body = handler._read_json()
                request = {k: body.get(k) for k in ("scope", "major", "middle")}
                chat = app.item_generation_bots.create(request, body.get("message", ""))
                handler._send_json(HTTPStatus.CREATED, chat)
                return
        if len(rest) >= 2 and rest[0] == "chats":
            from .item_generation_bot import load_chat
            chat_id = rest[1]
            if len(rest) == 2 and method == "GET":
                handler._send_json(HTTPStatus.OK, load_chat(root, chat_id))
                return
            if len(rest) == 3 and rest[2] == "messages" and method == "POST":
                chat = app.item_generation_bots.send(chat_id, handler._read_json().get("message", ""))
                handler._send_json(HTTPStatus.ACCEPTED, chat)
                return
            if len(rest) == 3 and rest[2] == "import" and method == "POST":
                body = handler._read_json()
                chat = load_chat(root, chat_id)
                drafts = app.item_generation_bots.read_drafts(chat_id)
                request = validate_request({**chat["request"], "variants": 0,
                                            "new": max(1, min(10, len(drafts))),
                                            "difficulty": body.get("difficulty") or "중",
                                            "figure": "auto"})
                job = pipeline.import_bot_drafts(root, request, drafts, bot_session_id=chat.get("session_id"))
                app.item_generation_worker.wake()
                handler._send_json(HTTPStatus.CREATED, job)
                return
        raise ApiError(HTTPStatus.NOT_FOUND, "문항 생성 API 경로를 찾을 수 없습니다.")
    except ItemGenerationConflict as exc:
        raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
    except ItemGenerationError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
