"""
Load test for the rate limiter.

Why a custom script instead of k6/locust: this sandbox has restricted
network access for installing extra tooling, and honestly, writing
this yourself is a better learning exercise than treating load testing
as a black box CLI tool. This measures exactly what a resume bullet
needs: sustained throughput (req/s) and latency percentiles (p50/p95/p99)
under concurrent load.

Two scenarios are run:
1. High-limit scenario: limit is set so high that requests are never
   denied. This measures pure throughput/latency of the allow path
   (Redis round trip + Lua script execution + FastAPI overhead).
2. Mixed scenario: a realistic limit where roughly half of requests
   get denied, to show the deny path (which returns early) is not
   meaningfully slower.
"""

import asyncio
import time
import uuid
import statistics
import httpx

BASE_URL = "http://localhost:8000"
CONCURRENCY = 50          # simultaneous in-flight requests
TOTAL_REQUESTS = 2000     # total requests to send per scenario


async def fire_one(client: httpx.AsyncClient, client_id: str, limit: int):
    start = time.perf_counter()
    try:
        resp = await client.post(f"{BASE_URL}/check", json={
            "client_id": client_id, "limit": limit, "window_seconds": 60
        })
        status = resp.status_code
    except Exception:
        status = -1
    elapsed_ms = (time.perf_counter() - start) * 1000
    return status, elapsed_ms


async def run_scenario(name: str, limit: int, use_shared_client_id: bool):
    print(f"\n=== Scenario: {name} ===")
    print(f"concurrency={CONCURRENCY}, total_requests={TOTAL_REQUESTS}, limit={limit}")

    client_id = f"loadtest_{uuid.uuid4().hex[:8]}" if use_shared_client_id else None
    latencies = []
    statuses = {}

    limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)
    async with httpx.AsyncClient(limits=limits, timeout=10.0) as client:
        sem = asyncio.Semaphore(CONCURRENCY)

        async def bounded_fire(i):
            async with sem:
                cid = client_id or f"loadtest_{uuid.uuid4().hex[:8]}"
                return await fire_one(client, cid, limit)

        start_time = time.perf_counter()
        results = await asyncio.gather(*[bounded_fire(i) for i in range(TOTAL_REQUESTS)])
        total_time = time.perf_counter() - start_time

    for status, latency_ms in results:
        latencies.append(latency_ms)
        statuses[status] = statuses.get(status, 0) + 1

    latencies.sort()
    n = len(latencies)
    p50 = latencies[int(n * 0.50)]
    p95 = latencies[int(n * 0.95)]
    p99 = latencies[int(n * 0.99)]
    avg = statistics.mean(latencies)
    throughput = TOTAL_REQUESTS / total_time

    print(f"Total time:      {total_time:.2f}s")
    print(f"Throughput:      {throughput:.1f} req/s")
    print(f"Latency avg:     {avg:.2f} ms")
    print(f"Latency p50:     {p50:.2f} ms")
    print(f"Latency p95:     {p95:.2f} ms")
    print(f"Latency p99:     {p99:.2f} ms")
    print(f"Status codes:    {statuses}")

    return {
        "name": name, "throughput": throughput, "p50": p50,
        "p95": p95, "p99": p99, "avg": avg, "statuses": statuses,
    }


async def main():
    results = []
    # Scenario 1: unique client per request -> nobody ever hits the limit,
    # measures pure allow-path performance.
    results.append(await run_scenario("Pure allow-path (unique clients)", limit=1_000_000, use_shared_client_id=False))

    # Scenario 2: single shared client with a low limit -> most requests
    # after the first few get denied, measures deny-path performance.
    results.append(await run_scenario("Deny-path (shared client, low limit)", limit=100, use_shared_client_id=True))

    print("\n=== Summary (paste into README) ===")
    for r in results:
        print(f"{r['name']}: {r['throughput']:.0f} req/s, "
              f"p50={r['p50']:.2f}ms, p95={r['p95']:.2f}ms, p99={r['p99']:.2f}ms")


if __name__ == "__main__":
    asyncio.run(main())
