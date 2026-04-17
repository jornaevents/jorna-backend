"""Database connection: engine, session factory, and dependency for FastAPI."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import DATABASE_URL

# SQLite requires check_same_thread=False; PostgreSQL does not need it.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

_pool_kwargs = (
    {}
    if DATABASE_URL.startswith("sqlite")
    else {
        "pool_size": 10,
        "max_overflow": 20,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }
)
engine = create_engine(DATABASE_URL, connect_args=_connect_args, **_pool_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Yield a DB session. Use with FastAPI Depends() so the session closes after each request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
