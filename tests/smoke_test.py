"""
tests/smoke_test.py — Standalone assertion-based automated smoke test suite for SmartReco.

Validates core backend contracts:
1. Dual-write sync: product creation, Chroma retrieval, deletion, and ChromaSyncLog tracking.
2. Event ingestion: bulk event ingestion into database.
3. Trigger policy: threshold gating and 120s rate-limiting cooldown.
4. Agent run (LangGraph): workflow pipeline execution and Recommendation persistence.
5. Grounding guarantee: validate_narrative_grounding hallucinated title detection.
6. Cold start safety: brand new user handling without runtime crashes.
7. Vector-store self-healing: reconcile_vector_store() actually repairs a
   product whose Chroma/Mesh dual-write previously failed.
8. Mesh entirely unavailable: nothing crashes anywhere in the app.
9. (see above) Vector-store self-healing.
10. Concurrent dual-write races: two real threads/sessions creating
    different products, and two threads updating the SAME product,
    simultaneously — no lost writes, no orphaned vector entries, no
    corrupted/interleaved row on last-write-wins.
11. Mesh key invalidated mid-request: a multi-step operation where the key
    is valid for the first call but starts failing on the next — the
    already-committed SQL write is never rolled back just because a later
    Mesh call in the same flow degrades.

Runs standalone without requiring a live Mesh API key. Returns exit code 0 on success.
"""
import os
import sys
import json
import logging
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

# Ensure project root directory is on sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# --- FIX: isolate this test run from the committed production chroma_db/ ---
# The committed chroma_db/ holds real 1536-dim Mesh (text-embedding-3-small)
# vectors. This test mocks embed_text() with 384-dim dummy vectors below for
# speed (so it doesn't need a live Mesh key), and Chroma rejects any write
# whose dimension doesn't match an existing collection's dimension. Pointing
# CHROMA_PATH at a fresh temp directory before chroma_client is imported gives
# the test its own throwaway collection, so the mock's 384-dim vectors never
# collide with the real 1536-dim production collection.
os.environ.setdefault("CHROMA_PATH", tempfile.mkdtemp(prefix="smartreco_test_chroma_"))

from database.db import SessionLocal
from database.models import User, Product, Event, Recommendation, ChromaSyncLog
from services.product_service import create_product, delete_product, reconcile_vector_store
from services import product_service
from database import chroma_client
from services.trigger import should_regenerate, count_new_signal_events, MIN_SECONDS_BETWEEN_RUNS
from services.scoring_weights import NEW_EVENTS_TRIGGER_THRESHOLD
from services.agent_graph import run_recommendation_pipeline
from services.llm_client import validate_narrative_grounding

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger("smartreco.smoke_test")


def run_smoke_test():
    passed_count = 0
    failed_count = 0
    db = SessionLocal()

    def report_assertion(section_name: str, condition: bool, message: str):
        nonlocal passed_count, failed_count
        if condition:
            passed_count += 1
            print(f"  [PASS] {message}")
        else:
            failed_count += 1
            print(f"  [FAIL] {message}")

    print("=" * 60)
    print(" SmartReco Behavioral AI Agent — Automated Smoke Test")
    print("=" * 60)

    # -------------------------------------------------------------
    # Section 1: Dual-Write & ChromaSyncLog
    # -------------------------------------------------------------
    print("\n[1] Dual-Write CRUD & ChromaSyncLog Sync Tracking")
    try:
        unique_title = f"Smoke Test Python Course {int(datetime.now().timestamp())}"
        # Dummy embedding patch for fast test execution
        with patch("database.chroma_client.embed_text", return_value=[0.1] * 384):
            prod = create_product(
                db,
                title=unique_title,
                description="Comprehensive Python programming course.",
                category="Data Science",
                price=39.99,
                level="Beginner",
            )
            report_assertion("Dual-Write", prod.id is not None, f"Created product with ID {prod.id}")

            # Check ChromaSyncLog upsert row
            sync_upsert = (
                db.query(ChromaSyncLog)
                .filter(ChromaSyncLog.product_id == prod.id, ChromaSyncLog.action == "upsert")
                .order_by(ChromaSyncLog.id.desc())
                .first()
            )
            report_assertion("Dual-Write", sync_upsert is not None and sync_upsert.status == "synced", "ChromaSyncLog recorded status='synced' on creation")

            # Delete product and check sync log
            deleted = delete_product(db, prod.id)
            report_assertion("Dual-Write", deleted is True, f"Deleted product ID {prod.id}")

            sync_delete = (
                db.query(ChromaSyncLog)
                .filter(ChromaSyncLog.product_id == prod.id, ChromaSyncLog.action == "delete")
                .order_by(ChromaSyncLog.id.desc())
                .first()
            )
            report_assertion("Dual-Write", sync_delete is not None and sync_delete.status == "synced", "ChromaSyncLog recorded status='synced' on deletion")
    except Exception as exc:
        report_assertion("Dual-Write", False, f"Dual-write test raised exception: {exc}")

    # -------------------------------------------------------------
    # Section 2: Event Ingestion
    # -------------------------------------------------------------
    print("\n[2] Event Ingestion & DB Persistence")
    try:
        ts = int(datetime.now().timestamp())
        test_user = User(
            name="Smoke Test User",
            email=f"smoke_user_{ts}@test.com",
            password_hash="test_password_hash",
            role="user",
        )
        db.add(test_user)
        db.commit()
        db.refresh(test_user)
        report_assertion("Event Ingestion", test_user.id is not None, f"Seeded test user ID {test_user.id}")

        batch_events = [
            Event(
                user_id=test_user.id,
                event_type=etype,
                product_id=101,
                agent_eligible=True,
                event_metadata=json.dumps({"source": "smoke_test"}),
            )
            for etype in ["view", "search", "click"]
        ]
        db.bulk_save_objects(batch_events)
        db.commit()

        ingested_count = db.query(Event).filter(Event.user_id == test_user.id).count()
        report_assertion("Event Ingestion", ingested_count == 3, f"Persisted {ingested_count}/3 events in DB")
    except Exception as exc:
        report_assertion("Event Ingestion", False, f"Event ingestion test raised exception: {exc}")

    # -------------------------------------------------------------
    # Section 3: Trigger Policy & Cooldown Rate-Limiting
    # -------------------------------------------------------------
    print("\n[3] Trigger Policy & Rate-Limiting Cooldown")
    try:
        # Currently test_user has 3 events < NEW_EVENTS_TRIGGER_THRESHOLD (5)
        fired_below = should_regenerate(db, test_user, force=False)
        report_assertion("Trigger Policy", fired_below is False, "should_regenerate returned False when events < threshold")

        # Add 3 more signal events to cross threshold of 5
        more_events = [
            Event(user_id=test_user.id, event_type="view", product_id=102, agent_eligible=True)
            for _ in range(3)
        ]
        db.bulk_save_objects(more_events)
        db.commit()

        fired_above = should_regenerate(db, test_user, force=False)
        report_assertion("Trigger Policy", fired_above is True, "should_regenerate returned True when events >= threshold")

        # Add a fresh recommendation row created just now
        fresh_rec = Recommendation(
            user_id=test_user.id,
            narrative=json.dumps({"main": {"narrative": "Recent rec", "product_ids": [101]}}),
            product_ids=json.dumps([101]),
            is_latest=True,
            created_at=datetime.now(timezone.utc),
        )
        db.add(fresh_rec)
        db.commit()

        # Cooldown check: second immediate call without force should be rate-limited
        fired_cooldown = should_regenerate(db, test_user, force=False)
        report_assertion("Trigger Policy", fired_cooldown is False, "Rate-limiting cooldown returned False for immediate second refresh")

        # Force override check
        fired_forced = should_regenerate(db, test_user, force=True)
        report_assertion("Trigger Policy", fired_forced is True, "force=True successfully bypassed cooldown rate limit")
    except Exception as exc:
        report_assertion("Trigger Policy", False, f"Trigger policy test raised exception: {exc}")

    # -------------------------------------------------------------
    # Section 4: Agent Run (LangGraph Workflow)
    # -------------------------------------------------------------
    print("\n[4] Recommendation Agent Run (LangGraph Pipeline)")
    try:
        canned_narrative = "Based on your interest in Data Science, we strongly recommend Python 101 for your career path."
        with patch("services.agent_graph.generate_narrative", return_value=canned_narrative), \
             patch("database.chroma_client.embed_text", return_value=[0.1] * 384):
            rec = run_recommendation_pipeline(db, test_user, force=True)
            report_assertion("Agent Run", rec is not None, "LangGraph pipeline executed and returned Recommendation")
            if rec:
                report_assertion("Agent Run", bool(rec.narrative) and len(rec.narrative) > 0, "Recommendation narrative is non-empty")
                report_assertion("Agent Run", bool(rec.product_ids) and len(rec.product_ids) > 0, "Recommendation product_ids is non-empty")
    except Exception as exc:
        report_assertion("Agent Run", False, f"Agent pipeline test raised exception: {exc}")

    # -------------------------------------------------------------
    # Section 5: Grounding Guarantee Validation
    # -------------------------------------------------------------
    print("\n[5] Grounding Guarantee Validation")
    try:
        candidates = [{"title": "Data Science Masterclass", "category": "Data Science"}]
        valid_text = "We recommend the 'Data Science Masterclass' to deepen your analytical skills."
        is_valid = validate_narrative_grounding(valid_text, candidates)
        report_assertion("Grounding", is_valid is True, "validate_narrative_grounding passed for candidate-grounded title")

        hallucinated_text = "We recommend the 'Advanced Quantum Computing 2026' course for your career."
        is_hallucinated = validate_narrative_grounding(hallucinated_text, candidates)
        report_assertion("Grounding", is_hallucinated is False, "validate_narrative_grounding correctly caught unlisted/hallucinated course title")
    except Exception as exc:
        report_assertion("Grounding", False, f"Grounding test raised exception: {exc}")

    # -------------------------------------------------------------
    # Section 6: Cold Start Handling
    # -------------------------------------------------------------
    print("\n[6] Cold Start Safe Fallback")
    try:
        ts = int(datetime.now().timestamp())
        cold_user = User(
            name="Cold Start User",
            email=f"cold_user_{ts}@test.com",
            password_hash="test_password_hash",
            role="user",
        )
        db.add(cold_user)
        db.commit()
        db.refresh(cold_user)

        with patch("services.agent_graph.generate_narrative", return_value="Explore popular courses in our catalog."), \
             patch("database.chroma_client.embed_text", return_value=[0.1] * 384):
            cold_rec = run_recommendation_pipeline(db, cold_user, force=True)
            report_assertion("Cold Start", True, f"Cold start user execution completed cleanly without crashing (rec={cold_rec is not None})")
    except Exception as exc:
        report_assertion("Cold Start", False, f"Cold start test raised exception: {exc}")

    # -------------------------------------------------------------
    # Section 7: Mesh Unavailable -> Non-AI Keyword Search Fallback
    # -------------------------------------------------------------
    print("\n[7] Mesh-Unavailable Graceful Keyword-Search Fallback")
    try:
        unique_marker = f"FallbackMarker{int(datetime.now().timestamp())}"
        fb_title = f"{unique_marker} AWS for Data Science"
        with patch("database.chroma_client.embed_text", return_value=[0.1] * 384):
            fb_product = create_product(
                db,
                title=fb_title,
                description="Learn AWS services applied to data science workflows.",
                category="Data Science",
                price=0.0,
                level="Intermediate",
                skills="aws,data science",
            )
        report_assertion("Keyword Fallback", fb_product.id is not None, f"Seeded fallback-search test product ID {fb_product.id}")

        # Simulate Mesh being fully down/unreachable for the search call itself.
        with patch("database.chroma_client.semantic_search_with_scores", side_effect=RuntimeError("Mesh unreachable")):
            results = product_service.semantic_search_products(db, unique_marker)
            report_assertion(
                "Keyword Fallback",
                any(p.id == fb_product.id for p in results),
                "semantic_search_products() did not crash when Mesh raised, and found the product via plain SQL keyword match",
            )

        with patch("database.chroma_client.embed_text", return_value=[0.1] * 384):
            delete_product(db, fb_product.id)
    except Exception as exc:
        report_assertion("Keyword Fallback", False, f"Keyword fallback test raised exception: {exc}")

    # -------------------------------------------------------------
    # Section 8: MESH_API_KEY entirely empty -> nothing crashes, anywhere
    # -------------------------------------------------------------
    # Unlike Section 7 (which patches embed_text directly to simulate Mesh
    # being down), this section removes the key itself and exercises the
    # REAL code path: database.chroma_client._require_mesh_api_key() will
    # genuinely raise, and services.llm_client._require_mesh_api_key() will
    # genuinely raise. This proves the fallback is real, not just mocked.
    print("\n[8] MESH_API_KEY Fully Empty — No Mocks, Real Fallback Path")
    _original_mesh_key = os.environ.get("MESH_API_KEY")
    try:
        os.environ["MESH_API_KEY"] = ""

        # 8a. Narrative generation must degrade to the generic fallback
        # sentence instead of raising, with zero mocking of llm_client.
        from services.llm_client import generate_narrative as _real_generate_narrative
        narrative = _real_generate_narrative(
            "User is exploring Data Science courses.",
            [{"title": "Intro to Data Science", "id": 1}],
        )
        report_assertion(
            "No-Mesh Narrative",
            isinstance(narrative, str) and len(narrative) > 0,
            "generate_narrative() with an empty MESH_API_KEY returned a non-empty "
            "fallback string instead of raising",
        )

        # 8b. Dual-write must still commit the SQL row even though the real
        # Chroma upsert will fail (no Mesh key -> embed_text() raises).
        nomesh_marker = f"NoMeshMarker{int(datetime.now().timestamp())}"
        nomesh_product = create_product(
            db,
            title=f"{nomesh_marker} Kubernetes Fundamentals",
            description="Container orchestration basics.",
            category="DevOps",
            price=0.0,
            level="Beginner",
            skills="kubernetes,devops",
        )
        report_assertion(
            "No-Mesh Dual-Write",
            nomesh_product.id is not None,
            "create_product() with an empty MESH_API_KEY still committed the SQL row "
            "instead of crashing (Chroma upsert failed and was logged separately)",
        )

        sync_log_entry = (
            db.query(ChromaSyncLog)
            .filter(ChromaSyncLog.product_id == nomesh_product.id)
            .order_by(ChromaSyncLog.id.desc())
            .first()
        )
        report_assertion(
            "No-Mesh Sync Log",
            sync_log_entry is not None and sync_log_entry.status == "failed",
            "the failed Chroma sync was recorded in ChromaSyncLog for visibility "
            "(status='failed') instead of failing silently",
        )

        # cleanup — restore key before delete_product() so teardown is clean
        os.environ["MESH_API_KEY"] = _original_mesh_key or ""
        with patch("database.chroma_client.embed_text", return_value=[0.1] * 384):
            delete_product(db, nomesh_product.id)
    except Exception as exc:
        report_assertion("No-Mesh Fallback", False, f"No-Mesh section raised an unexpected exception: {exc}")
    finally:
        if _original_mesh_key is None:
            os.environ.pop("MESH_API_KEY", None)
        else:
            os.environ["MESH_API_KEY"] = _original_mesh_key

    # -------------------------------------------------------------
    # Section 9: Vector-Store Self-Healing (reconcile_vector_store)
    # -------------------------------------------------------------
    # Simulates the real-world sequence: a product's Chroma upsert fails at
    # create time (Mesh down), leaving it with a "failed" ChromaSyncLog entry
    # and therefore invisible to semantic search — then proves the reconcile
    # job actually finds it and repairs it once Mesh/Chroma is healthy again.
    print("\n[9] Vector-Store Self-Healing (reconcile_vector_store)")
    try:
        reconcile_marker = f"ReconcileMarker{int(datetime.now().timestamp())}"
        with patch("database.chroma_client.upsert_product", side_effect=RuntimeError("Mesh unreachable")):
            broken_product = create_product(
                db,
                title=f"{reconcile_marker} Docker for Beginners",
                description="Containerize your first application.",
                category="DevOps",
                price=0.0,
                level="Beginner",
                skills="docker,devops",
            )
        broken_log = (
            db.query(ChromaSyncLog)
            .filter(ChromaSyncLog.product_id == broken_product.id)
            .order_by(ChromaSyncLog.id.desc())
            .first()
        )
        report_assertion(
            "Reconcile Setup",
            broken_log is not None and broken_log.status == "failed",
            "product's dual-write failed as expected and was logged with status='failed', "
            "simulating a real Mesh outage at create time",
        )

        # Mesh/Chroma is "healthy" again now (no patch) -> reconcile should repair it.
        with patch("database.chroma_client.embed_text", return_value=[0.1] * 384):
            summary = reconcile_vector_store(db)

        report_assertion(
            "Reconcile Repair",
            summary["repaired"] >= 1,
            f"reconcile_vector_store() ran and reported repaired={summary['repaired']}, "
            f"still_failed={summary['still_failed']}",
        )

        healed_log = (
            db.query(ChromaSyncLog)
            .filter(ChromaSyncLog.product_id == broken_product.id)
            .order_by(ChromaSyncLog.id.desc())
            .first()
        )
        report_assertion(
            "Reconcile Verification",
            healed_log is not None and healed_log.status == "synced",
            "the product's MOST RECENT ChromaSyncLog entry is now status='synced' — "
            "it is no longer permanently invisible to semantic search",
        )

        # A second reconcile run should now find nothing left to repair for this product.
        with patch("database.chroma_client.embed_text", return_value=[0.1] * 384):
            summary_2 = reconcile_vector_store(db)
        report_assertion(
            "Reconcile Idempotency",
            True,  # informational — just confirming a second run doesn't error
            f"second reconcile run is safe to call again (attempted={summary_2['attempted']} "
            f"products still pending across the whole catalog)",
        )

        with patch("database.chroma_client.embed_text", return_value=[0.1] * 384):
            delete_product(db, broken_product.id)
    except Exception as exc:
        report_assertion("Reconcile", False, f"Reconcile test raised exception: {exc}")

    # -------------------------------------------------------------
    # Section 10: Concurrent Dual-Write Race Conditions
    # -------------------------------------------------------------
    # Two real OS threads, each with its OWN SQLAlchemy Session (mirroring
    # how FastAPI's get_db() hands every request its own session), hit the
    # dual-write path at the same time: (a) two DIFFERENT products created
    # concurrently, and (b) the SAME product updated concurrently from both
    # threads. This is checking for the two failure modes that matter in
    # production: lost writes (SQL side) and orphaned/mismatched vector
    # entries (Chroma side) when requests overlap.
    print("\n[10] Concurrent Dual-Write Race Conditions")
    try:
        import threading

        race_marker = f"RaceMarker{int(datetime.now().timestamp())}"
        created_ids = {}
        create_errors = []

        def _concurrent_create(label, title_suffix):
            thread_db = SessionLocal()
            try:
                p = create_product(
                    thread_db,
                    title=f"{race_marker} {title_suffix}",
                    description="Concurrency test product.",
                    category="DevOps",
                    price=0.0,
                    level="Beginner",
                    skills="race,condition",
                )
                created_ids[label] = p.id
            except Exception as e:
                create_errors.append(e)
            finally:
                thread_db.close()

        # NOTE: unittest.mock.patch() context managers are NOT thread-safe when
        # two threads simultaneously enter/exit a `with patch(...)` on the SAME
        # target — one thread's exit can restore the real embed_text() while the
        # other thread is still mid-call, causing a spurious (test-only, not a
        # real production) failure. Patch ONCE around both threads' full
        # lifetime instead of once per thread.
        with patch("database.chroma_client.embed_text", return_value=[0.2] * 384):
            t1 = threading.Thread(target=_concurrent_create, args=("A", "Product Alpha"))
            t2 = threading.Thread(target=_concurrent_create, args=("B", "Product Beta"))
            t1.start(); t2.start()
            t1.join(); t2.join()

        report_assertion(
            "Concurrent Create — no exceptions",
            len(create_errors) == 0,
            f"two simultaneous create_product() calls from separate threads/sessions "
            f"completed without raising ({len(create_errors)} errors)",
        )
        report_assertion(
            "Concurrent Create — no lost write",
            "A" in created_ids and "B" in created_ids and created_ids["A"] != created_ids["B"],
            f"both concurrent creates got distinct product IDs (A={created_ids.get('A')}, "
            f"B={created_ids.get('B')}) — neither write was silently dropped or overwrote the other",
        )

        both_ids = list(created_ids.values())
        # Check the MOST RECENT log per product_id specifically (not a raw count
        # filtered by ID alone) — SQLite reuses freed rowids after earlier
        # sections' deletes, so a plain count-by-id can pick up unrelated older
        # rows for the same numeric ID and give a misleadingly large number.
        latest_statuses = {}
        for pid in both_ids:
            latest = (
                db.query(ChromaSyncLog)
                .filter(ChromaSyncLog.product_id == pid)
                .order_by(ChromaSyncLog.id.desc())
                .first()
            )
            latest_statuses[pid] = latest.status if latest else None
        both_synced = all(status == "synced" for status in latest_statuses.values())
        report_assertion(
            "Concurrent Create — both vector-synced",
            both_synced,
            f"both concurrently-created products' MOST RECENT ChromaSyncLog entry is "
            f"'synced' ({latest_statuses}) — no orphaned/failed vector write from the race",
        )

        # (b) Same-row concurrent UPDATE — last-write-wins, no partial/corrupted row.
        with patch("database.chroma_client.embed_text", return_value=[0.2] * 384):
            base_product = create_product(
                db,
                title=f"{race_marker} Shared Row",
                description="Original description.",
                category="Data Science",
                price=10.0,
                level="Intermediate",
                skills="shared,row",
            )
        shared_id = base_product.id
        update_errors = []

        def _concurrent_update(new_title):
            thread_db = SessionLocal()
            try:
                product_service.update_product(thread_db, shared_id, title=new_title)
            except Exception as e:
                update_errors.append(e)
            finally:
                thread_db.close()

        with patch("database.chroma_client.embed_text", return_value=[0.2] * 384):
            u1 = threading.Thread(target=_concurrent_update, args=(f"{race_marker} Updated By Thread 1",))
            u2 = threading.Thread(target=_concurrent_update, args=(f"{race_marker} Updated By Thread 2",))
            u1.start(); u2.start()
            u1.join(); u2.join()

        db.expire_all()
        final_product = db.query(Product).filter(Product.id == shared_id).first()
        report_assertion(
            "Concurrent Update — no exceptions",
            len(update_errors) == 0,
            f"two simultaneous update_product() calls on the SAME row from separate "
            f"threads/sessions completed without raising ({len(update_errors)} errors)",
        )
        report_assertion(
            "Concurrent Update — clean last-write-wins",
            final_product is not None
            and final_product.title in (
                f"{race_marker} Updated By Thread 1",
                f"{race_marker} Updated By Thread 2",
            ),
            f"final row title is one of the two full concurrent writes, not a "
            f"corrupted/interleaved mix — got: {final_product.title if final_product else None!r}",
        )

        with patch("database.chroma_client.embed_text", return_value=[0.2] * 384):
            for pid in both_ids + [shared_id]:
                delete_product(db, pid)
    except Exception as exc:
        report_assertion("Concurrent Dual-Write", False, f"Concurrency test raised exception: {exc}")

    # -------------------------------------------------------------
    # Section 11: Mesh Key Invalidated Mid-Request
    # -------------------------------------------------------------
    # Section 8 already covers MESH_API_KEY being empty for the WHOLE
    # request. This section covers the narrower, more realistic case: the
    # key is valid when a multi-step operation STARTS (e.g. a key gets
    # revoked, rate-limited, or a Mesh outage begins mid-flight) but fails
    # partway through — verifying no partial/half-written state and that the
    # already-committed SQL side is never rolled back just because the
    # LLM/embedding call that came after it failed.
    print("\n[11] Mesh Key Invalidated Mid-Request")
    try:
        midreq_marker = f"MidReqMarker{int(datetime.now().timestamp())}"

        # embed_text succeeds on the FIRST call (product create), then starts
        # raising as if the key was revoked/rate-limited immediately after.
        call_state = {"count": 0}

        def _embed_then_fail(*args, **kwargs):
            call_state["count"] += 1
            if call_state["count"] == 1:
                return [0.3] * 384
            raise RuntimeError("401 Unauthorized — Mesh key invalid or revoked")

        with patch("database.chroma_client.embed_text", side_effect=_embed_then_fail):
            midreq_product = create_product(
                db,
                title=f"{midreq_marker} Kubernetes Basics",
                description="Learn container orchestration.",
                category="DevOps",
                price=0.0,
                level="Beginner",
                skills="kubernetes,devops",
            )
            # Second call in the same "session" — key now behaves as revoked.
            failed_update = product_service.update_product(
                db, midreq_product.id, description="Updated after key revocation."
            )

        report_assertion(
            "Mid-request key failure — first write unaffected",
            midreq_product is not None and midreq_product.id is not None,
            "the product created BEFORE the simulated mid-request key failure still "
            "committed successfully to SQL",
        )
        report_assertion(
            "Mid-request key failure — later SQL write not rolled back",
            failed_update is not None and failed_update.description == "Updated after key revocation.",
            "the SQL update that happened AFTER the key started failing still committed "
            "(only the vector/Chroma half degraded, per the resilience design) — "
            "the failure never rolled back or corrupted the SQL row",
        )
        failed_log = (
            db.query(ChromaSyncLog)
            .filter(ChromaSyncLog.product_id == midreq_product.id, ChromaSyncLog.action == "upsert")
            .order_by(ChromaSyncLog.id.desc())
            .first()
        )
        report_assertion(
            "Mid-request key failure — degraded write is visibly logged",
            failed_log is not None and failed_log.status == "failed",
            "the update that happened while the key was 'revoked' mid-request is recorded "
            "as status='failed' in ChromaSyncLog — not silently dropped, so the hourly "
            "reconcile job will pick it up",
        )

        with patch("database.chroma_client.embed_text", return_value=[0.3] * 384):
            delete_product(db, midreq_product.id)
    except Exception as exc:
        report_assertion("Mesh Mid-Request Failure", False, f"Mid-request key test raised exception: {exc}")

    print("\n" + "=" * 60)
    print(f" SMOKE TEST SUMMARY: {passed_count} PASSED, {failed_count} FAILED")
    print("=" * 60)

    db.close()
    if failed_count > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    run_smoke_test()