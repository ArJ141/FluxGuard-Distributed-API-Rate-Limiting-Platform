"""
Automated tests for the rate limiter.

Two things we specifically want proof of, not just a manual demo:
1. Correctness: exactly `limit` requests succeed per window, no more.
2. Concurrency-safety: the race condition we designed against (two
   requests reading the same counter before either writes) does NOT
   let extra requests slip through, even when fired truly in parallel.
"""

import time
import uuid
import concurrent.futures
import httpx

BASE_URL = "http://localhost:8000"


def unique_client_id():
    # Fresh client id per test so tests don't interfere with each other
    # via leftover Redis keys from previous runs.
    return f"test_{uuid.uuid4().hex[:8]}"


def test_allows_requests_up_to_limit():
    client_id = unique_client_id()
    limit = 5

    for i in range(limit):
        resp = httpx.post(f"{BASE_URL}/check", json={
            "client_id": client_id, "limit": limit, "window_seconds": 10
        })
        assert resp.status_code == 200, f"Request {i+1} should be allowed"


def test_denies_requests_over_limit():
    client_id = unique_client_id()
    limit = 5

    for _ in range(limit):
        httpx.post(f"{BASE_URL}/check", json={
            "client_id": client_id, "limit": limit, "window_seconds": 10
        })

    # The (limit+1)-th request must be denied
    resp = httpx.post(f"{BASE_URL}/check", json={
        "client_id": client_id, "limit": limit, "window_seconds": 10
    })
    assert resp.status_code == 429


def test_concurrent_requests_do_not_exceed_limit():
    """
    This is the important one. Fire `limit + extra` requests at the
    SAME instant using a thread pool, for the SAME client_id. If the
    check-and-increment were not atomic, some of these requests would
    read a stale counter value and slip through, and allowed_count
    would end up higher than `limit`.
    """
    client_id = unique_client_id()
    limit = 10
    total_requests = 25

    def fire_request(_):
        resp = httpx.post(f"{BASE_URL}/check", json={
            "client_id": client_id, "limit": limit, "window_seconds": 10
        })
        return resp.status_code == 200

    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as executor:
        results = list(executor.map(fire_request, range(total_requests)))

    allowed_count = sum(results)
    assert allowed_count == limit, (
        f"Expected exactly {limit} requests to succeed under concurrent "
        f"load, but {allowed_count} succeeded. This would indicate a "
        f"race condition in the check-and-increment logic."
    )


def test_window_resets_over_time():
    """Confirms requests are allowed again once the window has elapsed."""
    client_id = unique_client_id()
    limit = 2
    window = 3  # short window so the test doesn't take long

    for _ in range(limit):
        resp = httpx.post(f"{BASE_URL}/check", json={
            "client_id": client_id, "limit": limit, "window_seconds": window
        })
        assert resp.status_code == 200

    denied = httpx.post(f"{BASE_URL}/check", json={
        "client_id": client_id, "limit": limit, "window_seconds": window
    })
    assert denied.status_code == 429

    time.sleep(window + 1)

    allowed_again = httpx.post(f"{BASE_URL}/check", json={
        "client_id": client_id, "limit": limit, "window_seconds": window
    })
    assert allowed_again.status_code == 200
