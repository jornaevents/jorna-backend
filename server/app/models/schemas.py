import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional

def generate_uuid():
    """Generates a UUIDv4 string for primary keys."""
    return str(uuid.uuid4())

class BookingStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAYMENT_CONFIRMED = "payment_confirmed"

@dataclass(kw_only=True)
class User:
    username: str
    email: str
    phone: str  # Store as string to handle country codes/leading zeros
    password: str
    f_name: str
    l_name: str
    age: int
    location: str  # Standardize with ISO 3166-1 alpha-2 codes (e.g., "US", "IN")
    gender: str
    language: str
    pfp_url: Optional[str] = None # Store the URL/Path, not the image binary
    user_id: str = field(default_factory=generate_uuid)

@dataclass(kw_only=True)
class Service:
    name: str
    price: float
    duration_minutes: int  # Explicit units (int) prevent float precision errors
    vendor_id: str # Foreign Key to Vendor
    experience: str
    media: Optional[List[str]] = None
    service_id: str = field(default_factory=generate_uuid)

@dataclass(kw_only=True)
class Vendor(User):
    bio: str
    # tags: List[str] # In SQLite, this would be a separate JOIN table
    rating: float
    num_events: int
    vendor_id: str = field(default_factory=generate_uuid)

@dataclass(kw_only=True)
class Booking:
    user_id: str
    vendor_id: str
    service_id: str
    event_name: str
    time_start: str
    time_end: str
    location: str
    date_iso: str = field(default_factory=lambda: datetime.now().isoformat())  # Use ISO-8601 Strings for SQLite/Swift compatibility
    status: str = BookingStatus.PENDING.value  # Store the raw string value?
    booking_id: str = field(default_factory=generate_uuid)

