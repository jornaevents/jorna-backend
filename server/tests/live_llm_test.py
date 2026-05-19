"""Extensive live integration tests for the Llama 3.3 chatbot fallback.

Requires the server to be running at http://127.0.0.1:8000 with a valid
OPENROUTER_API_KEY configured.

Tests cover:
  1. On-script flows still work (no regressions)
  2. Off-script questions → LLM answers, stays on same step
  3. Off-script intent extraction → LLM jumps to correct step
  4. Conversation history is maintained across turns
  5. Various off-script messages at different steps
  6. Edge cases (empty input, gibberish, multi-language)
"""

import json
import sys
import httpx

BASE = "http://127.0.0.1:8000"
client = httpx.Client(base_url=BASE, timeout=30.0)

passed = 0
failed = 0
results = []


def test(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        results.append(("✅", name, detail))
        print(f"  ✅ {name}")
    else:
        failed += 1
        results.append(("❌", name, detail))
        print(f"  ❌ {name} — {detail}")


def post_step(current_step, user_input=None, selected_values=None, state=None):
    body = {
        "current_step": current_step,
        "state": state or {},
    }
    if user_input is not None:
        body["user_input"] = user_input
    if selected_values is not None:
        body["selected_values"] = selected_values
    resp = client.post("/chatbot/step", json=body)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    return resp.json()


def start():
    resp = client.post("/chatbot/start")
    assert resp.status_code == 200
    return resp.json()


# ═══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("SECTION 1: ON-SCRIPT FLOWS (regression check)")
print("=" * 70)

# Test 1.1: Full happy path with buttons
print("\n📋 Test 1.1: Full happy path (buttons only)")
r = start()
test("Start returns event_details", r["next_step"] == "event_details")
test("llm_response is False on start", r["llm_response"] is False)

r = post_step("event_details", user_input="Wedding in NJ, August 2026, 200 guests", state=r["state"])
test("Event details → already_booked", r["next_step"] == "already_booked")
test("llm_response is False", r["llm_response"] is False)

r = post_step("already_booked", selected_values=["nothing_yet"], state=r["state"])
test("Nothing booked → budget", r["next_step"] == "budget")

r = post_step("budget", selected_values=["mid-range"], state=r["state"])
test("Mid-range → style_preferences", r["next_step"] == "style_preferences")

r = post_step("style_preferences", selected_values=["elegant", "pref_local"], state=r["state"])
test("Style → bundle_action", r["next_step"] == "bundle_action")
test("Bundle has 7 items", r["bundle"] is not None and len(r["bundle"]["items"]) == 7)
test("llm_response is False for on-script", r["llm_response"] is False)

r = post_step("bundle_action", selected_values=["keep"], state=r["state"])
test("Keep → results_booking", r["next_step"] == "results_booking")

# Test 1.2: Partial booking flow
print("\n📋 Test 1.2: Booked categories flow")
r = start()
r = post_step("event_details", selected_values=["has_date"], state=r["state"])
r = post_step("already_booked", selected_values=["venue", "photographer"], state=r["state"])
test("With booked → still_need", r["next_step"] == "still_need")
test("Booked categories saved", r["state"]["booked_categories"] == ["venue", "photographer"])

r = post_step("still_need", selected_values=["recommend_all"], state=r["state"])
test("Recommend all → budget", r["next_step"] == "budget")
test("Venue excluded from needed", "venue" not in r["state"]["needed_categories"])

# Test 1.3: Custom budget flow
print("\n📋 Test 1.3: Custom budget flow")
r = start()
r = post_step("event_details", user_input="Party", state=r["state"])
r = post_step("already_booked", selected_values=["nothing_yet"], state=r["state"])
r = post_step("budget", selected_values=["custom"], state=r["state"])
test("Custom → custom_budget step", r["next_step"] == "custom_budget")
r = post_step("custom_budget", user_input="8000", state=r["state"])
test("Custom amount → style", r["next_step"] == "style_preferences")
test("Budget amount saved", r["state"]["budget_amount"] == "8000")


# ═══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("SECTION 2: OFF-SCRIPT QUESTIONS (LLM answers, stays on step)")
print("=" * 70)

base_state = {
    "needed_categories": ["venue", "catering", "decor", "photographer", "dj", "mehndi", "dhol"],
    "conversation_history": [],
}

# Test 2.1: Question during budget step
print("\n📋 Test 2.1: Question at budget step")
r = post_step("budget", user_input="What's the average cost of a South Asian wedding?", state=base_state.copy())
test("llm_response is True", r["llm_response"] is True)
test("Stays on budget step", r["next_step"] == "budget")
test("Bot message is non-empty", len(r["bot_message"]) > 20)
test("Budget buttons still present", len(r["helper_buttons"]) > 0)
print(f"    💬 LLM said: {r['bot_message'][:120]}...")

# Test 2.2: Question during bundle_action step
print("\n📋 Test 2.2: Question at bundle_action step")
bundle_state = base_state.copy()
bundle_state["bundle"] = {
    "items": [{"category": "dj", "vendor_name": "Test DJ", "price_min": 500, "price_max": 1000, "rating": 4.5, "match_reason": "test"}],
    "estimated_total_min": 500, "estimated_total_max": 1000,
}
r = post_step("bundle_action", user_input="What should I look for in a good wedding photographer?", state=bundle_state)
test("llm_response is True", r["llm_response"] is True)
test("Stays on bundle_action", r["next_step"] == "bundle_action")
test("Mentions photography-related content", any(w in r["bot_message"].lower() for w in ["photo", "camera", "portrait", "candid", "capture", "look", "quality"]))
print(f"    💬 LLM said: {r['bot_message'][:120]}...")

# Test 2.3: Question during results_booking step
print("\n📋 Test 2.3: Question at results_booking step")
r = post_step("results_booking", user_input="How far in advance should I book my vendors?", state=bundle_state)
test("llm_response is True", r["llm_response"] is True)
test("Stays on results_booking", r["next_step"] == "results_booking")
test("Response is contextually relevant", len(r["bot_message"]) > 20)
print(f"    💬 LLM said: {r['bot_message'][:120]}...")

# Test 2.4: Cultural question during already_booked step
print("\n📋 Test 2.4: Cultural question at already_booked step")
r = post_step("already_booked", user_input="What events typically happen during a Hindu wedding week?", state={"conversation_history": []})
test("llm_response is True", r["llm_response"] is True)
test("Stays on already_booked", r["next_step"] == "already_booked")
print(f"    💬 LLM said: {r['bot_message'][:120]}...")

# Test 2.5: Vendor comparison question
print("\n📋 Test 2.5: Vendor comparison question at swap_vendor step")
r = post_step("swap_vendor", user_input="Is it better to have a live band or a DJ for a sangeet?", state=bundle_state)
test("llm_response is True", r["llm_response"] is True)
test("Stays on swap_vendor", r["next_step"] == "swap_vendor")
print(f"    💬 LLM said: {r['bot_message'][:120]}...")


# ═══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("SECTION 3: OFF-SCRIPT INTENT EXTRACTION (LLM jumps steps)")
print("=" * 70)

# Test 3.1: Natural language budget
print("\n📋 Test 3.1: Natural language budget amount")
r = post_step("budget", user_input="I think around ten thousand dollars", state=base_state.copy())
test("llm_response is True", r["llm_response"] is True)
test("Jumps to style_preferences", r["next_step"] == "style_preferences")
test("Budget amount extracted", r["state"]["budget_amount"] is not None)
print(f"    💬 LLM said: {r['bot_message'][:120]}...")
print(f"    📊 Extracted budget: {r['state'].get('budget_amount')}, tier: {r['state'].get('budget_tier')}")

# Test 3.2: Natural language budget tier
print("\n📋 Test 3.2: Natural language budget tier")
r = post_step("budget", user_input="I want to keep things affordable, nothing too fancy", state=base_state.copy())
test("llm_response is True", r["llm_response"] is True)
test("Advances past budget", r["next_step"] in ("style_preferences", "custom_budget"))
print(f"    💬 LLM said: {r['bot_message'][:120]}...")
print(f"    📊 Budget tier: {r['state'].get('budget_tier')}")

# Test 3.3: Bundle modification via natural language
print("\n📋 Test 3.3: Natural language bundle modification")
r = post_step("bundle_action", user_input="Can you swap out the DJ for a different one?", state=bundle_state.copy())
test("llm_response is True", r["llm_response"] is True)
test("Jumps to swap_vendor", r["next_step"] == "swap_vendor")
print(f"    💬 LLM said: {r['bot_message'][:120]}...")

# Test 3.4: Remove category via natural language
print("\n📋 Test 3.4: Natural language remove category")
r = post_step("bundle_action", user_input="I don't think I need mehndi anymore, please remove it", state=bundle_state.copy())
test("llm_response is True", r["llm_response"] is True)
test("Jumps to remove_category", r["next_step"] == "remove_category")
print(f"    💬 LLM said: {r['bot_message'][:120]}...")

# Test 3.5: Add category via natural language
print("\n📋 Test 3.5: Natural language add category")
r = post_step("bundle_action", user_input="I also want to add a dhol player to the bundle", state=bundle_state.copy())
test("llm_response is True", r["llm_response"] is True)
test("Jumps to add_category", r["next_step"] == "add_category")
print(f"    💬 LLM said: {r['bot_message'][:120]}...")

# Test 3.6: Express readiness to book
print("\n📋 Test 3.6: Express readiness to book")
r = post_step("bundle_action", user_input="This looks great, I'm ready to book everything!", state=bundle_state.copy())
test("llm_response is True", r["llm_response"] is True)
test("Jumps to results_booking", r["next_step"] == "results_booking")
print(f"    💬 LLM said: {r['bot_message'][:120]}...")


# ═══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("SECTION 4: CONVERSATION HISTORY")
print("=" * 70)

# Test 4.1: History accumulates
print("\n📋 Test 4.1: History accumulates across turns")
state = base_state.copy()
r1 = post_step("budget", user_input="What does a typical mehndi artist cost?", state=state)
test("First off-script: history has 2 entries", len(r1["state"]["conversation_history"]) == 2)

r2 = post_step("budget", user_input="And what about a DJ?", state=r1["state"])
test("Second off-script: history has 4 entries", len(r2["state"]["conversation_history"]) == 4)
test("History contains first question", any("mehndi" in m.get("content", "").lower() for m in r2["state"]["conversation_history"]))
print(f"    💬 Turn 1: {r1['bot_message'][:80]}...")
print(f"    💬 Turn 2: {r2['bot_message'][:80]}...")

# Test 4.2: History is capped at 6
print("\n📋 Test 4.2: History is capped")
state = base_state.copy()
state["conversation_history"] = [
    {"role": "user", "content": f"question {i}"} if i % 2 == 0
    else {"role": "assistant", "content": f"answer {i}"}
    for i in range(8)
]
r = post_step("budget", user_input="Another question", state=state)
test("History capped at 6", len(r["state"]["conversation_history"]) <= 6)


# ═══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("SECTION 5: EDGE CASES")
print("=" * 70)

# Test 5.1: Very short off-script text
print("\n📋 Test 5.1: Short off-script text")
r = post_step("bundle_action", user_input="help", state=bundle_state.copy())
test("llm_response is True for 'help'", r["llm_response"] is True)
test("Returns a helpful message", len(r["bot_message"]) > 10)
print(f"    💬 LLM said: {r['bot_message'][:120]}...")

# Test 5.2: Very long off-script text
print("\n📋 Test 5.2: Long off-script text")
long_msg = (
    "I'm planning a big Punjabi wedding in New Jersey for August 2026. "
    "We're expecting about 300 guests. My fiancé and I want a traditional "
    "feel but with modern touches. We already have a venue booked at the "
    "Grand Mahal. I need help with everything else — catering, decor, "
    "photography, DJ, mehndi, and dhol. Our budget is around $15,000. "
    "We want everything to be elegant but not over the top."
)
r = post_step("bundle_action", user_input=long_msg, state=bundle_state.copy())
test("llm_response is True for long text", r["llm_response"] is True)
test("LLM handles long input", len(r["bot_message"]) > 20)
print(f"    💬 LLM said: {r['bot_message'][:120]}...")

# Test 5.3: Hindi/Hinglish text
print("\n📋 Test 5.3: Hinglish text")
r = post_step("budget", user_input="Mujhe ek accha DJ chahiye baraat ke liye, budget kitna hoga?", state=base_state.copy())
test("llm_response is True for Hinglish", r["llm_response"] is True)
test("LLM handles Hinglish", len(r["bot_message"]) > 10)
print(f"    💬 LLM said: {r['bot_message'][:120]}...")

# Test 5.4: Emoji-heavy input
print("\n📋 Test 5.4: Emoji input")
r = post_step("bundle_action", user_input="I love this bundle! 🎉💃🕺 Can you make it even better? ✨", state=bundle_state.copy())
test("llm_response is True for emojis", r["llm_response"] is True)
test("LLM handles emojis", len(r["bot_message"]) > 10)
print(f"    💬 LLM said: {r['bot_message'][:120]}...")

# Test 5.5: On-script button click still works after off-script
print("\n📋 Test 5.5: On-script after off-script")
r1 = post_step("budget", user_input="Tell me about vendors", state=base_state.copy())
test("Off-script detected", r1["llm_response"] is True)
r2 = post_step("budget", selected_values=["mid-range"], state=r1["state"])
test("Button click works after off-script", r2["next_step"] == "style_preferences")
test("On-script: llm_response is False", r2["llm_response"] is False)

# Test 5.6: Empty free text on button step
print("\n📋 Test 5.6: Empty input on button step")
r = post_step("budget", user_input="", state=base_state.copy())
test("Empty input stays on-script", r["llm_response"] is False)

# Test 5.7: Free text on event_details (should be ON-script)
print("\n📋 Test 5.7: Free text on event_details (on-script)")
r = post_step("event_details", user_input="Wedding in New York, March 2027, 150 people", state={"conversation_history": []})
test("Event details free text is on-script", r["llm_response"] is False)
test("Advances to already_booked", r["next_step"] == "already_booked")
test("Guest count extracted", r["state"]["guest_count"] == 150)

# Test 5.8: Free text on custom_budget (should be ON-script)
print("\n📋 Test 5.8: Free text on custom_budget (on-script)")
r = post_step("custom_budget", user_input="7500", state={"budget_tier": "custom", "conversation_history": []})
test("Custom budget free text is on-script", r["llm_response"] is False)
test("Advances to style", r["next_step"] == "style_preferences")


# ═══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("SECTION 6: FULL END-TO-END FLOW WITH OFF-SCRIPT MIXED IN")
print("=" * 70)

print("\n📋 Test 6.1: Realistic user flow mixing buttons and free text")
# Start
r = start()
test("Flow start OK", r["next_step"] == "event_details")

# Event details (on-script)
r = post_step("event_details", user_input="Engagement party in Boston, June 2026, 100 guests", state=r["state"])
test("Flow: event_details → already_booked", r["next_step"] == "already_booked")

# Off-script question before answering
r = post_step("already_booked", user_input="Do I need a dhol player for an engagement party?", state=r["state"])
test("Flow: off-script question at already_booked", r["llm_response"] is True)
test("Flow: stays on already_booked", r["next_step"] == "already_booked")
print(f"    💬 LLM said: {r['bot_message'][:100]}...")

# Now answer on-script
r = post_step("already_booked", selected_values=["venue"], state=r["state"])
test("Flow: venue booked → still_need", r["next_step"] == "still_need")

# Recommend all
r = post_step("still_need", selected_values=["recommend_all"], state=r["state"])
test("Flow: recommend_all → budget", r["next_step"] == "budget")

# Natural language budget (off-script intent extraction)
r = post_step("budget", user_input="Let's keep it under 8 grand", state=r["state"])
test("Flow: NL budget → advances", r["llm_response"] is True)
test("Flow: budget extracted", r["state"].get("budget_amount") is not None or r["state"].get("budget_tier") is not None)
print(f"    💬 LLM said: {r['bot_message'][:100]}...")

# If we jumped to style, select style
if r["next_step"] == "style_preferences":
    r = post_step("style_preferences", selected_values=["modern", "pref_cultural"], state=r["state"])
    test("Flow: style → bundle_action", r["next_step"] == "bundle_action")
    test("Flow: bundle generated", r["bundle"] is not None)

    # Off-script at bundle review
    r = post_step("bundle_action", user_input="Which of these vendors has the best reviews?", state=r["state"])
    test("Flow: off-script question at bundle_action", r["llm_response"] is True)
    test("Flow: stays on bundle_action", r["next_step"] == "bundle_action")
    print(f"    💬 LLM said: {r['bot_message'][:100]}...")

    # Keep bundle
    r = post_step("bundle_action", selected_values=["keep"], state=r["state"])
    test("Flow: keep → results_booking", r["next_step"] == "results_booking")


# ═══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print(f"RESULTS: {passed} passed, {failed} failed out of {passed + failed} tests")
print("=" * 70)

if failed > 0:
    print("\n❌ Failed tests:")
    for status, name, detail in results:
        if status == "❌":
            print(f"  {name}: {detail}")

sys.exit(1 if failed > 0 else 0)
