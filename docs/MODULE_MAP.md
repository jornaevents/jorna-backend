# Module map

Start here to find where a change belongs. Every route lives in a router
under `server/app/routers/`, which delegates business logic to a matching
service under `server/app/services/` — routers stay thin (auth check,
request/response shape), services hold the actual logic. `main.py` (repo
root: `server/main.py`) wires everything together and additionally defines
the `/auth/*` routes directly (not split into a router).

## `server/main.py`

App entry point. Registers all routers, CORS, security headers, rate
limiting (`app/limiter.py`), Sentry init, the six background sweeps
(refresh/reset token cleanup, escrow auto-release, check-in reminders,
message digests, Google Calendar channel renewal, Google Calendar busy-time
re-sync — the first two daily, check-in reminders every 5 min, message
digests every 20 min, calendar channel renewal daily, calendar re-sync
every 4 hours), and the `/auth/*` endpoints
(register, login, Google sign-in, password reset, profile completion,
logout). Also owns a handful of non-API HTML "bounce back to the app"
landing pages Stripe/Google OAuth redirect to (`/payment-complete`,
`/vendor/stripe-onboard/return`, `/reset-password`, etc.) — these exist
because Stripe/OAuth require an `https://` return URL, which the iOS app's
custom URL scheme can't be directly.

## `server/app/routers/` + `server/app/services/` (paired by resource)

| Router | Prefix | Service | Responsibility |
|---|---|---|---|
| `vendors.py` | `/vendors` | `vendor_service.py` | Vendor profiles, search, categories/subcategories, Stripe Connect onboarding status |
| `calendar.py` | `/vendors` (calendar sub-routes) | `calendar_service.py` | Google Calendar OAuth + vendor availability sync |
| `bookings.py` | `/bookings` | `booking_service.py` | Booking lifecycle: create, accept/decline, cancel, status transitions |
| `bundles.py` | `/bundles` | `bundle_service.py` | Grouping multiple vendor bookings under one event; bundle comparison/selection |
| `events.py` | `/events` | `event_service.py` | Client-side event records an accepted bundle attaches to |
| `guests.py` | (no prefix) | `guest_service.py` | Guest list management for an event |
| `checkin.py` | (no prefix) | — (uses `reminder_service.py`) | Day-of check-in confirmation flow + reminder emails |
| `negotiations.py` | `/negotiations` | `negotiation_service.py` | Price/terms back-and-forth between client and vendor before a booking is confirmed |
| `change_requests.py` | (no prefix) | `change_request_service.py` | Post-confirmation change requests (reschedule, scope change) |
| `payments.py` | `/payments` | `stripe_service.py` | Stripe Checkout, Connect payouts, escrow hold/release/auto-release |
| `conversations.py` | `/conversations` | `conversation_service.py`, `message_service.py`, `ws_manager.py`, `pipeline_service.py` | Messaging threads (booking-subject and general), WebSocket delivery, mark-as-unread, "Add to leads" |
| `messages.py` | `/messages` | `message_service.py` | Message CRUD/pagination within a conversation |
| `notifications.py` | `/notifications` | `notification_service.py` | Push (FCM) + in-app notification records |
| `reviews.py` | `/reviews` | `review_service.py` | Post-event reviews/ratings |
| `services.py` (router) | `/services` | `service_service.py` | Vendor-listed services (what a vendor sells, pricing, media, categories, status, inclusions, add-ons, per-package terms — see DECISIONS #14) |
| `feed.py` | `/feed` | `feed_service.py` | Discovery/browse feed |
| `chatbot.py` | `/chatbot` | `chatbot_service.py`, `llm_service.py`, `plan_readiness.py` | Bundle-builder conversational flow (stateless — client holds state, see `docs/API.md`) |
| `moderation.py` | (no prefix) | — | Content reports, user blocks |
| `admin.py` | `/admin` | — | Admin-only endpoints (disputes, moderation review) |
| `users.py` | (no prefix) | `user_service.py` | User profile CRUD outside of auth |
| `contracts.py` (leads routes) | (no prefix) | `pipeline_service.py`, `contract_service.py` | The vendor's leads pipeline (`GET /leads/pipeline`), archiving requests/contracts and leads — see DECISIONS #20 |
| `contract_documents.py` | (no prefix) | `document_service.py` | Addenda and cancellation agreements attached to a signed booking, signed on the public link — see DECISIONS #21 |
| (proposal routes in `contracts.py`, `guest_bookings.py`) | — | `proposal_service.py` | The client's change proposals on an unsigned contract, and the vendor's accept / decline / revise; each side's saved draft — see DECISIONS #23, #24 |
| (negotiation routes in `contracts.py`, `guest_bookings.py`) | — | `field_negotiation_service.py` | Field-by-field contract negotiation in strict turns: per-field accept / counter / keep, settling into revisions, vendor locks, the signing gate — see DECISIONS #26 |
| (PDF routes in `contracts.py`, `guest_bookings.py`, `contract_documents.py`) | — | `pdf_service.py` | Contracts and attached documents as PDF downloads, from the signed snapshot once signed — see DECISIONS #22 |

`auth_service.py` backs the `/auth/*` routes defined directly in `main.py`
(no `auth.py` router file — this is a historical quirk, not a convention to
copy for new resources).

## `server/app/db/`

- `models.py` — every SQLAlchemy model (26 tables: `User`, `Vendor`,
  `Service`, `Booking`, `Bundle`, `Negotiation`, `NegotiationOffer`,
  `ChangeRequest`, `Event`, `EventFunction`, `Guest`, `GuestInvite`,
  `Conversation`/`ConversationMember`, `Message`, `GroupMessage`/
  `GroupMessageRead`, `Review`, `RefreshToken`, `PasswordResetToken`,
  `ContentReport`, `UserBlock`, `PushToken`, `Tag`, `VendorAvailability`,
  `StripeWebhookEvent`).
- `database.py` — engine/session setup, dialect-conditional connect args
  (SQLite vs Postgres), `get_db()` FastAPI dependency.

## Other top-level pieces

- `app/config.py` — every environment variable the app reads, with defaults
  and inline explanation of what each controls (DB, JWT, Stripe, Sentry,
  CORS, Google OAuth, Resend email, Supabase storage). Read this before
  adding a new env var or wondering what one does.
- `app/dependencies.py` — `get_current_user` and other FastAPI `Depends()`
  helpers shared across routers.
- `app/limiter.py` — `slowapi` rate-limiter instance, applied per-route via
  `@limiter.limit(...)` decorators (see `main.py`'s auth routes for examples).
- `app/observability.py` — Sentry init + PII scrubbing. See `docs/ARCHITECTURE.md`.
- `app/models/schemas.py`, `app/models/chatbot_schemas.py` — Pydantic
  request/response schemas (distinct from `db/models.py`'s SQLAlchemy models).
- `app/utils/` — pure helpers: `calendar.py`, `location.py`, `notifications.py`.
- `server/alembic/` — the live migration history (45 revisions). See
  `CLAUDE.md`'s critical rules for why the root `migrations/`/`alembic.ini`
  are a decoy.
- `server/tests/` — one file per feature area, mirrors the router/service
  list above almost 1:1. See `docs/TESTING.md`.
- `ig_scraper/` — standalone Instagram scraper for sourcing vendor leads;
  not imported by the FastAPI app.
