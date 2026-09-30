"""Desiconnect FastAPI application entry point.

Registers routers and exposes auth / vendor-search routes that
delegate to the service layer.
"""

import asyncio
import logging
import re
import os
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv()  # Load .env before any module reads os.environ

# Initialise error monitoring as early as possible so import/startup errors are
# captured too. No-op unless SENTRY_DSN is set.
from app.observability import init_sentry
init_sentry()

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy import text
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field, field_validator
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.orm import Session

from app.config import ALLOWED_ORIGINS, ALLOWED_ORIGIN_REGEX, ESCROW_ENABLED, SECRET_KEY, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, DATABASE_URL, INITIAL_ADMIN_EMAIL
from app.db.database import Base, engine, get_db
from app.db import models  # noqa: F401 -- registers tables with Base
from app.models.schemas import VendorCategory
from app.dependencies import get_current_user
from app.routers import admin, bundles, calendar, bookings, change_requests, chatbot, checkin, contracts, conversations, events, feed, guest_bookings, guests, messages, moderation, negotiations, notifications, users, vendors, services, payments, reviews
from app.services.auth_service import (
    AuthError,
    register_user,
    login_user,
    logout_user,
    change_password,
    google_sign_in_or_create,
    google_register,
    complete_profile,
    refresh_access_token,
    request_password_reset,
    reset_password,
    cleanup_expired_tokens,
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
    # No min_length here: password_strength below enforces the same 8-char
    # floor itself, with a message meant for the person typing it. A Field
    # constraint runs before a validator does, so pairing them left Pydantic's
    # own "String should have at least 8 characters" pre-empting the friendlier
    # one for the exact case it was written for.
    password: str

    username: str = Field(..., min_length=3, max_length=30)
    phone: Optional[str] = None
    f_name: str = Field(..., min_length=1, max_length=50)
    l_name: str = Field(..., min_length=1, max_length=50)
    age: int = Field(..., ge=13, le=120)
    location: str = Field(..., min_length=1, max_length=100)
    # Where that location is. Optional because a typed city that matched nothing
    # has no coordinates to send — but without them a vendor can't be placed
    # relative to an event, and being unplaceable is what keeps them out of
    # search results and bundles.
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
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
    # Sent with a changed location, so moving city moves the vendor on the map
    # rather than leaving them pinned where they signed up.
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
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


class RefreshRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr
    # "web" mails a link into the web app; anything else (default) targets the
    # iOS deep-link bridge. See _send_password_reset_email.
    client: Optional[str] = None


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=1)
    # See RegisterRequest.password — no min_length here for the same reason.
    new_password: str

    @field_validator("new_password")
    @classmethod
    def reset_password_strength(cls, v: str) -> str:
        return _validate_password(v)


class LogoutRequest(BaseModel):
    refresh_token: Optional[str] = None


# ── Background tasks ──────────────────────────────────────────────────

_TOKEN_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60  # daily
_ESCROW_RELEASE_INTERVAL_SECONDS = 24 * 60 * 60  # daily
# Every five minutes, because this one is about a moment rather than a deadline.
# The reminder is meant to land half an hour before a vendor is due, and a
# coarser sweep would spread that over the interval — a "you're on in thirty
# minutes" arriving with ten to go is worse than useless to somebody who still
# has to park.
_CHECKIN_REMINDER_INTERVAL_SECONDS = 5 * 60
# Batched, not per-message — a message is only ever in the next digest after
# it arrives, so a shorter interval only means someone waits less between
# a message landing and their digest catching it. 20 minutes is short enough
# that the digest still reads as "new," long enough that a real conversation
# reads as one email rather than several.
_MESSAGE_DIGEST_INTERVAL_SECONDS = 20 * 60
_PAYMENT_REMINDER_INTERVAL_SECONDS = 60 * 60  # hourly
_CALENDAR_CHANNEL_RENEWAL_INTERVAL_SECONDS = 24 * 60 * 60  # daily
_CALENDAR_BUSY_RESYNC_INTERVAL_SECONDS = 4 * 60 * 60  # every 4 hours


async def _periodic_token_cleanup():
    """Sweep expired refresh + password-reset tokens once a day.

    Runs immediately on startup, then every 24h. Each pass uses its own DB
    session. Failures are logged and never crash the loop.
    """
    from app.db.database import SessionLocal
    while True:
        try:
            with SessionLocal() as session:
                result = cleanup_expired_tokens(session)
            if result["refresh_tokens_deleted"] or result["password_reset_tokens_deleted"]:
                logger.info("Expired token sweep: %s", result)
        except Exception as exc:
            logger.warning("Token cleanup failed: %s", exc)
        await asyncio.sleep(_TOKEN_CLEANUP_INTERVAL_SECONDS)


async def _periodic_escrow_release():
    """Release escrow a client has left unanswered since the event, once a day.

    Same shape as the token sweep: immediately on startup, then every 24h, its
    own session each pass, failures logged rather than fatal. Daily is the right
    cadence for a seven-day deadline — a few hours either side of the boundary
    changes nothing, and the sweep is idempotent because releasing sets the
    status it filters on.
    """
    from app.db.database import SessionLocal
    from app.services.stripe_service import auto_release_due

    while True:
        try:
            with SessionLocal() as session:
                auto_release_due(db=session)
        except Exception as exc:
            logger.warning("Escrow auto-release sweep failed: %s", exc)
        await asyncio.sleep(_ESCROW_RELEASE_INTERVAL_SECONDS)


async def _periodic_checkin_reminders():
    """Email vendors whose booking starts in about half an hour.

    Same shape as the sweeps above — own session per pass, failures logged
    rather than fatal — but far more often, because it's aiming at a moment
    rather than counting down a deadline.

    Idempotent through the booking's own checkin_reminder_sent_at, which the
    query filters on: a window several sweeps wide would otherwise mean the same
    vendor being emailed every five minutes until their event started.
    """
    from app.db.database import SessionLocal
    from app.services.reminder_service import send_due_reminders

    while True:
        try:
            with SessionLocal() as session:
                send_due_reminders(db=session)
        except Exception as exc:
            logger.warning("Check-in reminder sweep failed: %s", exc)
        await asyncio.sleep(_CHECKIN_REMINDER_INTERVAL_SECONDS)


async def _periodic_payment_reminders():
    """Remind clients of scheduled payments coming due, and vendors of ones
    overdue (services/payment_reminder_service.py).

    Same shape as the sweeps above. Hourly is plenty for day-granular due
    dates, and it's idempotent through the payment_reminder events each
    reminder records on the contract's timeline.
    """
    from app.db.database import SessionLocal
    from app.services.payment_reminder_service import send_payment_reminders

    while True:
        try:
            with SessionLocal() as session:
                send_payment_reminders(db=session)
        except Exception as exc:
            logger.warning("Payment reminder sweep failed: %s", exc)
        await asyncio.sleep(_PAYMENT_REMINDER_INTERVAL_SECONDS)


async def _periodic_message_digests():
    """Email anyone with unread messages a summary, every twenty minutes.

    Same shape as the sweeps above — own session per pass, failures logged
    rather than fatal. Idempotent through each user's own
    last_message_digest_at, which the sweep advances whether or not it found
    anything to send: a user with nothing new costs one cheap query, not a
    resend attempt every pass.
    """
    from app.db.database import SessionLocal
    from app.services.message_digest_service import send_due_digests

    while True:
        try:
            with SessionLocal() as session:
                send_due_digests(db=session)
        except Exception as exc:
            logger.warning("Message digest sweep failed: %s", exc)
        await asyncio.sleep(_MESSAGE_DIGEST_INTERVAL_SECONDS)


async def _periodic_calendar_channel_renewal():
    """Re-watch any Google Calendar push channel expiring within a day.

    Google caps a channel at about a week, so a daily sweep leaves several
    days of margin against a missed pass — same shape as the sweeps above,
    own session per pass, failures logged rather than fatal.
    """
    from app.db.database import SessionLocal
    from app.services.calendar_service import renew_expiring_channels

    while True:
        try:
            with SessionLocal() as session:
                renew_expiring_channels(db=session)
        except Exception as exc:
            logger.warning("Calendar channel renewal sweep failed: %s", exc)
        await asyncio.sleep(_CALENDAR_CHANNEL_RENEWAL_INTERVAL_SECONDS)


async def _periodic_calendar_busy_resync():
    """Re-pull busy blocks for every connected vendor, every four hours —
    the safety net under the push channel above: a missed or lapsed
    notification is never more than a few hours stale, not silently wrong
    until someone happens to reconnect.
    """
    from app.db.database import SessionLocal
    from app.services.calendar_service import resync_all_connected_vendors

    while True:
        try:
            with SessionLocal() as session:
                resync_all_connected_vendors(db=session)
        except Exception as exc:
            logger.warning("Calendar busy-time resync sweep failed: %s", exc)
        await asyncio.sleep(_CALENDAR_BUSY_RESYNC_INTERVAL_SECONDS)


# ── App setup ─────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_display = "sqlite (local)" if DATABASE_URL.startswith("sqlite") else "postgresql"
    logger.info("Database: %s", db_display)

    if not SECRET_KEY:
        raise RuntimeError(
            "SECRET_KEY environment variable is not set. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if ESCROW_ENABLED:
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

    if not os.getenv("OPENROUTER_API_KEY"):
        logger.warning(
            "OPENROUTER_API_KEY is not set — bundle creator will use keyword matching "
            "instead of LLM-powered tag scoring. Set it in Railway for full functionality."
        )

    if not os.getenv("RESEND_API_KEY"):
        logger.warning(
            "RESEND_API_KEY is not set — booking notifications will only be delivered via "
            "push (FCM). Users without a device token won't receive email fallbacks."
        )

    if not DATABASE_URL.startswith("sqlite") and any("localhost" in o for o in ALLOWED_ORIGINS):
        logger.warning(
            "ALLOWED_ORIGINS contains localhost entries in a production environment: %s — "
            "set the ALLOWED_ORIGINS env var to your real frontend URL(s).",
            ALLOWED_ORIGINS,
        )

    if DATABASE_URL.startswith("sqlite"):
        Base.metadata.create_all(bind=engine)

    if INITIAL_ADMIN_EMAIL:
        from app.db.database import SessionLocal
        from app.db.models import User as UserModel
        with SessionLocal() as session:
            user = session.query(UserModel).filter(UserModel.email == INITIAL_ADMIN_EMAIL.lower()).first()
            if user and not user.is_admin:
                user.is_admin = True
                session.commit()
                logger.info("Bootstrapped admin: %s", INITIAL_ADMIN_EMAIL)
            elif not user:
                logger.warning(
                    "INITIAL_ADMIN_EMAIL set to '%s' but no user with that email exists yet.",
                    INITIAL_ADMIN_EMAIL,
                )

    # Start the background sweeps.
    background = [
        asyncio.create_task(_periodic_token_cleanup()),
        asyncio.create_task(_periodic_checkin_reminders()),
        asyncio.create_task(_periodic_message_digests()),
        asyncio.create_task(_periodic_payment_reminders()),
        asyncio.create_task(_periodic_calendar_channel_renewal()),
        asyncio.create_task(_periodic_calendar_busy_resync()),
    ]
    if ESCROW_ENABLED:
        background.append(asyncio.create_task(_periodic_escrow_release()))

    yield

    # Shutdown: stop them.
    for task in background:
        task.cancel()
    for task in background:
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Desiconnect API",
    description="Backend for Desiconnect — a marketplace for South Asian event vendors.",
    version="1.0.0",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(vendors.router)
app.include_router(calendar.router)
app.include_router(bookings.router)
app.include_router(checkin.router)
app.include_router(events.router)
app.include_router(guests.router)
app.include_router(notifications.router)
app.include_router(users.router)
app.include_router(services.router)
app.include_router(payments.router)
app.include_router(reviews.router)
app.include_router(messages.router)
app.include_router(bundles.router)
app.include_router(conversations.router)
app.include_router(negotiations.router)
app.include_router(change_requests.router)
app.include_router(feed.router)
app.include_router(chatbot.router)
app.include_router(moderation.router)
app.include_router(admin.router)
app.include_router(contracts.router)
app.include_router(guest_bookings.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
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


# ── Routes ────────────────────────────────────────────────────────────


@app.get("/calendar-connected", response_class=HTMLResponse, include_in_schema=False)
def calendar_connected(success: str = "true", vendor_id: str = "", error: str = ""):
    # Query params are attacker-controlled — escape them or this page is a
    # reflected-XSS vector (CSP allows inline scripts for the return pages).
    from html import escape
    if success == "true":
        body = f"<h2>Google Calendar connected!</h2><p>Vendor ID: {escape(vendor_id)}</p><p>You can close this tab.</p>"
    else:
        body = f"<h2>Failed to connect Google Calendar</h2><p>{escape(error)}</p><p>Close this tab and try again.</p>"
    return HTMLResponse(f"<html><body style='font-family:sans-serif;padding:2rem'>{body}</body></html>")


def _app_return_page(*, deep_link: str, heading: str, sub: str) -> HTMLResponse:
    """An HTML page that automatically bounces back into the Jorna app via its
    custom URL scheme, with a tappable fallback if the browser blocks the
    auto-redirect. Stripe requires an https return URL, so the app can't be the
    direct target — this page is the bridge."""
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<meta http-equiv='refresh' content='0;url={deep_link}'>
<script>window.location.replace({deep_link!r});</script>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;text-align:center;padding:3rem 1.5rem;color:#3a0a12;background:#faf7f0}}
  .btn{{display:inline-block;margin-top:1.5rem;padding:14px 28px;background:#661724;color:#fff;border-radius:14px;text-decoration:none;font-weight:700}}
</style></head><body>
<h2>{heading}</h2><p>{sub}</p>
<a class='btn' href='{deep_link}'>Return to Jorna</a>
</body></html>"""
    )


@app.get("/vendor/stripe-onboard/return", response_class=HTMLResponse, include_in_schema=False)
def stripe_onboard_return(vendor_id: str = ""):
    """Landing page Stripe redirects to after a vendor finishes Connect onboarding.
    Bounces back into the app, which re-checks the vendor's Stripe status."""
    deep_link = f"jorna://stripe-onboard-complete?vendor_id={quote(vendor_id, safe='')}"
    return _app_return_page(
        deep_link=deep_link,
        heading="Payment setup complete",
        sub="Returning you to Jorna…",
    )


@app.get("/vendor/stripe-onboard/refresh", response_class=HTMLResponse, include_in_schema=False)
def stripe_onboard_refresh(vendor_id: str = ""):
    """Stripe redirects here if the onboarding link expired or was reopened.
    Bounces back into the app, where the vendor can tap 'Continue Setup' again."""
    deep_link = f"jorna://stripe-onboard-complete?vendor_id={quote(vendor_id, safe='')}&refresh=1"
    return _app_return_page(
        deep_link=deep_link,
        heading="Setup link expired",
        sub="Returning you to Jorna to finish setup…",
    )


@app.get("/reset-password", response_class=HTMLResponse, include_in_schema=False)
def reset_password_landing(token: str = ""):
    """Landing page for the password-reset email link. Bounces into the app,
    which opens the choose-a-new-password sheet with the token prefilled."""
    deep_link = f"jorna://reset-password?token={quote(token, safe='')}"
    return _app_return_page(
        deep_link=deep_link,
        heading="Reset your password",
        sub="Opening Jorna so you can choose a new password…",
    )


@app.get("/payment-complete", response_class=HTMLResponse, include_in_schema=False)
def payment_complete(status: str = "success", booking_id: str = ""):
    """Landing page Stripe Checkout redirects to. Bounces back into the app,
    which refreshes the booking's payment state."""
    status = "success" if status == "success" else "cancel"
    deep_link = (
        f"jorna://payment-complete?booking_id={quote(booking_id, safe='')}&status={status}"
    )
    if status == "success":
        heading = "Payment complete"
        sub = "Returning you to Jorna — your payment is held securely until you confirm the event."
    else:
        heading = "Payment canceled"
        sub = "No charge was made. Returning you to Jorna…"
    return _app_return_page(deep_link=deep_link, heading=heading, sub=sub)


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


@app.get("/googleec2daeb88a212390.html", include_in_schema=False)
def google_site_verification():
    """Proves Jorna controls this Railway subdomain, via Google Search
    Console's HTML-file method — DNS verification isn't an option here,
    since Railway (not Jorna) owns up.railway.app's DNS. Required before
    events().watch() (Google Calendar push notifications,
    calendar_service.watch_calendar) will send anything to this domain.
    One static file per verified domain; safe to leave in place
    indefinitely once verified.
    """
    return PlainTextResponse("google-site-verification: googleec2daeb88a212390.html")


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
            latitude=body.latitude,
            longitude=body.longitude,
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
        return google_sign_in_or_create(access_token=body.access_token, db=db)
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.post("/auth/google/register")
@limiter.limit("5/minute")
def auth_google_register(request: Request, body: GoogleLookupRequest, db: Session = Depends(get_db)):
    """Sign in with Google, creating the account on first use — the whole sign-up in one tap.

    Separate from /auth/google/lookup, which still creates nothing: a client that
    shows a registration form after looking up must keep calling lookup, or it
    will try to register an address that now exists. Callers of this endpoint skip
    the form entirely.
    """
    try:
        return google_register(access_token=body.access_token, db=db)
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


@app.post("/auth/refresh")
@limiter.limit("20/minute")
def refresh_route(request: Request, body: RefreshRequest, db: Session = Depends(get_db)):
    """Exchange a valid refresh token for a new access token + rotated refresh token."""
    try:
        return refresh_access_token(refresh_token=body.refresh_token, db=db)
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.post("/auth/forgot-password")
@limiter.limit("3/minute")
def forgot_password_route(request: Request, body: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Email a single-use password reset link. Always returns 200 so the response
    can't be used to discover which email addresses are registered."""
    try:
        return request_password_reset(email=body.email, db=db, client=body.client or "ios")
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.post("/auth/reset-password")
@limiter.limit("5/minute")
def reset_password_route(request: Request, body: ResetPasswordRequest, db: Session = Depends(get_db)):
    """Set a new password using a valid reset token, invalidating all existing sessions."""
    try:
        return reset_password(token=body.token, new_password=body.new_password, db=db)
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.post("/auth/logout")
@limiter.limit("10/minute")
def logout_route(
    request: Request,
    body: LogoutRequest = LogoutRequest(),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Invalidate all access tokens. If refresh_token is supplied, only that device's
    refresh token is removed; otherwise all refresh tokens for the user are wiped."""
    try:
        return logout_user(
            user_id=current_user.user_id,
            db=db,
            refresh_token=body.refresh_token,
        )
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)




