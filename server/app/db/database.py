"""SQLite connection: engine, session factory, and dependency for FastAPI."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

SQLALCHEMY_DATABASE_URL = "sqlite:///./desiconnect.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Yield a DB session. Use with FastAPI Depends() so the session closes after each request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
