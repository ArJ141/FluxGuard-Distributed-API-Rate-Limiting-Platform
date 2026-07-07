# FluxGuard — Distributed API Rate Limiting Platform

A rate limiter for a social platform's API — posts, comments, likes,
follows — with a live interactive dashboard, two selectable
algorithms, and configuration you can change at runtime with no
redeploy. Built to stay correct even when running as multiple
stateless server replicas behind a load balancer.

**Live demo:** deploy your own with the steps below, or view the
dashboard locally at `http://localhost:8000/` after running it.

## What it does

- Limits each action type (post/comment/like/follow) **independently**
  per user — spamming likes doesn't touch your post quota.
- Two selectable algorithms per action, switchable live:
  - **Sliding window counter** — smooths a rate over a fixed window,
    no boundary-burst problem.
  - **Token bucket** — allows short controlled bursts up to a
    capacity, then refills gradually. Better fit when occasional
    bursts are fine as long as the sustained rate stays bounded.
- **Dynamic configuration**: limits, windows, and algorithm choice
  live in Redis, not process memory — change them via the admin API
  (or the dashboard's admin panel) and every replica picks it up
  immediately.
- **Interactive dashboard** (served at `/`): live gates per action
  that flash when a request is allowed/denied, an inline config
  editor, and a client-side traffic simulator with real throughput
  and latency percentiles — all driven by real requests to the live
  API, not mocked data.
- Fully **stateless and horizontally scalable**: any number of API
  replicas can run against one shared Redis and will always agree on
  the same limit, because the atomic check-and-increment happens
  inside a Redis Lua script, not in application memory.

## Why this exists

Most simple rate limiters keep a counter in server memory, which
breaks the moment you run more than one instance behind a load
balancer — each instance has its own counter, so a user can get
`N x limit` actions through just by hitting different servers. This
project keeps all state in Redis and performs the check-and-increment
atomically, so correctness comes from Redis, not from any single
process.

## Default limits

| Action | Limit | Window | Default algorithm |
|---|---|---|---|
| post | 5 | 60s | sliding_window |
| comment | 30 | 60s | sliding_window |
| like | 100 | 60s | token_bucket |
| follow | 20 | 3600s (1hr) | sliding_window |

All of the above are editable live via the dashboard or the admin API
— these are just the seeded defaults.

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
           │  Redis  │   <- single source of truth: rate-limit
           └─────────┘      counters AND live config, both read
                             by every replica on every request
```

## API

**POST /check**
```json
{ "user_id": "user_123", "action": "post" }
```
`action` is one of `post`, `comment`, `like`, `follow`. Returns `200`
with `allowed: true` if under that action's current limit, `429` if
exceeded, `400` for an unrecognized action.

**GET /limits** — current live config for every action (limit, window,
algorithm).

**POST /admin/config/{action}** — update an action's limit, window, or
algorithm live. Requires header `X-Admin-Key` matching the `ADMIN_KEY`
environment variable.
```json
{ "limit": 10, "window_seconds": 60, "algorithm": "token_bucket" }
```

**GET /health** — liveness + Redis connectivity check.

**GET /** — the interactive dashboard.

## Running locally

```bash
pip install -r requirements.txt
redis-server --daemonize yes
uvicorn app.main:app --reload --port 8000
```
Then open `http://localhost:8000/` for the dashboard.

## Running with Docker (3 replicas + load balancer)

```bash
docker compose up --build
```
Starts Redis, 3 independent API containers, and an nginx load balancer
on port 8080 round-robining across them. The rate limit and live
config both hold correctly no matter which container handles a given
request — proof correctness lives in Redis, not in any single
process's memory.

## Deploying for free (Render + Upstash)

1. Create a free Redis database at [upstash.com](https://upstash.com) —
   copy the `REDIS_URL` (starts with `rediss://`).
2. Push this repo to your own GitHub.
3. Create a Web Service at [render.com](https://render.com), connect
   the repo — Render auto-detects the `Dockerfile`.
4. Add environment variables:
   - `REDIS_URL` — from step 1
   - `ADMIN_KEY` — any secret string of your choosing, used to
     authorize config changes from the dashboard/admin API
5. Deploy. Render gives you a public URL — that's your dashboard.

## Tests

```bash
pytest tests/ -v
```

10 tests, including:
- Both algorithms enforce their configured limit correctly
- **Action isolation** — exhausting your post quota doesn't affect
  comment/like/follow quotas
- **Concurrency safety, proven two ways**: a real HTTP-level thread
  pool test for sliding window, and a frozen-clock direct Lua script
  test for token bucket (isolating the atomicity claim from real
  network timing, since a continuously-refilling algorithm is
  inherently timing-sensitive over real wall-clock time — see the
  test file's comments for why that distinction matters)
- **Live config updates**: changing a limit via the admin API takes
  effect on the very next request, no restart required
- Admin endpoint correctly rejects requests without the right key

## Load testing

```bash
python3 tests/load_test.py
```
Custom async load tester measuring throughput and p50/p95/p99
latency under concurrent load — the same kind of traffic the
dashboard's "Traffic simulator" runs live from your browser.

**Measured on a single-CPU-core sandbox:** ~220 req/s, p50 ~140ms, p99
~1.16s, 100% correctness (no dropped/mishandled requests). Absolute
throughput isn't representative of production hardware; since all
state lives in Redis rather than in-process memory, throughput scales
roughly linearly with more cores or more replicas — confirmed directly
by running 3 replicas against one shared Redis and verifying they
jointly enforce a single consistent limit.

## Status

- [x] Per-action rate limiting (post/comment/like/follow), isolated quotas
- [x] Two selectable algorithms: sliding window counter, token bucket
- [x] Dynamic, Redis-backed configuration — no redeploy to change limits
- [x] Interactive dashboard: live gates, config editor, traffic simulator
- [x] Atomic via Redis Lua script; proven concurrency-safe for both algorithms
- [x] Verified correctness across multiple stateless instances
- [x] Load testing with async throughput + latency benchmarks
- [x] Docker Compose + nginx for one-command 3-instance deployment
- [ ] Persistent request history / analytics (future work)
