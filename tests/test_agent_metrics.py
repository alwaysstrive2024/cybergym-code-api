from cybergym.agents.metrics import aggregate_trajectory_events


def test_trajectory_metrics_use_only_standardized_event_metadata() -> None:
    events = [
        {
            "timestamp": "2026-08-05T00:00:00+00:00",
            "event": "tool",
            "name": "read_file",
            "result": "source",
            "raw_result_chars": 100,
            "processed_result_chars": 60,
            "arguments": {"path": "parser.c", "start_line": 1, "max_lines": 20},
            "usage": {"prompt_tokens": 100},
        },
        {
            "timestamp": "2026-08-05T00:00:01+00:00",
            "event": "tool",
            "name": "read_file",
            "result": "This exact source range was already inspected.",
            "raw_result_chars": 50,
            "processed_result_chars": 50,
            "arguments": {"path": "parser.c", "start_line": 1, "max_lines": 20},
        },
        {"timestamp": "2026-08-05T00:00:02+00:00", "event": "policy_guidance"},
        {
            "timestamp": "2026-08-05T00:00:03+00:00",
            "event": "submission_outcome",
            "is_valid_exploit": True,
        },
    ]
    metrics = aggregate_trajectory_events(events)

    assert metrics["valid_poc"] is True
    assert metrics["first_submission_step"] == 2
    assert metrics["repeat_read_rate"] == 0.5
    assert metrics["unique_read_ranges"] == 1
    assert metrics["input_tokens"] == 100
    assert metrics["deterministic_reduction_chars"] == 40
    assert metrics["wall_time_seconds"] == 3.0
