from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from cybergym.agents.anthropic_bridge import (
    BridgeConfig,
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
        self.assertEqual(reasoning_cache, {})

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


if __name__ == "__main__":
    unittest.main()
