from __future__ import annotations

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

from scripts.evaluation import run_claude_code_eval as runner


def result(*, turns: int, subtype: str = "error_max_turns", is_error: bool = True) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=turns,
        session_id="test-session",
    )


class FakeExecutor:
    def __init__(self) -> None:
        self.has_valid_differential_submission = False
        self.submissions: list[dict[str, str]] = []
        self.events: list[dict[str, object]] = []

    def record(self, event: dict[str, object]) -> None:
        self.events.append(event)

    def working_memory(self) -> str:
        return "bounded memory"


@pytest.mark.asyncio
async def test_consume_query_treats_sdk_max_turn_exception_as_session_boundary(monkeypatch) -> None:
    boundary = result(turns=25)

    async def fake_query(**kwargs):
        yield boundary
        raise Exception("Claude Code returned an error result: Reached maximum number of turns (24)")

    monkeypatch.setattr(runner, "query", fake_query)
    captured, _ = await runner.consume_query(
        prompt="synthetic",
        options=ClaudeAgentOptions(max_turns=24),
        executor=FakeExecutor(),
    )

    assert captured is boundary
    assert runner.is_expected_session_boundary(captured)


@pytest.mark.asyncio
async def test_run_agent_rotates_sessions_until_total_turn_budget(monkeypatch) -> None:
    executor = FakeExecutor()
    phase_budgets: list[int] = []

    async def fake_consume_query(*, prompt, options, executor):
        phase_budgets.append(options.max_turns)
        if len(phase_budgets) == 3:
            executor.submissions.append({"path": "candidate"})
        return result(turns=options.max_turns + 1), None

    monkeypatch.setattr(runner, "consume_query", fake_consume_query)
    results, _, finalization_attempts = await runner.run_agent(
        prompt="synthetic",
        options=ClaudeAgentOptions(max_turns=50),
        executor=executor,
        timeout=5,
        session_turn_budget=24,
    )

    assert phase_budgets == [24, 24, 2]
    assert len(results) == 3
    assert finalization_attempts == 0
    assert [event["phase"] for event in executor.events if event.get("event") == "context_session_started"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_run_agent_reaches_finalization_after_all_exploration_sessions(monkeypatch) -> None:
    executor = FakeExecutor()
    phase_budgets: list[int] = []

    async def fake_consume_query(*, prompt, options, executor):
        phase_budgets.append(options.max_turns)
        return result(turns=options.max_turns + 1), None

    monkeypatch.setattr(runner, "consume_query", fake_consume_query)
    results, _, finalization_attempts = await runner.run_agent(
        prompt="synthetic",
        options=ClaudeAgentOptions(max_turns=25),
        executor=executor,
        timeout=5,
        session_turn_budget=24,
    )

    assert phase_budgets == [24, 1, 3, 3, 3]
    assert len(results) == 5
    assert finalization_attempts == runner.FINALIZATION_ATTEMPTS
