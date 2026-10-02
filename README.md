<div align="center">

# InferMesh

**One gateway. Explicit routing. Observable inference.**

An LLM gateway and observability platform by [Zhezhao Yu](https://github.com/ylz3333).

[Architecture](docs/ARCHITECTURE.md) · [Deployment](docs/DEPLOYMENT.md) · [Evidence & limitations](docs/RESULTS.md)

</div>

InferMesh puts a stable chat-completions API in front of hosted and self-hosted
OpenAI-compatible model servers. It chooses a provider using cost, observed latency,
availability, and a caller-declared task-complexity level, then records the route and outcome.

This is a runnable portfolio implementation with an offline mock mode. Real-model
performance and Kubernetes availability have **not** been measured in this repository.

## What is implemented

- **Policy routing:** cost, latency, and balanced policies; named model pools; explicit
  quality tiers; stream/tool capability filtering; latency and error-rate EWMAs.
- **Failure isolation:** bounded request deadlines, fallback before streaming starts,
  circuit breaking with a single recovery probe, provider concurrency caps, and
  replica-local admission/rate limits.
- **Compatible inference:** JSON and SSE chat completions, tool-call forwarding,
  provider-reported usage, cancellation cleanup, and sanitized upstream errors.
- **Observability:** OpenTelemetry request and provider-attempt spans, W3C trace
  propagation, optional OTLP/JSONL export, Prometheus metrics, and a live control panel.
- **Evaluation:** trace-linked regression fixtures, explicit substring safety checks,
  a configurable load generator, and deterministic provider fault injection.
- **Deployment:** non-root Docker image, local Compose + Jaeger stack, Kubernetes
  probes, rolling updates, HPA, resource limits, and a disruption budget.

## Run locally in two minutes

Requires Python 3.11+. No paid API key is needed for the demo.

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
python -m pip install -e .
```

Set a gateway key, then start the app:

```bash
export INFERMESH_API_KEY=local-demo-change-this-key
# PowerShell: $env:INFERMESH_API_KEY = "local-demo-change-this-key"
uvicorn infermesh.app:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000** and enter the same key. The API explorer is at `/docs`.
The dashboard labels mock providers and reports statistics for the connected replica.

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer local-demo-change-this-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"ping"}],"routing":{"policy":"cost","complexity":1}}'
```

Add `"stream":true` for SSE. Every successful response carries `X-Request-ID`,
`X-Trace-ID`, and `X-InferMesh-Provider`. An interrupted stream contains an error
event rather than a false `[DONE]`; the gateway never silently replays emitted tokens.

## Connect real models

Copy `config/providers.example.json` to the git-ignored `config.local.json`.
Replace its placeholder URLs and model IDs with your actual endpoints. Set
`INFERMESH_CONFIG=config.local.json` and provider credentials using the environment
variable names referenced in the config. Restart the gateway to apply changes.

An endpoint must implement `/v1/chat/completions`; for example, a configured vLLM
server or a hosted compatible API. Native provider-specific APIs need another adapter.
Model quality tiers and pricing are operator-supplied, not automatically benchmarked.
The request's `model` names a **pool**, not a provider's underlying model ID.

## Traces, tokens, tools, and trajectories

Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` to export spans. For local
evidence, set `INFERMESH_TRACE_FILE` to a writable JSONL path whose parent exists.
Telemetry includes provider attempts, routing policy, duration, failures, token counts,
and tool-call counts. Tools are forwarded; this service does not execute them.

Attach `metadata.trajectory_id`, `metadata.session_id`, and `metadata.step` to connect
calls from an agent workflow. Caller-supplied `traceparent` connects upstream traces.
Prompts, completions, and tool arguments are **not captured by default**. Set
`capture_content:true` in configuration only for data you are allowed to retain;
captured input/output content is truncated to 8 KiB per request direction.
Missing usage is marked `usage_reported:false`, not estimated as verified token usage.

`/metrics` and `/admin/status` require the gateway bearer key. The recent ledger holds
200 requests in memory; use an external telemetry backend for durable history.

## Reproduce checks and load scenarios

```bash
python -m pip install -e '.[test]'
pytest -q
python scripts/evaluate.py --output results/evaluation.json
python scripts/benchmark.py --requests 10000 --concurrency 32
```

For fault injection, restart with `INFERMESH_CONFIG=config/fault-demo.json`. Its
primary mock fails every second call; the fallback remains healthy. Benchmark output
records actual HTTP outcomes, provider counts, latency percentiles, and achieved RPS.
It exits unsuccessfully if the observed success fraction is below the configured gate.
These are closed-loop **mock gateway** measurements, not LLM throughput or a service SLA.

**Measured so far:** 21 automated tests passed on Windows/Python 3.14.7. Four offline
evaluation fixtures passed. The 10,000-request benchmark was stopped before a final
result at the owner's request; no RPS or availability number is claimed.
See [the evidence notes](docs/RESULTS.md) and the checked-in evaluation JSON.

## Architecture

```mermaid
flowchart LR
  Client --> Auth[Bearer auth / bounded request]
  Auth --> Policy[Pool + policy + capability gate]
  Policy --> Admission[Concurrency / circuit breaker]
  Admission --> Hosted[Hosted compatible API]
  Admission --> Local[Self-hosted compatible API]
  Admission --> Mock[Offline mock fixtures]
  Hosted --> Gateway[JSON / SSE response]
  Local --> Gateway
  Mock --> Gateway
  Gateway --> Client
  Policy -.-> OTel[OpenTelemetry spans]
  Gateway -.-> OTel
  OTel --> Backend[OTLP backend / JSONL]
  Gateway -.-> Metrics[Prometheus / control panel]
```

## Scope and tradeoffs

Routing state, rate limits, and the recent-request ledger are **per replica**. The
Kubernetes manifests demonstrate horizontal deployment, not distributed global quotas
or shared circuit state. There is one gateway API key and no tenant billing system.
Readiness checks local admission availability; it does not send paid model requests.
Retries can incur duplicate provider charges. A failed provider is attempted at most
once per request, and there is one end-to-end inference deadline.

The demo evaluation measures fixture regression and simple denylist matches. It is
not a comprehensive model safety benchmark. Kubernetes/GPU model serving, production
SLO validation, load-balanced dashboards, and global Redis quotas remain future work.

## License

MIT. See [LICENSE](LICENSE).
