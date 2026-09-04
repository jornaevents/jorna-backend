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
- **Migrations live in `server/alembic/`** (45 revisions) and run via
  `railway.toml`'s `preDeployCommand = "alembic upgrade head"` — i.e.
  **every push to `main` applies pending migrations to production Postgres
  before the new app version deploys.** There is no separate staging
  database this runs against first. See `CLAUDE.md` for the decoy root
  `alembic.ini`/`migrations/` to avoid.
- New migration: `cd server && venv/bin/alembic revision --autogenerate -m "..."`,
  then review the generated file before committing — autogenerate misses
  some changes (data migrations, some constraint changes).

## Payments & escrow (Stripe Connect)

All in `app/services/stripe_service.py`. Shape of the flow:

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
