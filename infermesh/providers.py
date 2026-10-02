"""Adapters for real OpenAI-compatible endpoints and explicit offline fixtures."""
import asyncio
import json
import os
import time
import uuid
from collections import defaultdict

import httpx


class UpstreamError(Exception):
    def __init__(self, code="upstream_error", retryable=True):
        self.code = code
        self.retryable = retryable


class Adapters:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.calls = defaultdict(int)

    async def open(self, provider, payload):
        if provider.kind == "mock":
            self.calls[provider.name] += 1
            await asyncio.sleep(provider.mock_latency_ms / 1000)
            if provider.mock_fail_every and self.calls[provider.name] % provider.mock_fail_every == 0:
                raise UpstreamError("mock_failure")
            message = str(payload["messages"][-1].get("content") or "")
            answer = "pong" if "ping" in message.lower() else "InferMesh demo response."
            return {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion", "created": int(time.time()),
                    "model": provider.model, "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": max(1, len(message) // 4), "completion_tokens": len(answer.split()), "total_tokens": max(1, len(message) // 4) + len(answer.split())}}
        headers = {"Content-Type": "application/json"}
        if provider.api_key_env:
            headers["Authorization"] = "Bearer " + os.environ[provider.api_key_env]
        outgoing = {k: v for k, v in payload.items() if k not in {"routing", "metadata"}}
        outgoing["model"] = provider.model
        if outgoing.get("stream"):
            outgoing["stream_options"] = {"include_usage": True}
        req = self.client.build_request("POST", provider.base_url.rstrip("/") + "/chat/completions", json=outgoing, headers=headers)
        try:
            response = await self.client.send(req, stream=True)
        except httpx.HTTPError:
            raise UpstreamError("connection_error") from None
        if response.status_code >= 400:
            retryable = response.status_code in {408, 429} or response.status_code >= 500
            await response.aclose()
            raise UpstreamError("upstream_http_" + str(response.status_code), retryable)
        return response


async def read_json(response):
    if isinstance(response, dict):
        return response
    data = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > 4 * 1024 * 1024:
                raise UpstreamError("response_too_large", False)
        result = json.loads(data)
        if not isinstance(result, dict) or not isinstance(result.get("choices"), list) or not result["choices"]:
            raise UpstreamError("invalid_response")
        if any(not isinstance(c, dict) or not isinstance(c.get("message"), dict)
               or not isinstance(c["message"].get("tool_calls", []), list) for c in result["choices"]):
            raise UpstreamError("invalid_response")
        return result
    except (ValueError, httpx.HTTPError):
        raise UpstreamError("invalid_response") from None
    finally:
        await response.aclose()


async def events(response):
    """Normalize SSE frames, bounded independently of total stream length."""
    if isinstance(response, dict):
        base = {k: response[k] for k in ("id", "created", "model")}
        base["object"] = "chat.completion.chunk"
        yield {**base, "choices": [{"index": 0, "delta": response["choices"][0]["message"], "finish_reason": None}]}
        yield {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": response["usage"]}
        return
    pending = b""
    done = False
    try:
        async for chunk in response.aiter_bytes():
            pending += chunk
            if len(pending) > 1024 * 1024:
                raise UpstreamError("stream_frame_too_large", False)
            pending = pending.replace(b"\r\n", b"\n")
            while b"\n\n" in pending:
                frame, pending = pending.split(b"\n\n", 1)
                data = b"\n".join(line[5:].lstrip() for line in frame.split(b"\n") if line.startswith(b"data:"))
                if not data:
                    continue
                if data == b"[DONE]":
                    done = True
                    return
                try:
                    obj = json.loads(data)
                    if not isinstance(obj, dict) or "error" in obj or not isinstance(obj.get("choices", []), list):
                        raise ValueError()
                    for choice in obj.get("choices", []):
                        if not isinstance(choice, dict) or not isinstance(choice.get("delta", {}), dict):
                            raise ValueError()
                        tools = choice.get("delta", {}).get("tool_calls", [])
                        if not isinstance(tools, list) or any(not isinstance(t, dict) for t in tools):
                            raise ValueError()
                except ValueError:
                    raise UpstreamError("invalid_stream", False) from None
                yield obj
        if not done:
            raise UpstreamError("incomplete_stream", False)
    finally:
        await response.aclose()

