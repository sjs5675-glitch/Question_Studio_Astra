"""Small JSON-over-HTTP boundary for transcription providers."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any


MAX_HTTP_RESPONSE_BYTES = 10 * 1024 * 1024
JsonPost = Callable[
    [str, Mapping[str, str], Mapping[str, Any], float], Mapping[str, Any]
]


class ProviderHTTPError(RuntimeError):
    pass


def _error_detail(raw: bytes) -> str:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return raw.decode("utf-8", errors="replace").strip()[:500]
    if isinstance(value, Mapping):
        error = value.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("message"), str):
            return error["message"][:500]
        if isinstance(error, str):
            return error[:500]
    return str(value)[:500]


def post_json(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: float,
    *,
    max_response_bytes: int = MAX_HTTP_RESPONSE_BYTES,
) -> Mapping[str, Any]:
    """POST one bounded JSON document using urllib."""

    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ProviderHTTPError("HTTP timeout must be positive")
    if (
        isinstance(max_response_bytes, bool)
        or not isinstance(max_response_bytes, int)
        or max_response_bytes < 1
    ):
        raise ProviderHTTPError("HTTP response limit must be positive")
    try:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProviderHTTPError(f"provider request is not valid JSON: {exc}") from exc
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Question-JSON-Studio/0.1",
            **dict(headers),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.getcode()
            raw = response.read(max_response_bytes + 1)
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc.read(4096))
        suffix = f": {detail}" if detail else ""
        raise ProviderHTTPError(f"provider HTTP {exc.code}{suffix}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise ProviderHTTPError(f"provider network request failed: {exc}") from exc
    if status < 200 or status >= 300:
        raise ProviderHTTPError(f"provider HTTP {status}")
    if not raw:
        raise ProviderHTTPError("provider returned an empty HTTP response")
    if len(raw) > max_response_bytes:
        raise ProviderHTTPError("provider HTTP response exceeds the safety limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProviderHTTPError("provider returned invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise ProviderHTTPError("provider response JSON root must be an object")
    return value
