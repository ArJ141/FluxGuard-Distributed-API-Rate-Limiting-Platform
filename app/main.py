"""
Distributed rate limiter API.

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
   - Token bucket: better for allowing controlled bursts. Good
     stretch goal to add as a second algorithm.
"""

import time
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import redis

app = FastAPI(title="Distributed Rate Limiter")

# Connect to Redis. In docker-compose this hostname will be "redis".
redis_client = redis.Redis(
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
    client_id: str
    limit: int = 100          # max requests per window
    window_seconds: int = 60  # window size


@app.post("/check")
def check_rate_limit(req: CheckRequest):
    now_ms = int(time.time() * 1000)
    window_ms = req.window_seconds * 1000

    current_window = now_ms // window_ms
    previous_window = current_window - 1
    elapsed_ms = now_ms - (current_window * window_ms)

    current_key = f"ratelimit:{req.client_id}:{current_window}"
    previous_key = f"ratelimit:{req.client_id}:{previous_window}"

    allowed, estimated_count = check_and_increment(
        keys=[current_key, previous_key],
        args=[req.limit, req.window_seconds, elapsed_ms],
    )

    result = {
        "allowed": bool(allowed),
        "limit": req.limit,
        "estimated_count": round(float(estimated_count), 2),
        "window_seconds": req.window_seconds,
    }

    if not allowed:
        raise HTTPException(status_code=429, detail=result)

    return result


@app.get("/health")
def health():
    try:
        redis_client.ping()
        return {"status": "ok", "redis": "connected"}
    except redis.ConnectionError:
        raise HTTPException(status_code=503, detail="redis unreachable")
