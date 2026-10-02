# Architecture and operational boundaries

## Request path

1. Authenticate the gateway key and cap the request at 1 MiB.
2. Validate pool, explicit complexity tier, and requested capabilities.
3. Apply replica-local token-bucket and in-flight admission limits.
4. Sort eligible providers by the chosen deterministic policy.
5. Reserve provider capacity without an intervening await; half-open circuits allow one probe.
6. Attempt providers sequentially within one wall-clock inference deadline.
7. Return JSON or transfer ownership of cleanup to the SSE iterator.
8. Release capacity, record outcome, and close request/attempt spans.

Provider 408/429/5xx and transport failures can fall back. Other upstream HTTP failures
stop attempts. No fallback occurs after stream bytes begin; clients receive an SSE error.
Successful HTTP headers do not imply that a stream completed: the ledger distinguishes
success, error, and client cancellation.

## Routing score

Eligibility requires `provider.quality >= routing.complexity`, requested tool/stream
support, circuit availability, and spare capacity. Cost is the sum of configured input
and output prices per million tokens, a simple proxy rather than a predicted invoice.

- Cost: price, then EWMA error rate and latency.
- Latency: EWMA latency multiplied by `(1 + 5 * error_rate)`, then price.
- Balanced: price + latency in seconds + `10 * error_rate`.

The quality tier is declared by the operator and complexity by the caller. This makes
decisions inspectable, but does not infer task difficulty or guarantee response quality.

## Observability and privacy

The OpenTelemetry SDK creates `infermesh.chat` and `provider.attempt` spans. OTLP/HTTP
exports are optional. JSONL export is useful for local evidence; each process must use
its own path and an operator-managed retention policy. Prometheus uses bounded
provider/outcome labels and never user prompts or request IDs as metric labels.

Input/output bodies are opt-in and truncated. Exception messages and provider error
bodies are not sent to clients or added to request logs. The bearer key protects
inference, telemetry, and the admin ledger; configure TLS and ingress controls before
remote access. Avoid raw content capture for sensitive prompts.

## Deployment boundaries

One event loop owns routing state. Multiple workers/pods have independent counters,
circuits, and history. Use OTLP aggregation for fleet traces. Current routing latency is
total completion time, not time-to-first-token. CPU-based HPA is supplied as an example;
LLM workloads often need concurrency or queue-length scaling in a real deployment.

The gateway forwards tool calls and caller-provided trajectory IDs. It is not an agent
runtime, tool sandbox, inference engine, policy classifier, or GPU scheduler.

## Reference documentation

- [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)
- [OpenTelemetry Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/)
- [OpenTelemetry exporters](https://opentelemetry.io/docs/languages/python/exporters/)
- [Kubernetes HPA](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/)
- [Kubernetes probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-probes/)
