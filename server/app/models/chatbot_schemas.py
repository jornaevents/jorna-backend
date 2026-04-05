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
    GENERATE_BUNDLE = "generate_bundle"
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


class HelperButton(BaseModel):
    label: str
    value: str


class BundleItem(BaseModel):
    category: str
    vendor_name: str
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
    location: Optional[str] = None
    guest_count: Optional[int] = None
    booked_categories: list[str] = Field(default_factory=list)
    needed_categories: list[str] = Field(default_factory=list)
    budget_tier: Optional[BudgetTier] = None
    budget_amount: Optional[str] = None
    style: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    bundle: Optional[Bundle] = None


# ── Request / Response ───────────────────────────────────────────────


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
