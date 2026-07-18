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

