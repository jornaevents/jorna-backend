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


class Vendor(Base):
    __tablename__ = "vendors"

    vendor_id = Column(String(36), primary_key=True, default=uuid_str)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    bio = Column(Text, nullable=False)
    rating = Column(Float, nullable=False)
    num_events = Column(Integer, nullable=False)


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
