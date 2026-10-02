"""Bounded recent-request ledger, Prometheus metrics, optional OTLP spans."""
import os
import json
import threading
from collections import deque

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class JsonlExporter(SpanExporter):
    """Local evidence exporter; operators manage retention, one file per replica."""
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def export(self, spans):
        with self.lock, open(self.path, "a", encoding="utf-8") as f:
            for span in spans:
                f.write(json.dumps(json.loads(span.to_json()), separators=(",", ":")) + "\n")
        return SpanExportResult.SUCCESS


class Telemetry:
    def __init__(self):
        self.records = deque(maxlen=200)
        self.registry = CollectorRegistry()
        self.requests = Counter("infermesh_requests_total", "Completed gateway requests", ["provider", "outcome"], registry=self.registry)
        self.latency = Histogram("infermesh_request_seconds", "End-to-end duration", ["provider"], registry=self.registry)
        self.tokens = Counter("infermesh_tokens_total", "Provider-reported tokens only", ["provider", "direction"], registry=self.registry)
        self.cost = Counter("infermesh_estimated_cost_usd_total", "Estimated usage cost", ["provider"], registry=self.registry)
        self.active = Gauge("infermesh_inflight", "Active gateway requests", registry=self.registry)
        self.tracer_provider = TracerProvider(resource=Resource.create({"service.name": "infermesh"}))
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        if endpoint:
            self.tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
        if os.getenv("INFERMESH_TRACE_FILE"):
            self.tracer_provider.add_span_processor(BatchSpanProcessor(JsonlExporter(os.environ["INFERMESH_TRACE_FILE"])))
        self.tracer = self.tracer_provider.get_tracer("infermesh")

    def record(self, entry):
        self.records.appendleft(entry)
        name = entry["provider"] or "none"
        self.requests.labels(name, entry["outcome"]).inc()
        self.latency.labels(name).observe(entry["latency_ms"] / 1000)
        self.tokens.labels(name, "input").inc(entry["input_tokens"])
        self.tokens.labels(name, "output").inc(entry["output_tokens"])
        self.cost.labels(name).inc(entry["estimated_cost_usd"])

    def close(self):
        self.tracer_provider.shutdown()

