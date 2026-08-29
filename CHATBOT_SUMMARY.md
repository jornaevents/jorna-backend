> **Superseded.** This file is kept for history only — current, maintained content lives in docs/API.md's Chatbot / bundle builder section. If this file says something different, trust the newer doc.

# Chatbot Bundle Builder — How It Works

The bundle builder is a guided chatbot flow that collects event details from the user, generates a personalised vendor bundle using real database vendors, and creates real bookings when the user confirms.

---

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /chatbot/start` | None | Returns the opening Step 0 prompt + empty state |
| `POST /chatbot/step` | Bearer JWT | Process one step; returns next prompt + updated state |
| `POST /chatbot/bundle` | None | Single-shot bundle from all inputs at once (not persisted) |
| `POST /chatbot/bundles` | Bearer JWT | Persists three draft bundles to compare |
| `POST /bundles/{bundle_id}/select` | Bearer JWT | Keep one comparison bundle, discard the rest |

### 1. Step-by-Step Flow (`POST /chatbot/start` → `POST /chatbot/step`)
A guided conversation — one question at a time. `/chatbot/start` returns the first prompt; each `/chatbot/step` requires the user to be logged in. The client carries all state between requests; the backend is completely stateless.

### 2. Three-Bundle Comparison (`POST /chatbot/bundles`)
Skip the conversation entirely. Send all inputs at once and receive three bundles to compare side by side. Requires the user to be logged in — the three bundles are persisted as drafts and the user keeps one via `POST /bundles/{bundle_id}/select` (see below).

### 3. Single-Shot Bundle (`POST /chatbot/bundle`)
Same inputs as the comparison endpoint but returns one bundle immediately as a `StepResponse` at `bundle_action`. No auth, nothing persisted — useful for a quick preview that the user can then refine through `/chatbot/step`.

---

## Step-by-Step Flow

### Step 1 — Event Details (`event_details`)
Collects event date, location, and guest count. The user can type free text, click a button, or both.

### Step 1b — Event Time (`event_time`)
Collects the event start/end time. The user picks a preset (Morning / Afternoon / Evening / Full day / Not sure) or types free text like "3pm to 9pm". "Not sure" stores `TBD` so the booking can be filled in later.

### Step 2 — Already Booked (`already_booked`)
The user selects which vendor categories they already have booked. These are excluded from the generated bundle.

### Step 3 — Still Need (`still_need`)
The user picks which categories to include, or selects "Recommend everything I still need" to auto-fill all remaining categories.

### Step 4 — Budget (`budget`)
The user picks a tier: Budget-friendly / Mid-range / Premium / Custom. If Custom is selected, a sub-step collects the specific amount.

### Step 5 — Style & Preferences (`style_preferences`)
The user selects a style vibe (Elegant, Traditional, Modern, Luxury, Fun, Minimal) and preferences (Cultural experience, Highly rated, Local vendors, etc.).

### Step 6 — Bundle Revealed (`bundle_action`)
The backend generates a bundle from real DB vendors and returns it. The user can:
- Keep the bundle
- Swap a single vendor
- Remove or add a category
- Request a cheaper or premium version
- Start over

### Step 7 — Booking Confirmation (`results_booking`)
The user chooses to book the whole bundle or select specific categories.

### Step 7b — Partial Booking (`partial_booking`)
If the user chose "Book only some categories", they multi-select which categories to book now. Unselected categories are skipped — no booking is created for them.

### Step 8 — Done
Real `Bundle` and `Booking` records are created in the database. The response returns `is_complete: true`, `bundle_id`, and `booking_ids`. The frontend navigates to the bundle page. Each vendor is notified and must approve individually.

---

## Three-Bundle Comparison

`POST /chatbot/bundles` returns three bundles from the same inputs, each with a different vendor selection strategy:

| Bundle | Primary factor | Style/preferences |
|---|---|---|
| Budget | Lowest price | Tiebreaker only |
| Top Rated | Highest rating | Tiebreaker only |
| Balanced | Rating 50% + Price 30% | 20% of score |

Each option includes a `factors` array so the frontend can display what's being prioritised.

Because the caller is authenticated, all three bundles (and their bookings) are **persisted as draft `Bundle` records sharing a hidden `bundle_group_id`**, and each option carries its own `bundle_id`. No vendor is notified yet.

To keep one, the frontend calls `POST /bundles/{bundle_id}/select` with the chosen option's `bundle_id`. That endpoint deletes the other two draft bundles and their bookings, clears the group on the survivor (it becomes a normal draft), **notifies the chosen bundle's vendors**, and returns the chosen bundle. Selecting is therefore a commit — it books the chosen bundle. The user can still refine an option through the step-by-step flow starting at `bundle_action` (using the option's `state`) *before* calling `/select`.

---

## Vendor Selection Logic

When generating a bundle the backend:

1. Queries real vendors from the DB for each needed category
2. Filters out vendors with a `pending` or `confirmed` booking on the event date (or within the date range if only a range was given)
3. If `latitude`/`longitude` are provided, filters out vendors whose travel radius doesn't cover the event location
4. Makes one LLM call (OpenRouter, free Llama model) to identify which tags in the DB are relevant to the user's style and preferences — handles niche South Asian terms (bhangra, sangeet, garba) that hardcoded keyword lists would miss
5. Caches the LLM result for 1 hour — only one API call per bundle request regardless of vendor count
6. Falls back to hardcoded keyword matching if `OPENROUTER_API_KEY` is not set
7. Scores each vendor using rating + tag overlap (user tags + Instagram tags) + price alignment
8. Picks the highest-scoring vendor per category
9. Falls back to mock placeholder data if no real vendors exist for a category

---

## Off-Script Inputs

At any step the user can type free text instead of clicking a button. The backend detects this and routes it to Llama 3.3 via OpenRouter. The LLM:
- Answers the question naturally and concisely
- Tries to extract structured intent (e.g. "I want a DJ and photographer" → sets categories and jumps to the right step)
- Stays on the current step if just answering a question
- Always nudges the user back to the current step after answering

The response is identical to a normal step response with `llm_response: true` set. The frontend doesn't need special handling.

---

## How Booking Creation Works

When the user confirms a bundle (`book_all` or `book_some`):

1. A `Bundle` record is created in the DB with `status: "draft"`
2. A `Booking` record is created for each bundle item that has a real `vendor_id` and `service_id`
3. Mock placeholder items (no real vendor) are skipped
4. All bookings start at `status: "pending"` — each vendor must approve individually
5. The response sets `is_complete: true` with `bundle_id` and `booking_ids`
6. The frontend detects `is_complete === true` and navigates to `/bundles/{bundle_id}`

In the step-by-step flow, vendors are notified as soon as the bookings are created. In the three-bundle comparison flow, the draft bookings are created up front but vendor notifications are **held until the user calls `POST /bundles/{bundle_id}/select`** — only the chosen bundle's vendors are notified.

---

## Instagram Tag Enrichment

Vendors link their Instagram username in-app (`PATCH /vendors/me`). The scraper:
1. Runs weekly via cron-job.org (`POST /admin/scraper/run`)
2. Fetches all vendors with an Instagram username linked
3. Scrapes their posts via Apify, extracts relevant hashtags
4. Stores tags in `instagram_tags` (separate from user-inputted tags so vendors keep control of their own tags)

Instagram tags are included alongside user-inputted tags when scoring vendors during bundle generation. A vendor with a `bhangra` tag from Instagram will score higher when a user selects "Cultural experience".

---

## Key API Fields

### `StepResponse` (returned by `POST /chatbot/step`)

| Field | Type | Description |
|---|---|---|
| `next_step` | string | The current step the frontend should render |
| `bot_message` | string | Message to display in the chat UI |
| `helper_buttons` | array | Buttons to render (label + value) |
| `state` | object | Full session state — send this back unchanged with the next request |
| `bundle` | object | Generated bundle (present from `bundle_action` onwards) |
| `is_complete` | bool | `true` when bookings have been created — navigate to bundle page |
| `bundle_id` | string | DB bundle ID (only set when `is_complete` is true) |
| `booking_ids` | array | DB booking IDs (only set when `is_complete` is true) |
| `llm_response` | bool | `true` when response was generated by the LLM fallback |

### `BundleOption` (returned by `POST /chatbot/bundles`)

| Field | Type | Description |
|---|---|---|
| `label` | string | "Budget Bundle", "Top Rated Bundle", "Balanced Bundle" |
| `description` | string | One-line summary of the strategy |
| `factors` | array | Ordered priority factors e.g. `["Lowest price (primary)", "Style match (tiebreaker)"]` |
| `bundle` | object | The generated bundle |
| `state` | object | State to pass into `/chatbot/step` to continue refining (at `bundle_action`) |
| `bundle_id` | string | DB bundle ID of the persisted draft — pass to `POST /bundles/{bundle_id}/select` to keep this one |

### `BundleItem` (inside a bundle)

| Field | Type | Description |
|---|---|---|
| `category` | string | Vendor category key e.g. `"dj"` |
| `vendor_id` | string or null | DB vendor ID — null means mock/placeholder |
| `service_id` | string or null | DB service ID |
| `vendor_name` | string | Display name |
| `pfp_url` | string or null | Profile photo URL |
| `price_min` | float | Minimum service price |
| `price_max` | float | Maximum service price |
| `rating` | float | Vendor rating (0–5) |
| `match_reason` | string | Short description of why this vendor was selected |
