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

## 2. SQLite for dev/test, Postgres for staging and production

**Update 2026-09-28:** a staging Railway environment with its own Postgres
now sits between CI and production (docs/STAGING.md), so every migration
runs against real Postgres before production. The rest of this entry is the
original reasoning for keeping tests on SQLite, which still holds.

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

---

## 12. Escrow disabled for the MVP via an `ESCROW_ENABLED` flag, not deleted

For the leaner MVP, every vendor gets paid off-platform (Venmo/Zelle) — no
Stripe checkout, no held funds, no payout step. Rather than rip Stripe out,
`app.config.ESCROW_ENABLED` (defaults `true`) is a single switch, checked in
four places:

- `main.py`'s boot-time check no longer requires `STRIPE_SECRET_KEY`/
  `STRIPE_WEBHOOK_SECRET`, and skips scheduling the daily escrow-release
  sweep, when the flag is off.
- `booking_service.create_booking` forces every new booking's
  `payment_method` to `"manual"` regardless of what's stored on the vendor
  row — this is the actual kill switch, and it doesn't depend on vendor rows
  being migrated first (see below).
- `vendor_service.update_vendor` rejects setting `payment_method` back to
  `"stripe"` and requires at least a Venmo handle or Zelle contact, but only
  when the update actually touches a payment field — an unrelated save (e.g.
  bio during onboarding, before payment info is ever set) isn't blocked by a
  requirement it isn't trying to satisfy yet.
- `routers/payments.py`'s `_require_escrow` dependency 403s the endpoints
  with no manual-track equivalent: Connect onboarding/status, PaymentIntent/
  Checkout creation, payment sync, event-confirm (fund release), reschedule
  refund, disputes, and saved-card setup/sync/forget. Endpoints that already
  serve both tracks — `cancel`, `cancellation-preview`, `earnings`,
  `mark-paid`, `confirm-received` — are untouched; they already have correct
  manual-track branches (see `docs/DECISIONS.md` #7/#11 for the Stripe-track
  policy they still carry when the flag is back on).

Existing vendor rows with `payment_method="stripe"` are deliberately **not**
backfilled — the booking-time override above makes that unnecessary, and a
bulk UPDATE against production with no staging DB to test it on isn't worth
the risk for a value that's now unreachable anyway. `stripe_service.py`, the
Stripe columns on `Booking`, and the Stripe-only tests are left in place,
untested-in-CI-by-default but otherwise unchanged — flipping `ESCROW_ENABLED`
back to `true` is the entire rollback, no code or schema revert needed.

---

## 13. Guest/contract bookings: no account, ever — a deliberate lower-trust agreement

For the vendor-authored "Contracts" flow (a vendor creates the whole booking
— event, price, terms — for a client who has never used Jorna), the client
fills in their own details and e-signs via a public link with **no login,
no account, ever** — matching a design mockup the product decision was based
on. This is a deliberate tradeoff, not an oversight:

- `Booking.user_id` is nullable; a guest booking has `guest_name`/
  `guest_email`/`guest_phone` instead of a joinable `User`, and a
  `contract_token` (same pattern as `Guest.token` in the RSVP system —
  `secrets.token_urlsafe(24)`, unguessable, not derived from `booking_id`)
  as the public link's entire credential.
- **The token is the entire security boundary.** Anyone who obtains the
  link — forwarded, screenshotted, leaked in a group chat — can read it and,
  more importantly, **sign** it and self-attest payment, with no identity
  verification of any kind. There is no OTP/magic-link infrastructure
  anywhere in this codebase to add cheaply. The accepted mitigation is: a
  long random token, aggressive rate limits on the sign/attestation
  endpoints specifically, and the vendor's own off-platform follow-up
  (phone/text) as the real verification step — consistent with "you'll pay/
  coordinate directly," not an in-app guarantee.
- A guest booking supports **no messaging, negotiation, change requests, or
  client-side GPS check-in** — all of those assume two authenticated `User`
  rows. `message_service.send_message`, `conversation_service.open_booking_
  thread`, and `negotiation_service.start_negotiation` all refuse a
  `user_id IS NULL` booking explicitly (400, not a crash) — a vendor calling
  one of these on their own guest booking used to be let through by a
  `caller_user_id in (booking.user_id, vendor_user.user_id)`-style check
  that treats `None` as a valid match, then failed trying to create a
  message/conversation-member row with a null user id. Change requests need
  no such guard — they're keyed on `Bundle.user_id`, and a guest booking has
  no bundle at all, so `propose()` is already unreachable. Reviews, the
  client side of GPS check-in, and the client-authenticated manual-payment
  self-attestation (`mark_booking_paid`) all already fail closed for a null
  `user_id` for a subtler reason: every one of them compares
  `booking.user_id != caller_user_id` or `== caller_user_id`, and no real
  caller id ever equals `None` — confirmed with tests
  (`tests/test_guest_booking_guards.py`), not just reasoned about.
- *Since #17, a signed-in client's accepted request is also a contract and
  is signed on this same token link — see #17 for how that changed the
  boundary above.*
- **No account-claim / magic-link flow is in scope.** Once signed, a guest
  has no way to log back in and see or manage the booking again — all
  further coordination happens outside the app. This was an explicit,
  accepted product tradeoff, not a gap to quietly fill in later without
  re-raising it.
- Deposit self-attestation (`deposit_marked_paid_at`/
  `deposit_confirmed_received_at`) is a second pair alongside the existing
  `manual_payment_marked_at`/`manual_payment_confirmed_at`, which keep
  meaning "the full/remaining balance" — see `Booking.deposit_percent` and
  friends. A booking with no deposit configured never touches the new pair.

## 14. Package status instead of delete; add-ons as JSON; experience on the vendor (0063)

**Context.** A package (`Service`) was one price and free text. Deleting one
that any booking referenced failed outright (`bookings.service_id` is a
non-null FK), and there was no way to take a package off the listing
without deleting it. Years of experience was required on every package,
though it describes the vendor.

**Decision.**
- `Service.status` ∈ active / hidden / archived. *Active* is listed and
  bookable. *Hidden* is off every client-facing list (search, `GET
  /services`, AI bundles) and not bookable from the marketplace
  (`create_booking` → 409), but the vendor can still use it in a contract —
  a private package. *Archived* is retired: nowhere new, contracts refuse it,
  existing bookings keep their row. `service_service.listed()` is the
  single "a client can see this" filter; by-id lookups for an existing
  booking deliberately don't use it.
- `DELETE /services/{id}` archives instead of deleting when any booking
  references the package (still 204, so older clients see "gone").
- `GET /services?include_unlisted=true` returns hidden/archived packages
  only to the signed-in owner of `vendor_id` (`get_optional_user`).
- `add_ons` and `inclusions` are JSON on the row, not tables: a contract
  will snapshot the add-ons it uses (Phase 2), so nothing joins back to
  them. Each add-on gets a stable `id` so a snapshot can still say which one
  it was after a rename.
- Per-package `deposit_percent` / `cancellation_window_hours` /
  `overtime_rate_cents` override `Vendor.default_*` when set.
- `Vendor.years_experience` (backfilled from the leading number of the
  vendor's `Service.experience` text). `Service.experience` stays required
  in the table for older clients but is optional on create, filled from the
  vendor's years.

**Consequence.** Everything is additive — older iOS/web clients keep
working unchanged and simply don't see the new fields.

## 15. Contracts are offers: a tentative, expiring hold; hard block on signing (0064)

**Context.** A contract was created `APPROVED`, which is what the
double-booking guard counts, so it blocked its vendor's date from the
moment it was written — through being ignored, forgotten, or never sent —
until someone voided it. Nothing recorded whether it had been sent, opened
or turned down.

**Decision** (the user's, 2026-09-23).
- `Booking.contract_status`: draft / sent / viewed / signed / declined /
  voided, only on contract bookings. `Booking.status` stays `APPROVED` for
  a live contract so every existing "is this booking live" reader is
  unchanged; declined and voided are `REJECTED` with `CLIENT_DECLINED` /
  `VENDOR_WITHDREW`.
- A sent (or viewed) contract holds its date **tentatively** until
  `hold_expires_at`: `Vendor.contract_hold_days`, default 7, overridable per
  send (1–60). Signing is the hard block. A draft holds nothing and its
  link 404s. `booking_service.commits_vendor_date()` is the single SQL rule
  for "this booking takes the date", used by the conflict check, calendar
  availability and the bundle builder.
- **Expired is derived, not stored**: a sent/viewed contract past its hold.
  No sweeper, and a resend (`POST /contracts/{id}/send`) just moves the
  deadline — if the date is still free. An expired offer can't be signed:
  the hold lapsing is exactly when the date could have gone to someone else.
  Hold expiry and offer expiry are deliberately the same deadline — one
  date for the vendor to reason about.
- Signing re-runs the double-booking guard (409), catching anything the hold
  didn't — a pre-hold contract, or a hold restored by hand.
- "Viewed" is the client's first read of the link. The vendor's own "View
  as client" passes `?preview=true` so it doesn't count; a forged preview
  only leaves viewed unset, so it isn't a security boundary.
- Sending doesn't email the client — vendors share the link themselves,
  as before. That's for the Phase 2b builder's Send step.

**Migration.** Existing unsigned, un-voided contracts were backfilled as
`sent` with a fresh 7-day hold from deploy time, rather than an age-based
expiry that would have released a batch of vendor dates the moment it
shipped.

## 16. Contracts are proposals: line items, a payment schedule, clauses, a signed snapshot (0065)

**Context.** A contract was one package at one price with one optional
deposit and a couple of free-text terms. Vendors quote several packages,
add-ons and one-off extras, take payment in stages, and need to show later
exactly what a client agreed to.

**Decision.** (`services/contract_document.py` owns the shapes.)
- **Line items are snapshots.** `Booking.line_items` holds packages, add-ons
  and custom lines with the vendor's own price (the catalogue only fills a
  blank). Nothing joins back to `Service`, so renaming, repricing or
  archiving a package never changes a contract. The server computes totals;
  `amount_cents` stays the grand total so every existing reader works.
  `service_id` is the first package — the column can't be null. The old
  one-package create is stored as a one-line contract.
- **A schedule replaces the single deposit** on builder-made contracts.
  Installments must add up to the total to the cent, and are due on
  signing, on a date, or N days before the event. The single-deposit fields
  are **mirrored from the schedule** (`sync_legacy_payment_fields`: the
  first of two or more payments is "the deposit"; marked paid once every
  remaining one is; confirmed once all are), and the old deposit/payment
  endpoints act on installments — so the Bookings page, earnings, the
  pipeline and iOS needed no change. Contracts without a schedule behave
  exactly as before.
- **Clauses are sent text**, `[{key, title, body}]`. The key follows a clause
  across contracts; the text is what's agreed.
- **A signature is for one revision.** Every edit bumps `revision`; the
  client page sends the revision it showed, and signing a stale one is a
  409. Signing freezes `signed_snapshot` (everything agreed, plus signer and
  time) and its SHA-256 — the record to settle a dispute against, rather
  than the live row.
- **Templates are per account** (`contract_templates`), replacing browser
  storage. The body is the builder's own shape; the server never computes
  from it.
- **Timeline** (`contract_events`): created, sent/resent, emailed, viewed,
  edited, signed, declined, voided, payment marked/confirmed. Contracts from
  before this derive one from their timestamps; "expired" is appended on
  read, since it's never stored (#15).
- **Sending can email the client their link** (`email_client`), best
  effort. A vendor can still just share the link.

**Consequence.** Additive: no backfill, and older clients see a scheduled
contract through its deposit fields.

## 17. Marketplace requests become proposals, signed on the no-login link (Phase 4)

**Context.** Two ways to book, two models: a vendor-written contract (#13,
#15, #16) needed the client's signature and could carry a payment plan; a
signed-in client's marketplace request was final the moment the vendor
clicked Accept — no contract, no deposit, no signature, and the date locked
for good.

**Decision** (the user's, 2026-09-23 and 2026-09-27).
- **Accepting a request turns it into a proposal, in place.** The same row
  gets a contract token, line items, a payment schedule and clauses, and is
  sent with the usual tentative hold (#15); the client signs to make it
  final. Messages, bundle and event stay attached because it's the same
  booking.
- **Both ways of accepting do this.** A plain Accept (`PUT
  /bookings/{id}/status` approved — the web Bookings page and iOS) builds the
  proposal from the request and the vendor's usual terms
  (`contract_service.attach_proposal`: the package's own deposit/
  cancellation/overtime over the vendor defaults; deposit on signing and the
  balance 14 days before, or all on signing; default terms as clauses).
  `POST /bookings/{id}/propose` accepts with a document the vendor wrote in
  the builder. `booking_service.check_can_accept` is the one set of checks
  for both.
- A per-guest/per-hour request whose quantity isn't known has no total, so a
  plain Accept 409s and points to the builder.
- **The client signs on the same no-login link as a guest**
  (`/guest-bookings/{token}`), emailed to their account address. The user
  chose this over a signed-in screen knowingly: it works in every client
  app today without changes, and it means **#13's rule "never serve a real
  account's booking off a token" no longer holds** — the rule now is that
  only a booking that has become a contract (`contract_status` set) answers
  to a token. An account booking that never became one has no token.
- **Nothing is owed before signing.** The signed-in "I've paid" endpoints
  (`mark_booking_paid`, `mark_deposit_paid`) refuse an unsigned contract and
  act on the schedule once it's signed.
- A vendor backing out before it's signed voids the proposal, so the link
  says withdrawn.
- Requests accepted before this keep working as they were — no contract,
  nothing to sign.

**Known gap.** A date change (change request) on an accepted-but-unsigned
proposal moves the booking without bumping its revision.

## 18. Payment reminders: the client before and on the day, the vendor when overdue

**Context.** Contracts have payment schedules (#16) with due dates, but
nothing said those dates out loud again after signing. Jorna never holds the
money, so collecting it was the vendor chasing by text.

**Decision** (the user's, 2026-09-28: remind both sides).
- `services/payment_reminder_service.py`, an hourly sweep in `main.py`.
- **Client:** an email 3 days before a payment is due and on the due date,
  linking to their contract page to mark it sent. Nothing ahead of a "due
  when signed" payment — the signed receipt already said so.
- **Vendor:** a push + email (`notify_vendor_contract_event`) once a payment
  is 3 days past due and still not marked sent.
- A payment marked sent or confirmed gets nothing further.
- A date that passed before the contract was signed counts from the signing
  day (`contract_document.effective_due`) — a late-signed contract's
  balance is due, not overdue. Every schedule view carries it as
  `effective_due`, so the client app shows the same date the emails do.
- Each reminder is a `payment_reminder` timeline event, which is also the
  dedupe: the sweep can run any number of times. A sweep that finds a payment
  past several moments at once (a restart, a late signature) sends only the
  latest, not a burst.
- Days are UTC calendar days, like `due_on`.


## 19. A signed contract is never deleted by tidying a plan

- Removing a booking from a plan, swapping its package or deleting the whole
  plan used to delete the booking row outright — and with it the signed
  snapshot, its SHA-256 and the timeline (#16). Unpaid signed contracts
  weren't covered by the money guard (`MONEY_MOVED_STATUSES`), because on the
  Venmo/Zelle track no money "moves" through Jorna.
- `bundle_service._has_signed_contract` now refuses both client delete
  paths. Ending a signed booking is a cancellation, which keeps the record;
  a swap after one is cancelled adds the replacement and leaves it be.
- Deleting an account is refused too, while either side of it has a signed
  contract — cancelled or not — whose event isn't over yet
  (`user_service._contract_still_ahead`: its last day is behind us at the
  venue, the same "over" escrow release uses; no date yet counts as ahead).
  Mid-agreement, the contract is both parties' record, and one of them
  closing their account mustn't take it from the other. Once the event has
  passed, the account can go, and its contracts with it.
- The check sits in those callers, not `_delete_booking_cascade`, so the
  duplicate cleanup (which already keeps the most progressed booking) can't
  be aborted halfway by one.

## 20. One leads pipeline: inquiries and negotiations, archived not deleted (0066)

**Context.** The vendor web app's redesign puts everything before a signed
contract on one Leads page. That list was spread over pending marketplace
requests, unsigned contracts and the informal `leads` table, each read by
the client with its own idea of what "needs you" meant.

**Decision.** `GET /leads/pipeline` (`pipeline_service.py`) owns the list
and its rules, so web and iOS show the same thing:

- **Inquiry** until the contract link has been sent; **Negotiation** from
  then until it's signed (sent, viewed, countered, declined, expired).
  Signed, voided, declined requests, converted and won/lost leads drop out.
- **Needs you**: a new request or lead, a draft not yet sent, a couple's
  counter, or a declined or expired offer. **Waiting**: sent or viewed, the
  vendor's counter outstanding, or a lead marked contacted/quoted.
- **Archive is not a status.** `bookings.vendor_archived_at` and
  `leads.archived_at` only hide an item from the vendor; an archived
  request is still pending to its couple, and unarchiving restores it.
- **Copying the contract link counts as sending it** — the web app calls the
  existing send endpoint without email, so the hold starts either way.
- `bookings.created_at` exists from 0066 on (null before; nothing on an old
  booking says when it was made), so "new this week" and "waiting since"
  work for new requests.
- "Add to leads" (`POST /conversations/{id}/lead`) links a lead to the
  thread and the client (`leads.conversation_id`, `user_id`), and is
  idempotent per thread. "Mark as unread" is per member
  (`conversation_members.marked_unread_at`), cleared by opening the thread.

