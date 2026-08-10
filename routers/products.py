import json
import logging
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database.db import get_db
from database.models import Product, Enrollment, Wishlist, User, Event, Recommendation
from routers.auth import get_current_user
from services import product_service
from services.interest_profile import get_dominant_categories
from services.tracking_prefs import is_agent_tracking_enabled

logger = logging.getLogger("smartreco.products")
router = APIRouter()

MAX_SEARCH_QUERY_LENGTH = 200  # see rationale at the /api/search route below
templates = Jinja2Templates(directory="templates")



@router.get("/catalog", response_class=HTMLResponse)
@router.get("/products", response_class=HTMLResponse)
def list_products(
    request: Request,
    category: Optional[str] = None,
    level: Optional[str] = None,
    price: Optional[str] = None,
    sort: Optional[str] = None,
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    products = product_service.get_all_products(
        db, category=category, level=level, price_filter=price, sort_by=sort
    )
    categories = product_service.get_categories(db)
    wishlist_ids = product_service.get_user_wishlist_product_ids(db, user.id)

    if category is None or category == "All Categories":
        dominant = set(get_dominant_categories(db, user, top_n=2))
        if dominant:
            products = sorted(
                products,
                key=lambda p: 0 if p.category in dominant else 1,
            )

    return templates.TemplateResponse(
        request,
        "catalog.html",
        {
            "user": user,
            "active_page": "catalog",
            "products": products,
            "categories": categories,
            "active_category": category or "All Categories",
            "active_level": level or "All Levels",
            "active_price": price or "Price: All",
            "active_sort": sort or "Sort: Recommended",
            "wishlist_ids": wishlist_ids,
        },
    )


@router.get("/course-details", response_class=HTMLResponse)
@router.get("/products/{product_id}", response_class=HTMLResponse)
def product_detail(request: Request, product_id: Optional[int] = None, id: Optional[int] = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    target_id = product_id or id
    if not target_id:
        first_product = db.query(Product).order_by(Product.id).first()
        target_id = first_product.id if first_product else 1

    product = product_service.get_product(db, target_id)
    if not product:
        return RedirectResponse("/catalog", status_code=302)

    related = product_service.semantic_search_products(db, product.category, top_k=3)
    related = [r for r in related if r.id != product.id]

    wishlist_ids = product_service.get_user_wishlist_product_ids(db, user.id)
    is_saved = product.id in wishlist_ids

    enrolled = db.query(Enrollment).filter(
        Enrollment.user_id == user.id, Enrollment.product_id == product.id
    ).first()

    return templates.TemplateResponse(
        request,
        "course-details.html",
        {
            "user": user,
            "active_page": "catalog",
            "product": product,
            "related_products": related,
            "is_saved": is_saved,
            "is_enrolled": bool(enrolled),
        },
    )


@router.get("/api/search")
def search_products(q: str = "", request: Request = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # MAX_SEARCH_QUERY_LENGTH: an unbounded query string is wasted cost either way
    # it's handled — a huge string sent to Mesh for embedding burns tokens for no
    # retrieval benefit, and the keyword_fallback.py LIKE-query path scans just as
    # uselessly on garbage-length input. No real search intent needs more than a
    # couple hundred characters.
    q = q.strip()[:MAX_SEARCH_QUERY_LENGTH]

    if not q:
        products = product_service.get_all_products(db)
    else:
        products = product_service.semantic_search_products(db, q, user=user)

    return {"results": [p.to_dict() for p in products]}


@router.post("/api/wishlist/toggle")
def toggle_wishlist_route(
    request: Request,
    product_id: int = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    is_added = product_service.toggle_wishlist(db, user.id, product_id)
    return {"saved": is_added, "product_id": product_id}


# ---------------- Instructor / Creator Management ----------------

def _can_manage_course(user: User, product: Product) -> bool:
    """
    SECURITY (tightened further): ownership is now decided ONLY by the real,
    non-spoofable `instructor_id` foreign key.

    - If `product.instructor_id` is set and matches the logged-in user -> allowed.
    - If `product.instructor_id` is NULL (this only happens for the pre-existing
      seed/demo catalog rows that were created before real user accounts existed —
      they carry a free-text `instructor_name` like "Andrew Ng" for display only,
      with no real account behind it) -> ONLY an admin may manage them. No student
      or instructor-mode user can claim, edit, or delete a seed course, no matter
      what their display name is.
    - The previous `instructor_name`-equality fallback (added right after the
      substring-match exploit fix) has been removed entirely. It was still based
      on a self-reported, user-editable field (`user.name`) and was not a real
      identity check — only the FK is trustworthy. This does not affect any
      product created through the app today: `routers/products.py` ->
      `api_create_product()` always stamps `instructor_id=user.id` for new
      courses, so every course an instructor creates going forward is safely
      theirs and untouched by this change.
    """
    if not user or not product:
        return False
    if user.role == "admin":
        return True
    if product.instructor_id is None:
        # Seed/demo data with no real owner account — admin-only, by design.
        return False
    if getattr(user, "active_mode", "student") == "instructor":
        return product.instructor_id == user.id
    return False


@router.get("/admin", response_class=HTMLResponse)
@router.get("/admin", response_class=HTMLResponse)
def instructor_panel(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # SECURITY FIX: this used to silently flip active_mode to "instructor"
    # and persist it to the DB just because the user visited /admin — that
    # granted course-management access with zero explicit consent. Now we
    # only show the panel to users who are already admin or already in
    # instructor mode (set explicitly via the existing /switch-mode route);
    # everyone else is bounced with an explanation instead of being
    # auto-upgraded.
    if getattr(user, "active_mode", "student") != "instructor" and user.role != "admin":
        return templates.TemplateResponse(
            request,
            "admin.html",
            {
                "user": user,
                "active_page": "admin",
                "products": [],
                "categories": [],
                "total_users": 0,
                "total_courses": 0,
                "total_recs": 0,
                "total_events": 0,
                "needs_instructor_mode": True,
                # FIX (panel labeling): even the "not set up yet" placeholder
                # state must not claim to be an Admin Panel for a non-admin.
                "is_admin": False,
                "panel_label": "Instructor Panel",
            },
        )

    # FIX (panel labeling): role decides BOTH which courses load AND what the
    # panel calls itself, so an admin never sees "Instructor Panel" over a
    # full-catalog view, and an instructor never sees "Admin Panel" over a
    # view that's actually scoped to just their own courses. This removes
    # the confusion the two roles used to share a single generic title.
    is_admin = user.role == "admin"

    if is_admin:
        products = product_service.get_all_products(db)
    else:
        # instructor mode (non-admin): ID-based ownership only — see
        # get_instructor_courses() in services/product_service.py. A brand
        # new instructor with 0 real courses correctly sees an empty list
        # here, never someone else's courses.
        products = product_service.get_instructor_courses(db, user)

    categories = product_service.get_categories(db)

    total_users_cnt = db.query(User).count()
    total_courses_cnt = db.query(Product).count()
    total_recs_cnt = db.query(Recommendation).count()
    total_events_cnt = db.query(Event).count()

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "user": user,
            "active_page": "admin",
            "products": products,
            "categories": categories,
            "total_users": total_users_cnt,
            "total_courses": total_courses_cnt,
            "total_recs": total_recs_cnt,
            "total_events": total_events_cnt,
            "is_admin": is_admin,
            "panel_label": "Admin Panel" if is_admin else "Instructor Panel",
        },
    )



@router.post("/api/products")
def api_create_product(
    request: Request,
    db: Session = Depends(get_db),
    title: str = Form(...),
    description: str = Form(...),
    category: str = Form(...),
    price: float = Form(0.0),
    level: str = Form("Beginner"),
    skills: str = Form(""),
    instructor_name: str = Form(""),
    rating: float = Form(None),
    num_ratings: int = Form(None),
    duration_hours: float = Form(None),
):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # SECURITY FIX: previously ANY logged-in user (even plain "student" role,
    # never switched to instructor mode) could create catalog products via
    # this endpoint — there was no role/mode check at all. Only an admin, or
    # a user actively in instructor mode, may add courses now. This matches
    # the check already enforced on update/delete (_can_manage_course).
    is_instructor_mode = getattr(user, "active_mode", "student") == "instructor"
    if user.role != "admin" and not is_instructor_mode:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    ins_name = instructor_name.strip() if instructor_name else (user.name or user.email.split("@")[0])

    product = product_service.create_product(
        db=db,
        title=title,
        description=description,
        category=category,
        price=price,
        level=level,
        skills=skills,
        instructor_id=user.id,
        instructor_name=ins_name,
        rating=rating,
        num_ratings=num_ratings,
        duration_hours=duration_hours,
        status="active",
    )

    return {"id": product.id, "title": product.title}


@router.put("/api/products/{product_id}")
def api_update_product(
    product_id: int,
    request: Request,
    db: Session = Depends(get_db),
    title: str = Form(None),
    description: str = Form(None),
    category: str = Form(None),
    price: float = Form(None),
    level: str = Form(None),
    skills: str = Form(None),
    instructor_name: str = Form(None),
    rating: float = Form(None),
    num_ratings: int = Form(None),
    duration_hours: float = Form(None),
):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    product = product_service.get_product(db, product_id)
    if not product:
        return JSONResponse({"error": "not found"}, status_code=404)

    if not _can_manage_course(user, product):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    updated = product_service.update_product(
        db,
        product_id,
        title=title,
        description=description,
        category=category,
        price=price,
        level=level,
        skills=skills,
        instructor_name=instructor_name,
        rating=rating,
        num_ratings=num_ratings,
        duration_hours=duration_hours,
    )

    return {"id": updated.id}


@router.delete("/api/products/{product_id}")
def api_delete_product(
    product_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    product = product_service.get_product(db, product_id)
    if not product:
        return JSONResponse({"error": "not found"}, status_code=404)

    if not _can_manage_course(user, product):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    ok = product_service.delete_product(db, product_id)
    return {"deleted": ok}


@router.post("/api/enroll")
def api_enroll(
    request: Request,
    db: Session = Depends(get_db),
    product_id: int = Form(...),
):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    product = product_service.get_product(db, product_id)
    if not product:
        return JSONResponse({"error": "not found"}, status_code=404)

    existing = (
        db.query(Enrollment)
        .filter(Enrollment.user_id == user.id, Enrollment.product_id == product_id)
        .first()
    )
    if existing:
        return {"already_enrolled": True}

    enrollment = Enrollment(user_id=user.id, product_id=product_id)
    db.add(enrollment)

    if is_agent_tracking_enabled(db, user, request):
        db.add(Event(
            user_id=user.id,
            event_type="enroll",
            product_id=product_id,
            event_metadata=json.dumps({"source": "course_details", "category": product.category}),
            agent_eligible=True,
        ))

    # Conversion tracking: check if product_id was in any of user's recommendations
    user_recs = db.query(Recommendation).filter(Recommendation.user_id == user.id).all()
    for rec in user_recs:
        try:
            pids = json.loads(rec.product_ids) if rec.product_ids else []
            if product_id in pids and not getattr(rec, "converted", False):
                rec.converted = True
                rec.converted_at = datetime.now(timezone.utc)
                logger.info("Recommendation conversion tracked: rec_id=%s user_id=%s product_id=%s", rec.id, user.id, product_id)
        except Exception:
            pass

    db.commit()
    logger.info("User enrolled: user_id=%s product_id=%s product_title=%r", user.id, product.id, product.title)

    return {"enrolled": True}