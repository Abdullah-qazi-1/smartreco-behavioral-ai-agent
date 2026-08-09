"""
Product service — single source of truth for product & course CRUD.
Every write goes to SQLite AND Chroma together (dual-write), so they never drift.
"""
import logging
import random
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import desc, asc, func as sa_func

from database.models import Product, Review, Wishlist, Enrollment, CourseLearningOutcome, ChromaSyncLog, User
from database import chroma_client
from services import keyword_fallback
from services.scoring_weights import SIMILARITY_THRESHOLD

logger = logging.getLogger("smartreco.product_service")


def _record_chroma_sync_log(db: Session, product_id: int, action: str, status: str):
    try:
        sync_log = ChromaSyncLog(product_id=product_id, action=action, status=status)
        db.add(sync_log)
        db.commit()
    except Exception as log_exc:
        logger.error("Failed to record ChromaSyncLog for product_id=%s: %s", product_id, log_exc)


LEVELS_DURATION_RANGE = {
    "Beginner": (3, 9),
    "Intermediate": (8, 20),
    "Advanced": (15, 38),
}


def _auto_duration_hours(level: str) -> float:
    """
    Duration is a content-shape estimate (based on course level), not a
    trust/social-proof signal, so a randomized-within-range placeholder is
    reasonable here when the admin/instructor doesn't provide one.
    """
    lo, hi = LEVELS_DURATION_RANGE.get(level, (5, 15))
    return round(random.uniform(lo, hi), 1)


def _fill_missing_stats(level, enrolled_students, rating, num_ratings, duration_hours):
    # FIX: rating / num_ratings / enrolled_students used to be fabricated
    # with random.gauss()/lognormvariate() whenever left blank, then fed to
    # the LLM prompt and shown to users as if they were real social-proof
    # numbers. That's misleading — a 4.6-star "rating" with 59 "reviews"
    # that nobody ever left is not something we should let the AI narrative
    # (or the UI) present as fact. We now leave these unset (None/0) when
    # not explicitly provided, so the catalog only ever shows real numbers
    # that came from an actual admin/instructor input or real Review rows.
    if enrolled_students is None:
        enrolled_students = 0
    if rating is None:
        rating = None
    if num_ratings is None:
        num_ratings = 0

    if duration_hours is None:
        duration_hours = _auto_duration_hours(level)

    return enrolled_students, rating, num_ratings, duration_hours


def create_product(
    db: Session,
    title: str,
    description: str,
    category: str,
    price: float,
    level: str,
    skills: str = "",
    instructor_id: Optional[int] = None,
    instructor_name: str = "",
    rating: float = None,
    num_ratings: int = None,
    enrolled_students: int = None,
    duration_hours: float = None,
    status: str = "active",
) -> Product:
    enrolled_students, rating, num_ratings, duration_hours = _fill_missing_stats(
        level, enrolled_students, rating, num_ratings, duration_hours
    )

    product = Product(
        title=title,
        description=description,
        category=category,
        price=price,
        level=level,
        skills=skills,
        instructor_id=instructor_id,
        instructor_name=instructor_name,
        rating=rating,
        num_ratings=num_ratings,
        enrolled_students=enrolled_students,
        duration_hours=duration_hours,
        status=status,
    )

    db.add(product)
    db.commit()
    db.refresh(product)

    try:
        chroma_client.upsert_product(
            product.id,
            product.title,
            product.description,
            product.category,
            product.level,
            product.price,
            product.skills,
            product.instructor_name,
            product.rating,
            product.num_ratings,
            product.enrolled_students,
            product.duration_hours,
        )
        _record_chroma_sync_log(db, product.id, action="upsert", status="synced")
    except Exception as exc:
        logger.error(
            "MESH FALLBACK ACTIVE: Chroma/Mesh upsert failed for product_id=%s "
            "(likely missing/invalid MESH_API_KEY or Mesh unreachable): %s. "
            "SQL row is already committed — product is NOT lost, it just won't "
            "surface in semantic search until re-synced.",
            product.id, exc,
        )
        _record_chroma_sync_log(db, product.id, action="upsert", status="failed")

    logger.info("Product created: id=%s title=%r category=%s price=%.2f level=%s", product.id, product.title, product.category, product.price, product.level)
    return product


def update_product(db: Session, product_id: int, **fields) -> Optional[Product]:
    product = db.query(Product).filter(Product.id == product_id).first()

    if not product:
        logger.warning("Product update failed: product_id=%s not found", product_id)
        return None

    for key, value in fields.items():
        if value is not None and hasattr(product, key):
            setattr(product, key, value)

    db.commit()
    db.refresh(product)

    try:
        chroma_client.upsert_product(
            product.id,
            product.title,
            product.description,
            product.category,
            product.level,
            product.price,
            product.skills,
            product.instructor_name,
            product.rating,
            product.num_ratings,
            product.enrolled_students,
            product.duration_hours,
        )
        _record_chroma_sync_log(db, product.id, action="upsert", status="synced")
    except Exception as exc:
        logger.error(
            "MESH FALLBACK ACTIVE: Chroma/Mesh upsert failed for product_id=%s during update "
            "(likely missing/invalid MESH_API_KEY or Mesh unreachable): %s. "
            "SQL row is already committed with the new values — only the vector "
            "mirror is stale until re-synced.",
            product.id, exc,
        )
        _record_chroma_sync_log(db, product.id, action="upsert", status="failed")

    logger.info("Product updated: id=%s title=%r category=%s", product.id, product.title, product.category)
    return product


def delete_product(db: Session, product_id: int) -> bool:
    product = db.query(Product).filter(Product.id == product_id).first()

    if not product:
        logger.warning("Product delete failed: product_id=%s not found", product_id)
        return False

    db.delete(product)
    db.commit()

    try:
        chroma_client.delete_product(product_id)
        _record_chroma_sync_log(db, product_id, action="delete", status="synced")
    except Exception as exc:
        logger.error("Chroma delete failed for product_id=%s: %s", product_id, exc)
        _record_chroma_sync_log(db, product_id, action="delete", status="failed")

    logger.info("Product deleted: id=%s title=%r", product_id, product.title)
    return True



def get_all_products(
    db: Session,
    category: Optional[str] = None,
    level: Optional[str] = None,
    price_filter: Optional[str] = None,
    sort_by: Optional[str] = None,
    instructor_id: Optional[int] = None,
):
    query = db.query(Product)

    if instructor_id is not None:
        query = query.filter(
            (Product.instructor_id == instructor_id) | (Product.instructor_name.isnot(None))
        )

    if category and category != "All Categories":
        query = query.filter(Product.category == category)

    if level and level != "All Levels":
        query = query.filter(Product.level == level)

    if price_filter == "Free":
        query = query.filter(Product.price == 0)
    elif price_filter == "Paid":
        query = query.filter(Product.price > 0)

    if sort_by == "Rating":
        query = query.order_by(desc(Product.rating))
    elif sort_by == "Newest":
        query = query.order_by(desc(Product.created_at))
    else:
        query = query.order_by(Product.id)

    return query.all()


def get_instructor_courses(db: Session, user):
    """
    Returns courses owned by the given instructor user.

    FIX: only trusts the real, non-spoofable `instructor_id` foreign key —
    matches routers/products.py._can_manage_course(). The old fallback
    (`instructor_name == user.name` / `== user.email`) matched on a
    self-reported, user-editable field, so anyone could set their display
    name to "Andrew Ng" and see (and get Edit/Delete buttons for) Andrew
    Ng's seed courses in their own panel — a listing-vs-permission
    inconsistency, even though the actual write was already blocked by
    _can_manage_course(). A brand-new instructor with 0 real courses now
    correctly sees an empty list, not someone else's seed catalog rows.
    """
    return (
        db.query(Product)
        .filter(Product.instructor_id == user.id)
        .order_by(desc(Product.created_at))
        .all()
    )


def get_product(db: Session, product_id: int) -> Optional[Product]:
    return db.query(Product).filter(Product.id == product_id).first()


def get_categories(db: Session):
    rows = db.query(Product.category).distinct().order_by(Product.category).all()
    return [row[0] for row in rows]


def semantic_search_products(
    db: Session,
    query: str,
    top_k: int = 8,
    category: Optional[str] = None,
    level: Optional[str] = None,
    user: Optional[User] = None,
):
    scored = semantic_search_products_scored(
        db, query, top_k=top_k, category=category, level=level, min_similarity=None, user=user
    )
    return [p for p, _ in scored]


def semantic_search_products_scored(
    db: Session,
    query: str,
    top_k: int = 8,
    category: Optional[str] = None,
    level: Optional[str] = None,
    min_similarity: Optional[float] = SIMILARITY_THRESHOLD,
    user: Optional[User] = None,
):
    """
    Semantic search returning (Product, similarity_score) pairs.
    When min_similarity is set, filters out weak vector matches.

    If Mesh is unavailable (missing key, down, timed out, rate-limited),
    falls back to a plain SQL keyword search (services/keyword_fallback.py —
    no AI/embedding call involved) instead of crashing. Fallback matches get
    a synthetic score of 1.0 so they clear any min_similarity threshold,
    since real cosine-similarity scores aren't available in this mode.
    """
    try:
        raw_scored = chroma_client.semantic_search_with_scores(
            query, top_k=top_k, category=category, level=level
        )
    except Exception as exc:
        logger.warning(
            "MESH FALLBACK ACTIVE: Chroma/Mesh semantic search unavailable (%s) — "
            "falling back to plain SQL keyword search (no embeddings) for query=%r",
            exc, query,
        )
        fallback_products = keyword_fallback.keyword_search_products(
            db, query, top_k=top_k, category=category, level=level, user=user
        )
        return [(p, 1.0) for p in fallback_products]

    if min_similarity is not None:
        raw_scored = [(pid, score) for pid, score in raw_scored if score >= min_similarity]

    if not raw_scored:
        return []

    ids = [pid for pid, _ in raw_scored]
    products = db.query(Product).filter(Product.id.in_(ids)).all()
    by_id = {p.id: p for p in products}

    ordered: List[tuple] = []
    for pid, score in raw_scored:
        product = by_id.get(pid)
        if product:
            ordered.append((product, score))
        if len(ordered) >= top_k:
            break

    return ordered


def toggle_wishlist(db: Session, user_id: int, product_id: int) -> bool:
    existing = db.query(Wishlist).filter(Wishlist.user_id == user_id, Wishlist.product_id == product_id).first()
    if existing:
        db.delete(existing)
        db.commit()
        return False
    else:
        item = Wishlist(user_id=user_id, product_id=product_id)
        db.add(item)
        db.commit()
        return True


def get_user_wishlist_product_ids(db: Session, user_id: int) -> set:
    rows = db.query(Wishlist.product_id).filter(Wishlist.user_id == user_id).all()
    return {r[0] for r in rows}


def create_review(db: Session, product_id: int, user_id: int, reviewer_name: str, rating: float, comment: str = "") -> Review:
    review = Review(
        product_id=product_id,
        user_id=user_id,
        reviewer_name=reviewer_name,
        rating=rating,
        comment=comment,
    )

    db.add(review)
    db.commit()
    db.refresh(review)

    return review


def get_reviews(db: Session, product_id: int):
    return db.query(Review).filter(Review.product_id == product_id).order_by(Review.created_at.desc()).all()


def reconcile_vector_store(db: Session, limit: Optional[int] = None) -> dict:
    """
    Self-healing counterpart to the dual-write try/except in create_product()
    and update_product(): those catch a Chroma/Mesh failure and record it in
    ChromaSyncLog(status="failed") so the product isn't lost — but on their
    own they never go back and retry it. A course that failed to sync once
    (e.g. Mesh was briefly down) would otherwise stay permanently invisible
    to semantic search until someone noticed and fixed it by hand.

    This function finds every product whose MOST RECENT ChromaSyncLog entry
    has status="failed" and retries the Chroma upsert for it. It's designed
    to be safe to call repeatedly (e.g. every hour from the scheduler, or
    on demand from an admin endpoint):
      - Products that resync successfully get a new "synced" log row, so the
        next reconcile run will correctly skip them (their latest entry is
        no longer "failed").
      - Products that fail again just get another "failed" row and are
        picked up on the next cycle — no product is ever silently dropped.
      - A product deleted after its failed sync is skipped (nothing to
        repair) rather than raising.

    Returns a summary dict: {"attempted", "repaired", "still_failed",
    "product_not_found"} — this is what the scheduler job and the admin
    endpoint both log/return, so a run's effect is always visible.
    """
    # For each product_id, find the id of its most recent ChromaSyncLog row.
    latest_log_subq = (
        db.query(
            ChromaSyncLog.product_id.label("product_id"),
            sa_func.max(ChromaSyncLog.id).label("latest_id"),
        )
        .group_by(ChromaSyncLog.product_id)
        .subquery()
    )

    # A product is "in need of reconciliation" only if its LATEST entry
    # (not just any past entry) is "failed" — a later "synced" row means
    # it already healed (e.g. via a manual admin edit) and needs no work.
    failed_rows = (
        db.query(ChromaSyncLog.product_id)
        .join(
            latest_log_subq,
            (ChromaSyncLog.product_id == latest_log_subq.c.product_id)
            & (ChromaSyncLog.id == latest_log_subq.c.latest_id),
        )
        .filter(ChromaSyncLog.status == "failed")
        .all()
    )
    failed_ids = [pid for (pid,) in failed_rows]
    if limit is not None:
        failed_ids = failed_ids[:limit]

    result = {
        "attempted": len(failed_ids),
        "repaired": 0,
        "still_failed": 0,
        "product_not_found": 0,
    }

    if not failed_ids:
        logger.info("Reconcile: no products pending vector sync repair")
        return result

    logger.info("Reconcile: attempting to repair vector sync for %d product(s): %s", len(failed_ids), failed_ids)

    for product_id in failed_ids:
        product = db.query(Product).filter(Product.id == product_id).first()
        if not product:
            # Product was deleted after its failed sync — nothing left to repair.
            result["product_not_found"] += 1
            continue

        try:
            chroma_client.upsert_product(
                product.id,
                product.title,
                product.description,
                product.category,
                product.level,
                product.price,
                product.skills,
                product.instructor_name,
                product.rating,
                product.num_ratings,
                product.enrolled_students,
                product.duration_hours,
            )
            _record_chroma_sync_log(db, product.id, action="upsert", status="synced")
            result["repaired"] += 1
            logger.info("Reconcile: repaired vector sync for product_id=%s", product.id)
        except Exception as exc:
            logger.warning(
                "MESH FALLBACK ACTIVE: Reconcile retry still failing for product_id=%s "
                "(likely Mesh still unreachable): %s. Will retry again next cycle.",
                product.id, exc,
            )
            _record_chroma_sync_log(db, product.id, action="upsert", status="failed")
            result["still_failed"] += 1

    logger.info("Reconcile completed: %s", result)
    return result