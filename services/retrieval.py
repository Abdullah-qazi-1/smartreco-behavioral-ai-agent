## services/retrieval.py

"""
Level 4.3 — Retrieval.

Given a user, produces a grounded, category-constrained (and, when the
user has set an onboarding experience_level, level-aware) set of
candidate products, following the pipeline in CLAUDE.md's
"Recommendation Agent — Design & Edge Cases" section plus the
search-intent branch and level-preference added in-conversation:

  1. Dominant category from interest_profile.py (single category —
     multi-interest clustering deliberately deferred).
  2. Category-constrained Chroma semantic search, PREFERRING the
     user's stated experience_level (Beginner/Intermediate/Advanced,
     set at onboarding) when available — a soft preference, not a hard
     filter: if too few level-matched results exist, remainder is
     filled from category-only (any-level) results, so results never
     come back empty just because of the level preference.
  3. Instructor-aware branch: if the user's most recent search matched
     a real instructor's name, ALSO fetch their own courses plus 1-2
     better-rated alternatives from a different instructor.
  4. Search-intent branch: if the most recent search points to a field
     DIFFERENT from the dominant category, treat it as an explicit
     cross-field signal — find a "bridge" course. If the user is
     Intermediate/Advanced overall, prefer a non-Beginner bridge course
     first (they likely don't need a from-scratch intro even in an
     unfamiliar field), falling back to any-level if none found.
  5. Already-viewed products excluded from primary results.

Returns raw SQLAlchemy Product rows — hydration + narrative generation
happen one layer up (services/agent.py).
"""
import json
import re
import time
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import desc

from database.models import Event, Product, User, Enrollment
from services import product_service

from services.scoring_engine import (
    build_category_profile_for_retrieval,
    count_personalization_events,
    diversify,
    filter_already_owned,
    remove_bot_noise,
    resolve_retrieval_categories,
    fetch_scoring_events,
)
from services.scoring_weights import MIN_EVENTS_FOR_PERSONALIZATION
from services.category_taxonomy import CATEGORY_TOPICS, related_weight, infer_category_from_query
from services.scoring_weights import (
    COLD_START_RESULTS_LIMIT,
    PRIMARY_RESULTS_LIMIT,
    ALTERNATIVE_RESULTS_LIMIT,
    RECENT_TITLES_FOR_QUERY,
    SEARCH_INTENT_RELATEDNESS_CEILING,
    BRIDGE_RESULTS_LIMIT,
)

VALID_LEVELS = {"Beginner", "Intermediate", "Advanced"}
BEGINNER_SIGNAL_WORDS = {"beginner", "basics", "fundamentals", "101", "intro", "introduction", "getting", "started"}

# In-memory cache for search-based recommendations (keyed by normalized query + user tags + limit).
# Avoids repeated Chroma calls for the same query within a server session/day.
_search_recommendation_cache: Dict[str, Tuple[float, dict]] = {}
SEARCH_REC_CACHE_TTL_SECONDS = 86400
SEMANTIC_CANDIDATE_POOL = 20


def _user_experience_level(user: User) -> Optional[str]:
    level = getattr(user, "experience_level", None)
    return level if level in VALID_LEVELS else None


def _recently_viewed_titles_in_category(db: Session, user: User, category: str) -> List[str]:
    events = (
        db.query(Event)
        .filter(
            Event.user_id == user.id,
            Event.agent_eligible == True,  # noqa: E712
            Event.product_id.isnot(None),
        )
        .order_by(Event.created_at.desc())
        .limit(50)
        .all()
    )

    seen_product_ids: List[int] = []
    for e in events:
        if e.product_id not in seen_product_ids:
            seen_product_ids.append(e.product_id)

    if not seen_product_ids:
        return []

    products = (
        db.query(Product.id, Product.title, Product.category)
        .filter(Product.id.in_(seen_product_ids))
        .all()
    )
    info_by_id = {pid: (title, cat) for pid, title, cat in products}

    titles = []
    for pid in seen_product_ids:
        entry = info_by_id.get(pid)
        if entry and entry[1] == category:
            titles.append(entry[0])
        if len(titles) >= RECENT_TITLES_FOR_QUERY:
            break

    return titles


def _viewed_product_ids(db: Session, user: User) -> set:
    rows = (
        db.query(Event.product_id)
        .filter(Event.user_id == user.id, Event.product_id.isnot(None))
        .distinct()
        .all()
    )
    return {r[0] for r in rows}
def _enrolled_product_ids(db: Session, user: User) -> set:
    rows = (
        db.query(Enrollment.product_id)
        .filter(Enrollment.user_id == user.id)
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def _build_category_query_text(category: str, recent_titles: List[str]) -> str:
    if recent_titles:
        return ". ".join(recent_titles)
    topics = CATEGORY_TOPICS.get(category, [])
    return f"{category}. " + ", ".join(topics[:8])


def _level_preferred_search(
    db: Session,
    query_text: str,
    category: str,
    preferred_level: Optional[str],
    top_k: int,
    hard_level_filter: bool = False,
    user: Optional[User] = None,
) -> Tuple[List[Product], float]:
    """
    Semantic search returning (products, max_similarity_score).
    Supports hard level filtering when level signal is strong, falling back to soft preference.
    """
    results: List[Product] = []
    seen_ids: set = set()
    max_score = 0.0

    if preferred_level:
        level_matched = product_service.semantic_search_products_scored(
            db, query_text, top_k=top_k, category=category, level=preferred_level, user=user
        )
        for product, score in level_matched:
            if score > max_score:
                max_score = score
            if product.id not in seen_ids:
                results.append(product)
                seen_ids.add(product.id)

    # If hard level filter requested and level_matched found items, do not top up from other levels
    if hard_level_filter and results:
        return results[:top_k], max_score

    if len(results) < top_k:
        fallback = product_service.semantic_search_products_scored(
            db, query_text, top_k=top_k * 2, category=category, user=user
        )
        for product, score in fallback:
            if score > max_score:
                max_score = score
            if product.id not in seen_ids:
                results.append(product)
                seen_ids.add(product.id)
            if len(results) >= top_k:
                break

    return results[:top_k], max_score


def get_recent_search_queries(db: Session, user_id: int, limit: int = 15) -> List[dict]:
    """
    Returns recent distinct search queries (newest first).
    - Globally dedupes identical queries (keeps newest occurrence).
    - Drops partial typing fragments when a longer query with the same prefix exists.
    """
    events = (
        db.query(Event)
        .filter(
            Event.user_id == user_id,
            Event.event_type == "search",
            Event.agent_eligible == True,  # noqa: E712
        )
        .order_by(Event.created_at.desc())
        .limit(limit * 6)
        .all()
    )

    history: List[dict] = []
    seen_normalized: set = set()

    for event in events:
        if not event.event_metadata:
            continue
        try:
            meta = json.loads(event.event_metadata)
        except (TypeError, ValueError):
            continue
        raw_query = meta.get("query")
        if not isinstance(raw_query, str):
            continue
        query = _clean_search_query(raw_query)
        if not query or len(query) < 2:
            continue
        normalized = query.lower()
        if normalized in seen_normalized:
            continue
        # Skip partial keystroke queries when a longer search with same prefix already kept.
        if any(
            h["query"].lower().startswith(normalized) and len(h["query"]) > len(query)
            for h in history
        ):
            continue
        ts = event.created_at.isoformat() if event.created_at else None
        history.append({"query": query, "timestamp": ts})
        seen_normalized.add(normalized)
        if len(history) >= limit:
            break

    return history


def get_last_search_query(db: Session, user_id: int) -> Optional[str]:
    """Returns the most recent agent-eligible search event query for a user, or None."""
    recent = get_recent_search_queries(db, user_id, limit=1)
    return recent[0]["query"] if recent else None


def _clean_search_query(query: str) -> str:
    """Light normalization that preserves domain qualifiers like 'for data science'."""
    return " ".join(query.strip().split())


def _normalize_cache_key(query: str, user: Optional[User], limit: int) -> str:
    tags = ",".join(sorted(_user_interest_tags(user)))
    return f"{_clean_search_query(query).lower()}|{tags}|{limit}|{user.id if user else 0}"


def _cache_get_search_result(key: str) -> Optional[dict]:
    entry = _search_recommendation_cache.get(key)
    if not entry:
        return None
    cached_at, payload = entry
    if time.time() - cached_at > SEARCH_REC_CACHE_TTL_SECONDS:
        _search_recommendation_cache.pop(key, None)
        return None
    return payload


def _cache_set_search_result(key: str, payload: dict) -> None:
    _search_recommendation_cache[key] = (time.time(), payload)


def _user_interest_tags(user: Optional[User]) -> List[str]:
    if not user or not user.interests:
        return []
    return [t.strip().lower() for t in user.interests.split(",") if t.strip()]


def _split_query_subject_qualifier(query: str) -> Tuple[str, Optional[str]]:
    parts = re.split(r"\s+for\s+", query, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return query, None


def _product_profile_overlap_score(
    product: Product, tags: List[str], query: str, qualifier: Optional[str] = None
) -> float:
    """Boost courses whose metadata overlaps subject, qualifier, and interest tags."""
    text = f"{product.title} {product.description} {product.skills or ''} {product.category}".lower()
    score = 0.0

    subject, parsed_qualifier = _split_query_subject_qualifier(query)
    qualifier = qualifier or parsed_qualifier

    # Subject keywords (e.g. "aws", "web development") must dominate ranking.
    for word in subject.lower().split():
        if len(word) > 2 and word in text:
            score += 2.0

    if qualifier:
        if qualifier.lower() in text:
            score += 2.0
        for word in qualifier.lower().split():
            if len(word) > 2 and word in text:
                score += 0.5

    for tag in tags:
        if tag in text:
            score += 0.8

    # Penalize obvious category mismatch vs inferred subject category.
    subject_category = infer_category_from_query(subject)
    if subject_category and product.category and product.category != subject_category:
        score -= 1.5

    return score


def _rerank_search_products(
    products: List[Product], query: str, tags: List[str], qualifier: Optional[str] = None
) -> List[Product]:
    if not products:
        return []
    scored = [
        (_product_profile_overlap_score(p, tags, query, qualifier), -idx, p)
        for idx, p in enumerate(products)
    ]
    scored.sort(reverse=True)
    return [p for _, _, p in scored]


def _semantic_candidates_for_query(
    db: Session, query: str, limit: int
) -> Tuple[List[Product], Optional[str]]:
    """Category-constrained semantic search using the subject before 'for', if present."""
    subject, qualifier = _split_query_subject_qualifier(query)
    primary_category = infer_category_from_query(subject) or infer_category_from_query(query)

    candidates: List[Product] = []
    if primary_category:
        candidates = product_service.semantic_search_products(
            db, query, top_k=SEMANTIC_CANDIDATE_POOL, category=primary_category
        )

    if len(candidates) < limit:
        existing_ids = {p.id for p in candidates}
        fallback = product_service.semantic_search_products(
            db, query, top_k=SEMANTIC_CANDIDATE_POOL
        )
        for p in fallback:
            if p.id not in existing_ids:
                candidates.append(p)
                existing_ids.add(p.id)

    return candidates, qualifier


def _query_without_instructor(query: str, instructor_name: str) -> str:
    cleaned = query
    patterns = [
        rf"\s+by\s+{re.escape(instructor_name)}\s*$",
        rf"\s+{re.escape(instructor_name)}\s*$",
    ]
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    return _clean_search_query(cleaned)


def _instructor_field_matches(
    db: Session, instructor_name: str, field_query: str, tags: List[str], limit: int
) -> List[Product]:
    """Semantic match limited to courses by a specific instructor."""
    search_text = field_query or instructor_name
    subject, qualifier = _split_query_subject_qualifier(search_text)
    primary_category = infer_category_from_query(subject) or infer_category_from_query(search_text)

    candidates = product_service.semantic_search_products(
        db, search_text, top_k=SEMANTIC_CANDIDATE_POOL, category=primary_category
    ) if primary_category else product_service.semantic_search_products(
        db, search_text, top_k=SEMANTIC_CANDIDATE_POOL
    )
    instructor_products = [p for p in candidates if (p.instructor_name or "").strip() == instructor_name.strip()]
    if not instructor_products:
        instructor_products = (
            db.query(Product)
            .filter(Product.instructor_name == instructor_name)
            .order_by(desc(Product.rating), desc(Product.enrolled_students))
            .limit(SEMANTIC_CANDIDATE_POOL)
            .all()
        )
    reranked = _rerank_search_products(instructor_products, search_text, tags, qualifier)
    return reranked[:limit]


def _instructor_best_overall(db: Session, instructor_name: str, limit: int = 3) -> List[Product]:
    return (
        db.query(Product)
        .filter(Product.instructor_name == instructor_name)
        .order_by(
            Product.rating.desc().nullslast(),
            Product.enrolled_students.desc().nullslast(),
            Product.num_ratings.desc().nullslast(),
        )
        .limit(limit)
        .all()
    )


def get_search_based_recommendations(
    db: Session,
    query: str,
    user: Optional[User] = None,
    limit: int = 3,
) -> dict:
    """
    Returns search-matched courses for a single query string.
    Payload: {products, reason_type, reason_message, instructor_name}
    """
    cleaned = _clean_search_query(query)
    if not cleaned:
        return {"products": [], "reason_type": "search", "reason_message": None, "instructor_name": None}

    cache_key = _normalize_cache_key(cleaned, user, limit)
    cached = _cache_get_search_result(cache_key)
    if cached is not None:
        return cached

    tags = _user_interest_tags(user)
    instructor = _match_instructor(db, cleaned)

    if instructor:
        field_query = _query_without_instructor(cleaned, instructor)
        field_matches = _instructor_field_matches(db, instructor, field_query, tags, limit)
        if field_matches:
            payload = {
                "products": [p.to_dict() for p in field_matches],
                "reason_type": "instructor_match",
                "reason_message": None,
                "instructor_name": instructor,
            }
            _cache_set_search_result(cache_key, payload)
            return payload

        fallback_courses = _instructor_best_overall(db, instructor, limit=min(limit, 3))
        payload = {
            "products": [p.to_dict() for p in fallback_courses],
            "reason_type": "instructor_fallback",
            "reason_message": (
                f"We don't have a course on this exact topic from {instructor}, but here are "
                f"some of their most-loved courses — highly rated and taken by many learners "
                f"in related fields."
            ),
            "instructor_name": instructor,
        }
        _cache_set_search_result(cache_key, payload)
        return payload

    # Full raw query embedded in Chroma — subject used for category filter, qualifier for re-rank boost.
    candidates, qualifier = _semantic_candidates_for_query(db, cleaned, limit)
    reranked = _rerank_search_products(candidates, cleaned, tags, qualifier)
    payload = {
        "products": [p.to_dict() for p in reranked[:limit]],
        "reason_type": "search",
        "reason_message": None,
        "instructor_name": None,
    }
    _cache_set_search_result(cache_key, payload)
    return payload


def build_search_history(db: Session, user: User, limit: int = 10) -> dict:
    """
    Builds AI Insights search context:
      - latest: most recent search → up to 3 courses (main content area)
      - sidebar: older searches → 1 top course each (scrollable sidebar, max limit-1)
    """
    queries = get_recent_search_queries(db, user.id, limit=limit)
    latest: Optional[dict] = None
    sidebar: List[dict] = []

    if not queries:
        return {"latest": None, "sidebar": []}

    latest_result = get_search_based_recommendations(
        db, queries[0]["query"], user=user, limit=3
    )
    if latest_result["products"]:
        latest = {
            "query": queries[0]["query"],
            "timestamp": queries[0]["timestamp"],
            "products": latest_result["products"][:3],
            "reason_type": latest_result["reason_type"],
            "reason_message": latest_result.get("reason_message"),
            "instructor_name": latest_result.get("instructor_name"),
        }

    for entry in queries[1:limit]:
        result = get_search_based_recommendations(
            db, entry["query"], user=user, limit=1
        )
        if not result["products"]:
            continue
        sidebar.append({
            "query": entry["query"],
            "timestamp": entry["timestamp"],
            "product": result["products"][0],
            "reason_type": result["reason_type"],
            "reason_message": result.get("reason_message"),
            "instructor_name": result.get("instructor_name"),
        })

    return {"latest": latest, "sidebar": sidebar}


def build_profile_search_narrative(user: User, recent_search: Optional[str]) -> str:
    """Connects interest profile with the user's most recent search for the Why panel."""
    if user.interests:
        interest_labels = ", ".join(t.strip() for t in user.interests.split(",") if t.strip())
    else:
        interest_labels = "your learning goals"

    if recent_search:
        return (
            f"Since you're focused on {interest_labels} and recently searched "
            f"'{recent_search}', we're prioritizing courses that connect your profile "
            f"with that search — including cloud and cross-domain options that overlap "
            f"with your data-science interests, not just generic keyword matches."
        )

    return (
        f"Based on your interest profile ({interest_labels}) and recent browsing activity, "
        f"our recommendation engine has identified courses aligned with your stated goals "
        f"and behavioral signals."
    )


def _most_recent_search_query(db: Session, user: User) -> Optional[str]:
    return get_last_search_query(db, user.id)

def _match_instructor(db: Session, search_query: Optional[str]) -> Optional[str]:
    if not search_query:
        return None
    query_lower = search_query.strip().lower()
    if not query_lower:
        return None

    instructor_rows = (
        db.query(Product.instructor_name)
        .filter(Product.instructor_name.isnot(None), Product.instructor_name != "")
        .distinct()
        .all()
    )

    best_match: Optional[str] = None
    best_len = 0
    for (instructor_name,) in instructor_rows:
        if not instructor_name:
            continue
        name_lower = instructor_name.strip().lower()
        if name_lower in query_lower and len(name_lower) > best_len:
            best_match = instructor_name
            best_len = len(name_lower)

    return best_match

def _instructor_branch(db: Session, instructor_name: str) -> Dict[str, List[Product]]:
    own_courses = (
        db.query(Product)
        .filter(Product.instructor_name == instructor_name)
        .order_by(Product.rating.desc().nullslast())
        .limit(PRIMARY_RESULTS_LIMIT)
        .all()
    )

    if not own_courses:
        return {"own_courses": [], "alternatives": []}

    categories = [c.category for c in own_courses if c.category]
    target_category = max(set(categories), key=categories.count) if categories else None

    alternatives: List[Product] = []
    if target_category:
        alternatives = (
            db.query(Product)
            .filter(
                Product.category == target_category,
                Product.instructor_name != instructor_name,
            )
            .order_by(
                Product.rating.desc().nullslast(),
                Product.num_ratings.desc().nullslast(),
                Product.enrolled_students.desc().nullslast(),
            )
            .limit(ALTERNATIVE_RESULTS_LIMIT)
            .all()
        )

    return {"own_courses": own_courses, "alternatives": alternatives}


def _search_intent_branch(db: Session, dominant_category: str, search_query: str, user_level: Optional[str]) -> Optional[Dict]:
    inferred_category = infer_category_from_query(search_query)
    if not inferred_category or inferred_category == dominant_category:
        return None

    relatedness = related_weight(inferred_category, dominant_category)
    if relatedness >= SEARCH_INTENT_RELATEDNESS_CEILING:
        return None

    # Skill-level heuristic: if the user is Intermediate/Advanced
    # overall (or the search phrasing itself isn't beginner-sounding),
    # prefer a non-Beginner bridge course first — a direct, specific
    # search into a new field from an otherwise-experienced learner
    # usually doesn't need a from-scratch intro.
    query_words = set(search_query.lower().split())
    looks_beginner_phrased = bool(query_words & BEGINNER_SIGNAL_WORDS)
    prefer_non_beginner = (user_level in ("Intermediate", "Advanced")) and not looks_beginner_phrased

    bridge_query = f"{search_query} for {dominant_category}"
    bridge_products: List[Product] = []

    if prefer_non_beginner:
        for candidate_level in ("Intermediate", "Advanced"):
            bridge_products = product_service.semantic_search_products(
                db, bridge_query, top_k=BRIDGE_RESULTS_LIMIT, category=inferred_category, level=candidate_level
            )
            if bridge_products:
                break

    if not bridge_products:
        bridge_products = product_service.semantic_search_products(
            db, bridge_query, top_k=BRIDGE_RESULTS_LIMIT, category=inferred_category
        )

    if not bridge_products:
        bridge_products = product_service.semantic_search_products(
            db, search_query, top_k=BRIDGE_RESULTS_LIMIT, category=inferred_category
        )

    if not bridge_products:
        return None

    return {
        "search_query": search_query,
        "inferred_category": inferred_category,
        "products": bridge_products,
    }


def get_recommendation_candidates(db: Session, user: User, force_widened: bool = False) -> Optional[Dict]:
    already_enrolled = _enrolled_product_ids(db, user)

    # --- Spec scoring pipeline: bot filter → category scores → dominance rule ---
    raw_events = fetch_scoring_events(db, user)
    cleaned_events = remove_bot_noise(raw_events)

    event_count = count_personalization_events(cleaned_events)
    if event_count < MIN_EVENTS_FOR_PERSONALIZATION and not force_widened:
        return _cold_start_result(db, already_enrolled)

    category_scores, _cleaned = build_category_profile_for_retrieval(db, user, pre_cleaned_events=cleaned_events)
    if not category_scores:
        return _cold_start_result(db, already_enrolled)

    sorted_cats = sorted(category_scores.items(), key=lambda kv: kv[1], reverse=True)
    retrieval_categories = resolve_retrieval_categories(sorted_cats)
    if not retrieval_categories:
        return _cold_start_result(db, already_enrolled)

    dominant_category = retrieval_categories[0]
    preferred_level = _user_experience_level(user)
    hard_level = (event_count >= 5 and preferred_level is not None and not force_widened)
    already_viewed = _viewed_product_ids(db, user)

    # Check for price sensitivity: if user dismissed higher priced items
    dismissed_product_ids = {e["product_id"] for e in cleaned_events if e.get("type") == "dismiss" and e.get("product_id")}
    dismissed_expensive = False
    if dismissed_product_ids:
        dismissed_prods = db.query(Product.price).filter(Product.id.in_(dismissed_product_ids)).all()
        dismissed_expensive = any((p[0] or 0) > 40 for p in dismissed_prods)

    primary_products: List[Product] = []
    seen_product_ids: set = set()
    per_category_limit = max(3, PRIMARY_RESULTS_LIMIT // len(retrieval_categories))
    overall_max_similarity = 0.0

    for cat in retrieval_categories:
        recent_titles = _recently_viewed_titles_in_category(db, user, cat)
        query_text = _build_category_query_text(cat, recent_titles)
        cat_products, max_score = _level_preferred_search(
            db, query_text, cat, preferred_level, per_category_limit + 4, hard_level_filter=hard_level, user=user
        )
        if max_score > overall_max_similarity:
            overall_max_similarity = max_score

        for p in cat_products:
            if p.id in seen_product_ids:
                continue
            if p.id in already_viewed or p.id in already_enrolled:
                continue
            # Price-sensitivity check: filter out expensive options if user dismissed high priced items
            if dismissed_expensive and (p.price or 0) > 60:
                continue
            primary_products.append(p)
            seen_product_ids.add(p.id)

    # Stricter secondary score check: compare top candidate similarity against catalog best score (~0.15 delta)
    # If the score delta is too wide, mark as low confidence fallback
    catalog_best = product_service.semantic_search_products_scored(
        db, dominant_category, top_k=1, category=dominant_category, min_similarity=None
    )
    catalog_best_score = catalog_best[0][1] if catalog_best else 0.85
    if (catalog_best_score - overall_max_similarity > 0.15) and not force_widened:
        return _cold_start_result(db, already_enrolled, low_confidence=True)

    primary_products = filter_already_owned(primary_products, already_enrolled)

    if len(primary_products) < 2:
        return _cold_start_result(db, already_enrolled, low_confidence=True)

    # Re-ranking pass for non-search main recommendation candidates
    user_tags = _user_interest_tags(user)
    primary_products = _rerank_search_products(
        primary_products, query=dominant_category, tags=user_tags
    )

    primary_products = diversify(
        primary_products,
        final_count=PRIMARY_RESULTS_LIMIT,
        user_id=user.id,
        db=db,
    )

    result = {
        "dominant_category": dominant_category,
        "retrieval_categories": retrieval_categories,
        "category_scores": sorted_cats[:3],
        "primary_products": primary_products,
        "instructor_mode": False,
        "instructor_name": None,
        "instructor_own_products": [],
        "instructor_alternative_products": [],
        "search_intent_branch": None,
    }

    last_search_query = _most_recent_search_query(db, user)
    matched_instructor = _match_instructor(db, last_search_query)

    if matched_instructor:
        branch = _instructor_branch(db, matched_instructor)
        result["instructor_mode"] = True
        result["instructor_name"] = matched_instructor
        result["instructor_own_products"] = branch["own_courses"]
        result["instructor_alternative_products"] = branch["alternatives"]
    elif last_search_query:
        result["search_intent_branch"] = _search_intent_branch(
            db, dominant_category, last_search_query, preferred_level
        )

    return result



def _cold_start_result(
    db: Session,
    already_enrolled: set,
    low_confidence: bool = False,
) -> Optional[Dict]:
    """Trending catalog fallback when personalization is unavailable or weak."""
    cold_start_products = (
        db.query(Product)
        .filter(~Product.id.in_(already_enrolled) if already_enrolled else True)
        .order_by(
            Product.rating.desc().nullslast(),
            Product.enrolled_students.desc().nullslast(),
        )
        .limit(COLD_START_RESULTS_LIMIT)
        .all()
    )
    if not cold_start_products:
        return None
    return {
        "dominant_category": None,
        "primary_products": cold_start_products,
        "instructor_mode": False,
        "instructor_name": None,
        "instructor_own_products": [],
        "instructor_alternative_products": [],
        "search_intent_branch": None,
        "cold_start": True,
        "low_confidence": low_confidence,
    }