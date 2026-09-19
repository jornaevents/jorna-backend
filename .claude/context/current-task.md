# Current Task

> Template for handing off in-progress work to a fresh Claude Code session.
> When a task gets complex or the context window is filling up, fill this in
> and tell the user; a new session can then be started with:
> "Read `.claude/context/current-task.md` and continue the task."
>
> Keep the whole file under ~2,000 tokens. This is scratch state for the
> *current* task, not permanent documentation — don't duplicate content that
> belongs in `docs/ARCHITECTURE.md`, `docs/MODULE_MAP.md`, `docs/API.md`, or
> `docs/DECISIONS.md`; link to those instead of restating them. Overwrite this
> file for each new task rather than appending to its history.

## Goal

Backend half of a vendor-dashboard redesign spanning this repo and a new
frontend repo (`~/Documents/GitHub/jorna/jorna-vendor`, forked from
`jorna-website`). Full plan:
`/Users/yd/.claude/plans/delightful-leaping-starlight.md`. This repo's slice
is Sections 1, 3, and 4 of that plan: the data model, the guard audit, and
the new guest-booking/contracts/clients/leads endpoints.

## Current Status

Branch `feature/vendor-contracts-data-model`, **not pushed, no PR yet**.
Sections 1 (data model), 4 (guard audit), and 3 (the actual endpoints) are
all done and committed (`26e71d1`, `6aa13f7`). This repo's entire slice of
the plan is done — remaining work is all in the frontend repo
(`jorna-vendor`) now: see that repo's own `current-task.md`. **Not yet
committed**: a bug fix (below) found while manually testing the frontend's
new Contract-defaults settings UI — `Vendor.default_*` fields round-trip
through `PATCH`/`GET /vendors/me` now; staged as an uncommitted change on
this same branch, waiting on user go-ahead to commit.

## Bug found + fixed this session: `Vendor.default_*` never round-tripped

Manually testing `jorna-vendor`'s new "Contract defaults" settings section
(saves via `PATCH /vendors/me`, reads via `GET /vendors/me`) showed the UI
reporting "Saved" but the values never coming back on reload. Root cause was
a triple gap, all three layers missing the six `default_*` fields
independently:
1. `UpdateVendorRequest` (`app/routers/vendors.py`) didn't declare them, so
   Pydantic silently dropped them from the request body before any service
   code ran.
2. `update_vendor`'s (`app/services/vendor_service.py`) field-write
   allowlist didn't include them either — belt-and-suspenders gap.
3. `get_vendor`/`get_my_vendor`'s hand-built response dicts
   (`app/services/vendor_service.py`) didn't return them, so even a correct
   write would never have been visible.
Fixed all three (response fields added only to `get_my_vendor`, not the
public `get_vendor`, since these are private seed values for the vendor's
own Contracts builder — not client-facing). Added
`tests/test_vendor_contract_defaults.py` (2 new tests: round-trip, and
absent-by-default). Full suite: 928 passed, 21 skipped (was 926 before this
fix's 2 new tests). Re-verified manually end-to-end via the frontend's
`/vendor-profile` Contract-defaults form → reload → `/contracts/new`
pre-fill, all correct now.

## What Was Done

- Five additive Alembic migrations (`0058`–`0062`): `bookings.user_id`
  nullable + guest contact fields + `contract_token`; contract terms
  (deposit %, cancellation window, overtime/addon rates, `contract_terms`
  JSON) + `signer_name`/`signed_at`; a second self-attestation pair
  (`deposit_marked_paid_at`/`deposit_confirmed_received_at`, mirroring the
  existing manual-payment pair); `Vendor.default_*` contract-defaults
  columns; a new `leads` table.
- `PaymentStatus` enum (`app/models/schemas.py`) gained
  `DEPOSIT_MARKED_PAID`/`DEPOSIT_CONFIRMED_PAID`.
- Guard audit: `message_service.send_message`,
  `conversation_service.open_booking_thread`, and
  `negotiation_service.start_negotiation` now explicitly 400 on a
  `user_id IS NULL` booking. Everything else (change requests, reviews,
  client-side check-in, `mark_booking_paid`) was confirmed to already fail
  closed with no code change — see `docs/DECISIONS.md` #13 for the full
  reasoning and `tests/test_guest_booking_guards.py` for the regression
  tests that lock it in.
- Also fixed a pre-existing structural bug in `docs/DECISIONS.md`: an
  earlier session's edit had spliced entry #12 into the middle of entry
  #11's own prose. Restored #11 as one contiguous block; #12 and the new
  #13 now sit cleanly after it.

## Verification

- Full migration chain (all 62 revisions, including the 5 new ones) applies
  **and downgrades** cleanly against a real local Postgres 16 (throwaway
  instance via Homebrew's `postgresql@16`, not Docker — Docker isn't
  installed in this environment). Torn down after verifying; nothing
  persists locally.
- `venv/bin/python -m pytest -q` (note: use `venv/bin/python -m X`, not
  `venv/bin/X` directly — the venv's script shebangs still hardcode this
  repo's pre-reorg path, `.../GitHub/Desiconnect/...` instead of
  `.../GitHub/jorna/Desiconnect/...`, and are broken as standalone
  executables) — 928 passed, 21 skipped, nothing broken (as of the
  `default_*` fix above).

## Remaining Work (this repo)

None for the plan as currently scoped. Possible later follow-ups, not
blocking: (a) push this branch and open a PR once the frontend catches up
enough to demo end-to-end — check with the user first, same as the
`ESCROW_ENABLED` work's pattern; (b) the plan's Section 3 table originally
called for a vendor-side "confirm-deposit-received" restricted to
non-guest bookings — on closer reading during implementation this was
loosened, correctly: `confirm_deposit_received` only ever checks the
vendor's identity, never `booking.user_id`, so it already works for guest
and real-account bookings alike with no separate guest-only path needed;
documented in the commit message and `docs/ARCHITECTURE.md`, not a gap.

Frontend work resumes in `jorna-vendor` (see that repo's own
`current-task.md`) — Contracts builder UI, the public signing page,
deposit-attestation UI, then the pipeline/Clients/Leads read-only views
last. It talks to this repo's new endpoints
(`POST /contracts`, `GET/PATCH /contracts/{id}`, `GET /vendors/me/clients`,
`/leads*`, `/guest-bookings/{token}*`, `/payments/bookings/{id}/{mark,confirm}-deposit-*`).

## Notes for the Next Agent

- No PR open yet for this branch — check with the user before pushing/
  opening one, same as the pattern from the recently-shipped
  `ESCROW_ENABLED` work (branch, PR, wait for explicit "merge" before
  landing — this repo auto-deploys to Railway with production migrations
  on every push to `main`, no staging DB).
- The email-receipt-on-signing decision (send via the existing Resend
  integration, `RESEND_API_KEY`/`EMAIL_FROM` in `app/config.py`) means
  `guest_email` should be validated non-empty before the sign endpoint
  succeeds, not left truly optional.
