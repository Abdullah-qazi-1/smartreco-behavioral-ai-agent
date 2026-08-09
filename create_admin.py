"""
Run this once to create an admin account.
Signup from the website always creates role="user" — this script is the
only way to create an admin, on purpose (keeps admin creation deliberate).

Usage:
    python create_admin.py
"""
from database.db import Base, engine, SessionLocal  # noqa: E402
from database.models import User  # noqa: E402
from routers.auth import hash_password  # noqa: E402

Base.metadata.create_all(bind=engine)

db = SessionLocal()

print("=== Create Admin Account ===")
name = input("Name: ").strip()
email = input("Email: ").strip().lower()
password = input("Password: ").strip()

existing = db.query(User).filter(User.email == email).first()
if existing:
    existing.role = "admin"
    db.commit()
    print(f"'{email}' already existed — promoted to admin.")
else:
    admin = User(
        name=name,
        email=email,
        password_hash=hash_password(password),
        role="admin",
    )
    db.add(admin)
    db.commit()
    print(f"Admin account created for '{email}'.")

db.close()
