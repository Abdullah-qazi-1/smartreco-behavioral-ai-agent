"""
Thin wrapper around the centralized scoring engine.

This file preserves the legacy import point for catalog sort bias and
product listing logic, but actual profile scoring is centralized in
services/scoring_engine.py.
"""
from typing import List

from sqlalchemy.orm import Session

from database.models import User
from services.scoring_engine import build_category_profile


def get_dominant_categories(db: Session, user: User, top_n: int = 2) -> List[str]:
    profile = build_category_profile(db, user)
    if not profile:
        return []
    ranked = sorted(profile.items(), key=lambda kv: kv[1], reverse=True)
    return [category for category, _ in ranked[:top_n]]
