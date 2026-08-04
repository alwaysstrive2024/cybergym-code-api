#!/usr/bin/env python3
"""Append a task ID once when an evaluation did not produce a valid exploit."""

from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--eval-exit", type=int, required=True)
    parser.add_argument("--verify-exit", type=int, required=True)
    return parser.parse_args()


def failed(args: argparse.Namespace) -> bool:
    if args.eval_exit != 0 or args.verify_exit != 0:
        return True
    try:
        verification = json.loads((args.run_dir / "verification.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return verification.get("valid_exploit_count", 0) < 1


def main() -> int:
    args = parse_args()
    if not failed(args):
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a+", encoding="utf-8") as output:
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        output.seek(0)
        existing = {line.strip() for line in output if line.strip()}
        if args.task_id not in existing:
            output.write(f"{args.task_id}\n")
            output.flush()
        fcntl.flock(output.fileno(), fcntl.LOCK_UN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
