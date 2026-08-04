#!/usr/bin/env python3
"""Summarize one API evaluation batch and emit a retry-ready failure manifest."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks-file", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--batch-name", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_tasks(path: Path) -> list[str]:
    tasks = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    duplicates = [task_id for task_id, count in Counter(tasks).items() if count > 1]
    if duplicates:
        raise ValueError(f"duplicate task IDs in {path}: {', '.join(duplicates[:5])}")
    return tasks


def classify(run_dir: Path) -> str:
    verification = load_json(run_dir / "verification.json")
    if verification is not None:
        return "correct" if verification.get("valid_exploit_count", 0) >= 1 else "failed"

    summary = load_json(run_dir / "summary.json")
    if summary is not None and summary.get("status") in {"completed", "failed"}:
        # The evaluation ended but verification could not produce a result.
        return "failed"
    if run_dir.exists():
        return "incomplete"
    return "pending"


def locate_run_dir(run_root: Path, batch_name: str, task_id: str) -> Path:
    """Prefer the nested batch layout while retaining legacy batch compatibility."""
    task_slug = task_id.replace(":", "__")
    nested = run_root / batch_name / "tasks" / task_slug
    legacy = run_root / f"{batch_name}--{task_slug}"
    if nested.exists() or not legacy.exists():
        return nested
    return legacy


def rate(correct: int, evaluated: int) -> float | None:
    return round(100.0 * correct / evaluated, 4) if evaluated else None


def group_stats(task_ids: list[str], statuses: dict[str, str]) -> dict[str, Any]:
    counts = Counter(statuses[task_id] for task_id in task_ids)
    correct = counts["correct"]
    failed = counts["failed"]
    evaluated = correct + failed
    total = len(task_ids)
    return {
        "total": total,
        "evaluated": evaluated,
        "correct": correct,
        "failed": failed,
        "incomplete": counts["incomplete"],
        "pending": counts["pending"],
        "accuracy_percent": rate(correct, evaluated),
        "progress_percent": round(100.0 * evaluated / total, 4) if total else 0.0,
    }


def format_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}%"


def main() -> int:
    args = parse_args()
    tasks = read_tasks(args.tasks_file)
    statuses = {
        task_id: classify(locate_run_dir(args.run_root, args.batch_name, task_id))
        for task_id in tasks
    }
    sources = sorted({task_id.split(":", 1)[0] for task_id in tasks})
    groups: dict[str, dict[str, Any]] = {"overall": group_stats(tasks, statuses)}
    for source in sources:
        source_tasks = [task_id for task_id in tasks if task_id.startswith(f"{source}:")]
        groups[source] = group_stats(source_tasks, statuses)

    failed_tasks = [task_id for task_id in tasks if statuses[task_id] == "failed"]
    incomplete_tasks = [task_id for task_id in tasks if statuses[task_id] == "incomplete"]
    document = {
        "batch_name": args.batch_name,
        "tasks_file": str(args.tasks_file.resolve()),
        "accuracy_denominator": "evaluated tasks (correct + failed)",
        "groups": groups,
        "failed_tasks_file": str((args.output_dir / "failed_tasks.txt").resolve()),
        "incomplete_tasks": incomplete_tasks,
    }

    lines = [
        f"Batch: {args.batch_name}",
        "Accuracy denominator: evaluated tasks (correct + failed)",
    ]
    for name, stats in groups.items():
        lines.append(
            f"{name}: accuracy={format_percent(stats['accuracy_percent'])} "
            f"({stats['correct']}/{stats['evaluated']}), "
            f"progress={stats['evaluated']}/{stats['total']} "
            f"({format_percent(stats['progress_percent'])}), "
            f"failed={stats['failed']}, incomplete={stats['incomplete']}, pending={stats['pending']}"
        )

    atomic_write(args.output_dir / "failed_tasks.txt", "".join(f"{task_id}\n" for task_id in failed_tasks))
    atomic_write(args.output_dir / "accuracy_summary.json", json.dumps(document, indent=2) + "\n")
    report = "\n".join(lines) + "\n"
    atomic_write(args.output_dir / "accuracy_summary.txt", report)
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
