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
        self.infrastructure_failure = None
        self.submissions: list[dict[str, str]] = []
        self.events: list[dict[str, object]] = []

    def record(self, event: dict[str, object]) -> None:
        self.events.append(event)

    def working_memory(self) -> str:
        return "bounded memory"

    def set_execution_phase(self, phase: str, allowed_names=None) -> None:
        self.phase = phase


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
    resumed_sessions: list[str | None] = []
    explicit_session_ids: list[str | None] = []

    async def fake_consume_query(*, prompt, options, executor):
        phase_budgets.append(options.max_turns)
        resumed_sessions.append(options.resume)
        explicit_session_ids.append(options.session_id)
        if len(phase_budgets) == 3:
            executor.submissions.append({"path": "candidate"})
        return result(turns=options.max_turns + 1), None

    monkeypatch.setattr(runner, "consume_query", fake_consume_query)
    results, _, finalization_attempts = await runner.run_agent(
        prompt="synthetic",
        options=ClaudeAgentOptions(
            max_turns=50,
            session_id="11111111-1111-4111-8111-111111111111",
        ),
        executor=executor,
        timeout=5,
        session_turn_budget=24,
    )

    assert phase_budgets == [24, 24, 2]
    assert resumed_sessions == [None, "test-session", "test-session"]
    assert explicit_session_ids == ["11111111-1111-4111-8111-111111111111", None, None]
    assert len(results) == 3
    assert finalization_attempts == 0
    assert [event["phase"] for event in executor.events if event.get("event") == "context_session_started"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_run_agent_reaches_finalization_after_all_exploration_sessions(monkeypatch) -> None:
    executor = FakeExecutor()
    phase_budgets: list[int] = []
    resumed_sessions: list[str | None] = []
    explicit_session_ids: list[str | None] = []

    async def fake_consume_query(*, prompt, options, executor):
        phase_budgets.append(options.max_turns)
        resumed_sessions.append(options.resume)
        explicit_session_ids.append(options.session_id)
        return result(turns=options.max_turns + 1), None

    monkeypatch.setattr(runner, "consume_query", fake_consume_query)
    results, _, finalization_attempts = await runner.run_agent(
        prompt="synthetic",
        options=ClaudeAgentOptions(
            max_turns=25,
            session_id="22222222-2222-4222-8222-222222222222",
        ),
        executor=executor,
        timeout=5,
        session_turn_budget=24,
    )

    assert phase_budgets == [24, 1, 3, 3, 3]
    assert resumed_sessions == [None, "test-session", "test-session", "test-session", "test-session"]
    assert explicit_session_ids == [
        "22222222-2222-4222-8222-222222222222",
        None,
        None,
        None,
        None,
    ]
    assert len(results) == 5
    assert finalization_attempts == runner.FINALIZATION_ATTEMPTS
