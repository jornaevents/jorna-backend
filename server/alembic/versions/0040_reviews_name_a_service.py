"""a review names the listing it is about

Revision ID: 0040_review_service
Revises: 0039_price_unit
Create Date: 2026-07-30

A review was attached to a vendor and nothing narrower, so a vendor's rating was
one number covering everything they sell. That is the wrong number to print on a
service page: a decorator who is excellent at mandap work and ordinary at
florals had both listings claiming the blended score, which overstates one and
undersells the other.

The information was always there — a review points at a booking, and a booking
names its service — but two joins deep is not somewhere a listing page can read
from, and no aggregate could be computed without walking every review.

So the service is recorded on the review, backfilled here from the booking, and
each listing carries its own average the way vendors already do.

The vendor's rating does not change meaning and is not recomputed: it is still
the mean of every review naming that vendor. Both averages come from the same
rows, so they cannot contradict each other.
"""
from alembic import op
import sqlalchemy as sa

revision = "0040_review_service"
down_revision = "0039_price_unit"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    op.add_column("reviews", sa.Column("service_id", sa.String(36), nullable=True))
    op.add_column(
        "services",
        sa.Column("rating", sa.Float, nullable=False, server_default="0"),
    )
    op.add_column(
        "services",
        sa.Column("num_reviews", sa.Integer, nullable=False, server_default="0"),
    )

    # bookings.service_id is NOT NULL, so every existing review gets one.
    conn.execute(
        sa.text(
            """
            UPDATE reviews
               SET service_id = (
                   SELECT b.service_id FROM bookings b
                    WHERE b.booking_id = reviews.booking_id
               )
            """
        )
    )

    # Then the aggregates, from the rows that now exist rather than from a second
    # pass over bookings — so what a listing displays is arithmetic on the very
    # reviews a client can scroll through under it, on day one as afterwards.
    #
    # Averaged in Python, not in SQL. Postgres has no round(double precision, int)
    # — only round(numeric, int) — so the obvious ROUND(AVG(...), 2) parses here
    # and fails there, on the deploy, after the columns are already added.
    # Services with no reviews keep the 0 default and are not visited.
    rows = conn.execute(
        sa.text(
            """
            SELECT service_id, COUNT(*) AS n, AVG(rating) AS avg_rating
              FROM reviews
             WHERE service_id IS NOT NULL
             GROUP BY service_id
            """
        )
    ).fetchall()

    for service_id, n, avg_rating in rows:
        conn.execute(
            sa.text(
                "UPDATE services SET num_reviews = :n, rating = :r"
                " WHERE service_id = :id"
            ),
            {"n": n, "r": round(float(avg_rating), 2), "id": service_id},
        )

    op.create_index("ix_reviews_service_id", "reviews", ["service_id"])

    # batch mode so this runs on SQLite too — the only way to check the backfill
    # above against a real database before it touches production.
    with op.batch_alter_table("reviews") as batch:
        batch.create_foreign_key(
            "fk_reviews_service_id", "services", ["service_id"], ["service_id"]
        )


def downgrade():
    with op.batch_alter_table("reviews") as batch:
        batch.drop_constraint("fk_reviews_service_id", type_="foreignkey")
    op.drop_index("ix_reviews_service_id", table_name="reviews")
    op.drop_column("services", "num_reviews")
    op.drop_column("services", "rating")
    op.drop_column("reviews", "service_id")
