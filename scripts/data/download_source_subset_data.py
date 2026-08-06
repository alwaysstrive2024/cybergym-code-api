#!/usr/bin/env python3
"""Download only the public source assets needed by CyberGym's official subset.

The benchmark task generator needs each task's description and vulnerable
source archive.  Fixed archives and patches are deliberately not downloaded:
they are not part of the agent-visible task and would waste substantial disk.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from huggingface_hub import snapshot_download


DATASET_ID = "sunblaze-ucb/cybergym"
TASKS_FILE = Path(__file__).resolve().parents[1] / "manifests" / "source_subset_tasks.txt"


def task_ids(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]


def patterns(tasks: list[str]) -> list[str]:
    output: list[str] = []
    for task_id in tasks:
        family, task_number = task_id.split(":", 1)
        base = f"data/{family}/{task_number}"
        output.extend((f"{base}/description.txt", f"{base}/repo-vul.tar.gz"))
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("cybergym_data"))
    parser.add_argument("--tasks-file", type=Path, default=TASKS_FILE)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--revision", default="main")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tasks = task_ids(args.tasks_file)
    requested = patterns(tasks)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=DATASET_ID,
        repo_type="dataset",
        revision=args.revision,
        local_dir=args.data_dir,
        allow_patterns=requested,
        max_workers=args.max_workers,
    )
    missing = [relative for relative in requested if not (args.data_dir / relative).is_file()]
    if missing:
        raise RuntimeError(f"required subset data was not downloaded: {', '.join(missing)}")
    print(f"Ready: {len(tasks)} tasks / {len(requested)} source assets in {args.data_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
