## services/trigger.py

"""
Level 4.3 — Trigger logic for regenerating recommendations.

Decides WHEN the recommendation agent should run (not what it should
return — that's services/agent.py's job). Kept deliberately simple and
cheap: a plain event count comparison, no LLM call involved here, so
it's safe to call on every /api/events request without cost concern.

Design reference: CLAUDE.md Level 4.3 — "Trigger logic — should_regenerate
(user_id), threshold-based (default plan: 5 naye genuine/agent_eligible
events since last recommendation)". Confirmed with project owner: 5.
"""
import os
import logging
from datetime import datetime, timezone
from typing import Optional
from services.scoring_weights import NEW_EVENTS_TRIGGER_THRESHOLD
from services.metrics import record_trigger_evaluation
from sqlalchemy.orm import Session

from database.models import Event, Recommendation, User

logger = logging.getLogger("smartreco.trigger")


# Minimum cooldown in seconds between recommendation runs (default 120s)
MIN_SECONDS_BETWEEN_RUNS = int(os.getenv("MIN_SECONDS_BETWEEN_RUNS", "120"))

# Event types that count as "signal" toward the trigger threshold.
TRIGGER_SIGNAL_EVENT_TYPES = {"view", "search", "click", "time_spent", "dismiss", "enroll"}


def _last_recommendation(db: Session, user_id: int) -> Optional[Recommendation]:
    return (
        db.query(Recommendation)
        .filter(Recommendation.user_id == user_id)
        .order_by(Recommendation.created_at.desc())
        .first()
    )


def count_new_signal_events(db: Session, user_id: int, since: Optional[datetime] = None) -> int:
    """
    Counts agent_eligible, signal-carrying events for this user created
    after `since` (or all-time if `since` is None — i.e. no recommendation
    has ever been generated yet). Excludes generic non-product page views.
    """
    query = db.query(Event).filter(
        Event.user_id == user_id,
        Event.agent_eligible == True,  # noqa: E712
        Event.event_type.in_(TRIGGER_SIGNAL_EVENT_TYPES),
    )
    # Exclude non-product views/time_spent (e.g. dashboard refreshes) from trigger count
    query = query.filter(
        ~((Event.event_type.in_(["view", "time_spent"])) & (Event.product_id.is_(None)))
    )
    if since is not None:
        query = query.filter(Event.created_at > since)
    return query.count()


def should_regenerate(db: Session, user: User, force: bool = False) -> bool:
    """
    True if enough new activity has happened since the user's last
    recommendation (or since ever, if they've never had one) to justify
    an LLM call, and MIN_SECONDS_BETWEEN_RUNS cooldown has elapsed.
    Bypassed if force=True.
    """
    if user.role == "admin":
        record_trigger_evaluation(user.id, fired=False, reason="admin_user")
        logger.info("Trigger evaluated for user_id=%s: fired=False (admin role)", user.id)
        return False

    if force:
        record_trigger_evaluation(user.id, fired=True, reason="force_override")
        logger.info("Trigger evaluated for user_id=%s: fired=True (force=True override)", user.id)
        return True

    last_rec = _last_recommendation(db, user.id)

    # Cooldown rate limiting check
    if not force and last_rec and last_rec.created_at:
        last_time = last_rec.created_at
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - last_time).total_seconds()
        if elapsed < MIN_SECONDS_BETWEEN_RUNS:
            record_trigger_evaluation(user.id, fired=False, reason="rate_limited")
            logger.info(
                "Trigger evaluated for user_id=%s: fired=False (rate_limited: elapsed %.1fs < %ds)",
                user.id, elapsed, MIN_SECONDS_BETWEEN_RUNS
            )
            return False

    since = last_rec.created_at if last_rec else None

    new_count = count_new_signal_events(db, user.id, since=since)
    fired = new_count >= NEW_EVENTS_TRIGGER_THRESHOLD

    record_trigger_evaluation(user.id, fired=fired, reason=f"new_events={new_count}/{NEW_EVENTS_TRIGGER_THRESHOLD}")
    logger.info(
        "Trigger evaluated for user_id=%s: fired=%s new_events=%d threshold=%d",
        user.id, fired, new_count, NEW_EVENTS_TRIGGER_THRESHOLD
    )
    return fired
