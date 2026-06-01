from enum import Enum


class BookingStatus(str, Enum):
    PENDING = "pending"
    NEGOTIATION_ONGOING = "negotiation_ongoing"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAYMENT_CONFIRMED = "payment_confirmed"


class VendorCategory(str, Enum):
    DJ = "dj"
    DHOL = "dhol"
    VENUE = "venue"
    CATERING = "catering"
    PHOTOGRAPHY = "photography"
    VIDEOGRAPHY = "videography"
    DECORATION = "decoration"
    MEHNDI = "mehndi"
    PLANNING = "planning"
    MUA = "mua"
    OTHER = "other"

