#!/usr/bin/env python3
"""Serve one local Qwen model through a small OpenAI-compatible API.

This intentionally uses Transformers' native BitsAndBytes loader instead of
vLLM's BNB loader: the latter currently fails on the Qwen3.6 MoE checkpoints.
The server is single-request serialized so deterministic benchmark settings
remain meaningful and the 8-bit model has one controlled CUDA allocation.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import re
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import torch
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, StoppingCriteria, StoppingCriteriaList


LOG = logging.getLogger("cybergym.transformers_8bit")
TOOL_CALL_PATTERN = re.compile(
    r"<tool_call>\s*<function=([^>\s]+)>\s*(.*?)\s*</function>\s*</tool_call>",
    re.DOTALL,
)
PARAMETER_PATTERN = re.compile(r"<parameter=([^>\s]+)>\s*(.*?)\s*</parameter>", re.DOTALL)


class GenerationWallClockLimit(StoppingCriteria):
    """Stop synchronous `generate` between tokens after a fixed wall-clock budget."""

    def __init__(self, limit_seconds: float):
        self.limit_seconds = limit_seconds
        self.started = time.perf_counter()
        self.expired = False

    def __call__(self, input_ids: Any, scores: Any, **kwargs: Any) -> bool:
        self.expired = time.perf_counter() - self.started >= self.limit_seconds
        return self.expired


def arguments_from_xml(body: str) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    for name, raw_value in PARAMETER_PATTERN.findall(body):
        value = raw_value.strip()
        try:
            arguments[name] = json.loads(value)
        except json.JSONDecodeError:
            arguments[name] = value
    return arguments


def parse_tool_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    for name, body in TOOL_CALL_PATTERN.findall(text):
        calls.append(
            {
                "id": f"call_{uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments_from_xml(body), ensure_ascii=False),
                },
            }
        )
    content = TOOL_CALL_PATTERN.sub("", text).strip() or None
    return content, calls


def template_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI's JSON-string tool arguments to Qwen template mappings."""
    normalized = copy.deepcopy(messages)
    for message in normalized:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, Mapping) else None
            if not isinstance(function, dict):
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    decoded = json.loads(arguments)
                except json.JSONDecodeError:
                    decoded = {"_raw": arguments}
                function["arguments"] = decoded if isinstance(decoded, Mapping) else {"value": decoded}
    return normalized


class ModelServer:
    def __init__(self, model_repository: str, served_model_name: str, revision: str | None, max_generation_seconds: float):
        self.model_repository = model_repository
        self.served_model_name = served_model_name
        self.revision = revision
        self.max_generation_seconds = max_generation_seconds
        self.tokenizer: Any = None
        self.model: Any = None
        self.lock = asyncio.Lock()

    def load(self) -> None:
        kwargs: dict[str, Any] = {"trust_remote_code": True}
        if self.revision:
            kwargs["revision"] = self.revision
        LOG.info("Loading tokenizer for %s at revision %s", self.model_repository, self.revision or "default")
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_repository, **kwargs)
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        LOG.info("Loading 8-bit model weights with Transformers BitsAndBytes")
        self.model = AutoModelForCausalLM.from_pretrained(
            self.model_repository,
            **kwargs,
            quantization_config=BitsAndBytesConfig(load_in_8bit=True),
            torch_dtype=torch.bfloat16,
            device_map="auto",
        )
        self.model.eval()
        LOG.info("Model loaded; hf_device_map=%s", getattr(self.model, "hf_device_map", None))

    def _generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("stream"):
            raise HTTPException(status_code=400, detail="streaming is not enabled for this benchmark server")
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=400, detail="messages must be a non-empty list")
        tools = payload.get("tools")
        if tools is not None and not isinstance(tools, list):
            raise HTTPException(status_code=400, detail="tools must be a list")
        try:
            max_tokens = min(max(int(payload.get("max_tokens") or 4096), 1), 8192)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="max_tokens must be an integer") from exc
        try:
            temperature = float(payload.get("temperature") or 0.0)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="temperature must be numeric") from exc
        seed = payload.get("seed")
        if seed is not None:
            try:
                torch.manual_seed(int(seed))
                torch.cuda.manual_seed_all(int(seed))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="seed must be an integer") from exc
        try:
            prompt = self.tokenizer.apply_chat_template(
                template_messages(messages),
                tools=tools,
                tokenize=False,
                add_generation_prompt=True,
            )
        except Exception as exc:  # surface malformed OpenAI message sequences clearly
            raise HTTPException(status_code=400, detail=f"invalid chat template input: {exc}") from exc
        encoded = self.tokenizer(prompt, return_tensors="pt")
        input_device = self.model.get_input_embeddings().weight.device
        encoded = {key: value.to(input_device) for key, value in encoded.items()}
        do_sample = temperature > 0
        generation_kwargs: dict[str, Any] = {
            **encoded,
            "max_new_tokens": max_tokens,
            "do_sample": do_sample,
            "use_cache": True,
            "pad_token_id": self.tokenizer.pad_token_id,
        }
        wall_clock_limit = GenerationWallClockLimit(self.max_generation_seconds)
        generation_kwargs["stopping_criteria"] = StoppingCriteriaList([wall_clock_limit])
        if do_sample:
            generation_kwargs["temperature"] = temperature
            generation_kwargs["top_p"] = float(payload.get("top_p") or 1.0)
        started = time.perf_counter()
        with torch.inference_mode():
            generated = self.model.generate(**generation_kwargs)
        elapsed = time.perf_counter() - started
        if wall_clock_limit.expired:
            LOG.warning("Generation exceeded %.1fs wall-clock budget", self.max_generation_seconds)
            raise HTTPException(status_code=504, detail=f"generation exceeded {self.max_generation_seconds:.0f}s wall-clock budget")
        completion_ids = generated[0, encoded["input_ids"].shape[1] :]
        output_text = self.tokenizer.decode(completion_ids, skip_special_tokens=True)
        content, tool_calls = parse_tool_calls(output_text)
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        LOG.info(
            "Generated %d tokens in %.2fs; tool_calls=%d",
            completion_ids.shape[0],
            elapsed,
            len(tool_calls),
        )
        return {
            "id": f"chatcmpl-{uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.served_model_name,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            "usage": {
                "prompt_tokens": int(encoded["input_ids"].shape[1]),
                "completion_tokens": int(completion_ids.shape[0]),
                "total_tokens": int(encoded["input_ids"].shape[1] + completion_ids.shape[0]),
            },
        }


def make_app(server: ModelServer) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield

    app = FastAPI(title="CyberGym Transformers 8-bit OpenAI bridge", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "model": server.served_model_name}

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {"object": "list", "data": [{"id": server.served_model_name, "object": "model"}]}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="request body must be JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="request body must be an object")
        async with server.lock:
            return JSONResponse(server._generate(payload))

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--served-model-name", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--revision")
    parser.add_argument("--max-generation-seconds", type=float, default=600.0)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args()
    if args.max_generation_seconds <= 0:
        raise SystemExit("--max-generation-seconds must be positive")
    server = ModelServer(args.model, args.served_model_name, args.revision, args.max_generation_seconds)
    server.load()
    uvicorn.run(make_app(server), host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
