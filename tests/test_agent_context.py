from __future__ import annotations

import json

from cybergym.agents.context import ContextLedger, MEMORY_MARKER, compact_messages, sanitize_assistant_message


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
