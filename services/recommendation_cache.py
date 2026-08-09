"""
Short-lived in-process cache for GET /api/recommendations responses.
Invalidates when the user's latest recommendation row changes.
"""
import time
from typing import Any, Dict, Optional, Tuple

_cache: Dict[int, Tuple[float, str, dict]] = {}
CACHE_TTL_SECONDS = 30


def get_cached(user_id: int, rec_fingerprint: Optional[str]) -> Optional[dict]:
    if not rec_fingerprint:
        return None
    entry = _cache.get(user_id)
    if not entry:
        return None
    cached_at, fingerprint, payload = entry
    if fingerprint != rec_fingerprint:
        return None
    if time.time() - cached_at > CACHE_TTL_SECONDS:
        _cache.pop(user_id, None)
        return None
    return payload


def set_cached(user_id: int, rec_fingerprint: Optional[str], payload: dict) -> None:
    if not rec_fingerprint:
        return
    _cache[user_id] = (time.time(), rec_fingerprint, payload)


def invalidate_user(user_id: int) -> None:
    _cache.pop(user_id, None)
