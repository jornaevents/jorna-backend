# Decisions

Why things are built the way they are. Check here before "fixing" something
that's actually deliberate.

## 1. Backend and both frontends are separate repos

Three repos (`Desiconnect`, `front_end_desiconnect`, `jorna-website`), three
independent deploy targets (Railway, App Store/TestFlight, Cloudflare Pages).
This backend is the schema/business-logic source of truth for both clients —
see `CLAUDE.md`. Tradeoff: a booking/pricing/escrow change usually needs
coordinated PRs across repos, and client docs describing this API can drift
from what the code actually does (this is exactly what `docs/API.md`
existing here, as the canonical version, is meant to reduce).

## 2. SQLite for dev/test, Postgres for production — no separate staging DB

`app/config.py` defaults `DATABASE_URL` to a local SQLite file when unset.
Tests (`server/tests/test_api.py`) hardcode `sqlite:///./test.db` rather than
requiring a running Postgres — keeps the suite fast (~9s for 700+ tests) and
runnable with zero setup, including in CI. The cost: migrations only ever get
tested by actually running against production Postgres on deploy (see
`CLAUDE.md`'s Railway auto-deploy warning) — there's no dry-run environment.
Treat any migration as higher-risk than an ordinary code change for this
reason.

## 3. Two `alembic` setups exist; only `server/alembic/` is live

The root `alembic.ini`/`migrations/` predate the code being organized under
`server/` and were never deleted. Deleting them now is safe (nothing
references them — `railway.toml` points at `server/`) but hasn't been done
so this note exists instead; feel free to delete them in a cleanup PR if
you're already touching this area, but don't assume they're wired up if you
see them.

## 4. Root `src/`/Vite files are a dead Figma-Make prototype, not the web app

Predates `jorna-website` (the real, deployed Next.js frontend) and was never
removed. Same as #3 — safe to delete, not yet done, don't assume it's live.

## 5. Access tokens are short-lived and held in memory only; refresh tokens are longer-lived and rotate on use

See `docs/API.md`'s Auth section for the mechanics. The asymmetry (60 min
memory-only vs 30 day persisted-but-rotating) is deliberate: it bounds how
much damage a stolen access token can do (short window, never touches disk)
while keeping the actual attack surface — the refresh token — single-use per
rotation, so a stolen-then-used refresh token is detectable (the legitimate
client's next refresh attempt fails).

## 6. `/auth/forgot-password` always returns 200

Prevents using the endpoint to enumerate registered email addresses. Don't
"fix" this to return 404 for unknown emails.

## 7. Escrow auto-releases after a deadline if the client never confirms

`auto_release_due` (daily sweep, `docs/ARCHITECTURE.md`) exists so a vendor
who did the work isn't held hostage by a client who simply never opens the
app again. It's idempotent by filtering on booking status, so running it
more or less often than daily wouldn't change behavior, only latency.

## 8. Chatbot state is entirely client-held

The bundle-builder backend has no session store — every `/chatbot/step`
response's `state` is round-tripped by the client on the next call. This
keeps the backend stateless and horizontally scalable with zero session
affinity concerns, at the cost of every request needing the full state
payload. Don't add server-side session storage for this without a specific
reason (e.g. state size becoming a bandwidth problem) — it was avoided
deliberately.

## 9. CI lints for errors only (`E9,F821,F822,F823`), not full ruff defaults

Full default ruff rules currently flag ~830 pre-existing issues, almost all
style (unused imports, f-strings without placeholders) — not bugs. Gating CI
on all of them on day one would make the check something people learn to
ignore rather than trust. The narrow selection (syntax errors + undefined
names) currently passes clean and catches the class of error most likely to
actually break something. Widening this (or adding a formatter) is a
reasonable follow-up once the existing style issues are cleaned up
separately — see `docs/TESTING.md`.

## 10. The old root-level `AUTH_FLOW.md`, `BOOKING_FLOW.md`, `CHATBOT_FRONTEND.md`, `CHATBOT_SUMMARY.md`, `VENDOR_CATEGORIES.md` are superseded, not deleted

Their still-accurate content was folded into `docs/API.md` (auth token
mechanics, chatbot endpoints, booking/bundle flow, vendor categories) and
this file. They're kept for history rather than deleted outright — each now
has a pointer at the top to where its content lives now. Don't treat them as
current; if one says something `docs/API.md` doesn't, the newer doc wins.

## 11. A cancelled booking's money splits on a linear ramp, not a flat cutoff

`stripe_service.cancel_booking`/`cancellation_split` replaced the old flat
`request_refund` (full refund within 24h of payment, nothing after). The
new shape: full refund for `GRACE_HOURS` (24h) after the *vendor accepts*,
then — instead of refunds simply stopping — the client's payment splits
between the platform and the vendor on a ramp from 99%/1% right after grace
to 1%/99% by the day before the event.

The ramp exists because a flat cutoff treats "cancelled an hour after the
window closed" the same as "cancelled the day before the wedding," and the
vendor's position in those two cases isn't remotely the same — the closer to
the event, the less realistic it is they can fill the date with other work,
so the policy shifts to protecting them rather than the platform's take as
the date approaches. The platform keeps the larger share early (when a
cancellation is still relatively low-cost for the vendor to absorb) and the
smaller share late (when it isn't).

Vendor-initiated cancellation of an already-accepted, paid booking is a
separate, deliberately asymmetric rule: always a full refund to the client,
at any point, no ramp — a vendor backing out of a commitment forfeits their
share entirely, unlike a client changing their mind.

The existing flat 90/10 reschedule-decline refund
(`RESCHEDULE_CANCELLATION_PCT`, `refund_after_failed_reschedule`) is
deliberately untouched by this — that's "the vendor can't meet a new date
the client asked for," not a cancellation, and doesn't reuse the ramp.
