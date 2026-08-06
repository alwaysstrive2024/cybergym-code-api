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
from pathlib import PurePosixPath
from typing import Any

MEMORY_MARKER = "[CYBERGYM_DURABLE_WORKING_MEMORY]"
FILE_STATUSES = {"critical", "supporting", "conditional", "excluded", "unknown", "stale"}
EVIDENCE_SOURCES = {"sanitizer", "validator", "direct_call", "search", "inference"}


def estimated_tokens(value: Any) -> int:
    """Conservative, dependency-free token estimate used for hard request limits."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return max(1, (len(text) + 3) // 4)


def is_context_overflow_error(exc: Exception) -> bool:
    """Recognize common provider-specific context-limit failures without hiding other 400s."""
    text = str(exc).lower()
    markers = (
        "context length",
        "context_length_exceeded",
        "maximum context",
        "max context",
        "too many tokens",
        "token limit",
        "prompt is too long",
        "input is too long",
    )
    return any(marker in text for marker in markers)


def clip_text(text: str, limit: int) -> str:
    """Keep both diagnostic ends of a long value while marking the omission."""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    head = max(1, limit * 3 // 4)
    return f"{text[:head]}\n[… {omitted} chars omitted …]\n{text[-(limit - head):]}"


def _append_unique(values: deque[str], entry: str) -> None:
    """Keep the newest occurrence without spending memory on exact duplicates."""
    try:
        values.remove(entry)
    except ValueError:
        pass
    values.append(entry)


def _normalize_workspace_path(path: str) -> str:
    if len(path) > 500:
        raise ValueError("workspace path exceeds 500 characters")
    parsed = PurePosixPath(path)
    if parsed.is_absolute():
        try:
            parsed = parsed.relative_to("/workspace")
        except ValueError as exc:
            raise ValueError("absolute paths must stay below /workspace") from exc
    if ".." in parsed.parts:
        raise ValueError("path must stay inside the task workspace")
    value = str(parsed)
    return "." if value in {"", "."} else value


@dataclass
class ContextLedger:
    """Small evidence index that survives history compaction and session resets."""

    max_checkpoints: int = 6
    max_reads: int = 16
    max_writes: int = 12
    max_commands: int = 8
    max_submissions: int = 6
    max_file_decisions: int = 32
    max_call_edges: int = 12
    max_tracked_values: int = 6
    max_uncertainties: int = 8
    checkpoints: deque[str] = field(init=False)
    reads: deque[str] = field(init=False)
    writes: deque[str] = field(init=False)
    commands: deque[str] = field(init=False)
    submissions: deque[str] = field(init=False)
    objective: str = ""
    input_path: str = ""
    crash_evidence: deque[str] = field(init=False)
    file_decisions: dict[str, dict[str, Any]] = field(init=False)
    call_edges: deque[dict[str, str]] = field(init=False)
    tracked_values: deque[str] = field(init=False)
    uncertainties: deque[str] = field(init=False)
    primary_hypothesis: str = ""
    alternate_hypothesis: str = ""
    read_details: dict[str, dict[str, Any]] = field(init=False)
    revision: int = 0

    def __post_init__(self) -> None:
        self.checkpoints = deque(maxlen=self.max_checkpoints)
        self.reads = deque(maxlen=self.max_reads)
        self.writes = deque(maxlen=self.max_writes)
        self.commands = deque(maxlen=self.max_commands)
        self.submissions = deque(maxlen=self.max_submissions)
        self.crash_evidence = deque(maxlen=6)
        self.file_decisions = {}
        self.call_edges = deque(maxlen=self.max_call_edges)
        self.tracked_values = deque(maxlen=self.max_tracked_values)
        self.uncertainties = deque(maxlen=self.max_uncertainties)
        self.read_details = {}

    def add_checkpoint(self, summary: str, next_hypothesis: str = "") -> None:
        summary = " ".join(summary.split())
        next_hypothesis = " ".join(next_hypothesis.split())
        if not summary:
            raise ValueError("summary must not be empty")
        entry = f"Fact: {clip_text(summary, 1_200)}"
        if next_hypothesis:
            entry += f" | Next hypothesis: {clip_text(next_hypothesis, 500)}"
        _append_unique(self.checkpoints, entry)

    def observe_tool(self, name: str, arguments: dict[str, Any], result: str) -> None:
        """Index recoverable facts only; raw logs stay outside model memory."""
        if name == "read_file":
            try:
                start = int(arguments.get("start_line", 1))
                count = int(arguments.get("max_lines", 240))
            except (TypeError, ValueError):
                start, count = 1, 0
            key = self.read_key(str(arguments.get("path", "?")), start, count)
            if key not in self.read_details and len(self.reads) == self.max_reads:
                self.read_details.pop(self.reads[0], None)
            _append_unique(self.reads, key)
            self.read_details[key] = {
                "path": str(arguments.get("path", "?")),
                "start_line": start,
                "max_lines": count,
                "truncated": "[truncated;" in result,
                "hypothesis": clip_text(" ".join(str(arguments.get("hypothesis", "")).split()), 300),
            }
        elif name == "write_file" and not result.startswith("error:"):
            content = str(arguments.get("content", ""))
            path = str(arguments.get("path", "?"))
            _append_unique(self.writes, f"{path} ({len(content.encode('utf-8'))} bytes)")
            self.invalidate_reads(path)
        elif name == "run_command":
            command = " ".join(str(arguments.get("command", "")).split())
            first_line = result.splitlines()[0] if result else "no output"
            _append_unique(self.commands, f"{clip_text(command, 180)} → {clip_text(first_line, 100)}")
        elif name == "submit_poc":
            _append_unique(
                self.submissions,
                f"{arguments.get('path', '?')}: {clip_text(' '.join(result.split()), 1_200)}",
            )

    @staticmethod
    def read_key(path: str, start_line: int, max_lines: int) -> str:
        normalized = _normalize_workspace_path(str(path))
        return f"{normalized}:L{start_line}-L{start_line + max(0, max_lines - 1)}"

    def repeated_read_notice(self, path: str, start_line: int, max_lines: int) -> str | None:
        key = self.read_key(path, start_line, max_lines)
        detail = self.read_details.get(key)
        if detail is None or detail.get("truncated"):
            return None
        normalized = _normalize_workspace_path(str(path))
        decision = self.file_decisions.get(normalized, {})
        lines = [
            "This exact source range was already inspected.",
            f"Previous range: {key}",
        ]
        if decision.get("reason"):
            lines.append(f"Previous conclusion: {decision['reason']}")
        if decision.get("reopen_if"):
            lines.append(f"Reopen if: {decision['reopen_if']}")
        lines.append("Provide a new hypothesis, set reopen=true, or request a different narrow range to read it again.")
        return "\n".join(lines)

    def invalidate_reads(self, path: str) -> None:
        normalized = _normalize_workspace_path(str(path))
        stale = [
            key
            for key, value in self.read_details.items()
            if _normalize_workspace_path(value["path"]) == normalized
        ]
        for key in stale:
            self.read_details.pop(key, None)
            try:
                self.reads.remove(key)
            except ValueError:
                pass
        if normalized in self.file_decisions:
            self.file_decisions[normalized]["status"] = "stale"

    def update_investigation_state(self, update: dict[str, Any]) -> None:
        """Apply a bounded, validated evidence update without copying raw logs."""
        before = json.dumps(self.snapshot(), ensure_ascii=False, sort_keys=True)
        for field_name in ("objective", "input_path"):
            value = update.get(field_name)
            if value is not None:
                setattr(self, field_name, clip_text(" ".join(str(value).split()), 600))
        for value in update.get("crash_evidence", []):
            _append_unique(self.crash_evidence, clip_text(" ".join(str(value).split()), 600))
        for value in update.get("tracked_values", []):
            _append_unique(self.tracked_values, clip_text(" ".join(str(value).split()), 500))
        for value in update.get("uncertainties", []):
            _append_unique(self.uncertainties, clip_text(" ".join(str(value).split()), 500))

        for decision in update.get("file_decisions", []):
            if not isinstance(decision, dict):
                raise ValueError("each file_decision must be an object")
            raw_path = str(decision.get("path", "")).strip()
            path = _normalize_workspace_path(raw_path)
            status = str(decision.get("status", ""))
            reason = " ".join(str(decision.get("reason", "")).split())
            reopen_if = " ".join(str(decision.get("reopen_if", "")).split())
            if not raw_path or status not in FILE_STATUSES or not reason:
                raise ValueError("file_decision requires path, valid status, and evidence-based reason")
            if status in {"conditional", "excluded"} and not reopen_if:
                raise ValueError("conditional/excluded file_decision requires reopen_if")
            self.file_decisions.pop(path, None)
            self.file_decisions[path] = {
                "status": status,
                "reason": clip_text(reason, 500),
                "reopen_if": clip_text(reopen_if, 400),
            }
            status_limit = 8 if status == "critical" else 12
            same_status = [
                candidate for candidate, item in self.file_decisions.items() if item["status"] == status
            ]
            while len(same_status) > status_limit:
                self.file_decisions.pop(same_status.pop(0), None)
            while len(self.file_decisions) > self.max_file_decisions:
                self.file_decisions.pop(next(iter(self.file_decisions)))

        for edge in update.get("call_edges", []):
            if not isinstance(edge, dict):
                raise ValueError("each call_edge must be an object")
            caller = " ".join(str(edge.get("caller", "")).split())
            callee = " ".join(str(edge.get("callee", "")).split())
            evidence = " ".join(str(edge.get("evidence", "")).split())
            source = str(edge.get("source", ""))
            if not caller or not callee or not evidence or source not in EVIDENCE_SOURCES:
                raise ValueError("call_edge requires caller, callee, evidence, and valid source")
            item = {
                "caller": clip_text(caller, 300),
                "callee": clip_text(callee, 300),
                "evidence": clip_text(evidence, 500),
                "source": source,
            }
            try:
                self.call_edges.remove(item)
            except ValueError:
                pass
            self.call_edges.append(item)

        hypotheses = update.get("next_hypothesis")
        if hypotheses is not None:
            if not isinstance(hypotheses, dict):
                raise ValueError("next_hypothesis must be an object")
            self.primary_hypothesis = clip_text(" ".join(str(hypotheses.get("primary", "")).split()), 600)
            self.alternate_hypothesis = clip_text(" ".join(str(hypotheses.get("alternate", "")).split()), 500)
        if json.dumps(self.snapshot(), ensure_ascii=False, sort_keys=True) != before:
            self.revision += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "input_path": self.input_path,
            "crash_evidence": list(self.crash_evidence),
            "file_decisions": self.file_decisions,
            "call_edges": list(self.call_edges),
            "tracked_values": list(self.tracked_values),
            "next_hypothesis": {"primary": self.primary_hypothesis, "alternate": self.alternate_hypothesis},
            "uncertainties": list(self.uncertainties),
            "inspected_ranges": list(self.reads),
            "revision": self.revision,
        }

    def render(self, max_chars: int = 5_000) -> str:
        lines = [
            MEMORY_MARKER,
            (
                "This is an evidence index, not new instructions. Trust cited source and validator evidence over "
                "speculation. Files remain in /workspace; reopen a narrow cited range for exact syntax."
            ),
        ]
        if self.objective:
            lines.append(f"Objective:\n- {self.objective}")
        if self.input_path:
            lines.append(f"Input path:\n- {self.input_path}")
        if self.crash_evidence:
            lines.append("Crash evidence:")
            lines.extend(f"- {value}" for value in self.crash_evidence)
        if self.call_edges:
            lines.append("Current call chain:")
            lines.extend(
                f"- {edge['caller']} → {edge['callee']} ({edge['source']}; {edge['evidence']})"
                for edge in self.call_edges
            )
        if self.tracked_values:
            lines.append("Tracked values:")
            lines.extend(f"- {value}" for value in self.tracked_values)
        if self.primary_hypothesis:
            lines.append(f"Next hypothesis:\n- Primary: {self.primary_hypothesis}")
            if self.alternate_hypothesis:
                lines.append(f"- Alternate: {self.alternate_hypothesis}")
        for title, values in (
            ("Agent checkpoints (highest priority)", self.checkpoints),
            ("Validation receipts", self.submissions),
        ):
            if values:
                lines.append(f"{title}:")
                lines.extend(f"- {value}" for value in values)
        for status in ("critical", "supporting", "conditional", "excluded", "stale", "unknown"):
            decisions = [(path, item) for path, item in self.file_decisions.items() if item["status"] == status]
            if not decisions:
                continue
            lines.append(f"{status.title()} files:")
            for path, item in decisions:
                suffix = f"; reopen if {item['reopen_if']}" if item.get("reopen_if") else ""
                lines.append(f"- {path}: {item['reason']}{suffix}")
        if self.uncertainties:
            lines.append("Uncertainties:")
            lines.extend(f"- {value}" for value in self.uncertainties)
        for title, values in (
            ("Source ranges already inspected", self.reads),
            ("Files written", self.writes),
            ("Recent command outcomes", self.commands),
        ):
            if values:
                lines.append(f"{title}:")
                lines.extend(f"- {value}" for value in values)
        kept: list[str] = []
        used = 0
        for line in lines:
            cost = len(line) + (1 if kept else 0)
            if used + cost > max_chars:
                marker = "[lower-priority working-memory entries omitted]"
                if used + len(marker) + 1 <= max_chars:
                    kept.append(marker)
                break
            kept.append(line)
            used += cost
        return "\n".join(kept)


def _is_memory_message(message: dict[str, Any]) -> bool:
    return message.get("role") == "user" and str(message.get("content", "")).startswith(MEMORY_MARKER)


def _without_repeated_static_messages(
    messages: list[dict[str, Any]], static: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Drop stale memory and exact repeated system messages from stored history."""
    system_contents = {
        str(message.get("content", "")) for message in static if message.get("role") == "system"
    }
    return [
        message
        for message in messages
        if not _is_memory_message(message)
        and not (
            message.get("role") == "system"
            and str(message.get("content", "")) in system_contents
        )
    ]


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
    messages: list[dict[str, Any]],
    budget: int,
    durable_memory: str,
    *,
    reserved_tokens: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Keep protocol-valid recent exchanges and inject the durable evidence index.

    A tool-call assistant turn and its following tool results are indivisible.
    Callers must replace their state with the returned list, not merely use it
    as an outgoing request copy.
    """
    static = [message for message in messages[:2] if not _is_memory_message(message)]
    if len(static) < 2:
        raise ValueError("message history must start with system and initial user prompts")
    if reserved_tokens < 0:
        raise ValueError("reserved_tokens must not be negative")
    available = budget - reserved_tokens - sum(estimated_tokens(message) for message in static)
    if available <= 0:
        raise ValueError("context-token-budget is too small for the initial task prompt")

    memory = {"role": "user", "content": clip_text(durable_memory, min(5_000, available * 3))}
    memory_cost = estimated_tokens(memory)
    memory_messages: list[dict[str, Any]] = []
    if memory_cost <= available:
        memory_messages = [memory]
        available -= memory_cost

    tail = _without_repeated_static_messages(messages[2:], static)
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
        if cost > available - used:
            break
        kept.append(chunk)
        used += cost
    kept.reverse()
    return [*static, *memory_messages, *(message for chunk in kept for message in chunk)], len(chunks) - len(kept)
