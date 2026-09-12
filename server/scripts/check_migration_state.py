"""Pre-deploy guard: refuse to run `alembic upgrade head` if the database's
current alembic_version isn't a revision this repo's migration chain
actually knows about.

`alembic upgrade head` on its own will happily upgrade from *any* stamped
starting point with no such check — that's how the 2026-08-30 incident
happened: alembic_version was stamped to a revision that existed nowhere in
git, and the next deploy upgraded from it without complaint. Railway's
preDeployCommand runs this script first (see railway.toml); a nonzero exit
here stops the deploy before it can touch a database in a state git doesn't
recognize.
"""

import sys

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.config import DATABASE_URL


def known_revisions(config_path: str = "alembic.ini") -> set[str]:
    config = Config(config_path)
    script = ScriptDirectory.from_config(config)
    return {rev.revision for rev in script.walk_revisions()}


def current_db_revision(engine) -> str | None:
    if not inspect(engine).has_table("alembic_version"):
        return None
    with engine.connect() as conn:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        return row[0] if row else None


def main() -> int:
    engine = create_engine(DATABASE_URL)
    db_revision = current_db_revision(engine)

    if db_revision is None:
        print("No alembic_version table yet — treating as a fresh database.")
        return 0

    valid = known_revisions()
    if db_revision not in valid:
        print(
            f"REFUSING TO DEPLOY: alembic_version is stamped to "
            f"{db_revision!r}, which does not exist anywhere in this repo's "
            f"migration chain ({len(valid)} known revisions). "
            "`alembic upgrade head` would upgrade blindly from an origin "
            "git doesn't recognize — this is exactly what happened on "
            "2026-08-30. Investigate the actual DB state before deploying; "
            'see CLAUDE.md\'s "Diagnosing a failed Railway deploy" section.',
            file=sys.stderr,
        )
        return 1

    print(f"alembic_version {db_revision!r} is a known revision — proceeding.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
