"""HTTP control plane and OpenAI-compatible chat-completions gateway."""
import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.background import BackgroundTask

from .config import Settings
from .providers import Adapters, UpstreamError, events, read_json
from .routing import Router
from .telemetry import Telemetry

log = logging.getLogger("infermesh")


class Routing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: Literal["balanced", "cost", "latency"] = "balanced"
    complexity: int = Field(default=1, ge=1, le=3)


class Chat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = "auto"
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=256)
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    max_tokens: int | None = Field(default=None, ge=1, le=131072)
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    routing: Routing = Field(default_factory=Routing)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=16)


def create_app(settings: Settings | None = None, *, client: httpx.AsyncClient | None = None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = settings or Settings.load()
        app.state.key = os.getenv("INFERMESH_API_KEY", "")
        if len(app.state.key) < 16:
            raise RuntimeError("Set INFERMESH_API_KEY to a value of at least 16 characters")
        app.state.router = Router(app.state.settings)
        app.state.telemetry = Telemetry()
        app.state.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5), limits=httpx.Limits(max_connections=200, max_keepalive_connections=40), follow_redirects=False)
        app.state.adapters = Adapters(app.state.client)
        app.state.inflight = 0
        app.state.bucket = float(app.state.settings.requests_per_minute)
        app.state.bucket_at = time.monotonic()
        yield
        if client is None:
            await app.state.client.aclose()
        app.state.telemetry.close()

    app = FastAPI(title="InferMesh", version="0.1.0", lifespan=lifespan,
                  description="Policy-driven LLM routing with bounded failover and traceable outcomes.")

    def authorize(authorization: str = Header(default="")):
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        if not hmac.compare_digest(token.encode(), app.state.key.encode()):
            raise HTTPException(401, "Invalid gateway API key", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready():
        # Readiness is admission availability; not an active paid inference probe.
        router = app.state.router
        ok = any(router.available(n) for n in router.providers)
        return JSONResponse({"ready": ok}, status_code=200 if ok else 503)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        return HTMLResponse(Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8"), headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
            "Cache-Control": "no-store"})

    @app.get("/v1/models", dependencies=[Depends(authorize)])
    async def models():
        return {"object": "list", "data": [{"id": n, "object": "model", "owned_by": "infermesh"} for n in app.state.settings.pools]}

    @app.get("/admin/status", dependencies=[Depends(authorize)])
    async def status():
        return {"providers": app.state.router.snapshot(), "inflight": app.state.inflight,
                "capture_content": app.state.settings.capture_content,
                "scope": "This replica; last 200 completed requests", "requests": list(app.state.telemetry.records)}

    @app.get("/metrics", dependencies=[Depends(authorize)])
    async def metrics():
        return Response(generate_latest(app.state.telemetry.registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

    @app.post("/v1/chat/completions", dependencies=[Depends(authorize)])
    async def chat(request: Request):
        cfg = app.state.settings
        # Limit bytes before parsing and never echo request bodies in validation errors.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                raise HTTPException(413, "Request exceeds 1 MiB")
        try:
            data = Chat.model_validate_json(body)
        except ValidationError:
            raise HTTPException(422, "Invalid chat request; see /docs for schema") from None
        if any(m.get("role") not in {"system", "developer", "user", "assistant", "tool"} for m in data.messages):
            raise HTTPException(422, "Unsupported message role")
        if data.model not in cfg.pools:
            raise HTTPException(404, "Unknown model pool")
        now = time.monotonic()
        app.state.bucket = min(cfg.requests_per_minute, app.state.bucket + (now - app.state.bucket_at) * cfg.requests_per_minute / 60)
        app.state.bucket_at = now
        if app.state.bucket < 1 or app.state.inflight >= cfg.max_inflight:
            raise HTTPException(429, "Gateway capacity exceeded", headers={"Retry-After": "1"})
        app.state.bucket -= 1
        router = app.state.router
        candidates = router.candidates(data.model, data.routing.policy, data.routing.complexity, bool(data.tools), data.stream)
        if not candidates:
            raise HTTPException(503, "No eligible provider currently available", headers={"Retry-After": "1"})

        app.state.inflight += 1
        telemetry = app.state.telemetry
        telemetry.active.inc()
        rid = uuid.uuid4().hex
        parent = TraceContextTextMapPropagator().extract(dict(request.headers))
        span = telemetry.tracer.start_span("infermesh.chat", context=parent, attributes={"request.id": rid, "routing.policy": data.routing.policy, "routing.complexity": data.routing.complexity, "gen_ai.request.model": data.model})
        span_context = trace.set_span_in_context(span)
        trace_id = format(span.get_span_context().trace_id, "032x")
        payload = data.model_dump(exclude_none=True)
        deadline = now + cfg.timeout_seconds
        entry = {"request_id": rid, "trace_id": trace_id, "time": time.time(), "provider": None,
                 "outcome": "error", "attempts": [], "input_tokens": 0, "output_tokens": 0,
                 "usage_reported": False, "estimated_cost_usd": 0, "tool_calls": 0,
                 "latency_ms": 0, "policy": data.routing.policy}
        if cfg.capture_content:
            entry["messages"] = json.dumps(data.messages, ensure_ascii=False)[:8192]
            span.set_attribute("gen_ai.input.messages", entry["messages"])
        for field in ("session_id", "trajectory_id", "step"):
            if field in data.metadata:
                span.set_attribute("agent." + field, data.metadata[field][:128])
        finalized = False

        def finish():
            nonlocal finalized
            if finalized:
                return
            finalized = True
            entry["latency_ms"] = round((time.monotonic() - now) * 1000, 3)
            telemetry.record(entry)
            app.state.inflight -= 1
            telemetry.active.dec()
            span.set_attribute("request.outcome", entry["outcome"])
            if entry["outcome"] == "error":
                span.set_status(Status(StatusCode.ERROR, "inference_failed"))
            span.end()
            log.info(json.dumps({k: v for k, v in entry.items() if k not in {"messages", "completion"}}))

        def usage(result, provider):
            u = result.get("usage")
            if not isinstance(u, dict):
                return
            def count(key):
                v = u.get(key, 0)
                return v if type(v) is int and v >= 0 else 0
            entry["usage_reported"] = True
            entry["input_tokens"] = count("prompt_tokens")
            entry["output_tokens"] = count("completion_tokens")
            entry["estimated_cost_usd"] = (entry["input_tokens"] * provider.input_per_million + entry["output_tokens"] * provider.output_per_million) / 1_000_000
            span.set_attribute("gen_ai.usage.input_tokens", entry["input_tokens"])
            span.set_attribute("gen_ai.usage.output_tokens", entry["output_tokens"])

        for provider in candidates:
            if not router.acquire(provider.name):
                continue
            started = time.monotonic()
            response = None
            attempt = telemetry.tracer.start_span("provider.attempt", context=span_context, attributes={"provider": provider.name, "gen_ai.request.model": provider.model})
            entry["attempts"].append(provider.name)
            try:
                async with asyncio.timeout_at(deadline):
                    response = await app.state.adapters.open(provider, payload)
                    if not data.stream:
                        result = await read_json(response)
                entry["provider"] = provider.name
                span.set_attribute("gen_ai.provider.name", provider.name)
                headers = {"X-Request-ID": rid, "X-InferMesh-Provider": provider.name, "X-Trace-ID": trace_id}
                if not data.stream:
                    usage(result, provider)
                    entry["tool_calls"] = sum(len(c.get("message", {}).get("tool_calls", [])) for c in result["choices"])
                    span.set_attribute("gen_ai.tool_calls.count", entry["tool_calls"])
                    if cfg.capture_content:
                        entry["completion"] = json.dumps(result["choices"], ensure_ascii=False)[:8192]
                        span.set_attribute("gen_ai.output.messages", entry["completion"])
                    entry["outcome"] = "success"
                    router.release(provider.name, True, (time.monotonic() - started) * 1000)
                    attempt.end()
                    finish()
                    return JSONResponse(result, headers=headers)

                async def stream_body():
                    ok = None
                    tool_ids = set()
                    try:
                        async with asyncio.timeout_at(deadline):
                            async for event in events(response):
                                usage(event, provider)
                                for choice in event.get("choices", []):
                                    delta = choice.get("delta", {})
                                    for tool in delta.get("tool_calls", []):
                                        tool_ids.add((choice.get("index", 0), tool.get("index", 0)))
                                    if cfg.capture_content:
                                        entry["completion"] = (entry.get("completion", "") + json.dumps(delta, ensure_ascii=False))[:8192]
                                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                        ok = True
                        entry["outcome"] = "success"
                        yield "data: [DONE]\n\n"
                    except asyncio.CancelledError:
                        entry["outcome"] = "cancelled"
                        raise
                    except (UpstreamError, httpx.HTTPError, TimeoutError, ValueError):
                        ok = False
                        attempt.set_status(Status(StatusCode.ERROR, "stream_failed"))
                        yield 'data: {"error":{"type":"upstream_stream_error","message":"Stream interrupted; no automatic replay"}}\n\n'
                    finally:
                        if isinstance(response, httpx.Response):
                            await response.aclose()
                        entry["tool_calls"] = len(tool_ids)
                        span.set_attribute("gen_ai.tool_calls.count", len(tool_ids))
                        if cfg.capture_content:
                            span.set_attribute("gen_ai.output.messages", entry.get("completion", ""))
                        router.release(provider.name, ok, (time.monotonic() - started) * 1000)
                        attempt.end()
                        finish()
                return StreamingResponse(stream_body(), media_type="text/event-stream", headers={**headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}, background=BackgroundTask(finish))
            except asyncio.CancelledError:
                if isinstance(response, httpx.Response):
                    await response.aclose()
                router.release(provider.name, None, (time.monotonic() - started) * 1000)
                attempt.end()
                entry["outcome"] = "cancelled"
                finish()
                raise
            except (UpstreamError, httpx.HTTPError, TimeoutError) as exc:
                if isinstance(response, httpx.Response):
                    await response.aclose()
                router.release(provider.name, False, (time.monotonic() - started) * 1000)
                attempt.set_status(Status(StatusCode.ERROR, getattr(exc, "code", "timeout_or_transport")))
                attempt.end()
                if time.monotonic() >= deadline or (isinstance(exc, UpstreamError) and not exc.retryable):
                    break
        finish()
        return JSONResponse({"error": {"type": "upstream_unavailable", "message": "No provider completed this request", "request_id": rid}}, status_code=502, headers={"X-Request-ID": rid})

    return app


app = create_app()
