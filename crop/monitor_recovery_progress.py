from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Sequence

from recovery_progress import ProgressError, STATUSES, refresh_recovery_progress
from astra_runtime import validate_parsed_paths


def positive_interval(value: str) -> float:
    interval = float(value)
    if interval <= 0:
        raise argparse.ArgumentTypeError("interval must be > 0")
    return interval


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="지정한 recovery run의 로컬 산출물을 감시합니다.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=positive_interval, default=5.0)
    parser.add_argument("--status", choices=sorted(STATUSES), default=None)
    parser.add_argument("--current-item", default=None)
    parser.add_argument("--stage", default=None)
    parser.add_argument("--stage-text", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_parsed_paths(args)
        while True:
            progress = refresh_recovery_progress(
                args.run_dir,
                status=args.status,
                current_item=args.current_item,
                stage=args.stage,
                stage_text=args.stage_text,
            )
            print(json.dumps(progress, ensure_ascii=False), flush=True)
            if not args.watch or progress["status"] != "running":
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as exc:
        print(f"recovery progress monitor failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
