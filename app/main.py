"""
FluxGuard: distributed API rate limiter for a social platform.

Limits posts, comments, likes, and follows per user, independently,
staying correct even when the API runs as multiple stateless replicas
behind a load balancer. Two selectable algorithms per action:

- sliding_window: smooths a rate over a fixed window, no boundary
  bursting. Good default for most actions.
- token_bucket: allows short controlled bursts up to a capacity, then
  refills gradually. Good fit when occasional bursts are fine as long
  as the sustained rate stays bounded (e.g. rapid-fire likes).

Configuration (limit/window/algorithm per action) is stored in Redis,
not in process memory, so it can be changed live via the admin API and
every replica picks up the change immediately -- no redeploy needed.
"""

import time
import os
import json
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
import redis.asyncio as aredis

app = FastAPI(title="FluxGuard Rate Limiter")

DEFAULT_CONFIG = {
    "post":    {"limit": 5,   "window_seconds": 60,   "algorithm": "sliding_window"},
    "comment": {"limit": 30,  "window_seconds": 60,   "algorithm": "sliding_window"},
    "like":    {"limit": 100, "window_seconds": 60,   "algorithm": "token_bucket"},
    "follow":  {"limit": 20,  "window_seconds": 3600, "algorithm": "sliding_window"},
}

ADMIN_KEY = os.getenv("ADMIN_KEY", "change-me-please")

REDIS_URL = os.getenv("REDIS_URL")
if REDIS_URL:
    redis_client = aredis.from_url(REDIS_URL, decode_responses=True)
else:
    redis_client = aredis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True,
    )

APP_DIR = os.path.dirname(__file__)

with open(os.path.join(APP_DIR, "sliding_window.lua")) as f:
    sliding_window_script = redis_client.register_script(f.read())

with open(os.path.join(APP_DIR, "token_bucket.lua")) as f:
    token_bucket_script = redis_client.register_script(f.read())


async def get_config():
    """
    Read live config from Redis. Falls back to defaults for any action
    not yet stored (e.g. on very first run before anything has been
    saved), and lazily seeds Redis so future reads/edits are consistent.
    """
    raw = await redis_client.hgetall("fluxguard:config")
    config = {}
    for action, defaults in DEFAULT_CONFIG.items():
        if action in raw:
            config[action] = json.loads(raw[action])
        else:
            config[action] = defaults
            await redis_client.hset("fluxguard:config", action, json.dumps(defaults))
    return config


class CheckRequest(BaseModel):
    user_id: str
    action: str


class ConfigUpdate(BaseModel):
    limit: int
    window_seconds: int
    algorithm: str  # "sliding_window" or "token_bucket"


async def _check_sliding_window(user_id: str, action: str, limit: int, window_seconds: int):
    now_ms = int(time.time() * 1000)
    window_ms = window_seconds * 1000
    current_window = now_ms // window_ms
    previous_window = current_window - 1
    elapsed_ms = now_ms - (current_window * window_ms)

    current_key = f"ratelimit:{action}:{user_id}:{current_window}"
    previous_key = f"ratelimit:{action}:{user_id}:{previous_window}"

    allowed, count = await sliding_window_script(
        keys=[current_key, previous_key],
        args=[limit, window_seconds, elapsed_ms],
    )
    return bool(allowed), float(count)


async def _check_token_bucket(user_id: str, action: str, limit: int, window_seconds: int):
    # capacity = limit (max burst), refill_rate = limit / window in tokens/sec
    refill_rate = limit / window_seconds
    now_ms = int(time.time() * 1000)
    bucket_key = f"bucket:{action}:{user_id}"

    allowed, tokens_remaining = await token_bucket_script(
        keys=[bucket_key],
        args=[limit, refill_rate, now_ms],
    )
    return bool(allowed), float(tokens_remaining)


@app.post("/check")
async def check_rate_limit(req: CheckRequest):
    config = await get_config()
    if req.action not in config:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action '{req.action}'. Valid actions: {list(config.keys())}",
        )

    cfg = config[req.action]

    if cfg["algorithm"] == "token_bucket":
        allowed, count = await _check_token_bucket(req.user_id, req.action, cfg["limit"], cfg["window_seconds"])
    else:
        allowed, count = await _check_sliding_window(req.user_id, req.action, cfg["limit"], cfg["window_seconds"])

    result = {
        "allowed": allowed,
        "user_id": req.user_id,
        "action": req.action,
        "algorithm": cfg["algorithm"],
        "limit": cfg["limit"],
        "estimated_count": round(count, 2),
        "window_seconds": cfg["window_seconds"],
    }

    if not allowed:
        raise HTTPException(status_code=429, detail=result)

    return result


@app.get("/limits")
async def get_limits():
    return await get_config()


@app.post("/admin/config/{action}")
async def update_config(action: str, update: ConfigUpdate, x_admin_key: Optional[str] = Header(None)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing admin key")

    if action not in DEFAULT_CONFIG:
        raise HTTPException(status_code=400, detail=f"Unknown action '{action}'")

    if update.algorithm not in ("sliding_window", "token_bucket"):
        raise HTTPException(status_code=400, detail="algorithm must be 'sliding_window' or 'token_bucket'")

    new_config = {
        "limit": update.limit,
        "window_seconds": update.window_seconds,
        "algorithm": update.algorithm,
    }
    await redis_client.hset("fluxguard:config", action, json.dumps(new_config))
    return {"action": action, "config": new_config, "message": "Updated live -- all replicas pick this up immediately"}


@app.get("/health")
async def health():
    try:
        await redis_client.ping()
        return {"status": "ok", "redis": "connected"}
    except Exception:
        raise HTTPException(status_code=503, detail="redis unreachable")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    dashboard_path = os.path.join(APP_DIR, "static", "dashboard.html")
    with open(dashboard_path) as f:
        return f.read()
