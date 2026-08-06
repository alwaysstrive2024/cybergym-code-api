"""Content-free aggregation of standardized trajectory events for A/B analysis."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any


def aggregate_trajectory_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    tool_calls = reads = repeated = raw_chars = processed_chars = visible_chars = 0
    input_tokens = excluded_reopens = 0
    unique_ranges: set[tuple[str, int, int]] = set()
    read_files: set[str] = set()
    excluded_files: set[str] = set()
    context_overflows = policy_blocks = policy_guidance = 0
    first_submission_step: int | None = None
    valid_poc = False
    submissions = invalid_submissions = 0
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    last_policy_state: dict[str, Any] = {}
    for event in events:
        timestamp = event.get("timestamp")
        if isinstance(timestamp, str):
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                first_timestamp = parsed if first_timestamp is None else min(first_timestamp, parsed)
                last_timestamp = parsed if last_timestamp is None else max(last_timestamp, parsed)
            except ValueError:
                pass
        kind = event.get("event")
        usage = event.get("usage")
        if isinstance(usage, dict):
            input_tokens += int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
        if kind == "tool":
            tool_calls += 1
            raw_chars += int(event.get("raw_result_chars", 0) or 0)
            processed_chars += int(event.get("processed_result_chars", 0) or 0)
            visible_chars += len(str(event.get("result", "")))
            if event.get("name") == "read_file":
                reads += 1
                arguments = event.get("arguments", {})
                if isinstance(arguments, dict):
                    path = str(arguments.get("path", "?"))
                    start = int(arguments.get("start_line", 1) or 1)
                    count = int(arguments.get("max_lines", 240) or 240)
                    unique_ranges.add((path, start, count))
                    read_files.add(path)
                    if path in excluded_files:
                        excluded_reopens += 1
                if "exact source range was already inspected" in str(event.get("result", "")):
                    repeated += 1
            elif event.get("name") == "update_investigation_state":
                arguments = event.get("arguments", {})
                decisions = arguments.get("file_decisions", []) if isinstance(arguments, dict) else []
                for decision in decisions if isinstance(decisions, list) else []:
                    if isinstance(decision, dict) and decision.get("status") == "excluded":
                        excluded_files.add(str(decision.get("path", "")))
        elif kind == "submission_outcome":
            submissions += 1
            if first_submission_step is None:
                first_submission_step = tool_calls
            if event.get("is_valid_exploit") is True:
                valid_poc = True
            elif event.get("is_valid_exploit") is False:
                invalid_submissions += 1
        elif kind in {"context_overflow_recovery", "context_overflow"}:
            context_overflows += 1
        elif kind == "policy_blocked":
            policy_blocks += 1
        elif kind == "policy_guidance":
            policy_guidance += 1
        elif kind == "policy_state":
            last_policy_state = event
    duration = (last_timestamp - first_timestamp).total_seconds() if first_timestamp and last_timestamp else None
    return {
        "valid_poc": valid_poc,
        "any_submission": submissions > 0,
        "submission_count": submissions,
        "invalid_submissions_before_success": invalid_submissions,
        "first_submission_step": first_submission_step,
        "tool_calls": tool_calls,
        "read_calls": reads,
        "unique_read_files": len(read_files),
        "unique_read_ranges": len(unique_ranges),
        "repeat_read_rate": repeated / reads if reads else 0.0,
        "excluded_reopen_rate": excluded_reopens / reads if reads else 0.0,
        "raw_tool_chars": raw_chars,
        "processed_tool_chars": processed_chars,
        "model_visible_tool_chars": visible_chars,
        "deterministic_reduction_chars": max(0, raw_chars - processed_chars),
        "context_overflow_count": context_overflows,
        "input_tokens": input_tokens,
        "policy_block_count": policy_blocks,
        "policy_guidance_count": policy_guidance,
        "wall_time_seconds": duration,
        "final_policy_state": {key: value for key, value in last_policy_state.items() if key not in {"event", "timestamp"}},
    }
