# Chatbot Bundle Builder — Frontend Implementation Guide

The bundle builder is a multi-step chatbot flow that collects event details from the user and generates a personalised vendor bundle. At the end the user books vendors directly through the chatbot, creating real bookings in the database.

This guide is the contract for the Jorna client (SwiftUI iOS app). It is platform-neutral where possible — "screen" means a chat turn, "tap a chip" means selecting a `helper_button`, and "navigate to the bundle detail screen" means showing the booked bundle. The **step-by-step flow is the primary experience**; the 3-bundle comparison flow at the end is an optional alternative entry point.

---

## API Overview

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /chatbot/start` | None | Begin a new session, returns initial state |
| `POST /chatbot/step` | Bearer JWT | Process a step, returns next prompt + updated state |
| `POST /chatbot/bundle` | None | Single-shot bundle from all inputs at once (not persisted) |
| `POST /chatbot/bundles` | Bearer JWT | Persists 3 draft bundles (Budget, Top Rated, Balanced) to compare |
| `POST /bundles/{bundle_id}/select` | Bearer JWT | Keep one bundle from a comparison group, discard the rest |

> Auth note: `/chatbot/step`, `/chatbot/bundles`, and `/bundles/{bundle_id}/select` all require a logged-in user and return `401` without a valid Bearer token. `/chatbot/start` and `/chatbot/bundle` are open.

### State management

The backend is stateless. Every response includes a `state` object — the client must hold it in memory and send it back unchanged with each subsequent `/chatbot/step` request. Think of it as a session token carried by the client. Never persist it to disk or a database; it lives only for the duration of the conversation.

`current_step` in the request is the step you are *answering* — i.e. the `next_step` from the response you are replying to.

```
POST /chatbot/step
{
  "current_step": "budget",
  "selected_values": ["mid-range"],
  "user_input": null,
  "state": { ...exact state from previous response... }
}
```

### Selection mode per step

How the user answers each step. "Auto-send" means tapping a chip submits immediately; "multi-select" means the user stages several chips then taps **Continue**. Every step also accepts free text via `user_input` (see Off-Script Inputs).

| Step | Mode | Notes |
|---|---|---|
| `event_details` | Single (auto-send) + free text | `has_date` / `no_date` / `continue`; description typed as `user_input` |
| `event_time` | Single (auto-send) + free text | Presets, or custom like "3pm to 9pm" |
| `already_booked` | Multi-select | `nothing_yet` auto-sends and clears the rest |
| `still_need` | Multi-select | `recommend_all` auto-sends |
| `budget` | Single (auto-send) | `custom` opens the next step |
| `custom_budget` | Single (auto-send) + free text | Preset buttons or typed amount |
| `style_preferences` | Multi-select | Pick one or more style chips **and** any preference chips, then Continue |
| `bundle_action` | Single (auto-send) | |
| `swap_vendor` | Single (auto-send) | One category at a time |
| `remove_category` | Single (auto-send) | One category at a time |
| `add_category` | Single (auto-send) | One category at a time |
| `results_booking` | Single (auto-send) | |
| `partial_booking` | Multi-select | Pick categories to book now, then Continue |

> Distinguishing category chips: on multi-select steps, only the bundle-slot chips (`venue`, `dj`, …) toggle; action chips like `recommend_all` / `nothing_yet` auto-send. The slot keys are the 10 listed under Screen 2.

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

### Screen 1b — Event Time (`event_time`)

**Bot message:** "What time does your event start and end?"

**UI elements:**
- Single-select buttons: **Morning (8am – 1pm)** / **Afternoon (12pm – 6pm)** / **Evening (5pm – 11pm)** / **Full day (8am – 11pm)** / **Not sure yet**
- Optional free-text input for custom times (e.g. "3pm to 9pm")

**Notes:**
- Presets send their value in `selected_values` (e.g. `["morning"]`)
- Free text sends the user's input as `user_input`
- "Not sure yet" stores `time_start: "TBD"` — the booking is created with TBD and can be updated later

**Checklist:**
- [ ] Single-select card/chip UI for presets
- [ ] Optional free-text field for custom times
- [ ] Store returned `state` for next request

---

### Screen 2 — Already Booked (`already_booked`)

**Bot message:** "Before I build your bundle, what do you already have booked?"

**UI elements:**
- Multi-select buttons for each vendor category
- Button: **Nothing yet** (`nothing_yet`)

**Bundle slots (10):** Venue (`venue`) / Catering (`catering`) / Photography (`photography`) / Videography (`videography`) / DJ (`dj`) / Dhol (`dhol`) / Floral & Decor (`floral_decor`) / Makeup & Hair (`makeup`) / Mehndi (`mehndi`) / Cultural Services (`cultural_services`)

> Note: these are chatbot **bundle slots**, not raw vendor categories. Some slots target a subcategory under the hood — e.g. `dj` and `dhol` are both `music_entertainment` vendors, and `makeup`/`mehndi` are both `beauty`. Send the slot key (left of each label) in `selected_values`. The full vendor taxonomy (jewelry, attire, transportation, etc.) is for vendor registration and is browsable via vendor search, not the bundle builder.

**Checklist:**
- [ ] Multi-select chip UI (user can select multiple categories)
- [ ] "Nothing yet" deselects all others
- [ ] Send all selected values in `selected_values` array

---

### Screen 3 — Still Need (`still_need`)

**Bot message:** "What do you want included in your bundle?"

**UI elements:**
- Button: **Recommend everything I still need** (`recommend_all`) — rendered first
- Multi-select buttons for remaining categories (already-booked ones excluded)

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
- Every bundle item is a real, bookable service (`vendor_id` and `service_id` are always set) — placeholder/mock vendors were removed.
- A requested category with no available vendor (no supply, or all booked on the date) is **not** shown as an item. It's listed in `bundle.unfilled_categories` instead — surface a "we couldn't find an available X for your date" note so the user knows it was left out.
- Cheaper/premium regenerates the bundle with a different tier — the full bundle card updates in place.

**Checklist:**
- [ ] Bundle card component with vendor photo, name, rating, price range, match reason
- [ ] Estimated total display
- [ ] Show a note for any `unfilled_categories` (couldn't find an available vendor)
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

**UI elements:** Category buttons for bundle slots NOT already in the bundle

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

**Bot message (book all):** "Your bundle has been created with N booking(s) submitted. Each vendor will review and confirm your request."

**Bot message (partial booking):** "Booked N vendor(s) from your bundle. Each vendor will review and confirm your request."

Both paths return `next_step: "results_booking"`, `is_complete: true`, and empty `helper_buttons`.

**Response fields:**
```json
{
  "bundle_id": "abc-123",
  "booking_ids": ["id1", "id2", "id3"]
}
```

**Checklist:**
- [ ] Check `is_complete == true` to detect that bookings were created (single unambiguous signal)
- [ ] When complete, hide the chips/input and show a "View My Bundle" action
- [ ] Navigate to the bundle detail screen using `bundle_id` (e.g. `GET /bundles/{bundle_id}`)
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

## 3-Bundle Comparison Flow (Optional Alternative Entry Point)

> Optional. The Jorna client today uses only the step-by-step flow above. This section specifies the comparison flow for when it's built — it is not required for the core experience.

Instead of the step-by-step flow, the frontend can show 3 bundles at once using `POST /chatbot/bundles`. This is better for users who already know what they want. **Requires a logged-in user** (Bearer JWT).

**UI:** 3 cards (stacked or horizontally paged on phone) — Budget Bundle / Top Rated Bundle / Balanced Bundle

`POST /chatbot/bundles` returns `{ "options": [ ... ] }` with three `BundleOption`s. Because the caller is authenticated, the backend **persists all three as draft bundles** sharing a hidden comparison group, and each option carries its own `bundle_id`. No vendor is notified at this point.

```json
{
  "label": "Budget Bundle",
  "description": "Best value — quality vendors at the lowest prices",
  "factors": ["Lowest price (primary)", "Style match (tiebreaker)"],
  "bundle": { ... },
  "state": { ... },
  "bundle_id": "abc-123"
}
```

### Selecting a bundle is a commit, not just a UI switch

Each card has a **Select** button that calls `POST /bundles/{bundle_id}/select` with that option's `bundle_id`. This endpoint:
- Deletes the other two draft bundles and their bookings
- Clears the comparison group on the chosen bundle (it becomes a normal draft)
- **Notifies the vendors** on the chosen bundle's bookings (they move into the vendor's pending queue)
- Returns the chosen bundle with its bookings (a bundle object, not a `StepResponse`)

So selecting a comparison bundle effectively books it — treat it as a confirmation action, not a preview. If you want the user to refine before committing, do it *before* calling `/select`: post to `/chatbot/step` with `current_step: "bundle_action"` and the option's `state` (its `next_step` is already `bundle_action`). Only call `/select` once the user is happy.

**Checklist:**
- [ ] Card layout (stacked or paged on phone)
- [ ] Each card shows `label`, `description`, estimated total, and top vendors
- [ ] Display `factors` as small tags/chips under the card title so users understand what's prioritised
- [ ] Confirm before calling `/select` — it discards the other two bundles and notifies vendors
- [ ] After `/select` succeeds, navigate to the bundle detail screen for the chosen `bundle_id`
- [ ] (Optional) Let the user refine an option via `/chatbot/step` at `bundle_action` before selecting
- [ ] All inputs optional — send whatever the user has provided

---

## General Checklist

- [ ] Hold `state` in memory between steps — never store it to disk or a database
- [ ] Gate `/chatbot/step` on login; on `401`, show a "please log in to continue" message instead of an error
- [ ] Show a typing/loading indicator while waiting for step responses (LLM calls can take 2–4s)
- [ ] Handle network errors gracefully — surface a Retry that re-sends the last request without losing state
- [ ] Keep the input bar pinned to the bottom with the chat transcript scrolling above
- [ ] Only the latest turn is interactive — once answered, freeze its chips so the user can't re-tap a past step
- [ ] `is_complete == true` means bookings were created — surface "View My Bundle" and navigate to the bundle detail screen using `bundle_id`
