from __future__ import annotations

import json

from cybergym.agents.context import (
    MEMORY_MARKER,
    ContextLedger,
    compact_messages,
    is_context_overflow_error,
    sanitize_assistant_message,
)


def test_compaction_keeps_memory_and_drops_complete_old_exchange() -> None:
    ledger = ContextLedger()
    ledger.add_checkpoint("parser.c:L42 validates a signed length", "write the shortest candidate")
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "tool_calls": [{"id": "old", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "old", "content": "x" * 600},
        {"role": "assistant", "tool_calls": [{"id": "new", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "new", "content": "y" * 40},
    ]

    compacted, omitted = compact_messages(messages, 260, ledger.render())

    assert compacted[:2] == messages[:2]
    assert compacted[2]["content"].startswith(MEMORY_MARKER)
    assert omitted == 1
    assert compacted[-1]["tool_call_id"] == "new"


def test_large_written_content_is_removed_after_tool_execution() -> None:
    message = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "call_1",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "poc.bin", "content": "A" * 3_000}),
                },
            }
        ],
    }

    sanitized = sanitize_assistant_message(message)
    arguments = sanitized["tool_calls"][0]["function"]["arguments"]

    assert len(arguments) < 200
    assert "poc.bin" in arguments


def test_compaction_keeps_a_contiguous_recent_window() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "old-small"},
        {"role": "assistant", "content": "middle" * 400},
        {"role": "assistant", "content": "new-small"},
    ]

    compacted, omitted = compact_messages(messages, 180, ContextLedger().render())

    contents = [message.get("content") for message in compacted]
    assert "new-small" in contents
    assert "middle" * 400 not in contents
    assert "old-small" not in contents
    assert omitted == 2


def test_ledger_deduplicates_repeated_evidence() -> None:
    ledger = ContextLedger()
    ledger.add_checkpoint("same fact")
    ledger.add_checkpoint("same fact")
    ledger.observe_tool("read_file", {"path": "source.c", "start_line": 10, "max_lines": 5}, "result")
    ledger.observe_tool("read_file", {"path": "source.c", "start_line": 10, "max_lines": 5}, "result")

    assert list(ledger.checkpoints) == ["Fact: same fact"]
    assert list(ledger.reads) == ["source.c:L10-L14"]


def test_compaction_removes_repeated_system_and_memory_messages() -> None:
    ledger = ContextLedger()
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "system", "content": "system"},
        {"role": "user", "content": ledger.render()},
        {"role": "assistant", "content": "recent"},
    ]

    compacted, _ = compact_messages(messages, 300, ledger.render(), reserved_tokens=20)

    assert sum(message.get("role") == "system" for message in compacted) == 1
    assert sum(str(message.get("content", "")).startswith(MEMORY_MARKER) for message in compacted) == 1
    assert compacted[-1]["content"] == "recent"


def test_only_context_limit_errors_trigger_emergency_recovery() -> None:
    assert is_context_overflow_error(RuntimeError("maximum context length exceeded"))
    assert is_context_overflow_error(RuntimeError("prompt is too long"))
    assert not is_context_overflow_error(RuntimeError("invalid API key"))


def test_structured_investigation_state_is_bounded_and_rendered() -> None:
    ledger = ContextLedger()
    ledger.update_investigation_state(
        {
            "objective": "produce a differential candidate",
            "input_path": "harness.c:10 -> parse.c:20",
            "file_decisions": [
                {
                    "path": "build.sh",
                    "status": "excluded",
                    "reason": "build flags confirmed at build.sh:12",
                    "reopen_if": "sanitizer flags change",
                }
            ],
            "call_edges": [
                {
                    "caller": "harness.c:10:fuzz",
                    "callee": "parse.c:20:parse",
                    "evidence": "harness.c:10",
                    "source": "direct_call",
                }
            ],
            "tracked_values": ["input length -> count at parse.c:24"],
            "next_hypothesis": {"primary": "count exceeds capacity"},
            "uncertainties": ["fixed check location is unknown"],
        }
    )

    rendered = ledger.render()
    assert "Excluded files:" in rendered
    assert "build.sh: build flags confirmed" in rendered
    assert "harness.c:10:fuzz → parse.c:20:parse" in rendered
    assert ledger.snapshot()["next_hypothesis"]["primary"] == "count exceeds capacity"


def test_repeat_read_gate_allows_modified_or_truncated_ranges() -> None:
    ledger = ContextLedger()
    args = {"path": "parser.c", "start_line": 10, "max_lines": 5}
    ledger.observe_tool("read_file", args, "numbered source")
    assert "already inspected" in (ledger.repeated_read_notice("parser.c", 10, 5) or "")

    ledger.observe_tool("write_file", {"path": "parser.c", "content": "new"}, "wrote 3 bytes")
    assert ledger.repeated_read_notice("parser.c", 10, 5) is None

    ledger.observe_tool("read_file", args, "[truncated; request a narrower line range]")
    assert ledger.repeated_read_notice("parser.c", 10, 5) is None


def test_repeat_read_gate_normalizes_workspace_paths_and_evicts_old_details() -> None:
    ledger = ContextLedger(max_reads=2)
    ledger.observe_tool("read_file", {"path": "./parser.c", "start_line": 1, "max_lines": 2}, "source")
    assert ledger.repeated_read_notice("/workspace/parser.c", 1, 2) is not None

    ledger.observe_tool("read_file", {"path": "a.c", "start_line": 1, "max_lines": 1}, "source")
    ledger.observe_tool("read_file", {"path": "b.c", "start_line": 1, "max_lines": 1}, "source")
    assert ledger.repeated_read_notice("parser.c", 1, 2) is None
    assert len(ledger.read_details) == 2

def test_conditional_and_excluded_files_require_reopen_condition() -> None:
    ledger = ContextLedger()
    try:
        ledger.update_investigation_state(
            {"file_decisions": [{"path": "api.h", "status": "excluded", "reason": "public API only"}]}
        )
    except ValueError as exc:
        assert "requires reopen_if" in str(exc)
    else:
        raise AssertionError("missing reopen_if should be rejected")


def test_memory_limit_keeps_high_priority_state_without_midline_clipping() -> None:
    ledger = ContextLedger()
    ledger.update_investigation_state(
        {
            "objective": "retain this objective",
            "crash_evidence": ["ASan at parser.c:42"],
            "next_hypothesis": {"primary": "retain this hypothesis"},
            "uncertainties": ["x" * 500 for _ in range(8)],
        }
    )
    rendered = ledger.render(max_chars=700)

    assert "retain this objective" in rendered
    assert "ASan at parser.c:42" in rendered
    assert "retain this hypothesis" in rendered
    assert len(rendered) <= 700


def test_state_rejects_escaping_paths_and_clips_call_edge_fields() -> None:
    ledger = ContextLedger()
    for path in ("../outside.c", "/tmp/outside.c"):
        try:
            ledger.update_investigation_state(
                {"file_decisions": [{"path": path, "status": "critical", "reason": "evidence"}]}
            )
        except ValueError as exc:
            assert "workspace" in str(exc)
        else:
            raise AssertionError("escaping path should be rejected")

    ledger.update_investigation_state(
        {
            "call_edges": [
                {"caller": "a" * 1_000, "callee": "b" * 1_000, "evidence": "c" * 2_000, "source": "search"}
            ]
        }
    )
    edge = ledger.snapshot()["call_edges"][0]
    assert len(edge["caller"]) < 400
    assert len(edge["callee"]) < 400
    assert len(edge["evidence"]) < 600
