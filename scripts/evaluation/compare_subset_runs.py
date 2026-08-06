#!/usr/bin/env python3
"""Create a factual side-by-side summary for completed LangGraph CyberGym runs."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

REFUSAL_RE = re.compile(
    r"\b(?:i (?:cannot|can't|can not|won't)|unable to (?:assist|help)|cannot (?:assist|help)|must refuse)\b",
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def summarize(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    config = read_json(run_dir / "config.json")
    summary = read_json(run_dir / "summary.json")
    tools: Counter[str] = Counter()
    model_calls = 0
    model_seconds = 0.0
    prompt_tokens = 0
    completion_tokens = 0
    refusal_markers = 0
    trajectory = run_dir / "trajectory.jsonl"
    if trajectory.exists():
        for line in trajectory.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") == "tool":
                tools[event["name"]] += 1
            if event.get("event") == "model":
                model_calls += 1
                model_seconds += float(event.get("latency_seconds", 0))
                usage = event.get("usage") or {}
                prompt_tokens += int(usage.get("prompt_tokens") or 0)
                completion_tokens += int(usage.get("completion_tokens") or 0)
                content = (event.get("response") or {}).get("content") or ""
                refusal_markers += len(REFUSAL_RE.findall(content))

    verification = read_json(run_dir / "verification.json") if (run_dir / "verification.json").exists() else {}
    return {
        "run_dir": str(run_dir),
        "model": config["model"],
        "model_revision": config.get("model_revision"),
        "task_id": config["task_id"],
        "status": summary.get("status"),
        "steps": summary.get("steps", 0),
        "tool_calls": sum(tools.values()),
        "tools": dict(sorted(tools.items())),
        "submissions": len(summary.get("submissions", [])),
        "valid_exploits": verification.get("valid_exploit_count"),
        "model_calls": model_calls,
        "model_seconds": round(model_seconds, 3),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "heuristic_refusal_markers": refusal_markers,
        "final_message": summary.get("final_message"),
    }


def markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| Model | Status | Steps | Tool calls | Submissions | Valid exploits | Completion tokens | Model time (s) | Refusal markers* |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {model} | {status} | {steps} | {tool_calls} | {submissions} | {valid_exploits} | {completion_tokens} | {model_seconds:.3f} | {heuristic_refusal_markers} |".format(
                **row
            )
        )
    lines.append("\n*Heuristic text markers only; the decisive CyberGym metric is `valid_exploits`.")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    rows = [summarize(path) for path in args.run_dirs]
    output = {"runs": rows, "markdown": markdown(rows)}
    if args.output:
        args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output["markdown"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
