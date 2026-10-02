"""Replica-local adaptive scoring and circuit breaking; no hidden model judge."""
import time
from dataclasses import dataclass

from .config import Provider, Settings


@dataclass
class State:
    latency_ms: float
    error_rate: float = 0
    failures: int = 0
    open_until: float = 0
    inflight: int = 0


class Router:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.providers = {p.name: p for p in settings.providers}
        self.states = {p.name: State(p.initial_latency_ms) for p in settings.providers}

    def candidates(self, pool: str, policy: str, complexity: int, tools: bool, stream: bool):
        now = time.monotonic()
        eligible = [self.providers[n] for n in self.settings.pools.get(pool, [])
                    if self.providers[n].quality >= complexity
                    and (not tools or self.providers[n].supports_tools)
                    and (not stream or self.providers[n].supports_stream)
                    and self.available(n, now)]
        def score(p):
            state = self.states[p.name]
            # Pricing is a normalized estimate, not an invoice or token prediction.
            cost = p.input_per_million + p.output_per_million
            if policy == "cost":
                return (cost, state.error_rate, state.latency_ms)
            if policy == "latency":
                return (state.latency_ms * (1 + state.error_rate * 5), cost)
            return (cost + state.latency_ms / 1000 + state.error_rate * 10, p.name)
        return sorted(eligible, key=score)

    def available(self, name, now=None):
        s = self.states[name]
        return (s.open_until <= (now if now is not None else time.monotonic())
                and s.inflight < self.providers[name].concurrency
                and (s.failures < self.settings.failure_threshold or s.inflight == 0))

    def acquire(self, name):
        # No await between availability check and reservation on one event loop.
        if not self.available(name):
            return False
        self.states[name].inflight += 1
        return True

    def release(self, name, ok: bool | None, elapsed_ms: float):
        s = self.states[name]
        s.inflight = max(0, s.inflight - 1)
        if ok is None:  # client cancellation isn't provider unavailability
            return
        s.latency_ms = 0.8 * s.latency_ms + 0.2 * elapsed_ms
        s.error_rate = 0.8 * s.error_rate + 0.2 * (not ok)
        if ok:
            s.failures = 0
            s.open_until = 0
        else:
            s.failures += 1
            if s.failures >= self.settings.failure_threshold:
                s.open_until = time.monotonic() + self.settings.cooldown_seconds

    def snapshot(self):
        now = time.monotonic()
        return [{"name": p.name, "model": p.model, "kind": p.kind,
                 "circuit": "open" if s.open_until > now else "half-open" if s.failures >= self.settings.failure_threshold else "closed",
                 "inflight": s.inflight, "latency_ms": round(s.latency_ms, 2),
                 "error_rate": round(s.error_rate, 4), "quality": p.quality}
                for p in self.providers.values() for s in [self.states[p.name]]]

