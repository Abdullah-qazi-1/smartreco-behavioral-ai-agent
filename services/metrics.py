"""
services/metrics.py — Lightweight in-memory operational metrics collector for SmartReco.

Tracks LLM calls (total, today, success/failure) and recommendation trigger evaluations (fired vs skipped).
Thread-safe using Python's standard threading.Lock.
"""
import logging
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Any, List

logger = logging.getLogger("smartreco.metrics")

_lock = Lock()

_llm_calls_total = 0
_llm_calls_success = 0
_llm_calls_failed = 0
_llm_call_timestamps: List[datetime] = []

_trigger_fired_total = 0
_trigger_skipped_total = 0


def record_llm_call(provider: str, model: str, success: bool, duration_ms: float = 0.0) -> None:
    """Record an LLM call event."""
    global _llm_calls_total, _llm_calls_success, _llm_calls_failed
    now = datetime.now(timezone.utc)
    with _lock:
        _llm_calls_total += 1
        if success:
            _llm_calls_success += 1
        else:
            _llm_calls_failed += 1
        _llm_call_timestamps.append(now)

    logger.info(
        "Recorded LLM call: provider=%s model=%s success=%s duration_ms=%.1f (total_calls=%d)",
        provider, model, success, duration_ms, _llm_calls_total
    )


def record_trigger_evaluation(user_id: int, fired: bool, reason: str = "") -> None:
    """Record a trigger evaluation event (fired vs skipped)."""
    global _trigger_fired_total, _trigger_skipped_total
    with _lock:
        if fired:
            _trigger_fired_total += 1
        else:
            _trigger_skipped_total += 1

    total = _trigger_fired_total + _trigger_skipped_total
    logger.info(
        "Trigger evaluated: user_id=%s fired=%s reason=%s (fired=%d, skipped=%d, total=%d)",
        user_id, fired, reason, _trigger_fired_total, _trigger_skipped_total, total
    )


def get_llm_metrics() -> Dict[str, Any]:
    """Returns aggregated LLM metrics (total calls, calls today, success rate)."""
    now = datetime.now(timezone.utc)
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)

    with _lock:
        calls_today = sum(1 for ts in _llm_call_timestamps if ts >= today_start)
        total = _llm_calls_total
        success = _llm_calls_success
        failed = _llm_calls_failed

    return {
        "total_calls": total,
        "calls_today": calls_today,
        "successful_calls": success,
        "failed_calls": failed,
    }


def get_trigger_metrics() -> Dict[str, Any]:
    """Returns aggregated trigger evaluation metrics (fired, skipped, fire rate)."""
    with _lock:
        fired = _trigger_fired_total
        skipped = _trigger_skipped_total
        total = fired + skipped

    fire_rate = round(fired / total, 4) if total > 0 else 0.0

    return {
        "total_evaluations": total,
        "fired": fired,
        "skipped": skipped,
        "fire_rate": fire_rate,
    }
