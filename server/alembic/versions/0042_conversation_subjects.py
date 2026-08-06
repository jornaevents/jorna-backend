"""a conversation is about something, and a message has a kind

Revision ID: 0042_conversation_subjects
Revises: 0041_change_requests
Create Date: 2026-08-06

There were two messaging systems. `conversations` held a group chat per bundle,
with a socket, a membership table and per-user read receipts. `messages` held a
1:1 thread per booking, with none of those, and no caller anywhere in the web
app — a complete system nothing had ever opened.

Neither could answer the question a client actually has first, which is asked
before any of this exists: "can you do the 14th?" A conversation could not be
created without a bundle, because bundle_id was NOT NULL.

So a conversation gains a subject — a bundle, a booking, or an enquiry about a
vendor — and the two systems become one. `messages` rows are copied into
two-member conversations and the old table is left in place, empty of meaning
but intact, until iOS is confirmed off it. Nothing reads it after this.

A message also gains a kind. Price negotiation has always been a conversation
held somewhere else: offers carry a message field, a timestamp and an author,
and were rendered in a panel beside the thread they belonged in. Writing them
as typed messages puts one ordering on the whole exchange.
"""
from alembic import op
import sqlalchemy as sa

revision = "0042_conversation_subjects"
down_revision = "0041_change_requests"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # ── The subject ──────────────────────────────────────────────────
    #
    # server_default 'bundle' rather than a backfill: every conversation that
    # exists today is a bundle chat, and the default is what makes the column
    # NOT NULL without a second pass.
    op.add_column(
        "conversations",
        sa.Column("subject_type", sa.String(16), nullable=False, server_default="bundle"),
    )
    op.add_column("conversations", sa.Column("booking_id", sa.String(36), nullable=True))
    op.add_column("conversations", sa.Column("vendor_id", sa.String(36), nullable=True))
    # Denormalised so an enquiry can be found, and constrained, without walking
    # the membership table. The other member is the vendor's user.
    op.add_column("conversations", sa.Column("client_user_id", sa.String(36), nullable=True))

    with op.batch_alter_table("conversations") as batch:
        # The whole point: a conversation no longer needs a plan to exist.
        batch.alter_column("bundle_id", existing_type=sa.String(36), nullable=True)
        batch.create_foreign_key(
            "fk_conversations_booking_id", "bookings", ["booking_id"], ["booking_id"]
        )
        batch.create_foreign_key(
            "fk_conversations_vendor_id", "vendors", ["vendor_id"], ["vendor_id"]
        )
        batch.create_foreign_key(
            "fk_conversations_client_user_id", "users", ["client_user_id"], ["user_id"]
        )

    op.create_index("ix_conversations_booking_id", "conversations", ["booking_id"])
    op.create_index("ix_conversations_vendor_id", "conversations", ["vendor_id"])

    # One enquiry thread per client and vendor, enforced here rather than only
    # in the service — find-or-create races, and two threads with the same two
    # people in them is a bug nobody can see until a vendor answers the wrong
    # one. Partial, so it says nothing about bundle or booking conversations,
    # which are legitimately many per pair.
    op.create_index(
        "uq_conversations_enquiry",
        "conversations",
        ["vendor_id", "client_user_id"],
        unique=True,
        sqlite_where=sa.text("subject_type = 'enquiry'"),
        postgresql_where=sa.text("subject_type = 'enquiry'"),
    )

    # ── The kind ─────────────────────────────────────────────────────
    op.add_column(
        "group_messages",
        sa.Column("kind", sa.String(16), nullable=False, server_default="text"),
    )
    # JSON rather than columns. An offer carries an amount and an offer_id, a
    # reference card carries a service_id, a system notice carries neither —
    # three sets of nullable columns to hold them is how a message table turns
    # into a booking table. Nothing queries inside it; it is read only when the
    # message that owns it is rendered.
    op.add_column("group_messages", sa.Column("meta", sa.JSON, nullable=True))

    # ── The old 1:1 threads ──────────────────────────────────────────
    #
    # One conversation per booking that has messages, its two parties as
    # members, and every message copied with its author and timestamp intact. A
    # thread that lost its order or its authorship would be worse than one that
    # was left behind.
    bookings_with_messages = conn.execute(
        sa.text(
            """
            SELECT m.booking_id, b.user_id AS client_user_id, v.user_id AS vendor_user_id,
                   b.vendor_id, MIN(m.created_at) AS started_at
              FROM messages m
              JOIN bookings b ON b.booking_id = m.booking_id
              JOIN vendors v ON v.vendor_id = b.vendor_id
             GROUP BY m.booking_id, b.user_id, v.user_id, b.vendor_id
            """
        )
    ).fetchall()

    for booking_id, client_user_id, vendor_user_id, vendor_id, started_at in bookings_with_messages:
        conv_id = _uuid()
        conn.execute(
            sa.text(
                """
                INSERT INTO conversations
                    (conversation_id, bundle_id, booking_id, vendor_id, client_user_id,
                     subject_type, type, name, created_at)
                VALUES
                    (:cid, NULL, :bid, :vid, :client, 'booking', 'direct', :name, :created)
                """
            ),
            {
                "cid": conv_id,
                "bid": booking_id,
                "vid": vendor_id,
                "client": client_user_id,
                # Named at read time from the booking's service; a name frozen
                # here would go stale the first time a listing was renamed.
                "name": "",
                "created": started_at,
            },
        )

        for uid in dict.fromkeys([client_user_id, vendor_user_id]):
            conn.execute(
                sa.text(
                    "INSERT INTO conversation_members"
                    " (member_id, conversation_id, user_id, joined_at)"
                    " VALUES (:mid, :cid, :uid, :joined)"
                ),
                {"mid": _uuid(), "cid": conv_id, "uid": uid, "joined": started_at},
            )

        rows = conn.execute(
            sa.text(
                "SELECT message_id, sender_id, receiver_id, content, created_at, is_read"
                "  FROM messages WHERE booking_id = :bid ORDER BY created_at ASC"
            ),
            {"bid": booking_id},
        ).fetchall()

        for _old_id, sender_id, receiver_id, content, created_at, is_read in rows:
            new_id = _uuid()
            conn.execute(
                sa.text(
                    "INSERT INTO group_messages"
                    " (message_id, conversation_id, sender_id, content, created_at, kind)"
                    " VALUES (:mid, :cid, :sender, :content, :created, 'text')"
                ),
                {
                    "mid": new_id,
                    "cid": conv_id,
                    "sender": sender_id,
                    "content": content,
                    "created": created_at,
                },
            )
            # The sender has read their own message, here as everywhere else.
            reads = [sender_id] + ([receiver_id] if is_read else [])
            for uid in dict.fromkeys(reads):
                conn.execute(
                    sa.text(
                        "INSERT INTO group_message_reads (read_id, message_id, user_id, read_at)"
                        " VALUES (:rid, :mid, :uid, :read_at)"
                    ),
                    {"rid": _uuid(), "mid": new_id, "uid": uid, "read_at": created_at},
                )


def downgrade():
    # The copied threads go first: leaving them would strand booking
    # conversations with no column left to say what they are about.
    conn = op.get_bind()
    conv_ids = [
        row[0]
        for row in conn.execute(
            sa.text("SELECT conversation_id FROM conversations WHERE subject_type = 'booking'")
        ).fetchall()
    ]
    for cid in conv_ids:
        conn.execute(
            sa.text(
                "DELETE FROM group_message_reads WHERE message_id IN"
                " (SELECT message_id FROM group_messages WHERE conversation_id = :cid)"
            ),
            {"cid": cid},
        )
        conn.execute(
            sa.text("DELETE FROM group_messages WHERE conversation_id = :cid"), {"cid": cid}
        )
        conn.execute(
            sa.text("DELETE FROM conversation_members WHERE conversation_id = :cid"), {"cid": cid}
        )
        conn.execute(
            sa.text("DELETE FROM conversations WHERE conversation_id = :cid"), {"cid": cid}
        )

    # An enquiry has no bundle to fall back to, so it cannot survive a column
    # that is about to be NOT NULL again.
    conn.execute(sa.text("DELETE FROM conversations WHERE subject_type = 'enquiry'"))

    op.drop_column("group_messages", "meta")
    op.drop_column("group_messages", "kind")

    op.drop_index("uq_conversations_enquiry", table_name="conversations")
    op.drop_index("ix_conversations_vendor_id", table_name="conversations")
    op.drop_index("ix_conversations_booking_id", table_name="conversations")

    with op.batch_alter_table("conversations") as batch:
        batch.drop_constraint("fk_conversations_client_user_id", type_="foreignkey")
        batch.drop_constraint("fk_conversations_vendor_id", type_="foreignkey")
        batch.drop_constraint("fk_conversations_booking_id", type_="foreignkey")
        batch.alter_column("bundle_id", existing_type=sa.String(36), nullable=False)

    op.drop_column("conversations", "client_user_id")
    op.drop_column("conversations", "vendor_id")
    op.drop_column("conversations", "booking_id")
    op.drop_column("conversations", "subject_type")


def _uuid() -> str:
    import uuid

    return str(uuid.uuid4())
