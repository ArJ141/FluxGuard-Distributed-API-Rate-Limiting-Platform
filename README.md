# Social Platform API Rate Limiter

Rate limits per-user actions (posts, comments, likes, follows) on a
social platform's API — e.g. max 5 posts/min, 30 comments/min, 100
likes/min per user — and stays correct even when the API runs as
multiple independent server replicas behind a load balancer.

## Why this exists

Real platforms limit each action type separately: spamming likes
shouldn't use up your ability to post, and vice versa. Most simple
rate limiters also keep counters in server memory, which breaks the
moment you run more than one server instance — each instance has its
own counter, so a user can get `N x limit` actions through just by
hitting different servers. This project fixes both problems: limits
are tracked per action, and all state lives in Redis with an atomic
Lua script doing the check-and-increment, so any number of stateless
app servers enforce one consistent limit per user per action.

## Preset limits

| Action | Limit | Window |
|---|---|---|
| post | 5 | 60s |
| comment | 30 | 60s |
| like | 100 | 60s |
| follow | 20 | 3600s (1hr) |

Configurable in `app/main.py` (`ACTION_LIMITS`).

## Algorithm: Sliding Window Counter

| Algorithm | Memory per user | Accuracy | Notes |
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
           └─────────┘      atomic Lua script check-and-increment,
                             keys namespaced per action per user
```

## API

**POST /check**
```json
{
  "user_id": "user_123",
  "action": "post"
}
```
`action` must be one of `post`, `comment`, `like`, `follow`. Returns
`200` with `allowed: true` if under that action's limit, or `429` with
`allowed: false` if exceeded. Returns `400` for an unrecognized action.

**GET /limits** — returns the configured limits, e.g. for a frontend
to show "you have 2 posts left this minute."

**GET /health** — liveness + Redis connectivity check.

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

Starts Redis, 3 independent API containers, and an nginx load balancer
on port 8080 round-robining across them. The rate limit holds correctly
no matter which container handles a given request — proof correctness
lives in Redis, not in any single process's memory.

## Deploying for free (Render + Upstash)

1. Create a free Redis database at [upstash.com](https://upstash.com) —
   copy the `REDIS_URL` it gives you (starts with `rediss://`).
2. Push this repo to your own GitHub.
3. Create a new Web Service at [render.com](https://render.com), connect
   the repo — Render auto-detects the `Dockerfile`.
4. Add an environment variable `REDIS_URL` with the value from step 1.
5. Deploy. Render gives you a public URL (`https://your-app.onrender.com`).

The app reads `REDIS_URL` if set (see `app/main.py`), falling back to
local `REDIS_HOST`/`REDIS_PORT` for local development.

## Tests

```bash
pytest tests/ -v
```

5 tests, including:
- Per-action limits enforced correctly (post limit = 5, etc.)
- **Action isolation**: exhausting your post quota doesn't affect your
  comment or like quota for the same user
- **Concurrency safety**: 150 simultaneous "like" requests fired via a
  thread pool against a limit of 100 — exactly 100 succeed, proving the
  atomic Lua script prevents the race condition a naive
  read-then-write implementation would suffer from
- Unknown actions rejected with a clear 400

## Load testing

```bash
python3 tests/load_test.py
```

Custom async load tester (no external tool needed) measuring
throughput and p50/p95/p99 latency under concurrent load.

**Measured on this single-CPU-core sandbox:** ~220-225 req/s, p50 ~140ms,
p99 ~1.16s, with 100% of requests handled correctly (no errors, no
dropped requests) whether they were allowed or denied. Absolute
throughput isn't representative of production hardware — the same
code with more cores or more replicas behind the load balancer scales
roughly linearly, since all state lives in Redis rather than
in-process memory. This was confirmed directly by running 3 replicas
against one shared Redis and verifying they jointly enforce a single
consistent limit.

One real bug this surfaced during development: the endpoint originally
used a sync `def` with the sync redis-py client. FastAPI runs sync
endpoints in a small worker thread pool (~40 threads by default), so
under concurrent load, requests queued waiting for a free thread — a
self-inflicted bottleneck unrelated to Redis or the network. Switching
to `async def` with `redis.asyncio` removed that bottleneck by running
directly on the event loop instead.

## Status

- [x] Per-action rate limiting (post/comment/like/follow), isolated quotas
- [x] Sliding window counter algorithm, atomic via Redis Lua script
- [x] Automated tests including action isolation + concurrency safety
- [x] Verified correctness across multiple stateless instances
- [x] Load testing with async throughput + latency benchmarks
- [x] Docker Compose + nginx for one-command 3-instance deployment
- [ ] Token bucket as a second selectable algorithm (future work)
- [ ] Deployed live instance (Render + Upstash — see deployment steps above)
