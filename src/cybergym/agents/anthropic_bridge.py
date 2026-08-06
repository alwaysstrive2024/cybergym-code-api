"""Local Anthropic Messages to OpenAI Chat Completions bridge.

Claude Code speaks the Anthropic Messages protocol.  CyberGym API profiles
currently point at OpenAI-compatible providers, so this process performs the
small, auditable protocol conversion needed by the Claude Code backend.  It is
bound to loopback by the launcher and pins every request to the profile model.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

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
    prompt_cache_key_mode: str = "off"


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _stable_tool_call_id(*parts: Any) -> str:
    """Derive a retry-stable ID when an upstream protocol omits one."""
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return f"call_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:24]}"


def _request_namespace(payload: dict[str, Any], explicit_session_id: str | None = None) -> str:
    """Create a non-sensitive namespace, preferring an explicit validated session UUID."""
    if explicit_session_id:
        try:
            normalized = str(UUID(explicit_session_id))
        except ValueError:
            normalized = ""
        if normalized:
            return "session_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    messages = payload.get("messages", [])
    first_message = messages[0] if messages else {}
    stable_start = {
        "system": payload.get("system"),
        "first_message": first_message,
        "model": payload.get("model"),
    }
    encoded = json.dumps(stable_start, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _reasoning_key(namespace: str, tool_id: str) -> str:
    return f"{namespace}:{tool_id}" if namespace else tool_id


def _anthropic_usage(usage: Any, *, default_input: int = 0, default_output: int = 0) -> dict[str, int]:
    """Map OpenAI token usage without double-counting cached prompt tokens."""
    prompt_tokens = int(_value(usage, "prompt_tokens", default_input) or default_input)
    output_tokens = int(_value(usage, "completion_tokens", default_output) or default_output)
    details = _value(usage, "prompt_tokens_details")
    cached_tokens = int(_value(details, "cached_tokens", 0) or 0)
    cached_tokens = max(0, min(prompt_tokens, cached_tokens))
    mapped = {
        "input_tokens": prompt_tokens - cached_tokens,
        "output_tokens": output_tokens,
    }
    if details is not None:
        mapped["cache_read_input_tokens"] = cached_tokens
        mapped["cache_creation_input_tokens"] = 0
    return mapped


def _classify_upstream_error(exc: Exception) -> tuple[int, str, str | None]:
    """Preserve actionable upstream status instead of flattening every failure to 502."""
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return 429, "rate_limit_error", "30"
    if isinstance(status_code, int) and 500 <= status_code <= 599:
        return status_code, "api_error", None
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return 504, "timeout_error", None
    return 502, "api_error", None


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PromptCacheMonitor:
    """Track prefix stability and cache usage without retaining request content."""

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}

    def before_request(self, namespace: str, request: dict[str, Any]) -> dict[str, Any]:
        message_hashes = [_canonical_sha256(message) for message in request.get("messages", [])]
        tools_hash = _canonical_sha256(request.get("tools", []))
        state = self._sessions.setdefault(namespace, {"request_count": 0, "cache_hits": 0})
        previous_hashes = state.get("message_hashes", [])
        previous_tools_hash = state.get("tools_sha256")
        common = 0
        if previous_tools_hash in {None, tools_hash}:
            for old, new in zip(previous_hashes, message_hashes, strict=False):
                if old != new:
                    break
                common += 1
        is_prefix = bool(previous_hashes) and common == len(previous_hashes)
        state.update(
            request_count=int(state["request_count"]) + 1,
            message_hashes=message_hashes,
            tools_sha256=tools_hash,
            message_count=len(message_hashes),
            common_prefix_messages=common,
            previous_request_is_prefix=is_prefix,
            request_messages_sha256=_canonical_sha256(message_hashes),
        )
        return self._public_state(namespace, state)

    def after_usage(self, namespace: str, usage: dict[str, int]) -> dict[str, Any]:
        state = self._sessions.setdefault(namespace, {"request_count": 0, "cache_hits": 0})
        cached = int(usage.get("cache_read_input_tokens", 0))
        uncached = int(usage.get("input_tokens", 0))
        total = cached + uncached
        if cached > 0:
            state["cache_hits"] = int(state.get("cache_hits", 0)) + 1
        state.update(
            prompt_tokens=total,
            cached_tokens=cached,
            cache_hit_rate=(cached / total if total else 0.0),
        )
        return self._public_state(namespace, state)

    def snapshot(self) -> dict[str, Any]:
        sessions = {
            namespace: self._public_state(namespace, state)
            for namespace, state in self._sessions.items()
        }
        return {"session_count": len(sessions), "sessions": sessions}

    @staticmethod
    def _public_state(namespace: str, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "session_namespace": namespace,
            **{key: value for key, value in state.items() if key != "message_hashes"},
        }


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
    reasoning_namespace: str = "",
) -> dict[str, Any]:
    """Convert one Anthropic Messages request without forwarding Claude-only beta fields."""
    messages: list[dict[str, Any]] = []
    system_text = _text_from_blocks(payload.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for message_index, source in enumerate(payload.get("messages", [])):
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
        for block_index, block in enumerate(content):
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(str(block.get("text", "")))
            elif block_type == "tool_use":
                tool_id = block.get("id") or _stable_tool_call_id(
                    "anthropic", message_index, block_index, block.get("name", ""), block.get("input", {})
                )
                tool_calls.append(
                    {
                        "id": tool_id,
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
                        reasoning_cache[_reasoning_key(reasoning_namespace, call["id"])]
                        for call in tool_calls
                        if _reasoning_key(reasoning_namespace, call["id"]) in reasoning_cache
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
    reasoning_namespace: str = "",
) -> None:
    if reasoning_cache is None or not reasoning_content:
        return
    for tool_id in tool_ids:
        key = _reasoning_key(reasoning_namespace, tool_id)
        existing = reasoning_cache.get(key)
        if existing is not None and existing != reasoning_content:
            raise ValueError("upstream reused one tool-call ID with different reasoning")
        reasoning_cache[key] = reasoning_content


def openai_to_anthropic(
    response: Any,
    requested_model: str,
    reasoning_cache: dict[str, str] | None = None,
    reasoning_namespace: str = "",
    tool_id_seed: str = "",
) -> dict[str, Any]:
    choice = _value(response, "choices", [])[0]
    message = _value(choice, "message")
    blocks: list[dict[str, Any]] = []
    content = _value(message, "content")
    if content:
        blocks.append({"type": "text", "text": content})
    tool_calls = _value(message, "tool_calls", []) or []
    tool_ids: list[str] = []
    for call_index, call in enumerate(tool_calls):
        function = _value(call, "function", {})
        raw_arguments = _value(function, "arguments", "{}") or "{}"
        try:
            parsed_arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            parsed_arguments = {"_raw": raw_arguments}
        tool_id = _value(call, "id") or _stable_tool_call_id(
            "openai",
            tool_id_seed or _value(response, "id", ""),
            call_index,
            _value(function, "name", ""),
            raw_arguments,
        )
        tool_ids.append(tool_id)
        blocks.append(
            {
                "type": "tool_use",
                "id": tool_id,
                "name": _value(function, "name", ""),
                "input": parsed_arguments,
            }
        )
    _remember_reasoning(
        reasoning_cache, tool_ids, _value(message, "reasoning_content"), reasoning_namespace
    )
    usage = _value(response, "usage")
    return {
        "id": _value(response, "id") or f"msg_{uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": blocks,
        "stop_reason": _stop_reason(_value(choice, "finish_reason"), has_tools=bool(tool_calls)),
        "stop_sequence": None,
        "usage": _anthropic_usage(usage),
    }


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def openai_stream_to_anthropic(
    stream: Any,
    requested_model: str,
    reasoning_cache: dict[str, str] | None = None,
    reasoning_namespace: str = "",
    usage_observer: Callable[[dict[str, int]], None] | None = None,
    tool_id_seed: str = "",
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
    cached_input_tokens: int | None = None
    reasoning_parts: list[str] = []
    try:
        async for chunk in stream:
            usage = _value(chunk, "usage")
            if usage:
                input_tokens = int(_value(usage, "prompt_tokens", input_tokens) or input_tokens)
                output_tokens = int(_value(usage, "completion_tokens", output_tokens) or output_tokens)
                details = _value(usage, "prompt_tokens_details")
                if details is not None:
                    cached_input_tokens = int(_value(details, "cached_tokens", 0) or 0)
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
            call["id"] = call["id"] or _stable_tool_call_id(
                "openai-stream", tool_id_seed, call_index, call["name"], call["arguments"]
            )
            yield _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": next_block_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": call["id"],
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
            reasoning_namespace,
        )
        mapped_usage = _anthropic_usage(
            {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                **(
                    {"prompt_tokens_details": {"cached_tokens": cached_input_tokens}}
                    if cached_input_tokens is not None
                    else {}
                ),
            }
        )
        if usage_observer is not None:
            usage_observer(mapped_usage)
        yield _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": _stop_reason(finish_reason, has_tools=bool(tool_buffers)),
                    "stop_sequence": None,
                },
                "usage": mapped_usage,
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
    cache_monitor = PromptCacheMonitor()

    def authorized(request: Request) -> bool:
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        api_key = request.headers.get("x-api-key", "")
        return hmac.compare_digest(bearer, config.gateway_token) or hmac.compare_digest(
            api_key, config.gateway_token
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "model": config.upstream_model}

    @app.get("/cache-status")
    async def cache_status() -> dict[str, Any]:
        return cache_monitor.snapshot()

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
        reasoning_namespace = _request_namespace(
            payload, request.headers.get("x-cybergym-session-id")
        )
        requested_model = str(payload.get("model") or config.upstream_model)
        converted = anthropic_to_openai(
            payload, config.upstream_model, reasoning_cache, reasoning_namespace
        )
        if config.prompt_cache_key_mode == "stable":
            converted["prompt_cache_key"] = reasoning_namespace
        request_diagnostic = cache_monitor.before_request(reasoning_namespace, converted)
        LOG.info("prompt_cache_request %s", json.dumps(request_diagnostic, sort_keys=True))
        try:
            if payload.get("stream"):
                stream = await upstream.chat.completions.create(
                    **converted,
                    stream=True,
                    stream_options={"include_usage": True},
                )
                return StreamingResponse(
                    openai_stream_to_anthropic(
                        stream,
                        requested_model,
                        reasoning_cache,
                        reasoning_namespace,
                        lambda usage: LOG.info(
                            "prompt_cache_usage %s",
                            json.dumps(
                                cache_monitor.after_usage(reasoning_namespace, usage), sort_keys=True
                            ),
                        ),
                        request_diagnostic["request_messages_sha256"],
                    ),
                    media_type="text/event-stream",
                    headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
                )
            response = await upstream.chat.completions.create(**converted)
            converted_response = openai_to_anthropic(
                response,
                requested_model,
                reasoning_cache,
                reasoning_namespace,
                request_diagnostic["request_messages_sha256"],
            )
            LOG.info(
                "prompt_cache_usage %s",
                json.dumps(
                    cache_monitor.after_usage(reasoning_namespace, converted_response["usage"]),
                    sort_keys=True,
                ),
            )
            return JSONResponse(converted_response)
        except Exception as exc:
            # Do not log the request payload: it can contain benchmark context.
            LOG.warning("Upstream chat completion failed: %s: %s", type(exc).__name__, exc)
            status_code, error_type, retry_after = _classify_upstream_error(exc)
            return JSONResponse(
                status_code=status_code,
                content={
                    "type": "error",
                    "error": {
                        "type": error_type,
                        "message": f"upstream request failed: {type(exc).__name__}: {exc}",
                    },
                },
                headers={"Retry-After": retry_after} if retry_after else None,
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
    prompt_cache_key_mode = os.environ.get("CYBERGYM_PROMPT_CACHE_KEY_MODE", "off")
    if prompt_cache_key_mode not in {"off", "stable"}:
        raise RuntimeError("CYBERGYM_PROMPT_CACHE_KEY_MODE must be off or stable")
    return BridgeConfig(
        upstream_base_url=os.environ["CYBERGYM_UPSTREAM_BASE_URL"],
        upstream_model=os.environ["CYBERGYM_UPSTREAM_MODEL"],
        upstream_api_key=os.environ["CYBERGYM_UPSTREAM_API_KEY"],
        gateway_token=os.environ["CYBERGYM_CLAUDE_GATEWAY_TOKEN"],
        prompt_cache_key_mode=prompt_cache_key_mode,
    )


def main() -> None:
    args = parse_args()
    uvicorn.run(create_app(config_from_environment()), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
