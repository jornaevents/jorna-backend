"""Central application configuration loaded from environment variables."""
import os

# ── Database ──────────────────────────────────────────────────────────
# Default to SQLite for local development; set DATABASE_URL to a
# PostgreSQL connection string (e.g. Supabase) for staging/production.
def _with_postgres_driver(url: str) -> str:
    """Name the psycopg2 driver in a Postgres URL that doesn't name one.

    Railway (and Heroku) hand out postgres://, an alias SQLAlchemy 2.x dropped.
    And a bare postgresql:// means whichever driver SQLAlchemy defaults to —
    psycopg2 up to 2.0, psycopg 3 from 2.1 — so a routine dependency bump would
    leave the app unable to connect, since only psycopg2-binary is installed.
    A URL that already names a driver (postgresql+…://) is left alone.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    return url


DATABASE_URL: str = _with_postgres_driver(
    os.getenv("DATABASE_URL", "").strip() or "sqlite:///./test.db"
)

# ── JWT ───────────────────────────────────────────────────────────────
# Generate a secure value with:
#   python -c "import secrets; print(secrets.token_hex(32))"
# Intentionally no default — the app will refuse to start without this.
SECRET_KEY: str = os.getenv("SECRET_KEY", "")
ALGORITHM: str = "HS256"
# Access token lifetime in minutes (default 60 minutes). Short-lived; rotate via refresh token.
ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
# Refresh token lifetime in days (default 30 days).
REFRESH_TOKEN_EXPIRE_DAYS: int = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))
# Password-reset token lifetime in minutes (default 60 minutes). Short-lived, single-use.
PASSWORD_RESET_EXPIRE_MINUTES: int = int(os.getenv("PASSWORD_RESET_EXPIRE_MINUTES", "60"))

# ── Google Calendar OAuth ─────────────────────────────────────────────
# Where Google redirects after the vendor grants access.
# Must match exactly what is registered in Google Cloud Console.
GOOGLE_OAUTH_REDIRECT_URI: str = os.getenv(
    "GOOGLE_OAUTH_REDIRECT_URI",
    "http://localhost:8000/vendors/auth/callback",
)

# Where the backend redirects the vendor's browser after OAuth completes.
FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:3000")

# Where Google POSTs a push notification when a watched calendar changes.
# Must be a publicly reachable HTTPS URL — Railway's own deployment URL
# satisfies this with no extra infra. Google requires this to be set on the
# same OAuth-verified domain as the app, same as the redirect URI above.
GOOGLE_CALENDAR_WEBHOOK_URL: str = os.getenv(
    "GOOGLE_CALENDAR_WEBHOOK_URL",
    "http://localhost:8000/vendors/google-calendar/webhook",
)

# Base URL of the Jorna web app. Used to build Stripe Checkout return URLs for
# browser clients, which need to land back in the web app rather than on the
# iOS deep-link bridge page. Not client-supplied — that would be an open redirect.
WEB_APP_URL: str = os.getenv("WEB_APP_URL", "https://jornaevents.com/app")

# ── Admin bootstrap ───────────────────────────────────────────────────
# If set, this email address is automatically promoted to admin on startup.
# Use this to create the first admin without needing direct DB access.
INITIAL_ADMIN_EMAIL: str = os.getenv("INITIAL_ADMIN_EMAIL", "")

# ── Stripe ───────────────────────────────────────────────────────────
# Master switch for Stripe Connect escrow (checkout, payouts, disputes, the
# daily auto-release sweep). Defaults on so nothing changes until this is
# explicitly set to "false" — flip it in the deploy environment as a separate
# step from shipping the code that reads it. When off, every booking is
# forced onto the manual Venmo/Zelle track regardless of a vendor's stored
# payment_method (see booking_service.create_booking) and Stripe-only
# endpoints/boot checks are skipped. See docs/DECISIONS.md #12.
ESCROW_ENABLED: bool = os.getenv("ESCROW_ENABLED", "true").strip().lower() != "false"

# New contracts negotiate field by field (docs/DECISIONS.md #26) instead of
# by whole proposal (#23). On by default; set FIELD_NEGOTIATION=false to go
# back. Either way, only contracts created afterwards change.
FIELD_NEGOTIATION: bool = os.getenv("FIELD_NEGOTIATION", "true").strip().lower() == "true"
STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY: str = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
# Integer percent taken by the platform on each booking (e.g. 5 = 5 %).
PLATFORM_FEE_PERCENT: int = int(os.getenv("PLATFORM_FEE_PERCENT", "5"))

# ── Supabase Storage ──────────────────────────────────────────────────
# Service role key (Settings → API in Supabase dashboard).
# Required only for server-side file uploads; keep this secret.
SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "")

# ── Email (Resend) ────────────────────────────────────────────────────
# Transactional email via Resend (https://resend.com). Used as a fallback
# channel for booking notifications when a user has no FCM push token, and
# for password-reset emails. Email is silently skipped when unset.
RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "")
# Must be an address on a domain verified in your Resend dashboard.
EMAIL_FROM: str = os.getenv("EMAIL_FROM", "Jorna <noreply@desiconnect.com>")

# ── YouTube ───────────────────────────────────────────────────────────
YOUTUBE_API_KEY: str = os.getenv("YOUTUBE_API_KEY", "")

# ── Error monitoring (Sentry) ─────────────────────────────────────────
# Set SENTRY_DSN to your Sentry project's DSN to enable error reporting.
# Unset → Sentry is disabled and the app runs untouched (see app/observability.py).
SENTRY_DSN: str = os.getenv("SENTRY_DSN", "").strip()
# Tag events with an environment; defaults to production on a real DB, else development.
SENTRY_ENVIRONMENT: str = (
    os.getenv("SENTRY_ENVIRONMENT", "").strip()
    or ("production" if not DATABASE_URL.startswith("sqlite") else "development")
)
# Fraction of requests traced for performance (0.0 = errors only; keeps cost/free-tier low).
SENTRY_TRACES_SAMPLE_RATE: float = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0"))
# Release identifier for grouping — Railway injects the commit SHA automatically.
RELEASE: str = os.getenv("RAILWAY_GIT_COMMIT_SHA", "").strip() or os.getenv("RELEASE", "").strip() or None

# ── CORS ──────────────────────────────────────────────────────────────
# Comma-separated list of allowed origins.
# Example: ALLOWED_ORIGINS=https://app.example.com,https://www.example.com
_raw_origins = os.getenv("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS: list[str] = (
    [o.strip() for o in _raw_origins.split(",") if o.strip()]
    if _raw_origins
    else ["http://localhost:3000", "http://localhost:8080"]
)
# Optional regex, ORed with ALLOWED_ORIGINS by CORSMiddleware — lets a whole
# subdomain pattern in through one setting instead of editing the list per
# deploy. Used for jorna-website's per-PR Cloudflare Pages previews
# (pr-<n>.jorna-events.pages.dev), which we can't enumerate in advance.
ALLOWED_ORIGIN_REGEX: str | None = os.getenv("ALLOWED_ORIGIN_REGEX", "").strip() or None
