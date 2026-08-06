#!/usr/bin/env python3
"""Aggregate content-free A/B metrics from one or more CyberGym trajectories."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from cybergym.agents.metrics import aggregate_trajectory_events


def read_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: trajectory event must be an object")
            events.append(value)
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trajectories", nargs="+", type=Path)
    args = parser.parse_args()
    report = {
        str(path): aggregate_trajectory_events(read_events(path))
        for path in args.trajectories
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

