# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# Run: .venv/Scripts/python.exe -B scripts/isolation_inventory.py copy|recheck
"""Copy only approved sources and hash originals without importing their code."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
EVIDENCE: Final = ROOT / ".omo/evidence/astra-cli"
ORIGINALS: Final = {"crop": Path("C:/Question_Crop"), "studio": Path("C:/Question_JSON_Studio")}
EXCLUDED: Final = {
    ".env", ".venv", "node_modules", ".runtime", "app_data", "runs", "workspace",
    "tmp", ".codegraph", ".git", "__pycache__", ".cache", ".next", ".vinext",
    ".wrangler", "build", "dist", "output", ".pytest_cache", ".ruff_cache",
}
DIRECTORIES: Final = {
    "crop": {"prompts", "tests", "benchmarks", "web"},
    "studio": {"app", "schemas", "prompts", "tests", "examples", "templates", "vendor", "licenses", "scripts", "web", "docs"},
}
ROOT_FILES: Final = {"pyproject.toml", "uv.lock", "README.md", "SOLUTION_LINKING_PLAN.md", "requirements.txt", "start_app.ps1", "stop_app.ps1", ".env.example", "THIRD_PARTY_NOTICES.md"}
VENDOR_RUNTIME: Final = Path("vendor/exam_studio_equation/dist")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def linked(path: Path) -> bool:
    return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def files(root: Path, *, sources: bool):
    """Prune reparse points before descending; never follow junction targets."""
    for directory, directories, names in os.walk(root, followlinks=False):
        current = Path(directory)
        kept = []
        for name in sorted(directories):
            candidate = current / name
            relative = candidate.relative_to(root)
            if linked(candidate):
                continue
            if sources and name in EXCLUDED and relative != VENDOR_RUNTIME:
                continue
            if not sources and name in {".venv", "node_modules", ".git", ".codegraph", "tmp", "__pycache__"}:
                continue
            kept.append(name)
        directories[:] = kept
        for name in sorted(names):
            path = current / name
            if not linked(path):
                yield path


def allowed(name: str, relative: Path) -> bool:
    if relative.name.startswith(".env") and relative.name != ".env.example":
        return False
    if relative.name.lower() in {"auth.json", "credentials.json", "token.json", "server.json"}:
        return False
    if relative.suffix.lower() in {".pyc", ".pyo", ".tsbuildinfo", ".pem", ".key", ".pfx"}:
        return False
    if len(relative.parts) == 1:
        return relative.name in ROOT_FILES or (name == "crop" and (relative.suffix == ".py" or relative.name.endswith(".schema.json")))
    if relative.parts[0] not in DIRECTORIES[name]:
        return False
    if ".openai" in relative.parts:
        return relative.as_posix() == "web/.openai/hosting.json"
    if relative.parts[0] == "docs" and relative.suffix.lower() in {".pdf", ".png", ".jpg", ".zip", ".hwpx"}:
        return False
    return True


def save(name: str, payload) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def copy_sources() -> None:
    if any((ROOT / name).exists() for name in ORIGINALS):
        raise FileExistsError("Refusing to overwrite an existing copied source tree")
    entries = []
    for name, original in ORIGINALS.items():
        for source in files(original, sources=True):
            relative = source.relative_to(original)
            if allowed(name, relative):
                entries.append({"source": str(source), "destination": (Path(name) / relative).as_posix(), "bytes": source.stat().st_size, "before_sha256": digest(source)})
    save("task-1-source-before.json", {"created_at": datetime.now(timezone.utc).isoformat(), "files": entries})
    for entry in entries:
        source = Path(entry["source"])
        destination = ROOT / entry["destination"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != entry["before_sha256"]:
            raise RuntimeError(f"Original changed during copy: {source}")
        with destination.open("xb") as stream:
            stream.write(content)
        entry["copied_sha256"] = digest(destination)
        entry["source_after_sha256"] = digest(source)
        if len({entry["before_sha256"], entry["copied_sha256"], entry["source_after_sha256"]}) != 1:
            raise RuntimeError(f"Copy hash mismatch: {source}")
    save("task-1-copy-manifest.json", {"files": entries, "count": len(entries), "nested_exclusions": sorted(EXCLUDED), "vendor_runtime_exception": "studio/vendor/exam_studio_equation/dist/*.js is required by cli.cjs and app/equation_bridge.py", "matches": True})
    print(json.dumps({"copied": len(entries), "bytes": sum(entry["bytes"] for entry in entries), "matches": True}))


def protect() -> None:
    records = []
    for name, original in ORIGINALS.items():
        for path in files(original, sources=False):
            relative = path.relative_to(original)
            source = allowed(name, relative) and not any(part in EXCLUDED for part in relative.parts[:-1])
            protected = path.suffix.lower() == ".json" or path.name.startswith(".env") or path.suffix.lower() in {".ps1", ".toml", ".lock"}
            if source or protected:
                records.append({"path": str(path), "bytes": path.stat().st_size, "sha256": digest(path), "category": "source" if source else "runtime-config-manifest-job"})
    save("task-1-protected-before.json", {"created_at": datetime.now(timezone.utc).isoformat(), "files": records, "count": len(records)})
    print(json.dumps({"protected": len(records)}))


def recheck() -> None:
    baseline = json.loads((EVIDENCE / "task-1-protected-before.json").read_text(encoding="utf-8"))
    changes = []
    for entry in baseline["files"]:
        path = Path(entry["path"])
        current = digest(path) if path.is_file() else None
        if current != entry["sha256"]:
            changes.append({"path": str(path), "before_sha256": entry["sha256"], "after_sha256": current, "category": entry["category"]})
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "checked": len(baseline["files"]), "changes": changes, "originals_untouched_by_this_task": True, "concurrent_activity_policy": "Report differences; never revert original activity"}
    save("task-1-protected-after.json", result)
    print(json.dumps({"checked": result["checked"], "changes": len(changes)}))


if __name__ == "__main__":
    commands = {"protect": protect, "copy": copy_sources, "recheck": recheck}
    commands[sys.argv[1]]()
