"""Provider-independent exploration policy and trajectory metrics."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

PolicyMode = Literal["baseline", "guided", "enforced"]


@dataclass(frozen=True)
class PolicyConfig:
    mode: PolicyMode = "guided"
    read_call_budget: int = 18
    source_char_budget: int = 120_000
    stale_tool_limit: int = 6
    first_submit_tool_deadline: int = 12
    version: str = "context-v1"

    def __post_init__(self) -> None:
        if self.mode not in {"baseline", "guided", "enforced"}:
            raise ValueError("invalid policy mode")
        if min(
            self.read_call_budget,
            self.source_char_budget,
            self.stale_tool_limit,
            self.first_submit_tool_deadline,
        ) < 1:
            raise ValueError("policy budgets must be positive")


@dataclass
class PolicyState:
    tool_calls: int = 0
    read_calls: int = 0
    unique_read_ranges: int = 0
    repeated_read_blocks: int = 0
    source_chars_visible: int = 0
    raw_tool_chars: int = 0
    processed_tool_chars: int = 0
    model_visible_tool_chars: int = 0
    first_candidate_write_step: int | None = None
    first_submission_step: int | None = None
    submission_count: int = 0
    invalid_submission_count: int = 0
    hypothesis_revision: int = 0
    tools_since_hypothesis_change: int = 0
    policy_guidance_count: int = 0
    policy_block_count: int = 0
    phase: str = "exploration"
    awaiting_hypothesis_after_invalid_submission: bool = False

    def snapshot(self, config: PolicyConfig) -> dict[str, Any]:
        return {**asdict(self), "config": asdict(config)}


@dataclass(frozen=True)
class PolicyDecision:
    reason: str
    message: str
    blocked: bool


class ExplorationPolicy:
    """Track metrics in every mode and optionally guide/block broad browsing."""

    BROWSING_TOOLS = {"list_files", "read_file", "run_command"}

    def __init__(self, config: PolicyConfig | None = None):
        self.config = config or PolicyConfig()
        self.state = PolicyState()
        self._read_ranges: set[str] = set()
        self._last_hypothesis_revision = 0

    def set_phase(self, phase: str) -> None:
        self.state.phase = phase

    @staticmethod
    def _is_narrow_hypothesis_read(name: str, arguments: dict[str, Any]) -> bool:
        try:
            max_lines = int(arguments.get("max_lines", 240))
        except (TypeError, ValueError):
            return False
        return (
            name == "read_file"
            and bool(str(arguments.get("hypothesis", "")).strip())
            and bool(str(arguments.get("expected_evidence", "")).strip())
            and 1 <= max_lines <= 200
        )

    def before_tool(self, name: str, arguments: dict[str, Any]) -> PolicyDecision | None:
        if self.config.mode == "baseline" or name not in self.BROWSING_TOOLS:
            return None
        narrow = self._is_narrow_hypothesis_read(name, arguments)
        reason = ""
        if self.state.awaiting_hypothesis_after_invalid_submission and not narrow:
            reason = "invalid_submission_requires_hypothesis_revision"
        elif (
            self.state.read_calls >= self.config.read_call_budget
            or self.state.source_chars_visible >= self.config.source_char_budget
        ) and not narrow:
            reason = "read_budget"
        elif self.state.tools_since_hypothesis_change >= self.config.stale_tool_limit and not narrow:
            reason = "stale_hypothesis"
        elif (
            self.state.tool_calls >= self.config.first_submit_tool_deadline
            and self.state.submission_count == 0
            and not narrow
        ):
            reason = "first_submit_deadline"
        if not reason:
            return None
        blocked = self.config.mode == "enforced"
        action = (
            "Update the primary hypothesis with evidence, write/adjust the best candidate, or submit it. "
            "A read_file call remains allowed when it is <=200 lines and includes hypothesis plus expected_evidence."
        )
        return PolicyDecision(reason=reason, message=action, blocked=blocked)

    def observe_before_result(self, name: str, arguments: dict[str, Any], hypothesis_revision: int) -> None:
        self.state.tool_calls += 1
        previous_revision = self._last_hypothesis_revision
        self.sync_hypothesis_revision(hypothesis_revision)
        if hypothesis_revision == previous_revision and name in self.BROWSING_TOOLS:
            self.state.tools_since_hypothesis_change += 1
        if name == "read_file":
            self.state.read_calls += 1
            key = f"{arguments.get('path', '?')}:{arguments.get('start_line', 1)}:{arguments.get('max_lines', 240)}"
            self._read_ranges.add(key)
            self.state.unique_read_ranges = len(self._read_ranges)
        elif name == "write_file" and self.state.first_candidate_write_step is None:
            self.state.first_candidate_write_step = self.state.tool_calls

    def sync_hypothesis_revision(self, hypothesis_revision: int) -> None:
        if hypothesis_revision != self._last_hypothesis_revision:
            self.state.hypothesis_revision = hypothesis_revision
            self.state.tools_since_hypothesis_change = 0
            self.state.awaiting_hypothesis_after_invalid_submission = False
            self._last_hypothesis_revision = hypothesis_revision

    def observe_visible_result(
        self, name: str, result: str, *, raw_chars: int = 0, processed_chars: int = 0
    ) -> None:
        self.state.raw_tool_chars += raw_chars
        self.state.processed_tool_chars += processed_chars
        self.state.model_visible_tool_chars += len(result)
        if name == "read_file":
            self.state.source_chars_visible += len(result)
        if name == "submit_poc":
            self.state.submission_count += 1
            if self.state.first_submission_step is None:
                self.state.first_submission_step = self.state.tool_calls

    def observe_submission(self, valid: bool) -> None:
        if valid:
            self.state.awaiting_hypothesis_after_invalid_submission = False
        else:
            self.state.invalid_submission_count += 1
            self.state.awaiting_hypothesis_after_invalid_submission = True

    def repeat_blocked(self) -> None:
        self.state.repeated_read_blocks += 1
