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
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app.config import ALLOWED_ORIGINS, SECRET_KEY, STRIPE_SECRET_KEY
from app.db.database import Base, engine, get_db
from app.db import models  # noqa: F401 -- registers tables with Base
from app.models.schemas import VendorCategory
from app.dependencies import get_current_user
from app.routers import calendar, bookings, notifications, users, vendors, services, payments
from app.services.auth_service import (
    AuthError,
    register_user,
    login_user,
    change_password,
    lookup_google_linked_user,
)
from app.services.vendor_service import search_vendors


# ── Rate limiter ──────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)


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


@app.on_event("startup")
def startup():
    """Validate required config and create DB tables."""
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
def auth_google_lookup(body: GoogleLookupRequest, db: Session = Depends(get_db)):
    """After Google OAuth, check if this Supabase identity is linked to a Jorna user; if so, return a FastAPI JWT."""
    try:
        return lookup_google_linked_user(access_token=body.access_token, db=db)
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.post("/auth/change-password")
def change_password_route(
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


@app.get("/vendors/search")
def vendor_search(
    service_name: str,
    latitude: float,
    longitude: float,
    category: Optional[VendorCategory] = Query(None, description="Filter by vendor category"),
    tag: Optional[str] = Query(None, description="Filter by tag (e.g. 'bridal mehndi')"),
    db: Session = Depends(get_db),
):
    """Search for vendors offering a specific service within their travel radius."""
    vendors = search_vendors(
        service_name=service_name,
        latitude=latitude,
        longitude=longitude,
        category=category.value if category else None,
        tag=tag,
        db=db,
    )
    return {"vendors": vendors}


