"""Business logic for the chatbot bundle-builder flow.

Every public function is a pure-ish helper that takes the current step +
user input + state and returns the next step response.  Bundle generation
uses mock data for now—swap in real DB queries later.

Off-script user inputs are routed to Llama 3.3 via the llm_service module.
"""

import logging
import re
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


# ── LLM intent → step mapping ────────────────────────────────────────


def _apply_llm_intent(
    llm_result: LLMResult,
    state: ChatbotState,
    current_step: ChatStep,
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
            bundle = generate_bundle(state)
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


async def process_step(
    current_step: ChatStep,
    user_input: Optional[str],
    selected_values: list[str],
    state: ChatbotState,
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
        resp = _apply_llm_intent(llm_result, state, current_step)
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
        bundle = generate_bundle(state)
        state.bundle = bundle

        resp = StepResponse(
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
            state.bundle = generate_bundle(state)
            resp = _step_bundle_action(state)
        elif selection == "premium_bundle":
            state.budget_tier = BudgetTier.PREMIUM
            state.bundle = generate_bundle(state)
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
        if selection == "book_all":
            resp = StepResponse(
                next_step=ChatStep.RESULTS_BOOKING,
                bot_message="Great! Proceeding to book your entire bundle. (Booking flow placeholder)",
                helper_buttons=[],
                state=state,
                bundle=state.bundle,
            )
        elif selection == "book_some":
            resp = _step_partial_booking(state)
        elif selection == "contact":
            resp = StepResponse(
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
            resp = StepResponse(
                next_step=ChatStep.RESULTS_BOOKING,
                bot_message="Your bundle has been saved for later!",
                helper_buttons=[],
                state=state,
                bundle=state.bundle,
            )
        elif selection in ("go_back", "done_contact"):
            resp = _step_bundle_action(state)
        else:
            resp = _step_results_booking(state)
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── STEP 7b: PARTIAL BOOKING ─────────────────────────────────────
    if current_step == ChatStep.PARTIAL_BOOKING:
        booked = [v for v in selections if v in VENDOR_CATEGORIES]
        resp = StepResponse(
            next_step=ChatStep.RESULTS_BOOKING,
            bot_message=f"Proceeding to book: {', '.join(CATEGORY_LABELS.get(c, c) for c in booked)}. (Booking flow placeholder)",
            helper_buttons=[],
            state=state,
            bundle=state.bundle,
        )
        _append_history(state, user_input, resp.bot_message)
        return resp

    # Fallback
    logger.warning("Unhandled step: %s", current_step)
    return get_initial_step()
