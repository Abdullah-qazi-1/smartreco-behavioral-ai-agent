"""
Recent user activity feed for Dashboard sidebar.
"""
import json
from typing import List, Dict, Any

from sqlalchemy.orm import Session

from database.models import Event, Product


def get_recent_activity(db: Session, user_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    """Returns human-readable activity rows from recent agent-eligible events."""
    events = (
        db.query(Event)
        .filter(Event.user_id == user_id, Event.agent_eligible == True)  # noqa: E712
        .order_by(Event.created_at.desc())
        .limit(limit * 2)
        .all()
    )

    product_ids = {e.product_id for e in events if e.product_id}
    products_by_id = {}
    if product_ids:
        rows = db.query(Product).filter(Product.id.in_(product_ids)).all()
        products_by_id = {p.id: p for p in rows}

    activity: List[Dict[str, Any]] = []
    for e in events:
        label = _format_event_label(e, products_by_id)
        if not label:
            continue
        activity.append({
            "label": label,
            "event_type": e.event_type,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        })
        if len(activity) >= limit:
            break

    return activity


def _format_event_label(event: Event, products_by_id: dict) -> str:
    meta = {}
    if event.event_metadata:
        try:
            meta = json.loads(event.event_metadata)
        except (TypeError, ValueError):
            meta = {}

    if event.event_type == "search":
        query = meta.get("query", "").strip()
        return f'Searched: "{query}"' if query else None

    if event.event_type == "view":
        if event.product_id and event.product_id in products_by_id:
            return f"Viewed: {products_by_id[event.product_id].title}"
        path = meta.get("path", "")
        if path and "/catalog" in path:
            return "Viewed Course Catalog"
        if path and "/ai-insights" in path:
            return "Checked AI Recommendations"
        return "Viewed a page"

    if event.event_type == "click":
        if event.product_id and event.product_id in products_by_id:
            return f"Clicked: {products_by_id[event.product_id].title}"
        return "Clicked a course"

    if event.event_type == "time_spent":
        seconds = meta.get("seconds")
        if event.product_id and event.product_id in products_by_id:
            title = products_by_id[event.product_id].title
            return f"Spent {seconds}s on: {title}" if seconds else f"Viewed: {title}"
        return None

    if event.event_type == "dismiss":
        if event.product_id and event.product_id in products_by_id:
            return f"Dismissed: {products_by_id[event.product_id].title}"
        return "Marked not interested"

    if event.event_type == "enroll":
        if event.product_id and event.product_id in products_by_id:
            return f"Enrolled: {products_by_id[event.product_id].title}"
        return "Enrolled in a course"

    return None
