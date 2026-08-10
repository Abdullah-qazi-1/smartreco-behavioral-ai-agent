"""
Short-lived in-process cache for GET /api/recommendations responses.
Invalidates when the user's latest recommendation row changes.
"""
import time
from typing import Any, Dict, Optional, Tuple

_cache: Dict[int, Tuple[float, str, dict]] = {}
# 10 minutes. This is a per-user, fingerprint-scoped cache — get_cached() already
# invalidates immediately (regardless of this TTL) whenever the user's underlying
# Recommendation row changes, since rec_fingerprint stops matching. So this TTL is
# NOT what determines freshness of the recommendation itself; it only bounds how
# long we keep serving the *same already-generated* payload for repeat GET
# /api/recommendations calls when nothing new has actually happened (e.g. a user
# re-opening the dashboard tab, or a client re-fetch on focus). 30s was too short
# for that job — it just forced the exact same JSON payload to be
# re-serialized/re-sent on almost every request even when the fingerprint hadn't
# moved. 10 minutes matches the trigger cooldown class of magnitude
# (MIN_SECONDS_BETWEEN_RUNS in services/trigger.py) instead of being an order of
# magnitude shorter than it for no reason, while still being far short of stale
# in practice, since a fingerprint change evicts it instantly anyway.
CACHE_TTL_SECONDS = 600


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