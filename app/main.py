"""
API rate limiter for a social platform: limits posts, comments, likes,
and follows per user, independently, so a single user can't spam any
one action even though all actions share the same underlying engine.

Design notes (the stuff you'll explain in an interview):

1. Statelessness: this FastAPI process holds NO in-memory counters.
   All state lives in Redis. That's what makes it "distributed" --
   you can run 5 copies of this process behind a load balancer and
   they all agree on the rate limit, because they all check the
   same Redis keys.

2. Atomicity: the check-and-increment happens inside a single Lua
   script executed by Redis (see sliding_window.lua). This avoids
   the classic read-then-write race condition you'd get if you did
   `count = redis.get(key); if count < limit: redis.set(key, count+1)`
   as two separate round trips.

3. Why sliding window counter and not token bucket or sliding log:
   - Fixed window: simplest, but allows 2x burst at window boundaries.
   - Sliding window log: perfectly accurate (stores every request
     timestamp) but memory-expensive at scale -- O(n) per client.
   - Sliding window counter (what we use): O(1) memory per client,
     small approximation error, good enough for almost all real
     rate limiting. This is what Cloudflare and Kong use in practice.

4. Per-action limits: each action type (post, comment, like, follow)
   has its own limit and window, and its own Redis key namespace, so
   spamming comments doesn't use up your post quota and vice versa.
   This mirrors how real platforms rate limit -- Twitter/X, for
   example, limits posts, likes, and follows completely separately.
"""

import time
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import redis.asyncio as aredis

app = FastAPI(title="Social Platform Rate Limiter")

# Preset limits per action. In a real system these might live in a
# config file or admin-editable database table -- kept as a simple
# dict here since that's not the interesting part of this project.
ACTION_LIMITS = {
    "post":    {"limit": 5,   "window_seconds": 60},   # 5 posts/min
    "comment": {"limit": 30,  "window_seconds": 60},   # 30 comments/min
    "like":    {"limit": 100, "window_seconds": 60},   # 100 likes/min
    "follow":  {"limit": 20,  "window_seconds": 3600}, # 20 follows/hour
}

# Prefer a full connection URL (what every cloud Redis provider gives
# you -- Upstash, Render, Railway, etc. -- including TLS via rediss://)
# and fall back to host/port for local development.
REDIS_URL = os.getenv("REDIS_URL")
if REDIS_URL:
    redis_client = aredis.from_url(REDIS_URL, decode_responses=True)
else:
    redis_client = aredis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True,
    )

# Load the Lua script once at startup and register it with Redis.
# redis-py caches the script's SHA and re-uses it (EVALSHA) for speed
# instead of re-sending the full script text on every call.
with open(os.path.join(os.path.dirname(__file__), "sliding_window.lua")) as f:
    SLIDING_WINDOW_SCRIPT = f.read()

check_and_increment = redis_client.register_script(SLIDING_WINDOW_SCRIPT)


class CheckRequest(BaseModel):
    user_id: str
    action: str  # "post", "comment", "like", or "follow"


async def _run_check(user_id: str, action: str, limit: int, window_seconds: int):
    now_ms = int(time.time() * 1000)
    window_ms = window_seconds * 1000

    current_window = now_ms // window_ms
    previous_window = current_window - 1
    elapsed_ms = now_ms - (current_window * window_ms)

    # Namespacing by action means a user's comment count and post count
    # are tracked as completely separate Redis keys -- spamming one
    # action never eats into another action's quota.
    current_key = f"ratelimit:{action}:{user_id}:{current_window}"
    previous_key = f"ratelimit:{action}:{user_id}:{previous_window}"

    allowed, estimated_count = await check_and_increment(
        keys=[current_key, previous_key],
        args=[limit, window_seconds, elapsed_ms],
    )
    return bool(allowed), float(estimated_count)


@app.post("/check")
async def check_rate_limit(req: CheckRequest):
    if req.action not in ACTION_LIMITS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action '{req.action}'. Valid actions: {list(ACTION_LIMITS.keys())}",
        )

    config = ACTION_LIMITS[req.action]
    allowed, estimated_count = await _run_check(
        req.user_id, req.action, config["limit"], config["window_seconds"]
    )

    result = {
        "allowed": allowed,
        "user_id": req.user_id,
        "action": req.action,
        "limit": config["limit"],
        "estimated_count": round(estimated_count, 2),
        "window_seconds": config["window_seconds"],
    }

    if not allowed:
        raise HTTPException(status_code=429, detail=result)

    return result


@app.get("/limits")
async def get_limits():
    """So a frontend/client can display 'you have X posts left' etc."""
    return ACTION_LIMITS


@app.get("/health")
async def health():
    try:
        await redis_client.ping()
        return {"status": "ok", "redis": "connected"}
    except Exception:
        raise HTTPException(status_code=503, detail="redis unreachable")
