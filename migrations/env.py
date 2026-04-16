import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool
from alembic import context

# ── Load .env so DATABASE_URL is available ────────────────────────────
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / "server" / ".env")

# ── Make sure `app` package is importable ─────────────────────────────
SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# Import Base and all models so Alembic sees every table.
from app.db.database import Base  # noqa: E402
import app.db.models  # noqa: E402, F401 — registers all ORM classes with Base

target_metadata = Base.metadata

# ── Alembic config ────────────────────────────────────────────────────
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override sqlalchemy.url from the environment (never hardcode credentials).
_db_url = os.getenv("DATABASE_URL", "").strip() or "sqlite:///./server/test.db"
config.set_main_option("sqlalchemy.url", _db_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
