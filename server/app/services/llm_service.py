"""LLM fallback service — routes off-script chatbot inputs to Llama 3.3 via OpenRouter.

Two main responsibilities:
  1. Detect whether user input is "off-script" for the current step.
  2. Call Llama 3.3 to generate a helpful response + optionally extract
     structured intent so the chatbot can jump to the right step.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from openai import AsyncOpenAI

from app.models.chatbot_schemas import ChatStep, VENDOR_CATEGORIES, CATEGORY_LABELS

logger = logging.getLogger(__name__)

# ── Expected values per step (for off-script detection) ──────────────

_EXPECTED_VALUES: dict[ChatStep, set[str]] = {
    ChatStep.EVENT_DETAILS: {"has_date", "no_date", "continue"},
    ChatStep.ALREADY_BOOKED: set(VENDOR_CATEGORIES) | {"nothing_yet"},
    ChatStep.STILL_NEED: set(VENDOR_CATEGORIES) | {"recommend_all", "other"},
    ChatStep.BUDGET: {"budget-friendly", "mid-range", "premium", "custom", "unknown"},
    ChatStep.CUSTOM_BUDGET: {"under_3000", "3000_7000", "7000_12000", "custom_amount"},
    ChatStep.STYLE_PREFERENCES: {
        "elegant", "traditional", "modern", "luxury", "fun", "minimal", "not_sure",
        "pref_cultural", "pref_budget", "pref_luxury", "pref_highly_rated",
        "pref_local", "pref_fast",
    },
    ChatStep.BUNDLE_ACTION: {
        "keep", "customize", "swap", "remove", "add",
        "cheaper", "premium_bundle", "start_over",
    },
    ChatStep.MANUAL_CUSTOMIZE: set(VENDOR_CATEGORIES) | {"entire_bundle"},
    ChatStep.SWAP_VENDOR: set(VENDOR_CATEGORIES),
    ChatStep.REMOVE_CATEGORY: set(VENDOR_CATEGORIES),
    ChatStep.ADD_CATEGORY: set(VENDOR_CATEGORIES) | {"other"},
    ChatStep.RESULTS_BOOKING: {
        "book_all", "book_some", "contact", "save", "go_back", "done_contact",
    },
    ChatStep.PARTIAL_BOOKING: set(VENDOR_CATEGORIES),
}

# Steps where free-text input is expected as part of normal flow
_FREE_TEXT_STEPS: set[ChatStep] = {
    ChatStep.EVENT_DETAILS,
    ChatStep.CUSTOM_BUDGET,
    ChatStep.STYLE_PREFERENCES,
}


# ── Dataclass for LLM results ───────────────────────────────────────


@dataclass
class LLMResult:
    """Structured result from the Llama 3.3 call."""
    bot_message: str
    extracted_intent: Optional[str] = None
    suggested_step: Optional[str] = None
    extracted_values: dict = field(default_factory=dict)


# ── Off-script detection ─────────────────────────────────────────────


def is_off_script(
    current_step: ChatStep,
    user_input: Optional[str],
    selected_values: list[str],
) -> bool:
    """Return True if the user's input does not match expected values for the step.

    If the user clicked buttons (selected_values), they're on-script.
    If the user typed free text on a step that expects it, they're on-script.
    Otherwise, treat it as off-script.
    """
    # Button selections → always on-script
    if selected_values:
        expected = _EXPECTED_VALUES.get(current_step, set())
        # If at least one selected value is valid, treat as on-script
        if any(v in expected for v in selected_values):
            return False
        # Even invalid buttons — let the step handler deal with it
        return False

    # No buttons, no text → not off-script (just empty)
    if not user_input or not user_input.strip():
        return False

    # Free-text steps: heuristic — if the text is very short and looks like
    # a value (number, date, etc.) it's probably on-script
    if current_step in _FREE_TEXT_STEPS:
        return False

    # Any other step: user typed free text where only buttons were expected
    return True


# ── System prompt ────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are the DesiConnect event planning assistant — an AI chatbot that helps \
users plan South Asian events (weddings, engagements, garba nights, etc.) by \
building vendor bundles.

The user is currently on step "{current_step}" of the bundle-builder flow.
The available steps and their purposes:
- event_details: Collect event date, location, guest count
- already_booked: Ask what vendor categories they already have
- still_need: Ask which vendor categories to include in the bundle
- budget: Choose a budget tier (budget-friendly, mid-range, premium, custom)
- custom_budget: Specify a custom budget amount
- style_preferences: Choose style/vibe and preferences
- bundle_action: Review and modify the generated bundle
- manual_customize / swap_vendor / remove_category / add_category: Bundle edits
- results_booking: Final booking actions

Available vendor categories: {categories}

Your job:
1. Answer the user's question helpfully and concisely in the context of \
South Asian event planning.
2. Analyze whether the user's message contains an actionable intent that maps \
to one of the chatbot steps.
3. Respond in JSON with this exact structure:
{{
  "bot_message": "Your friendly, helpful response to the user",
  "extracted_intent": "one of: set_event_details, set_booked, set_needed, set_budget, set_style, modify_bundle, book, ask_question, or null",
  "suggested_step": "the ChatStep enum value to jump to, or null to stay on current step",
  "extracted_values": {{}}
}}

For extracted_values, include any structured data you can extract:
- For set_event_details: {{"location": "...", "guest_count": N, "event_date": "..."}}
- For set_budget: {{"budget_tier": "budget-friendly|mid-range|premium", "budget_amount": "..."}}
- For set_booked / set_needed: {{"categories": ["venue", "catering", ...]}}
- For set_style: {{"style": ["elegant", ...], "preferences": [...]}}
- For modify_bundle: {{"action": "swap|remove|add", "category": "..."}}
- For ask_question: {{}} (just answer the question, stay on current step)

Important rules:
- Keep bot_message concise (1-3 sentences).
- Only suggest a step jump if you are confident about the user's intent.
- If the user is just asking a question, set extracted_intent to "ask_question" \
and suggested_step to null.
- Always be warm, helpful, and culturally aware.
- If you're unsure, ask for clarification and stay on the current step.
"""


def _build_messages(
    current_step: ChatStep,
    user_input: str,
    conversation_history: list[dict],
) -> list[dict]:
    """Build the message list for the OpenRouter API call."""
    categories_str = ", ".join(
        f"{k} ({v})" for k, v in CATEGORY_LABELS.items()
    )

    messages = [
        {
            "role": "system",
            "content": _SYSTEM_PROMPT.format(
                current_step=current_step.value,
                categories=categories_str,
            ),
        },
    ]

    # Add recent conversation history for context (max 6 messages)
    for msg in conversation_history[-6:]:
        messages.append(msg)

    # Add the current user message
    messages.append({"role": "user", "content": user_input})

    return messages


# ── OpenRouter / Llama 3.3 call ──────────────────────────────────────


async def get_llm_response(
    current_step: ChatStep,
    user_input: str,
    conversation_history: list[dict],
) -> LLMResult:
    """Send the user's off-script input to Llama 3.3 via OpenRouter and parse the response."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key or api_key == "your-openrouter-api-key-here":
        logger.warning("OPENROUTER_API_KEY not configured — returning generic fallback")
        return LLMResult(
            bot_message=(
                "I'm not sure I understood that. Could you try using the "
                "buttons above, or rephrase your question?"
            ),
        )

    client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )
    messages = _build_messages(current_step, user_input, conversation_history)

    try:
        chat_completion = await client.chat.completions.create(
            model="meta-llama/llama-3.3-70b-instruct",
            messages=messages,
            temperature=0.3,
            max_tokens=512,
            response_format={"type": "json_object"},
        )

        raw = chat_completion.choices[0].message.content
        logger.info("LLM raw response: %s", raw)

        parsed = json.loads(raw)
        return LLMResult(
            bot_message=parsed.get("bot_message", "I'm here to help! Could you clarify?"),
            extracted_intent=parsed.get("extracted_intent"),
            suggested_step=parsed.get("suggested_step"),
            extracted_values=parsed.get("extracted_values", {}),
        )

    except json.JSONDecodeError:
        logger.error("LLM returned non-JSON response: %s", raw)
        return LLMResult(
            bot_message=raw if raw else "I'm here to help — could you rephrase that?",
        )
    except Exception as exc:
        logger.error("LLM call failed: %s", exc, exc_info=True)
        return LLMResult(
            bot_message=(
                "I had trouble processing that. Could you try using the "
                "buttons, or rephrase your question?"
            ),
        )
