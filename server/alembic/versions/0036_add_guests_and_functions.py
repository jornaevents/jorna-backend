"""guest lists, per function

Revision ID: 0036_guests
Revises: 0035_saved_card
Create Date: 2026-07-29

A celebration here is rarely one gathering. A wedding is a mehndi, a sangeet, a
ceremony and a reception — different days, different guest lists, and separately
catered, which is the part that matters: a per-person total is billed against
the headcount for *that* function, not for the wedding as a whole.

So three tables rather than one.

event_functions   the gatherings a celebration is made of. An event with none
                  behaves as it always did; the UI creates a single default
                  function when a host starts a guest list.

guests            a person, or a household, invited to the celebration. One row
                  per invitation rather than per body — "the Kapoor family, 4"
                  is how a guest list is actually kept, and it is what makes the
                  headcount add up.

guest_invites     which functions each guest is asked to, and what they said
                  about each. Being in this table is the invitation; the status
                  is the answer.

Tokens are the credential on pages with no login, so they're random rather than
derived from an id. Each guest gets one, and the event gets one for the open
link a host drops in a group chat — where whoever opens it adds themselves.
"""
from alembic import op
import sqlalchemy as sa

revision = "0036_guests"
down_revision = "0035_saved_card"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "event_functions",
        sa.Column("function_id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("events.event_id"), nullable=False, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        # Its own day and hours, which is the whole reason functions exist. Null
        # falls back to the event's.
        sa.Column("date_iso", sa.String(50), nullable=True),
        sa.Column("time_start", sa.String(50), nullable=True),
        sa.Column("time_end", sa.String(50), nullable=True),
        sa.Column("location", sa.String(255), nullable=True),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "guests",
        sa.Column("guest_id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("events.event_id"), nullable=False, index=True),
        # The only thing required to start a list. One that can't be begun until
        # you have everybody's email is a list nobody begins.
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        # How many the host expects under this name. The guest corrects it when
        # they reply.
        sa.Column("party_size", sa.Integer, nullable=False, server_default="1"),
        sa.Column("token", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("note", sa.Text, nullable=True),
        # Set when a guest arrives through the open link rather than being added
        # by the host — worth being able to tell apart.
        sa.Column("self_added", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "guest_invites",
        sa.Column("invite_id", sa.String(36), primary_key=True),
        sa.Column("guest_id", sa.String(36), sa.ForeignKey("guests.guest_id"), nullable=False, index=True),
        sa.Column("function_id", sa.String(36), sa.ForeignKey("event_functions.function_id"), nullable=False, index=True),
        # no_reply | attending | declined. Being here is the invitation.
        sa.Column("status", sa.String(20), nullable=False, server_default="no_reply"),
        # What they actually said, which may not be what was expected of them.
        # The gap between this and guests.party_size is the reconciliation.
        sa.Column("attending_count", sa.Integer, nullable=True),
        sa.Column("responded_at", sa.DateTime, nullable=True),
        sa.UniqueConstraint("guest_id", "function_id", name="uq_guest_function"),
    )

    # The open link, minted when a host first asks for one.
    op.add_column("events", sa.Column("invite_token", sa.String(64), nullable=True))
    op.create_index("ix_events_invite_token", "events", ["invite_token"], unique=True)


def downgrade():
    op.drop_index("ix_events_invite_token", table_name="events")
    op.drop_column("events", "invite_token")
    op.drop_table("guest_invites")
    op.drop_table("guests")
    op.drop_table("event_functions")
