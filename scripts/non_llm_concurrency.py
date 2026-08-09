"""Safe HTTP concurrency benchmark that never calls the chat/LLM endpoint.

The benchmark only exercises:
  * GET /api/metrics?format=prometheus (in-memory metrics rendering)
  * GET /api/ready (PostgreSQL + pgvector + Redis readiness probe)

It deliberately does not call /api/chat, /api/health, or any write endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx


DEFAULT_BASE = "http://127.0.0.1:7860"
DEFAULT_OUTPUT = "non_llm_concurrency_results_20260803.json"


@dataclass
class RequestResult:
    status: int | None
    elapsed_ms: float
    error: str | None = None


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    rank = (len(values) - 1) * p
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return round(values[low], 3)
    return round(values[low] + (values[high] - values[low]) * (rank - low), 3)


def summarize(results: list[RequestResult], wall_seconds: float, sent: int | None = None) -> dict[str, Any]:
    durations = [r.elapsed_ms for r in results]
    statuses = Counter(str(r.status) if r.status is not None else "transport_error" for r in results)
    errors = [r.error for r in results if r.error]
    successful = sum(1 for r in results if r.status is not None and 200 <= r.status < 300)
    return {
        "sent": sent if sent is not None else len(results),
        "completed": len(results),
        "successful_2xx": successful,
        "error_rate": round((len(results) - successful) / len(results), 6) if results else 0.0,
        "wall_seconds": round(wall_seconds, 3),
        "observed_rps": round(len(results) / wall_seconds, 3) if wall_seconds > 0 else 0.0,
        "statuses": dict(statuses),
        "latency_ms": {
            "min": round(min(durations), 3) if durations else None,
            "avg": round(statistics.fmean(durations), 3) if durations else None,
            "p50": percentile(durations, 0.50),
            "p95": percentile(durations, 0.95),
            "p99": percentile(durations, 0.99),
            "max": round(max(durations), 3) if durations else None,
        },
        "sample_errors": errors[:10],
    }


async def one(client: httpx.AsyncClient, path: str) -> RequestResult:
    started = time.perf_counter()
    try:
        response = await client.get(path)
        return RequestResult(
            status=response.status_code,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception as exc:  # transport/timeouts are part of the result
        return RequestResult(
            status=None,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )


async def warmup(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    results = await asyncio.gather(*(one(client, path) for _ in range(10)))
    return summarize(results, 0.0)


async def burst(client: httpx.AsyncClient, path: str, concurrency: int, rounds: int) -> dict[str, Any]:
    all_results: list[RequestResult] = []
    started = time.perf_counter()
    for _ in range(rounds):
        all_results.extend(await asyncio.gather(*(one(client, path) for _ in range(concurrency))))
        await asyncio.sleep(0.25)
    return summarize(all_results, time.perf_counter() - started, sent=len(all_results))


async def steady_rate(
    client: httpx.AsyncClient,
    path: str,
    target_rps: float,
    duration_seconds: float,
    max_in_flight: int,
) -> dict[str, Any]:
    """Generate a fixed arrival rate without letting in-flight work grow forever."""
    results: list[RequestResult] = []
    tasks: set[asyncio.Task[RequestResult]] = set()
    started = time.perf_counter()
    sent = 0
    deadline = started + duration_seconds

    while True:
        scheduled_at = started + (sent / target_rps)
        if scheduled_at >= deadline:
            break
        await asyncio.sleep(max(0.0, scheduled_at - time.perf_counter()))

        task = asyncio.create_task(one(client, path))
        tasks.add(task)
        sent += 1

        if len(tasks) >= max_in_flight:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            tasks = pending
            results.extend(task.result() for task in done)

    if tasks:
        results.extend(await asyncio.gather(*tasks))
    return summarize(results, time.perf_counter() - started, sent=sent)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    endpoints = {
        "metrics": "/api/metrics?format=prometheus",
        "ready": "/api/ready",
    }
    selected = [args.endpoint] if args.endpoint != "both" else list(endpoints)
    results: dict[str, Any] = {
        "base_url": args.base_url,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "llm_called": False,
        "write_endpoints_called": False,
        "method": "GET only; /api/chat, /api/health and all write endpoints excluded",
        "tests": {},
    }

    limits = httpx.Limits(
        max_connections=args.max_connections,
        max_keepalive_connections=min(args.max_connections, 200),
    )
    timeout = httpx.Timeout(args.timeout, connect=args.timeout)
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        timeout=timeout,
        limits=limits,
        trust_env=False,
        headers={"Accept": "text/plain,application/json"},
    ) as client:
        for name in selected:
            path = endpoints[name]
            warm = await warmup(client, path)
            endpoint_results: dict[str, Any] = {"path": path, "warmup": warm}

            if args.bursts:
                burst_results = []
                for concurrency in args.bursts:
                    report = await burst(client, path, concurrency, args.rounds)
                    burst_results.append({"concurrency": concurrency, **report})
                    # Stop ramping if the endpoint is clearly unhealthy.
                    if report["error_rate"] > 0.05:
                        break
                endpoint_results["burst_tests"] = burst_results

            if args.target_rps > 0:
                endpoint_results["steady_test"] = {
                    "target_rps": args.target_rps,
                    "duration_seconds": args.duration,
                    "max_in_flight": args.max_in_flight,
                    **await steady_rate(
                        client,
                        path,
                        args.target_rps,
                        args.duration,
                        args.max_in_flight,
                    ),
                }
            results["tests"][name] = endpoint_results
            await asyncio.sleep(2)
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--endpoint", choices=["metrics", "ready", "both"], default="both")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-connections", type=int, default=600)
    parser.add_argument("--max-in-flight", type=int, default=500)
    parser.add_argument("--target-rps", type=float, default=333.0)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument(
        "--bursts",
        type=int,
        nargs="*",
        default=[25, 50, 100, 200, 400, 800],
    )
    parser.add_argument("--output", type=Path, default=Path(DEFAULT_OUTPUT))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = asyncio.run(run(args))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nSaved: {args.output.resolve()}")


if __name__ == "__main__":
    main()
