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

# ── CORS ──────────────────────────────────────────────────────────────
# Comma-separated list of allowed origins.
# Example: ALLOWED_ORIGINS=https://app.example.com,https://www.example.com
_raw_origins = os.getenv("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS: list[str] = (
    [o.strip() for o in _raw_origins.split(",") if o.strip()]
    if _raw_origins
    else ["http://localhost:3000", "http://localhost:8080"]
)
