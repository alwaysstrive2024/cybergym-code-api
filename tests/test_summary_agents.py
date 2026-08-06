from cybergym.agents.summary_agents import _bounded_summary_input, _parse_json_object


def test_summary_json_parser_accepts_fenced_object_and_rejects_array() -> None:
    assert _parse_json_object('```json\n{"artifact_type":"source"}\n```')["artifact_type"] == "source"
    try:
        _parse_json_object("[]")
    except ValueError as exc:
        assert "JSON object" in str(exc)
    else:
        raise AssertionError("array summary should be rejected")


def test_summary_input_has_a_hard_limit_and_explicit_omission() -> None:
    bounded = _bounded_summary_input("x" * 100_000, limit=1_000)
    assert len(bounded) < 1_100
    assert "deterministic middle omission" in bounded
