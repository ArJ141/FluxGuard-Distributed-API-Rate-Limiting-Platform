"""
Automated tests for the social platform rate limiter.

Same two things we care about as before, now expressed through
actions (post/comment/like/follow) instead of a generic client:
1. Correctness: exactly the configured limit succeeds per action.
2. Concurrency-safety: no race condition lets extra requests through
   even when fired truly in parallel.
3. New: actions are isolated -- spamming "post" doesn't touch the
   "comment" quota for the same user.
"""

import uuid
import concurrent.futures
import httpx

BASE_URL = "http://localhost:8000"


def unique_user_id():
    return f"user_{uuid.uuid4().hex[:8]}"


def test_post_limit_enforced():
    user_id = unique_user_id()
    # ACTION_LIMITS["post"] = 5 per 60s
    for i in range(5):
        resp = httpx.post(f"{BASE_URL}/check", json={"user_id": user_id, "action": "post"})
        assert resp.status_code == 200, f"Post {i+1} should be allowed"

    denied = httpx.post(f"{BASE_URL}/check", json={"user_id": user_id, "action": "post"})
    assert denied.status_code == 429


def test_actions_have_independent_quotas():
    """
    A user who has used up their post quota should still be able to
    comment and like -- these are meant to be tracked completely
    separately, mirroring how real platforms rate limit each action
    type independently.
    """
    user_id = unique_user_id()

    # Exhaust the post quota (limit = 5)
    for _ in range(5):
        httpx.post(f"{BASE_URL}/check", json={"user_id": user_id, "action": "post"})
    denied_post = httpx.post(f"{BASE_URL}/check", json={"user_id": user_id, "action": "post"})
    assert denied_post.status_code == 429

    # But comment and like should be completely unaffected
    comment_resp = httpx.post(f"{BASE_URL}/check", json={"user_id": user_id, "action": "comment"})
    assert comment_resp.status_code == 200

    like_resp = httpx.post(f"{BASE_URL}/check", json={"user_id": user_id, "action": "like"})
    assert like_resp.status_code == 200


def test_unknown_action_rejected():
    resp = httpx.post(f"{BASE_URL}/check", json={"user_id": unique_user_id(), "action": "delete_account"})
    assert resp.status_code == 400


def test_concurrent_requests_do_not_exceed_limit():
    """
    Fire many more "like" requests than the limit allows, all at the
    same instant via a thread pool, for the same user. If the
    check-and-increment were not atomic, some would read a stale
    counter and slip through -- allowed_count would exceed the limit.
    """
    user_id = unique_user_id()
    limit = 100  # ACTION_LIMITS["like"]
    total_requests = 150

    def fire_request(_):
        resp = httpx.post(f"{BASE_URL}/check", json={"user_id": user_id, "action": "like"})
        return resp.status_code == 200

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        results = list(executor.map(fire_request, range(total_requests)))

    allowed_count = sum(results)
    assert allowed_count == limit, (
        f"Expected exactly {limit} likes to succeed under concurrent "
        f"load, but {allowed_count} succeeded -- indicates a race "
        f"condition in the check-and-increment logic."
    )


def test_limits_endpoint_returns_config():
    resp = httpx.get(f"{BASE_URL}/limits")
    assert resp.status_code == 200
    data = resp.json()
    assert "post" in data and "comment" in data and "like" in data
