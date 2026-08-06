from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cybergym.agents.policy import PolicyConfig
from cybergym.agents.runtime import TaskSandbox, ToolExecutor


class FakeSandbox:
    def __init__(self, result: str):
        self.result = result
        self.read_calls = 0

    def read_file(self, path: str, start_line: int, max_lines: int, max_chars: int) -> str:
        self.read_calls += 1
        return self.result

    def read_file_bytes(self, path: str) -> bytes:
        return self.result.encode()


def make_executor(
    tmp_path: Path,
    sandbox: FakeSandbox,
    max_chars: int = 12_288,
    summary_provider=None,
    policy_config=None,
) -> ToolExecutor:
    return ToolExecutor(
        tmp_path,
        sandbox,  # type: ignore[arg-type]
        tmp_path / "trajectory.jsonl",
        agent_facing_task_id="task",
        agent_id="agent",
        checksum="checksum",
        server="http://unused.invalid",
        max_tool_result_chars=max_chars,
        summary_provider=summary_provider,
        policy_config=policy_config,
    )


def last_tool_event(tmp_path: Path) -> dict:
    events = [json.loads(line) for line in (tmp_path / "trajectory.jsonl").read_text().splitlines()]
    return next(event for event in reversed(events) if event.get("event") == "tool")


def test_runtime_persists_raw_and_processed_views_for_short_results(tmp_path: Path) -> None:
    raw = "     1\t/*\n     2\t * Copyright 2019 Example\n     3\t * GNU General Public License\n     4\t */\n     5\tint f(void);\n"
    executor = make_executor(tmp_path, FakeSandbox(raw))

    visible = executor.invoke("read_file", {"path": "source.c"})

    assert "Copyright" not in visible
    assert (tmp_path / "tool-results/raw/00001-read_file.txt").read_text() == raw
    assert (tmp_path / "tool-results/processed/00001-read_file.txt").read_text() == visible
    event = last_tool_event(tmp_path)
    assert event["result_view"] == "processed"
    assert event["processing"]["license_lines_omitted"] == 4
    assert len(event["raw_result_sha256"]) == 64
    assert len(event["processed_result_sha256"]) == 64
    assert event["raw_result_sha256"] != event["processed_result_sha256"]


def test_runtime_warns_before_exact_repeat_and_allows_new_hypothesis(tmp_path: Path) -> None:
    raw = "     1\tint f(void);\n"
    sandbox = FakeSandbox(raw)
    executor = make_executor(tmp_path, sandbox)
    arguments = {"path": "source.c", "start_line": 1, "max_lines": 10}

    executor.invoke("read_file", arguments)
    warning = executor.invoke("read_file", arguments)
    reopened = executor.invoke("read_file", {**arguments, "hypothesis": "verify a changed caller assumption"})

    assert "already inspected" in warning
    assert reopened == raw
    assert sandbox.read_calls == 2


def test_runtime_bounds_processed_not_raw_result(tmp_path: Path) -> None:
    raw = "exit_code=1\n" + ("noise\n" * 1000) + "ERROR: AddressSanitizer: heap-buffer-overflow\n#0 0x1 in parse a.c:9\n"
    executor = make_executor(tmp_path, FakeSandbox("unused"), max_chars=500)

    visible, metadata = executor._bounded_result("run_command", raw)

    assert "AddressSanitizer" in visible
    assert metadata["result_view"] == "summary"
    assert metadata["raw_result_chars"] == len(raw)
    assert "sanitizer_or_runtime_log" in visible
    assert metadata["summary_kind"] == "deterministic_evidence"
    assert (tmp_path / metadata["summary_result_path"]).is_file()


def test_list_files_prioritizes_evidence_files_and_prunes_noise() -> None:
    sandbox = object.__new__(TaskSandbox)

    def fake_exec(command, *, workdir=None):
        shell = command[-1]
        assert "-name .git" in shell
        assert "-name node_modules" in shell
        output = (
            b"f\t/workspace/z/deep.c\n"
            b"f\t/workspace/src/parser.c\n"
            b"f\t/workspace/tests/fuzz_decode.c\n"
            b"f\t/workspace/README.md\n"
        )
        return 0, output, b""

    sandbox._exec = fake_exec  # type: ignore[method-assign]
    visible = sandbox.list_files(".", max_entries=3)

    assert visible.splitlines() == [
        "README.md",
        "tests/fuzz_decode.c",
        "src/parser.c",
        "[truncated; showing 3 prioritized entries]",
    ]


def test_langgraph_exports_the_shared_runtime() -> None:
    from scripts.evaluation import run_langgraph_eval

    assert run_langgraph_eval.TaskSandbox is TaskSandbox
    assert run_langgraph_eval.ToolExecutor is ToolExecutor


def test_selective_agent_summary_is_validated_and_audited(tmp_path: Path) -> None:
    def summarize(name, processed, arguments):
        assert name == "read_file"
        assert "int parse" in processed
        return {
            "artifact_type": "source",
            "file": arguments["path"],
            "proven_facts": [{"location": "parser.c:2", "fact": "parse accepts a length"}],
            "uncertainties": ["destination capacity is not shown"],
        }

    raw = "".join(f"{line:>6}\tint parse_{line}(int length);\n" for line in range(1, 100))
    executor = make_executor(tmp_path, FakeSandbox(raw), max_chars=300, summary_provider=summarize)
    visible = executor.invoke("read_file", {"path": "parser.c"})
    event = last_tool_event(tmp_path)

    assert '"artifact_type": "source"' in visible
    assert event["summary_kind"] == "independent_agent"
    assert (tmp_path / event["summary_result_path"]).is_file()


def test_invalid_agent_summary_falls_back_to_processed_truncation(tmp_path: Path) -> None:
    def invalid_summary(name, processed, arguments):
        return {"artifact_type": "source", "proven_facts": [{"fact": "missing location"}]}

    raw = "".join(f"{line:>6}\tint value_{line};\n" for line in range(1, 100))
    executor = make_executor(tmp_path, FakeSandbox(raw), max_chars=300, summary_provider=invalid_summary)
    executor.invoke("read_file", {"path": "source.c"})
    event = last_tool_event(tmp_path)

    assert event["result_view"] == "processed_truncated"
    assert event["summary_fallback"] == "ValueError"


def test_enforced_policy_blocks_broad_read_after_budget(tmp_path: Path) -> None:
    sandbox = FakeSandbox("     1\tint value;\n")
    executor = make_executor(
        tmp_path,
        sandbox,
        policy_config=PolicyConfig(mode="enforced", read_call_budget=1),
    )
    executor.invoke("read_file", {"path": "a.c"})
    blocked = executor.invoke("read_file", {"path": "b.c"})

    assert blocked.startswith("policy_blocked: read_budget")
    assert sandbox.read_calls == 1
    assert executor.metrics()["policy_block_count"] == 1


def test_executor_finalization_phase_rejects_non_submission_tools(tmp_path: Path) -> None:
    executor = make_executor(tmp_path, FakeSandbox("     1\tint value;\n"))
    executor.set_execution_phase("finalization", {"submit_poc"})

    result = executor.invoke("read_file", {"path": "source.c"})

    assert result == "error: tool read_file is not available in this phase"


def test_submit_server_error_marks_infrastructure_failure(tmp_path: Path) -> None:
    executor = make_executor(tmp_path, FakeSandbox("poc"))
    executor.differential_submit = True
    response = SimpleNamespace(
        status_code=500,
        text='{"detail":"Docker registry TLS handshake timeout"}',
        is_success=False,
        json=lambda: {"detail": "Docker registry TLS handshake timeout"},
    )

    with patch("cybergym.agents.runtime.httpx.post", return_value=response):
        result = executor.invoke("submit_poc", {"path": "poc"})

    assert '"status_code": 500' in result
    assert executor.infrastructure_failure == {
        "operation": "submit_poc",
        "status_code": 500,
        "response": {"detail": "Docker registry TLS handshake timeout"},
    }
