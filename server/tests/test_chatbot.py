"""Tests for the chatbot bundle-builder API.

Covers:
  - Service-layer unit tests (step transitions, bundle generation)
  - Router integration tests via FastAPI TestClient
  - LLM fallback tests (off-script detection + mocked Llama 3.3 responses)
"""

import os
import pytest

# Ensure DATABASE_URL is set before importing main (which triggers database.py)
from dotenv import load_dotenv
load_dotenv()
if "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = "sqlite:///./test_chatbot.db"

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import app
from app.db.models import User, Vendor, Service
from tests.test_api import TestingSessionLocal, make_auth_headers
from app.models.chatbot_schemas import (
    BudgetTier,
    Bundle,
    ChatbotState,
    ChatStep,
    DateRange,
    StepRequest,
    CHATBOT_CATEGORIES,
    CHATBOT_SLOTS,
)
from app.services.chatbot_service import (
    generate_bundle,
    get_initial_step,
    process_step,
)
from app.services.llm_service import (
    LLMResult,
    is_off_script,
)

client = TestClient(app)


# ── Fixtures ──────────────────────────────────────────────────────────
# Mock vendors were removed: the builder only ever fills a slot from a real DB
# service. So the bundle-generation tests need real supply seeded first. Seed
# two vendors per chatbot slot — a cheaper (rating 4.5) and a premium (rating
# 5.0, pricier) one — so budget vs. premium selection is genuinely exercised.
_SEED_PRICES = [("budget", 900.0, 4.5), ("premium", 2500.0, 5.0)]


@pytest.fixture(scope="module", autouse=True)
def _seed_chatbot_services():
    import uuid
    db = TestingSessionLocal()
    for slot_key, slot in CHATBOT_SLOTS.items():
        cat, sub = slot["category"], slot["subcategory"]
        for tag, price, rating in _SEED_PRICES:
            uid = uuid.uuid4().hex[:8]
            u = User(
                email=f"seed_{slot_key}_{tag}_{uid}@test.com",
                username=f"seed_{slot_key}_{tag}_{uid}",
                password="pw", phone="1", f_name=slot_key, l_name=tag,
                age=30, location="NJ", gender="F", language="EN", token_version=0,
            )
            db.add(u); db.commit(); db.refresh(u)
            v = Vendor(user_id=u.user_id, bio=f"{slot_key} {tag}", category=cat,
                       subcategory=sub, rating=rating, num_events=15)
            db.add(v); db.commit(); db.refresh(v)
            s = Service(name=f"{slot_key}-{tag}", price=price, duration_minutes=120,
                        vendor_id=v.vendor_id, experience="exp",
                        category=cat, subcategory=sub, negotiable=False)
            db.add(s); db.commit()
    db.close()
    yield


@pytest.fixture
def db():
    """A DB session for tests that build bundles from real seeded supply."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════
# SERVICE-LAYER UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════


class TestGetInitialStep:
    def test_returns_event_details_step(self):
        resp = get_initial_step()
        assert resp.next_step == ChatStep.EVENT_DETAILS

    def test_has_helper_buttons(self):
        resp = get_initial_step()
        labels = [b.label for b in resp.helper_buttons]
        assert "I have a date" in labels
        assert "No date yet" in labels
        assert "Continue" in labels

    def test_state_is_empty(self):
        resp = get_initial_step()
        assert resp.state.event_date is None
        assert resp.state.guest_count is None
        assert resp.state.booked_categories == []
        assert resp.state.bundle is None


@pytest.mark.asyncio
class TestEventDetailsStep:
    async def test_advances_to_already_booked(self):
        state = ChatbotState()
        resp = await process_step(ChatStep.EVENT_DETAILS, "August 2026, NJ, 300 guests", [], state)
        assert resp.next_step == ChatStep.EVENT_TIME

    async def test_extracts_guest_count(self):
        state = ChatbotState()
        resp = await process_step(ChatStep.EVENT_DETAILS, "around 300 guests in New Jersey", [], state)
        assert resp.state.guest_count == 300

    async def test_extracts_date(self):
        state = ChatbotState()
        resp = await process_step(ChatStep.EVENT_DETAILS, "August 2026, NJ", [], state)
        assert resp.state.event_date is not None
        assert "august" in resp.state.event_date.lower()

    async def test_no_date_button(self):
        state = ChatbotState()
        resp = await process_step(ChatStep.EVENT_DETAILS, None, ["no_date"], state)
        assert resp.state.event_date == "TBD"
        assert resp.next_step == ChatStep.EVENT_TIME

    async def test_guest_count_not_confused_with_year(self):
        state = ChatbotState()
        resp = await process_step(ChatStep.EVENT_DETAILS, "August 2026, New Jersey", [], state)
        # Should NOT set guest_count to 2026
        assert resp.state.guest_count is None or resp.state.guest_count != 2026


@pytest.mark.asyncio
class TestAlreadyBookedStep:
    async def test_nothing_yet_goes_to_still_need(self):
        """Nothing booked → the user still chooses what they need on the next
        step (with "recommend everything" one tap away) rather than being
        auto-assigned all categories."""
        state = ChatbotState()
        resp = await process_step(ChatStep.ALREADY_BOOKED, None, ["nothing_yet"], state)
        assert resp.next_step == ChatStep.STILL_NEED
        assert resp.state.booked_categories == []

    async def test_categories_selected_goes_to_still_need(self):
        state = ChatbotState()
        resp = await process_step(ChatStep.ALREADY_BOOKED, None, ["venue", "photography"], state)
        assert resp.next_step == ChatStep.STILL_NEED
        assert resp.state.booked_categories == ["venue", "photography"]

    async def test_single_category(self):
        state = ChatbotState()
        resp = await process_step(ChatStep.ALREADY_BOOKED, None, ["dj"], state)
        assert resp.next_step == ChatStep.STILL_NEED
        assert resp.state.booked_categories == ["dj"]


@pytest.mark.asyncio
class TestStillNeedStep:
    async def test_recommend_all(self):
        state = ChatbotState(booked_categories=["venue", "photography"])
        resp = await process_step(ChatStep.STILL_NEED, None, ["recommend_all"], state)
        assert resp.next_step == ChatStep.BUDGET
        assert "venue" not in resp.state.needed_categories
        assert "photography" not in resp.state.needed_categories
        assert "catering" in resp.state.needed_categories
        assert "dj" in resp.state.needed_categories

    async def test_specific_categories(self):
        state = ChatbotState(booked_categories=["venue"])
        resp = await process_step(ChatStep.STILL_NEED, None, ["dj", "mehndi", "dhol"], state)
        assert resp.next_step == ChatStep.BUDGET
        assert sorted(resp.state.needed_categories) == sorted(["dj", "mehndi", "dhol"])

    async def test_removes_already_booked_duplicates(self):
        state = ChatbotState(booked_categories=["venue", "dj"])
        resp = await process_step(ChatStep.STILL_NEED, None, ["venue", "dj", "mehndi"], state)
        assert "venue" not in resp.state.needed_categories
        assert "dj" not in resp.state.needed_categories
        assert "mehndi" in resp.state.needed_categories

    async def test_other_category_is_filtered(self):
        # "other" is not a core chatbot category — it should be dropped, leaving catering
        state = ChatbotState(booked_categories=[])
        resp = await process_step(ChatStep.STILL_NEED, None, ["catering", "other"], state)
        assert "other" not in resp.state.needed_categories
        assert "catering" in resp.state.needed_categories


@pytest.mark.asyncio
class TestBudgetStep:
    async def test_budget_friendly(self):
        state = ChatbotState()
        resp = await process_step(ChatStep.BUDGET, None, ["budget-friendly"], state)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES
        assert resp.state.budget_tier == BudgetTier.BUDGET_FRIENDLY

    async def test_mid_range(self):
        state = ChatbotState()
        resp = await process_step(ChatStep.BUDGET, None, ["mid-range"], state)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES
        assert resp.state.budget_tier == BudgetTier.MID_RANGE

    async def test_premium(self):
        state = ChatbotState()
        resp = await process_step(ChatStep.BUDGET, None, ["premium"], state)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES

    async def test_custom_goes_to_custom_budget(self):
        state = ChatbotState()
        resp = await process_step(ChatStep.BUDGET, None, ["custom"], state)
        assert resp.next_step == ChatStep.CUSTOM_BUDGET
        assert resp.state.budget_tier == BudgetTier.CUSTOM

    async def test_not_sure(self):
        state = ChatbotState()
        resp = await process_step(ChatStep.BUDGET, None, ["unknown"], state)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES
        assert resp.state.budget_tier == BudgetTier.UNKNOWN


@pytest.mark.asyncio
class TestCustomBudgetStep:
    async def test_amount_advances_to_style(self):
        state = ChatbotState(budget_tier=BudgetTier.CUSTOM)
        resp = await process_step(ChatStep.CUSTOM_BUDGET, "8000", [], state)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES
        assert resp.state.budget_amount == "8000"

    async def test_button_range(self):
        state = ChatbotState(budget_tier=BudgetTier.CUSTOM)
        resp = await process_step(ChatStep.CUSTOM_BUDGET, None, ["3000_7000"], state)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES
        assert resp.state.budget_amount == "3000_7000"


@pytest.mark.asyncio
class TestStylePreferencesStep:
    async def test_generates_bundle(self, db):
        state = ChatbotState(
            needed_categories=["dj", "catering", "floral_decor"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        resp = await process_step(ChatStep.STYLE_PREFERENCES, None, ["elegant", "pref_local"], state, db=db)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert resp.bundle is not None
        assert len(resp.bundle.items) == 3
        assert resp.state.style == ["elegant"]
        assert resp.state.preferences == ["pref_local"]

    async def test_free_text_style(self, db):
        state = ChatbotState(
            needed_categories=["dj"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        resp = await process_step(ChatStep.STYLE_PREFERENCES, "elegant and classy", [], state, db=db)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert resp.bundle is not None

    async def test_bundle_has_correct_categories(self, db):
        cats = ["dj", "mehndi", "dhol"]
        state = ChatbotState(needed_categories=cats, budget_tier=BudgetTier.PREMIUM)
        resp = await process_step(ChatStep.STYLE_PREFERENCES, None, ["modern"], state, db=db)
        bundle_cats = [item.category for item in resp.bundle.items]
        assert sorted(bundle_cats) == sorted(cats)


class TestBundleGeneration:
    def test_basic_bundle(self, db):
        state = ChatbotState(
            needed_categories=["dj", "catering"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        bundle = generate_bundle(state, db)
        assert len(bundle.items) == 2
        assert bundle.estimated_total_min > 0
        # Single-price model: each service has one price, so the bundle's min and
        # max totals are equal (no range).
        assert bundle.estimated_total_max == bundle.estimated_total_min
        # Every slot is a real, bookable service (a vendor + service id).
        assert all(i.vendor_id and i.service_id for i in bundle.items)

    def test_budget_friendly_cheaper(self, db):
        state_budget = ChatbotState(
            needed_categories=["dj"],
            budget_tier=BudgetTier.BUDGET_FRIENDLY,
        )
        state_prem = ChatbotState(
            needed_categories=["dj"],
            budget_tier=BudgetTier.PREMIUM,
        )
        budget_bundle = generate_bundle(state_budget, db)
        premium_bundle = generate_bundle(state_prem, db)
        assert budget_bundle.estimated_total_max < premium_bundle.estimated_total_max

    def test_all_categories(self, db):
        state = ChatbotState(
            needed_categories=list(CHATBOT_CATEGORIES),
            budget_tier=BudgetTier.MID_RANGE,
        )
        bundle = generate_bundle(state, db)
        assert len(bundle.items) == len(CHATBOT_CATEGORIES)
        assert bundle.unfilled_categories == []

    def test_empty_categories(self, db):
        state = ChatbotState(needed_categories=[], budget_tier=BudgetTier.MID_RANGE)
        bundle = generate_bundle(state, db)
        assert len(bundle.items) == 0
        assert bundle.estimated_total_min == 0

    def test_unknown_category_skipped(self, db):
        state = ChatbotState(
            needed_categories=["dj", "other"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        bundle = generate_bundle(state, db)
        # "other" isn't a real slot → no supply → left out and reported, never
        # padded with a placeholder vendor.
        assert len(bundle.items) == 1
        assert bundle.unfilled_categories == ["other"]


@pytest.mark.asyncio
class TestBundleActionStep:
    def _make_state_with_bundle(self, db):
        state = ChatbotState(
            needed_categories=list(CHATBOT_CATEGORIES),
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state, db)
        return state

    async def test_keep_goes_to_results(self, db):
        state = self._make_state_with_bundle(db)
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["keep"], state, db=db)
        assert resp.next_step == ChatStep.RESULTS_BOOKING

    async def test_customize_goes_to_manual(self, db):
        state = self._make_state_with_bundle(db)
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["customize"], state, db=db)
        assert resp.next_step == ChatStep.MANUAL_CUSTOMIZE

    async def test_swap_goes_to_swap(self, db):
        state = self._make_state_with_bundle(db)
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["swap"], state, db=db)
        assert resp.next_step == ChatStep.SWAP_VENDOR

    async def test_remove_goes_to_remove(self, db):
        state = self._make_state_with_bundle(db)
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["remove"], state, db=db)
        assert resp.next_step == ChatStep.REMOVE_CATEGORY

    async def test_add_goes_to_add(self, db):
        state = self._make_state_with_bundle(db)
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["add"], state, db=db)
        assert resp.next_step == ChatStep.ADD_CATEGORY

    async def test_cheaper_regenerates(self, db):
        state = self._make_state_with_bundle(db)
        original_max = state.bundle.estimated_total_max
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["cheaper"], state, db=db)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert resp.state.budget_tier == BudgetTier.BUDGET_FRIENDLY
        assert resp.bundle.estimated_total_max < original_max

    async def test_premium_regenerates(self, db):
        state = self._make_state_with_bundle(db)
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["premium_bundle"], state, db=db)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert resp.state.budget_tier == BudgetTier.PREMIUM

    async def test_start_over(self, db):
        state = self._make_state_with_bundle(db)
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["start_over"], state, db=db)
        assert resp.next_step == ChatStep.EVENT_DETAILS


@pytest.mark.asyncio
class TestSwapVendor:
    async def test_swap_changes_vendor(self, db):
        state = ChatbotState(
            needed_categories=["dj"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state, db)
        original_name = state.bundle.items[0].vendor_name

        resp = await process_step(ChatStep.SWAP_VENDOR, None, ["dj"], state, db=db)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        swapped_name = resp.state.bundle.items[0].vendor_name
        assert swapped_name != original_name


@pytest.mark.asyncio
class TestRemoveCategory:
    async def test_remove_shrinks_bundle(self, db):
        state = ChatbotState(
            needed_categories=["dj", "mehndi", "dhol"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state, db)
        assert len(state.bundle.items) == 3

        resp = await process_step(ChatStep.REMOVE_CATEGORY, None, ["mehndi"], state, db=db)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert len(resp.state.bundle.items) == 2
        cats = [i.category for i in resp.state.bundle.items]
        assert "mehndi" not in cats
        assert "Done" in resp.bot_message


@pytest.mark.asyncio
class TestAddCategory:
    async def test_add_expands_bundle(self, db):
        state = ChatbotState(
            needed_categories=["dj"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state, db)
        assert len(state.bundle.items) == 1

        resp = await process_step(ChatStep.ADD_CATEGORY, None, ["mehndi"], state, db=db)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert len(resp.state.bundle.items) == 2
        cats = [i.category for i in resp.state.bundle.items]
        assert "mehndi" in cats
        assert "Added" in resp.bot_message


@pytest.mark.asyncio
class TestResultsBookingStep:
    def _make_state(self, db):
        state = ChatbotState(
            needed_categories=["dj", "catering"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state, db)
        return state

    async def test_book_all(self, db):
        state = self._make_state(db)
        resp = await process_step(ChatStep.RESULTS_BOOKING, None, ["book_all"], state, db=db)
        assert "book" in resp.bot_message.lower() or "Proceeding" in resp.bot_message

    async def test_book_some_goes_to_partial(self, db):
        state = self._make_state(db)
        resp = await process_step(ChatStep.RESULTS_BOOKING, None, ["book_some"], state, db=db)
        assert resp.next_step == ChatStep.PARTIAL_BOOKING

    async def test_save(self, db):
        state = self._make_state(db)
        resp = await process_step(ChatStep.RESULTS_BOOKING, None, ["save"], state, db=db)
        assert resp.next_step == ChatStep.RESULTS_BOOKING

    async def test_go_back(self, db):
        state = self._make_state(db)
        resp = await process_step(ChatStep.RESULTS_BOOKING, None, ["go_back"], state, db=db)
        assert resp.next_step == ChatStep.BUNDLE_ACTION

    async def test_contact(self, db):
        state = self._make_state(db)
        resp = await process_step(ChatStep.RESULTS_BOOKING, None, ["contact"], state, db=db)
        assert resp.next_step == ChatStep.RESULTS_BOOKING


@pytest.mark.asyncio
class TestPartialBooking:
    async def test_selects_categories(self, db):
        state = ChatbotState(
            needed_categories=["dj", "catering"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state, db)
        resp = await process_step(ChatStep.PARTIAL_BOOKING, None, ["dj"], state, db=db)
        assert resp.next_step == ChatStep.RESULTS_BOOKING


class TestSubcategoryTargeting:
    """A bundle slot must target its specific subcategory in the DB.

    DJ and Dhol are both `music_entertainment` vendors but different
    subcategories — the chatbot must keep them distinct so a user can book a
    dhol player specifically. Same for Makeup vs Mehndi within `beauty`.
    """

    def _seed(self):
        import uuid
        from app.db.models import User, Vendor, Service
        db = TestingSessionLocal()
        uid = str(uuid.uuid4())[:8]

        def _vendor(sub, label, price):
            u = User(
                email=f"sub_{sub}_{uid}@test.com", username=f"sub_{sub}_{uid}",
                password="pw", phone="1", f_name=label, l_name="V",
                age=30, location="NJ", gender="F", language="EN", token_version=0,
            )
            db.add(u); db.commit(); db.refresh(u)
            cat = "beauty" if sub in ("bridal_makeup", "mehndi_artist") else "music_entertainment"
            v = Vendor(user_id=u.user_id, bio=f"{label} vendor", category=cat,
                       subcategory=sub, rating=4.8, num_events=20)
            db.add(v); db.commit(); db.refresh(v)
            # Matching is service-first: the slot filters on Service.category /
            # subcategory, so the service (not just the vendor) must carry them.
            s = Service(name=label, price=price, duration_minutes=120,
                        vendor_id=v.vendor_id, experience="exp",
                        category=cat, subcategory=sub)
            db.add(s); db.commit()
            return v.vendor_id

        ids = {
            "dj": _vendor("dj", "DJ Vendor", 1000.0),
            "dhol": _vendor("dhol", "Dhol Vendor", 800.0),
            "mehndi": _vendor("mehndi_artist", "Mehndi Vendor", 500.0),
        }
        db.close()
        return ids

    def _chosen_vendor(self, slot: str):
        from app.db.models import Vendor
        self._seed()
        db = TestingSessionLocal()
        state = ChatbotState(needed_categories=[slot], budget_tier=BudgetTier.MID_RANGE)
        bundle = generate_bundle(state, db)
        assert len(bundle.items) == 1
        vendor_id = bundle.items[0].vendor_id
        vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
        db.close()
        return bundle.items[0], vendor

    def test_dhol_slot_targets_music_entertainment_dhol(self):
        item, vendor = self._chosen_vendor("dhol")
        assert item.category == "dhol"
        # Must resolve to a dhol vendor, never a DJ — both are music_entertainment
        assert vendor is not None
        assert vendor.category == "music_entertainment"
        assert vendor.subcategory == "dhol"

    def test_mehndi_slot_targets_beauty_mehndi_artist(self):
        item, vendor = self._chosen_vendor("mehndi")
        assert vendor is not None
        assert vendor.category == "beauty"
        assert vendor.subcategory == "mehndi_artist"


# ═══════════════════════════════════════════════════════════════════════
# FULL FLOW INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestFullFlowNothingBooked:
    """Walk through the complete flow when user has nothing booked yet."""

    async def test_complete_flow(self, db):
        # Step 0: Start
        resp = get_initial_step()
        assert resp.next_step == ChatStep.EVENT_DETAILS

        # Step 0 → 0b: Event details
        resp = await process_step(ChatStep.EVENT_DETAILS, "August 2026, NJ, 200 people", [], resp.state, db=db)
        assert resp.next_step == ChatStep.EVENT_TIME

        # Step 0b → 1: Event time
        resp = await process_step(ChatStep.EVENT_TIME, None, ["evening"], resp.state, db=db)
        assert resp.next_step == ChatStep.ALREADY_BOOKED

        # Step 1 → 2: Nothing booked → choose what's needed
        resp = await process_step(ChatStep.ALREADY_BOOKED, None, ["nothing_yet"], resp.state, db=db)
        assert resp.next_step == ChatStep.STILL_NEED
        assert resp.state.booked_categories == []

        # Step 2 → 3: Recommend everything still needed
        resp = await process_step(ChatStep.STILL_NEED, None, ["recommend_all"], resp.state, db=db)
        assert resp.next_step == ChatStep.BUDGET
        assert len(resp.state.needed_categories) == 10

        # Step 3 → 4: Budget
        resp = await process_step(ChatStep.BUDGET, None, ["mid-range"], resp.state, db=db)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES

        # Step 4 → 5/6: Style → bundle
        resp = await process_step(ChatStep.STYLE_PREFERENCES, None, ["elegant"], resp.state, db=db)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert resp.bundle is not None
        assert len(resp.bundle.items) == 10

        # Step 6 → 7: Keep bundle
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["keep"], resp.state, db=db)
        assert resp.next_step == ChatStep.RESULTS_BOOKING

        # Step 7: Book all
        resp = await process_step(ChatStep.RESULTS_BOOKING, None, ["book_all"], resp.state, db=db)
        assert "book" in resp.bot_message.lower() or "Proceeding" in resp.bot_message


@pytest.mark.asyncio
class TestFullFlowWithBooked:
    """Walk through the flow when user has some categories already booked."""

    async def test_complete_flow(self, db):
        resp = get_initial_step()
        resp = await process_step(ChatStep.EVENT_DETAILS, "Wedding in NJ", [], resp.state, db=db)
        assert resp.next_step == ChatStep.EVENT_TIME
        resp = await process_step(ChatStep.EVENT_TIME, None, ["full_day"], resp.state, db=db)
        assert resp.next_step == ChatStep.ALREADY_BOOKED

        # Has venue and photographer
        resp = await process_step(ChatStep.ALREADY_BOOKED, None, ["venue", "photography"], resp.state, db=db)
        assert resp.next_step == ChatStep.STILL_NEED

        # Recommend all remaining
        resp = await process_step(ChatStep.STILL_NEED, None, ["recommend_all"], resp.state, db=db)
        assert resp.next_step == ChatStep.BUDGET
        assert "venue" not in resp.state.needed_categories
        assert "photography" not in resp.state.needed_categories

        # Premium budget
        resp = await process_step(ChatStep.BUDGET, None, ["premium"], resp.state, db=db)
        resp = await process_step(ChatStep.STYLE_PREFERENCES, None, ["traditional"], resp.state, db=db)
        assert resp.bundle is not None
        assert len(resp.bundle.items) == 8  # 10 - venue - photography

        # Swap DJ
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["swap"], resp.state, db=db)
        resp = await process_step(ChatStep.SWAP_VENDOR, None, ["dj"], resp.state, db=db)
        assert resp.next_step == ChatStep.BUNDLE_ACTION

        # Save for later
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["keep"], resp.state, db=db)
        resp = await process_step(ChatStep.RESULTS_BOOKING, None, ["save"], resp.state, db=db)
        assert resp.next_step == ChatStep.RESULTS_BOOKING


@pytest.mark.asyncio
class TestCustomBudgetFlow:
    """Test the custom budget sub-flow."""

    async def test_custom_budget_path(self):
        resp = get_initial_step()
        resp = await process_step(ChatStep.EVENT_DETAILS, "Party in NYC", [], resp.state)
        resp = await process_step(ChatStep.EVENT_TIME, None, ["not_sure"], resp.state)
        resp = await process_step(ChatStep.ALREADY_BOOKED, None, ["nothing_yet"], resp.state)

        # Custom budget
        resp = await process_step(ChatStep.BUDGET, None, ["custom"], resp.state)
        assert resp.next_step == ChatStep.CUSTOM_BUDGET

        resp = await process_step(ChatStep.CUSTOM_BUDGET, "6000", [], resp.state)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES
        assert resp.state.budget_amount == "6000"


# ═══════════════════════════════════════════════════════════════════════
# OFF-SCRIPT DETECTION TESTS
# ═══════════════════════════════════════════════════════════════════════


class TestOffScriptDetection:
    """Test the is_off_script heuristic in llm_service."""

    def test_button_click_is_on_script(self):
        assert is_off_script(ChatStep.BUDGET, None, ["mid-range"]) is False

    def test_free_text_on_button_step_is_off_script(self):
        # Budget step expects buttons, not free text
        assert is_off_script(ChatStep.BUDGET, "What's the best venue?", []) is True

    def test_free_text_on_free_text_step_is_on_script(self):
        # Event details step accepts free text
        assert is_off_script(ChatStep.EVENT_DETAILS, "Wedding in NJ", []) is False

    def test_empty_input_is_on_script(self):
        assert is_off_script(ChatStep.BUDGET, "", []) is False
        assert is_off_script(ChatStep.BUDGET, None, []) is False

    def test_custom_budget_free_text_is_on_script(self):
        assert is_off_script(ChatStep.CUSTOM_BUDGET, "5000", []) is False

    def test_style_free_text_is_on_script(self):
        assert is_off_script(ChatStep.STYLE_PREFERENCES, "elegant and classy", []) is False

    def test_bundle_action_free_text_is_off_script(self):
        assert is_off_script(ChatStep.BUNDLE_ACTION, "Can you find me a florist?", []) is True

    def test_results_booking_free_text_is_off_script(self):
        assert is_off_script(ChatStep.RESULTS_BOOKING, "How do I pay?", []) is True


# ═══════════════════════════════════════════════════════════════════════
# LLM FALLBACK TESTS (mocked — no real API calls)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestLLMFallback:
    """Test that off-script inputs are routed to the (mocked) LLM."""

    @patch("app.services.chatbot_service.get_llm_response", new_callable=AsyncMock)
    async def test_off_script_gets_llm_response(self, mock_llm):
        """Free text on a button-only step should trigger LLM fallback."""
        mock_llm.return_value = LLMResult(
            bot_message="Great question! A DJ plays music while dhol players perform live drums for the baraat.",
            extracted_intent="ask_question",
            suggested_step=None,
        )
        state = ChatbotState()
        resp = await process_step(
            ChatStep.BUDGET,
            "What's the difference between a DJ and dhol?",
            [],
            state,
        )
        assert resp.llm_response is True
        assert resp.next_step == ChatStep.RESULTS_BOOKING or "dhol" in resp.bot_message
        # Should stay on the same step (budget)
        assert resp.next_step == ChatStep.BUDGET
        mock_llm.assert_called_once()

    @patch("app.services.chatbot_service.get_llm_response", new_callable=AsyncMock)
    async def test_llm_intent_jumps_to_budget(self, mock_llm):
        """LLM extracts a budget intent and jumps to style step."""
        mock_llm.return_value = LLMResult(
            bot_message="Got it, I'll set your budget to around $5,000.",
            extracted_intent="set_budget",
            suggested_step="style_preferences",
            extracted_values={"budget_tier": "mid-range", "budget_amount": "5000"},
        )
        state = ChatbotState(needed_categories=["dj", "catering"])
        resp = await process_step(
            ChatStep.BUDGET,
            "I think around five thousand dollars",
            [],
            state,
        )
        assert resp.llm_response is True
        assert resp.next_step == ChatStep.STYLE_PREFERENCES
        assert resp.state.budget_amount == "5000"

    @patch("app.services.chatbot_service.get_llm_response", new_callable=AsyncMock)
    async def test_llm_intent_sets_event_details(self, mock_llm):
        """LLM extracts event details and advances to already_booked."""
        mock_llm.return_value = LLMResult(
            bot_message="Sounds lovely! A wedding in New Jersey with 200 guests in August 2026.",
            extracted_intent="set_event_details",
            extracted_values={
                "location": "New Jersey",
                "guest_count": 200,
                "event_date": "August 2026",
            },
        )
        state = ChatbotState()
        # Simulate off-script on a non-free-text step (though event_details IS free-text,
        # we test the intent mapping directly)
        resp = await process_step(
            ChatStep.ALREADY_BOOKED,
            "Actually I forgot to say — it's a wedding in NJ, 200 guests, August 2026",
            [],
            state,
        )
        assert resp.llm_response is True
        assert resp.state.location == "New Jersey"
        assert resp.state.guest_count == 200
        assert resp.state.event_date == "August 2026"

    @patch("app.services.chatbot_service.get_llm_response", new_callable=AsyncMock)
    async def test_on_script_does_not_call_llm(self, mock_llm):
        """Button clicks should NOT trigger the LLM."""
        state = ChatbotState()
        resp = await process_step(ChatStep.BUDGET, None, ["mid-range"], state)
        assert resp.llm_response is False
        mock_llm.assert_not_called()

    @patch("app.services.chatbot_service.get_llm_response", new_callable=AsyncMock)
    async def test_conversation_history_is_updated(self, mock_llm):
        """Off-script messages should be appended to conversation_history."""
        mock_llm.return_value = LLMResult(
            bot_message="That's a great question about venues!",
            extracted_intent="ask_question",
        )
        state = ChatbotState()
        assert len(state.conversation_history) == 0

        resp = await process_step(
            ChatStep.BUNDLE_ACTION,
            "Tell me about popular venues",
            [],
            state,
        )
        # Should have at least user + assistant messages
        assert len(resp.state.conversation_history) >= 2

    @patch("app.services.chatbot_service.get_llm_response", new_callable=AsyncMock)
    async def test_llm_modify_bundle_swap(self, mock_llm, db):
        """LLM can trigger a bundle modification (swap)."""
        mock_llm.return_value = LLMResult(
            bot_message="Sure, let me swap out the DJ for you!",
            extracted_intent="modify_bundle",
            extracted_values={"action": "swap", "category": "dj"},
        )
        state = ChatbotState(
            needed_categories=["dj", "catering"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state, db)

        resp = await process_step(
            ChatStep.BUNDLE_ACTION,
            "Can you swap the DJ for someone else?",
            [],
            state,
            db=db,
        )
        assert resp.llm_response is True
        assert resp.next_step == ChatStep.SWAP_VENDOR


# ═══════════════════════════════════════════════════════════════════════
# HTTP ENDPOINT TESTS (via TestClient)
# ═══════════════════════════════════════════════════════════════════════


def _step_auth_headers():
    """Create a test user and return auth headers for chatbot step tests."""
    import uuid
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]
    user = User(
        email=f"chatbot_step_{uid}@test.com",
        username=f"chatbot_step_{uid}",
        password="pw", phone="1", f_name="A", l_name="B",
        age=25, location="NJ", gender="M", language="EN", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    headers = make_auth_headers(user)
    db.close()
    return headers


class TestChatbotEndpoints:
    def test_start_endpoint(self):
        response = client.post("/chatbot/start")
        assert response.status_code == 200
        data = response.json()
        assert data["next_step"] == "event_details"
        assert data["bot_message"]
        assert len(data["helper_buttons"]) > 0
        assert data["state"]["booked_categories"] == []

    def test_step_endpoint_event_details(self):
        headers = _step_auth_headers()
        start_resp = client.post("/chatbot/start")
        state = start_resp.json()["state"]

        response = client.post("/chatbot/step", headers=headers, json={
            "current_step": "event_details",
            "user_input": "Summer 2026, 150 guests in NJ",
            "selected_values": [],
            "state": state,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["next_step"] == "event_time"

    def test_step_endpoint_full_flow(self):
        headers = _step_auth_headers()

        # Start
        resp = client.post("/chatbot/start").json()
        state = resp["state"]

        # Event details
        resp = client.post("/chatbot/step", headers=headers, json={
            "current_step": "event_details",
            "user_input": "August 2026",
            "state": state,
        }).json()
        state = resp["state"]

        # Event time
        resp = client.post("/chatbot/step", headers=headers, json={
            "current_step": "event_time",
            "selected_values": ["evening"],
            "state": state,
        }).json()
        state = resp["state"]

        # Nothing booked → picks needs on the next step
        resp = client.post("/chatbot/step", headers=headers, json={
            "current_step": "already_booked",
            "selected_values": ["nothing_yet"],
            "state": state,
        }).json()
        assert resp["next_step"] == "still_need"
        state = resp["state"]

        # Recommend everything still needed
        resp = client.post("/chatbot/step", headers=headers, json={
            "current_step": "still_need",
            "selected_values": ["recommend_all"],
            "state": state,
        }).json()
        assert resp["next_step"] == "budget"
        state = resp["state"]

        # Mid-range budget
        resp = client.post("/chatbot/step", headers=headers, json={
            "current_step": "budget",
            "selected_values": ["mid-range"],
            "state": state,
        }).json()
        assert resp["next_step"] == "style_preferences"
        state = resp["state"]

        # Style
        resp = client.post("/chatbot/step", headers=headers, json={
            "current_step": "style_preferences",
            "selected_values": ["elegant"],
            "state": state,
        }).json()
        assert resp["next_step"] == "bundle_action"
        assert resp["bundle"] is not None
        assert len(resp["bundle"]["items"]) == 10

    def test_step_endpoint_invalid_step(self):
        """Sending an unknown step value should return 422."""
        headers = _step_auth_headers()
        response = client.post("/chatbot/step", headers=headers, json={
            "current_step": "non_existent_step",
            "state": {},
        })
        assert response.status_code == 422

    def test_llm_response_field_present(self):
        """Verify the llm_response field is in the response schema."""
        response = client.post("/chatbot/start")
        data = response.json()
        assert "llm_response" in data
        assert data["llm_response"] is False


# ═══════════════════════════════════════════════════════════════════════
# VENUE DISTANCE
# ═══════════════════════════════════════════════════════════════════════


class TestVenueDistance:
    """A venue is where the event happens, so it qualifies on where the building
    stands — not on its owner's home address or their travel radius.

    The regression these cover: the slot filter used to judge every category by
    the vendor's User coords + travel_radius_miles, so a venue three states away
    was offered whenever its owner happened to live near the event (or had
    open_to_long_distance set, which waived the check entirely).
    """

    # Event anchor and two venues: one across town, one ~250 miles away. Both
    # owned by vendors living right next to the event, so the *old* filter kept
    # both — that's what makes this a regression test rather than a tautology.
    EVENT_LAT, EVENT_LNG = 40.7128, -74.0060      # New York, NY
    NEAR_LAT, NEAR_LNG = 40.7357, -74.1724        # Newark, NJ — ~10 mi
    FAR_LAT, FAR_LNG = 42.3601, -71.0589          # Boston, MA — ~190 mi

    @pytest.fixture
    def venues(self, db):
        """Two venue services, near and far, owned by locally-based vendors."""
        import uuid
        from app.db.models import User, Vendor, Service

        made = []
        for tag, vlat, vlng, long_distance in [
            ("near", self.NEAR_LAT, self.NEAR_LNG, False),
            ("far", self.FAR_LAT, self.FAR_LNG, True),
        ]:
            uid = uuid.uuid4().hex[:8]
            u = User(
                email=f"venuedist_{tag}_{uid}@test.com",
                username=f"venuedist_{tag}_{uid}",
                password="pw", phone="1", f_name="Venue", l_name=tag,
                age=30, location="NY", gender="F", language="EN", token_version=0,
                # Owner sits next to the event either way, so only the venue's
                # own coordinates can tell the two apart.
                latitude=self.EVENT_LAT, longitude=self.EVENT_LNG,
            )
            db.add(u); db.commit(); db.refresh(u)
            v = Vendor(
                user_id=u.user_id, bio=f"venue {tag}", category="venue",
                subcategory=None, rating=4.8, num_events=10,
                travel_radius_miles=30, open_to_long_distance=long_distance,
            )
            db.add(v); db.commit(); db.refresh(v)
            s = Service(
                name=f"venuedist-{tag}-{uid}", price=1000.0, vendor_id=v.vendor_id,
                experience="exp", category="venue", subcategory=None,
                negotiable=False, location=f"{tag} hall",
                venue_latitude=vlat, venue_longitude=vlng,
            )
            db.add(s); db.commit(); db.refresh(s)
            made.append(s)

        yield made

        for s in made:
            db.delete(s)
        db.commit()

    def _venue_names(self, db, state):
        from app.services.chatbot_service import _candidate_service_rows
        rows = _candidate_service_rows(
            "venue", state, db,
            booked_vendor_ids=set(), price_cap=float("inf"), used_vendor_ids=set(),
        )
        return {s.name for s, _v, _u in rows}

    def test_far_venue_is_excluded(self, db, venues):
        near, far = venues
        state = ChatbotState(latitude=self.EVENT_LAT, longitude=self.EVENT_LNG)
        names = self._venue_names(db, state)
        assert near.name in names
        # Excluded despite open_to_long_distance — a building does not travel.
        assert far.name not in names

    def test_no_event_coords_filters_nothing(self, db, venues):
        """A free-typed city gives no coordinates, so there is nothing to measure
        against — the filter stays out of the way rather than emptying the slot."""
        near, far = venues
        names = self._venue_names(db, ChatbotState())
        assert {near.name, far.name} <= names

    def test_venue_without_coordinates_is_excluded(self, db):
        """Unknown distance is exactly what this filter exists to exclude."""
        import uuid
        from app.db.models import User, Vendor, Service

        uid = uuid.uuid4().hex[:8]
        u = User(
            email=f"venuedist_nocoord_{uid}@test.com",
            username=f"venuedist_nocoord_{uid}",
            password="pw", phone="1", f_name="Venue", l_name="nocoord",
            age=30, location="NY", gender="F", language="EN", token_version=0,
            latitude=self.EVENT_LAT, longitude=self.EVENT_LNG,
        )
        db.add(u); db.commit(); db.refresh(u)
        v = Vendor(user_id=u.user_id, bio="venue nocoord", category="venue",
                   subcategory=None, rating=4.8, num_events=10)
        db.add(v); db.commit(); db.refresh(v)
        s = Service(name=f"venuedist-nocoord-{uid}", price=1000.0,
                    vendor_id=v.vendor_id, experience="exp", category="venue",
                    subcategory=None, negotiable=False)
        db.add(s); db.commit(); db.refresh(s)

        try:
            state = ChatbotState(latitude=self.EVENT_LAT, longitude=self.EVENT_LNG)
            assert s.name not in self._venue_names(db, state)
        finally:
            db.delete(s); db.commit()

    def test_traveling_vendor_still_uses_travel_radius(self, db):
        """The non-venue path is untouched: a DJ based far away but open to long
        distance is still offered, which is the rule a venue must not inherit."""
        import uuid
        from app.db.models import User, Vendor, Service

        uid = uuid.uuid4().hex[:8]
        u = User(
            email=f"djdist_{uid}@test.com", username=f"djdist_{uid}",
            password="pw", phone="1", f_name="DJ", l_name="far",
            age=30, location="MA", gender="F", language="EN", token_version=0,
            latitude=self.FAR_LAT, longitude=self.FAR_LNG,
        )
        db.add(u); db.commit(); db.refresh(u)
        v = Vendor(user_id=u.user_id, bio="dj far", category="music_entertainment",
                   subcategory="dj", rating=4.8, num_events=10,
                   travel_radius_miles=30, open_to_long_distance=True)
        db.add(v); db.commit(); db.refresh(v)
        s = Service(name=f"djdist-{uid}", price=1000.0, vendor_id=v.vendor_id,
                    experience="exp", category="music_entertainment",
                    subcategory="dj", negotiable=False)
        db.add(s); db.commit(); db.refresh(s)

        try:
            from app.services.chatbot_service import _candidate_service_rows
            state = ChatbotState(latitude=self.EVENT_LAT, longitude=self.EVENT_LNG)
            rows = _candidate_service_rows(
                "dj", state, db,
                booked_vendor_ids=set(), price_cap=float("inf"), used_vendor_ids=set(),
            )
            assert s.name in {r[0].name for r in rows}
        finally:
            db.delete(s); db.commit()


# ── Rate-priced services in a bundle ──────────────────────────────────
#
# A bundle item is a service the client is about to book, so its price has to be
# the one the booking will carry. Every slot used to be priced at service.price
# raw — for a per-person caterer that is a per-head rate, so a $62 listing
# contributed $62 to a total sitting beside an $8,500 venue, and the "estimated
# total" a client chose a bundle on was a sum of incomparable numbers.


class TestRatePricedBundleItems:
    """Per-person / per-hour services must price as totals, not as rates."""

    CAT = "catering"

    @pytest.fixture
    def per_head_service(self, db):
        """A caterer at $62 a head, alone in its category so it must be picked."""
        import uuid
        uid = uuid.uuid4().hex[:8]
        u = User(
            email=f"perhead_{uid}@test.com", username=f"perhead_{uid}",
            password="pw", phone="1", f_name="Per", l_name="Head",
            age=30, location="NJ", gender="F", language="EN", token_version=0,
        )
        db.add(u); db.commit(); db.refresh(u)
        v = Vendor(user_id=u.user_id, bio="per head", category="catering",
                   rating=5.0, num_events=99)
        db.add(v); db.commit(); db.refresh(v)
        s = Service(name=f"perhead-{uid}", price=62.0, price_unit="person",
                    vendor_id=v.vendor_id, experience="exp",
                    category="catering", negotiable=False)
        db.add(s); db.commit(); db.refresh(s)
        yield s
        db.delete(s); db.commit()
        db.delete(v); db.commit()
        db.delete(u); db.commit()

    def _item(self, bundle, service_name):
        return next((i for i in bundle.items if i.service_name == service_name), None)

    def test_per_person_resolves_to_a_total(self, db, per_head_service):
        """$62/head x 200 guests is $12,400 — not $62."""
        from app.services.chatbot_service import generate_bundle

        state = ChatbotState(
            needed_categories=[self.CAT],
            budget_tier=BudgetTier.PREMIUM,   # inf cap, so nothing is filtered out
            guest_count=200,
        )
        bundle = generate_bundle(state, db)
        item = self._item(bundle, per_head_service.name)
        assert item is not None, "the only caterer in its category should be picked"
        assert item.price_min == 12400.0
        assert item.price_unit == "person"
        assert item.price_pending_quantity is False
        assert bundle.estimated_total_min == 12400.0
        assert bundle.pending_quantity_count == 0

    def test_without_a_guest_count_it_stays_a_rate_and_says_so(self, db, per_head_service):
        """No headcount: the rate is all there is, and it must be flagged."""
        from app.services.chatbot_service import generate_bundle

        state = ChatbotState(
            needed_categories=[self.CAT],
            budget_tier=BudgetTier.PREMIUM,
        )
        bundle = generate_bundle(state, db)
        item = self._item(bundle, per_head_service.name)
        assert item is not None
        assert item.price_min == 62.0            # the rate, unresolved
        assert item.price_pending_quantity is True
        # The bundle total therefore isn't the price of the bundle, and says so.
        assert bundle.pending_quantity_count == 1

    def test_flat_priced_items_are_never_pending(self, db):
        """A flat rate IS the total — it must not be flagged as unresolved."""
        from app.services.chatbot_service import generate_bundle

        state = ChatbotState(
            needed_categories=["dj"],
            budget_tier=BudgetTier.PREMIUM,
            guest_count=200,
        )
        bundle = generate_bundle(state, db)
        assert bundle.items, "seeded DJs should fill the slot"
        for item in bundle.items:
            assert item.price_pending_quantity is False
            assert item.price_unit is None       # seeds carry no price_unit
        assert bundle.pending_quantity_count == 0

    def test_budget_cap_measures_the_total_not_the_rate(self, db, per_head_service):
        """A $62 rate that costs $12,400 must not clear a $1,500 cap.

        This is the selection half of the same bug: comparing a rate to a cap let
        per-person services through every budget tier untouched, so a
        "budget-friendly" bundle could be the most expensive of the three.
        """
        from app.services.chatbot_service import _candidate_service_rows

        state = ChatbotState(needed_categories=[self.CAT], guest_count=200)
        rows = _candidate_service_rows(
            self.CAT, state, db,
            booked_vendor_ids=set(), price_cap=1500.0, used_vendor_ids=set(),
        )
        names = {r[0].name for r in rows}
        # The cap keeps everything only when nothing qualifies; the seeded
        # flat-rate caterers are under $1,500, so this one should be dropped.
        assert per_head_service.name not in names, (
            "a $12,400 caterer cleared a $1,500 cap because only its rate was compared"
        )

    def test_the_estimate_equals_what_the_booking_will_be_charged(self, db, per_head_service):
        """The preview and the persisted booking must agree.

        They are computed by different code — _price_for here, estimate_amount_cents
        in _create_bundle_from_chatbot — so this pins them together. A total shown
        before booking that isn't the total booked is the whole complaint.
        """
        from app.services.booking_service import estimate_amount_cents
        from app.services.chatbot_service import event_dates, generate_bundle

        state = ChatbotState(
            needed_categories=[self.CAT],
            budget_tier=BudgetTier.PREMIUM,
            guest_count=175,
            event_date="2027-06-05",
        )
        bundle = generate_bundle(state, db)
        item = self._item(bundle, per_head_service.name)
        assert item is not None

        date_iso, date_end = event_dates(state)
        booked_cents = estimate_amount_cents(
            per_head_service,
            guest_count=state.guest_count,
            date_iso=date_iso,
            date_end=date_end,
            time_start=state.time_start,
            time_end=state.time_end,
        )
        assert booked_cents is not None
        assert item.price_min == round(booked_cents / 100, 2)


# ── What a date range means ───────────────────────────────────────────
#
# "Sometime in October" is a window the date falls inside. It was being persisted
# as date_iso -> date_end, which is the pair meaning "first and last day of the
# engagement" everywhere else in the app — so an unsettled fortnight became a
# fortnight-long booking: a per-day vendor billed fourteen times, escrow locked
# until the last day, fourteen rows of run sheet. A celebration that really does
# run several days says so with event_date + event_date_end.


class TestEventDateSemantics:
    """date_range is a window; event_date_end is a duration. Never the reverse."""

    def test_a_window_takes_its_first_day_and_drops_the_width(self):
        from app.services.chatbot_service import event_dates

        state = ChatbotState(
            date_range=DateRange(start="2027-10-01", end="2027-10-15"),
        )
        assert event_dates(state) == ("2027-10-01", None)

    def test_a_settled_span_keeps_both_ends(self):
        from app.services.chatbot_service import event_dates

        state = ChatbotState(event_date="2027-06-05", event_date_end="2027-06-07")
        assert event_dates(state) == ("2027-06-05", "2027-06-07")

    def test_a_settled_date_beats_a_window(self):
        """Both supplied: the one the client actually chose wins."""
        from app.services.chatbot_service import event_dates

        state = ChatbotState(
            event_date="2027-06-05",
            date_range=DateRange(start="2027-10-01", end="2027-10-15"),
        )
        assert event_dates(state) == ("2027-06-05", None)

    def test_no_date_at_all_is_no_date(self):
        from app.services.chatbot_service import event_dates

        assert event_dates(ChatbotState()) == (None, None)

    def test_a_window_does_not_bill_a_per_day_service_for_its_width(self, db):
        """The bug, priced. $500/day over a 15-day window is $500, not $7,500."""
        from app.services.chatbot_service import _price_for

        class S:
            name, price, price_unit = "Marquee hire", 500.0, "day"

        window = ChatbotState(date_range=DateRange(start="2027-10-01", end="2027-10-15"))
        assert _price_for(S(), window) == (500.0, False)

        # A real three-day celebration still bills three days.
        span = ChatbotState(event_date="2027-06-05", event_date_end="2027-06-07")
        assert _price_for(S(), span) == (1500.0, False)

    def test_a_window_excludes_no_vendor_on_availability(self, db):
        """A window is not evidence of a conflict with any particular day.

        The overlap test dropped every vendor with a single booking anywhere
        inside the window — for "sometime in October", most of the good ones, on
        the strength of a date the client hadn't picked.
        """
        from app.services.chatbot_service import _get_booked_vendor_ids

        state = ChatbotState(date_range=DateRange(start="2027-10-01", end="2027-10-31"))
        assert _get_booked_vendor_ids(state, db) == set()

    def test_a_settled_span_still_checks_availability(self, db):
        """Narrowing must still happen once there is a real date to narrow on."""
        import uuid
        from app.db.models import Booking
        from app.services.chatbot_service import _get_booked_vendor_ids

        uid = uuid.uuid4().hex[:8]
        u = User(
            email=f"busy_{uid}@test.com", username=f"busy_{uid}", password="pw",
            phone="1", f_name="Busy", l_name="Vendor", age=30, location="NJ",
            gender="F", language="EN", token_version=0,
        )
        db.add(u); db.commit(); db.refresh(u)
        v = Vendor(user_id=u.user_id, bio="busy", category="catering",
                   rating=4.0, num_events=1)
        db.add(v); db.commit(); db.refresh(v)
        s = Service(name=f"busy-{uid}", price=100.0, vendor_id=v.vendor_id,
                    experience="exp", category="catering", negotiable=False)
        db.add(s); db.commit(); db.refresh(s)
        b = Booking(user_id=u.user_id, vendor_id=v.vendor_id, service_id=s.service_id,
                    date_iso="2027-06-06", time_start="10:00", time_end="12:00",
                    location="NJ", status="approved")
        db.add(b); db.commit()

        try:
            # The busy day sits inside the celebration's span.
            state = ChatbotState(event_date="2027-06-05", event_date_end="2027-06-07")
            assert v.vendor_id in _get_booked_vendor_ids(state, db)
            # ... and outside a different one.
            clear = ChatbotState(event_date="2027-09-05")
            assert v.vendor_id not in _get_booked_vendor_ids(clear, db)
        finally:
            db.delete(b); db.commit()
            db.delete(s); db.commit()
            db.delete(v); db.commit()
            db.delete(u); db.commit()

    def test_the_persisted_booking_matches_what_was_priced(self, db):
        """_create_bundle_from_chatbot must date bookings the way event_dates says."""
        from app.db.models import Booking, Bundle
        from app.services.chatbot_service import (
            _create_bundle_from_chatbot,
            event_dates,
            generate_bundle,
        )

        state = ChatbotState(
            needed_categories=["dj"],
            budget_tier=BudgetTier.MID_RANGE,
            location="Newark, NJ",
            date_range=DateRange(start="2027-10-01", end="2027-10-15"),
        )
        state.bundle = generate_bundle(state, db)
        assert state.bundle.items, "seeded DJs should fill the slot"

        bundle_id, booking_ids = _create_bundle_from_chatbot(
            state, user_id="test-user-dates", categories=None, db=db,
            notify_vendors=False,
        )
        try:
            expected_iso, expected_end = event_dates(state)
            for bid in booking_ids:
                bk = db.query(Booking).filter(Booking.booking_id == bid).first()
                assert bk.date_iso == expected_iso == "2027-10-01"
                assert bk.date_end is expected_end is None, (
                    "the window's width must not become the booking's duration"
                )
        finally:
            db.query(Booking).filter(Booking.bundle_id == bundle_id).delete()
            db.query(Bundle).filter(Bundle.bundle_id == bundle_id).delete()
            db.commit()
