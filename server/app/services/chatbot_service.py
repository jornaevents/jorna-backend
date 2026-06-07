"""Business logic for the chatbot bundle-builder flow.

Every public function is a pure-ish helper that takes the current step +
user input + state and returns the next step response.  Bundle generation
uses mock data for now—swap in real DB queries later.

Off-script user inputs are routed to Llama 3.3 via the llm_service module.
"""

import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.models.chatbot_schemas import (
    BudgetTier,
    Bundle,
    BundleItem,
    BundleOption,
    BundleRequest,
    ChatbotState,
    ChatStep,
    CATEGORY_LABELS,
    HelperButton,
    MultiBundleResponse,
    StepResponse,
    VENDOR_CATEGORIES,
)
from app.services.llm_service import (
    LLMResult,
    get_llm_response,
    is_off_script,
)

logger = logging.getLogger(__name__)

MAX_HISTORY = 6  # keep conversation_history compact


# ── Helpers ──────────────────────────────────────────────────────────


def _cat_buttons(exclude: list[str] | None = None) -> list[HelperButton]:
    """Return helper buttons for every vendor category, optionally excluding some."""
    excluded = set(exclude or [])
    return [
        HelperButton(label=CATEGORY_LABELS[c], value=c)
        for c in VENDOR_CATEGORIES
        if c not in excluded
    ]


def _append_history(
    state: ChatbotState,
    user_msg: Optional[str],
    bot_msg: str,
) -> None:
    """Append a user/assistant pair to conversation_history, capped at MAX_HISTORY."""
    if user_msg:
        state.conversation_history.append({"role": "user", "content": user_msg})
    state.conversation_history.append({"role": "assistant", "content": bot_msg})
    # Keep only the most recent messages
    state.conversation_history = state.conversation_history[-MAX_HISTORY:]


# ── Step prompt + buttons ────────────────────────────────────────────


def get_initial_step() -> StepResponse:
    """Return the opening Step 0 response."""
    return StepResponse(
        next_step=ChatStep.EVENT_DETAILS,
        bot_message="Let's start with the basics. Tell me about your event.",
        helper_buttons=[
            HelperButton(label="I have a date", value="has_date"),
            HelperButton(label="No date yet", value="no_date"),
            HelperButton(label="Continue", value="continue"),
        ],
        state=ChatbotState(),
    )


def _step_event_time(state: ChatbotState) -> StepResponse:
    return StepResponse(
        next_step=ChatStep.EVENT_TIME,
        bot_message="What time does your event start and end?",
        helper_buttons=[
            HelperButton(label="Morning (8am – 1pm)", value="morning"),
            HelperButton(label="Afternoon (12pm – 6pm)", value="afternoon"),
            HelperButton(label="Evening (5pm – 11pm)", value="evening"),
            HelperButton(label="Full day (8am – 11pm)", value="full_day"),
            HelperButton(label="Not sure yet", value="not_sure"),
        ],
        state=state,
    )


def _step_already_booked(state: ChatbotState) -> StepResponse:
    return StepResponse(
        next_step=ChatStep.ALREADY_BOOKED,
        bot_message="Before I build your bundle, what do you already have booked?",
        helper_buttons=_cat_buttons() + [
            HelperButton(label="Nothing yet", value="nothing_yet"),
        ],
        state=state,
    )


def _step_still_need(state: ChatbotState) -> StepResponse:
    return StepResponse(
        next_step=ChatStep.STILL_NEED,
        bot_message="What do you want included in your bundle?",
        helper_buttons=[
            HelperButton(label="Recommend everything I still need", value="recommend_all"),
        ] + _cat_buttons(exclude=state.booked_categories) + [
            HelperButton(label="Other", value="other"),
        ],
        state=state,
    )


def _step_budget(state: ChatbotState) -> StepResponse:
    return StepResponse(
        next_step=ChatStep.BUDGET,
        bot_message="What kind of budget should I use for your bundle?",
        helper_buttons=[
            HelperButton(label="Budget-friendly", value="budget-friendly"),
            HelperButton(label="Mid-range", value="mid-range"),
            HelperButton(label="Premium", value="premium"),
            HelperButton(label="Custom budget", value="custom"),
            HelperButton(label="Not sure", value="unknown"),
        ],
        state=state,
    )


def _step_custom_budget(state: ChatbotState) -> StepResponse:
    return StepResponse(
        next_step=ChatStep.CUSTOM_BUDGET,
        bot_message="What total budget should I stay within for this bundle?",
        helper_buttons=[
            HelperButton(label="Under $3,000", value="under_3000"),
            HelperButton(label="$3,000–$7,000", value="3000_7000"),
            HelperButton(label="$7,000–$12,000", value="7000_12000"),
            HelperButton(label="Custom", value="custom_amount"),
        ],
        state=state,
    )


def _step_style(state: ChatbotState) -> StepResponse:
    return StepResponse(
        next_step=ChatStep.STYLE_PREFERENCES,
        bot_message="What kind of vibe are you going for, and any must-haves?",
        helper_buttons=[
            # Style
            HelperButton(label="Elegant", value="elegant"),
            HelperButton(label="Traditional", value="traditional"),
            HelperButton(label="Modern", value="modern"),
            HelperButton(label="Luxury", value="luxury"),
            HelperButton(label="Fun / energetic", value="fun"),
            HelperButton(label="Minimal", value="minimal"),
            HelperButton(label="Not sure", value="not_sure"),
            # Preferences
            HelperButton(label="Cultural experience", value="pref_cultural"),
            HelperButton(label="Budget-friendly picks", value="pref_budget"),
            HelperButton(label="Luxury feel", value="pref_luxury"),
            HelperButton(label="Highly rated vendors", value="pref_highly_rated"),
            HelperButton(label="Local vendors", value="pref_local"),
            HelperButton(label="Fast response", value="pref_fast"),
        ],
        state=state,
    )


def _step_bundle_action(state: ChatbotState) -> StepResponse:
    return StepResponse(
        next_step=ChatStep.BUNDLE_ACTION,
        bot_message="What would you like to do with this bundle?",
        helper_buttons=[
            HelperButton(label="Keep this bundle", value="keep"),
            HelperButton(label="Customize manually", value="customize"),
            HelperButton(label="Swap a vendor", value="swap"),
            HelperButton(label="Remove a category", value="remove"),
            HelperButton(label="Add a category", value="add"),
            HelperButton(label="See cheaper bundle", value="cheaper"),
            HelperButton(label="See premium bundle", value="premium_bundle"),
            HelperButton(label="Start over", value="start_over"),
        ],
        state=state,
        bundle=state.bundle,
    )


def _step_manual_customize(state: ChatbotState) -> StepResponse:
    return StepResponse(
        next_step=ChatStep.MANUAL_CUSTOMIZE,
        bot_message="Which category do you want to customize?",
        helper_buttons=_cat_buttons() + [
            HelperButton(label="Entire bundle", value="entire_bundle"),
        ],
        state=state,
        bundle=state.bundle,
    )


def _step_swap_vendor(state: ChatbotState) -> StepResponse:
    return StepResponse(
        next_step=ChatStep.SWAP_VENDOR,
        bot_message="Which category do you want to swap?",
        helper_buttons=_cat_buttons(),
        state=state,
        bundle=state.bundle,
    )


def _step_remove_category(state: ChatbotState) -> StepResponse:
    bundle_cats = [item.category for item in (state.bundle.items if state.bundle else [])]
    return StepResponse(
        next_step=ChatStep.REMOVE_CATEGORY,
        bot_message="Which category do you want to remove from the bundle?",
        helper_buttons=[
            HelperButton(label=CATEGORY_LABELS.get(c, c.title()), value=c)
            for c in bundle_cats
        ],
        state=state,
        bundle=state.bundle,
    )


def _step_add_category(state: ChatbotState) -> StepResponse:
    bundle_cats = {item.category for item in (state.bundle.items if state.bundle else [])}
    return StepResponse(
        next_step=ChatStep.ADD_CATEGORY,
        bot_message="What would you like to add to the bundle?",
        helper_buttons=_cat_buttons(exclude=list(bundle_cats)) + [
            HelperButton(label="Other", value="other"),
        ],
        state=state,
        bundle=state.bundle,
    )


def _step_results_booking(state: ChatbotState) -> StepResponse:
    return StepResponse(
        next_step=ChatStep.RESULTS_BOOKING,
        bot_message="Ready to book? You can book the whole bundle or select specific categories.",
        helper_buttons=[
            HelperButton(label="Book this whole bundle", value="book_all"),
            HelperButton(label="Book only some categories", value="book_some"),
            HelperButton(label="Go back and edit", value="go_back"),
        ],
        state=state,
        bundle=state.bundle,
    )


def _step_partial_booking(state: ChatbotState) -> StepResponse:
    bundle_cats = [item.category for item in (state.bundle.items if state.bundle else [])]
    return StepResponse(
        next_step=ChatStep.PARTIAL_BOOKING,
        bot_message="Which categories do you want to book now?",
        helper_buttons=[
            HelperButton(label=CATEGORY_LABELS.get(c, c.title()), value=c)
            for c in bundle_cats
        ],
        state=state,
        bundle=state.bundle,
    )


# ── Multi-bundle generation ───────────────────────────────────────────


def _build_bundle_with_strategy(
    state: ChatbotState,
    strategy: str,
    db: Session,
) -> Bundle:
    """Build a bundle using a specific selection strategy.

    strategy:
      'budget'    — pick the cheapest vendor per category
      'top_rated' — pick the highest rated vendor per category
      'balanced'  — balance rating and price equally

    All three strategies also factor in LLM-scored tag relevance when
    style/preferences are provided, so cultural or style preferences
    influence which vendor is picked even within a price/rating sort.
    """
    from app.db.models import Vendor, Service, User, Tag
    from app.services.llm_service import get_relevant_tags_for_preferences
    from app.utils.location import calculate_distance_miles

    items: list[BundleItem] = []
    total_min = 0.0
    total_max = 0.0

    # Pre-compute LLM tag relevance once for all categories (same as _generate_bundle_from_db)
    llm_relevant_tags: set[str] | None = None
    if state.style or state.preferences:
        user_tag_names = [name for (name,) in db.query(Tag.name).all()]
        ig_tags: list[str] = []
        for (ig,) in db.query(Vendor.instagram_tags).filter(Vendor.instagram_tags.isnot(None)).all():
            if isinstance(ig, list):
                ig_tags.extend(t.lower() for t in ig)
        unique_tags = list(set(user_tag_names + ig_tags))
        if unique_tags:
            result = get_relevant_tags_for_preferences(
                preferences=list(state.preferences),
                style=list(state.style),
                candidate_tags=unique_tags,
            )
            llm_relevant_tags = result or None

    # Pre-compute once — same set applies to every category in this bundle
    booked_vendor_ids = _get_booked_vendor_ids(state, db)
    price_cap = _per_category_cap(state)

    for cat in state.needed_categories:
        db_category = _CATEGORY_MAP.get(cat)
        if not db_category:
            continue

        vendor_rows = (
            db.query(Vendor, User)
            .join(User, Vendor.user_id == User.user_id)
            .filter(Vendor.category == db_category)
            .all()
        )

        # Filter by travel radius if event coordinates are provided
        if state.latitude is not None and state.longitude is not None:
            vendor_rows = [
                (v, u) for v, u in vendor_rows
                if u.latitude is None or u.longitude is None or
                v.open_to_long_distance or
                calculate_distance_miles(state.latitude, state.longitude, u.latitude, u.longitude) <= v.travel_radius_miles
            ]

        # Filter out vendors already booked on the event date
        if booked_vendor_ids:
            vendor_rows = [(v, u) for v, u in vendor_rows if v.vendor_id not in booked_vendor_ids]

        if not vendor_rows:
            mock_tier = {
                "budget": BudgetTier.BUDGET_FRIENDLY,
                "top_rated": BudgetTier.PREMIUM,
                "balanced": BudgetTier.MID_RANGE,
            }.get(strategy, BudgetTier.MID_RANGE)
            item = _mock_item_for_category(cat, mock_tier)
            if item:
                items.append(item)
                total_min += item.price_min
                total_max += item.price_max
            continue

        vendor_ids = [v.vendor_id for v, _ in vendor_rows]
        all_services = db.query(Service).filter(Service.vendor_id.in_(vendor_ids)).all()
        services_by_vendor: dict[str, list] = {}
        for svc in all_services:
            services_by_vendor.setdefault(svc.vendor_id, []).append(svc)

        # Budget enforcement: keep vendors with at least one service within cap.
        if price_cap < float("inf"):
            within_budget = [
                (v, u) for v, u in vendor_rows
                if any(s.price <= price_cap for s in services_by_vendor.get(v.vendor_id, []))
                or not services_by_vendor.get(v.vendor_id)
            ]
            if within_budget:
                vendor_rows = within_budget

        def _avg_price(v) -> float:
            svcs = services_by_vendor.get(v.vendor_id, [])
            within = [s for s in svcs if s.price <= price_cap] if price_cap < float("inf") else svcs
            pool = within if within else svcs
            return sum(s.price for s in pool) / len(pool) if pool else 0.0

        def _tag_bonus(v) -> float:
            """Small bonus for vendors whose tags match user preferences."""
            if not llm_relevant_tags:
                return 0.0
            v_tags = {t.name.lower() for t in (v.tags or [])} | {t.lower() for t in (v.instagram_tags or [])}
            return len(v_tags & llm_relevant_tags) * 0.1

        if strategy == "budget":
            vendor_rows_sorted = sorted(vendor_rows, key=lambda r: (_avg_price(r[0]), -_tag_bonus(r[0])))
        elif strategy == "top_rated":
            vendor_rows_sorted = sorted(vendor_rows, key=lambda r: (-(r[0].rating or 0.0), -_tag_bonus(r[0])))
        else:  # balanced
            max_price = max((_avg_price(v) for v, _ in vendor_rows), default=1.0) or 1.0
            max_rating = max((v.rating or 0.0 for v, _ in vendor_rows), default=1.0) or 1.0
            vendor_rows_sorted = sorted(
                vendor_rows,
                key=lambda r: (r[0].rating or 0.0) / max_rating * 0.5
                              + (1 - _avg_price(r[0]) / max_price) * 0.3
                              + _tag_bonus(r[0]) * 0.2,
                reverse=True,
            )

        vendor, user = vendor_rows_sorted[0]
        services = services_by_vendor.get(vendor.vendor_id, [])

        if services:
            within_cap = [s for s in services if s.price <= price_cap]
            candidate_services = within_cap if within_cap else services
            prices = [s.price for s in candidate_services]
            p_min, p_max = min(prices), max(prices)
            best = min(candidate_services, key=lambda s: s.price)
            service_id = best.service_id
            match_reason = best.description or best.experience or vendor.bio or ""
        else:
            p_min = p_max = 0.0
            service_id = None
            match_reason = vendor.bio or ""

        items.append(BundleItem(
            category=cat,
            vendor_id=vendor.vendor_id,
            service_id=service_id,
            vendor_name=f"{user.f_name} {user.l_name}",
            pfp_url=user.pfp_url,
            price_min=round(p_min, 2),
            price_max=round(p_max, 2),
            rating=vendor.rating or 0.0,
            match_reason=(match_reason[:120] + "…") if len(match_reason) > 120 else match_reason,
        ))
        total_min += p_min
        total_max += p_max

    return Bundle(
        items=items,
        estimated_total_min=round(total_min, 2),
        estimated_total_max=round(total_max, 2),
    )


def generate_multi_bundle(req: BundleRequest, db: Session | None = None) -> MultiBundleResponse:
    """Generate 3 bundle options for users who aren't sure what they want.

    Returns Budget, Top Rated, and Balanced bundles so the user can compare
    and pick one to refine further using the /chatbot/step flow.
    """
    needed = req.needed_categories or [
        c for c in VENDOR_CATEGORIES if c not in req.booked_categories
    ]

    _PRESETS = [
        (
            "budget", BudgetTier.BUDGET_FRIENDLY, "Budget Bundle",
            "Best value — quality vendors at the lowest prices",
            ["Lowest price (primary)", "Style match (tiebreaker)"],
        ),
        (
            "top_rated", BudgetTier.PREMIUM, "Top Rated Bundle",
            "The best of the best — highest rated vendors regardless of price",
            ["Highest rating (primary)", "Style match (tiebreaker)"],
        ),
        (
            "balanced", BudgetTier.MID_RANGE, "Balanced Bundle",
            "The sweet spot — great quality at a reasonable price",
            ["Rating 50%", "Price 30%", "Style match 20%"],
        ),
    ]

    options: list[BundleOption] = []

    for strategy, tier, label, description, factors in _PRESETS:
        state = ChatbotState(
            event_date=req.event_date,
            date_range=req.date_range,
            location=req.location,
            latitude=req.latitude,
            longitude=req.longitude,
            guest_count=req.guest_count,
            booked_categories=req.booked_categories,
            needed_categories=needed,
            budget_tier=tier,
            budget_amount=req.budget_amount,
            style=req.style,
            preferences=req.preferences,
        )

        bundle = (
            _build_bundle_with_strategy(state, strategy, db)
            if db is not None
            else _generate_bundle_mock(state)
        )
        state.bundle = bundle

        options.append(BundleOption(
            label=label,
            description=description,
            factors=factors,
            bundle=bundle,
            state=state,
        ))

    return MultiBundleResponse(options=options)


# ── Single-shot bundle generation ────────────────────────────────────


def generate_bundle_from_request(req: BundleRequest, db: Session | None = None) -> StepResponse:
    """Accept all user inputs at once and return a bundle immediately.

    Any field can be omitted — sensible defaults are applied:
    - needed_categories defaults to all categories minus booked ones
    - budget_tier defaults to mid-range
    """
    state = ChatbotState(
        event_date=req.event_date,
        date_range=req.date_range,
        location=req.location,
        latitude=req.latitude,
        longitude=req.longitude,
        guest_count=req.guest_count,
        booked_categories=req.booked_categories,
        needed_categories=req.needed_categories or [
            c for c in VENDOR_CATEGORIES if c not in req.booked_categories
        ],
        budget_tier=req.budget_tier or BudgetTier.MID_RANGE,
        budget_amount=req.budget_amount,
        style=req.style,
        preferences=req.preferences,
    )

    bundle = generate_bundle(state, db=db)
    state.bundle = bundle

    def _looks_like_date(val: str | None) -> bool:
        return bool(val and val.lower() not in ("string", "null", "") and len(val) >= 4)

    date_info = ""
    if req.date_range and (_looks_like_date(req.date_range.start) or _looks_like_date(req.date_range.end)):
        start = req.date_range.start if _looks_like_date(req.date_range.start) else "?"
        end = req.date_range.end if _looks_like_date(req.date_range.end) else "?"
        date_info = f" for your event between {start} and {end}"
    elif _looks_like_date(req.event_date):
        date_info = f" for your event on {req.event_date}"

    return StepResponse(
        next_step=ChatStep.BUNDLE_ACTION,
        bot_message=f"Here's your bundle{date_info}.",
        helper_buttons=[
            HelperButton(label="Keep this bundle", value="keep"),
            HelperButton(label="Swap a vendor", value="swap"),
            HelperButton(label="Remove a category", value="remove"),
            HelperButton(label="Add a category", value="add"),
            HelperButton(label="See cheaper options", value="cheaper"),
            HelperButton(label="See premium options", value="premium_bundle"),
            HelperButton(label="Start over", value="start_over"),
        ],
        state=state,
        bundle=bundle,
    )


# ── Mock bundle generation ───────────────────────────────────────────


# Mock vendor pool keyed by category
_MOCK_VENDORS: dict[str, list[dict]] = {
    "venue": [
        {"name": "The Grand Mahal Banquet", "price_min": 3000, "price_max": 8000, "rating": 4.8, "reason": "Spacious traditional venue"},
        {"name": "Sapphire Gardens", "price_min": 5000, "price_max": 12000, "rating": 4.9, "reason": "Elegant outdoor setting"},
    ],
    "catering": [
        {"name": "Spice & Soul Catering", "price_min": 2000, "price_max": 5000, "rating": 5.0, "reason": "Award-winning South Asian cuisine"},
        {"name": "Royal Feast Kitchen", "price_min": 1500, "price_max": 3500, "rating": 4.7, "reason": "Vegetarian-friendly menu"},
    ],
    "decor": [
        {"name": "Marigold Dreams Decor", "price_min": 1500, "price_max": 4000, "rating": 4.9, "reason": "Stunning floral and mandap setups"},
        {"name": "Desi Glam Décor", "price_min": 800, "price_max": 2500, "rating": 4.6, "reason": "Modern fusion designs"},
    ],
    "photographer": [
        {"name": "Moments in Motion Photography", "price_min": 2000, "price_max": 5000, "rating": 4.9, "reason": "Cinematic storytelling"},
        {"name": "Desi Lens Studio", "price_min": 1200, "price_max": 3000, "rating": 4.7, "reason": "Traditional + candid specialist"},
    ],
    "dj": [
        {"name": "Beats & Bhangra DJ", "price_min": 800, "price_max": 2000, "rating": 4.9, "reason": "Bollywood & Bhangra specialist"},
        {"name": "DJ NaachLe", "price_min": 600, "price_max": 1500, "rating": 4.6, "reason": "High energy Punjabi sets"},
    ],
    "mehndi": [
        {"name": "Henna by Priya", "price_min": 300, "price_max": 1200, "rating": 5.0, "reason": "Bridal mehndi specialist"},
        {"name": "MehndiQueens", "price_min": 200, "price_max": 800, "rating": 4.8, "reason": "Intricate Rajasthani designs"},
    ],
    "dhol": [
        {"name": "BollyDhol Beats", "price_min": 400, "price_max": 1000, "rating": 4.8, "reason": "High-energy baraat performances"},
        {"name": "Rhythm & Dhol", "price_min": 300, "price_max": 800, "rating": 4.7, "reason": "Traditional Punjabi dhol players"},
    ],
}

# Budget tier → index into mock list (0 = premium/first, 1 = budget/second)
_TIER_INDEX = {
    BudgetTier.BUDGET_FRIENDLY: 1,
    BudgetTier.MID_RANGE: 0,
    BudgetTier.PREMIUM: 0,
    BudgetTier.CUSTOM: 0,
    BudgetTier.UNKNOWN: 0,
}


def generate_bundle(state: ChatbotState, db: Session | None = None) -> Bundle:
    """Build a bundle from real DB vendors when db is provided, otherwise use mock data."""
    if db is not None:
        return _generate_bundle_from_db(state, db)
    return _generate_bundle_mock(state)


# Chatbot category keys → DB vendor category values
_CATEGORY_MAP = {
    "venue": "venue",
    "catering": "catering",
    "decor": "decoration",
    "photographer": "photography",
    "dj": "dj",
    "mehndi": "mehndi",
    "dhol": "dhol",
}

# Max price per vendor per category for each preset tier (inf = no cap)
_PRESET_TIER_CAPS: dict[BudgetTier, float] = {
    BudgetTier.BUDGET_FRIENDLY: 1500.0,
    BudgetTier.MID_RANGE:       4000.0,
    BudgetTier.PREMIUM:         float("inf"),
    BudgetTier.UNKNOWN:         float("inf"),
    BudgetTier.CUSTOM:          float("inf"),  # overridden by budget_amount
}


def _parse_budget_amount(budget_amount: str | None) -> float | None:
    """Convert a budget_amount string to a total dollar figure.

    Handles preset keys from the CUSTOM_BUDGET step buttons, plain dollar
    strings like '$10,000', and bare numerics like '10000'.
    """
    if not budget_amount:
        return None
    _PRESETS = {
        "under_3000":   3000.0,
        "3000_7000":    7000.0,
        "7000_12000":  12000.0,
    }
    if budget_amount in _PRESETS:
        return _PRESETS[budget_amount]
    cleaned = re.sub(r"[^\d.]", "", budget_amount)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _per_category_cap(state: ChatbotState) -> float:
    """Return the max price per vendor category given the user's budget.

    For custom budgets the total is split evenly across all needed categories.
    Returns inf when no cap applies.
    """
    tier = state.budget_tier or BudgetTier.UNKNOWN
    if tier == BudgetTier.CUSTOM:
        total = _parse_budget_amount(state.budget_amount)
        if total:
            n = len(state.needed_categories) or 1
            return total / n
        return float("inf")
    return _PRESET_TIER_CAPS.get(tier, float("inf"))

# User style/preference selections → tag keywords to match against vendor tags
_STYLE_TAG_KEYWORDS: dict[str, list[str]] = {
    "elegant":     ["elegant", "luxury", "premium", "sophisticated", "upscale"],
    "traditional": ["traditional", "classical", "cultural", "heritage", "authentic"],
    "modern":      ["modern", "contemporary", "fusion", "trendy", "stylish"],
    "luxury":      ["luxury", "premium", "high-end", "exclusive", "vip"],
    "fun":         ["fun", "energetic", "party", "vibrant", "upbeat", "lively"],
    "minimal":     ["minimal", "simple", "clean", "minimalist", "understated"],
    "pref_cultural":     ["cultural", "bhangra", "bollywood", "desi", "south asian", "traditional", "punjabi"],
    "pref_luxury":       ["luxury", "premium", "high-end", "exclusive"],
    "pref_highly_rated": [],   # handled by rating weight boost
    "pref_budget":       [],   # handled by price filtering
    "pref_local":        [],   # handled by location (not yet implemented)
    "pref_fast":         [],   # no response-time data yet
}


def _get_booked_vendor_ids(state: ChatbotState, db: Session) -> set[str]:
    """Return vendor IDs with a pending or confirmed booking overlapping the event dates.

    Uses interval overlap logic: existing booking overlaps the request when
        existing.date_iso <= requested_end AND existing.date_end_or_start >= requested_start

    Returns an empty set when no date information is available.
    """
    from app.db.models import Booking

    # Determine the requested start and end dates
    if state.date_range and (state.date_range.start or state.date_range.end):
        req_start = state.date_range.start or state.date_range.end
        req_end = state.date_range.end or state.date_range.start
    elif state.event_date and state.event_date not in ("TBD", ""):
        req_start = req_end = state.event_date
    else:
        return set()

    # Overlap condition:
    #   booking starts on or before our end AND booking ends on or after our start
    # date_end is null for single-day bookings — treat null date_end as same as date_iso
    from sqlalchemy import case
    booking_end = case(
        (Booking.date_end.isnot(None), Booking.date_end),
        else_=Booking.date_iso,
    )

    rows = (
        db.query(Booking.vendor_id)
        .filter(
            Booking.status.in_(["pending", "confirmed"]),
            Booking.date_iso <= req_end,
            booking_end >= req_start,
        )
        .all()
    )
    return {row.vendor_id for row in rows}


def _score_vendor(
    vendor,
    services,
    style: list[str],
    preferences: list[str],
    tier: BudgetTier,
    llm_relevant_tags: set[str] | None = None,
) -> float:
    """Score a vendor for bundle selection.

    Higher score = better match. Factors:
    - Rating (primary signal)
    - Tag overlap with user's style and preferences
    - Experience (num_events) as a tiebreaker
    - Price alignment with budget tier
    """
    # Base: rating out of 5, doubled so it dominates
    score = (vendor.rating or 0.0) * 2.0

    # Combine user-inputted tags and Instagram-scraped tags
    user_tag_names = {t.name.lower() for t in (vendor.tags or [])}
    ig_tag_names = {t.lower() for t in (vendor.instagram_tags or [])}
    vendor_tag_names = user_tag_names | ig_tag_names

    if llm_relevant_tags is not None:
        # LLM path: check how many LLM-identified relevant tags this vendor has
        matches = len(vendor_tag_names & llm_relevant_tags)
        score += matches * 0.5
    else:
        # Fallback: hardcoded keyword matching via _STYLE_TAG_KEYWORDS
        wanted_keywords: set[str] = set()
        for sel in list(style) + list(preferences):
            wanted_keywords.update(_STYLE_TAG_KEYWORDS.get(sel, []))
        if wanted_keywords:
            matches = sum(1 for kw in wanted_keywords if any(kw in tag for tag in vendor_tag_names))
            score += matches * 0.5

    # Boost for highly-rated preference
    if "pref_highly_rated" in preferences:
        score += (vendor.rating or 0.0) * 0.5

    # Experience tiebreaker (capped at 1.0)
    score += min((vendor.num_events or 0) * 0.05, 1.0)

    # Price alignment: penalise mismatches between tier and vendor pricing
    if services:
        prices = [s.price for s in services]
        avg_price = sum(prices) / len(prices)
        if tier == BudgetTier.BUDGET_FRIENDLY and avg_price > 3000:
            score -= 1.0
        elif tier == BudgetTier.PREMIUM and avg_price < 1000:
            score -= 1.0

    return score


def _generate_bundle_from_db(state: ChatbotState, db: Session) -> Bundle:
    """Query real vendors from the DB, one per needed category."""
    from app.db.models import Vendor, Service, User
    from app.services.llm_service import get_relevant_tags_for_preferences
    from app.utils.location import calculate_distance_miles

    tier = state.budget_tier or BudgetTier.MID_RANGE
    items: list[BundleItem] = []
    total_min = 0.0
    total_max = 0.0

    # Pre-compute LLM tag relevance once for this entire bundle request.
    # Queries only tag name strings (not full Vendor objects) for efficiency.
    llm_relevant_tags: set[str] | None = None
    if state.style or state.preferences:
        from app.db.models import Tag
        user_tag_names = [name for (name,) in db.query(Tag.name).all()]
        ig_tags: list[str] = []
        for (ig,) in db.query(Vendor.instagram_tags).filter(Vendor.instagram_tags.isnot(None)).all():
            if isinstance(ig, list):
                ig_tags.extend(t.lower() for t in ig)
        unique_tags = list(set(user_tag_names + ig_tags))
        if unique_tags:
            llm_relevant_tags = get_relevant_tags_for_preferences(
                preferences=list(state.preferences),
                style=list(state.style),
                candidate_tags=unique_tags,
            ) or None  # None triggers keyword fallback in _score_vendor

    # Pre-compute once — same set applies to every category in this bundle
    booked_vendor_ids = _get_booked_vendor_ids(state, db)
    price_cap = _per_category_cap(state)

    for cat in state.needed_categories:
        db_category = _CATEGORY_MAP.get(cat)
        if not db_category:
            continue

        # Find vendors for this category, joined with their user for name/pfp
        vendor_rows = (
            db.query(Vendor, User)
            .join(User, Vendor.user_id == User.user_id)
            .filter(Vendor.category == db_category)
            .all()
        )

        # Filter by travel radius if event coordinates are provided
        if state.latitude is not None and state.longitude is not None:
            vendor_rows = [
                (v, u) for v, u in vendor_rows
                if u.latitude is None or u.longitude is None or
                v.open_to_long_distance or
                calculate_distance_miles(state.latitude, state.longitude, u.latitude, u.longitude) <= v.travel_radius_miles
            ]

        # Filter out vendors already booked on the event date
        if booked_vendor_ids:
            vendor_rows = [(v, u) for v, u in vendor_rows if v.vendor_id not in booked_vendor_ids]

        if not vendor_rows:
            # No real vendors — fall back to mock for this category
            mock_item = _mock_item_for_category(cat, tier)
            if mock_item:
                items.append(mock_item)
                total_min += mock_item.price_min
                total_max += mock_item.price_max
            continue

        # Fetch services for all candidate vendors in one query
        vendor_ids = [v.vendor_id for v, _ in vendor_rows]
        all_services = db.query(Service).filter(Service.vendor_id.in_(vendor_ids)).all()
        services_by_vendor: dict[str, list] = {}
        for svc in all_services:
            services_by_vendor.setdefault(svc.vendor_id, []).append(svc)

        # Budget enforcement: keep vendors that have at least one service within the cap.
        # If none qualify, fall back to all vendors (show cheapest available over budget).
        if price_cap < float("inf"):
            within_budget = [
                (v, u) for v, u in vendor_rows
                if any(s.price <= price_cap for s in services_by_vendor.get(v.vendor_id, []))
                or not services_by_vendor.get(v.vendor_id)
            ]
            if within_budget:
                vendor_rows = within_budget

        # Score every vendor and pick the best match
        scored = [
            (
                _score_vendor(v, services_by_vendor.get(v.vendor_id, []), state.style, state.preferences, tier, llm_relevant_tags),
                v,
                u,
                services_by_vendor.get(v.vendor_id, []),
            )
            for v, u in vendor_rows
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        _, vendor, user, services = scored[0]

        if services:
            # Prefer services within the budget cap; fall back to all if none qualify.
            within_cap = [s for s in services if s.price <= price_cap]
            candidate_services = within_cap if within_cap else services
            prices = [s.price for s in candidate_services]
            p_min = min(prices)
            p_max = max(prices)
            best_service = min(candidate_services, key=lambda s: s.price)
            service_id = best_service.service_id
            match_reason = best_service.description or best_service.experience or vendor.bio or ""
        else:
            p_min = 0.0
            p_max = 0.0
            service_id = None
            match_reason = vendor.bio or ""

        items.append(BundleItem(
            category=cat,
            vendor_id=vendor.vendor_id,
            service_id=service_id,
            vendor_name=f"{user.f_name} {user.l_name}",
            pfp_url=user.pfp_url,
            price_min=round(p_min, 2),
            price_max=round(p_max, 2),
            rating=vendor.rating or 0.0,
            match_reason=(match_reason[:120] + "…") if len(match_reason) > 120 else match_reason,
        ))
        total_min += p_min
        total_max += p_max

    return Bundle(
        items=items,
        estimated_total_min=round(total_min, 2),
        estimated_total_max=round(total_max, 2),
    )


def _mock_item_for_category(cat: str, tier: BudgetTier) -> BundleItem | None:
    """Return a mock BundleItem for a category when no real vendors exist."""
    pool = _MOCK_VENDORS.get(cat)
    if not pool:
        return None
    vendor_idx = _TIER_INDEX.get(tier, 0)
    vendor = pool[vendor_idx] if vendor_idx < len(pool) else pool[0]
    price_mult = 0.8 if tier == BudgetTier.BUDGET_FRIENDLY else (1.3 if tier == BudgetTier.PREMIUM else 1.0)
    return BundleItem(
        category=cat,
        vendor_name=vendor["name"],
        price_min=round(vendor["price_min"] * price_mult, 2),
        price_max=round(vendor["price_max"] * price_mult, 2),
        rating=vendor["rating"],
        match_reason=vendor["reason"],
    )


def _generate_bundle_mock(state: ChatbotState) -> Bundle:
    """Pure mock bundle — used when no DB session is available."""
    tier = state.budget_tier or BudgetTier.UNKNOWN
    items: list[BundleItem] = []
    total_min = 0.0
    total_max = 0.0
    for cat in state.needed_categories:
        item = _mock_item_for_category(cat, tier)
        if item:
            items.append(item)
            total_min += item.price_min
            total_max += item.price_max
    return Bundle(
        items=items,
        estimated_total_min=round(total_min, 2),
        estimated_total_max=round(total_max, 2),
    )


# ── LLM intent → step mapping ────────────────────────────────────────


def _apply_llm_intent(
    llm_result: LLMResult,
    state: ChatbotState,
    current_step: ChatStep,
    db: Session | None = None,
) -> StepResponse:
    """Map an LLM-extracted intent to a step response, or stay on current step."""
    intent = llm_result.extracted_intent
    values = llm_result.extracted_values or {}
    suggested = llm_result.suggested_step

    # ── Set event details and advance ────────────────────────────────
    if intent == "set_event_details":
        if values.get("location"):
            state.location = values["location"]
        if values.get("guest_count"):
            state.guest_count = int(values["guest_count"])
        if values.get("event_date"):
            state.event_date = values["event_date"]
        resp = _step_already_booked(state)
        resp.bot_message = f"{llm_result.bot_message}\n\n{resp.bot_message}"
        resp.llm_response = True
        return resp

    # ── Set booked categories ────────────────────────────────────────
    if intent == "set_booked":
        cats = [c for c in values.get("categories", []) if c in VENDOR_CATEGORIES]
        if cats:
            state.booked_categories = cats
            resp = _step_still_need(state)
        else:
            state.booked_categories = []
            state.needed_categories = list(VENDOR_CATEGORIES)
            resp = _step_budget(state)
        resp.bot_message = f"{llm_result.bot_message}\n\n{resp.bot_message}"
        resp.llm_response = True
        return resp

    # ── Set needed categories ────────────────────────────────────────
    if intent == "set_needed":
        cats = [c for c in values.get("categories", []) if c in VENDOR_CATEGORIES]
        state.needed_categories = [c for c in cats if c not in state.booked_categories]
        resp = _step_budget(state)
        resp.bot_message = f"{llm_result.bot_message}\n\n{resp.bot_message}"
        resp.llm_response = True
        return resp

    # ── Set budget ───────────────────────────────────────────────────
    if intent == "set_budget":
        tier_map = {
            "budget-friendly": BudgetTier.BUDGET_FRIENDLY,
            "mid-range": BudgetTier.MID_RANGE,
            "premium": BudgetTier.PREMIUM,
        }
        tier_str = values.get("budget_tier", "")
        state.budget_tier = tier_map.get(tier_str, BudgetTier.CUSTOM)
        if values.get("budget_amount"):
            state.budget_amount = str(values["budget_amount"])
            state.budget_tier = BudgetTier.CUSTOM
        resp = _step_style(state)
        resp.bot_message = f"{llm_result.bot_message}\n\n{resp.bot_message}"
        resp.llm_response = True
        return resp

    # ── Set style ────────────────────────────────────────────────────
    if intent == "set_style":
        if values.get("style"):
            state.style = values["style"] if isinstance(values["style"], list) else [values["style"]]
        if values.get("preferences"):
            state.preferences = values["preferences"] if isinstance(values["preferences"], list) else [values["preferences"]]
        # Generate bundle if we have enough info
        if state.needed_categories and state.budget_tier:
            bundle = generate_bundle(state, db=db)
            state.bundle = bundle
            resp = _step_bundle_action(state)
            resp.bot_message = f"{llm_result.bot_message}\n\nHere's the bundle I built for you."
        else:
            resp = _step_style(state)
            resp.bot_message = llm_result.bot_message
        resp.llm_response = True
        return resp

    # ── Modify bundle ────────────────────────────────────────────────
    if intent == "modify_bundle":
        action = values.get("action", "")
        if action == "swap":
            resp = _step_swap_vendor(state)
        elif action == "remove":
            resp = _step_remove_category(state)
        elif action == "add":
            resp = _step_add_category(state)
        else:
            resp = _step_bundle_action(state)
        resp.bot_message = f"{llm_result.bot_message}\n\n{resp.bot_message}"
        resp.llm_response = True
        return resp

    # ── Book ─────────────────────────────────────────────────────────
    if intent == "book":
        resp = _step_results_booking(state)
        resp.bot_message = f"{llm_result.bot_message}\n\n{resp.bot_message}"
        resp.llm_response = True
        return resp

    # ── Explicit step jump from LLM ──────────────────────────────────
    if suggested:
        step_map = {s.value: s for s in ChatStep}
        target_step = step_map.get(suggested)
        if target_step:
            # Build the appropriate step response
            step_builders = {
                ChatStep.EVENT_DETAILS: lambda: get_initial_step(),
                ChatStep.EVENT_TIME: lambda: _step_event_time(state),
                ChatStep.ALREADY_BOOKED: lambda: _step_already_booked(state),
                ChatStep.STILL_NEED: lambda: _step_still_need(state),
                ChatStep.BUDGET: lambda: _step_budget(state),
                ChatStep.CUSTOM_BUDGET: lambda: _step_custom_budget(state),
                ChatStep.STYLE_PREFERENCES: lambda: _step_style(state),
                ChatStep.BUNDLE_ACTION: lambda: _step_bundle_action(state),
                ChatStep.MANUAL_CUSTOMIZE: lambda: _step_manual_customize(state),
                ChatStep.SWAP_VENDOR: lambda: _step_swap_vendor(state),
                ChatStep.REMOVE_CATEGORY: lambda: _step_remove_category(state),
                ChatStep.ADD_CATEGORY: lambda: _step_add_category(state),
                ChatStep.RESULTS_BOOKING: lambda: _step_results_booking(state),
                ChatStep.PARTIAL_BOOKING: lambda: _step_partial_booking(state),
            }
            builder = step_builders.get(target_step)
            if builder:
                resp = builder()
                resp.bot_message = f"{llm_result.bot_message}\n\n{resp.bot_message}"
                resp.llm_response = True
                return resp

    # ── Default: answer the question, stay on current step ───────────
    # Re-build the current step's response but with the LLM's message
    step_builders = {
        ChatStep.EVENT_DETAILS: lambda: get_initial_step(),
        ChatStep.ALREADY_BOOKED: lambda: _step_already_booked(state),
        ChatStep.STILL_NEED: lambda: _step_still_need(state),
        ChatStep.BUDGET: lambda: _step_budget(state),
        ChatStep.CUSTOM_BUDGET: lambda: _step_custom_budget(state),
        ChatStep.STYLE_PREFERENCES: lambda: _step_style(state),
        ChatStep.BUNDLE_ACTION: lambda: _step_bundle_action(state),
        ChatStep.MANUAL_CUSTOMIZE: lambda: _step_manual_customize(state),
        ChatStep.SWAP_VENDOR: lambda: _step_swap_vendor(state),
        ChatStep.REMOVE_CATEGORY: lambda: _step_remove_category(state),
        ChatStep.ADD_CATEGORY: lambda: _step_add_category(state),
        ChatStep.RESULTS_BOOKING: lambda: _step_results_booking(state),
        ChatStep.PARTIAL_BOOKING: lambda: _step_partial_booking(state),
    }
    builder = step_builders.get(current_step)
    if builder:
        resp = builder()
        resp.bot_message = llm_result.bot_message
        resp.llm_response = True
        return resp

    # Ultimate fallback
    resp = get_initial_step()
    resp.bot_message = llm_result.bot_message
    resp.llm_response = True
    return resp


# ── Main step processor ──────────────────────────────────────────────


def _create_bundle_from_chatbot(
    state: ChatbotState,
    user_id: str,
    categories: list[str] | None,
    db: Session,
) -> tuple[str, list[str]]:
    """Create a Bundle and Bookings in the DB from the chatbot state.

    categories: list of category keys to book, or None to book all items.
    Returns (bundle_id, [booking_id, ...]).
    """
    from datetime import datetime, timezone
    from app.db.models import Bundle, Booking

    items_to_book = [
        item for item in (state.bundle.items if state.bundle else [])
        if item.vendor_id and item.service_id
        and (categories is None or item.category in categories)
    ]

    event_name = state.event_date or state.location or "My Event"
    location = state.location or "TBD"

    if state.date_range and state.date_range.start:
        date_iso = state.date_range.start
        date_end = state.date_range.end or None
    else:
        date_iso = state.event_date or "TBD"
        date_end = None

    now = datetime.now(timezone.utc)
    bundle = Bundle(
        user_id=user_id,
        name=f"{event_name} Bundle",
        event_name=event_name,
        status="draft",
        created_at=now,
        updated_at=now,
    )
    db.add(bundle)
    db.flush()

    booking_ids: list[str] = []
    for item in items_to_book:
        booking = Booking(
            user_id=user_id,
            vendor_id=item.vendor_id,
            service_id=item.service_id,
            date_iso=date_iso,
            date_end=date_end,
            time_start=state.time_start or "TBD",
            time_end=state.time_end or "TBD",
            location=location,
            status="pending",
            bundle_id=bundle.bundle_id,
        )
        db.add(booking)
        db.flush()
        booking_ids.append(booking.booking_id)

    db.commit()
    return bundle.bundle_id, booking_ids


async def process_step(
    current_step: ChatStep,
    user_input: Optional[str],
    selected_values: list[str],
    state: ChatbotState,
    db: Session | None = None,
    user_id: str | None = None,
) -> StepResponse:
    """Process user input for *current_step* and return the next step response.

    If the input is off-script (free text that doesn't match expected values),
    it's routed to Llama 3.3 for a natural response + intent extraction.
    """

    # ── Off-script detection ─────────────────────────────────────────
    if is_off_script(current_step, user_input, selected_values):
        logger.info(
            "Off-script input detected at step %s: %r",
            current_step.value,
            user_input,
        )
        llm_result = await get_llm_response(
            current_step=current_step,
            user_input=user_input or "",
            conversation_history=state.conversation_history,
        )
        resp = _apply_llm_intent(llm_result, state, current_step, db=db)
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── On-script: deterministic step processing ─────────────────────

    # Convenience: first selected value or free text
    selection = selected_values[0] if selected_values else (user_input or "").strip()
    selections = selected_values if selected_values else ([s.strip() for s in (user_input or "").split(",") if s.strip()])

    # ── STEP 0: EVENT DETAILS ────────────────────────────────────────
    if current_step == ChatStep.EVENT_DETAILS:
        # Save whatever the user typed as event info
        if user_input:
            text = user_input.lower()
            # Rough extraction — real NLP later
            state.location = user_input  # store raw for now
            # Try to pull guest count (require context word to avoid matching years)
            guest_match = re.search(r"(?:around|about|~)?\s*(\d{2,4})\s+(?:guests?|people|attendees)", text)
            if guest_match:
                state.guest_count = int(guest_match.group(1))
            # Try to pull date
            date_match = re.search(
                r"(january|february|march|april|may|june|july|august|september|october|november|december)"
                r"\s*\d{0,4}",
                text,
            )
            if date_match:
                state.event_date = date_match.group(0).strip()
        if selection == "no_date":
            state.event_date = "TBD"

        resp = _step_event_time(state)
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── STEP 0b: EVENT TIME ──────────────────────────────────────────
    if current_step == ChatStep.EVENT_TIME:
        _TIME_PRESETS = {
            "morning":   ("8:00 AM",  "1:00 PM"),
            "afternoon": ("12:00 PM", "6:00 PM"),
            "evening":   ("5:00 PM",  "11:00 PM"),
            "full_day":  ("8:00 AM",  "11:00 PM"),
            "not_sure":  ("TBD",      "TBD"),
        }
        if selection in _TIME_PRESETS:
            state.time_start, state.time_end = _TIME_PRESETS[selection]
        elif user_input:
            # Accept free text like "3pm to 9pm" — store as-is
            state.time_start = user_input
            state.time_end = "TBD"

        resp = _step_already_booked(state)
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── STEP 1: ALREADY BOOKED ───────────────────────────────────────
    if current_step == ChatStep.ALREADY_BOOKED:
        if "nothing_yet" in selections or selection == "nothing_yet":
            state.booked_categories = []
            state.needed_categories = list(VENDOR_CATEGORIES)
            resp = _step_budget(state)
        else:
            state.booked_categories = [
                v for v in selections if v in VENDOR_CATEGORIES
            ]
            resp = _step_still_need(state)
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── STEP 2: STILL NEED ──────────────────────────────────────────
    if current_step == ChatStep.STILL_NEED:
        if "recommend_all" in selections or selection == "recommend_all":
            state.needed_categories = [
                c for c in VENDOR_CATEGORIES if c not in state.booked_categories
            ]
        else:
            chosen = [v for v in selections if v in VENDOR_CATEGORIES]
            # Remove duplicates already booked
            state.needed_categories = [
                c for c in chosen if c not in state.booked_categories
            ]
            # Handle "other"
            if "other" in selections:
                state.needed_categories.append("other")
        resp = _step_budget(state)
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── STEP 3: BUDGET ───────────────────────────────────────────────
    if current_step == ChatStep.BUDGET:
        tier_map = {
            "budget-friendly": BudgetTier.BUDGET_FRIENDLY,
            "mid-range": BudgetTier.MID_RANGE,
            "premium": BudgetTier.PREMIUM,
            "custom": BudgetTier.CUSTOM,
            "unknown": BudgetTier.UNKNOWN,
        }
        tier = tier_map.get(selection, BudgetTier.UNKNOWN)
        state.budget_tier = tier

        if tier == BudgetTier.CUSTOM:
            resp = _step_custom_budget(state)
        else:
            resp = _step_style(state)
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── STEP 3b: CUSTOM BUDGET ───────────────────────────────────────
    if current_step == ChatStep.CUSTOM_BUDGET:
        state.budget_amount = selection or user_input
        resp = _step_style(state)
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── STEP 4: STYLE & PREFERENCES ─────────────────────────────────
    if current_step == ChatStep.STYLE_PREFERENCES:
        style_vals = {"elegant", "traditional", "modern", "luxury", "fun", "minimal", "not_sure"}
        pref_vals = {"pref_cultural", "pref_budget", "pref_luxury", "pref_highly_rated", "pref_local", "pref_fast"}

        state.style = [v for v in selections if v in style_vals] or (
            [user_input] if user_input else ["not_sure"]
        )
        state.preferences = [v for v in selections if v in pref_vals]

        # Generate bundle
        bundle = generate_bundle(state, db=db)
        state.bundle = bundle

        budget_note = ""
        if state.budget_tier == BudgetTier.CUSTOM:
            total_budget = _parse_budget_amount(state.budget_amount)
            if total_budget and bundle.estimated_total_max > total_budget:
                budget_note = (
                    f" The best available vendors for your categories come in at "
                    f"${bundle.estimated_total_min:,.0f}–${bundle.estimated_total_max:,.0f}, "
                    f"which may exceed your ${total_budget:,.0f} budget — "
                    "you can remove categories or swap vendors to bring it down."
                )

        resp = StepResponse(
            next_step=ChatStep.BUNDLE_ACTION,
            bot_message=f"Here's the bundle I built for you.{budget_note}",
            helper_buttons=[
                HelperButton(label="Keep this bundle", value="keep"),
                HelperButton(label="Customize manually", value="customize"),
                HelperButton(label="Swap a vendor", value="swap"),
                HelperButton(label="Remove a category", value="remove"),
                HelperButton(label="Add a category", value="add"),
                HelperButton(label="See cheaper bundle", value="cheaper"),
                HelperButton(label="See premium bundle", value="premium_bundle"),
                HelperButton(label="Start over", value="start_over"),
            ],
            state=state,
            bundle=bundle,
        )
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── STEP 6: BUNDLE ACTION ────────────────────────────────────────
    if current_step == ChatStep.BUNDLE_ACTION:
        if selection == "keep":
            resp = _step_results_booking(state)
        elif selection == "customize":
            resp = _step_manual_customize(state)
        elif selection == "swap":
            resp = _step_swap_vendor(state)
        elif selection == "remove":
            resp = _step_remove_category(state)
        elif selection == "add":
            resp = _step_add_category(state)
        elif selection == "cheaper":
            state.budget_tier = BudgetTier.BUDGET_FRIENDLY
            state.bundle = generate_bundle(state, db=db)
            resp = _step_bundle_action(state)
        elif selection == "premium_bundle":
            state.budget_tier = BudgetTier.PREMIUM
            state.bundle = generate_bundle(state, db=db)
            resp = _step_bundle_action(state)
        elif selection == "start_over":
            resp = get_initial_step()
        else:
            resp = _step_bundle_action(state)
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── STEP 6A: MANUAL CUSTOMIZE ────────────────────────────────────
    if current_step == ChatStep.MANUAL_CUSTOMIZE:
        # For now: regenerate the selected category with alternate vendor
        if state.bundle:
            for item in state.bundle.items:
                if item.category in selections:
                    pool = _MOCK_VENDORS.get(item.category, [])
                    # Pick alternate vendor (the other one)
                    alt = [v for v in pool if v["name"] != item.vendor_name]
                    if alt:
                        item.vendor_name = alt[0]["name"]
                        item.price_min = alt[0]["price_min"]
                        item.price_max = alt[0]["price_max"]
                        item.rating = alt[0]["rating"]
                        item.match_reason = alt[0]["reason"]
            # Recalculate totals
            state.bundle.estimated_total_min = sum(i.price_min for i in state.bundle.items)
            state.bundle.estimated_total_max = sum(i.price_max for i in state.bundle.items)
        resp = _step_bundle_action(state)
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── STEP 6B: SWAP VENDOR ─────────────────────────────────────────
    if current_step == ChatStep.SWAP_VENDOR:
        # Same logic as manual customize for a single category
        if state.bundle:
            cat_to_swap = selection
            for item in state.bundle.items:
                if item.category == cat_to_swap:
                    pool = _MOCK_VENDORS.get(item.category, [])
                    alt = [v for v in pool if v["name"] != item.vendor_name]
                    if alt:
                        item.vendor_name = alt[0]["name"]
                        item.price_min = alt[0]["price_min"]
                        item.price_max = alt[0]["price_max"]
                        item.rating = alt[0]["rating"]
                        item.match_reason = alt[0]["reason"]
            state.bundle.estimated_total_min = sum(i.price_min for i in state.bundle.items)
            state.bundle.estimated_total_max = sum(i.price_max for i in state.bundle.items)
        resp = _step_bundle_action(state)
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── STEP 6C: REMOVE CATEGORY ─────────────────────────────────────
    if current_step == ChatStep.REMOVE_CATEGORY:
        if state.bundle:
            state.bundle.items = [i for i in state.bundle.items if i.category not in selections]
            state.needed_categories = [c for c in state.needed_categories if c not in selections]
            state.bundle.estimated_total_min = sum(i.price_min for i in state.bundle.items)
            state.bundle.estimated_total_max = sum(i.price_max for i in state.bundle.items)

        resp = _step_bundle_action(state)
        resp.bot_message = "Done — I removed that category from your bundle."
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── STEP 6D: ADD CATEGORY ────────────────────────────────────────
    if current_step == ChatStep.ADD_CATEGORY:
        new_cats = [v for v in selections if v in VENDOR_CATEGORIES and v not in state.needed_categories]
        state.needed_categories.extend(new_cats)

        # Generate items for the new categories
        for cat in new_cats:
            pool = _MOCK_VENDORS.get(cat)
            if pool:
                vendor = pool[0]
                if state.bundle is None:
                    state.bundle = Bundle()
                state.bundle.items.append(BundleItem(
                    category=cat,
                    vendor_name=vendor["name"],
                    price_min=vendor["price_min"],
                    price_max=vendor["price_max"],
                    rating=vendor["rating"],
                    match_reason=vendor["reason"],
                ))

        if state.bundle:
            state.bundle.estimated_total_min = sum(i.price_min for i in state.bundle.items)
            state.bundle.estimated_total_max = sum(i.price_max for i in state.bundle.items)

        resp = _step_bundle_action(state)
        resp.bot_message = "Added — here's your updated bundle."
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── STEP 7: RESULTS & BOOKING ────────────────────────────────────
    if current_step == ChatStep.RESULTS_BOOKING:
        if selection == "book_some":
            resp = _step_partial_booking(state)
        elif selection == "go_back":
            resp = _step_bundle_action(state)
        elif selection == "book_all":
            if db and user_id and state.bundle:
                bundle_id, booking_ids = _create_bundle_from_chatbot(state, user_id, None, db)
                resp = StepResponse(
                    next_step=ChatStep.RESULTS_BOOKING,
                    bot_message=(
                        f"Your bundle has been created with {len(booking_ids)} booking(s) submitted. "
                        "Each vendor will review and confirm your request."
                    ),
                    helper_buttons=[],
                    state=state,
                    bundle=state.bundle,
                    bundle_id=bundle_id,
                    booking_ids=booking_ids,
                    is_complete=True,
                )
            else:
                resp = _step_results_booking(state)
        else:
            resp = _step_results_booking(state)
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── STEP 7b: PARTIAL BOOKING ─────────────────────────────────────
    if current_step == ChatStep.PARTIAL_BOOKING:
        chosen_cats = [v for v in selections if v in VENDOR_CATEGORIES]
        if chosen_cats and db and user_id and state.bundle:
            bundle_id, booking_ids = _create_bundle_from_chatbot(state, user_id, chosen_cats, db)
            resp = StepResponse(
                next_step=ChatStep.RESULTS_BOOKING,
                bot_message=(
                    f"Booked {len(booking_ids)} vendor(s) from your bundle. "
                    "Each vendor will review and confirm your request."
                ),
                helper_buttons=[],
                state=state,
                bundle=state.bundle,
                bundle_id=bundle_id,
                booking_ids=booking_ids,
                is_complete=True,
            )
        else:
            resp = _step_results_booking(state)
        _append_history(state, user_input, resp.bot_message)
        return resp

    # Fallback
    logger.warning("Unhandled step: %s", current_step)
    return get_initial_step()
