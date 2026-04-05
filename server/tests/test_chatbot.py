"""Tests for the chatbot bundle-builder API.

Covers:
  - Service-layer unit tests (step transitions, bundle generation)
  - Router integration tests via FastAPI TestClient
"""

import os
import pytest

# Ensure DATABASE_URL is set before importing main (which triggers database.py)
from dotenv import load_dotenv
load_dotenv()
if "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = "sqlite:///./test_chatbot.db"

from fastapi.testclient import TestClient

from main import app
from app.models.chatbot_schemas import (
    BudgetTier,
    Bundle,
    ChatbotState,
    ChatStep,
    StepRequest,
    VENDOR_CATEGORIES,
)
from app.services.chatbot_service import (
    generate_bundle,
    get_initial_step,
    process_step,
)

client = TestClient(app)


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


class TestEventDetailsStep:
    def test_advances_to_already_booked(self):
        state = ChatbotState()
        resp = process_step(ChatStep.EVENT_DETAILS, "August 2026, NJ, 300 guests", [], state)
        assert resp.next_step == ChatStep.ALREADY_BOOKED

    def test_extracts_guest_count(self):
        state = ChatbotState()
        resp = process_step(ChatStep.EVENT_DETAILS, "around 300 guests in New Jersey", [], state)
        assert resp.state.guest_count == 300

    def test_extracts_date(self):
        state = ChatbotState()
        resp = process_step(ChatStep.EVENT_DETAILS, "August 2026, NJ", [], state)
        assert resp.state.event_date is not None
        assert "august" in resp.state.event_date.lower()

    def test_no_date_button(self):
        state = ChatbotState()
        resp = process_step(ChatStep.EVENT_DETAILS, None, ["no_date"], state)
        assert resp.state.event_date == "TBD"
        assert resp.next_step == ChatStep.ALREADY_BOOKED

    def test_guest_count_not_confused_with_year(self):
        state = ChatbotState()
        resp = process_step(ChatStep.EVENT_DETAILS, "August 2026, New Jersey", [], state)
        # Should NOT set guest_count to 2026
        assert resp.state.guest_count is None or resp.state.guest_count != 2026


class TestAlreadyBookedStep:
    def test_nothing_yet_skips_to_budget(self):
        state = ChatbotState()
        resp = process_step(ChatStep.ALREADY_BOOKED, None, ["nothing_yet"], state)
        assert resp.next_step == ChatStep.BUDGET
        assert resp.state.booked_categories == []
        assert resp.state.needed_categories == list(VENDOR_CATEGORIES)

    def test_categories_selected_goes_to_still_need(self):
        state = ChatbotState()
        resp = process_step(ChatStep.ALREADY_BOOKED, None, ["venue", "photographer"], state)
        assert resp.next_step == ChatStep.STILL_NEED
        assert resp.state.booked_categories == ["venue", "photographer"]

    def test_single_category(self):
        state = ChatbotState()
        resp = process_step(ChatStep.ALREADY_BOOKED, None, ["dj"], state)
        assert resp.next_step == ChatStep.STILL_NEED
        assert resp.state.booked_categories == ["dj"]


class TestStillNeedStep:
    def test_recommend_all(self):
        state = ChatbotState(booked_categories=["venue", "photographer"])
        resp = process_step(ChatStep.STILL_NEED, None, ["recommend_all"], state)
        assert resp.next_step == ChatStep.BUDGET
        assert "venue" not in resp.state.needed_categories
        assert "photographer" not in resp.state.needed_categories
        assert "catering" in resp.state.needed_categories
        assert "dj" in resp.state.needed_categories

    def test_specific_categories(self):
        state = ChatbotState(booked_categories=["venue"])
        resp = process_step(ChatStep.STILL_NEED, None, ["dj", "mehndi", "dhol"], state)
        assert resp.next_step == ChatStep.BUDGET
        assert sorted(resp.state.needed_categories) == sorted(["dj", "mehndi", "dhol"])

    def test_removes_already_booked_duplicates(self):
        state = ChatbotState(booked_categories=["venue", "dj"])
        resp = process_step(ChatStep.STILL_NEED, None, ["venue", "dj", "mehndi"], state)
        assert "venue" not in resp.state.needed_categories
        assert "dj" not in resp.state.needed_categories
        assert "mehndi" in resp.state.needed_categories

    def test_other_category(self):
        state = ChatbotState(booked_categories=[])
        resp = process_step(ChatStep.STILL_NEED, None, ["catering", "other"], state)
        assert "other" in resp.state.needed_categories


class TestBudgetStep:
    def test_budget_friendly(self):
        state = ChatbotState()
        resp = process_step(ChatStep.BUDGET, None, ["budget-friendly"], state)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES
        assert resp.state.budget_tier == BudgetTier.BUDGET_FRIENDLY

    def test_mid_range(self):
        state = ChatbotState()
        resp = process_step(ChatStep.BUDGET, None, ["mid-range"], state)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES
        assert resp.state.budget_tier == BudgetTier.MID_RANGE

    def test_premium(self):
        state = ChatbotState()
        resp = process_step(ChatStep.BUDGET, None, ["premium"], state)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES

    def test_custom_goes_to_custom_budget(self):
        state = ChatbotState()
        resp = process_step(ChatStep.BUDGET, None, ["custom"], state)
        assert resp.next_step == ChatStep.CUSTOM_BUDGET
        assert resp.state.budget_tier == BudgetTier.CUSTOM

    def test_not_sure(self):
        state = ChatbotState()
        resp = process_step(ChatStep.BUDGET, None, ["unknown"], state)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES
        assert resp.state.budget_tier == BudgetTier.UNKNOWN


class TestCustomBudgetStep:
    def test_amount_advances_to_style(self):
        state = ChatbotState(budget_tier=BudgetTier.CUSTOM)
        resp = process_step(ChatStep.CUSTOM_BUDGET, "8000", [], state)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES
        assert resp.state.budget_amount == "8000"

    def test_button_range(self):
        state = ChatbotState(budget_tier=BudgetTier.CUSTOM)
        resp = process_step(ChatStep.CUSTOM_BUDGET, None, ["3000_7000"], state)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES
        assert resp.state.budget_amount == "3000_7000"


class TestStylePreferencesStep:
    def test_generates_bundle(self):
        state = ChatbotState(
            needed_categories=["dj", "catering", "decor"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        resp = process_step(ChatStep.STYLE_PREFERENCES, None, ["elegant", "pref_local"], state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert resp.bundle is not None
        assert len(resp.bundle.items) == 3
        assert resp.state.style == ["elegant"]
        assert resp.state.preferences == ["pref_local"]

    def test_free_text_style(self):
        state = ChatbotState(
            needed_categories=["dj"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        resp = process_step(ChatStep.STYLE_PREFERENCES, "elegant and classy", [], state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert resp.bundle is not None

    def test_bundle_has_correct_categories(self):
        cats = ["dj", "mehndi", "dhol"]
        state = ChatbotState(needed_categories=cats, budget_tier=BudgetTier.PREMIUM)
        resp = process_step(ChatStep.STYLE_PREFERENCES, None, ["modern"], state)
        bundle_cats = [item.category for item in resp.bundle.items]
        assert sorted(bundle_cats) == sorted(cats)


class TestBundleGeneration:
    def test_basic_bundle(self):
        state = ChatbotState(
            needed_categories=["dj", "catering"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        bundle = generate_bundle(state)
        assert len(bundle.items) == 2
        assert bundle.estimated_total_min > 0
        assert bundle.estimated_total_max > bundle.estimated_total_min

    def test_budget_friendly_cheaper(self):
        state_budget = ChatbotState(
            needed_categories=["dj"],
            budget_tier=BudgetTier.BUDGET_FRIENDLY,
        )
        state_prem = ChatbotState(
            needed_categories=["dj"],
            budget_tier=BudgetTier.PREMIUM,
        )
        budget_bundle = generate_bundle(state_budget)
        premium_bundle = generate_bundle(state_prem)
        assert budget_bundle.estimated_total_max < premium_bundle.estimated_total_max

    def test_all_categories(self):
        state = ChatbotState(
            needed_categories=list(VENDOR_CATEGORIES),
            budget_tier=BudgetTier.MID_RANGE,
        )
        bundle = generate_bundle(state)
        assert len(bundle.items) == len(VENDOR_CATEGORIES)

    def test_empty_categories(self):
        state = ChatbotState(needed_categories=[], budget_tier=BudgetTier.MID_RANGE)
        bundle = generate_bundle(state)
        assert len(bundle.items) == 0
        assert bundle.estimated_total_min == 0

    def test_unknown_category_skipped(self):
        state = ChatbotState(
            needed_categories=["dj", "other"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        bundle = generate_bundle(state)
        assert len(bundle.items) == 1  # "other" has no mock vendors


class TestBundleActionStep:
    def _make_state_with_bundle(self):
        state = ChatbotState(
            needed_categories=list(VENDOR_CATEGORIES),
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state)
        return state

    def test_keep_goes_to_results(self):
        state = self._make_state_with_bundle()
        resp = process_step(ChatStep.BUNDLE_ACTION, None, ["keep"], state)
        assert resp.next_step == ChatStep.RESULTS_BOOKING

    def test_customize_goes_to_manual(self):
        state = self._make_state_with_bundle()
        resp = process_step(ChatStep.BUNDLE_ACTION, None, ["customize"], state)
        assert resp.next_step == ChatStep.MANUAL_CUSTOMIZE

    def test_swap_goes_to_swap(self):
        state = self._make_state_with_bundle()
        resp = process_step(ChatStep.BUNDLE_ACTION, None, ["swap"], state)
        assert resp.next_step == ChatStep.SWAP_VENDOR

    def test_remove_goes_to_remove(self):
        state = self._make_state_with_bundle()
        resp = process_step(ChatStep.BUNDLE_ACTION, None, ["remove"], state)
        assert resp.next_step == ChatStep.REMOVE_CATEGORY

    def test_add_goes_to_add(self):
        state = self._make_state_with_bundle()
        resp = process_step(ChatStep.BUNDLE_ACTION, None, ["add"], state)
        assert resp.next_step == ChatStep.ADD_CATEGORY

    def test_cheaper_regenerates(self):
        state = self._make_state_with_bundle()
        original_max = state.bundle.estimated_total_max
        resp = process_step(ChatStep.BUNDLE_ACTION, None, ["cheaper"], state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert resp.state.budget_tier == BudgetTier.BUDGET_FRIENDLY
        assert resp.bundle.estimated_total_max < original_max

    def test_premium_regenerates(self):
        state = self._make_state_with_bundle()
        resp = process_step(ChatStep.BUNDLE_ACTION, None, ["premium_bundle"], state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert resp.state.budget_tier == BudgetTier.PREMIUM

    def test_start_over(self):
        state = self._make_state_with_bundle()
        resp = process_step(ChatStep.BUNDLE_ACTION, None, ["start_over"], state)
        assert resp.next_step == ChatStep.EVENT_DETAILS


class TestSwapVendor:
    def test_swap_changes_vendor(self):
        state = ChatbotState(
            needed_categories=["dj"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state)
        original_name = state.bundle.items[0].vendor_name

        resp = process_step(ChatStep.SWAP_VENDOR, None, ["dj"], state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        swapped_name = resp.state.bundle.items[0].vendor_name
        assert swapped_name != original_name


class TestRemoveCategory:
    def test_remove_shrinks_bundle(self):
        state = ChatbotState(
            needed_categories=["dj", "mehndi", "dhol"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state)
        assert len(state.bundle.items) == 3

        resp = process_step(ChatStep.REMOVE_CATEGORY, None, ["mehndi"], state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert len(resp.state.bundle.items) == 2
        cats = [i.category for i in resp.state.bundle.items]
        assert "mehndi" not in cats
        assert "Done" in resp.bot_message


class TestAddCategory:
    def test_add_expands_bundle(self):
        state = ChatbotState(
            needed_categories=["dj"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state)
        assert len(state.bundle.items) == 1

        resp = process_step(ChatStep.ADD_CATEGORY, None, ["mehndi"], state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert len(resp.state.bundle.items) == 2
        cats = [i.category for i in resp.state.bundle.items]
        assert "mehndi" in cats
        assert "Added" in resp.bot_message


class TestResultsBookingStep:
    def _make_state(self):
        state = ChatbotState(
            needed_categories=["dj", "catering"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state)
        return state

    def test_book_all(self):
        state = self._make_state()
        resp = process_step(ChatStep.RESULTS_BOOKING, None, ["book_all"], state)
        assert "book" in resp.bot_message.lower() or "Proceeding" in resp.bot_message

    def test_book_some_goes_to_partial(self):
        state = self._make_state()
        resp = process_step(ChatStep.RESULTS_BOOKING, None, ["book_some"], state)
        assert resp.next_step == ChatStep.PARTIAL_BOOKING

    def test_save(self):
        state = self._make_state()
        resp = process_step(ChatStep.RESULTS_BOOKING, None, ["save"], state)
        assert "saved" in resp.bot_message.lower()

    def test_go_back(self):
        state = self._make_state()
        resp = process_step(ChatStep.RESULTS_BOOKING, None, ["go_back"], state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION

    def test_contact(self):
        state = self._make_state()
        resp = process_step(ChatStep.RESULTS_BOOKING, None, ["contact"], state)
        assert "contact" in resp.bot_message.lower()


class TestPartialBooking:
    def test_selects_categories(self):
        state = ChatbotState(
            needed_categories=["dj", "catering"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state)
        resp = process_step(ChatStep.PARTIAL_BOOKING, None, ["dj"], state)
        assert resp.next_step == ChatStep.RESULTS_BOOKING
        assert "DJ" in resp.bot_message


# ═══════════════════════════════════════════════════════════════════════
# FULL FLOW INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════


class TestFullFlowNothingBooked:
    """Walk through the complete flow when user has nothing booked yet."""

    def test_complete_flow(self):
        # Step 0: Start
        resp = get_initial_step()
        assert resp.next_step == ChatStep.EVENT_DETAILS

        # Step 0 → 1: Event details
        resp = process_step(ChatStep.EVENT_DETAILS, "August 2026, NJ, 200 people", [], resp.state)
        assert resp.next_step == ChatStep.ALREADY_BOOKED

        # Step 1 → 3: Nothing booked (skip step 2)
        resp = process_step(ChatStep.ALREADY_BOOKED, None, ["nothing_yet"], resp.state)
        assert resp.next_step == ChatStep.BUDGET
        assert len(resp.state.needed_categories) == 7

        # Step 3 → 4: Budget
        resp = process_step(ChatStep.BUDGET, None, ["mid-range"], resp.state)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES

        # Step 4 → 5/6: Style → bundle
        resp = process_step(ChatStep.STYLE_PREFERENCES, None, ["elegant"], resp.state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert resp.bundle is not None
        assert len(resp.bundle.items) == 7

        # Step 6 → 7: Keep bundle
        resp = process_step(ChatStep.BUNDLE_ACTION, None, ["keep"], resp.state)
        assert resp.next_step == ChatStep.RESULTS_BOOKING

        # Step 7: Book all
        resp = process_step(ChatStep.RESULTS_BOOKING, None, ["book_all"], resp.state)
        assert "book" in resp.bot_message.lower() or "Proceeding" in resp.bot_message


class TestFullFlowWithBooked:
    """Walk through the flow when user has some categories already booked."""

    def test_complete_flow(self):
        resp = get_initial_step()
        resp = process_step(ChatStep.EVENT_DETAILS, "Wedding in NJ", [], resp.state)
        assert resp.next_step == ChatStep.ALREADY_BOOKED

        # Has venue and photographer
        resp = process_step(ChatStep.ALREADY_BOOKED, None, ["venue", "photographer"], resp.state)
        assert resp.next_step == ChatStep.STILL_NEED

        # Recommend all remaining
        resp = process_step(ChatStep.STILL_NEED, None, ["recommend_all"], resp.state)
        assert resp.next_step == ChatStep.BUDGET
        assert "venue" not in resp.state.needed_categories
        assert "photographer" not in resp.state.needed_categories

        # Premium budget
        resp = process_step(ChatStep.BUDGET, None, ["premium"], resp.state)
        resp = process_step(ChatStep.STYLE_PREFERENCES, None, ["traditional"], resp.state)
        assert resp.bundle is not None
        assert len(resp.bundle.items) == 5  # 7 - venue - photographer

        # Swap DJ
        resp = process_step(ChatStep.BUNDLE_ACTION, None, ["swap"], resp.state)
        resp = process_step(ChatStep.SWAP_VENDOR, None, ["dj"], resp.state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION

        # Save for later
        resp = process_step(ChatStep.BUNDLE_ACTION, None, ["keep"], resp.state)
        resp = process_step(ChatStep.RESULTS_BOOKING, None, ["save"], resp.state)
        assert "saved" in resp.bot_message.lower()


class TestCustomBudgetFlow:
    """Test the custom budget sub-flow."""

    def test_custom_budget_path(self):
        resp = get_initial_step()
        resp = process_step(ChatStep.EVENT_DETAILS, "Party in NYC", [], resp.state)
        resp = process_step(ChatStep.ALREADY_BOOKED, None, ["nothing_yet"], resp.state)

        # Custom budget
        resp = process_step(ChatStep.BUDGET, None, ["custom"], resp.state)
        assert resp.next_step == ChatStep.CUSTOM_BUDGET

        resp = process_step(ChatStep.CUSTOM_BUDGET, "6000", [], resp.state)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES
        assert resp.state.budget_amount == "6000"


# ═══════════════════════════════════════════════════════════════════════
# HTTP ENDPOINT TESTS (via TestClient)
# ═══════════════════════════════════════════════════════════════════════


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
        # First get initial state
        start_resp = client.post("/chatbot/start")
        state = start_resp.json()["state"]

        response = client.post("/chatbot/step", json={
            "current_step": "event_details",
            "user_input": "Summer 2026, 150 guests in NJ",
            "selected_values": [],
            "state": state,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["next_step"] == "already_booked"

    def test_step_endpoint_full_flow(self):
        # Start
        resp = client.post("/chatbot/start").json()
        state = resp["state"]

        # Event details
        resp = client.post("/chatbot/step", json={
            "current_step": "event_details",
            "user_input": "August 2026",
            "state": state,
        }).json()
        state = resp["state"]

        # Nothing booked
        resp = client.post("/chatbot/step", json={
            "current_step": "already_booked",
            "selected_values": ["nothing_yet"],
            "state": state,
        }).json()
        assert resp["next_step"] == "budget"
        state = resp["state"]

        # Mid-range budget
        resp = client.post("/chatbot/step", json={
            "current_step": "budget",
            "selected_values": ["mid-range"],
            "state": state,
        }).json()
        assert resp["next_step"] == "style_preferences"
        state = resp["state"]

        # Style
        resp = client.post("/chatbot/step", json={
            "current_step": "style_preferences",
            "selected_values": ["elegant"],
            "state": state,
        }).json()
        assert resp["next_step"] == "bundle_action"
        assert resp["bundle"] is not None
        assert len(resp["bundle"]["items"]) == 7

    def test_step_endpoint_invalid_step(self):
        """Sending an unknown step value should return 422."""
        response = client.post("/chatbot/step", json={
            "current_step": "non_existent_step",
            "state": {},
        })
        assert response.status_code == 422
