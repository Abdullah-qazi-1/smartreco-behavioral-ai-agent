"""
Simple in-memory rate limiter for public API endpoints.
"""
import os
import time
from collections import defaultdict
from typing import Dict, Tuple

from fastapi import Request
from starlette.responses import JSONResponse

# path prefix -> (max_requests, window_seconds)
RATE_LIMITS: Dict[str, Tuple[int, int]] = {
    "/api/events": (120, 60),
    "/api/track": (120, 60),
    "/api/recommendations": (60, 60),
    "/api/recommendations/refresh": (10, 60),
    "/api/ai/refresh": (10, 60),
    # Auth endpoints previously had NO throttling at all, which allowed
    # unlimited password-guessing against /login and mass account creation
    # / email-enumeration against /signup. Limits kept generous enough that
    # a real user retrying a mistyped password a few times is never affected.
    "/login": (10, 60),
    "/signup": (5, 60),
}

_buckets: Dict[str, list] = defaultdict(list)

# By default we only trust the direct socket peer address (request.client.host),
# because X-Forwarded-For is a plain client-supplied header: anyone can send a
# fresh fake value on every request and fully bypass IP-based rate limiting.
# Only trust X-Forwarded-For if this app is actually deployed behind a proxy
# that sets/overwrites it (nginx, Cloudflare, etc.) — set TRUST_PROXY_HEADERS=true
# in that environment's .env.
_TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "false").strip().lower() == "true"


def _client_key(request: Request) -> str:
    if _TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def check_rate_limit(request: Request) -> JSONResponse | None:
    path = request.url.path
    rule = None
    for prefix, cfg in RATE_LIMITS.items():
        if path == prefix or path.startswith(prefix + "/"):
            rule = cfg
            break
    if not rule:
        return None

    max_requests, window = rule
    now = time.time()
    key = f"{_client_key(request)}:{path}"
    timestamps = _buckets[key]
    _buckets[key] = [ts for ts in timestamps if now - ts < window]

    if len(_buckets[key]) >= max_requests:
        return JSONResponse(
            {"error": "rate_limit_exceeded", "retry_after_seconds": window},
            status_code=429,
        )

    _buckets[key].append(now)
    return None