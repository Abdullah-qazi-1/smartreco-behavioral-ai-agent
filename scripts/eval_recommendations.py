"""
scripts/eval_recommendations.py — Evaluation script for SmartReco Recommendation Quality.

Evaluates recommendation pipeline behavior against 3 synthetic user profiles:
  1. Profile A: User who only viewed 2 products in one category (Data Science).
  2. Profile B: User with mixed/conflicting category signals (Web Dev + Cloud/DevOps).
  3. Profile C: Cold-start user with zero events.

Prints recommended products and generated narrative for verification without needing UI interaction.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

# Ensure project root is in python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy.orm import Session
from database.db import SessionLocal
from database.models import User, UserProfile, Event, Product, Recommendation
from services.agent_graph import run_recommendation_pipeline

def create_synthetic_user(db: Session, name: str, email: str, level: str = "Beginner", interests: str = "") -> User:
    """Helper to get or create a synthetic test user in database."""
    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(
            name=name,
            email=email,
            password_hash="test-hash",
            role="user",
            active_mode="student",
            experience_level=level,
            interests=interests,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        profile = UserProfile(user_id=user.id, agent_tracking_enabled=True)
        db.add(profile)
        db.commit()
    else:
        user.name = name
        user.experience_level = level
        user.interests = interests
        db.commit()
        db.refresh(user)
        if not user.profile:
            profile = UserProfile(user_id=user.id, agent_tracking_enabled=True)
            db.add(profile)
            db.commit()
        else:
            user.profile.agent_tracking_enabled = True
            db.commit()
    return user



def seed_synthetic_events(db: Session, user: User, event_specs: list):
    """Clears past test events for user and seeds synthetic test fixtures."""
    db.query(Event).filter(Event.user_id == user.id).delete()
    db.commit()

    now = datetime.now(timezone.utc)
    for idx, spec in enumerate(event_specs):
        created_at = now - timedelta(minutes=spec.get("minutes_ago", (len(event_specs) - idx) * 10))
        meta = {}
        if "seconds" in spec:
            meta["seconds"] = spec["seconds"]
        if "query" in spec:
            meta["query"] = spec["query"]

        import json
        event = Event(
            user_id=user.id,
            event_type=spec["type"],
            product_id=spec.get("product_id"),
            agent_eligible=True,
            event_metadata=json.dumps(meta) if meta else None,
            created_at=created_at,
        )
        db.add(event)
    db.commit()


def run_evaluation():
    db = SessionLocal()
    print("=" * 80)
    print("SMARTRECO RECOMMENDATION QUALITY EVALUATION SUITE")
    print("=" * 80)

    try:
        # Fetch some products from DB for catalog context
        sample_ds = db.query(Product).filter(Product.category == "Data Science").first()
        sample_web = db.query(Product).filter(Product.category == "Web Development").first()
        sample_cloud = db.query(Product).filter(Product.category == "Cloud & DevOps").first()

        p_ds_id = sample_ds.id if sample_ds else 1
        p_web_id = sample_web.id if sample_web else 2
        p_cloud_id = sample_cloud.id if sample_cloud else 3

        # ---------------------------------------------------------------------
        # TEST CASE 1: Single Category User (2 views in Data Science)
        # ---------------------------------------------------------------------
        print("\n[TEST CASE 1] Profile A: Single Category User (2 Views in Data Science)")
        user1 = create_synthetic_user(db, "eval_user_a", "eval_user_a@test.com", level="Beginner", interests="Data Science")
        
        events_a = [
            {"type": "view", "product_id": p_ds_id, "seconds": 45, "minutes_ago": 30},
            {"type": "view", "product_id": p_ds_id, "seconds": 60, "minutes_ago": 15},
            {"type": "view", "product_id": p_ds_id, "seconds": 90, "minutes_ago": 5},
            {"type": "search", "query": "python data science", "minutes_ago": 4},
            {"type": "click", "product_id": p_ds_id, "minutes_ago": 2},
            {"type": "view", "product_id": p_ds_id, "seconds": 120, "minutes_ago": 1},
        ]
        seed_synthetic_events(db, user1, events_a)

        rec1 = run_recommendation_pipeline(db, user1, force=True)
        print(f"-> Generated Recommendation ID: {rec1.id if rec1 else 'None'}")
        if rec1:
            print(f"-> Trigger Reason: {rec1.trigger_reason}")
            import json
            payload1 = json.loads(rec1.narrative)
            print(f"-> Narrative Payload Keys: {list(payload1.keys())}")
            if "main" in payload1:
                print(f"-> Main Narrative:\n   \"{payload1['main']['narrative']}\"")
                print(f"-> Main Product IDs: {payload1['main']['product_ids']}")

        # ---------------------------------------------------------------------
        # TEST CASE 2: Mixed/Conflicting Category Signals (Web Dev + Cloud)
        # ---------------------------------------------------------------------
        print("\n[TEST CASE 2] Profile B: Mixed Signals (Web Development + Cloud & DevOps)")
        user2 = create_synthetic_user(db, "eval_user_b", "eval_user_b@test.com", level="Intermediate", interests="Web Development, Cloud")
        
        events_b = [
            {"type": "view", "product_id": p_web_id, "seconds": 40, "minutes_ago": 50},
            {"type": "search", "query": "react javascript", "minutes_ago": 45},
            {"type": "view", "product_id": p_web_id, "seconds": 80, "minutes_ago": 40},
            {"type": "search", "query": "aws cloud architecture", "minutes_ago": 30},
            {"type": "view", "product_id": p_cloud_id, "seconds": 110, "minutes_ago": 20},
            {"type": "click", "product_id": p_cloud_id, "minutes_ago": 10},
        ]
        seed_synthetic_events(db, user2, events_b)

        rec2 = run_recommendation_pipeline(db, user2, force=True)
        print(f"-> Generated Recommendation ID: {rec2.id if rec2 else 'None'}")
        if rec2:
            print(f"-> Trigger Reason: {rec2.trigger_reason}")
            import json
            payload2 = json.loads(rec2.narrative)
            print(f"-> Narrative Payload Keys: {list(payload2.keys())}")
            if "main" in payload2:
                print(f"-> Main Narrative:\n   \"{payload2['main']['narrative']}\"")
                print(f"-> Main Product IDs: {payload2['main']['product_ids']}")
            if "search_intent" in payload2:
                print(f"-> Search Intent Narrative:\n   \"{payload2['search_intent']['narrative']}\"")
                print(f"-> Search Intent Query: {payload2['search_intent'].get('query')}")

        # ---------------------------------------------------------------------
        # TEST CASE 3: Cold-Start User (0 events)
        # ---------------------------------------------------------------------
        print("\n[TEST CASE 3] Profile C: Cold-Start User (0 Events)")
        user3 = create_synthetic_user(db, "eval_user_c", "eval_user_c@test.com", level="Beginner", interests="")
        seed_synthetic_events(db, user3, [])

        rec3 = run_recommendation_pipeline(db, user3, force=True)
        print(f"-> Generated Recommendation ID: {rec3.id if rec3 else 'None'}")
        if rec3:
            print(f"-> Trigger Reason: {rec3.trigger_reason}")
            import json
            payload3 = json.loads(rec3.narrative)
            print(f"-> Narrative Payload Keys: {list(payload3.keys())}")
            if "main" in payload3:
                print(f"-> Main Narrative:\n   \"{payload3['main']['narrative']}\"")
                print(f"-> Main Product IDs: {payload3['main']['product_ids']}")

        print("\n" + "=" * 80)
        print("EVALUATION COMPLETED SUCCESSFULLY")
        print("=" * 80)

    finally:
        db.close()


if __name__ == "__main__":
    run_evaluation()
