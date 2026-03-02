"""SQLAlchemy table definitions for User, Vendor, Service, Booking."""
import uuid
from sqlalchemy import Column, String, Integer, Float, Text, ForeignKey
from sqlalchemy.dialects.sqlite import JSON

from .database import Base


def uuid_str():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    user_id = Column(String(36), primary_key=True, default=uuid_str)
    username = Column(String(255), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    phone = Column(String(50), nullable=False)
    password = Column(String(255), nullable=False)
    f_name = Column(String(255), nullable=False)
    l_name = Column(String(255), nullable=False)
    age = Column(Integer, nullable=False)
    location = Column(String(10), nullable=False)
    gender = Column(String(50), nullable=False)
    language = Column(String(50), nullable=False)
    pfp_url = Column(String(512), nullable=True)

    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    city = Column(String(100), nullable=True)
    state = Column(String(50), nullable=True)

    # Firebase Cloud Messaging token for push notifications
    fcm_token = Column(String(512), nullable=True)


class Vendor(Base):
    __tablename__ = "vendors"

    vendor_id = Column(String(36), primary_key=True, default=uuid_str)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    bio = Column(Text, nullable=False)
    rating = Column(Float, nullable=False)
    num_events = Column(Integer, nullable=False)

    travel_radius_miles = Column(Integer, default=30)
    google_access_token = Column(String(512), nullable=True)
    google_refresh_token = Column(String(512), nullable=True)
    calendar_id = Column(String(255), nullable=True)


class Service(Base):
    __tablename__ = "services"

    service_id = Column(String(36), primary_key=True, default=uuid_str)
    name = Column(String(255), nullable=False)
    price = Column(Float, nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    vendor_id = Column(String(36), ForeignKey("vendors.vendor_id"), nullable=False)
    experience = Column(Text, nullable=False)
    media = Column(JSON, nullable=True)


class Booking(Base):
    __tablename__ = "bookings"

    booking_id = Column(String(36), primary_key=True, default=uuid_str)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    vendor_id = Column(String(36), ForeignKey("vendors.vendor_id"), nullable=False)
    service_id = Column(String(36), ForeignKey("services.service_id"), nullable=False)
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


class VendorAvailability(Base):
    __tablename__ = "vendor_availability"

    availability_id = Column(String(36), primary_key=True, default=uuid_str)
    vendor_id = Column(String(36), ForeignKey("vendors.vendor_id"), nullable=False)
    day_of_week = Column(Integer, nullable=False)
    start_time = Column(String(5), nullable=False)
    end_time = Column(String(5), nullable=False)
