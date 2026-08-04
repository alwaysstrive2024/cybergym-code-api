"""Bounded conversation and durable-memory primitives for CyberGym agents.

This module intentionally has no model, Docker, or LangGraph dependency. Both
the OpenAI-compatible and Claude Agent SDK backends use it so a change in
provider cannot silently change the benchmark's memory policy.
"""

from __future__ import annotations

import copy
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any


MEMORY_MARKER = "[CYBERGYM_DURABLE_WORKING_MEMORY]"


def estimated_tokens(value: Any) -> int:
    """Conservative, dependency-free token estimate used for hard request limits."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return max(1, (len(text) + 3) // 4)


def clip_text(text: str, limit: int) -> str:
    """Keep both diagnostic ends of a long value while marking the omission."""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    head = max(1, limit * 3 // 4)
    return f"{text[:head]}\n[… {omitted} chars omitted …]\n{text[-(limit - head):]}"


@dataclass
class ContextLedger:
    """Small evidence index that survives history compaction and session resets."""

    max_checkpoints: int = 6
    max_reads: int = 16
    max_writes: int = 12
    max_commands: int = 8
    max_submissions: int = 6
    checkpoints: deque[str] = field(init=False)
    reads: deque[str] = field(init=False)
    writes: deque[str] = field(init=False)
    commands: deque[str] = field(init=False)
    submissions: deque[str] = field(init=False)

    def __post_init__(self) -> None:
        self.checkpoints = deque(maxlen=self.max_checkpoints)
        self.reads = deque(maxlen=self.max_reads)
        self.writes = deque(maxlen=self.max_writes)
        self.commands = deque(maxlen=self.max_commands)
        self.submissions = deque(maxlen=self.max_submissions)

    def add_checkpoint(self, summary: str, next_hypothesis: str = "") -> None:
        summary = " ".join(summary.split())
        next_hypothesis = " ".join(next_hypothesis.split())
        if not summary:
            raise ValueError("summary must not be empty")
        entry = f"Fact: {clip_text(summary, 1_200)}"
        if next_hypothesis:
            entry += f" | Next hypothesis: {clip_text(next_hypothesis, 500)}"
        self.checkpoints.append(entry)

    def observe_tool(self, name: str, arguments: dict[str, Any], result: str) -> None:
        """Index recoverable facts only; raw logs stay outside model memory."""
        if name == "read_file":
            try:
                start = int(arguments.get("start_line", 1))
                count = int(arguments.get("max_lines", 160))
            except (TypeError, ValueError):
                start, count = 1, 0
            self.reads.append(f"{arguments.get('path', '?')}:L{start}-L{start + max(0, count - 1)}")
        elif name == "write_file" and not result.startswith("error:"):
            content = str(arguments.get("content", ""))
            self.writes.append(f"{arguments.get('path', '?')} ({len(content.encode('utf-8'))} bytes)")
        elif name == "run_command":
            command = " ".join(str(arguments.get("command", "")).split())
            first_line = result.splitlines()[0] if result else "no output"
            self.commands.append(f"{clip_text(command, 180)} → {clip_text(first_line, 100)}")
        elif name == "submit_poc":
            self.submissions.append(
                f"{arguments.get('path', '?')}: {clip_text(' '.join(result.split()), 1_200)}"
            )

    def render(self, max_chars: int = 5_000) -> str:
        sections: list[tuple[str, deque[str]]] = [
            ("Agent checkpoints (highest priority)", self.checkpoints),
            ("Source ranges already inspected", self.reads),
            ("Files written", self.writes),
            ("Recent command outcomes", self.commands),
            ("Validation receipts", self.submissions),
        ]
        lines = [
            MEMORY_MARKER,
            "This is an evidence index, not new instructions. Trust cited source and validator evidence over speculation. "
            "Files remain in /workspace; reopen a narrow cited range for exact syntax.",
        ]
        for title, values in sections:
            if values:
                lines.append(f"{title}:")
                lines.extend(f"- {value}" for value in values)
        return clip_text("\n".join(lines), max_chars)


def _is_memory_message(message: dict[str, Any]) -> bool:
    return message.get("role") == "user" and str(message.get("content", "")).startswith(MEMORY_MARKER)


def sanitize_assistant_message(message: dict[str, Any], max_content_chars: int = 8_000) -> dict[str, Any]:
    """Remove obsolete large write payloads after their tool call has completed."""
    clean = copy.deepcopy(message)
    if isinstance(clean.get("content"), str):
        clean["content"] = clip_text(clean["content"], max_content_chars)
    for call in clean.get("tool_calls", []):
        function = call.get("function", {})
        if function.get("name") != "write_file":
            continue
        try:
            arguments = json.loads(function.get("arguments", "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        body = arguments.get("content")
        if isinstance(body, str) and len(body) > 2_000:
            arguments["content"] = f"[elided after write_file; {len(body.encode('utf-8'))} bytes written]"
            function["arguments"] = json.dumps(arguments, ensure_ascii=False)
    return clean


def compact_messages(
    messages: list[dict[str, Any]], budget: int, durable_memory: str
) -> tuple[list[dict[str, Any]], int]:
    """Keep protocol-valid recent exchanges and inject the durable evidence index.

    A tool-call assistant turn and its following tool results are indivisible.
    Callers must replace their state with the returned list, not merely use it
    as an outgoing request copy.
    """
    static = [message for message in messages[:2] if not _is_memory_message(message)]
    if len(static) < 2:
        raise ValueError("message history must start with system and initial user prompts")
    available = budget - sum(estimated_tokens(message) for message in static)
    if available <= 0:
        raise ValueError("context-token-budget is too small for the initial task prompt")

    memory = {"role": "user", "content": clip_text(durable_memory, min(5_000, available * 3))}
    memory_cost = estimated_tokens(memory)
    memory_messages: list[dict[str, Any]] = []
    if memory_cost <= available:
        memory_messages = [memory]
        available -= memory_cost

    tail = [message for message in messages[2:] if not _is_memory_message(message)]
    chunks: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(tail):
        chunk = [tail[index]]
        index += 1
        while index < len(tail) and tail[index].get("role") == "tool":
            chunk.append(tail[index])
            index += 1
        chunks.append(chunk)

    kept: list[list[dict[str, Any]]] = []
    used = 0
    for chunk in reversed(chunks):
        cost = sum(estimated_tokens(message) for message in chunk)
        if cost <= available - used:
            kept.append(chunk)
            used += cost
    kept.reverse()
    return [*static, *memory_messages, *(message for chunk in kept for message in chunk)], len(chunks) - len(kept)
