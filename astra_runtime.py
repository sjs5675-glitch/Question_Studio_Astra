from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import socket
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parent
LEGACY_PORTS: Final = frozenset({3000, 8765, 8875})


@dataclass(frozen=True, slots=True)
class PathDiagnostic:
    raw: str
    resolved: str | None
    root: str


class IsolationError(ValueError):
    def __init__(self, setting: str, reason: str, *, path: PathDiagnostic | None = None) -> None:
        self.setting = setting
        self.reason = reason
        self.path = path
        super().__init__(f"{setting}: {reason}")


_EXTENDED_LOCAL_PATH = re.compile(r"^\\\\\?\\([A-Za-z]:\\.*)$")


def _local_path_for_comparison(value: str | Path, setting: str) -> Path:
    raw = str(value)
    match = _EXTENDED_LOCAL_PATH.fullmatch(raw)
    if match:
        return Path(match.group(1))
    if raw.startswith(("\\\\?\\", "\\\\.\\")):
        raise IsolationError(setting, "unsupported Windows device path")
    return Path(raw)


def inside(value: str | Path, setting: str) -> Path:
    raw = str(value)
    diagnostic = PathDiagnostic(raw, None, str(ROOT))
    if not raw or raw != raw.strip() or any(ord(character) < 32 for character in raw):
        raise IsolationError(setting, "empty, padded or control-character path", path=diagnostic)
    candidate = _local_path_for_comparison(raw, setting)
    if candidate.drive and not candidate.is_absolute():
        raise IsolationError(setting, "drive-relative paths are not supported", path=diagnostic)
    if any(":" in part or part.endswith((" ", ".")) for part in candidate.parts[1:] if part not in {".", ".."}):
        raise IsolationError(setting, "ambiguous Windows path", path=diagnostic)
    requested = Path(raw) if candidate.is_absolute() else ROOT / candidate
    resolved = requested.resolve()
    comparable = _local_path_for_comparison(resolved, setting)
    comparable_root = _local_path_for_comparison(ROOT.resolve(), setting)
    if not comparable.is_relative_to(comparable_root):
        raise IsolationError(setting, "path must resolve inside C:/Question_Studio_Astra", path=replace(diagnostic, resolved=str(resolved)))
    return comparable


def port_number(value: str | int, setting: str) -> int:
    if type(value) not in {str, int} or not str(value).isascii() or not str(value).isdecimal():
        raise IsolationError(setting, "port must be a decimal integer")
    result = int(value)
    if not 1024 <= result <= 65535 or result in LEGACY_PORTS:
        raise IsolationError(setting, "port must be 1024..65535 and must not use an original service port")
    return result


@dataclass(frozen=True, slots=True)
class Settings:
    crop_root: Path
    studio_root: Path
    workspace: Path
    temp: Path
    cache: Path
    runtime: Path
    learning_root: Path
    ports_file: Path
    crop_port: int
    studio_port: int
    web_port: int

    @property
    def python(self) -> Path:
        return ROOT / ".venv/Scripts/python.exe"

    @property
    def crop_api(self) -> str:
        return f"http://127.0.0.1:{self.crop_port}"


def load_settings(environment: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if environment is None else environment
    defaults = {"crop_root": "crop", "studio_root": "studio", "workspace": "studio/workspace", "temp": "tmp", "cache": ".cache", "runtime": ".runtime", "learning_root": "data/solution_learning", "ports_file": "config/ports.json"}
    paths = {name: inside(env.get(f"ASTRA_{name.upper()}", value), f"ASTRA_{name.upper()}") for name, value in defaults.items()}
    persisted = {}
    if paths["ports_file"].exists():
        try:
            persisted = json.loads(paths["ports_file"].read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IsolationError("ports_file", "unreadable or malformed port settings") from exc
        if type(persisted) is not dict or set(persisted) != {"crop_port", "studio_port", "web_port"}:
            raise IsolationError("ports_file", "expected exactly crop_port, studio_port and web_port")
    ports = {name: port_number(env.get(f"ASTRA_{name.upper()}", persisted.get(name, default)), name) for name, default in (("crop_port", 8766), ("studio_port", 8876), ("web_port", 3001))}
    if len(set(ports.values())) != 3:
        raise IsolationError("ports", "service ports must be distinct")
    return Settings(crop_root=paths["crop_root"], studio_root=paths["studio_root"], workspace=paths["workspace"], temp=paths["temp"], cache=paths["cache"], runtime=paths["runtime"], learning_root=paths["learning_root"], ports_file=paths["ports_file"], crop_port=ports["crop_port"], studio_port=ports["studio_port"], web_port=ports["web_port"])


def validate_parsed_paths(arguments: argparse.Namespace, paths: Sequence[str] = ("workspace", "web_root", "import_root", "import_roots", "pdf_root", "pdf_roots", "crop_root", "recovery_root", "out", "output", "run_dir", "output_run_dir", "overlay_dir")) -> None:
    for name in paths:
        value = getattr(arguments, name, None)
        if value is None:
            continue
        for path in value if isinstance(value, (list, tuple)) else (value,):
            inside(path, name)
            inside(Path(path).expanduser().resolve(), name)
    value = getattr(arguments, "port", None)
    if value is not None:
        port_number(value, "port")


def configure_process(settings: Settings) -> None:
    if os.name == "nt" and ctypes.windll.shell32.IsUserAnAdmin():
        raise IsolationError("privilege", "관리자 권한으로 실행할 수 없습니다. 일반 권한으로 실행해 주세요.")
    sys.dont_write_bytecode = True
    for directory in (settings.temp, settings.cache):
        directory.mkdir(parents=True, exist_ok=True)
    os.environ.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "TEMP": str(settings.temp), "TMP": str(settings.temp), "TMPDIR": str(settings.temp), "UV_CACHE_DIR": str(settings.cache / "uv"), "UV_PYTHON_INSTALL_DIR": str(settings.cache / "uv-python"), "UV_PYTHON_DOWNLOADS": "never", "npm_config_cache": str(settings.cache / "npm"), "XDG_CACHE_HOME": str(settings.cache), "WRANGLER_LOG_PATH": str(settings.cache / "wrangler/logs"), "MINIFLARE_REGISTRY_PATH": str(settings.cache / "miniflare")})
    tempfile.tempdir = str(settings.temp)


def bootstrap() -> Settings:
    settings = load_settings()
    configure_process(settings)
    return settings


def choose_ports(settings: Settings, *, component: str | None = None) -> Settings:
    selected = {}
    reservations = []
    try:
        for name in ("crop_port", "studio_port", "web_port"):
            preferred = port_number(getattr(settings, name), name)
            if component is not None and name != f"{component}_port":
                selected[name] = preferred
                continue
            listener = socket.socket()
            reservations.append(listener)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                listener.bind(("127.0.0.1", preferred))
            except OSError:
                listener.bind(("127.0.0.1", 0))
            selected[name] = port_number(listener.getsockname()[1], name)
        if len(set(selected.values())) != 3:
            raise IsolationError("ports", "service ports must be distinct")
        result = replace(settings, crop_port=selected["crop_port"], studio_port=selected["studio_port"], web_port=selected["web_port"])
        result.ports_file.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=result.ports_file.parent, prefix=".ports-", suffix=".tmp", delete=False) as stream:
                temporary_path = Path(stream.name)
                json.dump(selected, stream, indent=2)
                stream.write("\n")
            os.replace(temporary_path, result.ports_file)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return result
    finally:
        for listener in reservations:
            listener.close()


def summary(settings: Settings) -> dict[str, str | int | bool]:
    return {"root": str(ROOT), "preflight_only": True, **{key: str(value) if isinstance(value, Path) else value for key, value in asdict(settings).items()}, "python": str(settings.python), "crop_api": settings.crop_api}
