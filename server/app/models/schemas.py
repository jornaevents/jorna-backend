from enum import Enum


class BookingStatus(str, Enum):
    PENDING = "pending"
    NEGOTIATION_ONGOING = "negotiation_ongoing"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAYMENT_CONFIRMED = "payment_confirmed"


class RejectionReason(str, Enum):
    """Why a booking's status became REJECTED — that one status value covers
    three different real events, and nothing recorded which. A client-
    initiated cancellation isn't a member here: it already has its own
    marker (`Booking.cancelled_at`), set by the same code path that would
    otherwise need to write CLIENT_CANCELLED here too.
    """

    VENDOR_DECLINED = "vendor_declined"
    VENDOR_WITHDREW = "vendor_withdrew"
    RESCHEDULE_FAILED = "reschedule_failed"


class PriceUnit(str, Enum):
    """What a service's rate multiplies by.

    The column behind this was free text, and two functions read it with
    different ideas of what counted as per-person: a caterer priced "per head"
    passed the check that decides whether a request may be sent, then failed the
    one that works out what to charge. The request went out, the vendor
    accepted, and nobody could pay them.

    Four values, and the column now holds nothing else. What arrives is still
    read forgivingly — clients have been sending "Per Hour" for years — but it
    is stored canonical, so there is only one string for anything downstream to
    have an opinion about.
    """

    PERSON = "person"
    HOUR = "hour"
    DAY = "day"
    EVENT = "event"
    PERFORMER = "performer"


PRICE_UNITS = tuple(u.value for u in PriceUnit)


class VendorCategory(str, Enum):
    VENUE = "venue"
    PLANNING = "planning"
    CATERING = "catering"
    BAR_BEVERAGE = "bar_beverage"
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


# Display names for each category. Kept beside the enum so the taxonomy and the
# words for it live together — clients read these from /vendors/categories rather
# than each inventing their own.
CATEGORY_LABELS: dict[str, str] = {
    "venue": "Venue",
    "planning": "Planning & Coordination",
    "catering": "Catering",
    "bar_beverage": "Bar & Beverage",
    "cakes_desserts": "Cakes & Desserts",
    "photography": "Photography",
    "videography": "Videography",
    "music_entertainment": "Music & Entertainment",
    "floral_decor": "Floral & Decor",
    "rentals": "Rentals",
    "lighting_av": "Lighting & AV",
    "beauty": "Beauty",
    "attire": "Attire",
    "jewelry": "Jewelry",
    "stationery": "Stationery",
    "transportation": "Transportation",
    "officiants": "Officiants",
    "guest_hospitality": "Guest Hospitality",
    "favors_gifts": "Favors & Gifts",
    "cultural_services": "Cultural Services",
    "post_wedding": "Post-Wedding",
    "other": "Other",
}

# Valid subcategories per category. Categories not listed here have no subcategories.
VENDOR_SUBCATEGORIES: dict[str, list[str]] = {
    "music_entertainment": [
        "dj", "dhol", "tabla", "sitar", "veena", "bansuri",
        "shehnai", "harmonium", "sarangi", "vocalist",
        "live_band", "brass_band", "mc",
    ],
    "beauty": [
        "bridal_makeup", "hair_stylist", "mehndi_artist", "nail_artist",
    ],
    "floral_decor": [
        "floral", "decor",
    ],
    "officiants": [
        "hindu_pandit", "muslim_qazi", "sikh_granthi",
        "christian_officiant", "civil_officiant",
    ],
    "cultural_services": [
        "bhangra_troupe", "giddha_troupe", "baraat_dancers",
        "turban_tying", "doli_palki", "fire_act",
    ],
    "cakes_desserts": [
        "wedding_cake", "general_desserts",
    ],
    "attire": [
        "bridal", "groom", "bridal_party", "groomsmen", "general",
    ],
}

SUBCATEGORY_LABELS: dict[str, str] = {
    # Music & Entertainment
    "dj": "DJ",
    "dhol": "Dhol",
    "tabla": "Tabla",
    "sitar": "Sitar",
    "veena": "Veena",
    "bansuri": "Bansuri / Flute",
    "shehnai": "Shehnai",
    "harmonium": "Harmonium",
    "sarangi": "Sarangi",
    "vocalist": "Vocalist / Singer",
    "live_band": "Live Band",
    "brass_band": "Brass Band / Baraat Band",
    "mc": "MC / Emcee",
    # Beauty
    "bridal_makeup": "Bridal Makeup",
    "hair_stylist": "Hair Stylist",
    "mehndi_artist": "Mehndi Artist",
    "nail_artist": "Nail Artist",
    # Floral & Decor
    "floral": "Floral",
    "decor": "Decor",
    # Officiants
    "hindu_pandit": "Hindu Pandit / Priest",
    "muslim_qazi": "Muslim Qazi / Imam",
    "sikh_granthi": "Sikh Granthi",
    "christian_officiant": "Christian Officiant",
    "civil_officiant": "Civil / Non-denominational Officiant",
    # Cultural Services
    "bhangra_troupe": "Bhangra Troupe",
    "giddha_troupe": "Giddha Troupe",
    "baraat_dancers": "Baraat Dancers",
    "turban_tying": "Turban Tying",
    "doli_palki": "Doli / Palki Service",
    "fire_act": "Fire Act / Specialty Performance",
    # Cakes & Desserts
    "wedding_cake": "Wedding Cake",
    "general_desserts": "General Desserts",
    # Attire
    "bridal": "Bridal",
    "groom": "Groom",
    "bridal_party": "Bridal Party",
    "groomsmen": "Groomsmen",
    "general": "General",
}

