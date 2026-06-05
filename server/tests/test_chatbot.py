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
from app.db.models import User
from tests.test_api import TestingSessionLocal, make_auth_headers
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
from app.services.llm_service import (
    LLMResult,
    is_off_script,
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
    async def test_nothing_yet_skips_to_budget(self):
        state = ChatbotState()
        resp = await process_step(ChatStep.ALREADY_BOOKED, None, ["nothing_yet"], state)
        assert resp.next_step == ChatStep.BUDGET
        assert resp.state.booked_categories == []
        assert resp.state.needed_categories == list(VENDOR_CATEGORIES)

    async def test_categories_selected_goes_to_still_need(self):
        state = ChatbotState()
        resp = await process_step(ChatStep.ALREADY_BOOKED, None, ["venue", "photographer"], state)
        assert resp.next_step == ChatStep.STILL_NEED
        assert resp.state.booked_categories == ["venue", "photographer"]

    async def test_single_category(self):
        state = ChatbotState()
        resp = await process_step(ChatStep.ALREADY_BOOKED, None, ["dj"], state)
        assert resp.next_step == ChatStep.STILL_NEED
        assert resp.state.booked_categories == ["dj"]


@pytest.mark.asyncio
class TestStillNeedStep:
    async def test_recommend_all(self):
        state = ChatbotState(booked_categories=["venue", "photographer"])
        resp = await process_step(ChatStep.STILL_NEED, None, ["recommend_all"], state)
        assert resp.next_step == ChatStep.BUDGET
        assert "venue" not in resp.state.needed_categories
        assert "photographer" not in resp.state.needed_categories
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

    async def test_other_category(self):
        state = ChatbotState(booked_categories=[])
        resp = await process_step(ChatStep.STILL_NEED, None, ["catering", "other"], state)
        assert "other" in resp.state.needed_categories


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
    async def test_generates_bundle(self):
        state = ChatbotState(
            needed_categories=["dj", "catering", "decor"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        resp = await process_step(ChatStep.STYLE_PREFERENCES, None, ["elegant", "pref_local"], state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert resp.bundle is not None
        assert len(resp.bundle.items) == 3
        assert resp.state.style == ["elegant"]
        assert resp.state.preferences == ["pref_local"]

    async def test_free_text_style(self):
        state = ChatbotState(
            needed_categories=["dj"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        resp = await process_step(ChatStep.STYLE_PREFERENCES, "elegant and classy", [], state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert resp.bundle is not None

    async def test_bundle_has_correct_categories(self):
        cats = ["dj", "mehndi", "dhol"]
        state = ChatbotState(needed_categories=cats, budget_tier=BudgetTier.PREMIUM)
        resp = await process_step(ChatStep.STYLE_PREFERENCES, None, ["modern"], state)
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


@pytest.mark.asyncio
class TestBundleActionStep:
    def _make_state_with_bundle(self):
        state = ChatbotState(
            needed_categories=list(VENDOR_CATEGORIES),
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state)
        return state

    async def test_keep_goes_to_results(self):
        state = self._make_state_with_bundle()
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["keep"], state)
        assert resp.next_step == ChatStep.RESULTS_BOOKING

    async def test_customize_goes_to_manual(self):
        state = self._make_state_with_bundle()
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["customize"], state)
        assert resp.next_step == ChatStep.MANUAL_CUSTOMIZE

    async def test_swap_goes_to_swap(self):
        state = self._make_state_with_bundle()
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["swap"], state)
        assert resp.next_step == ChatStep.SWAP_VENDOR

    async def test_remove_goes_to_remove(self):
        state = self._make_state_with_bundle()
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["remove"], state)
        assert resp.next_step == ChatStep.REMOVE_CATEGORY

    async def test_add_goes_to_add(self):
        state = self._make_state_with_bundle()
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["add"], state)
        assert resp.next_step == ChatStep.ADD_CATEGORY

    async def test_cheaper_regenerates(self):
        state = self._make_state_with_bundle()
        original_max = state.bundle.estimated_total_max
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["cheaper"], state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert resp.state.budget_tier == BudgetTier.BUDGET_FRIENDLY
        assert resp.bundle.estimated_total_max < original_max

    async def test_premium_regenerates(self):
        state = self._make_state_with_bundle()
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["premium_bundle"], state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert resp.state.budget_tier == BudgetTier.PREMIUM

    async def test_start_over(self):
        state = self._make_state_with_bundle()
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["start_over"], state)
        assert resp.next_step == ChatStep.EVENT_DETAILS


@pytest.mark.asyncio
class TestSwapVendor:
    async def test_swap_changes_vendor(self):
        state = ChatbotState(
            needed_categories=["dj"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state)
        original_name = state.bundle.items[0].vendor_name

        resp = await process_step(ChatStep.SWAP_VENDOR, None, ["dj"], state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        swapped_name = resp.state.bundle.items[0].vendor_name
        assert swapped_name != original_name


@pytest.mark.asyncio
class TestRemoveCategory:
    async def test_remove_shrinks_bundle(self):
        state = ChatbotState(
            needed_categories=["dj", "mehndi", "dhol"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state)
        assert len(state.bundle.items) == 3

        resp = await process_step(ChatStep.REMOVE_CATEGORY, None, ["mehndi"], state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert len(resp.state.bundle.items) == 2
        cats = [i.category for i in resp.state.bundle.items]
        assert "mehndi" not in cats
        assert "Done" in resp.bot_message


@pytest.mark.asyncio
class TestAddCategory:
    async def test_add_expands_bundle(self):
        state = ChatbotState(
            needed_categories=["dj"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state)
        assert len(state.bundle.items) == 1

        resp = await process_step(ChatStep.ADD_CATEGORY, None, ["mehndi"], state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert len(resp.state.bundle.items) == 2
        cats = [i.category for i in resp.state.bundle.items]
        assert "mehndi" in cats
        assert "Added" in resp.bot_message


@pytest.mark.asyncio
class TestResultsBookingStep:
    def _make_state(self):
        state = ChatbotState(
            needed_categories=["dj", "catering"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state)
        return state

    async def test_book_all(self):
        state = self._make_state()
        resp = await process_step(ChatStep.RESULTS_BOOKING, None, ["book_all"], state)
        assert "book" in resp.bot_message.lower() or "Proceeding" in resp.bot_message

    async def test_book_some_goes_to_partial(self):
        state = self._make_state()
        resp = await process_step(ChatStep.RESULTS_BOOKING, None, ["book_some"], state)
        assert resp.next_step == ChatStep.PARTIAL_BOOKING

    async def test_save(self):
        state = self._make_state()
        resp = await process_step(ChatStep.RESULTS_BOOKING, None, ["save"], state)
        assert resp.next_step == ChatStep.RESULTS_BOOKING

    async def test_go_back(self):
        state = self._make_state()
        resp = await process_step(ChatStep.RESULTS_BOOKING, None, ["go_back"], state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION

    async def test_contact(self):
        state = self._make_state()
        resp = await process_step(ChatStep.RESULTS_BOOKING, None, ["contact"], state)
        assert resp.next_step == ChatStep.RESULTS_BOOKING


@pytest.mark.asyncio
class TestPartialBooking:
    async def test_selects_categories(self):
        state = ChatbotState(
            needed_categories=["dj", "catering"],
            budget_tier=BudgetTier.MID_RANGE,
        )
        state.bundle = generate_bundle(state)
        resp = await process_step(ChatStep.PARTIAL_BOOKING, None, ["dj"], state)
        assert resp.next_step == ChatStep.RESULTS_BOOKING
        assert resp.next_step == ChatStep.RESULTS_BOOKING


# ═══════════════════════════════════════════════════════════════════════
# FULL FLOW INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestFullFlowNothingBooked:
    """Walk through the complete flow when user has nothing booked yet."""

    async def test_complete_flow(self):
        # Step 0: Start
        resp = get_initial_step()
        assert resp.next_step == ChatStep.EVENT_DETAILS

        # Step 0 → 0b: Event details
        resp = await process_step(ChatStep.EVENT_DETAILS, "August 2026, NJ, 200 people", [], resp.state)
        assert resp.next_step == ChatStep.EVENT_TIME

        # Step 0b → 1: Event time
        resp = await process_step(ChatStep.EVENT_TIME, None, ["evening"], resp.state)
        assert resp.next_step == ChatStep.ALREADY_BOOKED

        # Step 1 → 3: Nothing booked (skip step 2)
        resp = await process_step(ChatStep.ALREADY_BOOKED, None, ["nothing_yet"], resp.state)
        assert resp.next_step == ChatStep.BUDGET
        assert len(resp.state.needed_categories) == 7

        # Step 3 → 4: Budget
        resp = await process_step(ChatStep.BUDGET, None, ["mid-range"], resp.state)
        assert resp.next_step == ChatStep.STYLE_PREFERENCES

        # Step 4 → 5/6: Style → bundle
        resp = await process_step(ChatStep.STYLE_PREFERENCES, None, ["elegant"], resp.state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION
        assert resp.bundle is not None
        assert len(resp.bundle.items) == 7

        # Step 6 → 7: Keep bundle
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["keep"], resp.state)
        assert resp.next_step == ChatStep.RESULTS_BOOKING

        # Step 7: Book all
        resp = await process_step(ChatStep.RESULTS_BOOKING, None, ["book_all"], resp.state)
        assert "book" in resp.bot_message.lower() or "Proceeding" in resp.bot_message


@pytest.mark.asyncio
class TestFullFlowWithBooked:
    """Walk through the flow when user has some categories already booked."""

    async def test_complete_flow(self):
        resp = get_initial_step()
        resp = await process_step(ChatStep.EVENT_DETAILS, "Wedding in NJ", [], resp.state)
        assert resp.next_step == ChatStep.EVENT_TIME
        resp = await process_step(ChatStep.EVENT_TIME, None, ["full_day"], resp.state)
        assert resp.next_step == ChatStep.ALREADY_BOOKED

        # Has venue and photographer
        resp = await process_step(ChatStep.ALREADY_BOOKED, None, ["venue", "photographer"], resp.state)
        assert resp.next_step == ChatStep.STILL_NEED

        # Recommend all remaining
        resp = await process_step(ChatStep.STILL_NEED, None, ["recommend_all"], resp.state)
        assert resp.next_step == ChatStep.BUDGET
        assert "venue" not in resp.state.needed_categories
        assert "photographer" not in resp.state.needed_categories

        # Premium budget
        resp = await process_step(ChatStep.BUDGET, None, ["premium"], resp.state)
        resp = await process_step(ChatStep.STYLE_PREFERENCES, None, ["traditional"], resp.state)
        assert resp.bundle is not None
        assert len(resp.bundle.items) == 5  # 7 - venue - photographer

        # Swap DJ
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["swap"], resp.state)
        resp = await process_step(ChatStep.SWAP_VENDOR, None, ["dj"], resp.state)
        assert resp.next_step == ChatStep.BUNDLE_ACTION

        # Save for later
        resp = await process_step(ChatStep.BUNDLE_ACTION, None, ["keep"], resp.state)
        resp = await process_step(ChatStep.RESULTS_BOOKING, None, ["save"], resp.state)
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
    async def test_llm_modify_bundle_swap(self, mock_llm):
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
        state.bundle = generate_bundle(state)

        resp = await process_step(
            ChatStep.BUNDLE_ACTION,
            "Can you swap the DJ for someone else?",
            [],
            state,
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

        # Nothing booked
        resp = client.post("/chatbot/step", headers=headers, json={
            "current_step": "already_booked",
            "selected_values": ["nothing_yet"],
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
        assert len(resp["bundle"]["items"]) == 7

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
