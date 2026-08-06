from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from cybergym.agents.anthropic_bridge import (
    BridgeConfig,
    PromptCacheMonitor,
    _classify_upstream_error,
    _request_namespace,
    anthropic_to_openai,
    create_app,
    openai_stream_to_anthropic,
    openai_to_anthropic,
)


class FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        self._iterator = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration:
            raise StopAsyncIteration from None


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.response


class ClaudeCodeBridgeTests(unittest.TestCase):
    def test_cache_monitor_detects_append_only_prefix_and_cached_usage(self):
        monitor = PromptCacheMonitor()
        first = {
            "model": "model",
            "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
            "tools": [{"type": "function", "function": {"name": "read"}}],
        }
        second = {
            **first,
            "messages": [*first["messages"], {"role": "assistant", "content": "a"}],
        }
        first_state = monitor.before_request("session_hash", first)
        second_state = monitor.before_request("session_hash", second)
        usage_state = monitor.after_usage(
            "session_hash", {"input_tokens": 20, "output_tokens": 1, "cache_read_input_tokens": 80}
        )
        self.assertFalse(first_state["previous_request_is_prefix"])
        self.assertTrue(second_state["previous_request_is_prefix"])
        self.assertEqual(second_state["common_prefix_messages"], 2)
        self.assertEqual(usage_state["cached_tokens"], 80)
        self.assertEqual(usage_state["cache_hit_rate"], 0.8)
        self.assertNotIn("message_hashes", monitor.snapshot()["sessions"]["session_hash"])

    def test_cache_monitor_detects_changed_historical_prefix(self):
        monitor = PromptCacheMonitor()
        monitor.before_request("session_hash", {"messages": [{"role": "system", "content": "first"}]})
        changed = monitor.before_request(
            "session_hash", {"messages": [{"role": "system", "content": "changed"}]}
        )
        self.assertFalse(changed["previous_request_is_prefix"])
        self.assertEqual(changed["common_prefix_messages"], 0)

    def test_explicit_session_uuid_controls_namespace_without_prompt_content(self):
        session_a = "11111111-1111-4111-8111-111111111111"
        session_b = "22222222-2222-4222-8222-222222222222"
        payload = {"system": "same", "messages": [{"role": "user", "content": "same"}]}
        namespace_a = _request_namespace(payload, session_a)
        namespace_b = _request_namespace(payload, session_b)
        self.assertNotEqual(namespace_a, namespace_b)
        self.assertEqual(namespace_a, _request_namespace({"messages": []}, session_a))
        self.assertNotIn(session_a, namespace_a)

    def test_upstream_errors_keep_actionable_status_classes(self):
        rate_limit = RuntimeError("limited")
        rate_limit.status_code = 429
        service_unavailable = RuntimeError("unavailable")
        service_unavailable.status_code = 503
        self.assertEqual(_classify_upstream_error(rate_limit), (429, "rate_limit_error", "30"))
        self.assertEqual(_classify_upstream_error(service_unavailable), (503, "api_error", None))
        self.assertEqual(_classify_upstream_error(TimeoutError("slow")), (504, "timeout_error", None))
        self.assertEqual(_classify_upstream_error(ConnectionError("closed")), (502, "api_error", None))

    def test_anthropic_tool_exchange_converts_to_openai(self):
        converted = anthropic_to_openai(
            {
                "system": [{"type": "text", "text": "system"}],
                "messages": [
                    {"role": "user", "content": "inspect"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "checking"},
                            {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.c"}},
                        ],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "source"}],
                    },
                ],
                "tools": [
                    {
                        "name": "read_file",
                        "description": "read",
                        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                    }
                ],
                "max_tokens": 123,
            },
            "deepseek-test",
        )
        self.assertEqual(converted["model"], "deepseek-test")
        self.assertEqual([message["role"] for message in converted["messages"]], ["system", "user", "assistant", "tool"])
        self.assertEqual(converted["messages"][2]["tool_calls"][0]["function"]["arguments"], '{"path": "a.c"}')
        self.assertEqual(converted["messages"][3]["tool_call_id"], "toolu_1")
        self.assertEqual(converted["tools"][0]["function"]["name"], "read_file")

    def test_openai_tool_call_converts_to_anthropic(self):
        reasoning_cache: dict[str, str] = {}
        response = SimpleNamespace(
            id="chat_1",
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    message=SimpleNamespace(
                        content="candidate",
                        reasoning_content="private upstream reasoning",
                        tool_calls=[
                            SimpleNamespace(
                                id="call_1",
                                function=SimpleNamespace(name="submit_poc", arguments='{"path":"poc.bin"}'),
                            )
                        ],
                    ),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )
        converted = openai_to_anthropic(response, "deepseek-test", reasoning_cache)
        self.assertEqual(converted["stop_reason"], "tool_use")
        self.assertEqual(converted["content"][1]["type"], "tool_use")
        self.assertEqual(converted["content"][1]["input"], {"path": "poc.bin"})
        self.assertEqual(converted["usage"], {"input_tokens": 10, "output_tokens": 5})
        self.assertNotIn("reasoning", json.dumps(converted))
        follow_up = anthropic_to_openai(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "call_1", "name": "submit_poc", "input": {"path": "poc.bin"}}
                        ],
                    },
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}]},
                ],
                "max_tokens": 10,
            },
            "deepseek-test",
            reasoning_cache,
        )
        self.assertEqual(follow_up["messages"][0]["reasoning_content"], "private upstream reasoning")
        self.assertEqual(reasoning_cache, {"call_1": "private upstream reasoning"})
        repeated_follow_up = anthropic_to_openai(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "call_1", "name": "submit_poc", "input": {"path": "poc.bin"}}
                        ],
                    },
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}]},
                ],
                "max_tokens": 10,
            },
            "deepseek-test",
            reasoning_cache,
        )
        self.assertEqual(repeated_follow_up["messages"], follow_up["messages"])
        self.assertEqual(reasoning_cache, {"call_1": "private upstream reasoning"})

    def test_nonstreaming_cached_token_usage_is_mapped_without_double_counting(self):
        response = SimpleNamespace(
            id="chat_cached",
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="ok", tool_calls=[]))],
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=7,
                prompt_tokens_details=SimpleNamespace(cached_tokens=60),
            ),
        )
        converted = openai_to_anthropic(response, "deepseek-test")
        self.assertEqual(
            converted["usage"],
            {
                "input_tokens": 40,
                "output_tokens": 7,
                "cache_read_input_tokens": 60,
                "cache_creation_input_tokens": 0,
            },
        )

    def test_gateway_auth_and_nonstreaming_request(self):
        response = SimpleNamespace(
            id="chat_2",
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="ok", tool_calls=[]))],
            usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
        )
        completions = FakeCompletions(response)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        app = create_app(
            BridgeConfig("http://unused", "deepseek-test", "upstream", "gateway-secret"),
            client=fake_client,
        )
        with TestClient(app) as client:
            unauthorized = client.post("/v1/messages", json={})
            self.assertEqual(unauthorized.status_code, 401)
            accepted = client.post(
                "/v1/messages",
                headers={"Authorization": "Bearer gateway-secret"},
                json={"model": "claude-alias", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["model"], "claude-alias")
        self.assertEqual(completions.requests[0]["model"], "deepseek-test")

    def test_stable_prompt_cache_key_is_forwarded(self):
        response = SimpleNamespace(
            id="chat_cache_key",
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="ok", tool_calls=[]))],
            usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
        )
        completions = FakeCompletions(response)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        app = create_app(
            BridgeConfig(
                "http://unused",
                "deepseek-test",
                "upstream",
                "gateway-secret",
                prompt_cache_key_mode="stable",
            ),
            client=fake_client,
        )
        payload = {
            "model": "claude-alias",
            "max_tokens": 10,
            "system": [{"type": "text", "text": "stable system"}],
            "messages": [{"role": "user", "content": "stable first message"}],
        }
        with TestClient(app) as client:
            first = client.post(
                "/v1/messages", headers={"Authorization": "Bearer gateway-secret"}, json=payload
            )
            second = client.post(
                "/v1/messages", headers={"Authorization": "Bearer gateway-secret"}, json=payload
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            completions.requests[0]["prompt_cache_key"],
            completions.requests[1]["prompt_cache_key"],
        )

    def test_streaming_gateway_requests_upstream_usage(self):
        stream = FakeStream(
            [
                SimpleNamespace(
                    choices=[],
                    usage=SimpleNamespace(
                        prompt_tokens=100,
                        completion_tokens=1,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=80),
                    ),
                )
            ]
        )
        completions = FakeCompletions(stream)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        app = create_app(
            BridgeConfig("http://unused", "deepseek-test", "upstream", "gateway-secret"),
            client=fake_client,
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/messages",
                headers={"Authorization": "Bearer gateway-secret"},
                json={"max_tokens": 8, "stream": True, "messages": [{"role": "user", "content": "x"}]},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(completions.requests[0]["stream"])
        self.assertEqual(completions.requests[0]["stream_options"], {"include_usage": True})

    def test_missing_anthropic_tool_id_is_retry_stable(self):
        payload = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "read_file", "input": {"path": "a.c"}}],
                }
            ]
        }
        first = anthropic_to_openai(payload, "deepseek-test")
        second = anthropic_to_openai(payload, "deepseek-test")
        self.assertEqual(first["messages"], second["messages"])
        self.assertTrue(first["messages"][0]["tool_calls"][0]["id"].startswith("call_"))

    def test_reasoning_cache_is_isolated_by_session_namespace(self):
        reasoning_cache = {
            "session-a:call_1": "reasoning a",
            "session-b:call_1": "reasoning b",
        }
        payload = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "call_1", "name": "read_file", "input": {}}],
                }
            ]
        }
        converted_a = anthropic_to_openai(payload, "deepseek-test", reasoning_cache, "session-a")
        converted_b = anthropic_to_openai(payload, "deepseek-test", reasoning_cache, "session-b")
        self.assertEqual(converted_a["messages"][0]["reasoning_content"], "reasoning a")
        self.assertEqual(converted_b["messages"][0]["reasoning_content"], "reasoning b")
        self.assertEqual(len(reasoning_cache), 2)

    def test_reused_tool_id_cannot_overwrite_historical_reasoning(self):
        reasoning_cache = {"session-a:call_1": "original reasoning"}
        response = SimpleNamespace(
            id="chat_collision",
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    message=SimpleNamespace(
                        content=None,
                        reasoning_content="different reasoning",
                        tool_calls=[
                            SimpleNamespace(
                                id="call_1",
                                function=SimpleNamespace(name="read_file", arguments="{}"),
                            )
                        ],
                    ),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
        with self.assertRaisesRegex(ValueError, "reused one tool-call ID"):
            openai_to_anthropic(response, "deepseek-test", reasoning_cache, "session-a")
        self.assertEqual(reasoning_cache["session-a:call_1"], "original reasoning")

    def test_missing_openai_tool_id_is_unique_per_request_seed(self):
        response = SimpleNamespace(
            id="",
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    message=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="",
                                function=SimpleNamespace(name="read_file", arguments="{}"),
                            )
                        ],
                    ),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
        first = openai_to_anthropic(response, "deepseek-test", tool_id_seed="request-a")
        second = openai_to_anthropic(response, "deepseek-test", tool_id_seed="request-b")
        self.assertNotEqual(first["content"][0]["id"], second["content"][0]["id"])


class ClaudeCodeStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_tool_call_uses_anthropic_sse_events(self):
        call = SimpleNamespace(
            index=0,
            id="call_1",
            function=SimpleNamespace(name="submit_poc", arguments='{"path":"poc.bin"}'),
        )
        stream = FakeStream(
            [
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[call]), finish_reason=None)],
                    usage=None,
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[]), finish_reason="tool_calls")],
                    usage=None,
                ),
            ]
        )
        raw = "".join([event async for event in openai_stream_to_anthropic(stream, "deepseek-test")])
        payloads = [
            json.loads(line.removeprefix("data: "))
            for line in raw.splitlines()
            if line.startswith("data: ")
        ]
        tool_start = next(
            payload
            for payload in payloads
            if payload.get("type") == "content_block_start"
            and payload.get("content_block", {}).get("type") == "tool_use"
        )
        self.assertEqual(tool_start["content_block"]["name"], "submit_poc")
        delta = next(
            payload
            for payload in payloads
            if payload.get("type") == "content_block_delta"
            and payload.get("delta", {}).get("type") == "input_json_delta"
        )
        self.assertEqual(delta["delta"]["partial_json"], '{"path":"poc.bin"}')
        message_delta = next(payload for payload in payloads if payload.get("type") == "message_delta")
        self.assertEqual(message_delta["delta"]["stop_reason"], "tool_use")

    async def test_streaming_missing_tool_id_uses_same_id_for_reasoning_cache(self):
        reasoning_cache: dict[str, str] = {}
        call = SimpleNamespace(
            index=0,
            id="",
            function=SimpleNamespace(name="read_file", arguments='{"path":"a.c"}'),
        )
        stream = FakeStream(
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                reasoning_content="synthetic reasoning",
                                tool_calls=[call],
                            ),
                            finish_reason="tool_calls",
                        )
                    ],
                    usage=None,
                )
            ]
        )
        raw = "".join(
            [event async for event in openai_stream_to_anthropic(stream, "deepseek-test", reasoning_cache)]
        )
        payloads = [
            json.loads(line.removeprefix("data: "))
            for line in raw.splitlines()
            if line.startswith("data: ")
        ]
        tool_start = next(
            payload
            for payload in payloads
            if payload.get("type") == "content_block_start"
            and payload.get("content_block", {}).get("type") == "tool_use"
        )
        effective_id = tool_start["content_block"]["id"]
        self.assertEqual(reasoning_cache, {effective_id: "synthetic reasoning"})

    async def test_streaming_cached_token_usage_is_mapped_without_double_counting(self):
        stream = FakeStream(
            [
                SimpleNamespace(
                    choices=[],
                    usage=SimpleNamespace(
                        prompt_tokens=100,
                        completion_tokens=7,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=60),
                    ),
                )
            ]
        )
        raw = "".join([event async for event in openai_stream_to_anthropic(stream, "deepseek-test")])
        payloads = [
            json.loads(line.removeprefix("data: "))
            for line in raw.splitlines()
            if line.startswith("data: ")
        ]
        message_delta = next(payload for payload in payloads if payload.get("type") == "message_delta")
        self.assertEqual(
            message_delta["usage"],
            {
                "input_tokens": 40,
                "output_tokens": 7,
                "cache_read_input_tokens": 60,
                "cache_creation_input_tokens": 0,
            },
        )


if __name__ == "__main__":
    unittest.main()
