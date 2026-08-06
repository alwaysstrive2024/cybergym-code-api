import json
import subprocess
import sys
from pathlib import Path


def test_metrics_cli_aggregates_synthetic_trajectory(tmp_path: Path) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    trajectory.write_text(
        json.dumps({"event": "tool", "name": "read_file", "result": "x", "raw_result_chars": 5, "processed_result_chars": 1})
        + "\n"
    )
    completed = subprocess.run(
        [sys.executable, "scripts/evaluation/summarize_trajectory_metrics.py", str(trajectory)],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    assert report[str(trajectory)]["read_calls"] == 1
    assert report[str(trajectory)]["deterministic_reduction_chars"] == 4

