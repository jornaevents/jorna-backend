from enum import Enum


class BookingStatus(str, Enum):
    PENDING = "pending"
    NEGOTIATION_ONGOING = "negotiation_ongoing"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAYMENT_CONFIRMED = "payment_confirmed"


class VendorCategory(str, Enum):
    VENUE = "venue"
    PLANNING = "planning"
    CATERING = "catering"
    CAKES_DESSERTS = "cakes_desserts"
    PHOTOGRAPHY = "photography"
    VIDEOGRAPHY = "videography"
    MUSIC_ENTERTAINMENT = "music_entertainment"
    FLORAL_DECOR = "floral_decor"
    RENTALS = "rentals"
    LIGHTING_AV = "lighting_av"
    BEAUTY = "beauty"
    ATTIRE = "attire"
    JEWELRY = "jewelry"
    STATIONERY = "stationery"
    TRANSPORTATION = "transportation"
    OFFICIANTS = "officiants"
    GUEST_HOSPITALITY = "guest_hospitality"
    FAVORS_GIFTS = "favors_gifts"
    CULTURAL_SERVICES = "cultural_services"
    POST_WEDDING = "post_wedding"
    OTHER = "other"

