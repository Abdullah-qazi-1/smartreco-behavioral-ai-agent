"""
Persisted + session agent tracking preference helpers.
"""
from typing import Optional

from fastapi import Request
from sqlalchemy.orm import Session

from database.models import User, UserProfile


def get_or_create_profile(db: Session, user: User) -> UserProfile:
    profile = db.query(UserProfile).filter(UserProfile.user_id == user.id).first()
    if not profile:
        profile = UserProfile(user_id=user.id, agent_tracking_enabled=True)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


def is_agent_tracking_enabled(db: Session, user: User, request: Optional[Request] = None) -> bool:
    """
    DB is source of truth; session mirrors it when request is available.
    Falls back to session-only if profile row is missing (legacy users).
    """
    profile = db.query(UserProfile).filter(UserProfile.user_id == user.id).first()
    if profile is not None:
        return bool(profile.agent_tracking_enabled)
    if request is not None:
        return bool(request.session.get("agent_tracking_enabled", True))
    return True


def set_agent_tracking_enabled(
    db: Session,
    user: User,
    request: Request,
    enabled: bool,
) -> bool:
    profile = get_or_create_profile(db, user)
    profile.agent_tracking_enabled = enabled
    request.session["agent_tracking_enabled"] = enabled
    db.commit()
    return enabled


def sync_session_tracking_from_db(db: Session, user: User, request: Request) -> bool:
    enabled = is_agent_tracking_enabled(db, user, request)
    request.session["agent_tracking_enabled"] = enabled
    return enabled
