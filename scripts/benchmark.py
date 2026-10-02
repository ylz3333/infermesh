"""Bounded-concurrency HTTP load test. Measures this run, never extrapolates uptime."""
import argparse
import asyncio
import json
import math
import os
import platform
import time
from collections import Counter
from pathlib import Path

import httpx


async def run(args):
    key = os.environ["INFERMESH_API_KEY"]
    counts = Counter()
    latencies = []
    providers = Counter()
    next_id = 0
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=60, limits=httpx.Limits(max_connections=args.concurrency)) as client:
        async def worker():
            nonlocal next_id
            while next_id < args.requests:
                index = next_id
                next_id += 1
                t = time.perf_counter()
                try:
                    r = await client.post(args.url.rstrip("/") + "/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={"model": "auto", "messages": [{"role": "user", "content": "ping"}], "routing": {"policy": "cost"}, "metadata": {"trajectory_id": "benchmark", "step": str(index)}})
                    counts[str(r.status_code)] += 1
                    if r.status_code == 200:
                        providers[r.headers.get("x-infermesh-provider", "unknown")] += 1
                except httpx.HTTPError:
                    counts["transport_error"] += 1
                latencies.append((time.perf_counter() - t) * 1000)
        await asyncio.gather(*[worker() for _ in range(args.concurrency)])
    elapsed = time.perf_counter() - started
    ordered = sorted(latencies)
    def percentile(p):
        return round(ordered[max(0, math.ceil(len(ordered) * p) - 1)], 3)
    summary = {"scenario": args.label, "provider_type": "mock unless real endpoint configuration is supplied",
               "requests": args.requests, "concurrency": args.concurrency, "duration_seconds": round(elapsed, 3),
               "rps": round(args.requests / elapsed, 3), "success_fraction": counts["200"] / args.requests,
               "status_codes": dict(counts), "provider_counts": dict(providers),
               "latency_ms": {"p50": percentile(.5), "p95": percentile(.95), "p99": percentile(.99)},
               "python": platform.python_version(), "platform": platform.platform(),
               "note": "Closed-loop load; success fraction is not a production availability SLA. Mock token counts and pricing are synthetic."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if summary["success_fraction"] >= args.min_success else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--requests", type=int, default=1000)
    p.add_argument("--concurrency", type=int, default=16)
    p.add_argument("--label", default="local-mock")
    p.add_argument("--min-success", type=float, default=.995)
    p.add_argument("--output", type=Path, default=Path("results/benchmark.json"))
    args = p.parse_args()
    if args.requests < 1 or not 1 <= args.concurrency <= 1000:
        p.error("requests must be positive; concurrency must be 1..1000")
    raise SystemExit(asyncio.run(run(args)))

