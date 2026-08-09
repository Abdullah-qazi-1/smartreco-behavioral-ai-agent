"""
services/analytics.py — Recommendation Analytics Service for SmartReco.

Calculates key business & AI performance metrics:
1. Total recommendations generated per user (average & breakdown).
2. Recommendation-to-enrollment conversion rate.
3. Trigger efficiency (fired vs skipped evaluation ratio & trigger reasons).
4. Most-recommended categories and products.
"""
import json
import logging
from collections import Counter
from typing import Dict, Any, List
from sqlalchemy.orm import Session
from sqlalchemy import func

from database.models import Recommendation, Enrollment, Product, User
from services.metrics import get_trigger_metrics

logger = logging.getLogger("smartreco.analytics")


def get_recommendations_per_user_stats(db: Session) -> Dict[str, Any]:
    """Computes total recommendations, unique users with recommendations, and average per user."""
    total_recs = db.query(Recommendation).count()
    if total_recs == 0:
        return {
            "total_recommendations": 0,
            "total_users_with_recommendations": 0,
            "avg_recommendations_per_user": 0.0,
            "top_users": [],
        }

    # User breakdown query
    user_counts = (
        db.query(User.id, User.name, User.email, func.count(Recommendation.id).label("rec_count"))
        .join(Recommendation, Recommendation.user_id == User.id)
        .group_by(User.id)
        .order_by(func.count(Recommendation.id).desc())
        .all()
    )

    total_users_with_recs = len(user_counts)
    avg_per_user = round(total_recs / total_users_with_recs, 2) if total_users_with_recs > 0 else 0.0

    top_users = [
        {
            "user_id": u.id,
            "name": u.name or u.email.split("@")[0],
            "email": u.email,
            "recommendation_count": u.rec_count,
        }
        for u in user_counts[:10]
    ]

    return {
        "total_recommendations": total_recs,
        "total_users_with_recommendations": total_users_with_recs,
        "avg_recommendations_per_user": avg_per_user,
        "top_users": top_users,
    }


def get_conversion_rate_stats(db: Session) -> Dict[str, Any]:
    """
    Computes recommendation-to-enrollment conversion rate.
    A recommendation is considered converted if the user enrolled in any of the recommended products.
    """
    recs = db.query(Recommendation).all()
    total_recs = len(recs)
    if total_recs == 0:
        return {
            "total_recommendations": 0,
            "converted_recommendations": 0,
            "conversion_rate_pct": 0.0,
        }

    # Fetch all user enrollments map: user_id -> set of product_ids enrolled
    all_enrollments = db.query(Enrollment.user_id, Enrollment.product_id).all()
    enrollment_map = {}
    for uid, pid in all_enrollments:
        enrollment_map.setdefault(uid, set()).add(pid)

    converted_count = 0
    for rec in recs:
        # Check if already flagged converted in DB column
        if getattr(rec, "converted", False):
            converted_count += 1
            continue

        # Dynamic fallback check: did user enroll in any recommended product?
        user_enrolled = enrollment_map.get(rec.user_id, set())
        if not user_enrolled:
            continue

        try:
            pids = json.loads(rec.product_ids) if rec.product_ids else []
            if any(pid in user_enrolled for pid in pids):
                converted_count += 1
        except Exception:
            pass

    conversion_rate_pct = round((converted_count / total_recs) * 100.0, 2)

    return {
        "total_recommendations": total_recs,
        "converted_recommendations": converted_count,
        "conversion_rate_pct": conversion_rate_pct,
    }


def get_trigger_efficiency_stats(db: Session) -> Dict[str, Any]:
    """Calculates trigger efficiency metrics and trigger reason breakdown."""
    trigger_metrics = get_trigger_metrics()

    # Query database trigger reasons breakdown
    reasons = (
        db.query(Recommendation.trigger_reason, func.count(Recommendation.id))
        .group_by(Recommendation.trigger_reason)
        .all()
    )
    reason_breakdown = {r[0] or "unknown": r[1] for r in reasons}

    return {
        "metrics": trigger_metrics,
        "trigger_reasons": reason_breakdown,
    }


def get_most_recommended_categories_and_products(db: Session) -> Dict[str, Any]:
    """Computes the top recommended products and top recommended categories."""
    recs = db.query(Recommendation.product_ids).all()
    product_counter = Counter()

    for (pids_json,) in recs:
        if not pids_json:
            continue
        try:
            pids = json.loads(pids_json)
            if isinstance(pids, list):
                product_counter.update(pids)
        except Exception:
            pass

    if not product_counter:
        return {"top_products": [], "top_categories": []}

    # Fetch product details for top products
    top_pid_counts = product_counter.most_common(10)
    top_pids = [pid for pid, _ in top_pid_counts]
    products = db.query(Product).filter(Product.id.in_(top_pids)).all()
    product_by_id = {p.id: p for p in products}

    category_counter = Counter()
    top_products_list = []

    for pid, count in top_pid_counts:
        prod = product_by_id.get(pid)
        if prod:
            category_counter[prod.category] += count
            top_products_list.append({
                "product_id": prod.id,
                "title": prod.title,
                "category": prod.category,
                "rating": prod.rating or 0.0,
                "recommendation_count": count,
            })

    top_categories_list = [
        {"category": cat, "recommendation_count": count}
        for cat, count in category_counter.most_common(5)
    ]

    return {
        "top_products": top_products_list,
        "top_categories": top_categories_list,
    }


def get_full_analytics_summary(db: Session) -> Dict[str, Any]:
    """Aggregates all recommendation analytics into a single response payload."""
    try:
        user_stats = get_recommendations_per_user_stats(db)
        conversion_stats = get_conversion_rate_stats(db)
        trigger_stats = get_trigger_efficiency_stats(db)
        popularity_stats = get_most_recommended_categories_and_products(db)

        return {
            "status": "ok",
            "recommendations_per_user": user_stats,
            "conversion_rate": conversion_stats,
            "trigger_efficiency": trigger_stats,
            "most_recommended": popularity_stats,
        }
    except Exception as exc:
        logger.error("Error generating analytics summary: %s", exc, exc_info=True)
        return {
            "status": "error",
            "error": str(exc),
        }
