from cybergym.agents.policy import ExplorationPolicy, PolicyConfig


def test_baseline_collects_metrics_without_guidance() -> None:
    policy = ExplorationPolicy(PolicyConfig(mode="baseline", first_submit_tool_deadline=1))
    policy.observe_before_result("read_file", {"path": "a.c"}, 0)

    assert policy.before_tool("read_file", {"path": "b.c"}) is None
    assert policy.state.read_calls == 1


def test_guided_policy_warns_after_stale_browsing_without_blocking() -> None:
    policy = ExplorationPolicy(PolicyConfig(mode="guided", stale_tool_limit=2))
    policy.observe_before_result("list_files", {"path": "."}, 0)
    policy.observe_before_result("run_command", {"command": "rg parse"}, 0)
    decision = policy.before_tool("list_files", {"path": "src"})

    assert decision is not None
    assert decision.reason == "stale_hypothesis"
    assert decision.blocked is False


def test_enforced_policy_allows_narrow_falsifiable_read_after_budget() -> None:
    policy = ExplorationPolicy(PolicyConfig(mode="enforced", read_call_budget=1))
    policy.observe_before_result("read_file", {"path": "a.c"}, 0)

    broad = policy.before_tool("read_file", {"path": "b.c", "max_lines": 240})
    narrow = policy.before_tool(
        "read_file",
        {
            "path": "b.c",
            "max_lines": 40,
            "hypothesis": "count reaches the array unchecked",
            "expected_evidence": "assignment or bounds check",
        },
    )

    assert broad is not None and broad.blocked
    assert narrow is None


def test_invalid_submission_requires_hypothesis_revision_for_broad_browsing() -> None:
    policy = ExplorationPolicy(PolicyConfig(mode="enforced"))
    policy.observe_submission(valid=False)
    assert policy.before_tool("run_command", {"command": "find ."}) is not None

    policy.observe_before_result("update_investigation_state", {}, 1)
    assert policy.before_tool("run_command", {"command": "rg parse"}) is None


def test_invalid_narrow_read_arguments_do_not_escape_policy_gate() -> None:
    policy = ExplorationPolicy(PolicyConfig(mode="enforced", read_call_budget=1))
    policy.observe_before_result("read_file", {"path": "a.c"}, 0)
    decision = policy.before_tool(
        "read_file",
        {"path": "b.c", "max_lines": "invalid", "hypothesis": "h", "expected_evidence": "e"},
    )
    assert decision is not None and decision.blocked
