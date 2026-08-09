## services/agent.py

"""
Level 4.3 — Recommendation Agent orchestrator.

Generates TWO separate narrative blocks per recommendation cycle
(instead of one blended paragraph):
  - "search_intent" — a short, standalone note about a recent
    cross-field search (e.g. an ML person searching "aws"), shown
    FIRST on the dashboard because it reflects the most recent,
    specific signal.
  - "main" — the broader, ongoing dominant-category recommendation,
    shown SECOND.

Recommendation.narrative stores a JSON string:
    {"main": {"narrative": str, "product_ids": [int, ...]},
     "search_intent": {"query": str, "category": str, "narrative": str,
                        "product_ids": [int, ...]}}   # search_intent key
                                                          omitted if no branch

This is still a single should_regenerate-gated trigger — generating two
narratives means two LLM calls per trigger (not per event), so the
"AI call sirf trigger pe" principle still holds.
"""
import json
import logging
from typing import List, Optional

from sqlalchemy.orm import Session

from database.models import Product, Recommendation, Review, User, Event
from services.trigger import should_regenerate
from services.retrieval import get_recommendation_candidates
from services.llm_client import generate_narrative
from services.tracking_prefs import is_agent_tracking_enabled

logger = logging.getLogger("smartreco.agent")

REVIEWS_PER_PRODUCT = 3


from collections import defaultdict

def _batch_hydrate_products(db: Session, products: List[Product]) -> List[dict]:
    """
    Converts Product ORM rows into plain, JSON-safe dicts for the LLM
    prompt, using a single batch query for reviews to prevent N+1 DB calls.
    """
    if not products:
        return []

    product_ids = [p.id for p in products]
    all_reviews = (
        db.query(Review)
        .filter(Review.product_id.in_(product_ids))
        .order_by(Review.created_at.desc())
        .all()
    )

    reviews_by_product: dict = defaultdict(list)
    for r in all_reviews:
        if r.comment and len(reviews_by_product[r.product_id]) < REVIEWS_PER_PRODUCT:
            reviews_by_product[r.product_id].append({"rating": r.rating, "comment": r.comment})

    hydrated = []
    for p in products:
        data = p.to_dict()
        data["recent_reviews"] = reviews_by_product.get(p.id, [])
        hydrated.append(data)

    return hydrated


def _dedupe_products(products: List[Product]) -> List[Product]:
    seen_ids = set()
    ordered: List[Product] = []
    for p in products:
        if p.id not in seen_ids:
            seen_ids.add(p.id)
            ordered.append(p)
    return ordered


def _build_main_narrative_context(candidates: dict) -> str:
    category = candidates["dominant_category"]

    if candidates.get("cold_start"):
        return (
            "This learner has no activity signal yet (brand new or hasn't "
            "browsed enough). Write a warm, welcoming paragraph recommending "
            "these popular, highly-rated courses as a great starting point, "
            "without pretending to know their specific interests."
        )

    lines = [f"The learner has shown strong, genuine interest in: {category}."]

    if candidates["instructor_mode"]:
        lines.append(
            f"They recently searched for an instructor named "
            f"'{candidates['instructor_name']}'. Show their courses first, "
            f"then mention 1-2 highly-rated alternatives from other "
            f"instructors in the same category as options worth considering."
        )

    return " ".join(lines)


def _build_search_intent_narrative_context(candidates: dict) -> str:
    """
    Written as a STANDALONE note (not part of the main paragraph) —
    the frontend renders this in its own separate block, above the
    main recommendation. Includes a lightweight skill-level cue: a
    direct, specific search into a brand-new field (not phrased like
    "basics"/"beginner"/"intro") suggests the learner isn't a total
    novice overall, even though this particular field is new to them —
    so the tone shouldn't be condescending or over-explain fundamentals.
    """
    branch = candidates["search_intent_branch"]
    category = candidates["dominant_category"]

    return (
        f"The learner's main field is {category}, but today they explicitly "
        f"searched for '{branch['search_query']}', which relates to "
        f"{branch['inferred_category']} — a different field. Write ONE short, "
        f"standalone paragraph (2-4 sentences) that: (1) explicitly opens by "
        f"acknowledging this specific search (e.g. 'we noticed you searched "
        f"for X today'), (2) explains how this course could help them apply "
        f"their {category} background to this new area, (3) mentions the "
        f"course's rating as a reason it's worth considering. IMPORTANT: this "
        f"learner searched directly for a specific, non-beginner-phrased topic "
        f"without first exploring introductory content in this new field — "
        f"that's a signal they already have general technical experience (from "
        f"their main field) and don't need a hand-holding, pure-beginner tone, "
        f"even though the subject itself is new to them."
    )


def generate_and_save_recommendation(db: Session, user: User, force: bool = False) -> Optional[Recommendation]:
    """
    Runs the full recommendation pipeline for one user using the LangGraph StateGraph workflow,
    saving a new Recommendation row if trigger threshold or force conditions are met.
    """
    from services.agent_graph import run_recommendation_pipeline
    return run_recommendation_pipeline(db, user, force=force)

