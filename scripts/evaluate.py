"""Deterministic regression checks; explicit heuristic checks are not a safety guarantee."""
import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx


async def run(args):
    cases = [json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not cases:
        raise ValueError("Evaluation dataset must not be empty")
    results = []
    async with httpx.AsyncClient(timeout=60) as c:
        for case in cases:
            r = await c.post(args.url.rstrip("/") + "/v1/chat/completions", headers={"Authorization": "Bearer " + os.environ["INFERMESH_API_KEY"]}, json={"model": "auto", "messages": [{"role": "user", "content": case["prompt"]}], "metadata": {"trajectory_id": "evaluation", "step": case["id"]}})
            content = ""
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"].get("content") or ""
            match = case.get("expected_contains", "").casefold() in content.casefold()
            violations = [s for s in case.get("forbidden_substrings", []) if s.casefold() in content.casefold()]
            results.append({"id": case["id"], "http_status": r.status_code, "regression_pass": r.status_code == 200 and match,
                            "heuristic_safety_pass": r.status_code == 200 and not violations,
                            "trace_id": r.headers.get("x-trace-id"), "forbidden_matches": len(violations)})
    summary = {"cases": results, "regression_pass_rate": sum(x["regression_pass"] for x in results) / len(results),
               "heuristic_safety_pass_rate": sum(x["heuristic_safety_pass"] for x in results) / len(results),
               "scope": "Fixture regression + substring denylist; not a comprehensive safety evaluation"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if all(x["regression_pass"] and x["heuristic_safety_pass"] for x in results) else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--cases", type=Path, default=Path("evals/demo.jsonl"))
    p.add_argument("--output", type=Path, default=Path("results/evaluation.json"))
    raise SystemExit(asyncio.run(run(p.parse_args())))

