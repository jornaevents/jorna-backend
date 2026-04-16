"""Central application configuration loaded from environment variables."""
import os

# ── Database ──────────────────────────────────────────────────────────
# Default to SQLite for local development; set DATABASE_URL to a
# PostgreSQL connection string (e.g. Supabase) for staging/production.
DATABASE_URL: str = os.getenv("DATABASE_URL", "").strip() or "sqlite:///./test.db"

# ── JWT ───────────────────────────────────────────────────────────────
# Generate a secure value with:
#   python -c "import secrets; print(secrets.token_hex(32))"
# Intentionally no default — the app will refuse to start without this.
SECRET_KEY: str = os.getenv("SECRET_KEY", "")
ALGORITHM: str = "HS256"
# Access token lifetime in minutes (default 60 minutes).
ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

# ── Google Calendar OAuth ─────────────────────────────────────────────
# Where Google redirects after the vendor grants access.
# Must match exactly what is registered in Google Cloud Console.
GOOGLE_OAUTH_REDIRECT_URI: str = os.getenv(
    "GOOGLE_OAUTH_REDIRECT_URI",
    "http://localhost:8000/vendors/auth/callback",
)

# Where the backend redirects the vendor's browser after OAuth completes.
FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:3000")

# ── Stripe ───────────────────────────────────────────────────────────
STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY: str = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
# Integer percent taken by the platform on each booking (e.g. 5 = 5 %).
PLATFORM_FEE_PERCENT: int = int(os.getenv("PLATFORM_FEE_PERCENT", "5"))

# ── CORS ──────────────────────────────────────────────────────────────
# Comma-separated list of allowed origins.
# Example: ALLOWED_ORIGINS=https://app.example.com,https://www.example.com
_raw_origins = os.getenv("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS: list[str] = (
    [o.strip() for o in _raw_origins.split(",") if o.strip()]
    if _raw_origins
    else ["http://localhost:3000", "http://localhost:8080"]
)
