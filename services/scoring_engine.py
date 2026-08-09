"""
Spec-aligned scoring engine for SmartReco recommendations.

Used by get_recommendation_candidates() for category selection, similarity
filtering, owned-product exclusion, and recommendation rotation (diversify).
"""
from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from sqlalchemy.orm import Session

from database.models import Event, Product, Recommendation, Review, User
from services.category_taxonomy import (
    ONBOARDING_TO_PRODUCT_CATEGORY_WEIGHTS,
    infer_category_from_query,
    get_related_categories,
)
from services.scoring_weights import (
    CATEGORY_DOMINANCE_RATIO,
    DECAY_HALF_LIFE_DAYS,
    DISMISS_WEIGHT,
    EVENT_BASE_WEIGHTS,
    EXPLICIT_INTEREST_BOOST,
    EXPLICIT_INTEREST_WEIGHT,
    EVENT_LOOKBACK_DAYS,
    LOOKBACK_DAYS,
    MIN_CATEGORY_SCORE_FOR_TAG,
    MIN_EVENTS_FOR_PERSONALIZATION,
    REVIEW_NEGATIVE_RATING_CUTOFF,
    REVIEW_NEGATIVE_WEIGHT,
    REVIEW_POSITIVE_RATING_CUTOFF,
    REVIEW_POSITIVE_WEIGHT,
    SIMILARITY_THRESHOLD,
)

# Event types that use dwell-time multiplier (spec: product_view)
VIEW_EVENT_TYPES = frozenset({"view", "time_spent", "product_view"})


def recency_weight(days_ago: float, half_life: float = DECAY_HALF_LIFE_DAYS) -> float:
    """Exponential recency decay: half_life-day half-life."""
    return math.pow(0.5, max(days_ago, 0.0) / half_life)


def time_multiplier(seconds_spent: Optional[float]) -> float:
    """Dwell-time multiplier for view / product_view events (spec tiers)."""
    if seconds_spent is None:
        return 1.0
    if seconds_spent < 5:
        return 0.2
    if seconds_spent < 30:
        return 1.0
    if seconds_spent < 120:
        return 1.5
    return 2.0


def frequency_boost(count: int) -> float:
    """Log-based dampening boost for repeated category events."""
    return 1 + math.log(count + 1, 2)


def remove_bot_noise(
    events: Sequence[Dict[str, Any]],
    min_gap_seconds: float = 0.3,
) -> List[Dict[str, Any]]:
    """Drop events that fire faster than min_gap_seconds after the previous one."""
    if not events:
        return []

    sorted_events = sorted(events, key=lambda e: e["timestamp"])
    cleaned: List[Dict[str, Any]] = []
    last_ts: Optional[datetime] = None

    for event in sorted_events:
        ts = event["timestamp"]
        if last_ts is not None and (ts - last_ts).total_seconds() < min_gap_seconds:
            continue
        cleaned.append(event)
        last_ts = ts

    return cleaned


def event_score(event: Dict[str, Any]) -> float:
    """Per-event score: base weight × recency × dwell multiplier."""
    base = EVENT_BASE_WEIGHTS.get(event["type"], 0.5)
    recency = recency_weight(event["days_ago"])
    mult = (
        time_multiplier(event.get("seconds_spent"))
        if event["type"] in VIEW_EVENT_TYPES
        else 1.0
    )
    return base * recency * mult


def compute_category_scores(
    events: Sequence[Dict[str, Any]],
    explicit_interests: Optional[Sequence[str]] = None,
) -> Dict[str, float]:
    """
    Aggregate per-category scores with frequency_boost and explicit-interest boost.
    Skips bounce/dismiss from positive category aggregation (negative handled via weight).
    """
    explicit_set = set(explicit_interests or [])
    raw_scores: Dict[str, float] = {}
    counts: Dict[str, int] = {}

    for event in events:
        if event["type"] in ("bounce",):
            continue
        category = event.get("category")
        if not category:
            continue

        score = event_score(event)
        if event["type"] == "dismiss":
            # Negative signal — still affects category score via negative weight
            pass

        raw_scores[category] = raw_scores.get(category, 0.0) + score
        counts[category] = counts.get(category, 0) + 1

    final_scores: Dict[str, float] = {}
    for category, score in raw_scores.items():
        score *= frequency_boost(counts[category])
        if category in explicit_set:
            score *= EXPLICIT_INTEREST_BOOST
        final_scores[category] = score

    return final_scores


def _decay_factor(event_time: datetime, now: datetime) -> float:
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    days_elapsed = max((now - event_time).total_seconds() / 86400.0, 0.0)
    return max(recency_weight(days_elapsed), 0.01)


def _dwell_seconds_for_event(event: Event) -> Optional[float]:
    if not event.event_metadata:
        return None
    try:
        meta = json.loads(event.event_metadata)
    except (TypeError, ValueError):
        return None
    seconds = meta.get("seconds")
    return float(seconds) if isinstance(seconds, (int, float)) else None


def _search_query_for_event(event: Event) -> Optional[str]:
    if not event.event_metadata:
        return None
    try:
        meta = json.loads(event.event_metadata)
    except (TypeError, ValueError):
        return None
    return meta.get("query")


def _is_search_aligned(
    product_events: List[Event],
    search_events: List[Event],
    product_category: str,
) -> bool:
    if not search_events:
        return False

    product_times = [e.created_at for e in product_events if e.created_at]
    if not product_times:
        return False

    window = timedelta(minutes=30)

    for search_event in search_events:
        if not search_event.created_at:
            continue
        query = _search_query_for_event(search_event)
        if not query:
            continue

        close_enough = any(
            abs((search_event.created_at - pt).total_seconds()) <= window.total_seconds()
            for pt in product_times
        )
        if not close_enough:
            continue

        inferred_category = infer_category_from_query(query)
        if not inferred_category:
            continue

        if get_related_categories(inferred_category).get(product_category, 0.0) >= 0.5:
            return True

    return False


def _confidence_weight_for_product(
    product_events: List[Event],
    search_events: List[Event],
    product_category: str,
) -> float:
    distinct_days = {e.created_at.date() for e in product_events if e.created_at}
    if len(distinct_days) >= 2:
        return 1.0

    time_spent_events = [e for e in product_events if e.event_type == "time_spent"]
    best_seconds = None
    if time_spent_events:
        best_seconds = max(
            (s for s in (_dwell_seconds_for_event(e) for e in time_spent_events) if s is not None),
            default=None,
        )

    if best_seconds is not None:
        dwell_weight = time_multiplier(best_seconds)
    else:
        dwell_weight = 0.4 * time_multiplier(None)

    if _is_search_aligned(product_events, search_events, product_category):
        return max(dwell_weight, 0.65)

    return dwell_weight


def _spread_related(raw_scores: Dict[str, float]) -> Dict[str, float]:
    spread: Dict[str, float] = defaultdict(float)
    for category, score in raw_scores.items():
        spread[category] += score
        for other_category, relatedness in get_related_categories(category).items():
            spread[other_category] += score * relatedness * 0.35
    return dict(spread)


def _apply_review_signals(db: Session, user: User, raw_scores: Dict[str, float], now: datetime) -> None:
    reviews = (
        db.query(Review.rating, Review.created_at, Product.category)
        .join(Product, Review.product_id == Product.id)
        .filter(Review.user_id == user.id)
        .all()
    )

    for rating, created_at, category in reviews:
        if rating is None or category is None:
            continue

        decay = _decay_factor(created_at, now) if created_at else 1.0

        if rating >= REVIEW_POSITIVE_RATING_CUTOFF:
            raw_scores[category] += REVIEW_POSITIVE_WEIGHT * decay
        elif rating <= REVIEW_NEGATIVE_RATING_CUTOFF:
            raw_scores[category] += REVIEW_NEGATIVE_WEIGHT * decay


def _apply_dismiss_signals(db: Session, user: User, raw_scores: Dict[str, float], now: datetime) -> None:
    dismiss_events = (
        db.query(Event.product_id, Event.created_at)
        .filter(
            Event.user_id == user.id,
            Event.agent_eligible == True,  # noqa: E712
            Event.event_type == "dismiss",
            Event.product_id.isnot(None),
        )
        .all()
    )
    if not dismiss_events:
        return

    product_ids = {pid for pid, _ in dismiss_events}
    products = (
        db.query(Product.id, Product.category)
        .filter(Product.id.in_(product_ids))
        .all()
    )
    category_by_product = {pid: category for pid, category in products}

    for product_id, created_at in dismiss_events:
        category = category_by_product.get(product_id)
        if not category:
            continue
        decay = _decay_factor(created_at, now) if created_at else 1.0
        raw_scores[category] += DISMISS_WEIGHT * decay


def _apply_onboarding_interest_signals(user: User, raw_scores: Dict[str, float], now: datetime) -> None:
    if not user.interests or not getattr(user, "interests_updated_at", None):
        return

    decay = _decay_factor(user.interests_updated_at, now)
    for onboarding_label in user.interests.split(","):
        onboarding_label = onboarding_label.strip()
        mapped = ONBOARDING_TO_PRODUCT_CATEGORY_WEIGHTS.get(onboarding_label, {})
        for product_category, weight in mapped.items():
            raw_scores[product_category] += EXPLICIT_INTEREST_WEIGHT * weight * decay


def build_category_profile(db: Session, user: User) -> Dict[str, float]:
    now = datetime.now(timezone.utc)
    raw_scores: Dict[str, float] = defaultdict(float)

    _apply_onboarding_interest_signals(user, raw_scores, now)

    lookback_cutoff = now - timedelta(days=EVENT_LOOKBACK_DAYS)
    all_events = (
        db.query(Event)
        .filter(
            Event.user_id == user.id,
            Event.agent_eligible == True,  # noqa: E712
            Event.created_at >= lookback_cutoff,
        )
        .all()
    )

    product_events_raw = [e for e in all_events if e.event_type in {"view", "time_spent", "click"} and e.product_id]
    search_events = [e for e in all_events if e.event_type == "search"]

    if product_events_raw:
        events_by_product: Dict[int, List[Event]] = defaultdict(list)
        for e in product_events_raw:
            events_by_product[e.product_id].append(e)

        products = (
            db.query(Product.id, Product.category)
            .filter(Product.id.in_(events_by_product.keys()))
            .all()
        )
        category_by_product = {pid: category for pid, category in products}

        for product_id, product_events in events_by_product.items():
            category = category_by_product.get(product_id)
            if not category:
                continue

            confidence_weight = _confidence_weight_for_product(product_events, search_events, category)
            if confidence_weight == 0:
                continue

            most_recent = max((e.created_at for e in product_events if e.created_at), default=now)
            decay = _decay_factor(most_recent, now)

            raw_scores[category] += confidence_weight * decay

    _apply_review_signals(db, user, raw_scores, now)
    _apply_dismiss_signals(db, user, raw_scores, now)

    return _spread_related(raw_scores)


def resolve_retrieval_categories(
    sorted_cats: List[Tuple[str, float]],
    dominance_ratio: float = CATEGORY_DOMINANCE_RATIO,
) -> List[str]:
    """
    If top category dominates (> ratio × second), retrieve from one category only.
    Otherwise blend the top two categories.
    """
    positive = [(c, s) for c, s in sorted_cats if s > 0]
    if not positive:
        return []

    if len(positive) == 1:
        return [positive[0][0]]

    if positive[0][1] > positive[1][1] * dominance_ratio:
        return [positive[0][0]]

    return [positive[0][0], positive[1][0]]


def filter_already_owned(
    candidates: Sequence[Product],
    enrolled_ids: set,
) -> List[Product]:
    """Exclude products the user is already enrolled in."""
    if not enrolled_ids:
        return list(candidates)
    return [c for c in candidates if c.id not in enrolled_ids]


def get_last_shown_product_ids(db: Session, user_id: int) -> List[int]:
    """Product IDs from the user's most recent recommendation row."""
    rec = (
        db.query(Recommendation)
        .filter(Recommendation.user_id == user_id, Recommendation.is_latest == True)  # noqa: E712
        .order_by(Recommendation.created_at.desc())
        .first()
    )
    if not rec or not rec.product_ids:
        return []
    try:
        ids = json.loads(rec.product_ids)
        return [int(i) for i in ids if i is not None]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def diversify(
    candidates: Sequence[Product],
    final_count: int = 3,
    user_id: Optional[int] = None,
    db: Optional[Session] = None,
) -> List[Product]:
    """
    Deprioritize products from the last recommendation; random-sample final_count
    from fresh pool, falling back to full pool when needed.
    """
    if not candidates:
        return []

    last_shown: List[int] = []
    if user_id is not None and db is not None:
        last_shown = get_last_shown_product_ids(db, user_id)

    pool = list(candidates[: final_count + 3])
    fresh = [c for c in pool if c.id not in last_shown]
    chosen_pool = fresh if len(fresh) >= final_count else pool

    sample_size = min(final_count, len(chosen_pool))
    if sample_size <= 0:
        return []
    return random.sample(chosen_pool, sample_size)


def explicit_product_categories(user: User) -> set:
    """Map onboarding interest labels to product category names for EXPLICIT_INTEREST_BOOST."""
    categories: set = set()
    if not user.interests:
        return categories
    for label in user.interests.split(","):
        label = label.strip()
        if not label:
            continue
        mapped = ONBOARDING_TO_PRODUCT_CATEGORY_WEIGHTS.get(label, {})
        categories.update(mapped.keys())
        categories.add(label)
    return categories


def _parse_metadata(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _category_for_event(
    event: Event,
    product_categories: Dict[int, str],
) -> Optional[str]:
    if event.product_id and event.product_id in product_categories:
        return product_categories[event.product_id]
    if event.event_type == "search":
        meta = _parse_metadata(event.event_metadata)
        query = meta.get("query")
        if isinstance(query, str) and query.strip():
            return infer_category_from_query(query)
    return None


def fetch_scoring_events(db: Session, user: User) -> List[Dict[str, Any]]:
    """Load agent-eligible events and normalize for scoring_engine functions."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)

    rows = (
        db.query(Event)
        .filter(
            Event.user_id == user.id,
            Event.agent_eligible == True,  # noqa: E712
            Event.created_at >= cutoff,
        )
        .order_by(Event.created_at.asc())
        .all()
    )

    product_ids = {e.product_id for e in rows if e.product_id}
    product_categories: Dict[int, str] = {}
    if product_ids:
        for pid, category in (
            db.query(Product.id, Product.category)
            .filter(Product.id.in_(product_ids))
            .all()
        ):
            if category:
                product_categories[pid] = category

    normalized: List[Dict[str, Any]] = []
    for event in rows:
        if not event.created_at:
            continue
        ts = event.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        days_ago = max((now - ts).total_seconds() / 86400.0, 0.0)
        meta = _parse_metadata(event.event_metadata)
        seconds_spent = meta.get("seconds")
        if isinstance(seconds_spent, (int, float)):
            seconds_spent = float(seconds_spent)
        else:
            seconds_spent = None

        category = _category_for_event(event, product_categories)
        normalized.append({
            "type": event.event_type,
            "category": category,
            "days_ago": days_ago,
            "seconds_spent": seconds_spent,
            "timestamp": ts,
            "product_id": event.product_id,
        })

    return normalized


def count_personalization_events(events: Sequence[Dict[str, Any]]) -> int:
    """Count events after bot filtering for cold-start gate."""
    return len(events)


def build_category_profile_for_retrieval(
    db: Session,
    user: User,
    pre_cleaned_events: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, float], List[Dict[str, Any]]]:
    """
    Retrieval/reasoning-path scoring: fetch → bot filter → category scores.
    Returns (category_scores, cleaned_events).

    Intentionally distinct from build_category_profile() above:
    - build_category_profile() is the richer profile used for catalog sort
      bias (routers/products.py via interest_profile.get_dominant_categories).
      It re-queries reviews and dismissals from the DB and applies
      confidence-weighting/search-alignment per product, because catalog
      sort runs once per page load and can afford the extra DB work.
    - build_category_profile_for_retrieval() (this function) is the fast
      path used by services/retrieval.py and services/reasoning.py. It
      scores directly off the already-fetched, already-bot-filtered event
      dicts (pre_cleaned_events) instead of re-querying the DB, since it
      runs inside the LangGraph recommendation pipeline where low latency
      matters and events/bot-filtering were already computed upstream in
      analyze_activity(). It does not separately re-pull review signals.

    If you need review-signal-aware scores in a retrieval/reasoning
    context, call build_category_profile(db, user) directly instead of
    adding a second copy of this function — there is now exactly one
    implementation per path.
    """
    if pre_cleaned_events is not None:
        cleaned = pre_cleaned_events
    else:
        raw = fetch_scoring_events(db, user)
        cleaned = remove_bot_noise(raw)
    explicit = explicit_product_categories(user)
    scores = compute_category_scores(cleaned, explicit_interests=explicit)
    return scores, cleaned


def passes_similarity_threshold(score: float) -> bool:
    return score >= SIMILARITY_THRESHOLD