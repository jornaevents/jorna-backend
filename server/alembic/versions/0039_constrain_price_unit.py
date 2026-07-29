"""price_unit holds one of four words

Revision ID: 0039_price_unit
Revises: 0038_checkin_resend
Create Date: 2026-07-29

services.price_unit was free text, and two functions read it with different
ideas of what counted as per person. The pricer accepted "per head", "guest",
"plate", "pax", "persons"; the check that decides whether a request may be sent
matched only the exact word "person". So a caterer priced "per head" sent with
no guest count, the vendor accepted, and the booking arrived at checkout with no
resolvable total — unpayable, past the only screen that could have fixed it.

The code on both sides now asks one function. This closes the door behind it:
the column is normalised to person/hour/day/event and constrained to those, so
no future path can put anything else there.

The mapping is written out here rather than imported from app code. A migration
has to keep meaning what it meant on the day it ran, and app code moves.

Unrecognisable values become 'event'. That is not a decision about them — it is
what they already priced as, since anything the pricer didn't recognise fell
through to the flat rate. NULL is left alone and stays permitted: a service that
never said how it charges means the same thing.
"""
from alembic import op
import sqlalchemy as sa

revision = "0039_price_unit"
down_revision = "0038_checkin_resend"
branch_labels = None
depends_on = None

ALLOWED = ("person", "hour", "day", "event")


def _canonical(raw: str) -> str:
    """The same rule booking_service._normalize_unit applied, frozen in time."""
    u = (raw or "").strip().lower()
    if u.startswith("per "):
        u = u[4:].strip()
    if u.startswith("hour"):
        return "hour"
    if u.startswith("day"):
        return "day"
    if u.startswith("event"):
        return "event"
    if u.startswith("person") or u in ("head", "plate", "guest", "pax"):
        return "person"
    # Everything else already priced flat. Naming it keeps that true and makes
    # it visible.
    return "event"


def upgrade():
    conn = op.get_bind()

    # First, because an empty string is not a unit — it's a form that submitted
    # a blank box, and calling that a deliberate choice of flat pricing would be
    # putting words in a vendor's mouth. Run after the pass below it would
    # already have become 'event'.
    conn.execute(
        sa.text("UPDATE services SET price_unit = NULL WHERE TRIM(price_unit) = ''")
    )

    rows = conn.execute(
        sa.text("SELECT service_id, price_unit FROM services WHERE price_unit IS NOT NULL")
    ).fetchall()

    for service_id, raw in rows:
        canon = _canonical(raw)
        if canon != raw:
            conn.execute(
                sa.text("UPDATE services SET price_unit = :u WHERE service_id = :id"),
                {"u": canon, "id": service_id},
            )

    # batch_alter_table so this is runnable on SQLite as well as Postgres — it
    # issues a plain ALTER on Postgres, which is what production takes, and the
    # copy-and-move strategy elsewhere. Being able to run the thing against a
    # scratch database is the only way to know the pass above did what it says.
    with op.batch_alter_table("services") as batch:
        batch.create_check_constraint(
            "ck_services_price_unit",
            "price_unit IS NULL OR price_unit IN ('person', 'hour', 'day', 'event')",
        )


def downgrade():
    # Only the constraint comes off. The normalised values are what every
    # reader now expects, and the originals ("per head", "Per Hour") are the
    # ambiguity this existed to remove — putting them back would mean
    # remembering which rows were rewritten, which nothing records.
    with op.batch_alter_table("services") as batch:
        batch.drop_constraint("ck_services_price_unit", type_="check")
