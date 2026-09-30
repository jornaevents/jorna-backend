"""Railway's actual preDeployCommand entry point: the migration-state guard,
then `alembic upgrade head` — both in this one Python process.

Previously these were chained as a single shell string,
`python -m scripts.check_migration_state && alembic upgrade head`. On
2026-09-20, that shipped a deploy where the guard ran and passed (its
"known revision — proceeding" line is in the log) but `alembic upgrade
head` never ran — no output, no error, nothing between the guard's print
and the container starting. The app went live five migrations behind its
own schema expectations, 500ing on every endpoint that touched a new
column until someone ran `alembic upgrade head` by hand. Root cause was
never pinned down (most likely Railway's preDeployCommand runner doesn't
reliably execute `&&` the way a real shell would) — this script sidesteps
the question entirely by not relying on shell chaining at all.

Calls `alembic.command.upgrade` directly (the same API `alembic upgrade`
the CLI wraps) rather than shelling out, so there's no second process for
anything to fail to launch.
"""

import sys

from alembic import command
from alembic.config import Config

from scripts.check_migration_state import main as check_migration_state


def main() -> int:
    guard_exit = check_migration_state()
    if guard_exit != 0:
        return guard_exit

    command.upgrade(Config("alembic.ini"), "head")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
