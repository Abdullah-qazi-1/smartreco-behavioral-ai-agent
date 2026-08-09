"""
Recommendation router for dashboard & AI Insights.
"""
import json
import logging
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database.db import get_db
from database.models import Recommendation, Product
from routers.auth import get_current_user
from services.agent import generate_and_save_recommendation
from services.trigger import should_regenerate
from services.retrieval import build_search_history, build_profile_search_narrative
from services.reasoning import build_recommendation_reasoning, load_stored_recommendation_reasoning
from services.tracking_prefs import is_agent_tracking_enabled
from services.recommendation_cache import get_cached, set_cached, invalidate_user

logger = logging.getLogger("smartreco.recommendations")
router = APIRouter()
templates = Jinja2Templates(directory="templates")



def _hydrate_ids(db: Session, product_ids: list) -> list:
    if not product_ids:
        return []
    rows = db.query(Product).filter(Product.id.in_(product_ids)).all()
    order = {pid: i for i, pid in enumerate(product_ids)}
    rows.sort(key=lambda p: order.get(p.id, 999))
    return [p.to_dict() for p in rows]


def _build_recommendation_payload(db: Session, user, rec, tracking_enabled: bool) -> dict:
    """Assembles recommendation blocks + structured reasoning for API/UI."""
    reasoning = None
    if rec:
        reasoning = load_stored_recommendation_reasoning(rec)
    if not reasoning:
        reasoning = build_recommendation_reasoning(db, user, tracking_enabled=tracking_enabled)

    if not rec:
        return {"exists": False, "reasoning": reasoning}

    try:
        payload = json.loads(rec.narrative) if rec.narrative else {}
    except (TypeError, ValueError):
        payload = {}

    main_block = None
    if payload.get("main"):
        main_block = {
            "narrative": payload["main"]["narrative"],
            "products": _hydrate_ids(db, payload["main"].get("product_ids", [])),
        }

    search_intent_block = None
    if payload.get("search_intent"):
        si = payload["search_intent"]
        search_intent_block = {
            "query": si.get("query"),
            "category": si.get("category"),
            "narrative": si["narrative"],
            "products": _hydrate_ids(db, si.get("product_ids", [])),
        }

    if not main_block and not search_intent_block:
        return {"exists": False, "reasoning": reasoning}

    return {
        "exists": True,
        "trigger_reason": rec.trigger_reason,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "search_intent": search_intent_block,
        "main": main_block,
        "reasoning": reasoning,
    }


@router.get("/ai-insights", response_class=HTMLResponse)
def ai_insights_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if should_regenerate(db, user):
        logger.info("AI Insights visit triggered recommendation refresh for user_id=%s", user.id)
        generate_and_save_recommendation(db, user)

    search_ctx = build_search_history(db, user, limit=10)
    latest_search = search_ctx.get("latest")
    sidebar_history = search_ctx.get("sidebar") or []
    recent_search = latest_search["query"] if latest_search else None
    why_narrative = build_profile_search_narrative(user, recent_search)
    tracking_enabled = is_agent_tracking_enabled(db, user, request)

    rec = (
        db.query(Recommendation)
        .filter(Recommendation.user_id == user.id, Recommendation.is_latest == True)
        .order_by(Recommendation.created_at.desc())
        .first()
    )

    reasoning = None
    if rec:
        reasoning = load_stored_recommendation_reasoning(rec)
    if not reasoning:
        reasoning = build_recommendation_reasoning(db, user, tracking_enabled=tracking_enabled)

    narrative_text = None
    picked_products = []

    # Recent catalog search drives Top 3 Curated Picks — not stale LLM profile-only recs.
    if latest_search and latest_search.get("products"):
        picked_products = latest_search["products"][:3]
    elif rec and rec.narrative:
        try:
            payload = json.loads(rec.narrative)
            search_intent = payload.get("search_intent") or {}
            if search_intent.get("product_ids") and recent_search:
                picked_products = _hydrate_ids(db, search_intent.get("product_ids", []))
            if not picked_products:
                main = payload.get("main") or {}
                narrative_text = main.get("narrative")
                picked_products = _hydrate_ids(db, main.get("product_ids", []))
        except Exception:
            pass

    if not picked_products:
        picked_products = [p.to_dict() for p in db.query(Product).order_by(Product.rating.desc()).limit(3).all()]

    return templates.TemplateResponse(
        request,
        "ai-insights.html",
        {
            "user": user,
            "active_page": "ai_insights",
            "recommendation": rec,
            "narrative_text": narrative_text,
            "why_narrative": why_narrative,
            "picked_products": picked_products,
            "latest_search": latest_search,
            "sidebar_history": sidebar_history,
            "reasoning": reasoning,
            "tracking_enabled": tracking_enabled,
        },
    )



@router.get("/api/recommendations")
@router.get("/api/recommendations/latest")
def get_recommendations(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    tracking_enabled = is_agent_tracking_enabled(db, user, request)

    rec = (
        db.query(Recommendation)
        .filter(Recommendation.user_id == user.id, Recommendation.is_latest == True)
        .order_by(Recommendation.created_at.desc())
        .first()
    )

    rec_fingerprint = None
    if rec:
        rec_fingerprint = f"{rec.id}:{rec.created_at.isoformat() if rec.created_at else ''}"

    cached = get_cached(user.id, rec_fingerprint)
    if cached is not None:
        return cached

    payload = _build_recommendation_payload(db, user, rec, tracking_enabled)
    if payload.get("exists"):
        logger.info(
            "Fetched recommendations for user_id=%s rec_id=%s match_score=%s",
            user.id, rec.id, payload.get("reasoning", {}).get("match_score"),
        )
    set_cached(user.id, rec_fingerprint, payload)
    return payload


@router.post("/api/recommendations/refresh")
@router.post("/api/ai/refresh")
def force_refresh_recommendation(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    logger.info("Force refresh requested for user_id=%s", user.id)
    if not is_agent_tracking_enabled(db, user, request):
        return JSONResponse(
            {"status": "skipped", "reason": "tracking_disabled"},
            status_code=403,
        )
    rec = generate_and_save_recommendation(db, user, force=True)
    invalidate_user(user.id)
    logger.info("Force refresh completed for user_id=%s rec_id=%s", user.id, rec.id if rec else None)
    return {"status": "refreshed", "recommendation_id": rec.id if rec else None}

