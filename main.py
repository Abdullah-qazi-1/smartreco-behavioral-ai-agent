import os
import logging
from fastapi import FastAPI, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.gzip import GZipMiddleware
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from database.db import Base, engine, get_db, run_migrations
from database import models  # noqa: F401 (import ensures tables get registered)
from database.models import Enrollment, Product
from routers.auth import router as auth_router, get_current_user
from routers.products import router as products_router
from routers import events, recommendations, monitoring
from services.activity import get_recent_activity
from services.tracking_prefs import is_agent_tracking_enabled
from services.rate_limit import check_rate_limit

load_dotenv()

# Without this, the "smartreco.*" module loggers (services/scheduler.py,
# services/llm_client.py, etc.) inherit Python's default root level of
# WARNING, so their logger.info(...) calls — including the digest-sent /
# digest-failed lines used for demo-video proof in the terminal — are
# silently dropped before they ever reach uvicorn's console output.
# LOG_LEVEL is overridable via .env if a quieter/louder default is wanted.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

_session_secret = os.getenv("SESSION_SECRET", "").strip()
if not _session_secret:
    raise RuntimeError(
        "SESSION_SECRET must be set in the environment (.env). "
        "Do not use hardcoded default secrets in production."
    )

_startup_logger = logging.getLogger("smartreco.startup")


def _validate_optional_env_at_startup():
    """
    Non-fatal environment sanity checks, logged once at boot. MESH_API_KEY is
    intentionally NOT required here (and never made fatal) — see the
    "Resilience — What Happens Without Mesh" README section: the whole point of
    that design is that the app must still start and serve non-AI paths without
    it. What IS worth a loud startup WARNING (not a crash) is a key that's
    PRESENT but almost certainly malformed/pasted-wrong, since that's a much
    harder failure mode to notice — every request "succeeds" at the Python level
    and then fails deep inside an HTTP call to Mesh, which is a worse debugging
    experience than catching the typo before the first request ever comes in.
    """
    mesh_key = os.getenv("MESH_API_KEY", "").strip()
    if not mesh_key:
        _startup_logger.warning(
            "MESH_API_KEY is not set — app will boot normally, but every AI "
            "feature (recommendations, narrative generation, semantic search) "
            "will run in its degraded fallback mode until a key is configured. "
            "See README 'Resilience' section."
        )
    elif not mesh_key.startswith("rsk_"):
        # Per the challenge's own Mesh API docs, a real Mesh key always starts
        # with "rsk_" — a key that doesn't match that shape is almost certainly
        # a copy-paste mistake (wrong env var, a raw OpenAI key, trailing/leading
        # whitespace not fully stripped, etc.), not a valid-but-unusual key.
        _startup_logger.warning(
            "MESH_API_KEY is set but does not start with the expected 'rsk_' "
            "prefix — this is very likely a copy-paste mistake (wrong key "
            "pasted, or a raw provider key instead of a Mesh key) rather than a "
            "valid key, and Mesh API calls will probably fail with an auth "
            "error at request time."
        )

    langsmith_tracing = os.getenv("LANGCHAIN_TRACING_V2", "").strip().lower() == "true"
    if langsmith_tracing and not os.getenv("LANGCHAIN_API_KEY", "").strip():
        _startup_logger.warning(
            "LANGCHAIN_TRACING_V2=true but LANGCHAIN_API_KEY is not set — "
            "LangSmith tracing will silently no-op instead of sending traces "
            "(see services/agent_graph.py's @traceable no-op fallback)."
        )


_validate_optional_env_at_startup()

app = FastAPI(title="SmartReco")

app.add_middleware(GZipMiddleware, minimum_size=500)
# https_only defaults to False so local http://localhost dev keeps working out of
# the box. Set SESSION_COOKIE_SECURE=true in .env for any real/HTTPS deployment
# so the session cookie is never sent over plain HTTP.
_session_cookie_secure = os.getenv("SESSION_COOKIE_SECURE", "false").strip().lower() == "true"
# 7-day absolute session lifetime instead of a cookie that never expires
# (max_age=None previously meant sessions lived forever with no timeout).
_session_max_age_seconds = int(os.getenv("SESSION_MAX_AGE_SECONDS", str(7 * 24 * 60 * 60)))
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    max_age=_session_max_age_seconds,
    same_site="lax",
    https_only=_session_cookie_secure,
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

Base.metadata.create_all(bind=engine)
run_migrations()

app.include_router(events.router)
app.include_router(recommendations.router)
app.include_router(auth_router)
app.include_router(products_router)
app.include_router(monitoring.router)


@app.middleware("http")
async def production_middleware(request: Request, call_next):
    limited = check_rate_limit(request)
    if limited is not None:
        return limited
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "public, max-age=86400")
    return response


from services.scheduler import start_scheduler, shutdown_scheduler

@app.on_event("startup")
def startup_event():
    start_scheduler()

@app.on_event("shutdown")
def shutdown_event():
    shutdown_scheduler()

@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    tracking_enabled = is_agent_tracking_enabled(db, user, request)

    enrolled_products = (
        db.query(Product)
        .join(Enrollment, Enrollment.product_id == Product.id)
        .filter(Enrollment.user_id == user.id)
        .order_by(Enrollment.created_at.desc())
        .all()
    )

    recent_activity = get_recent_activity(db, user.id, limit=5)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "active_page": "dashboard",
            "tracking_enabled": tracking_enabled,
            "enrolled_products": enrolled_products,
            "recent_activity": recent_activity,
        },
    )


@app.get("/my-learning", response_class=HTMLResponse)
def my_learning(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    tracking_enabled = is_agent_tracking_enabled(db, user, request)

    enrolled_products = (
        db.query(Product)
        .join(Enrollment, Enrollment.product_id == Product.id)
        .filter(Enrollment.user_id == user.id)
        .order_by(Enrollment.created_at.desc())
        .all()
    )

    return templates.TemplateResponse(
        request,
        "my-learning.html",
        {
            "user": user,
            "active_page": "my_learning",
            "tracking_enabled": tracking_enabled,
            "enrolled_products": enrolled_products,
        },
    )


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    tracking_enabled = is_agent_tracking_enabled(db, user, request)

    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "user": user,
            "active_page": "profile",
            "tracking_enabled": tracking_enabled,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    tracking_enabled = is_agent_tracking_enabled(db, user, request)

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "active_page": "settings",
            "tracking_enabled": tracking_enabled,
        },
    )