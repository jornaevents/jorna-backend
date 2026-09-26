# Architecture

## App structure

FastAPI app (`server/main.py`) with a router-per-resource layout under
`server/app/routers/`, each delegating to a same-named service under
`server/app/services/` — see `docs/MODULE_MAP.md` for the full pairing.
Request/response shapes are Pydantic models in `app/models/schemas.py` (plus
`chatbot_schemas.py` for the bundle builder); persistence is SQLAlchemy
models in `app/db/models.py`.

Startup (`lifespan()` in `main.py`) validates required env vars are set
(`SECRET_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` — the app refuses
to boot without these), bootstraps `INITIAL_ADMIN_EMAIL` as an admin if set,
creates tables directly (`Base.metadata.create_all`) when running on SQLite
(local/test only — Postgres uses Alembic instead), and starts six
background `asyncio` sweeps that run for the life of the process: expired
token cleanup (daily), escrow auto-release (daily), check-in reminder emails
(every 5 minutes), message-digest emails (every 20 minutes), Google Calendar
push-channel renewal (daily), and a Google Calendar busy-time re-sync safety
net under that channel (every 4 hours).

## Database & migrations

- Local/test: SQLite (`sqlite:///./test.db`), tables created directly at
  startup — no migration needed.
- Production: Postgres (Railway sets `DATABASE_URL`; `app/config.py`
  rewrites the `postgres://` scheme Railway provides to `postgresql://`,
  since SQLAlchemy 2.x dropped the old alias).
- **Migrations live in `server/alembic/`** (62 revisions) and run via
  `railway.toml`'s `preDeployCommand = "python -m scripts.predeploy"`
  (`server/scripts/predeploy.py`, which runs the migration-state guard
  then `alembic upgrade head` in one process) — i.e. **every push to
  `main` applies pending migrations to production Postgres before the new
  app version deploys.** There is no separate staging database this runs
  against first. See `CLAUDE.md`'s "Diagnosing a failed Railway deploy" for
  the decoy root `alembic.ini`/`migrations/` to avoid, and for why this
  isn't a bare `alembic upgrade head`.
- New migration: `cd server && venv/bin/alembic revision --autogenerate -m "..."`,
  then review the generated file before committing — autogenerate misses
  some changes (data migrations, some constraint changes).

## Payments & escrow (Stripe Connect) — currently disabled for the MVP

`app.config.ESCROW_ENABLED` (default `true`) gates the whole flow described
below. For the current MVP it's set `false` in the deploy environment: every
new booking is forced onto the manual Venmo/Zelle track
(`booking_service.create_booking`), a vendor can't select Stripe or leave
both contact fields empty (`vendor_service.update_vendor`), and the
Stripe-only endpoints below 403 instead of reaching Stripe
(`routers/payments.py`'s `_require_escrow`). Nothing here was deleted — see
`docs/DECISIONS.md` #12 for exactly what's gated vs. left always-on
(`cancel_booking`, `cancellation-preview`, `earnings`, `mark-paid`,
`confirm-received` already serve the manual track directly and aren't
touched by the flag). The rest of this section describes the flow as it
exists in code, live again if `ESCROW_ENABLED` is flipped back on.

**Three independent status fields, not one.** Easy to conflate since they all
answer some version of "how far along is this":

- `Booking.status` (`BookingStatus` enum, `app/models/schemas.py`) — the
  *request*: `pending` → (`negotiation_ongoing` ⇄ `pending`) → `approved` →
  `payment_confirmed`, or `rejected` at various points. No separate
  `cancelled` value — a vendor's decline and a vendor's post-approval
  withdrawal both land on `rejected`.
- `Booking.payment_status` (`PaymentStatus` enum, same file) — the *money*,
  tracked separately: `unpaid` → `processing` → `paid` → `released`, or
  `refunded`/`disputed`/`cancelled` along the way, on the protected Stripe
  track; `unpaid` → `marked_paid` → `confirmed_paid` on the manual
  self-reported track (`Booking.payment_method == "manual"`). A booking is
  routinely `payment_confirmed` + `paid` at the same time — that's normal,
  not a conflict; a UI showing booking state needs two pills, not one.
- `Bundle.status` (plain string, `app/db/models.py`) — the *plan* as a
  whole (`draft`/`active`/`completed`/`cancelled`), independent of both of
  the above.

Booking-level fields are what the rest of this section is about. Shape of
the flow:

1. A vendor completes Stripe Connect onboarding (`create_vendor_onboarding_url`
   → `get_vendor_stripe_status` polls completion) before they can be paid.
2. A client pays for a confirmed booking via Stripe Checkout
   (`create_checkout_session`) or a saved card (`charge_saved_card`);
   `PLATFORM_FEE_PERCENT` (env, default 5%) is deducted as the platform fee,
   the rest goes to the vendor's connected account — but **held**, not paid
   out yet (`create_payment_intent`/`create_checkout_session` compute this
   split up front).
3. Stripe webhooks (`handle_stripe_webhook`, verified against
   `STRIPE_WEBHOOK_SECRET`) update booking payment state as events land
   (`_on_payment_succeeded`, `_on_payment_failed`, `_on_account_updated`).
4. After the event, both sides confirm via `confirm_event`, which triggers
   `_release_funds` — this is what actually moves held money to the vendor.
5. If a client never confirms, `auto_release_due` (called by the daily sweep
   in `main.py`) releases funds automatically after a deadline, so a vendor
   isn't held hostage by an unresponsive client.
6. Disputes (`raise_dispute`/`resolve_dispute`) and cancellations
   (`cancel_booking`, `refund_after_failed_reschedule`) are separate paths
   that can interrupt the above at various points — read `docs/DECISIONS.md`
   for the reasoning behind specific edge cases (failed transfers, mid-flow
   cancellations) rather than assuming the happy path above is the only one.
   `cancel_booking` is a full refund within `GRACE_HOURS` (24h) of the
   vendor's acceptance; after that, up to the day before the event, the
   client gets nothing back and the payment splits between the platform and
   the vendor on a linear ramp instead (`cancellation_split` — 99%/1% right
   after grace, sliding to 1%/99% by the day before the event).

`StripeWebhookEvent` (in `db/models.py`) records processed webhook event IDs
for idempotency — Stripe can and does redeliver.

## Contracts — vendor-authored, no-login guest bookings

For the MVP, a vendor can author a whole booking themselves ("Contracts")
for a client who's never used Jorna and never logs in — see
`docs/DECISIONS.md` #13 for the full rationale and accepted risk tradeoffs.

- `routers/contracts.py` + `services/contract_service.py` (vendor-
  authenticated): `POST /contracts` creates a `Booking` with `user_id=None`,
  `status=APPROVED`, and a fresh `contract_token`; `GET`/`PATCH
  /contracts/{booking_id}` view/edit it (PATCH 400s once `signed_at` is
  set — a signed agreement is immutable). `GET /vendors/me/clients` groups
  the vendor's own bookings by `user_id` when present, else by
  `(guest_name, guest_phone)`. `Lead` CRUD (`/leads`) is a separate,
  minimal table for informal off-platform prospects that aren't a
  committed booking yet; `POST /leads/{id}/convert` turns one into a real
  contract (the lead only fills contact fields the vendor left blank).
  Create/convert/PATCH also take optional `guest_name`/`guest_email`/
  `guest_phone`/`location`, and reject a past date or an end before the
  start; a PATCH that moves the schedule re-runs the double-booking guard.
  `POST /contracts/{booking_id}/void` withdraws an **unsigned** contract
  (`status=REJECTED`, `rejected_reason=VENDOR_WITHDREW`), freeing its date —
  before this, an abandoned link held its date forever. Signed contracts
  can't be voided.
- **Offer lifecycle (0064, `docs/DECISIONS.md` #15).** `Booking.
  contract_status` is draft → sent → viewed → signed, or declined/voided;
  "expired" is derived on read (`contract_service.contract_state`), never
  stored. Create sends by default (`draft: true` saves without sending);
  `POST /contracts/{id}/send` sends a draft or resends a lapsed offer,
  restarting the hold. A sent/viewed contract holds its date until
  `hold_expires_at` (vendor's `contract_hold_days`, default 7, or a
  per-send `hold_days`); signed holds it for good. The one rule for "does
  this booking take the vendor's date" is `booking_service.
  commits_vendor_date()` — double-booking, calendar availability and the
  bundle builder all use it.
- `routers/guest_bookings.py` + `services/guest_booking_service.py`
  (fully public, no `Depends(get_current_user)` anywhere): the client's
  side, reached only by `contract_token` — read, fill in contact/venue
  details, e-sign (`POST /guest-bookings/{token}/sign`, which also emails a
  copy of the agreement via `email_service.send_email`), and self-report
  paying the deposit/balance. Signing and each "I've paid" also notify the
  vendor (push + email, `utils/notifications.notify_vendor_contract_event`).
  A voided or declined contract's link still reads (with `status`), but
  every write returns 410; so does signing an expired offer. A draft's link
  404s. The first read marks the contract viewed (`?preview=true` — the
  vendor's "View as client" — doesn't). `POST /guest-bookings/{token}/
  decline` lets the client turn it down (REJECTED + `CLIENT_DECLINED`),
  freeing the date and notifying the vendor. Signing re-runs the
  double-booking guard. Rate-limited more aggressively than most of
  this app (`slowapi`, same `limiter` instance as everywhere else) since
  there's no account behind any of these calls to throttle by identity.
- Deposit self-attestation (`deposit_marked_paid_at`/
  `deposit_confirmed_received_at`, `stripe_service.mark_deposit_paid`/
  `confirm_deposit_received`) is a second pair alongside the pre-existing
  full-balance one (`manual_payment_marked_at`/`confirmed_at`) — a booking
  with no `deposit_percent` set never touches it.
- A guest booking (`Booking.user_id IS NULL`) does not support messaging,
  negotiation, change requests, or client-side GPS check-in — all of those
  assume two authenticated `User` rows. See `docs/DECISIONS.md` #13 for
  exactly which existing code paths needed an explicit guard for this
  versus already failing closed on their own.

## Observability (Sentry)

`app/observability.py`, initialized as the very first thing in `main.py`
(before any router import) so import/startup errors are captured too.
Entirely a no-op unless `SENTRY_DSN` is set — safe to leave unconfigured
locally. When enabled: tags events with `SENTRY_ENVIRONMENT` (auto-inferred
as `production` on a non-SQLite `DATABASE_URL`, else `development`, but
overridable) and `RELEASE` (Railway's `RAILWAY_GIT_COMMIT_SHA`, injected
automatically — no manual release tagging needed). A `before_send` hook
(`_scrub`) strips `Authorization`/`Cookie`/`X-Api-Key` headers and all
cookies from outgoing events, on top of `send_default_pii=False` — defense
in depth against a bearer token or session cookie ending up in an error
report. `SENTRY_TRACES_SAMPLE_RATE` defaults to `0.0` (errors only, no
performance tracing) to stay within free-tier volume.

## Rate limiting

`app/limiter.py` provides a shared `slowapi` limiter instance; routes opt in
per-endpoint with `@limiter.limit("N/minute")` (see the `/auth/*` routes in
`main.py` for the pattern — registration and login are the most tightly
limited, at 3/min and 5/min respectively, since they're the endpoints most
attractive to abuse).

## Chatbot / bundle builder

Stateless by design: `app/services/chatbot_service.py` (+ `llm_service.py`
for LLM-assisted tag scoring when `OPENROUTER_API_KEY` is set, else keyword
matching, and `plan_readiness.py`) never persists conversation state
server-side. The client sends the full `state` object back on every
`/chatbot/step` call. See `docs/API.md` for the endpoint contract.
