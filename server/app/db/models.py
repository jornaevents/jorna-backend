"""SQLAlchemy table definitions for User, Vendor, Service, Booking, Tag."""
import uuid
from sqlalchemy import Column, String, Integer, Float, Text, ForeignKey, JSON, Table, Boolean, DateTime, UniqueConstraint
from sqlalchemy.orm import relationship

from .database import Base


def uuid_str():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    user_id = Column(String(36), primary_key=True, default=uuid_str)
    username = Column(String(255), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    phone = Column(String(50), nullable=True)
    password = Column(String(255), nullable=False)
    f_name = Column(String(255), nullable=False)
    l_name = Column(String(255), nullable=False)
    age = Column(Integer, nullable=True)
    location = Column(String(100), nullable=True)
    gender = Column(String(50), nullable=True)
    language = Column(String(50), nullable=True)
    pfp_url = Column(String(512), nullable=True)

    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    city = Column(String(100), nullable=True)
    state = Column(String(50), nullable=True)

    # Firebase Cloud Messaging token for push notifications
    fcm_token = Column(String(512), nullable=True)

    # Supabase Auth user id (UUID) when this Jorna account is linked to Google sign-in
    supabase_user_id = Column(String(36), unique=True, nullable=True)

    # Incremented on logout or password change to invalidate all previously issued tokens
    token_version = Column(Integer, nullable=False, default=0)

    is_admin = Column(Boolean, nullable=False, default=False)


# Many-to-many join table: one vendor has many tags, one tag belongs to many vendors.
vendor_tags = Table(
    "vendor_tags",
    Base.metadata,
    Column("vendor_id", String(36), ForeignKey("vendors.vendor_id"), primary_key=True),
    Column("tag_id",    String(36), ForeignKey("tags.tag_id"),    primary_key=True),
)


class Tag(Base):
    __tablename__ = "tags"

    tag_id = Column(String(36), primary_key=True, default=uuid_str)
    # Normalized (lowercase, stripped) tag name — unique across the whole table.
    name = Column(String(100), unique=True, nullable=False, index=True)


class Vendor(Base):
    __tablename__ = "vendors"

    vendor_id = Column(String(36), primary_key=True, default=uuid_str)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    bio = Column(Text, nullable=False)
    category = Column(String(50), nullable=False, default="other")
    rating = Column(Float, nullable=False)
    num_events = Column(Integer, nullable=False)

    travel_radius_miles = Column(Integer, default=30)
    google_access_token = Column(String(512), nullable=True)
    google_refresh_token = Column(String(512), nullable=True)
    calendar_id = Column(String(255), nullable=True)

    # Stripe Connect — set during vendor onboarding
    stripe_account_id = Column(String(255), nullable=True)
    stripe_onboarding_complete = Column(Boolean, nullable=False, default=False)

    tags = relationship("Tag", secondary=vendor_tags, backref="vendors")


class Service(Base):
    __tablename__ = "services"

    service_id = Column(String(36), primary_key=True, default=uuid_str)
    name = Column(String(255), nullable=False)
    price = Column(Float, nullable=False)
    duration_minutes = Column(Integer, nullable=True)
    vendor_id = Column(String(36), ForeignKey("vendors.vendor_id"), nullable=False, index=True)
    experience = Column(Text, nullable=False)
    media = Column(JSON, nullable=True)
    category = Column(String(50), nullable=True)
    price_unit = Column(String(50), nullable=True)
    description = Column(Text, nullable=True)


class Booking(Base):
    __tablename__ = "bookings"

    booking_id = Column(String(36), primary_key=True, default=uuid_str)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    vendor_id = Column(String(36), ForeignKey("vendors.vendor_id"), nullable=False, index=True)
    service_id = Column(String(36), ForeignKey("services.service_id"), nullable=False, index=True)
    event_name = Column(String(255), nullable=False)
    time_start = Column(String(50), nullable=False)
    time_end = Column(String(50), nullable=False)
    location = Column(String(255), nullable=False)
    date_iso = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False, default="pending")

    venue_latitude = Column(Float, nullable=True)
    venue_longitude = Column(Float, nullable=True)
    client_checked_in_at = Column(String(50), nullable=True)
    vendor_checked_in_at = Column(String(50), nullable=True)

    # Payment — populated when the customer pays after booking is confirmed
    payment_intent_id = Column(String(255), nullable=True, index=True)
    # unpaid | processing | paid | released | refunded | disputed
    payment_status = Column(String(50), nullable=False, default="unpaid")
    amount_cents = Column(Integer, nullable=True)       # total charged to customer
    platform_fee_cents = Column(Integer, nullable=True) # Desiconnect's cut
    currency = Column(String(10), nullable=False, default="usd")
    confirmed_at = Column(DateTime, nullable=True)      # when vendor approved — refund window starts here
    paid_at = Column(DateTime, nullable=True)           # when Stripe payment succeeded
    customer_confirmed_at = Column(DateTime, nullable=True)
    vendor_confirmed_at = Column(DateTime, nullable=True)
    funds_released_at = Column(DateTime, nullable=True)


class StripeWebhookEvent(Base):
    """Tracks processed Stripe webhook event IDs to prevent duplicate processing."""

    __tablename__ = "stripe_webhook_events"

    event_id = Column(String(255), primary_key=True)  # Stripe's evt_... ID
    processed_at = Column(DateTime, nullable=False)


class VendorAvailability(Base):
    __tablename__ = "vendor_availability"

    availability_id = Column(String(36), primary_key=True, default=uuid_str)
    vendor_id = Column(String(36), ForeignKey("vendors.vendor_id"), nullable=False, index=True)
    day_of_week = Column(Integer, nullable=False)
    start_time = Column(String(5), nullable=False)
    end_time = Column(String(5), nullable=False)


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("booking_id", name="uq_review_booking"),)

    review_id = Column(String(36), primary_key=True, default=uuid_str)
    booking_id = Column(String(36), ForeignKey("bookings.booking_id"), nullable=False, index=True)
    vendor_id = Column(String(36), ForeignKey("vendors.vendor_id"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    rating = Column(Float, nullable=False)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False)


class Event(Base):
    __tablename__ = "events"

    event_id = Column(String(36), primary_key=True, default=uuid_str)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    date_iso = Column(String(50), nullable=False)
    location = Column(String(255), nullable=False)
    event_type = Column(String(100), nullable=True)
    description = Column(Text, nullable=True)
    guest_count = Column(Integer, nullable=True)
    budget = Column(Float, nullable=True)
    services_needed = Column(JSON, nullable=True)
