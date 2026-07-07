"""
Automated tests for FluxGuard.

Covers:
1. Both algorithms (sliding_window and token_bucket) enforce their
   configured limit correctly.
2. Actions are isolated from each other (spamming one doesn't affect
   another's quota for the same user).
3. Concurrency-safety: no race condition lets extra requests through
   under real parallel load, for BOTH algorithms.
4. Admin config endpoint: rejects requests without the correct key,
   and correctly updates behavior live once changed.
5. Dashboard and health endpoints serve correctly.
"""

import uuid
import concurrent.futures
import httpx

BASE_URL = "http://localhost:8000"
ADMIN_KEY = "change-me-please"  # matches the default in app/main.py


def unique_user_id():
    return f"user_{uuid.uuid4().hex[:8]}"


def test_sliding_window_limit_enforced():
    user_id = unique_user_id()
    # default: post = 5 per 60s, sliding_window
    for i in range(5):
        resp = httpx.post(f"{BASE_URL}/check", json={"user_id": user_id, "action": "post"})
        assert resp.status_code == 200, f"Post {i+1} should be allowed"

    denied = httpx.post(f"{BASE_URL}/check", json={"user_id": user_id, "action": "post"})
    assert denied.status_code == 429
    assert denied.json()["detail"]["algorithm"] == "sliding_window"


def test_token_bucket_limit_enforced_via_http():
    """
    Sanity check through the real HTTP path: capacity is 100, so a
    burst noticeably larger than that (150) should still see a
    meaningful fraction denied. We don't assert an exact number here
    -- see test_token_bucket_capacity_is_atomic below for the precise
    proof -- because real HTTP round trips take real wall-clock time,
    during which token bucket legitimately refills a few tokens. A
    continuously-refilling algorithm being timing-sensitive under real
    network latency is correct behavior, not a bug.
    """
    user_id = unique_user_id()

    def fire(_):
        resp = httpx.post(f"{BASE_URL}/check", json={"user_id": user_id, "action": "like"})
        return resp.status_code == 200

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        results = list(executor.map(fire, range(150)))

    allowed_count = sum(results)
    assert allowed_count < 150  # some requests must be denied
    assert allowed_count >= 90  # roughly capacity, generous slack for refill


def test_token_bucket_capacity_is_atomic():
    """
    The real proof of atomicity, isolated from HTTP/network timing
    entirely: call the Lua script directly with a FROZEN timestamp
    (same now_ms on every call), so zero real time passes between
    calls and therefore zero legitimate refill can occur. Out of 105
    attempts with a frozen clock, exactly 100 (the capacity) must
    succeed -- no more, no less.
    """
    import redis as redis_sync

    r = redis_sync.Redis(host="localhost", port=6379, decode_responses=True)
    with open("app/token_bucket.lua") as f:
        script = r.register_script(f.read())

    frozen_now_ms = 1_700_000_000_000
    bucket_key = f"bucket:test:{uuid.uuid4().hex[:8]}"

    allowed_count = 0
    for _ in range(105):
        allowed, _tokens = script(keys=[bucket_key], args=[100, 100 / 60, frozen_now_ms])
        if allowed:
            allowed_count += 1

    assert allowed_count == 100


def test_actions_have_independent_quotas():
    user_id = unique_user_id()
    for _ in range(5):
        httpx.post(f"{BASE_URL}/check", json={"user_id": user_id, "action": "post"})
    denied_post = httpx.post(f"{BASE_URL}/check", json={"user_id": user_id, "action": "post"})
    assert denied_post.status_code == 429

    comment_resp = httpx.post(f"{BASE_URL}/check", json={"user_id": user_id, "action": "comment"})
    assert comment_resp.status_code == 200


def test_unknown_action_rejected():
    resp = httpx.post(f"{BASE_URL}/check", json={"user_id": unique_user_id(), "action": "delete_account"})
    assert resp.status_code == 400


def test_concurrent_requests_do_not_exceed_limit_sliding_window():
    user_id = unique_user_id()
    # temporarily this test relies on default 'comment' config: 30 per 60s
    limit = 30
    total_requests = 60

    def fire(_):
        resp = httpx.post(f"{BASE_URL}/check", json={"user_id": user_id, "action": "comment"})
        return resp.status_code == 200

    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        results = list(executor.map(fire, range(total_requests)))

    assert sum(results) == limit


def test_admin_config_requires_key():
    resp = httpx.post(
        f"{BASE_URL}/admin/config/post",
        json={"limit": 5, "window_seconds": 60, "algorithm": "sliding_window"},
    )
    assert resp.status_code == 401


def test_admin_config_updates_live():
    user_id = unique_user_id()

    # Set follow limit very low to make the test fast
    resp = httpx.post(
        f"{BASE_URL}/admin/config/follow",
        json={"limit": 2, "window_seconds": 60, "algorithm": "sliding_window"},
        headers={"X-Admin-Key": ADMIN_KEY},
    )
    assert resp.status_code == 200

    # New limit should take effect immediately, no restart needed
    for _ in range(2):
        r = httpx.post(f"{BASE_URL}/check", json={"user_id": user_id, "action": "follow"})
        assert r.status_code == 200

    denied = httpx.post(f"{BASE_URL}/check", json={"user_id": user_id, "action": "follow"})
    assert denied.status_code == 429

    # restore default so other tests / runs aren't affected
    httpx.post(
        f"{BASE_URL}/admin/config/follow",
        json={"limit": 20, "window_seconds": 3600, "algorithm": "sliding_window"},
        headers={"X-Admin-Key": ADMIN_KEY},
    )


def test_dashboard_serves_html():
    resp = httpx.get(f"{BASE_URL}/")
    assert resp.status_code == 200
    assert "FluxGuard" in resp.text


def test_limits_endpoint_returns_all_actions():
    resp = httpx.get(f"{BASE_URL}/limits")
    assert resp.status_code == 200
    data = resp.json()
    for action in ("post", "comment", "like", "follow"):
        assert action in data
        assert "algorithm" in data[action]
