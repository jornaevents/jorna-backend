"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(255), nullable=False, unique=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("password", sa.String(255), nullable=False),
        sa.Column("f_name", sa.String(255), nullable=False),
        sa.Column("l_name", sa.String(255), nullable=False),
        sa.Column("age", sa.Integer, nullable=False),
        sa.Column("location", sa.String(100), nullable=False),
        sa.Column("gender", sa.String(50), nullable=False),
        sa.Column("language", sa.String(50), nullable=False),
        sa.Column("pfp_url", sa.String(512), nullable=True),
        sa.Column("latitude", sa.Float, nullable=True),
        sa.Column("longitude", sa.Float, nullable=True),
        sa.Column("city", sa.String(100), nullable=True),
        sa.Column("state", sa.String(50), nullable=True),
        sa.Column("fcm_token", sa.String(512), nullable=True),
        sa.Column("supabase_user_id", sa.String(36), nullable=True, unique=True),
    )

    op.create_table(
        "tags",
        sa.Column("tag_id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
    )
    op.create_index("ix_tags_name", "tags", ["name"])

    op.create_table(
        "vendors",
        sa.Column("vendor_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("bio", sa.Text, nullable=False),
        sa.Column("category", sa.String(50), nullable=False, server_default="other"),
        sa.Column("rating", sa.Float, nullable=False),
        sa.Column("num_events", sa.Integer, nullable=False),
        sa.Column("travel_radius_miles", sa.Integer, server_default="30"),
        sa.Column("google_access_token", sa.String(512), nullable=True),
        sa.Column("google_refresh_token", sa.String(512), nullable=True),
        sa.Column("calendar_id", sa.String(255), nullable=True),
        sa.Column("stripe_account_id", sa.String(255), nullable=True),
        sa.Column("stripe_onboarding_complete", sa.Boolean, nullable=False, server_default="false"),
    )
    op.create_index("ix_vendors_user_id", "vendors", ["user_id"])

    op.create_table(
        "vendor_tags",
        sa.Column("vendor_id", sa.String(36), sa.ForeignKey("vendors.vendor_id"), primary_key=True),
        sa.Column("tag_id", sa.String(36), sa.ForeignKey("tags.tag_id"), primary_key=True),
    )

    op.create_table(
        "services",
        sa.Column("service_id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("price", sa.Float, nullable=False),
        sa.Column("duration_minutes", sa.Integer, nullable=True),
        sa.Column("vendor_id", sa.String(36), sa.ForeignKey("vendors.vendor_id"), nullable=False),
        sa.Column("experience", sa.Text, nullable=False),
        sa.Column("media", sa.JSON, nullable=True),
        sa.Column("category", sa.String(50), nullable=True),
        sa.Column("price_unit", sa.String(50), nullable=True),
        sa.Column("description", sa.Text, nullable=True),
    )
    op.create_index("ix_services_vendor_id", "services", ["vendor_id"])

    op.create_table(
        "bookings",
        sa.Column("booking_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("vendor_id", sa.String(36), sa.ForeignKey("vendors.vendor_id"), nullable=False),
        sa.Column("service_id", sa.String(36), sa.ForeignKey("services.service_id"), nullable=False),
        sa.Column("event_name", sa.String(255), nullable=False),
        sa.Column("time_start", sa.String(50), nullable=False),
        sa.Column("time_end", sa.String(50), nullable=False),
        sa.Column("location", sa.String(255), nullable=False),
        sa.Column("date_iso", sa.String(50), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("venue_latitude", sa.Float, nullable=True),
        sa.Column("venue_longitude", sa.Float, nullable=True),
        sa.Column("client_checked_in_at", sa.String(50), nullable=True),
        sa.Column("vendor_checked_in_at", sa.String(50), nullable=True),
        sa.Column("payment_intent_id", sa.String(255), nullable=True),
        sa.Column("payment_status", sa.String(50), nullable=False, server_default="unpaid"),
        sa.Column("amount_cents", sa.Integer, nullable=True),
        sa.Column("platform_fee_cents", sa.Integer, nullable=True),
        sa.Column("currency", sa.String(10), nullable=False, server_default="usd"),
        sa.Column("confirmed_at", sa.DateTime, nullable=True),
        sa.Column("paid_at", sa.DateTime, nullable=True),
        sa.Column("customer_confirmed_at", sa.DateTime, nullable=True),
        sa.Column("vendor_confirmed_at", sa.DateTime, nullable=True),
        sa.Column("funds_released_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_bookings_user_id", "bookings", ["user_id"])
    op.create_index("ix_bookings_vendor_id", "bookings", ["vendor_id"])
    op.create_index("ix_bookings_service_id", "bookings", ["service_id"])
    op.create_index("ix_bookings_payment_intent_id", "bookings", ["payment_intent_id"])

    op.create_table(
        "stripe_webhook_events",
        sa.Column("event_id", sa.String(255), primary_key=True),
        sa.Column("processed_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "vendor_availability",
        sa.Column("availability_id", sa.String(36), primary_key=True),
        sa.Column("vendor_id", sa.String(36), sa.ForeignKey("vendors.vendor_id"), nullable=False),
        sa.Column("day_of_week", sa.Integer, nullable=False),
        sa.Column("start_time", sa.String(5), nullable=False),
        sa.Column("end_time", sa.String(5), nullable=False),
    )
    op.create_index("ix_vendor_availability_vendor_id", "vendor_availability", ["vendor_id"])


def downgrade() -> None:
    op.drop_table("vendor_availability")
    op.drop_table("stripe_webhook_events")
    op.drop_table("bookings")
    op.drop_table("services")
    op.drop_table("vendor_tags")
    op.drop_table("vendors")
    op.drop_table("tags")
    op.drop_table("users")
