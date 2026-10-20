"""Every contract_status the code writes must be one Postgres allows.

The tests run on SQLite, which never gets ck_bookings_contract_status, so a
new status passes here and fails every write in production — which is what
'awaiting_vendor' did (0079). This reads the newest migration that sets the
constraint and every status literal the app assigns."""

import importlib.util
import re
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
VERSIONS = SERVER / "alembic" / "versions"


def _allowed() -> set[str]:
    # The newest migration that defines the list (they're numbered in order).
    path = sorted(p for p in VERSIONS.glob("0*.py") if "ck_bookings_contract_status" in p.read_text())[-1]
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statuses = getattr(module, "STATUSES", None) or getattr(module, "_STATUSES")
    return set(statuses) if not isinstance(statuses, str) else set(re.findall(r"'(\w+)'", statuses))


def _written() -> set[str]:
    found: set[str] = set()
    for path in (SERVER / "app").rglob("*.py"):
        for line in path.read_text().splitlines():
            if re.search(r"contract_status\s*=[^=]", line):
                found.update(re.findall(r"\"(\w+)\"", line.split("contract_status", 1)[1]))
    return found


def test_every_status_the_code_writes_is_allowed_by_the_database():
    written = _written()
    assert {"draft", "sent", "awaiting_vendor", "signed"} <= written  # the scan is finding them
    assert written <= _allowed(), f"not allowed by ck_bookings_contract_status: {written - _allowed()}"
