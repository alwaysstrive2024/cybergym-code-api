"""Local Anthropic Messages to OpenAI Chat Completions bridge.

Claude Code speaks the Anthropic Messages protocol.  CyberGym API profiles
currently point at OpenAI-compatible providers, so this process performs the
small, auditable protocol conversion needed by the Claude Code backend.  It is
bound to loopback by the launcher and pins every request to the profile model.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AsyncOpenAI


LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class BridgeConfig:
    upstream_base_url: str
    upstream_model: str
    upstream_api_key: str
    gateway_token: str


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _text_from_blocks(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: list[str] = []
    for block in content:
        block_type = _value(block, "type")
        if block_type == "text":
            parts.append(str(_value(block, "text", "")))
        elif block_type == "resource":
            resource = _value(block, "resource", {})
            parts.append(str(_value(resource, "text", "")))
    return "\n".join(part for part in parts if part)


def anthropic_to_openai(
    payload: dict[str, Any],
    upstream_model: str,
    reasoning_cache: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Convert one Anthropic Messages request without forwarding Claude-only beta fields."""
    messages: list[dict[str, Any]] = []
    system_text = _text_from_blocks(payload.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for source in payload.get("messages", []):
        role = source.get("role")
        content = source.get("content", "")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            messages.append({"role": role, "content": str(content)})
            continue

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        for block in content:
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(str(block.get("text", "")))
            elif block_type == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id") or f"call_{uuid4().hex}",
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                        },
                    }
                )
            elif block_type == "tool_result":
                result_text = _text_from_blocks(block.get("content", ""))
                if block.get("is_error"):
                    result_text = f"tool_error: {result_text}"
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": result_text,
                    }
                )

        if role == "assistant":
            converted: dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(part for part in text_parts if part) or None,
            }
            if tool_calls:
                converted["tool_calls"] = tool_calls
                if reasoning_cache is not None:
                    cached_values = [
                        reasoning_cache.pop(call["id"])
                        for call in tool_calls
                        if call["id"] in reasoning_cache
                    ]
                    cached_reasoning = cached_values[0] if cached_values else None
                    if cached_reasoning:
                        # DeepSeek thinking endpoints require their prior
                        # reasoning_content alongside the assistant tool call.
                        # Keep it only in the bridge; it is never exposed as an
                        # Anthropic text/thinking block or written to a run log.
                        converted["reasoning_content"] = cached_reasoning
            messages.append(converted)
        else:
            # Anthropic places tool results in a user turn.  OpenAI represents
            # each result as its own tool message.
            messages.extend(tool_results)
            if text_parts or not tool_results:
                messages.append({"role": role, "content": "\n".join(text_parts)})

    tools: list[dict[str, Any]] = []
    for source_tool in payload.get("tools", []):
        name = source_tool.get("name")
        if not name:
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": source_tool.get("description", ""),
                    "parameters": source_tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )

    request: dict[str, Any] = {
        "model": upstream_model,
        "messages": messages,
        "max_tokens": int(payload.get("max_tokens", 4096)),
    }
    if tools:
        request["tools"] = tools
    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type")
        if choice_type == "tool" and tool_choice.get("name"):
            request["tool_choice"] = {
                "type": "function",
                "function": {"name": tool_choice["name"]},
            }
        elif choice_type == "any":
            request["tool_choice"] = "required"
        elif choice_type in {"auto", "none"}:
            request["tool_choice"] = choice_type
    for source_name, target_name in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stop_sequences", "stop"),
    ):
        if payload.get(source_name) is not None:
            request[target_name] = payload[source_name]
    return request


def _stop_reason(value: str | None, *, has_tools: bool = False) -> str:
    if has_tools or value in {"tool_calls", "function_call"}:
        return "tool_use"
    if value in {"length", "max_tokens"}:
        return "max_tokens"
    if value in {"content_filter", "stop_sequence"}:
        return "stop_sequence"
    return "end_turn"


def _remember_reasoning(
    reasoning_cache: dict[str, str] | None,
    tool_ids: list[str],
    reasoning_content: str | None,
) -> None:
    if reasoning_cache is None or not reasoning_content:
        return
    while len(reasoning_cache) >= 4096:
        reasoning_cache.pop(next(iter(reasoning_cache)))
    for tool_id in tool_ids:
        reasoning_cache[tool_id] = reasoning_content


def openai_to_anthropic(
    response: Any,
    requested_model: str,
    reasoning_cache: dict[str, str] | None = None,
) -> dict[str, Any]:
    choice = _value(response, "choices", [])[0]
    message = _value(choice, "message")
    blocks: list[dict[str, Any]] = []
    content = _value(message, "content")
    if content:
        blocks.append({"type": "text", "text": content})
    tool_calls = _value(message, "tool_calls", []) or []
    tool_ids: list[str] = []
    for call in tool_calls:
        function = _value(call, "function", {})
        raw_arguments = _value(function, "arguments", "{}") or "{}"
        try:
            parsed_arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            parsed_arguments = {"_raw": raw_arguments}
        tool_id = _value(call, "id") or f"call_{uuid4().hex}"
        tool_ids.append(tool_id)
        blocks.append(
            {
                "type": "tool_use",
                "id": tool_id,
                "name": _value(function, "name", ""),
                "input": parsed_arguments,
            }
        )
    _remember_reasoning(reasoning_cache, tool_ids, _value(message, "reasoning_content"))
    usage = _value(response, "usage")
    return {
        "id": _value(response, "id") or f"msg_{uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": blocks,
        "stop_reason": _stop_reason(_value(choice, "finish_reason"), has_tools=bool(tool_calls)),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(_value(usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(_value(usage, "completion_tokens", 0) or 0),
        },
    }


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def openai_stream_to_anthropic(
    stream: Any,
    requested_model: str,
    reasoning_cache: dict[str, str] | None = None,
) -> AsyncIterator[str]:
    message_id = f"msg_{uuid4().hex}"
    yield _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": requested_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    text_open = False
    next_block_index = 0
    finish_reason: str | None = None
    tool_buffers: dict[int, dict[str, str]] = {}
    input_tokens = 0
    output_tokens = 0
    reasoning_parts: list[str] = []
    try:
        async for chunk in stream:
            usage = _value(chunk, "usage")
            if usage:
                input_tokens = int(_value(usage, "prompt_tokens", input_tokens) or input_tokens)
                output_tokens = int(_value(usage, "completion_tokens", output_tokens) or output_tokens)
            choices = _value(chunk, "choices", []) or []
            if not choices:
                continue
            choice = choices[0]
            finish_reason = _value(choice, "finish_reason") or finish_reason
            delta = _value(choice, "delta")
            text = _value(delta, "content")
            reasoning_content = _value(delta, "reasoning_content")
            if reasoning_content:
                reasoning_parts.append(reasoning_content)
            if text:
                if not text_open:
                    yield _sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": next_block_index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                    text_open = True
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": next_block_index,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            for call in _value(delta, "tool_calls", []) or []:
                call_index = int(_value(call, "index", 0) or 0)
                function = _value(call, "function", {})
                buffer = tool_buffers.setdefault(call_index, {"id": "", "name": "", "arguments": ""})
                buffer["id"] += _value(call, "id", "") or ""
                buffer["name"] += _value(function, "name", "") or ""
                buffer["arguments"] += _value(function, "arguments", "") or ""

        if text_open:
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": next_block_index})
            next_block_index += 1
        for call_index in sorted(tool_buffers):
            call = tool_buffers[call_index]
            yield _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": next_block_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": call["id"] or f"call_{uuid4().hex}",
                        "name": call["name"],
                        "input": {},
                    },
                },
            )
            if call["arguments"]:
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": next_block_index,
                        "delta": {"type": "input_json_delta", "partial_json": call["arguments"]},
                    },
                )
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": next_block_index})
            next_block_index += 1
        _remember_reasoning(
            reasoning_cache,
            [call["id"] for call in tool_buffers.values() if call["id"]],
            "".join(reasoning_parts) or None,
        )
        yield _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": _stop_reason(finish_reason, has_tools=bool(tool_buffers)),
                    "stop_sequence": None,
                },
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            },
        )
        yield _sse("message_stop", {"type": "message_stop"})
    except Exception as exc:
        yield _sse(
            "error",
            {
                "type": "error",
                "error": {"type": "api_error", "message": f"upstream stream failed: {type(exc).__name__}: {exc}"},
            },
        )


def create_app(config: BridgeConfig, client: Any | None = None) -> FastAPI:
    app = FastAPI(title="CyberGym Anthropic bridge")
    upstream = client or AsyncOpenAI(
        base_url=config.upstream_base_url,
        api_key=config.upstream_api_key,
        max_retries=0,
    )
    reasoning_cache: dict[str, str] = {}

    def authorized(request: Request) -> bool:
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        api_key = request.headers.get("x-api-key", "")
        return hmac.compare_digest(bearer, config.gateway_token) or hmac.compare_digest(
            api_key, config.gateway_token
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "model": config.upstream_model}

    @app.get("/v1/models")
    async def models(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse(status_code=401, content={"type": "error", "error": {"type": "authentication_error", "message": "invalid gateway credential"}})
        return JSONResponse(
            {"data": [{"id": config.upstream_model, "display_name": f"Claude Code runtime / {config.upstream_model}"}]}
        )

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse(status_code=401, content={"type": "error", "error": {"type": "authentication_error", "message": "invalid gateway credential"}})
        payload = await request.json()
        text = json.dumps(payload.get("messages", []), ensure_ascii=False) + _text_from_blocks(payload.get("system"))
        return JSONResponse({"input_tokens": max(1, (len(text) + 3) // 4)})

    @app.post("/v1/messages")
    async def messages(request: Request):
        if not authorized(request):
            return JSONResponse(status_code=401, content={"type": "error", "error": {"type": "authentication_error", "message": "invalid gateway credential"}})
        payload = await request.json()
        requested_model = str(payload.get("model") or config.upstream_model)
        converted = anthropic_to_openai(payload, config.upstream_model, reasoning_cache)
        try:
            if payload.get("stream"):
                stream = await upstream.chat.completions.create(**converted, stream=True)
                return StreamingResponse(
                    openai_stream_to_anthropic(stream, requested_model, reasoning_cache),
                    media_type="text/event-stream",
                    headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
                )
            response = await upstream.chat.completions.create(**converted)
            return JSONResponse(openai_to_anthropic(response, requested_model, reasoning_cache))
        except Exception as exc:
            # Do not log the request payload: it can contain benchmark context.
            LOG.warning("Upstream chat completion failed: %s: %s", type(exc).__name__, exc)
            wrapped_rate_limit = "HTTP 429" in str(exc)
            return JSONResponse(
                status_code=429 if wrapped_rate_limit else 502,
                content={
                    "type": "error",
                    "error": {
                        "type": "rate_limit_error" if wrapped_rate_limit else "api_error",
                        "message": f"upstream request failed: {type(exc).__name__}: {exc}",
                    },
                },
                headers={"Retry-After": "30"} if wrapped_rate_limit else None,
            )

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    return parser.parse_args()


def config_from_environment() -> BridgeConfig:
    names = (
        "CYBERGYM_UPSTREAM_BASE_URL",
        "CYBERGYM_UPSTREAM_MODEL",
        "CYBERGYM_UPSTREAM_API_KEY",
        "CYBERGYM_CLAUDE_GATEWAY_TOKEN",
    )
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"missing bridge environment variables: {', '.join(missing)}")
    return BridgeConfig(
        upstream_base_url=os.environ["CYBERGYM_UPSTREAM_BASE_URL"],
        upstream_model=os.environ["CYBERGYM_UPSTREAM_MODEL"],
        upstream_api_key=os.environ["CYBERGYM_UPSTREAM_API_KEY"],
        gateway_token=os.environ["CYBERGYM_CLAUDE_GATEWAY_TOKEN"],
    )


def main() -> None:
    args = parse_args()
    uvicorn.run(create_app(config_from_environment()), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
