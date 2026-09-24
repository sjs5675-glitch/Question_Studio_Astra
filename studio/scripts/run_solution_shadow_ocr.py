from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.batch import BatchCoordinator, TERMINAL_STATES
from app.transcription import PROVIDER_DEFAULTS
from astra_runtime import validate_parsed_paths


def _public_progress(record: dict[str, Any]) -> dict[str, Any]:
    summary = record.get("summary")
    ocr = summary.get("ocr") if isinstance(summary, dict) else None
    counters = {}
    if isinstance(ocr, dict):
        for key in (
            "total",
            "attempted",
            "succeeded",
            "failed",
            "provider_total",
            "provider_attempted",
            "provider_failed",
            "provider_cached",
            "application_total",
            "application_attempted",
            "content_reused",
            "work_total",
            "work_attempted",
        ):
            value = ocr.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                counters[key] = value
    return {
        "id": record.get("id"),
        "state": record.get("state"),
        "stage": record.get("stage"),
        "progress": record.get("progress"),
        "message": record.get("message"),
        "ocr": counters,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run missing solution OCR in an isolated shadow workspace."
    )
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--pdf-root", required=True, action="append", type=Path)
    parser.add_argument("--import-root", required=True, action="append", type=Path)
    parser.add_argument("--crop-root", required=True, type=Path)
    parser.add_argument(
        "--profile", choices=sorted(PROVIDER_DEFAULTS), default="balanced"
    )
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_parsed_paths(args)
    coordinator = BatchCoordinator(
        args.workspace,
        args.pdf_root,
        args.import_root,
        args.crop_root,
        poll_seconds=args.poll_seconds,
    )
    try:
        created = coordinator.transcribe_missing(args.project_id, args.profile)
        job_id = str(created["id"])
        last_revision: int | None = None
        while True:
            record = coordinator.get(job_id)
            revision = record.get("revision")
            if revision != last_revision:
                print(
                    json.dumps(_public_progress(record), ensure_ascii=False),
                    flush=True,
                )
                last_revision = revision if isinstance(revision, int) else last_revision
            if record.get("state") in TERMINAL_STATES:
                ocr = record.get("summary", {}).get("ocr", {})
                unresolved = int(ocr.get("failed") or 0) if isinstance(ocr, dict) else 1
                return 0 if record.get("state") == "completed" and unresolved == 0 else 2
            time.sleep(args.poll_seconds)
    finally:
        coordinator.close()


if __name__ == "__main__":
    raise SystemExit(main())
