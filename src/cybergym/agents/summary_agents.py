"""Optional model-backed tool-result summarizers with a strict JSON contract."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

SUMMARY_SYSTEM_PROMPT = """You compress authorized vulnerability-research tool evidence.
Return one JSON object only. Never invent a fact. Every proven_facts item must contain a source location such as
file.c:123 and a short fact. Put uncertain interpretations in uncertainties. Do not produce exploit conclusions.
Use these optional keys when supported: artifact_type, file, range, purpose, relevant_symbols, proven_facts,
call_edges, constraints, relevance, relevance_reason, discardable_sections, next_questions, uncertainties."""


def _bounded_summary_input(text: str, limit: int = 48_000) -> str:
    if len(text) <= limit:
        return text
    head = limit * 3 // 4
    return text[:head] + "\n[deterministic middle omission before summarization]\n" + text[-(limit - head) :]


def _parse_json_object(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1] if "\n" in value else value[3:]
        value = value.rsplit("```", 1)[0]
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("summary response must be a JSON object")
    return parsed


def make_anthropic_http_summarizer(
    *, base_url: str, model: str, api_key: str | None = None, auth_token: str | None = None
) -> Callable[[str, str, dict[str, Any]], dict[str, Any]]:
    root = base_url.rstrip("/")
    endpoint = root + ("/messages" if root.endswith("/v1") else "/v1/messages")

    def summarize(tool_name: str, processed: str, arguments: dict[str, Any]) -> dict[str, Any]:
        headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
        if api_key:
            headers["x-api-key"] = api_key
        if auth_token:
            headers["authorization"] = f"Bearer {auth_token}"
        response = httpx.post(
            endpoint,
            headers=headers,
            json={
                "model": model,
                "max_tokens": 1_500,
                "temperature": 0,
                "system": SUMMARY_SYSTEM_PROMPT,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"tool={tool_name}\narguments={json.dumps(arguments, ensure_ascii=False)}\n\n"
                            + _bounded_summary_input(processed)
                        ),
                    }
                ],
            },
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()
        blocks = payload.get("content", [])
        text = "\n".join(str(block.get("text", "")) for block in blocks if block.get("type") == "text")
        return _parse_json_object(text)

    return summarize


def make_openai_summarizer(client: Any, model: str) -> Callable[[str, str, dict[str, Any]], dict[str, Any]]:
    def summarize(tool_name: str, processed: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=1_500,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"tool={tool_name}\narguments={json.dumps(arguments, ensure_ascii=False)}\n\n"
                        + _bounded_summary_input(processed)
                    ),
                },
            ],
        )
        return _parse_json_object(response.choices[0].message.content or "")

    return summarize
