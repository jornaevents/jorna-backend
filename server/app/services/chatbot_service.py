"""Business logic for the chatbot bundle-builder flow.

Every public function is a pure-ish helper that takes the current step +
user input + state and returns the next step response.  Bundle generation
uses mock data for now—swap in real DB queries later.

Off-script user inputs are routed to Llama 3.3 via the llm_service module.
"""

import logging
import re
import uuid
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
    CHATBOT_CATEGORIES,
    CHATBOT_SLOTS,
    HelperButton,
    MultiBundleResponse,
    StepResponse,
)
from app.services.llm_service import (
    LLMResult,
    get_llm_response,
    is_off_script,
)

logger = logging.getLogger(__name__)

MAX_HISTORY = 6  # keep conversation_history compact


# ── Helpers ──────────────────────────────────────────────────────────


def _slot_label(slot_key: str) -> str:
    """Display label for a chatbot bundle slot key (falls back gracefully)."""
    slot = CHATBOT_SLOTS.get(slot_key)
    if slot:
        return slot["label"]
    return CATEGORY_LABELS.get(slot_key, slot_key.title())


def _rule_based_match_reason(
    category: str,
    rating: float,
    bio: str,
    style: list[str],
    preferences: list[str],
    budget_tier: "BudgetTier | None",
) -> str:
    """Return a short match reason based on user preferences and vendor attributes."""
    cat = _slot_label(category).lower()
    tier_val = budget_tier.value if budget_tier else ""

    if "pref_highly_rated" in preferences and rating >= 4.5:
        return f"Top-rated {cat} — {rating}★ from past clients"
    if "pref_budget" in preferences or tier_val == "budget-friendly":
        return f"Quality {cat} that fits your budget"
    if "pref_luxury" in preferences or tier_val == "premium":
        return f"Premium {cat} for a luxury experience"
    if "pref_cultural" in preferences:
        return f"Experienced with South Asian events and traditions"
    if "pref_local" in preferences:
        return f"Local {cat} vendor familiar with your area"
    if "traditional" in style:
        return f"Perfect for a traditional South Asian celebration"
    if "elegant" in style:
        return f"Elegant, refined {cat} to match your vision"
    if "luxury" in style:
        return f"Luxurious {cat} experience for your special day"
    if "modern" in style:
        return f"Modern, contemporary {cat} style"
    if "fun" in style:
        return f"Brings energy and fun to your event"
    if rating >= 4.5:
        return f"Highly rated at {rating}★"
    truncated = (bio[:117] + "…") if len(bio) > 117 else bio
    return truncated or f"Quality {cat} vendor"


def _cat_buttons(exclude: list[str] | None = None) -> list[HelperButton]:
    """Return helper buttons for every chatbot bundle slot, optionally excluding some."""
    excluded = set(exclude or [])
    return [
        HelperButton(label=CHATBOT_SLOTS[c]["label"], value=c)
        for c in CHATBOT_CATEGORIES
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
        ] + _cat_buttons(exclude=state.booked_categories),
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
            HelperButton(label=_slot_label(c), value=c)
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
        helper_buttons=_cat_buttons(exclude=list(bundle_cats)),
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
            HelperButton(label=_slot_label(c), value=c)
            for c in bundle_cats
        ],
        state=state,
        bundle=state.bundle,
    )


# ── Multi-bundle generation ───────────────────────────────────────────


def _candidate_service_rows(
    cat: str,
    state: ChatbotState,
    db: Session,
    booked_vendor_ids: set[str],
    price_cap: float,
    used_vendor_ids: set[str],
) -> list[tuple]:
    """Candidate (Service, Vendor, User) rows for a slot — the service-first unit.

    Filters by the slot's Service.category (and Service.subcategory when the slot
    specifies one, e.g. dj vs dhol). Applies, in order: the one-vendor-per-bundle
    dedup (skip vendors already used in this bundle), distance (a venue by where
    it stands, everyone else by their travel radius), already-booked vendors, and
    the per-category budget cap (kept only if some service survives).
    Returns [] when nothing qualifies so the caller can fall back to a mock item.
    """
    from app.db.models import Vendor, Service, User
    from app.utils.location import VENUE_MAX_DISTANCE_MILES, calculate_distance_miles

    flt = _slot_db_filter(cat)
    if not flt:
        return []
    db_category, db_subcategory = flt

    q = (
        db.query(Service, Vendor, User)
        .join(Vendor, Service.vendor_id == Vendor.vendor_id)
        .join(User, Vendor.user_id == User.user_id)
        .filter(Service.category == db_category)
        # AI bundles only pick fixed-price (non-negotiable) services, so the whole
        # bundle can be one-click confirmed with no per-service price step. A user
        # who wants a negotiable service adds it manually via the bundle editor.
        .filter(Service.negotiable == False)  # noqa: E712 (SQL boolean compare)
    )
    if db_subcategory:
        q = q.filter(Service.subcategory == db_subcategory)
    rows = q.all()  # list of (Service, Vendor, User)

    # Dedup: a vendor fills at most one slot per generated bundle. A user who
    # wants a vendor in two roles does it manually in the bundle editor.
    if used_vendor_ids:
        rows = [(s, v, u) for s, v, u in rows if v.vendor_id not in used_vendor_ids]

    # Distance (only when the event has coordinates). A venue and a traveling
    # vendor are different questions, and answering them the same way is what
    # let venues in the wrong city into a bundle:
    #
    #   - A traveling vendor comes to the event, so the test is how far the
    #     event is from where *they* are based, against their own radius —
    #     and open_to_long_distance waives it, because they said it would.
    #   - A venue cannot travel; it is the location. So it qualifies only on
    #     where the building actually stands (Service.venue_*, required of
    #     every venue service). The owner's home address is irrelevant — they
    #     may live nowhere near it — and open_to_long_distance must NOT waive
    #     this, or a building "travels" across the country.
    if state.latitude is not None and state.longitude is not None:
        if db_category == "venue":
            rows = [
                (s, v, u) for s, v, u in rows
                # No coordinates means the distance is unknowable, and an
                # unknown-distance venue is exactly what this filter exists to
                # exclude. Venue services created through the API always carry
                # them (service_service._require_venue_location).
                if s.venue_latitude is not None and s.venue_longitude is not None
                and calculate_distance_miles(
                    state.latitude, state.longitude, s.venue_latitude, s.venue_longitude
                ) <= VENUE_MAX_DISTANCE_MILES
            ]
        else:
            # A vendor qualifies by being near enough to come, or by saying they
            # travel anyway. Not by being unplaceable: no coordinates used to
            # mean "keep", so any vendor without a location on file was offered
            # for every event in the country — which is most of them, since
            # registration collects a city and throws its coordinates away.
            #
            # /vendors/search has always excluded them for exactly this reason.
            # Two readings of one rule, and this was the permissive one.
            rows = [
                (s, v, u) for s, v, u in rows
                if u.latitude is not None and u.longitude is not None
                and (
                    v.open_to_long_distance
                    or calculate_distance_miles(
                        state.latitude, state.longitude, u.latitude, u.longitude
                    ) <= v.travel_radius_miles
                )
            ]

    # Drop vendors already booked on the event date
    if booked_vendor_ids:
        rows = [(s, v, u) for s, v, u in rows if v.vendor_id not in booked_vendor_ids]

    # Budget cap: keep services within cap; if none qualify, keep all (show the
    # cheapest available even if over budget).
    if price_cap < float("inf"):
        within = [(s, v, u) for s, v, u in rows if s.price <= price_cap]
        if within:
            rows = within

    return rows


def _build_bundle_with_strategy(
    state: ChatbotState,
    strategy: str,
    db: Session,
    booked_vendor_ids: set[str] | None = None,
) -> Bundle:
    """Build a bundle using a specific selection strategy.

    strategy:
      'budget'    — pick the cheapest service per slot
      'top_rated' — pick the highest-rated vendor's service per slot
      'balanced'  — balance rating and price equally

    Slots are filled by services (matched on Service.category / subcategory),
    a vendor fills at most one slot per bundle, and each slot's price is the
    chosen service's own price. All three strategies also factor in LLM-scored
    tag relevance when style/preferences are provided.
    """
    from app.db.models import Vendor, Tag
    from app.services.llm_service import get_relevant_tags_for_preferences

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

    # Same set for every category in this bundle — and, when the caller passes
    # one, for every bundle in a set of options too. Deriving it here per bundle
    # is what let each option see the previous one's freshly written bookings.
    if booked_vendor_ids is None:
        booked_vendor_ids = _get_booked_vendor_ids(state, db)
    price_cap = _per_category_cap(state)
    used_vendor_ids: set[str] = set()

    def _tag_bonus(v) -> float:
        """Small bonus for vendors whose tags match user preferences."""
        if not llm_relevant_tags:
            return 0.0
        v_tags = {t.name.lower() for t in (v.tags or [])} | {t.lower() for t in (v.instagram_tags or [])}
        return len(v_tags & llm_relevant_tags) * 0.1

    unfilled: list[str] = []
    for cat in state.needed_categories:
        rows = _candidate_service_rows(cat, state, db, booked_vendor_ids, price_cap, used_vendor_ids)

        if not rows:
            # No real, available vendor for this slot — leave it out and report it
            # rather than inventing a placeholder.
            unfilled.append(cat)
            continue

        # Rank individual services by the strategy. row = (service, vendor, user).
        if strategy == "budget":
            rows.sort(key=lambda r: (r[0].price, -_tag_bonus(r[1])))
        elif strategy == "top_rated":
            rows.sort(key=lambda r: (-(r[1].rating or 0.0), r[0].price, -_tag_bonus(r[1])))
        else:  # balanced
            max_price = max((r[0].price for r in rows), default=1.0) or 1.0
            max_rating = max((r[1].rating or 0.0 for r in rows), default=1.0) or 1.0
            rows.sort(
                key=lambda r: (r[1].rating or 0.0) / max_rating * 0.5
                              + (1 - r[0].price / max_price) * 0.3
                              + _tag_bonus(r[1]) * 0.2,
                reverse=True,
            )

        service, vendor, user = rows[0]
        used_vendor_ids.add(vendor.vendor_id)
        price = round(service.price, 2)
        match_reason = service.description or service.experience or vendor.bio or ""

        items.append(BundleItem(
            category=cat,
            vendor_id=vendor.vendor_id,
            service_id=service.service_id,
            service_name=service.name,
            vendor_name=f"{user.f_name} {user.l_name}",
            pfp_url=user.pfp_url,
            price_min=price,
            price_max=price,
            rating=vendor.rating or 0.0,
            match_reason=_rule_based_match_reason(
                cat, vendor.rating or 0.0, match_reason,
                list(state.style), list(state.preferences), state.budget_tier,
            ),
        ))
        total_min += price
        total_max += price

    return Bundle(
        items=items,
        estimated_total_min=round(total_min, 2),
        estimated_total_max=round(total_max, 2),
        unfilled_categories=unfilled,
    )


def generate_multi_bundle(
    req: BundleRequest,
    db: Session,
    user_id: str | None = None,
) -> MultiBundleResponse:
    """Generate 3 bundle options for users who aren't sure what they want.

    Bundles are always built from real DB vendors. When user_id is provided,
    all 3 bundles and their bookings are persisted to the DB as drafts with a
    shared bundle_group_id.  The user then calls POST /bundles/{bundle_id}/select
    to keep one and discard the others. Vendor notifications are NOT sent until
    the user selects a bundle.
    """
    needed = req.needed_categories or [
        c for c in CHATBOT_CATEGORIES if c not in req.booked_categories
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
    group_id = str(uuid.uuid4()) if user_id else None

    # Who's genuinely unavailable, read once — before any of these options exist.
    #
    # Each option is saved as it's built, and saving writes real bookings. The
    # availability check then read them back, so option two found option one's
    # vendors "taken" and option three found both. These are alternatives for
    # one event — you keep one and the other two are deleted — so they were
    # competing for the same vendors and the first one always won. With a thin
    # category that emptied the later bundles outright; with a fat one it just
    # looked like variety, since no vendor could ever appear in two of them.
    #
    # They draw from the same pool now, which also means the same vendor may
    # appear in more than one. That's right: if one venue is the best fit at
    # every price point, the tiers should differ where there's genuine choice,
    # not by being denied the obvious answer.
    booked_vendor_ids = _get_booked_vendor_ids(
        ChatbotState(
            event_date=req.event_date,
            date_range=req.date_range,
            time_start=req.time_start,
            time_end=req.time_end,
            needed_categories=needed,
        ),
        db,
    )

    for strategy, tier, label, description, factors in _PRESETS:
        state = ChatbotState(
            event_date=req.event_date,
            date_range=req.date_range,
            location=req.location,
            latitude=req.latitude,
            longitude=req.longitude,
            guest_count=req.guest_count,
            time_start=req.time_start,
            time_end=req.time_end,
            booked_categories=req.booked_categories,
            needed_categories=needed,
            budget_tier=tier,
            budget_amount=req.budget_amount,
            style=req.style,
            preferences=req.preferences,
        )

        bundle = _build_bundle_with_strategy(state, strategy, db, booked_vendor_ids)
        state.bundle = bundle

        db_bundle_id: str | None = None
        if user_id and group_id:
            db_bundle_id, _ = _create_bundle_from_chatbot(
                state, user_id, None, db,
                bundle_group_id=group_id,
                notify_vendors=False,
            )

        options.append(BundleOption(
            label=label,
            description=description,
            factors=factors,
            bundle=bundle,
            state=state,
            bundle_id=db_bundle_id,
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
        time_start=req.time_start,
        time_end=req.time_end,
        booked_categories=req.booked_categories,
        needed_categories=req.needed_categories or [
            c for c in CHATBOT_CATEGORIES if c not in req.booked_categories
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
        bot_message=f"Here's your bundle{date_info}.{_unfilled_note(bundle)}",
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


def generate_bundle(state: ChatbotState, db: Session) -> Bundle:
    """Build a bundle from real DB vendors, one service per needed category.

    Slots with no available vendor are left out and recorded in
    ``bundle.unfilled_categories`` — the builder never invents placeholder
    ("mock") vendors, so a client only ever sees real, bookable services.
    """
    return _generate_bundle_from_db(state, db)


def _unfilled_note(bundle: Bundle | None) -> str:
    """A short sentence naming the categories we couldn't fill, or "" if none."""
    cats = bundle.unfilled_categories if bundle else []
    if not cats:
        return ""
    labels = [_slot_label(c) for c in cats]
    if len(labels) == 1:
        joined = labels[0]
    elif len(labels) == 2:
        joined = f"{labels[0]} and {labels[1]}"
    else:
        joined = ", ".join(labels[:-1]) + f", and {labels[-1]}"
    return (
        f" I couldn't find an available {joined} for your date, so I left "
        f"{'that' if len(labels) == 1 else 'those'} out — you can add "
        f"{'it' if len(labels) == 1 else 'them'} later if a vendor opens up."
    )


def _slot_db_filter(slot_key: str) -> tuple[str, str | None] | None:
    """Resolve a chatbot slot key to (db_category, db_subcategory).

    Returns None for unknown slots so the caller can skip them. When
    db_subcategory is not None, the vendor query must filter on it too —
    this is what lets a slot target a specific subcategory (e.g. dhol).
    """
    slot = CHATBOT_SLOTS.get(slot_key)
    if not slot:
        return None
    return slot["category"], slot["subcategory"]

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

    # Only bookings that actually commit a vendor. A pending request is a lead —
    # LOCKED_BOOKING_STATUSES says so, and nothing stops a vendor answering two
    # of them. Counting pending here meant one client's un-actioned request hid
    # a vendor from every other client's builder; worse, this builder writes
    # pending bookings for every option it generates, so an abandoned draft went
    # on hiding its vendors from everyone, indefinitely.
    from app.services.booking_service import LOCKED_BOOKING_STATUSES, booking_blocks

    rows = (
        db.query(Booking)
        .filter(
            Booking.status.in_(LOCKED_BOOKING_STATUSES),
            Booking.date_iso <= req_end,
            booking_end >= req_start,
        )
        .all()
    )

    # And only where the hours actually collide, by the same rule that decides
    # whether a vendor may accept. A vendor with a morning ceremony can take an
    # evening reception, so proposing them isn't proposing a doomed slot.
    # (Today the builder rarely knows its own times — it writes "TBD" — and an
    # unknown time can't be shown not to overlap, so this stays whole-day until
    # the builder asks. It's the shared rule that matters: one definition, so
    # what the builder offers and what a vendor may accept can't disagree.)
    return {
        b.vendor_id
        for b in rows
        if booking_blocks(
            b,
            date_iso=req_start,
            date_end=req_end if req_end != req_start else None,
            time_start=state.time_start,
            time_end=state.time_end,
        )
    }


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
    """Query real services from the DB, one per needed category (service-first).

    Each slot is filled by a single service (matched on Service.category /
    subcategory), scored via its vendor, with a vendor filling at most one slot
    per bundle. The slot price is the chosen service's own price.
    """
    from app.db.models import Vendor
    from app.services.llm_service import get_relevant_tags_for_preferences

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
    used_vendor_ids: set[str] = set()

    unfilled: list[str] = []
    for cat in state.needed_categories:
        rows = _candidate_service_rows(cat, state, db, booked_vendor_ids, price_cap, used_vendor_ids)

        if not rows:
            # No real, available vendor for this slot — leave it out and report it
            # rather than inventing a placeholder.
            unfilled.append(cat)
            continue

        # Score each candidate service by its vendor (price-aware via the single
        # service), then pick the highest score, tie-broken by the cheaper service.
        scored = [
            (_score_vendor(v, [s], state.style, state.preferences, tier, llm_relevant_tags), s.price, s, v, u)
            for s, v, u in rows
        ]
        scored.sort(key=lambda x: (-x[0], x[1]))
        _, _, service, vendor, user = scored[0]
        used_vendor_ids.add(vendor.vendor_id)

        price = round(service.price, 2)
        match_reason = service.description or service.experience or vendor.bio or ""

        items.append(BundleItem(
            category=cat,
            vendor_id=vendor.vendor_id,
            service_id=service.service_id,
            service_name=service.name,
            vendor_name=f"{user.f_name} {user.l_name}",
            pfp_url=user.pfp_url,
            price_min=price,
            price_max=price,
            rating=vendor.rating or 0.0,
            match_reason=_rule_based_match_reason(
                cat, vendor.rating or 0.0, match_reason,
                list(state.style), list(state.preferences), state.budget_tier,
            ),
        ))
        total_min += price
        total_max += price

    return Bundle(
        items=items,
        estimated_total_min=round(total_min, 2),
        estimated_total_max=round(total_max, 2),
        unfilled_categories=unfilled,
    )


def _real_item_for_category(
    cat: str,
    state: ChatbotState,
    db: Session,
    *,
    exclude_vendor_ids: set[str] | None = None,
) -> BundleItem | None:
    """Pick the top real service filling *cat* (highest-rated, cheapest tiebreak),
    or None when no available vendor exists. Backs the conversational swap/add
    steps so they only ever surface real, bookable vendors.
    """
    booked_vendor_ids = _get_booked_vendor_ids(state, db)
    rows = _candidate_service_rows(
        cat, state, db, booked_vendor_ids,
        _per_category_cap(state), exclude_vendor_ids or set(),
    )
    if not rows:
        return None
    rows.sort(key=lambda r: (-(r[1].rating or 0.0), r[0].price))
    service, vendor, user = rows[0]
    price = round(service.price, 2)
    match_reason = service.description or service.experience or vendor.bio or ""
    return BundleItem(
        category=cat,
        vendor_id=vendor.vendor_id,
        service_id=service.service_id,
        service_name=service.name,
        vendor_name=f"{user.f_name} {user.l_name}",
        pfp_url=user.pfp_url,
        price_min=price,
        price_max=price,
        rating=vendor.rating or 0.0,
        match_reason=_rule_based_match_reason(
            cat, vendor.rating or 0.0, match_reason,
            list(state.style), list(state.preferences), state.budget_tier,
        ),
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
        cats = [c for c in values.get("categories", []) if c in CHATBOT_CATEGORIES]
        state.booked_categories = cats
        resp = _step_still_need(state)
        resp.bot_message = f"{llm_result.bot_message}\n\n{resp.bot_message}"
        resp.llm_response = True
        return resp

    # ── Set needed categories ────────────────────────────────────────
    if intent == "set_needed":
        cats = [c for c in values.get("categories", []) if c in CHATBOT_CATEGORIES]
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
    bundle_group_id: str | None = None,
    notify_vendors: bool = True,
) -> tuple[str, list[str]]:
    """Create a Bundle and Bookings in the DB from the chatbot state.

    categories: list of category keys to book, or None to book all items.
    Returns (bundle_id, [booking_id, ...]).
    """
    from datetime import datetime, timezone
    from app.db.models import Bundle, Booking, Service
    from app.services.booking_service import estimate_amount_cents

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
        bundle_group_id=bundle_group_id,
        created_at=now,
        updated_at=now,
    )
    db.add(bundle)
    db.flush()

    booking_ids: list[str] = []
    created_bookings: list[Booking] = []
    for item in items_to_book:
        booking = Booking(
            user_id=user_id,
            vendor_id=item.vendor_id,
            service_id=item.service_id,
            date_iso=date_iso,
            date_end=date_end,
            guest_count=state.guest_count,
            time_start=state.time_start or "TBD",
            time_end=state.time_end or "TBD",
            location=location,
            status="pending",
            bundle_id=bundle.bundle_id,
        )
        # Estimated total = rate x quantity (guests / days / hours), per the
        # service's price_unit. Left nil (flat rate) when it can't be computed.
        svc = db.query(Service).filter(Service.service_id == item.service_id).first()
        est = estimate_amount_cents(
            svc,
            guest_count=state.guest_count,
            date_iso=date_iso,
            date_end=date_end,
            time_start=state.time_start,
            time_end=state.time_end,
        )
        if est is not None:
            booking.amount_cents = est
        db.add(booking)
        db.flush()
        booking_ids.append(booking.booking_id)
        created_bookings.append(booking)

    # Anchor the event's venue from the live venue booking and mirror its coords
    # onto the traveling vendors' bookings (DJ, catering, etc.) for GPS check-in.
    from app.services.booking_service import sync_event_venue
    sync_event_venue(bundle.bundle_id, db)

    db.commit()

    if notify_vendors:
        from app.services.booking_service import _get_booking_parties, _dispatch_status_notification
        for booking in created_bookings:
            try:
                client, _, vendor_user, service = _get_booking_parties(db, booking)
                _dispatch_status_notification("pending", booking, client, vendor_user, service, db, event_name=event_name)
            except Exception as exc:
                logger.warning("Chatbot booking notification failed for %s: %s", booking.booking_id, exc)

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
        else:
            state.booked_categories = [
                v for v in selections if v in CHATBOT_CATEGORIES
            ]
        resp = _step_still_need(state)
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── STEP 2: STILL NEED ──────────────────────────────────────────
    if current_step == ChatStep.STILL_NEED:
        if "recommend_all" in selections or selection == "recommend_all":
            state.needed_categories = [
                c for c in CHATBOT_CATEGORIES if c not in state.booked_categories
            ]
        else:
            chosen = [v for v in selections if v in CHATBOT_CATEGORIES]
            # Remove duplicates already booked
            state.needed_categories = [
                c for c in chosen if c not in state.booked_categories
            ]
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
        # Swap each selected category to a different real vendor.
        if state.bundle and db is not None:
            used = {i.vendor_id for i in state.bundle.items if i.vendor_id}
            for idx, item in enumerate(state.bundle.items):
                if item.category in selections:
                    alt = _real_item_for_category(item.category, state, db, exclude_vendor_ids=used)
                    if alt:
                        used.discard(item.vendor_id)
                        used.add(alt.vendor_id)
                        state.bundle.items[idx] = alt
            state.bundle.estimated_total_min = sum(i.price_min for i in state.bundle.items)
            state.bundle.estimated_total_max = sum(i.price_max for i in state.bundle.items)
        resp = _step_bundle_action(state)
        _append_history(state, user_input, resp.bot_message)
        return resp

    # ── STEP 6B: SWAP VENDOR ─────────────────────────────────────────
    if current_step == ChatStep.SWAP_VENDOR:
        # Same as manual customize, for a single category.
        if state.bundle and db is not None:
            cat_to_swap = selection
            used = {i.vendor_id for i in state.bundle.items if i.vendor_id}
            for idx, item in enumerate(state.bundle.items):
                if item.category == cat_to_swap:
                    alt = _real_item_for_category(cat_to_swap, state, db, exclude_vendor_ids=used)
                    if alt:
                        used.discard(item.vendor_id)
                        used.add(alt.vendor_id)
                        state.bundle.items[idx] = alt
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
        new_cats = [v for v in selections if v in CHATBOT_CATEGORIES and v not in state.needed_categories]
        state.needed_categories.extend(new_cats)

        # Fill each new category with a real vendor; skip any with no supply.
        if db is not None:
            used = {i.vendor_id for i in (state.bundle.items if state.bundle else []) if i.vendor_id}
            for cat in new_cats:
                item = _real_item_for_category(cat, state, db, exclude_vendor_ids=used)
                if item:
                    if state.bundle is None:
                        state.bundle = Bundle()
                    state.bundle.items.append(item)
                    if item.vendor_id:
                        used.add(item.vendor_id)

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
        chosen_cats = [v for v in selections if v in CHATBOT_CATEGORIES]
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
