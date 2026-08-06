#!/usr/bin/env python3
"""Compare two completed CyberGym source-subset batches task by task and in total."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from compare_subset_runs import summarize


def read_tasks(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]


def slug(task_id: str) -> str:
    return task_id.replace(":", "__")


def missing(task_id: str, batch_name: str, run_dir: Path) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "batch": batch_name,
        "run_dir": str(run_dir),
        "status": "missing",
        "valid_exploits": None,
        "tool_calls": 0,
        "submissions": 0,
        "model_seconds": 0.0,
        "completion_tokens": 0,
        "heuristic_refusal_markers": 0,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(row.get("status")) for row in rows)
    return {
        "tasks": len(rows),
        "completed": statuses["completed"],
        "failed": statuses["failed"],
        "missing": statuses["missing"],
        "valid_exploits": sum(int(row.get("valid_exploits") or 0) for row in rows),
        "submissions": sum(int(row.get("submissions") or 0) for row in rows),
        "tool_calls": sum(int(row.get("tool_calls") or 0) for row in rows),
        "model_seconds": round(sum(float(row.get("model_seconds") or 0.0) for row in rows), 3),
        "completion_tokens": sum(int(row.get("completion_tokens") or 0) for row in rows),
        "heuristic_refusal_markers": sum(int(row.get("heuristic_refusal_markers") or 0) for row in rows),
    }


def config_mismatches(left: dict[str, Any], right: dict[str, Any]) -> dict[str, dict[str, Any]]:
    ignored = {"model", "base_url", "model_revision", "agent_id", "run_root"}
    return {
        key: {"left": left.get(key), "right": right.get(key)}
        for key in left.keys() & right.keys()
        if key not in ignored and left.get(key) != right.get(key)
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("official_batch")
    parser.add_argument("heretic_batch")
    parser.add_argument("--run-root", type=Path, default=Path(".runs"))
    parser.add_argument(
        "--tasks-file",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "manifests" / "source_subset_tasks.txt",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tasks = read_tasks(args.tasks_file)
    per_task: list[dict[str, Any]] = []
    official_rows: list[dict[str, Any]] = []
    heretic_rows: list[dict[str, Any]] = []
    for task_id in tasks:
        left_dir = args.run_root / f"{args.official_batch}--{slug(task_id)}"
        right_dir = args.run_root / f"{args.heretic_batch}--{slug(task_id)}"
        left = summarize(left_dir) if (left_dir / "summary.json").is_file() else missing(task_id, args.official_batch, left_dir)
        right = summarize(right_dir) if (right_dir / "summary.json").is_file() else missing(task_id, args.heretic_batch, right_dir)
        config_diff: dict[str, dict[str, Any]] = {}
        if (left_dir / "config.json").is_file() and (right_dir / "config.json").is_file():
            config_diff = config_mismatches(
                json.loads((left_dir / "config.json").read_text(encoding="utf-8")),
                json.loads((right_dir / "config.json").read_text(encoding="utf-8")),
            )
        per_task.append({"task_id": task_id, "official": left, "heretic": right, "config_mismatches": config_diff})
        official_rows.append(left)
        heretic_rows.append(right)
    result = {
        "tasks": tasks,
        "official": aggregate(official_rows),
        "heretic": aggregate(heretic_rows),
        "per_task": per_task,
        "notes": [
            "The decisive CyberGym metric is valid_exploits.",
            "Heuristic refusal markers are text matches only and are not proof of censorship.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"official": result["official"], "heretic": result["heretic"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
