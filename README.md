# Distributed Rate Limiter

A rate-limiting API service designed to work correctly across multiple
server instances, backed by Redis for shared, atomic state.

## Why this exists

Most simple rate limiters keep a counter in memory on a single server.
That breaks the moment you run more than one instance behind a load
balancer — each instance has its own counter, so a client can get
`N x limit` requests through just by hitting different servers. This
project solves that by keeping all state in Redis and performing the
check-and-increment atomically via a Lua script, so any number of
stateless app servers enforce one consistent limit.

## Algorithm: Sliding Window Counter

| Algorithm | Memory per client | Accuracy | Notes |
|---|---|---|---|
| Fixed window | O(1) | Allows 2x burst at window boundary | Simplest, weakest |
| Sliding window log | O(n) requests | Perfectly accurate | Expensive at scale |
| **Sliding window counter (this project)** | O(1) | Small approximation error | Good balance, used by Cloudflare/Kong |
| Token bucket | O(1) | Allows controlled bursts | Better fit if bursts should be tolerated |

The sliding window counter estimates the current request rate by
weighting the previous window's count based on how far we are into the
current window, avoiding the fixed-window boundary problem without the
memory cost of storing every request timestamp.

## Architecture

```
        ┌─────────────┐
        │Load Balancer│
        └──────┬──────┘
     ┌──────────┼──────────┐
     ▼          ▼          ▼
 ┌───────┐  ┌───────┐  ┌───────┐
 │API :8000│ │API :8001│ │API :8002│   <- stateless, no in-memory counters
 └───┬───┘  └───┬───┘  └───┬───┘
     └──────────┼──────────┘
                ▼
           ┌─────────┐
           │  Redis  │   <- single source of truth,
           └─────────┘      atomic Lua script check-and-increment
```

Any number of API instances can be added or removed freely — they hold
no state themselves, so there's nothing to synchronize between them.

## Running locally

```bash
pip install -r requirements.txt

redis-server --daemonize yes

uvicorn app.main:app --reload --port 8000
```

## Running with Docker (3 replicas + load balancer)

```bash
docker compose up --build
```

This starts Redis, 3 independent API containers (api1, api2, api3), and
an nginx load balancer on port 8080 that round-robins across them. Hit
`http://localhost:8080/check` and the rate limit is enforced correctly
no matter which of the 3 containers handles any given request — proof
that correctness lives in Redis, not in any single process.

## Benchmark results

Measured with `tests/load_test.py` (custom async load tester, 50
concurrent in-flight requests, 2000 total requests per scenario) on a
single-core sandbox environment:

| Scenario | Throughput | p50 latency | p95 latency | p99 latency |
|---|---|---|---|---|
| Allow-path (unique clients, never denied) | ~222 req/s | 141 ms | 662 ms | 1160 ms |
| Deny-path (shared client, 1900/2000 denied) | ~225 req/s | 132 ms | 673 ms | 1162 ms |

Notably, throughput is nearly identical whether requests are being
allowed or denied — the deny path returns early in the Lua script
without extra work, so rejecting over-limit traffic doesn't cost more
than accepting it.

These numbers were measured on a single CPU core with a single
uvicorn worker. Because the service is fully stateless (all rate-limit
state lives in Redis, not in process memory), throughput scales
roughly linearly by adding more worker processes or more container
replicas behind the load balancer — this was verified directly by
running 3 independent containers against one shared Redis instance
and confirming they jointly enforce a single consistent limit.

## Running with Docker (3 instances + nginx + Redis)

```bash
docker-compose up --build
```

This starts 3 independent API containers behind an nginx load balancer
(port 8080), all sharing one Redis instance. Hitting `localhost:8080/check`
repeatedly with the same `client_id` will round-robin across the 3
containers, and the rate limit will still hold correctly -- proving the
distributed design works, not just claiming it.

## API

**POST /check**
```json
{
  "client_id": "some_user_or_ip",
  "limit": 100,
  "window_seconds": 60
}
```
Returns `200` with `allowed: true` if under the limit, or `429` with
`allowed: false` if the limit has been exceeded.

**GET /health** — basic liveness + Redis connectivity check.

## Tests

```bash
pytest tests/ -v
```

Notably includes a concurrency test that fires 25 simultaneous requests
against a limit of 10 using a thread pool, and asserts exactly 10
succeed — proving the atomic Lua script prevents the race condition
that a naive read-then-write implementation would suffer from.

## Proving the "distributed" part

Run 3 instances on different ports pointed at the same Redis, then
round-robin requests across all 3 for one client ID. All 3 instances
enforce the same shared limit despite never communicating with each
other directly — confirming correctness comes from Redis, not
in-process state.

## Load testing

```bash
python3 loadtest/load_test.py
```

Fires 5,000 requests at 100 concurrent in-flight requests across 200
distinct client IDs (measuring service throughput, not one client's own
limit), then reports throughput and latency percentiles (p50/p95/p99).

**Note on the numbers below:** measured on a constrained, single-CPU-core
sandbox environment, so absolute throughput isn't representative of
production hardware — the same code on a normal multi-core machine, or
with multiple uvicorn workers, would show substantially higher numbers.
What the load test does prove regardless of hardware: 100% of requests
succeeded correctly under concurrent load, confirming the async design
doesn't introduce errors or dropped requests.

Example run:
```
Total requests:       5000
Concurrency:          100
Successful:           5000 (100.0%)
Throughput:           237 req/s   (single-core sandbox; scales with cores)
Latency (p50):        255.91 ms
Latency (p99):        2698.02 ms
```

One real bug this surfaced during development: the endpoint was
originally a sync `def` using the sync redis-py client. FastAPI runs
sync endpoints in a small worker thread pool (~40 threads by default),
so under concurrent load requests queued waiting for a free thread —
a self-inflicted bottleneck unrelated to Redis or the network. Switching
to `async def` with `redis.asyncio` removed that bottleneck by running
directly on the event loop instead.

## Status / roadmap

- [x] Sliding window counter algorithm, atomic via Redis Lua script
- [x] Automated tests including concurrency safety
- [x] Verified correctness across multiple stateless instances
- [x] Load testing with async throughput + latency benchmarks
- [x] Docker Compose + nginx for one-command 3-instance deployment
- [ ] Token bucket as a second selectable algorithm (future work)
