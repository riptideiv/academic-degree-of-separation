"""Lightweight in-process rate limits for expensive OpenAlex-backed routes.

Protects the shared deployment API key from burst abuse. Limits are per client
IP (honoring the first X-Forwarded-For hop behind Render) and fail closed with
HTTP 429 when exceeded. Disable by setting the matching env var to 0.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


class SlidingWindowLimiter:
    """Count hits in a fixed wall-clock window per key."""

    def __init__(self, limit: int, window_s: float = 60.0):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        bucket = self._hits[key]
        cutoff = now - self.window_s
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(now)
        return True

    def clear(self) -> None:
        self._hits.clear()


# Defaults leave headroom for normal UI use (expand + explorer polling) while
# capping scripted hammering of the shared OpenAlex key.
_expand_limiter = SlidingWindowLimiter(_env_int("RATE_LIMIT_EXPAND_PER_MIN", 30))
_path_limiter = SlidingWindowLimiter(_env_int("RATE_LIMIT_PATH_PER_MIN", 30))
_institution_limiter = SlidingWindowLimiter(
    _env_int("RATE_LIMIT_INSTITUTION_PER_MIN", 20)
)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def enforce(limiter: SlidingWindowLimiter, request: Request, *, detail: str) -> None:
    if not limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail=detail)


def enforce_expand(request: Request) -> None:
    enforce(
        _expand_limiter,
        request,
        detail="Too many graph expansions. Wait a minute and try again.",
    )


def enforce_path(request: Request) -> None:
    enforce(
        _path_limiter,
        request,
        detail="Too many path searches. Wait a minute and try again.",
    )


def enforce_institution(request: Request) -> None:
    enforce(
        _institution_limiter,
        request,
        detail="Too many institution searches. Wait a minute and try again.",
    )


def reset_for_tests() -> None:
    """Clear limiter state and re-read env so monkeypatched limits apply."""
    _expand_limiter.limit = _env_int("RATE_LIMIT_EXPAND_PER_MIN", 30)
    _path_limiter.limit = _env_int("RATE_LIMIT_PATH_PER_MIN", 30)
    _institution_limiter.limit = _env_int("RATE_LIMIT_INSTITUTION_PER_MIN", 20)
    _expand_limiter.clear()
    _path_limiter.clear()
    _institution_limiter.clear()
