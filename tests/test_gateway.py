import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from infermesh.app import create_app
from infermesh.config import Settings
from infermesh.routing import Router

KEY = "test-key-not-a-production-secret"
HEADERS = {"Authorization": "Bearer " + KEY}
BODY = {"model": "auto", "messages": [{"role": "user", "content": "ping"}]}


def settings(**overrides):
    raw = {"providers": [
        {"name": "cheap", "kind": "mock", "model": "mock-a", "quality": 1, "initial_latency_ms": 5, "mock_latency_ms": 1},
        {"name": "capable", "kind": "mock", "model": "mock-b", "quality": 3, "input_per_million": 2, "initial_latency_ms": 30, "mock_latency_ms": 1}],
        "pools": {"auto": ["cheap", "capable"]}, "failure_threshold": 2}
    raw.update(overrides)
    return Settings.model_validate(raw)


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("INFERMESH_API_KEY", KEY)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("INFERMESH_TRACE_FILE", raising=False)


def test_auth_and_public_health():
    with TestClient(create_app(settings())) as c:
        assert c.get("/healthz").status_code == 200
        for url in ("/admin/status", "/metrics", "/v1/models"):
            assert c.get(url).status_code == 401
        assert c.post("/v1/chat/completions", json=BODY).status_code == 401


def test_missing_key_fails_startup(monkeypatch):
    monkeypatch.delenv("INFERMESH_API_KEY")
    with pytest.raises(RuntimeError, match="INFERMESH_API_KEY"):
        with TestClient(create_app(settings())):
            pass


@pytest.mark.parametrize("policy", ["cost", "balanced", "latency"])
def test_policies_and_complexity(policy):
    with TestClient(create_app(settings())) as c:
        r = c.post("/v1/chat/completions", headers=HEADERS, json={**BODY, "routing": {"policy": policy}})
        assert r.status_code == 200
        assert r.headers["x-infermesh-provider"] == "cheap"
        r = c.post("/v1/chat/completions", headers=HEADERS, json={**BODY, "routing": {"complexity": 3}})
        assert r.headers["x-infermesh-provider"] == "capable"


def test_failover_and_open_circuit():
    cfg = settings()
    cfg.providers[0].mock_fail_every = 1
    with TestClient(create_app(cfg)) as c:
        for _ in range(3):
            r = c.post("/v1/chat/completions", headers=HEADERS, json={**BODY, "routing": {"policy": "cost"}})
            assert r.status_code == 200
            assert r.headers["x-infermesh-provider"] == "capable"
        state = c.get("/admin/status", headers=HEADERS).json()
        assert state["providers"][0]["circuit"] == "open"
        assert state["requests"][0]["attempts"] == ["capable"]
        assert state["inflight"] == 0


def test_half_open_allows_one_probe():
    router = Router(settings())
    for _ in range(2):
        assert router.acquire("cheap")
        router.release("cheap", False, 10)
    assert not router.acquire("cheap")
    router.states["cheap"].open_until = time.monotonic() - 1
    assert router.acquire("cheap")
    assert not router.acquire("cheap")
    router.release("cheap", True, 10)
    assert router.snapshot()[0]["circuit"] == "closed"


def test_stream_usage_and_cleanup():
    with TestClient(create_app(settings())) as c:
        r = c.post("/v1/chat/completions", headers=HEADERS, json={**BODY, "stream": True})
        assert r.status_code == 200 and 'data: [DONE]' in r.text
        state = c.get("/admin/status", headers=HEADERS).json()
        assert state["inflight"] == 0
        assert state["requests"][0]["usage_reported"]
        assert state["requests"][0]["outcome"] == "success"


def test_rate_limit():
    with TestClient(create_app(settings(requests_per_minute=1))) as c:
        assert c.post("/v1/chat/completions", headers=HEADERS, json=BODY).status_code == 200
        assert c.post("/v1/chat/completions", headers=HEADERS, json=BODY).status_code == 429


def test_validation_and_body_limit():
    with TestClient(create_app(settings())) as c:
        assert c.post("/v1/chat/completions", headers=HEADERS, json={**BODY, "model": "missing"}).status_code == 404
        assert c.post("/v1/chat/completions", headers=HEADERS, json={**BODY, "routing": {"complexity": 4}}).status_code == 422
        assert c.post("/v1/chat/completions", headers=HEADERS, content=b"x" * (1024 * 1024 + 1)).status_code == 413
        assert c.post("/v1/chat/completions", headers=HEADERS, json={**BODY, "tools": [{"type": "function"}]}).status_code == 503


def test_deadline_and_capacity_release():
    cfg = settings(timeout_seconds=0.01)
    cfg.providers[0].mock_latency_ms = 100
    with TestClient(create_app(cfg)) as c:
        assert c.post("/v1/chat/completions", headers=HEADERS, json=BODY).status_code == 502
        status = c.get("/admin/status", headers=HEADERS).json()
        assert status["inflight"] == 0
        assert all(p["inflight"] == 0 for p in status["providers"])


def test_trace_privacy_metrics_and_parent():
    app = create_app(settings())
    exporter = InMemorySpanExporter()
    with TestClient(app) as c:
        app.state.telemetry.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
        c.post("/v1/chat/completions", headers={**HEADERS, "traceparent": "00-12345678901234567890123456789012-1234567890123456-01"}, json=BODY)
        spans = exporter.get_finished_spans()
        assert {s.name for s in spans} == {"infermesh.chat", "provider.attempt"}
        assert all(format(s.context.trace_id, "032x") == "12345678901234567890123456789012" for s in spans)
        assert all("ping" not in str(dict(s.attributes)) for s in spans)
        state = c.get("/admin/status", headers=HEADERS).json()
        assert "messages" not in state["requests"][0]
        assert "infermesh_requests_total" in c.get("/metrics", headers=HEADERS).text


def real_settings():
    return Settings.model_validate({"providers": [{"name": "real", "model": "actual-model", "base_url": "https://example.invalid/v1", "supports_tools": True}], "pools": {"auto": ["real"]}})


def test_real_adapter_payload_and_tool_forwarding():
    def handler(req):
        body = json.loads(req.content)
        assert body["model"] == "actual-model"
        assert "routing" not in body and "metadata" not in body
        assert body["tools"][0]["function"]["name"] == "lookup"
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}}], "usage": {"prompt_tokens": 5, "completion_tokens": 4}})
    with TestClient(create_app(real_settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))) as c:
        r = c.post("/v1/chat/completions", headers=HEADERS, json={**BODY, "tools": [{"type": "function", "function": {"name": "lookup"}}]})
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["tool_calls"][0]["id"] == "t1"
        assert c.get("/admin/status", headers=HEADERS).json()["requests"][0]["tool_calls"] == 1


@pytest.mark.parametrize("status", [401, 429, 500])
def test_upstream_errors_never_leak_provider_body(status):
    transport = httpx.MockTransport(lambda req: httpx.Response(status, text="secret-provider-key"))
    with TestClient(create_app(real_settings(), client=httpx.AsyncClient(transport=transport))) as c:
        r = c.post("/v1/chat/completions", headers=HEADERS, json=BODY)
        assert r.status_code == 502 and "secret-provider-key" not in r.text


def test_broken_stream_no_replay_or_false_done():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n', headers={"Content-Type": "text/event-stream"}))
    with TestClient(create_app(real_settings(), client=httpx.AsyncClient(transport=transport))) as c:
        r = c.post("/v1/chat/completions", headers=HEADERS, json={**BODY, "stream": True})
        assert "partial" in r.text and "upstream_stream_error" in r.text
        assert "[DONE]" not in r.text
        state = c.get("/admin/status", headers=HEADERS).json()
        assert state["inflight"] == 0 and state["requests"][0]["outcome"] == "error"


def test_real_sse_usage_and_done():
    content = b'data: {"choices":[{"delta":{"content":"hello"}}]}\r\n\r\ndata: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\ndata: [DONE]\n\n'
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=content))
    with TestClient(create_app(real_settings(), client=httpx.AsyncClient(transport=transport))) as c:
        r = c.post("/v1/chat/completions", headers=HEADERS, json={**BODY, "stream": True})
        assert "[DONE]" in r.text
        assert c.get("/admin/status", headers=HEADERS).json()["requests"][0]["input_tokens"] == 2


def test_invalid_provider_response_releases_capacity():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"choices": [None]}))
    with TestClient(create_app(real_settings(), client=httpx.AsyncClient(transport=transport))) as c:
        assert c.post("/v1/chat/completions", headers=HEADERS, json=BODY).status_code == 502
        assert c.get("/admin/status", headers=HEADERS).json()["inflight"] == 0


def test_concurrent_capacity_is_bounded():
    async def scenario():
        cfg = settings(max_inflight=1)
        cfg.providers[0].mock_latency_ms = 50
        app = create_app(cfg)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
                result = await asyncio.gather(*[c.post("/v1/chat/completions", headers=HEADERS, json=BODY) for _ in range(4)])
                assert sorted(r.status_code for r in result) == [200, 429, 429, 429]
                assert app.state.inflight == 0
    asyncio.run(scenario())


def test_duplicate_provider_configuration_rejected():
    cfg = settings().model_dump()
    cfg["providers"].append(cfg["providers"][0])
    with pytest.raises(ValueError, match="unique"):
        Settings.model_validate(cfg)
