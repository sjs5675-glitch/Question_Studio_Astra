# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# Run: .venv/Scripts/python.exe -B launch_astra.py --preflight
from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
from dataclasses import replace
from pathlib import Path

from astra_runtime import ROOT, IsolationError, choose_ports, configure_process, inside, load_settings, port_number, summary, validate_parsed_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated Astra launch/preflight; original services are never stopped")
    parser.add_argument("--component", choices=("crop", "studio", "web"), default="studio")
    parser.add_argument("--preflight", action="store_true")
    lifecycle = parser.add_mutually_exclusive_group()
    lifecycle.add_argument("--owned", action="store_true")
    lifecycle.add_argument("--stop-owned", action="store_true")
    parser.add_argument("--owner-id", default="studio")
    parser.add_argument("--workspace")
    parser.add_argument("--crop-root")
    parser.add_argument("--import-root", action="append")
    parser.add_argument("--pdf-root", action="append")
    parser.add_argument("--web-root")
    parser.add_argument("--recovery-root")
    parser.add_argument("--port")
    arguments = parser.parse_args(argv)
    try:
        validate_parsed_paths(arguments)
        settings = load_settings()
        if arguments.owned or arguments.stop_owned:
            from studio_owned_runtime import owner_directory, stop_owned

            if arguments.component != 'studio':
                raise IsolationError('owned', 'owned lifecycle is limited to the new Studio')
            owner_directory(settings, arguments.owner_id)
            if arguments.stop_owned:
                return stop_owned(settings, arguments.owner_id)
        if arguments.workspace is not None:
            settings = replace(settings, workspace=inside(arguments.workspace, "workspace"))
        if arguments.crop_root is not None:
            settings = replace(settings, crop_root=inside(arguments.crop_root, "crop_root"))
        if arguments.port is not None:
            settings = replace(settings, **{f"{arguments.component}_port": port_number(arguments.port, "port")})
        if len({settings.crop_port, settings.studio_port, settings.web_port}) != 3:
            raise IsolationError("ports", "service ports must be distinct")
        if Path(sys.executable).resolve() != settings.python.resolve():
            raise IsolationError("python", "use C:/Question_Studio_Astra/.venv/Scripts/python.exe")
        if not settings.python.is_file():
            raise IsolationError("python", "run uv sync in the new root before launching")
        for path in (settings.crop_root / "app_server.py", settings.studio_root / "app/server.py"):
            if not inside(path, "server").is_file():
                raise IsolationError("server", "copied server source is missing")
        configure_process(settings)
        settings = choose_ports(settings, component=arguments.component)
    except (IsolationError, OSError, ValueError) as exc:
        print(json.dumps({"status": "preflight_failed", "error": str(exc)}), file=sys.stderr)
        return 2
    if arguments.preflight:
        print(json.dumps({"status": "preflight_passed", **summary(settings)}, ensure_ascii=False))
        return 0
    if arguments.component == "web":
        parser.error("optional Crop web startup is pending the endpoint integration in Todo 10; preflight is available")
    os.environ.update({"ASTRA_WORKSPACE": str(settings.workspace), "ASTRA_CROP_ROOT": str(settings.crop_root), "ASTRA_CROP_PORT": str(settings.crop_port), "ASTRA_STUDIO_PORT": str(settings.studio_port), "ASTRA_WEB_PORT": str(settings.web_port)})
    os.environ.pop("PYTHONPATH", None)
    if arguments.component == "crop":
        os.chdir(settings.crop_root)
        sys.path.insert(0, str(settings.crop_root))
        sys.argv = [str(settings.crop_root / "app_server.py"), "--host", "127.0.0.1", "--port", str(settings.crop_port)]
        runpy.run_path(sys.argv[0], run_name="__main__")
    else:
        os.chdir(settings.studio_root)
        sys.path.insert(0, str(settings.studio_root))
        sys.argv = ["app.server", "--port", str(settings.studio_port), "--workspace", str(settings.workspace), "--crop-root", str(settings.crop_root)]
        for name, values in (("--import-root", arguments.import_root or [str(settings.crop_root)]), ("--pdf-root", arguments.pdf_root or [str(settings.workspace / ".uploads")])):
            for value in values:
                sys.argv.extend((name, value))
        for name, value in (("--web-root", arguments.web_root), ("--recovery-root", arguments.recovery_root)):
            if value is not None:
                sys.argv.extend((name, value))
        if arguments.owned:
            from studio_owned_runtime import serve_owned

            try:
                return serve_owned(settings, arguments)
            except (IsolationError, OSError, ValueError) as exc:
                print(json.dumps({'status': 'owned_failed', 'error': str(exc)}), file=sys.stderr)
                return 2
        runpy.run_module("app.server", run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
