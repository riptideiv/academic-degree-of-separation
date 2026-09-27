import pytest
from fastapi import HTTPException
from starlette.requests import Request

import backend.rate_limit as rate_limit
from backend.rate_limit import SlidingWindowLimiter, enforce_expand, enforce_institution


@pytest.fixture(autouse=True)
def _reset_limits(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_EXPAND_PER_MIN", "1")
    monkeypatch.setenv("RATE_LIMIT_PATH_PER_MIN", "1")
    monkeypatch.setenv("RATE_LIMIT_INSTITUTION_PER_MIN", "1")
    rate_limit.reset_for_tests()
    yield
    rate_limit.reset_for_tests()


def _request(ip: str = "203.0.113.10", forwarded: str | None = None) -> Request:
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "client": (ip, 12345),
        "server": ("test", 443),
    }
    return Request(scope)


def test_sliding_window_allows_then_blocks():
    limiter = SlidingWindowLimiter(2, window_s=60.0)
    assert limiter.allow("a") is True
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
    assert limiter.allow("b") is True


def test_enforce_expand_returns_429_after_limit():
    req = _request()
    enforce_expand(req)
    with pytest.raises(HTTPException) as exc:
        enforce_expand(req)
    assert exc.value.status_code == 429
    assert "graph expansions" in exc.value.detail.lower()


def test_rate_limits_are_per_ip_via_forwarded_for():
    enforce_institution(_request(forwarded="198.51.100.1, 10.0.0.1"))
    enforce_institution(_request(forwarded="198.51.100.2, 10.0.0.1"))
    with pytest.raises(HTTPException):
        enforce_institution(_request(forwarded="198.51.100.1, 10.0.0.1"))
