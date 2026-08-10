"""
Event tracking router — pure ingestion + background recommendation trigger.
Accepts a BATCH of events in one request, bulk-inserts them, then
schedules a non-blocking background check: "does this user now have
enough new genuine activity to justify a fresh recommendation?"

No LLM call happens in the request/response cycle itself — the
background task decides (via services.trigger.should_regenerate) and
only calls the LLM if the threshold is actually met. This keeps
/api/events fast regardless of whether a recommendation ends up being
generated.

Level 4.2 — Agent Tracking Toggle:
Every inserted event is stamped with `agent_eligible`, reflecting whether
the user had tracking switched ON at the moment the event happened
(read from the session, not the DB — this is a per-session preference,
always reset to ON on a fresh login by routers/auth.py). Once an event
is saved with agent_eligible=False, it is permanently excluded from the
recommendation pipeline — turning tracking back ON later does NOT
retroactively make those old events eligible.
"""
import json
import logging
from fastapi import APIRouter, Request, Depends, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from database.db import get_db, SessionLocal
from database.models import Event, User
from routers.auth import get_current_user
from services.agent import generate_and_save_recommendation
from services.tracking_prefs import is_agent_tracking_enabled, set_agent_tracking_enabled

router = APIRouter()
logger = logging.getLogger("smartreco.events")

VALID_EVENT_TYPES = {"view", "search", "click", "time_spent", "dismiss", "enroll", "scroll_depth"}
# NOTE: "scroll_depth" was added here because static/js/tracker.js has always fired
# scroll_depth events (25/50/75/100% milestones), but they were previously silently
# dropped at ingest since this set didn't include the type — a real data-loss bug,
# not a design choice. They are now persisted like any other event. They are
# intentionally NOT yet added to services/scoring_weights.EVENT_BASE_WEIGHTS — folding
# a new signal into the interest-scoring formula is a product/tuning decision, not a
# "make it not silently drop data" fix, so that remains a follow-up (see PROJECT.md
# Known Limitations).
MAX_EVENTS_PER_BATCH = 50  # safety cap — one browser batch should never be huge

# --- Input validation limits (explicit, enforced, and documented in README) ---
# A batch cap alone (MAX_EVENTS_PER_BATCH above) does not protect against a single
# request with an oversized BODY (e.g. one client sending a 50MB JSON blob) — the
# body is still fully read/parsed into memory by request.json() before any
# per-event truncation happens. These two limits close that gap:
MAX_REQUEST_BODY_BYTES = 256 * 1024       # 256 KB — generous for a 50-event batch
                                            # of small view/search/click events;
                                            # rejects anything wildly oversized
                                            # (e.g. abuse or a client-side bug)
                                            # BEFORE it's parsed, via Content-Length.
MAX_METADATA_JSON_CHARS = 2000            # per-event metadata dict, once
                                            # json.dumps()'d — a normal event's
                                            # metadata (search query, dwell seconds,
                                            # scroll %) is well under 200 chars; this
                                            # caps any single event from ballooning
                                            # the events table with unbounded text.


def _content_length_exceeds(request: Request, max_bytes: int) -> bool:
    """
    Cheap pre-check using the Content-Length header, so an oversized request is
    rejected BEFORE request.json() reads/parses the whole body into memory. Not a
    substitute for a reverse-proxy body-size limit in a real deployment (a client
    could omit/lie about Content-Length and stream a large chunked body), but it
    stops the common case — and defense in depth also matters here since not
    every deployment sits behind a proxy that enforces this.
    """
    content_length = request.headers.get("content-length")
    if content_length is None:
        return False
    try:
        return int(content_length) > max_bytes
    except ValueError:
        return False


def _run_recommendation_check(user_id: int):
    """
    Runs in the background, AFTER the /api/events response has already
    been sent to the browser.
    Boundary exception handler retains generic except Exception with clear logging
    so worker thread failures do not crash background processing.
    """
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return
        generate_and_save_recommendation(db, user)
    except Exception:
        logger.exception("Background recommendation check failed for user %s", user_id)
    finally:
        db.close()


@router.post("/api/events")
async def receive_events(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if _content_length_exceeds(request, MAX_REQUEST_BODY_BYTES):
        return JSONResponse(
            {"error": f"payload too large — max {MAX_REQUEST_BODY_BYTES} bytes"},
            status_code=413,
        )

    try:
        body = await request.json()
    except (json.JSONDecodeError, TypeError, ValueError):
        return JSONResponse({"error": "invalid json"}, status_code=400)

    raw_events = body.get("events", [])
    if not isinstance(raw_events, list) or not raw_events:
        return JSONResponse({"error": "no events"}, status_code=400)

    tracking_enabled = is_agent_tracking_enabled(db, user, request)

    if not tracking_enabled:
        return {"inserted": 0, "agent_eligible": False, "dropped": len(raw_events[:MAX_EVENTS_PER_BATCH])}

    to_insert = []
    for e in raw_events[:MAX_EVENTS_PER_BATCH]:
        event_type = e.get("event_type")
        if event_type not in VALID_EVENT_TYPES:
            continue

        # product_id must be a real int or absent — never trust the client's type
        # (routers/monitoring.py's digest-readiness math and every FK-based join
        # downstream assumes this column is either a valid integer or NULL).
        raw_product_id = e.get("product_id")
        if raw_product_id is not None and not isinstance(raw_product_id, int):
            continue
        product_id = raw_product_id

        metadata = e.get("metadata") if isinstance(e.get("metadata"), dict) else {}
        if e.get("client_timestamp"):
            metadata["client_timestamp"] = e["client_timestamp"]

        metadata_json = json.dumps(metadata) if metadata else None
        if metadata_json and len(metadata_json) > MAX_METADATA_JSON_CHARS:
            # Don't drop the whole event over an oversized metadata blob — just
            # cap what gets stored, the same non-fatal-degradation pattern used
            # everywhere else in this app (Mesh failures, SMTP/Telegram misses).
            metadata_json = metadata_json[:MAX_METADATA_JSON_CHARS]

        to_insert.append(Event(
            user_id=user.id,
            event_type=event_type,
            product_id=product_id,
            event_metadata=metadata_json,
            agent_eligible=True,
        ))

    if not to_insert:
        return JSONResponse({"error": "no valid events in batch"}, status_code=400)

    db.bulk_save_objects(to_insert)
    db.commit()

    logger.info("Event batch ingested: user_id=%s count=%d agent_eligible=%s", user.id, len(to_insert), tracking_enabled)

    background_tasks.add_task(_run_recommendation_check, user.id)

    return {"inserted": len(to_insert), "agent_eligible": tracking_enabled}


@router.post("/api/tracking-toggle")
async def set_tracking_toggle(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except (json.JSONDecodeError, TypeError, ValueError):
        return JSONResponse({"error": "invalid json"}, status_code=400)

    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return JSONResponse({"error": "'enabled' must be true or false"}, status_code=400)

    set_agent_tracking_enabled(db, user, request, enabled)
    logger.info("Tracking toggle updated: user_id=%s enabled=%s (persisted)", user.id, enabled)
    return {"agent_tracking_enabled": enabled}


@router.post("/api/track")
async def receive_events_track_alias(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Alias for /api/events — accepts the same batch payload."""
    return await receive_events(request, background_tasks, db)