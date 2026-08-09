import random
import sys
import os
from datetime import datetime

# ensure project root on path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import product_service
from database.db import SessionLocal

COMMENTS = [
    "Great course — learned a lot!",
    "Well explained, practical examples.",
    "Good overview but could use more exercises.",
    "Loved the instructor's style.",
    "Too fast-paced for beginners.",
    "Excellent projects and clear explanations.",
    "Not what I expected, but useful content.",
    "Highly recommended for intermediate learners.",
    "Solid course, some parts are outdated.",
    "Fantastic examples and clear pacing.",
]

REVIEWER_NAMES = [
    "Aisha", "Sam", "Ravi", "Priya", "Liam", "Noah", "Olivia", "Emma", "Lucas", "Mia",
    "Ava", "Ethan", "Sophia", "Amir", "Hana", "Carlos", "Diego", "Zara", "Ibrahim", "Nina",
]

def main():
    db = SessionLocal()
    try:
        products = product_service.get_all_products(db)
        total_reviews = 0

        for p in products:
            # random 1-6 reviews per product
            n = random.randint(1, 6)
            for _ in range(n):
                reviewer = random.choice(REVIEWER_NAMES)
                # base rating around product.rating if exists, else 4.2
                base = p.rating if p.rating is not None else 4.2
                # add small noise
                rating = max(1.0, min(5.0, round(random.gauss(base, 0.6), 1)))
                comment = random.choice(COMMENTS)

                product_service.create_review(db, p.id, None, reviewer, rating, comment)
                total_reviews += 1

        print(f"✅ Seeded {total_reviews} reviews across {len(products)} products.")
    finally:
        db.close()


if __name__ == '__main__':
    random.seed(42)
    main()
