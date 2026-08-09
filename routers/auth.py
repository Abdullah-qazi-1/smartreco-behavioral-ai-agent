"""
Authentication router.
Uses simple session-cookie auth (Starlette's SessionMiddleware).
Supports Dual-Mode Accounts: Any registered user can log in as either Student or Instructor.
"""
import re
import bcrypt
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import logging
from database.db import get_db
from database.models import User, UserProfile
from services.tracking_prefs import sync_session_tracking_from_db

logger = logging.getLogger("smartreco.auth")
router = APIRouter()
templates = Jinja2Templates(directory="templates")

INTEREST_CHOICES = [
    "AI", "Machine Learning", "Deep Learning", "Data Science",
    "Python", "Cloud", "AWS", "DevOps", "Cyber Security", "LLMs",
    "Web Development", "Backend", "Frontend", "Mobile", "UI/UX",
    "SQL", "MLOps",
]
EXPERIENCE_LEVEL_CHOICES = ["Beginner", "Intermediate", "Advanced"]
INTERESTS_REASK_INTERVAL = timedelta(days=182)


def hash_password(password: str) -> str:
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))


def password_strength_error(password: str) -> str | None:
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if not re.search(r"[a-z]", password):
        return "Password must include at least one lowercase letter."
    if not re.search(r"[A-Z]", password):
        return "Password must include at least one uppercase letter."
    return None


def interests_need_asking(user: User) -> bool:
    """
    True if the onboarding interests screen should be shown: user hasn't set interests
    or 6+ months have elapsed. Instructors operating in instructor mode skip this.
    """
    if getattr(user, "active_mode", "student") == "instructor":
        return False
    if not user.interests_updated_at:
        return True
    last_set = user.interests_updated_at
    if last_set.tzinfo is None:
        last_set = last_set.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last_set) > INTERESTS_REASK_INTERVAL


def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """Returns the logged-in User object, or None if not logged in."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.query(User).filter(User.id == user_id).first()
    if user and request.session.get("active_mode"):
        user.active_mode = request.session.get("active_mode")
    return user


def require_login(request: Request, db: Session = Depends(get_db)):
    return get_current_user(request, db)


# ---------------------------------------------------------------
# Routes
# ---------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user_id"):
        mode = request.session.get("active_mode", "student")
        target = "/admin" if mode == "instructor" else "/dashboard"
        return RedirectResponse(target, status_code=302)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "login_error": None,
            "signup_error": None,
            "success_message": None,
            "email_not_found": False,
        },
    )


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    email_clean = email.lower().strip()
    user = db.query(User).filter(User.email == email_clean).first()

    if not user:
        logger.warning("User login failed: email=%s not found", email_clean)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "login_error": None,
                "signup_error": None,
                "success_message": None,
                "email_not_found": True,
            },
            status_code=401,
        )

    if not verify_password(password, user.password_hash):
        logger.warning("User login failed: incorrect password for user_id=%s email=%s", user.id, email_clean)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "login_error": "Incorrect password. Please try again.",
                "signup_error": None,
                "success_message": None,
                "email_not_found": False,
            },
            status_code=401,
        )

    # Use the user's existing active_mode from the DB instead of a form field.
    # New signups default to "student"; mode switching happens via /api/switch-mode
    # from the profile dropdown after login, not at login time.
    active_mode = getattr(user, "active_mode", "student") or "student"

    request.session["user_id"] = user.id
    request.session["role"] = user.role
    request.session["email"] = user.email
    request.session["name"] = user.name or user.email.split("@")[0]
    request.session["active_mode"] = active_mode
    sync_session_tracking_from_db(db, user, request)

    logger.info("User login successful: user_id=%s email=%s active_mode=%s", user.id, user.email, active_mode)

    if active_mode == "instructor":
        return RedirectResponse("/admin", status_code=302)

    if interests_need_asking(user):
        return RedirectResponse("/onboarding/interests", status_code=302)

    return RedirectResponse("/dashboard", status_code=302)


@router.post("/signup")
def signup_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    email_clean = email.lower().strip()

    existing = db.query(User).filter(User.email == email_clean).first()
    if existing:
        logger.warning("User signup failed: email=%s already exists", email_clean)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "login_error": None,
                "signup_error": "An account with this email already exists.",
                "success_message": None,
                "email_not_found": False,
            },
            status_code=400,
        )

    weak_reason = password_strength_error(password)
    if weak_reason:
        logger.warning("User signup failed: weak password for email=%s (%s)", email_clean, weak_reason)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "login_error": None,
                "signup_error": weak_reason,
                "success_message": None,
                "email_not_found": False,
            },
            status_code=400,
        )

    active_mode = "student"


    user = User(
        name=name.strip(),
        email=email_clean,
        password_hash=hash_password(password),
        role="user",
        active_mode=active_mode,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    profile = UserProfile(user_id=user.id, agent_tracking_enabled=True)
    db.add(profile)
    db.commit()

    logger.info("User signup successful: user_id=%s email=%s active_mode=%s", user.id, user.email, active_mode)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "login_error": None,
            "signup_error": None,
            "success_message": "Account created! Please log in.",
            "email_not_found": False,
        },
    )


@router.post("/api/switch-mode")
def switch_mode(
    request: Request,
    mode: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    new_mode = "instructor" if mode.lower() == "instructor" else "student"
    request.session["active_mode"] = new_mode
    user.active_mode = new_mode
    db.commit()

    logger.info("User switched mode: user_id=%s new_mode=%s", user.id, new_mode)

    redirect_url = "/admin" if new_mode == "instructor" else "/dashboard"
    return {"status": "ok", "active_mode": new_mode, "redirect_url": redirect_url}


@router.post("/logout")
def logout(request: Request):
    user_id = request.session.get("user_id")
    request.session.clear()
    logger.info("User logged out: user_id=%s", user_id)
    return RedirectResponse("/login", status_code=302)



@router.get("/onboarding/interests", response_class=HTMLResponse)
def onboarding_interests_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if not interests_need_asking(user):
        return RedirectResponse("/dashboard", status_code=302)

    return templates.TemplateResponse(
        request,
        "onboarding.html",
        {
            "user": user,
            "interest_choices": INTEREST_CHOICES,
            "current_interests": user.interests.split(",") if user.interests else [],
            "experience_level_choices": EXPERIENCE_LEVEL_CHOICES,
            "current_experience_level": user.experience_level,
        },
    )


@router.post("/onboarding/interests")
def onboarding_interests_submit(
    request: Request,
    interests: List[str] = Form(default=[]),
    experience_level: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    clean_interests = [i for i in interests if i in INTEREST_CHOICES or i]
    user.interests = ",".join(clean_interests) if clean_interests else None
    user.interests_updated_at = datetime.now(timezone.utc)

    if experience_level in EXPERIENCE_LEVEL_CHOICES:
        user.experience_level = experience_level

    db.commit()
    return RedirectResponse("/dashboard", status_code=302)