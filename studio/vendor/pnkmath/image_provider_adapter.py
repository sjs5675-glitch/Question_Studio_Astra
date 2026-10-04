"""Astra adaptation of PNKmath/ngd-studio CodexCliImageProvider at cafa7e70."""

from __future__ import annotations

import os
import hashlib
import io
import json
import re
from contextlib import contextmanager
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterator

from PIL import Image, UnidentifiedImageError
from astra_cli.contracts import MODEL


def _read_image_bytes(path: Path) -> tuple[bytes | None, str]:
    for attempt in range(10):
        try:
            if path.is_symlink():
                return None, "unsafe_image_path"
            if not path.is_file():
                return None, "missing_image"
            size = path.stat().st_size
            if not 0 < size <= 32 * 1024 * 1024:
                return None, "image_size_out_of_range"
            raw = path.read_bytes()
            return (raw, "") if len(raw) == size else (None, "image_size_changed")
        except PermissionError as exc:
            if exc.errno != 13 and getattr(exc, "winerror", None) != 32:
                raise
            if attempt == 9:
                return None, "output_locked"
            time.sleep(0.5)
    return None, "output_locked"


def _selected_generated_image(events: bytes) -> tuple[bytes | None, dict[str, str] | None, str]:
    if len(events) > 8 * 1024 * 1024:
        return None, None, "event_stream_too_large"
    try:
        lines = events.decode("utf-8").splitlines()
        parsed = [json.loads(line) for line in lines if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None, "invalid_event_stream"
    threads = {event.get("thread_id") for event in parsed
               if isinstance(event, dict) and event.get("type") == "thread.started"}
    if len(threads) != 1:
        return None, None, "ambiguous_thread"
    thread_id = next(iter(threads))
    if not isinstance(thread_id, str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", thread_id):
        return None, None, "invalid_thread_id"
    if not any(isinstance(event, dict) and event.get("type") == "turn.completed" for event in parsed):
        return None, None, "turn_incomplete"
    messages = [event["item"].get("text") for event in parsed
                if isinstance(event, dict) and event.get("type") == "item.completed"
                and isinstance(event.get("item"), dict) and event["item"].get("type") == "agent_message"]
    if not messages or not isinstance(messages[-1], str) or "\n" in messages[-1].strip():
        return None, None, "missing_selected_path"
    selected = Path(messages[-1].strip())
    if not selected.is_absolute() or ".." in selected.parts or not re.fullmatch(
        r"exec-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.png", selected.name
    ):
        return None, None, "unsafe_selected_path"
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    folder = home / "generated_images" / thread_id
    if (folder.is_symlink() or selected.is_symlink()
            or selected.parent.resolve() != folder.resolve()):
        return None, None, "path_outside_thread"
    try:
        raw, read_error = _read_image_bytes(selected)
        if read_error:
            return None, None, "selected_image_locked" if read_error == "output_locked" else read_error
        if raw is None or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return None, None, "invalid_png_signature"
        with Image.open(io.BytesIO(raw)) as opened:
            if opened.format != "PNG" or opened.width > 4096 or opened.height > 4096:
                return None, None, "invalid_png_dimensions"
            opened.verify()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return None, None, "invalid_generated_image"
    return raw, {"thread_id": thread_id, "selected_name": selected.name,
                 "sha256": hashlib.sha256(raw).hexdigest()}, ""


@contextmanager
def _temporary_image_workspace(ref_path: Path) -> Iterator[Path]:
    workdir: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="astra-codex-clean-", ignore_cleanup_errors=True) as directory:
            workdir = Path(directory)
            yield workdir
    finally:
        if workdir is not None and workdir.exists():
            try:
                ref_path.with_suffix(".codex-cleanup-pending.json").write_text(
                    json.dumps({"temporary_directory": str(workdir)}), encoding="utf-8")
            except OSError:
                pass


class CodexCliImageProvider:
    label = "Codex CLI ImageGen"

    def __init__(self, codex_bin: str | None = None) -> None:
        if codex_bin is not None:
            self.codex_bin = codex_bin
        else:
            from astra_cli import AstraError
            from astra_cli.isolation import native_executable
            try:
                self.codex_bin = str(native_executable())
            except AstraError as exc:
                raise FileNotFoundError("codex_cli_unavailable") from exc

    def clean_image(self, ref_path: Path, ar: str, prompt: str) -> tuple[bytes | None, str | None]:
        with _temporary_image_workspace(ref_path) as workdir:
            source = workdir / "input.png"
            target = workdir / "output.png"
            shutil.copyfile(ref_path, source)
            task = (
                "Use the built-in image generation/editing tool to edit the attached image. "
                "Use the input as the edit target and preserve its exact composition and aspect ratio "
                f"({ar}). {prompt}\n"
                "Select exactly one generated PNG. Copy it to the exact output path below if possible. "
                "Your final response must be ONLY the absolute path of that selected PNG under "
                "$CODEX_HOME/generated_images/<this thread id>/, without Markdown or extra text. "
                "If image editing is unavailable, leave the output absent and reply NO_IMAGE.\n"
                f"Output path: {target}"
            )
            command = [self.codex_bin, "exec", "--ephemeral", "--skip-git-repo-check",
                       "--json", "--sandbox", "workspace-write", "--enable", "image_generation",
                       "--model", MODEL,
                       "--cd", str(workdir), "--image", str(source), "-"]
            selection_error: str | None = None
            def failure(code: str, result: subprocess.CompletedProcess[bytes] | None = None,
                        raw_output: bytes | None = None, inspect_target: bool = True) -> tuple[None, str]:
                detail = {"error_code": code, "returncode": result.returncode if result else None,
                          "stdout_bytes": len(result.stdout or b"") if result else 0,
                          "stderr_bytes": len(result.stderr or b"") if result else 0}
                if selection_error is not None:
                    detail["selection_error"] = selection_error
                observed, read_error = _read_image_bytes(target) if inspect_target and raw_output is None else (raw_output, "")
                if read_error and read_error != "missing_image":
                    detail["output_read_error"] = read_error
                if observed is not None:
                    size = len(observed)
                    detail["output_bytes"] = size
                    if size <= 32 * 1024 * 1024:
                        raw = observed
                        detail["output_sha256"] = hashlib.sha256(raw).hexdigest()
                        detail["output_prefix_hex"] = raw[:12].hex()
                        archived = ref_path.with_suffix(".invalid-output.bin")
                        try:
                            archived.write_bytes(raw)
                            detail["artifact_name"] = archived.name
                        except OSError as exc:
                            detail["archive_error"] = type(exc).__name__
                ref_path.with_suffix(".codex-diagnostic.json").write_text(
                    json.dumps(detail, ensure_ascii=False), encoding="utf-8")
                return None, code
            try:
                result = subprocess.run(command, input=task.encode("utf-8"), capture_output=True,
                                        timeout=900, check=False)
            except subprocess.TimeoutExpired:
                return failure("codex_cli_timeout")
            except OSError:
                return failure("codex_cli_launch_failed")
            if result.returncode != 0:
                return failure("codex_cli_failed", result)
            direct_data, direct_error = _read_image_bytes(target)
            if direct_error and direct_error not in {"missing_image", "output_locked"}:
                selection_error = direct_error
                return failure("codex_cli_invalid_image", result, inspect_target=False)
            if direct_data is None:
                data, receipt, selection_error = _selected_generated_image(result.stdout or b"")
                if data is not None and receipt is not None:
                    try:
                        ref_path.with_suffix(".codex-selection.json").write_text(
                            json.dumps(receipt), encoding="utf-8")
                    except OSError:
                        pass
                    return data, None
                if direct_error == "output_locked":
                    selection_error = f"direct_output_locked:{selection_error}"
                    return failure("codex_cli_output_locked", result, inspect_target=False)
                return failure("codex_cli_no_image", result)
            try:
                with Image.open(io.BytesIO(direct_data)) as opened:
                    if opened.format not in {"PNG", "JPEG", "WEBP"}:
                        return failure("codex_cli_invalid_image", result, raw_output=direct_data)
                    opened.verify()
                return direct_data, None
            except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
                return failure("codex_cli_invalid_image", result, raw_output=direct_data)
