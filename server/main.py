"""Desiconnect FastAPI application entry point.

Registers routers and exposes auth / vendor-search routes that
delegate to the service layer.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv()  # Load .env before any module reads os.environ

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from sqlalchemy import text
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field, field_validator
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.orm import Session

from app.config import ALLOWED_ORIGINS, SECRET_KEY, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, DATABASE_URL
from app.db.database import Base, engine, get_db
from app.db import models  # noqa: F401 -- registers tables with Base
from app.models.schemas import VendorCategory
from app.dependencies import get_current_user
from app.routers import calendar, bookings, notifications, users, vendors, services, payments
from app.services.auth_service import (
    AuthError,
    register_user,
    login_user,
    logout_user,
    change_password,
    lookup_google_linked_user,
    complete_profile,
)



# ── Rate limiter ──────────────────────────────────────────────────────

from app.limiter import limiter


# ── Request schemas ───────────────────────────────────────────────────

def _validate_password(v: str) -> str:
    """Enforce minimum password complexity."""
    if len(v) < 8:
        raise ValueError("Password must be at least 8 characters")
    if not re.search(r"[A-Z]", v):
        raise ValueError("Password must contain at least one uppercase letter")
    if not re.search(r"[a-z]", v):
        raise ValueError("Password must contain at least one lowercase letter")
    if not re.search(r"\d", v):
        raise ValueError("Password must contain at least one digit")
    return v


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    username: str = Field(..., min_length=3, max_length=30)
    phone: Optional[str] = None
    f_name: str = Field(..., min_length=1, max_length=50)
    l_name: str = Field(..., min_length=1, max_length=50)
    age: int = Field(..., ge=13, le=120)
    location: str = Field(..., min_length=1, max_length=100)
    gender: str = Field(..., min_length=1, max_length=20)
    language: str = Field(..., min_length=1, max_length=50)
    supabase_user_id: Optional[str] = None
    supabase_access_token: Optional[str] = None

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        return _validate_password(v)

    @field_validator("username")
    @classmethod
    def username_format(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_]+$", v):
            raise ValueError("Username may only contain letters, digits, and underscores")
        return v

    @field_validator("phone")
    @classmethod
    def phone_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        digits = re.sub(r"\D", "", v)
        if not 7 <= len(digits) <= 15:
            raise ValueError("Phone number must have between 7 and 15 digits")
        return v


class GoogleLookupRequest(BaseModel):
    access_token: str


class ProfileCompleteRequest(BaseModel):
    f_name: Optional[str] = None
    l_name: Optional[str] = None
    age: Optional[int] = Field(None, ge=13, le=120)
    location: Optional[str] = None
    gender: Optional[str] = None
    language: Optional[str] = None
    phone: Optional[str] = None


class LoginRequest(BaseModel):
    identifier: str = Field(..., min_length=1, description="Email address or username")
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def new_password_strength(cls, v: str) -> str:
        return _validate_password(v)


# ── App setup ─────────────────────────────────────────────────────────

app = FastAPI(
    title="Desiconnect API",
    description="Backend for Desiconnect — a marketplace for South Asian event vendors.",
    version="1.0.0",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(calendar.router)
app.include_router(bookings.router)
app.include_router(notifications.router)
app.include_router(users.router)
app.include_router(vendors.router)
app.include_router(services.router)
app.include_router(payments.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' cdn.jsdelivr.net fonts.googleapis.com; "
        "font-src 'self' fonts.gstatic.com; "
        "img-src 'self' data: fastapi.tiangolo.com; "
        "frame-ancestors 'none'"
    )
    return response


@app.on_event("startup")
def startup():
    """Validate required config and create DB tables."""
    db_display = "sqlite (local)" if DATABASE_URL.startswith("sqlite") else "postgresql"
    logger.info("Database: %s", db_display)

    if not SECRET_KEY:
        raise RuntimeError(
            "SECRET_KEY environment variable is not set. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if not STRIPE_SECRET_KEY:
        raise RuntimeError(
            "STRIPE_SECRET_KEY environment variable is not set. "
            "Add your Stripe test key (sk_test_...) to the .env file."
        )
    if not STRIPE_WEBHOOK_SECRET:
        raise RuntimeError(
            "STRIPE_WEBHOOK_SECRET environment variable is not set. "
            "Add your Stripe webhook signing secret (whsec_...) to the .env file."
        )

    is_production_db = not DATABASE_URL.startswith("sqlite")

    if is_production_db and any("localhost" in o for o in ALLOWED_ORIGINS):
        logger.warning(
            "ALLOWED_ORIGINS contains localhost entries in a production environment: %s — "
            "set the ALLOWED_ORIGINS env var to your real frontend URL(s).",
            ALLOWED_ORIGINS,
        )

    # Verify Alembic migrations are up to date (PostgreSQL only — SQLite is test/dev only).
    if not DATABASE_URL.startswith("sqlite"):
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
        from alembic.config import Config as AlembicConfig

        alembic_cfg = AlembicConfig("alembic.ini")
        script = ScriptDirectory.from_config(alembic_cfg)
        with engine.connect() as conn:
            migration_ctx = MigrationContext.configure(conn)
            current_heads = set(migration_ctx.get_current_heads())
            expected_heads = set(script.get_heads())
            if current_heads != expected_heads:
                raise RuntimeError(
                    f"Database migrations are not up to date. "
                    f"Run `alembic upgrade head` before starting. "
                    f"Current: {current_heads or 'none'}, Expected: {expected_heads}"
                )
    else:
        Base.metadata.create_all(bind=engine)


# ── Routes ────────────────────────────────────────────────────────────


@app.get("/")
def root():
    return {"message": "Desiconnect API", "status": "ok"}


@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Health check — verifies the database is reachable."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"status": "ok"}


@app.post("/auth/register")
@limiter.limit("3/minute")
def register(request: Request, body: RegisterRequest, db: Session = Depends(get_db)):
    """Create a new user. Password is hashed before storage."""
    try:
        return register_user(
            email=body.email,
            password=body.password,
            username=body.username,
            phone=body.phone,
            f_name=body.f_name,
            l_name=body.l_name,
            age=body.age,
            location=body.location,
            gender=body.gender,
            language=body.language,
            db=db,
            supabase_user_id=body.supabase_user_id,
            supabase_access_token=body.supabase_access_token,
        )
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        db.rollback()
        logger.exception("Unhandled error in /auth/register: %s", e)
        # Unique constraint violations (duplicate email/username/supabase_user_id)
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(status_code=400, detail="Email or username already taken")
        raise HTTPException(status_code=500, detail="Registration failed")


@app.post("/auth/login")
@limiter.limit("5/minute")
def login(request: Request, body: LoginRequest, db: Session = Depends(get_db)):
    """Verify email/password and return a JWT."""
    try:
        return login_user(identifier=body.identifier, password=body.password, db=db)
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.post("/auth/google/lookup")
@limiter.limit("5/minute")
def auth_google_lookup(request: Request, body: GoogleLookupRequest, db: Session = Depends(get_db)):
    """After Google OAuth, check if this Supabase identity is linked to a Jorna user; if so, return a FastAPI JWT."""
    try:
        return lookup_google_linked_user(access_token=body.access_token, db=db)
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.patch("/auth/profile")
@limiter.limit("10/minute")
def complete_profile_route(
    request: Request,
    body: ProfileCompleteRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Fill in profile fields left blank after Google sign-up (age, location, gender, etc.)."""
    try:
        return complete_profile(
            user_id=current_user.user_id,
            updates=body.model_dump(exclude_none=True),
            db=db,
        )
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.post("/auth/change-password")
@limiter.limit("5/minute")
def change_password_route(
    request: Request,
    body: ChangePasswordRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change the authenticated user's password after verifying the current one."""
    try:
        return change_password(
            user_id=current_user.user_id,
            current_password=body.current_password,
            new_password=body.new_password,
            db=db,
        )
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.post("/auth/logout")
@limiter.limit("10/minute")
def logout_route(request: Request, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    """Invalidate all tokens for the current user. The caller must re-authenticate."""
    try:
        return logout_user(user_id=current_user.user_id, db=db)
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)




