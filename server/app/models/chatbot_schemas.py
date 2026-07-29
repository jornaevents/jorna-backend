"""Pydantic models and enums for the chatbot bundle-builder API."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────


class ChatStep(str, Enum):
    EVENT_DETAILS = "event_details"
    EVENT_TIME = "event_time"
    ALREADY_BOOKED = "already_booked"
    STILL_NEED = "still_need"
    BUDGET = "budget"
    CUSTOM_BUDGET = "custom_budget"
    STYLE_PREFERENCES = "style_preferences"
    BUNDLE_ACTION = "bundle_action"
    MANUAL_CUSTOMIZE = "manual_customize"
    SWAP_VENDOR = "swap_vendor"
    REMOVE_CATEGORY = "remove_category"
    ADD_CATEGORY = "add_category"
    RESULTS_BOOKING = "results_booking"
    PARTIAL_BOOKING = "partial_booking"


class BudgetTier(str, Enum):
    BUDGET_FRIENDLY = "budget-friendly"
    MID_RANGE = "mid-range"
    PREMIUM = "premium"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


# All vendor categories — mirrors VendorCategory enum in schemas.py
VENDOR_CATEGORIES = [
    "venue",
    "planning",
    "catering",
    "bar_beverage",
    "cakes_desserts",
    "photography",
    "videography",
    "music_entertainment",
    "floral_decor",
    "rentals",
    "lighting_av",
    "beauty",
    "attire",
    "jewelry",
    "stationery",
    "transportation",
    "officiants",
    "guest_hospitality",
    "favors_gifts",
    "cultural_services",
    "post_wedding",
    "other",
]

CATEGORY_LABELS = {
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
    "lighting_av": "Lighting, AV & Production",
    "beauty": "Beauty",
    "attire": "Attire",
    "jewelry": "Jewelry",
    "stationery": "Stationery & Signage",
    "transportation": "Transportation",
    "officiants": "Officiants & Ceremony Services",
    "guest_hospitality": "Guest Hospitality",
    "favors_gifts": "Favors & Gifts",
    "cultural_services": "Specialty & Cultural Services",
    "post_wedding": "Post-Wedding Services",
    "other": "Other",
}


# Bundle slots the chatbot offers. Each slot is a distinct thing people book for
# a South Asian event and maps to a (db_category, db_subcategory) filter. This is
# how the chatbot targets specific subcategories — e.g. a Dhol player or a Mehndi
# artist — as standalone bundle items, rather than only their parent category.
# Two slots may share a db_category (e.g. DJ and Dhol are both
# music_entertainment) but never the same subcategory, so they stay distinct.
#
# The full VENDOR_CATEGORIES list is for vendor *registration*; categories not
# represented here (jewelry, stationery, transportation, etc.) remain discoverable
# via vendor search but are excluded from auto-generated bundles.
CHATBOT_SLOTS: dict[str, dict] = {
    "venue":             {"label": "Venue",             "category": "venue",               "subcategory": None},
    "catering":          {"label": "Catering",          "category": "catering",            "subcategory": None},
    "photography":       {"label": "Photography",       "category": "photography",         "subcategory": None},
    "videography":       {"label": "Videography",       "category": "videography",         "subcategory": None},
    "dj":                {"label": "DJ",                "category": "music_entertainment", "subcategory": "dj"},
    "dhol":              {"label": "Dhol",              "category": "music_entertainment", "subcategory": "dhol"},
    "floral_decor":      {"label": "Floral & Decor",    "category": "floral_decor",        "subcategory": None},
    "makeup":            {"label": "Makeup & Hair",     "category": "beauty",              "subcategory": "bridal_makeup"},
    "mehndi":            {"label": "Mehndi",            "category": "beauty",              "subcategory": "mehndi_artist"},
    "cultural_services": {"label": "Cultural Services", "category": "cultural_services",   "subcategory": None},
}

# Ordered list of slot keys — used for buttons, defaults, and request validation.
CHATBOT_CATEGORIES = list(CHATBOT_SLOTS.keys())


# ── Nested models ────────────────────────────────────────────────────


class DateRange(BaseModel):
    start: Optional[str] = None   # ISO date e.g. "2026-10-01"
    end: Optional[str] = None     # ISO date e.g. "2026-10-15"


class HelperButton(BaseModel):
    label: str
    value: str


class BundleItem(BaseModel):
    category: str
    vendor_id: Optional[str] = None
    service_id: Optional[str] = None
    service_name: Optional[str] = None  # the specific service filling this slot
    vendor_name: str
    pfp_url: Optional[str] = None
    price_min: float
    price_max: float
    rating: float
    match_reason: str


class Bundle(BaseModel):
    items: list[BundleItem] = []
    estimated_total_min: float = 0.0
    estimated_total_max: float = 0.0
    unfilled_categories: list[str] = Field(
        default_factory=list,
        description="Requested categories with no available vendor for the event "
        "(no real supply, or all booked on the date) — left out of the bundle so "
        "the client can be told what couldn't be filled.",
    )


# ── Chatbot state (carried by the client) ────────────────────────────


class ChatbotState(BaseModel):
    event_date: Optional[str] = None
    date_range: Optional[DateRange] = None
    location: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    guest_count: Optional[int] = None
    time_start: Optional[str] = None
    time_end: Optional[str] = None
    booked_categories: list[str] = Field(default_factory=list)
    needed_categories: list[str] = Field(default_factory=list)
    budget_tier: Optional[BudgetTier] = None
    budget_amount: Optional[str] = None
    style: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    bundle: Optional[Bundle] = None
    conversation_history: list[dict] = Field(
        default_factory=list,
        description="Recent user/assistant message pairs for LLM context (max 6)",
    )


# ── Request / Response ───────────────────────────────────────────────


def _split_categories(v: list[str]) -> list[str]:
    """Allow ['venue', 'catering'] or ['venue, catering'] — split comma-separated
    entries. Only core chatbot categories are accepted for bundle building."""
    result = []
    for item in v:
        for part in item.split(","):
            part = part.strip().lower()
            if part and part in CHATBOT_CATEGORIES:
                result.append(part)
    return result


class BundleOption(BaseModel):
    label: str
    description: str
    factors: list[str] = Field(
        default_factory=list,
        description="Ordered priority factors used to select vendors for this bundle",
    )
    bundle: Bundle
    state: "ChatbotState"
    bundle_id: Optional[str] = Field(
        default=None,
        description="DB bundle_id when the bundle was persisted — call POST /bundles/{bundle_id}/select to pick this one",
    )


class MultiBundleResponse(BaseModel):
    options: list[BundleOption]


class BundleRequest(BaseModel):
    """Single-shot bundle request — all fields optional.
    Provide whatever the user has selected and a bundle is returned immediately.
    """
    needed_categories: list[str] = Field(
        default_factory=list,
        description="Vendor categories to include e.g. ['dj', 'catering', 'venue']",
    )
    booked_categories: list[str] = Field(
        default_factory=list,
        description="Categories the user already has booked — excluded from the bundle",
    )

    from pydantic import field_validator

    @field_validator("needed_categories", "booked_categories", mode="before")
    @classmethod
    def split_comma_separated(cls, v: list[str]) -> list[str]:
        return _split_categories(v) if isinstance(v, list) else v
    budget_tier: Optional[BudgetTier] = Field(
        None,
        description="budget-friendly | mid-range | premium. Defaults to mid-range if omitted.",
    )
    budget_amount: Optional[str] = Field(
        None,
        description="Custom budget as a string e.g. '$10,000'. Only used when budget_tier is custom.",
    )
    event_date: Optional[str] = Field(None, description="Single event date e.g. '2026-10-15'")
    date_range: Optional[DateRange] = Field(None, description="Date range when the exact date is unknown")
    guest_count: Optional[int] = Field(None, description="Approximate number of guests")
    # Optional, and the bundle is built without them — but a vendor's day isn't
    # a single booking, so knowing the hours is what lets someone with a morning
    # job be offered for an evening one. Absent, every generated booking is
    # "TBD" and availability falls back to whole days.
    time_start: Optional[str] = Field(None, description="Event start time, 'HH:MM' e.g. '18:00'")
    time_end: Optional[str] = Field(None, description="Event end time, 'HH:MM' e.g. '23:00'")
    location: Optional[str] = Field(None, description="City or venue location")
    latitude: Optional[float] = Field(None, description="Event location latitude (for vendor travel-radius filtering)")
    longitude: Optional[float] = Field(None, description="Event location longitude (for vendor travel-radius filtering)")
    style: list[str] = Field(default_factory=list, description="Style preferences e.g. ['elegant', 'traditional']")
    preferences: list[str] = Field(default_factory=list, description="Vendor preferences e.g. ['pref_highly_rated', 'pref_local']")


class StepRequest(BaseModel):
    current_step: ChatStep
    user_input: Optional[str] = None           # free-text input
    selected_values: list[str] = Field(default_factory=list)  # button selections
    state: ChatbotState = Field(default_factory=ChatbotState)


class StepResponse(BaseModel):
    next_step: ChatStep
    bot_message: str
    helper_buttons: list[HelperButton] = Field(default_factory=list)
    state: ChatbotState
    bundle: Optional[Bundle] = None
    llm_response: bool = Field(
        default=False,
        description="True when the response was generated by the LLM fallback",
    )
    bundle_id: Optional[str] = Field(
        default=None,
        description="DB bundle ID created when the user confirms a bundle",
    )
    booking_ids: list[str] = Field(
        default_factory=list,
        description="DB booking IDs created when the user books vendors from a bundle",
    )
    is_complete: bool = Field(
        default=False,
        description="True when the flow is done and bookings have been created — frontend should navigate to the bundle page",
    )
