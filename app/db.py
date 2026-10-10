"""Database engine, session factory and schema creation (SQLite)."""
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


engine = None
SessionLocal = sessionmaker(autoflush=False, expire_on_commit=False)


def configure(url: str = config.DATABASE_URL):
    """(Re)bind the app to a database URL. Tests call this with a temp file."""
    global engine
    if engine is not None:
        engine.dispose()
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    SessionLocal.configure(bind=engine)
    return engine


# URS-008 / 21 CFR 11.10(e): the audit trail is append-only, enforced by the database itself,
# so even a bug in the application (or someone with a SQL console) cannot change history.
AUDIT_TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS audit_trail_no_update BEFORE UPDATE ON audit_trail
       BEGIN SELECT RAISE(ABORT, 'audit_trail is append-only: UPDATE not allowed'); END;""",
    """CREATE TRIGGER IF NOT EXISTS audit_trail_no_delete BEFORE DELETE ON audit_trail
       BEGIN SELECT RAISE(ABORT, 'audit_trail is append-only: DELETE not allowed'); END;""",
]


def create_schema(drop: bool = False):
    from . import models  # noqa: F401  (register tables)

    if drop:
        Base.metadata.drop_all(engine)   # dropping the table also drops its triggers
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for sql in AUDIT_TRIGGERS:
            conn.execute(text(sql))


def get_db():
    """FastAPI dependency: one session per request. Nothing is saved unless a route commits."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


configure()
