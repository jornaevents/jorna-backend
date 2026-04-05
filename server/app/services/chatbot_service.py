"""Business logic for the chatbot bundle-builder flow.

Every public function is a pure-ish helper that takes the current step +
user input + state and returns the next step response.  Bundle generation
uses mock data for now—swap in real DB queries later.
"""

import logging
from typing import Optional

from app.models.chatbot_schemas import (
    BudgetTier,
    Bundle,
    BundleItem,
    ChatbotState,
    ChatStep,
    CATEGORY_LABELS,
    HelperButton,
    StepResponse,
    VENDOR_CATEGORIES,
)

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────


def _cat_buttons(exclude: list[str] | None = None) -> list[HelperButton]:
    """Return helper buttons for every vendor category, optionally excluding some."""
    excluded = set(exclude or [])
    return [
        HelperButton(label=CATEGORY_LABELS[c], value=c)
        for c in VENDOR_CATEGORIES
        if c not in excluded
    ]


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
        bot_message="What would you like to do next?",
        helper_buttons=[
            HelperButton(label="Book this whole bundle", value="book_all"),
            HelperButton(label="Book only some categories", value="book_some"),
            HelperButton(label="Contact vendors first", value="contact"),
            HelperButton(label="Save bundle for later", value="save"),
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


def generate_bundle(state: ChatbotState) -> Bundle:
    """Build a mock bundle based on needed_categories and budget_tier."""
    tier = state.budget_tier or BudgetTier.UNKNOWN
    vendor_idx = _TIER_INDEX.get(tier, 0)

    items: list[BundleItem] = []
    total_min = 0.0
    total_max = 0.0

    for cat in state.needed_categories:
        pool = _MOCK_VENDORS.get(cat)
        if not pool:
            # Unknown category → skip
            continue
        vendor = pool[vendor_idx] if vendor_idx < len(pool) else pool[0]

        # Adjust prices by tier
        price_mult = 1.0
        if tier == BudgetTier.BUDGET_FRIENDLY:
            price_mult = 0.8
        elif tier == BudgetTier.PREMIUM:
            price_mult = 1.3

        p_min = round(vendor["price_min"] * price_mult, 2)
        p_max = round(vendor["price_max"] * price_mult, 2)

        items.append(BundleItem(
            category=cat,
            vendor_name=vendor["name"],
            price_min=p_min,
            price_max=p_max,
            rating=vendor["rating"],
            match_reason=vendor["reason"],
        ))
        total_min += p_min
        total_max += p_max

    return Bundle(
        items=items,
        estimated_total_min=round(total_min, 2),
        estimated_total_max=round(total_max, 2),
    )


# ── Main step processor ──────────────────────────────────────────────


def process_step(
    current_step: ChatStep,
    user_input: Optional[str],
    selected_values: list[str],
    state: ChatbotState,
) -> StepResponse:
    """Process user input for *current_step* and return the next step response."""

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
            import re
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

        return _step_already_booked(state)

    # ── STEP 1: ALREADY BOOKED ───────────────────────────────────────
    if current_step == ChatStep.ALREADY_BOOKED:
        if "nothing_yet" in selections or selection == "nothing_yet":
            state.booked_categories = []
            state.needed_categories = list(VENDOR_CATEGORIES)
            return _step_budget(state)
        else:
            state.booked_categories = [
                v for v in selections if v in VENDOR_CATEGORIES
            ]
            return _step_still_need(state)

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
        return _step_budget(state)

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
            return _step_custom_budget(state)
        return _step_style(state)

    # ── STEP 3b: CUSTOM BUDGET ───────────────────────────────────────
    if current_step == ChatStep.CUSTOM_BUDGET:
        state.budget_amount = selection or user_input
        return _step_style(state)

    # ── STEP 4: STYLE & PREFERENCES ─────────────────────────────────
    if current_step == ChatStep.STYLE_PREFERENCES:
        style_vals = {"elegant", "traditional", "modern", "luxury", "fun", "minimal", "not_sure"}
        pref_vals = {"pref_cultural", "pref_budget", "pref_luxury", "pref_highly_rated", "pref_local", "pref_fast"}

        state.style = [v for v in selections if v in style_vals] or (
            [user_input] if user_input else ["not_sure"]
        )
        state.preferences = [v for v in selections if v in pref_vals]

        # Generate bundle
        bundle = generate_bundle(state)
        state.bundle = bundle

        return StepResponse(
            next_step=ChatStep.BUNDLE_ACTION,
            bot_message="Here's the bundle I built for you.",
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

    # ── STEP 6: BUNDLE ACTION ────────────────────────────────────────
    if current_step == ChatStep.BUNDLE_ACTION:
        if selection == "keep":
            return _step_results_booking(state)
        elif selection == "customize":
            return _step_manual_customize(state)
        elif selection == "swap":
            return _step_swap_vendor(state)
        elif selection == "remove":
            return _step_remove_category(state)
        elif selection == "add":
            return _step_add_category(state)
        elif selection == "cheaper":
            state.budget_tier = BudgetTier.BUDGET_FRIENDLY
            state.bundle = generate_bundle(state)
            return _step_bundle_action(state)
        elif selection == "premium_bundle":
            state.budget_tier = BudgetTier.PREMIUM
            state.bundle = generate_bundle(state)
            return _step_bundle_action(state)
        elif selection == "start_over":
            return get_initial_step()
        else:
            return _step_bundle_action(state)

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
        return _step_bundle_action(state)

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
        return _step_bundle_action(state)

    # ── STEP 6C: REMOVE CATEGORY ─────────────────────────────────────
    if current_step == ChatStep.REMOVE_CATEGORY:
        if state.bundle:
            state.bundle.items = [i for i in state.bundle.items if i.category not in selections]
            state.needed_categories = [c for c in state.needed_categories if c not in selections]
            state.bundle.estimated_total_min = sum(i.price_min for i in state.bundle.items)
            state.bundle.estimated_total_max = sum(i.price_max for i in state.bundle.items)

        resp = _step_bundle_action(state)
        resp.bot_message = "Done — I removed that category from your bundle."
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
        return resp

    # ── STEP 7: RESULTS & BOOKING ────────────────────────────────────
    if current_step == ChatStep.RESULTS_BOOKING:
        if selection == "book_all":
            return StepResponse(
                next_step=ChatStep.RESULTS_BOOKING,
                bot_message="Great! Proceeding to book your entire bundle. (Booking flow placeholder)",
                helper_buttons=[],
                state=state,
                bundle=state.bundle,
            )
        elif selection == "book_some":
            return _step_partial_booking(state)
        elif selection == "contact":
            return StepResponse(
                next_step=ChatStep.RESULTS_BOOKING,
                bot_message="Opening vendor contact flow. (Contact flow placeholder)",
                helper_buttons=[
                    HelperButton(label="Done contacting", value="done_contact"),
                    HelperButton(label="Go back and edit", value="go_back"),
                ],
                state=state,
                bundle=state.bundle,
            )
        elif selection == "save":
            return StepResponse(
                next_step=ChatStep.RESULTS_BOOKING,
                bot_message="Your bundle has been saved for later!",
                helper_buttons=[],
                state=state,
                bundle=state.bundle,
            )
        elif selection in ("go_back", "done_contact"):
            return _step_bundle_action(state)
        else:
            return _step_results_booking(state)

    # ── STEP 7b: PARTIAL BOOKING ─────────────────────────────────────
    if current_step == ChatStep.PARTIAL_BOOKING:
        booked = [v for v in selections if v in VENDOR_CATEGORIES]
        return StepResponse(
            next_step=ChatStep.RESULTS_BOOKING,
            bot_message=f"Proceeding to book: {', '.join(CATEGORY_LABELS.get(c, c) for c in booked)}. (Booking flow placeholder)",
            helper_buttons=[],
            state=state,
            bundle=state.bundle,
        )

    # Fallback
    logger.warning("Unhandled step: %s", current_step)
    return get_initial_step()
