"""
services/keyword_fallback.py — Non-AI fallback search.

Used only when the Mesh embedding service is unavailable (missing key, down,
timed out, rate-limited). Does NOT call any AI/LLM/embedding provider —
pure SQL keyword matching plus deterministic ranking logic:

  1. Query text is tokenized and matched against title/skills/category/
     description (SQL LIKE — no vectors involved).
  2. Ranked by: how many tokens matched in the title (best name match
     first) > total tokens matched anywhere > this user's prior time-spent
     on that specific product (so among equally-good name matches, the one
     they've actually engaged with wins) > rating > enrolled_students.

This keeps `/api/search`, the "related courses" widget, and the
recommendation agent's retrieval step working (with reduced precision)
during a Mesh outage, instead of raising.
"""
import json
import re
import logging
from collections import defaultdict
from typing import List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from database.models import Product, Event, User

logger = logging.getLogger("smartreco.keyword_fallback")

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _user_time_spent_by_product(db: Session, user_id: int) -> dict:
    """Sums seconds_spent (from time_spent event metadata) per product_id for this user."""
    rows = (
        db.query(Event.product_id, Event.event_metadata)
        .filter(
            Event.user_id == user_id,
            Event.event_type == "time_spent",
            Event.product_id.isnot(None),
        )
        .all()
    )
    totals: dict = defaultdict(float)
    for product_id, metadata in rows:
        if not metadata:
            continue
        try:
            meta = json.loads(metadata)
        except (TypeError, ValueError):
            continue
        seconds = meta.get("seconds")
        if isinstance(seconds, (int, float)):
            totals[product_id] += float(seconds)
    return dict(totals)


def keyword_search_products(
    db: Session,
    query: str,
    top_k: int = 8,
    category: Optional[str] = None,
    level: Optional[str] = None,
    user: Optional[User] = None,
) -> List[Product]:
    """
    Plain SQL keyword search — no embeddings, no AI call. Returns Product rows
    ranked by title-match strength, then (if a user is given) how much time
    that user has spent on the matched product, then rating/popularity.
    """
    tokens = _tokenize(query)
    if not tokens:
        return []

    q = db.query(Product).filter(Product.status == "active")
    if category and category != "All Categories":
        q = q.filter(Product.category == category)
    if level and level != "All Levels":
        q = q.filter(Product.level == level)

    like_clauses = []
    for token in tokens:
        pattern = f"%{token}%"
        like_clauses.append(Product.title.ilike(pattern))
        like_clauses.append(Product.skills.ilike(pattern))
        like_clauses.append(Product.category.ilike(pattern))
        like_clauses.append(Product.description.ilike(pattern))

    candidates = q.filter(or_(*like_clauses)).all()
    if not candidates:
        return []

    time_spent_by_product = (
        _user_time_spent_by_product(db, user.id) if user is not None else {}
    )

    def rank_key(product: Product):
        title_text = (product.title or "").lower()
        blob = f"{product.title} {product.skills or ''} {product.category} {product.description or ''}".lower()
        title_hits = sum(1 for t in tokens if t in title_text)
        total_hits = sum(1 for t in tokens if t in blob)
        time_spent = time_spent_by_product.get(product.id, 0.0)
        return (
            title_hits,
            total_hits,
            time_spent,
            product.rating or 0.0,
            product.enrolled_students or 0,
        )

    ranked = sorted(candidates, key=rank_key, reverse=True)
    return ranked[:top_k]