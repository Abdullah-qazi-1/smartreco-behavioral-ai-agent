"""
routers/monitoring.py — Health check and operational metrics router.

Provides:
- GET /health: Checks SQLite DB connectivity, ChromaDB vector DB connectivity, and LLM provider configuration.
- GET /metrics: Returns total events ingested, total recommendations generated, LLM call stats, and trigger fire rates.
"""
import os
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from database.db import get_db
from database.models import Event, Recommendation
from database import chroma_client
from services.llm_client import get_client
from services.metrics import get_llm_metrics, get_trigger_metrics
from routers.auth import get_current_user
from database.models import User

logger = logging.getLogger("smartreco.monitoring")
router = APIRouter()


@router.get("/health")
def health_check(db: Session = Depends(get_db)):
    """
    Simple system health check for database, ChromaDB, and LLM provider.
    """
    checks = {}
    overall_healthy = True

    # 1. Database check
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = {"status": "ok", "engine": "sqlite"}
    except Exception as exc:
        overall_healthy = False
        checks["database"] = {"status": "error", "error": str(exc)}
        logger.error("Health check failed for database: %s", exc)

    # 2. ChromaDB check
    try:
        count = chroma_client.get_collection_count()
        checks["chromadb"] = {"status": "ok", "collection_count": count}
    except Exception as exc:
        overall_healthy = False
        checks["chromadb"] = {"status": "error", "error": str(exc)}
        logger.error("Health check failed for ChromaDB: %s", exc)

    # 3. LLM Provider & Embedding Backend check
    try:
        client, model = get_client()
        checks["llm"] = {
            "status": "ok",
            "provider": "mesh",
            "model": model,
            "configured": bool(client),
            "embedding_backend": chroma_client.get_embedding_backend(),
        }
    except Exception as exc:
        overall_healthy = False
        checks["llm"] = {"status": "error", "error": str(exc)}
        logger.error("Health check failed for LLM provider: %s", exc)

    status_str = "healthy" if overall_healthy else "unhealthy"
    status_code = 200 if overall_healthy else 503

    logger.info("Health check performed: overall_status=%s", status_str)
    return JSONResponse(
        content={
            "status": status_str,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": checks,
        },
        status_code=status_code,
    )


@router.get("/metrics")
def operational_metrics(request: Request, db: Session = Depends(get_db)):
    """
    Exposes basic operational metrics. Requires real admin role (course-owning
    instructors are intentionally excluded — this is platform-wide data, not
    scoped to a single instructor's courses).
    """
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if user.role != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)

    try:
        total_events = db.query(Event).count()
        total_recs = db.query(Recommendation).count()
        llm_stats = get_llm_metrics()
        trigger_stats = get_trigger_metrics()

        metrics_payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_events_ingested": total_events,
            "total_recommendations_generated": total_recs,
            "llm_calls": llm_stats,
            "trigger_evaluations": trigger_stats,
        }

        logger.info("Operational metrics retrieved: total_events=%d total_recs=%d", total_events, total_recs)
        return metrics_payload
    except Exception as exc:
        logger.error("Failed to compile operational metrics: %s", exc, exc_info=True)
        return JSONResponse({"error": "Failed to retrieve metrics", "details": str(exc)}, status_code=500)


@router.get("/api/admin/analytics")
@router.get("/api/analytics")
def admin_analytics(request: Request, db: Session = Depends(get_db)):
    """
    Exposes complete recommendation analytics summary for admin dashboard.
    Requires real admin role only.
    """
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if user.role != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)

    from services.analytics import get_full_analytics_summary
    return get_full_analytics_summary(db)


@router.post("/api/admin/run-digest")
def trigger_manual_digest(request: Request, db: Session = Depends(get_db)):
    """
    Manually triggers the daily digest batch job for testing and administrative review.
    Requires real admin role only — this sends live emails/Telegram messages to
    every eligible user, so it must not be reachable via self-service instructor mode.
    """
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if user.role != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)

    from services.scheduler import run_daily_digest_job
    summary = run_daily_digest_job()
    return {"status": "completed", "summary": summary}


@router.get("/api/admin/digest-readiness")
def digest_readiness(request: Request, db: Session = Depends(get_db)):
    """
    Read-only pre-flight check for the daily digest — reports whether delivery
    channels are configured and how many users/recommendations are currently
    in scope, WITHOUT sending anything. Meant to be checked before hitting
    POST /api/admin/run-digest (e.g. right before recording a demo), so a
    misconfigured SMTP/Telegram env var is caught ahead of time instead of
    surfacing only inside the run-digest summary's error list.
    Requires real admin role only.
    """
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if user.role != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)

    from services.scheduler import get_users_active_today

    smtp_configured = bool(
        os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASS")
    )
    telegram_configured = bool(
        os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")
    )

    active_users = get_users_active_today(db)
    active_user_ids = [u.id for u in active_users]

    users_with_recommendation = 0
    if active_user_ids:
        users_with_recommendation = (
            db.query(Recommendation.user_id)
            .filter(
                Recommendation.user_id.in_(active_user_ids),
                Recommendation.is_latest == True,  # noqa: E712
            )
            .distinct()
            .count()
        )
    users_needing_generation = len(active_user_ids) - users_with_recommendation

    logger.info(
        "Digest readiness checked by admin user_id=%s: smtp=%s telegram=%s "
        "active_users=%d with_rec=%d needing_generation=%d",
        user.id, smtp_configured, telegram_configured,
        len(active_user_ids), users_with_recommendation, users_needing_generation,
    )

    return {
        "channels": {
            "smtp_configured": smtp_configured,
            "telegram_configured": telegram_configured,
        },
        "active_users_today": len(active_user_ids),
        "users_with_saved_recommendation": users_with_recommendation,
        "users_needing_recommendation_generation": users_needing_generation,
    }


@router.post("/api/admin/reconcile-vectors")
def trigger_manual_reconcile(request: Request, db: Session = Depends(get_db)):
    """
    Manually triggers the self-healing vector-store reconcile job (normally runs
    hourly in the background — see services/scheduler.py). Retries the Chroma/Mesh
    upsert for every product whose most recent dual-write attempt failed, so an
    admin can force-repair sync drift immediately instead of waiting for the next
    scheduled cycle. Requires real admin role only.
    """
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if user.role != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)

    from services.product_service import reconcile_vector_store
    summary = reconcile_vector_store(db)
    return {"status": "completed", "summary": summary}