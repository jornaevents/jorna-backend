# Chatbot Bundle Builder — Frontend Implementation Guide

The bundle builder is a multi-step chatbot flow that collects event details from the user and generates a personalised vendor bundle. At the end the user books vendors directly through the chatbot, creating real bookings in the database.

---

## API Overview

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /chatbot/start` | None | Begin a new session, returns initial state |
| `POST /chatbot/step` | Bearer JWT | Process a step, returns next prompt + updated state |
| `POST /chatbot/bundle` | None | Single-shot bundle from all inputs at once |
| `POST /chatbot/bundles` | None | Returns 3 bundles (Budget, Top Rated, Balanced) to compare |

### State management

The backend is stateless. Every response includes a `state` object — the frontend must send it back unchanged with each subsequent `/chatbot/step` request. Think of it as a session token carried by the client.

```
POST /chatbot/step
{
  "current_step": "budget",
  "selected_values": ["mid-range"],
  "user_input": null,
  "state": { ...exact state from previous response... }
}
```

---

## Screen-by-Screen Flow

### Screen 1 — Event Details (`event_details`)

**Bot message:** "Let's start with the basics. Tell me about your event."

**UI elements:**
- Optional free-text input for event description (date, location, guest count)
- Buttons: **I have a date** (`has_date`) / **No date yet** (`no_date`) / **Continue** (`continue`)

**Notes:**
- If the user types free text, send it as `user_input`
- If the user clicks a button, send the value in `selected_values`
- Both can be sent together

**Checklist:**
- [ ] Render bot message
- [ ] Render helper buttons as tappable chips/cards
- [ ] Optional text input field above buttons
- [ ] Send `user_input` + `selected_values` together if both provided
- [ ] Store returned `state` for next request

---

### Screen 2 — Already Booked (`already_booked`)

**Bot message:** "Before I build your bundle, what do you already have booked?"

**UI elements:**
- Multi-select buttons for each vendor category
- Button: **Nothing yet** (`nothing_yet`)

**Categories:** Venue / Catering / Decor / Photographer / DJ / Mehndi / Dhol

**Checklist:**
- [ ] Multi-select chip UI (user can select multiple categories)
- [ ] "Nothing yet" deselects all others
- [ ] Send all selected values in `selected_values` array

---

### Screen 3 — Still Need (`still_need`)

**Bot message:** "What do you want included in your bundle?"

**UI elements:**
- Multi-select buttons for remaining categories (already-booked ones excluded)
- Button: **Recommend everything I still need** (`recommend_all`)
- Button: **Other** (`other`)

**Checklist:**
- [ ] Exclude categories the user already marked as booked
- [ ] "Recommend everything" auto-selects all remaining categories
- [ ] Multi-select supported

---

### Screen 4 — Budget (`budget`)

**Bot message:** "What kind of budget should I use for your bundle?"

**UI elements:**
- Single-select cards: **Budget-friendly** / **Mid-range** / **Premium** / **Custom budget** / **Not sure**

**If user selects Custom budget** → show Screen 4b

**Checklist:**
- [ ] Single-select card UI
- [ ] Navigate to custom budget screen if `custom` selected

---

### Screen 4b — Custom Budget (`custom_budget`)

**Bot message:** "What total budget should I stay within for this bundle?"

**UI elements:**
- Buttons: **Under $3,000** / **$3,000–$7,000** / **$7,000–$12,000** / **Custom amount**
- If **Custom amount** selected: show a numeric input field

**Checklist:**
- [ ] Preset budget buttons
- [ ] Free text/numeric input for custom amount
- [ ] Send typed amount as `user_input`

---

### Screen 5 — Style & Preferences (`style_preferences`)

**Bot message:** "What kind of vibe are you going for, and any must-haves?"

**UI elements:**
- Multi-select chips for style (pick one or more):
  - Elegant / Traditional / Modern / Luxury / Fun & energetic / Minimal / Not sure
- Multi-select chips for preferences (pick any):
  - Cultural experience / Budget-friendly picks / Luxury feel / Highly rated vendors / Local vendors / Fast response

**Checklist:**
- [ ] Two visually distinct groups: Style and Preferences
- [ ] Multi-select for both groups
- [ ] Both style and preference values sent in same `selected_values` array

---

### Screen 6 — Bundle Revealed (`bundle_action`)

**Bot message:** "Here's the bundle I built for you."

**UI elements:**
- Bundle card showing each vendor:
  - Category label
  - Vendor name + profile photo (`pfp_url`)
  - Star rating
  - Price range (`price_min` – `price_max`)
  - Match reason (short description)
- Estimated total at the bottom
- Action buttons:
  - **Keep this bundle** (`keep`)
  - **Customize manually** (`customize`)
  - **Swap a vendor** (`swap`)
  - **Remove a category** (`remove`)
  - **Add a category** (`add`)
  - **See cheaper bundle** (`cheaper`)
  - **See premium bundle** (`premium_bundle`)
  - **Start over** (`start_over`)

**Notes:**
- Items where `vendor_id` is null are mock/placeholder vendors (no real vendor available yet). Consider showing a "coming soon" badge.
- Cheaper/premium regenerates the bundle with a different tier — the full bundle card updates in place.

**Checklist:**
- [ ] Bundle card component with vendor photo, name, rating, price range, match reason
- [ ] Estimated total display
- [ ] Distinguish real vendors (has `vendor_id`) from mock placeholders
- [ ] All action buttons rendered
- [ ] Cheaper/premium swaps the displayed bundle without changing step
- [ ] Swap/Remove/Add navigate to sub-screens (6a, 6b, 6c)

---

### Screen 6a — Swap a Vendor (`swap_vendor`)

**Bot message:** "Which category do you want to swap?"

**UI elements:** Category buttons for all categories currently in the bundle

**Checklist:**
- [ ] Only show categories present in the current bundle
- [ ] After selection, bundle updates with alternative vendor

---

### Screen 6b — Remove a Category (`remove_category`)

**Bot message:** "Which category do you want to remove from the bundle?"

**UI elements:** Category buttons for all categories currently in the bundle

**Checklist:**
- [ ] Only show categories present in the current bundle
- [ ] Bundle card updates after removal

---

### Screen 6c — Add a Category (`add_category`)

**Bot message:** "What would you like to add to the bundle?"

**UI elements:** Category buttons for categories NOT already in the bundle + **Other**

**Checklist:**
- [ ] Exclude categories already in the bundle
- [ ] Bundle card updates after addition

---

### Screen 7 — Booking Confirmation (`results_booking`)

**Bot message:** "Ready to book? You can book the whole bundle or select specific categories."

**UI elements:**
- **Book this whole bundle** (`book_all`)
- **Book only some categories** (`book_some`)
- **Go back and edit** (`go_back`)

**Checklist:**
- [ ] Clear confirmation UI before booking is submitted
- [ ] Consider a summary of what will be booked (vendor names, prices)
- [ ] Disable buttons after booking is submitted to prevent double-tap

---

### Screen 7b — Partial Booking (`partial_booking`)

**Bot message:** "Which categories do you want to book now?"

**UI elements:** Category buttons for all items in the bundle (multi-select)

**Checklist:**
- [ ] Multi-select — user picks which categories to book now
- [ ] Unselected categories are skipped (no booking created)

---

### Screen 8 — Booked (`results_booking` with `bundle_id`)

**Bot message:** "Your bundle has been created with N booking(s) submitted. Each vendor will review and confirm your request."

**Response fields:**
```json
{
  "bundle_id": "abc-123",
  "booking_ids": ["id1", "id2", "id3"]
}
```

**Checklist:**
- [ ] Detect when response contains `bundle_id` (booking was created)
- [ ] Show success screen with summary
- [ ] Navigate to bundle detail page using `bundle_id`
- [ ] Each booking starts in `pending` status — vendors must approve

---

## Off-Script Inputs

At any step the user can type free text instead of clicking a button. The backend detects this and routes it to the LLM, which responds naturally and nudges the user back to the current step.

The response looks identical to a normal step response but has `"llm_response": true`.

**Checklist:**
- [ ] Show a text input field on every step (not just free-text steps)
- [ ] Render LLM responses as chat bubbles rather than structured step cards
- [ ] Still render the step's helper buttons below the LLM response
- [ ] No special handling needed — the backend manages the step transition

---

## Using Coordinates for Location Filtering

Pass the user's event coordinates to filter vendors by travel radius:

```json
{
  "needed_categories": ["dj", "venue"],
  "latitude": 40.7128,
  "longitude": -74.0060
}
```

**Checklist:**
- [ ] Request location permission from the user
- [ ] Pass `latitude` and `longitude` in `/chatbot/bundle` and `/chatbot/bundles` requests
- [ ] Pass `latitude` and `longitude` in the `state` object for `/chatbot/step`

---

## 3-Bundle Comparison Flow (Alternative Entry Point)

Instead of the step-by-step flow, the frontend can show 3 bundles at once using `POST /chatbot/bundles`. This is better for users who already know what they want.

**UI:** 3 side-by-side cards — Budget Bundle / Top Rated Bundle / Balanced Bundle

Each card has a **Select** button. Selecting one loads that bundle's `state` into the step-by-step flow at `bundle_action` so the user can refine it.

**Checklist:**
- [ ] 3-column card layout (or horizontal scroll on mobile)
- [ ] Each card shows estimated total and top vendors
- [ ] Selecting a card sets the active state and navigates to Screen 6
- [ ] All inputs optional — send whatever the user has provided

---

## General Checklist

- [ ] Persist `state` in memory (or component state) between steps — never store in a database
- [ ] Handle `401 Unauthorized` on `/chatbot/step` — user must be logged in
- [ ] Show loading indicator while waiting for step responses (LLM calls can take 2–4s)
- [ ] Handle network errors gracefully — allow retry without losing state
- [ ] On mobile, keep the button area fixed at the bottom with the chat scrolling above
- [ ] `bundle_id` in the response means bookings were created — always navigate away at this point
