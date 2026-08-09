"""
Resume Chroma embeddings for products that lack a successful upsert sync log.

Use after a partial/failed seed_data.py run — re-embeds ONLY missing/failed
products instead of re-running the full seed (which would burn Mesh balance twice).

Usage:
    python resync_chroma.py          # re-embed all unsynced products
    python resync_chroma.py --dry-run  # list IDs only, no Mesh calls
"""
import argparse
import sys

from dotenv import load_dotenv

load_dotenv()

from database.db import SessionLocal
from database.models import Product, ChromaSyncLog
from database import chroma_client
from services.product_service import _record_chroma_sync_log


def _synced_product_ids(db) -> set[int]:
    rows = (
        db.query(ChromaSyncLog.product_id)
        .filter(ChromaSyncLog.action == "upsert", ChromaSyncLog.status == "synced")
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def unsynced_products(db) -> list[Product]:
    synced = _synced_product_ids(db)
    products = db.query(Product).order_by(Product.id).all()
    return [p for p in products if p.id not in synced]


def resync(db, products: list[Product]) -> tuple[int, int]:
    ok, failed = 0, 0
    total = len(products)
    for i, product in enumerate(products, 1):
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
            ok += 1
        except Exception as exc:
            print(f"  [FAIL] product_id={product.id}: {exc}", file=sys.stderr)
            _record_chroma_sync_log(db, product.id, action="upsert", status="failed")
            failed += 1
        if i % 50 == 0:
            print(f"  ...{i}/{total} processed ({ok} synced, {failed} failed)")
    return ok, failed


def main():
    parser = argparse.ArgumentParser(description="Re-embed only unsynced products into Chroma")
    parser.add_argument("--dry-run", action="store_true", help="List unsynced product IDs only (no Mesh calls)")
    args = parser.parse_args()

    mesh_key = __import__("os").getenv("MESH_API_KEY", "").strip()
    if not args.dry_run and not mesh_key:
        print("[ABORT] MESH_API_KEY is missing or empty in .env", file=sys.stderr)
        sys.exit(1)

    db = SessionLocal()
    try:
        pending = unsynced_products(db)
        print(f"Unsynced products: {len(pending)} / {db.query(Product).count()} total")

        if args.dry_run:
            for p in pending[:10]:
                print(f"  id={p.id}  {p.title[:60]}")
            if len(pending) > 10:
                print(f"  ... and {len(pending) - 10} more")
            return

        if not pending:
            print("Nothing to re-embed — all products have a synced log entry.")
            return

        print(f"Re-embedding {len(pending)} products (Mesh calls: {len(pending)})...")
        ok, failed = resync(db, pending)
        print(f"Done: {ok} synced, {failed} failed ({ok} Mesh embedding calls succeeded)")
        if failed:
            sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
