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

## Status / roadmap

- [x] Sliding window counter algorithm, atomic via Redis Lua script
- [x] Automated tests including concurrency safety
- [x] Verified correctness across multiple stateless instances
- [ ] Load testing with k6/locust for throughput + latency benchmarks
- [ ] Docker Compose for one-command deployment
- [ ] Token bucket as a second selectable algorithm
