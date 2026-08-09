"""
Database connection setup.
Uses SQLite for simplicity — swap DATABASE_URL for Postgres later if needed,
no other code changes required since SQLAlchemy abstracts the difference.
"""
import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./smartreco.db")

_connect_args = {}
_engine_kwargs = {"pool_pre_ping": True}

if DATABASE_URL.startswith("sqlite"):
    _connect_args["check_same_thread"] = False
    _engine_kwargs["connect_args"] = _connect_args
else:
    _engine_kwargs.update(pool_size=10, max_overflow=20, pool_recycle=3600)

engine = create_engine(DATABASE_URL, **_engine_kwargs)

if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def run_migrations():
    from sqlalchemy import inspect, text
    from database.db import Base, engine

    with engine.connect() as conn:
        inspector = inspect(conn)
        existing_tables = inspector.get_table_names()

        for table_name, table_obj in Base.metadata.tables.items():
            if table_name in existing_tables:
                existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
                for col in table_obj.columns:
                    if col.name not in existing_cols:
                        col_type = col.type.compile(engine.dialect)
                        default_clause = ""
                        if col.default is not None and col.default.arg is not None:
                            val = col.default.arg
                            if isinstance(val, bool):
                                default_clause = f" DEFAULT {1 if val else 0}"
                            elif isinstance(val, (int, float)):
                                default_clause = f" DEFAULT {val}"
                            elif isinstance(val, str):
                                default_clause = f" DEFAULT '{val}'"
                        elif col.name == "active_mode":
                            default_clause = " DEFAULT 'student'"
                        elif col.name == "status":
                            default_clause = " DEFAULT 'active'"
                        elif col.name == "converted":
                            default_clause = " DEFAULT 0"
                        elif col.name == "agent_tracking_enabled":
                            default_clause = " DEFAULT 1"


                        alter_cmd = f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type}{default_clause}"
                        try:
                            conn.execute(text(alter_cmd))
                        except Exception as e:
                            pass

        conn.commit()

        # Composite indexes for hot event queries (idempotent on SQLite)
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS ix_events_user_created ON events (user_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_events_user_eligible_created ON events (user_id, agent_eligible, created_at)",
        ):
            try:
                conn.execute(text(idx_sql))
            except Exception:
                pass
        conn.commit()


def get_db():
    """FastAPI dependency — yields a DB session per-request, closes it after."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


