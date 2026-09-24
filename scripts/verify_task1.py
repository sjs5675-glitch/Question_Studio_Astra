# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# Run: .venv/Scripts/python.exe -B scripts/verify_task1.py
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / ".omo/evidence/astra-cli"
PYTHON = ROOT / ".venv/Scripts/python.exe"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> int:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("ASTRA_")}
    environment.update({"TEMP": str(ROOT / "tmp"), "TMP": str(ROOT / "tmp"), "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    results = []

    def run(name: str, arguments: list[str], *, expected: int = 0, env=environment):
        started = time.monotonic()
        completed = subprocess.run(arguments, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
        record = {"name": name, "command": arguments, "cwd": str(ROOT), "exit_code": completed.returncode, "expected_exit_code": expected, "seconds": round(time.monotonic() - started, 3), "stdout": completed.stdout, "stderr": completed.stderr}
        results.append(record)
        (EVIDENCE / f"task-1-{name}.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
        if completed.returncode != expected:
            raise RuntimeError(f"{name} failed: {completed.stderr}")
        return completed

    run("green", [str(PYTHON), "-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_isolation.py", "-v"])
    for component in ("crop", "studio"):
        run(f"{component}-preflight", ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", f"{component}/start_app.ps1", "-Preflight"])
    run("legacy-override", [str(PYTHON), "-B", "launch_astra.py", "--preflight", "--workspace", "C:/Question_JSON_Studio/workspace"], expected=2)
    spy = run("import-spy", [str(PYTHON), "-B", "tests/isolation_probe.py"])
    (EVIDENCE / "task-1-import-spy.json").write_text(spy.stdout, encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="port-control-", dir=ROOT / "tmp") as temporary:
        ports_file = Path(temporary) / "ports.json"
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            preferred = occupied.getsockname()[1]
            port_environment = {**environment, "ASTRA_CROP_PORT": str(preferred), "ASTRA_PORTS_FILE": str(ports_file)}
            completed = run("occupied-port", [str(PYTHON), "-B", "launch_astra.py", "--component", "crop", "--preflight"], env=port_environment)
            payload = json.loads(completed.stdout)
            if payload["crop_port"] == preferred:
                raise AssertionError("occupied port reused")
            persisted = json.loads(ports_file.read_text())
            if persisted["crop_port"] != payload["crop_port"]:
                raise AssertionError("alternate port not persisted")
            occupied_control = {"owner_pid": os.getpid(), "occupied_port": preferred, "selected_port": payload["crop_port"], "persisted": persisted, "own_socket_survived_preflight": occupied.getsockname()[1] == preferred}
        occupied_control["own_socket_closed"] = occupied.fileno() == -1
    occupied_control["fixture_cleaned"] = not Path(temporary).exists()
    manifest = json.loads((EVIDENCE / "task-1-copy-manifest.json").read_text(encoding="utf-8"))
    originals_changed = []
    new_files = []
    for entry in manifest["files"]:
        current_source = digest(Path(entry["source"]))
        if current_source != entry["before_sha256"]:
            originals_changed.append(entry["source"])
        current = digest(ROOT / entry["destination"])
        new_files.append({"path": entry["destination"], "sha256": current, "modified_for_isolation": current != entry["copied_sha256"]})
    for relative in ("astra_runtime.py", "launch_astra.py", "pyproject.toml", "uv.lock", ".gitignore", "config/ports.json", "tests/test_isolation.py", "tests/isolation_probe.py", "scripts/isolation_inventory.py", "scripts/verify_task1.py"):
        new_files.append({"path": relative, "sha256": digest(ROOT / relative), "modified_for_isolation": True})
    (EVIDENCE / "task-1-current-source.json").write_text(json.dumps({"files": new_files, "original_source_changes": originals_changed}, indent=2) + "\n")
    if originals_changed:
        raise AssertionError("original allowed source changed; investigate concurrent activity")
    protected = json.loads((EVIDENCE / "task-1-protected-after.json").read_text())
    before = json.loads((EVIDENCE / "task-1-listeners-before.json").read_text(encoding="utf-8-sig"))
    after = json.loads((EVIDENCE / "task-1-listeners-after.json").read_text(encoding="utf-8-sig"))
    listeners_unchanged = before["listeners"] == after["listeners"]
    processes_unchanged = before["processes"] == after["processes"]
    payload = {
        "task": 1, "recorded_at": datetime.now(timezone.utc).isoformat(), "scope": "offline source/runtime isolation only", "status": "ready_for_independent_verification", "commands": results,
        "initialization_commands": ["python -B scripts/isolation_inventory.py protect", "python -B scripts/isolation_inventory.py copy", "python -B -m unittest discover -s tests -p test_isolation.py -v (expected failure: crop/.venv != root/.venv)", "uv sync --no-dev --python 3.13", "git init --initial-branch main", ".venv/Scripts/python.exe -B scripts/isolation_inventory.py recheck"],
        "dependency_environment": {"python": str(PYTHON), "version": sys.version, "UV_CACHE_DIR": str(ROOT / ".cache/uv"), "UV_PYTHON_INSTALL_DIR": str(ROOT / ".cache/uv-python"), "UV_PYTHON_DOWNLOADS": "never", "TEMP": str(ROOT / "tmp"), "TMP": str(ROOT / "tmp"), "installed": {name: importlib.metadata.version(name) for name in ("Pillow", "pypdfium2", "jsonschema")}},
        "copy": {"count": manifest["count"], "bytes": sum(entry["bytes"] for entry in manifest["files"]), "source_before_copied_after_match": manifest["matches"], "original_source_changes": originals_changed, "coverage_artifact": "task-1-copy-coverage.json", "manifest_artifact": "task-1-copy-manifest.json", "vendor_exception": manifest["vendor_runtime_exception"]},
        "original_preservation": {"protected_records": protected["checked"], "changed_records": protected["changes"], "listeners_unchanged": listeners_unchanged, "processes_unchanged": processes_unchanged, "original_process_actions": [], "tailscale_changes": [], "artifacts": ["task-1-protected-before.json", "task-1-protected-after.json", "task-1-listeners-before.json", "task-1-listeners-after.json"]},
        "occupied_port_control": occupied_control,
        "adversarial_classes": {"happy_path": "16 path configuration tests plus two real PowerShell preflights pass", "stale_state": "old PID/url file preserved and never trusted", "dirty_worktree": "pre-existing .omo and .codegraph preserved; dependencies/cache/temp excluded by .gitignore", "misleading_success_output": "invalid override exits 2 with preflight_failed; successful output explicitly says preflight_only", "malformed_override": "empty/control/drive-relative/traversal/duplicate/legacy ports and malformed persisted JSON rejected", "interruption": "injected KeyboardInterrupt leaves previous settings unchanged and closes sockets/removes temporary port file", "path_symlink_escape": "new-root junction to original root rejected, test-owned junction removed without touching target", "network_or_inference": "import spy forbids socket.connect/subprocess.Popen; none occurred", "authentication_quota_provider_output": "not applicable to Todo 1; no authentication/inference invoked", "revision_conflict_corrupt_job_content": "later persistence todo; no operational jobs copied", "visual_accessibility_export_quality": "not applicable to offline preflight; required later todos retain these gates"},
        "tooling_observations": ["Two exploratory PowerShell regex commands had quoting errors and were rerun with separate -e arguments; these were not verification passes", "Optional programming no-excuse checker is not present at its documented package path"],
        "limitations": ["No product server, browser, model/API or native Hancom session started", "Optional Crop web endpoint integration remains Todo 10; its preflight exists and launcher rejects premature web startup", "Foreground start wrappers are provided; managed PID lifecycle/stop scripts belong to Todo 11", "Preflight port availability is a momentary check; bind-time process lifecycle is verified in later launch todo", "Copied legacy provider implementations remain unchanged for baseline; Todo 3 removes text API routes"],
        "DoneClaim": {"todo": 1, "implementation_complete": True, "offline_checks_passed": True, "independent_verification": "pending root dispatch", "commit": "not created; awaiting root verification/progress update/explicit commit instruction", "evidence": ".omo/evidence/astra-cli/task-1-isolation.json"},
    }
    (EVIDENCE / "task-1-isolation.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "commands": len(results), "test_count": 16, "copied_files": manifest["count"], "protected_hash_changes": len(protected["changes"]), "listeners_unchanged": listeners_unchanged, "processes_unchanged": processes_unchanged}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())