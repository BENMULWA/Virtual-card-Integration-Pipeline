# =========================================================
# ratelimit.py - in-process sliding-window limiter
# =========================================================
#
# Protects the auth endpoints (register / login / verify) from brute force and
# credential stuffing. In-process only: a multi-worker deployment needs a shared
# store (Redis) to enforce the limit globally - see README "Going live" notes.

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque

from fastapi import HTTPException, Request


@dataclass
class Bucket:
    hits: Deque[float] = field(default_factory=deque)
    locked_until: float = 0.0


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int, lockout_seconds: int = 0):
        self.limit = limit
        self.window = window_seconds
        self.lockout = lockout_seconds
        self._buckets: dict[str, Bucket] = defaultdict(Bucket)
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> None:
        """Raise 429 if the key is over its limit. Records a hit otherwise."""
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets[key]
            if bucket.locked_until > now:
                retry_after = int(bucket.locked_until - now) + 1
                raise HTTPException(
                    429,
                    f"Too many attempts. Try again in {retry_after}s.",
                    headers={"Retry-After": str(retry_after)},
                )
            while bucket.hits and now - bucket.hits[0] > self.window:
                bucket.hits.popleft()
            if len(bucket.hits) >= self.limit:
                if self.lockout:
                    bucket.locked_until = now + self.lockout
                    bucket.hits.clear()
                raise HTTPException(
                    429,
                    "Too many attempts. Please try again later.",
                    headers={"Retry-After": str(self.window)},
                )
            bucket.hits.append(now)

    async def reset(self, key: str) -> None:
        """Clear a key's history - called after a successful login."""
        async with self._lock:
            self._buckets.pop(key, None)


# Distinct limiters: logins are the strictest, registration is looser.
login_limiter = RateLimiter(limit=8, window_seconds=300, lockout_seconds=300)
register_limiter = RateLimiter(limit=5, window_seconds=3600, lockout_seconds=3600)
verify_limiter = RateLimiter(limit=10, window_seconds=600, lockout_seconds=600)


def client_key(request: Request, extra: str = "") -> str:
    """
    Identify the caller. Prefers the real client IP (only trusted when
    TRUST_PROXY_HEADERS is on, so a client cannot spoof X-Forwarded-For).
    """
    from app.core.config import TRUST_PROXY_HEADERS

    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            ip = forwarded.split(",")[0].strip()
        else:
            ip = request.client.host if request.client else "unknown"
    else:
        ip = request.client.host if request.client else "unknown"
    return f"{ip}:{extra}" if extra else ip
