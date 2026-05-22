"""Pydantic models and enums for the chatbot bundle-builder API."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────


class ChatStep(str, Enum):
    EVENT_DETAILS = "event_details"
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


# Categories the chatbot offers (display labels → internal keys)
VENDOR_CATEGORIES = [
    "venue",
    "catering",
    "decor",
    "photographer",
    "dj",
    "mehndi",
    "dhol",
]

CATEGORY_LABELS = {
    "venue": "Venue",
    "catering": "Catering",
    "decor": "Decor",
    "photographer": "Photographer",
    "dj": "DJ",
    "mehndi": "Mehndi",
    "dhol": "Dhol",
}


# ── Nested models ────────────────────────────────────────────────────


class DateRange(BaseModel):
    start: Optional[str] = None   # ISO date e.g. "2026-10-01"
    end: Optional[str] = None     # ISO date e.g. "2026-10-15"


class HelperButton(BaseModel):
    label: str
    value: str


class BundleItem(BaseModel):
    category: str
    vendor_id: Optional[str] = None    # None when falling back to mock data
    service_id: Optional[str] = None
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


# ── Chatbot state (carried by the client) ────────────────────────────


class ChatbotState(BaseModel):
    event_date: Optional[str] = None
    date_range: Optional[DateRange] = None
    location: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    guest_count: Optional[int] = None
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
    """Allow ['dj', 'venue'] or ['dj, venue'] — split comma-separated entries."""
    result = []
    for item in v:
        for part in item.split(","):
            part = part.strip().lower()
            if part and part in VENDOR_CATEGORIES:
                result.append(part)
    return result


class BundleOption(BaseModel):
    label: str
    description: str
    bundle: Bundle
    state: "ChatbotState"


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
