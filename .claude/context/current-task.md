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
Sections 1 (data model) and 4 (guard audit) are done and committed
(commit `26e71d1`). Section 3 (the actual endpoints — `contracts.py`,
`guest_bookings.py` routers) is **not started**.

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
  executables) — 900 passed, 21 skipped, nothing broken.

## Remaining Work (this repo)

Section 3 of the plan — new `server/app/routers/contracts.py` +
`server/app/services/contract_service.py` (vendor-authenticated: create/
edit a contract, `GET /vendors/me/clients`, `Lead` CRUD, vendor-side
deposit mark/confirm) and a new `server/app/routers/guest_bookings.py`
(fully public/unauthenticated: read-by-token, fill-in-details, sign,
guest-side deposit/payment mark, all rate-limited via the existing
`slowapi` `limiter`). This is the plan's own flagged **highest-uncertainty
step** — reread Section 3's abuse-surface discussion and
`docs/DECISIONS.md` #13 before building it, not just before shipping it.

Once this repo's endpoints exist, the frontend work resumes in
`jorna-vendor` (see that repo's own `current-task.md`) — Contracts builder
UI, the public signing page, deposit-attestation UI, then the pipeline/
Clients/Leads read-only views last.

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
