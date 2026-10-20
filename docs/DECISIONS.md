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

## 21. The document editor's layout; addenda and cancellations as signed attachments (0067)

**Context.** The vendor web app's new contract editor reads like a written
agreement: a title, free-text terms sections, and structured blocks (parties,
event, line items, payment schedule, signature) in an order the vendor
chooses. Vendors also asked for an addendum and a cancellation agreement
after a booking is signed.

**Decision.**

- `bookings.document_title` and `bookings.document_layout` store the
  editor's title and block order. A terms block's text is kept once, in
  `terms_clauses` (key = block id); the layout holds only `{id, type}`, so
  the signing page and iOS, which read clauses, keep working unchanged.
  Each structured block may appear at most once. Both freeze into the signed
  snapshot.
- `contract_templates.kind` is `agreement` | `addendum` | `cancellation`.
- **Addenda and cancellations are text-only documents** (`contract_documents`,
  `document_service.py`), attached to an agreed booking: a signed contract, or
  an approved/paid marketplace booking. They carry no price, date or hold, and
  signing one changes nothing on the booking. Any change still goes
  through the normal booking flows. The couple signs on the same no-login page
  (`/booking-link?d=<token>`), with the same frozen snapshot plus SHA-256 as a
  contract. A draft's link 404s until it's sent; a signed document can't be
  voided; declining or voiding makes the link 410.
- Sends and signatures go on the booking's timeline (`document_created`, `document_edited`, `document_emailed`, `document_viewed`,
  `document_sent`, `document_signed`, `document_declined`, `document_voided`).

## 22. Contracts and attached documents download as PDFs, drawn server-side

**Context.** Couples and vendors want a file of what they signed, to keep,
forward or print, and the web page alone isn't one.

**Decision.** Four download routes, all rendered by `pdf_service.py` with
fpdf2:

- `GET /contracts/{id}/pdf` and `GET /contract-documents/{id}/pdf` for the vendor.
- `GET /guest-bookings/{token}/pdf` and `GET /guest-documents/{token}/pdf` for the couple, by their link.

- **A signed PDF is drawn from the frozen snapshot**, not the live booking.
  It prints the signer, the time and the SHA-256, so the file can be checked
  against the record. An unsigned one is the current version, labelled as
  not signed.
- **Server-side, not the browser's print dialog.** The same file comes out
  everywhere, and an email receipt can attach it later.
- **fpdf2, not an HTML renderer.** It's pure Python with no system packages
  on the Railway image.
- **DejaVu Sans is bundled** (`app/assets/fonts`, with its licence), because
  the built-in PDF fonts are Latin-1 only. It doesn't shape Indic scripts, so
  a name typed in Devanagari prints with its characters unjoined. Shaping
  would need HarfBuzz, which isn't worth it yet.
- A draft has no public PDF, the same as its link. Downloading doesn't mark
  anything as viewed.

## 23. The client proposes changes to the contract; the vendor accepts, declines or revises (0068)

**Context.** A client's request becomes an inquiry, and the vendor's
contract makes it a negotiation. Until now the only thing a client could
push back on was price, through a separate counter-offer on the request
(the Negotiation tables). Everything else meant messaging the vendor and
waiting for them to edit. The user wanted one place to negotiate: the
contract itself.

**Decision.**

- **The client suggests edits to any term**, by their link: event details,
  line items (quantity, price, removing one, asking for something extra),
  the payment schedule, the cancellation window and overtime rate, and the
  clause text. A proposal gets the same checks as a vendor's edit: the
  payments add up to the total, and every clause has a title and text.
- **The contract is always the vendor's position.** The vendor answers:
  - **Accept**: the client's terms become the next revision.
  - **Decline**: the current version stands, with a note.
  - **Revise**: `PATCH /contracts/{id}` with `proposal_id`; the vendor's own
    edit is the answer.

  Accept and Revise send the new version back with the hold restarted, as a
  resend does. Decline leaves the hold as it was. There is no counter-proposal
  object; the client always signs a version the vendor sent.
- **One open proposal per contract.** A new one replaces it (superseded).
  So does any change to the version it was based on: a vendor edit that
  doesn't answer it, or voiding. Signing withdraws it, because the client
  signed the version on the table. Declining the contract withdraws it too.
- **The date hold keeps running while a proposal is open.** Proposing
  isn't a way to hold a date indefinitely, and the vendor's answer restarts
  it anyway.
- **A changed date is checked for overlaps on Accept**, through the same
  edit path (`contract_service.apply_update`) as a vendor's edit. A clash is
  a 409, and the proposal stays open.
- **Every version is kept** in `contract_revisions` (from 0068 on, plus the
  version a client was reading when a proposal or edit first touches an
  older contract), so either side can see what changed between two.
- **Price-only counters are retired** (`negotiation_service.NEW_NEGOTIATIONS_OPEN`).
  Starting one is a 410. A counter already open can still be answered, and
  the endpoints stay, so older iOS builds keep working.
- **Leads pipeline:** an open proposal is `needs_you` / `changes_proposed`,
  ahead of everything else. A version sent back in answer is `waiting` /
  `revised`.

**Out of scope.** Changing a signed contract still goes through an
addendum or cancellation agreement (#21). iOS doesn't show or send
proposals yet.

## 24. The negotiation workspace saves drafts on the server (0069)

**Context.** The vendor app's negotiation workspace (from the Figma Make
design) shows the contract with the other side's changes in red, and lets
each side type the values they want to send back. It has a Save draft
button for both sides. A draft kept in the browser would be lost on another
device, and the user wants real data everywhere.

**Decision.**

- **One draft per contract per side**, in `contract_drafts` (`party` is
  `vendor` or `client`). Saving replaces it. Each side reads only its own,
  in the `draft` field of its proposals list.
- **A draft is checked for shape only.** It may name only terms fields and
  must stay under 100 KB. The real checks run when it's sent, as a proposal
  or as the vendor's revision.
- **Sending spends it.** A client's proposal drops the client's draft. A
  vendor's Accept, Decline, Revise or plain edit drops the vendor's.
- **A stale draft is kept and flagged**, not deleted, when the contract moves
  past the revision it was made against. The workspace asks the person to
  look again instead of laying old values over new terms.
- **The vendor's draft remembers the proposal it answers**
  (`proposal_id`), so Send knows whether it's a Revise.


## 25. A vendor's usual payment schedule (0070)

`Vendor.default_payment_plan` is `{"preset": "full" | "deposit_balance" |
"three", "balance_days_before": int}`, set from the web Contracts page's
Defaults panel. The builder starts new contracts from it, and "Send with my
usual terms" (`contract_service._default_schedule`) builds its schedule from
it: the vendor's usual deposit (else half) for a deposit plan, thirds for
three payments, and the final balance `balance_days_before` the event.

**Why one field, not a stored schedule.** Amounts depend on each contract's
total, so what a vendor really has is a habit (which preset, when the balance
falls due), not a list of payments. The builder's presets already turn that
into payments.

**Null keeps the old rule.** Deposit + balance two weeks out when the vendor
takes a deposit, otherwise pay in full on signing, so vendors who never open
Defaults see no change.

## 26. Contracts negotiate field by field, in strict turns (0072)

Replaces #23's whole-proposal model for new contracts, behind
`FIELD_NEGOTIATION` (off until the web workspace ships). Spec, with the
diagrams: "Contract Negotiation — Logic Spec" (2026-10-04).

- **A field is one value** with a stable key (`event.date`,
  `line:<id>.price`, `clause:<key>.included`, …), the same ids the web
  app's `lib/negotiation` uses. `negotiation_fields` holds only fields
  that aren't plainly agreed: waiting on the vendor, waiting on the client,
  or settled.
- **Each side answers each field**: accept, counter, or keep (a counter
  with the agreed value). Keep doesn't end the discussion; the other side
  can ask again.
- **Strict turns.** `negotiation_turn` says whose move it is, and a send
  must answer everything waiting on the sender. The client can't withdraw a
  send. No round limit; the page nudges from round 6.
- **The contract only holds agreed values.** Accepting writes the value in
  as a new revision through the vendor-edit path (overlap check, totals),
  so the signed copy is always one whole version. When the total moves, the
  schedule is rescaled in proportion.
- **What the client may change:** event details, items and prices,
  policies, and whether a clause is included. Not clause text and not the
  payment schedule. Nothing in a group the vendor locked
  (`Vendor.negotiation_locks`, copied onto the contract at its first send,
  so a later Defaults change doesn't move a live negotiation). New items
  only from the vendor's own packages and add-ons.
- **Settled fields lock**; only the vendor can reopen one, which starts a
  new round.
- **Signing** needs nothing waiting, or `as_is`, which takes the vendor's
  open changes and drops the client's own.

**Why one table of fields, not a proposal per side.** Answers are per
field and some settle while others stay open, so the open state has to
live per field. A send is still kept whole (`negotiation_sends`) for the
timeline.

**Rollout.** Contracts already mid-negotiation keep #23. Once none are
open, the flag becomes the default and the proposal routes go read-only.
iOS follows in its own release.


## 27. A signature by link carries its own evidence (0073)

A typed name on a token link is only as good as what we can show about it
later. Each signature, of a contract or an attached document, now records:

- **Where it came from.** IP address and user agent, from the request
  (`app/utils/request_meta.py`). uvicorn trusts Railway's
  `X-Forwarded-For` (`entrypoint.sh`); before that, every request looked
  like Railway's edge, which also made the rate limiter count all
  visitors as one.
- **Consent to electronic records** (ESIGN §7001(c)). The words live once,
  in `app/services/esign_consent.py`. The guest GETs return them
  (`esign_consent`), and the signature stores the exact text and its
  version. Change the words → bump the version; a page holding an older
  version is refused (409). The wording waits on counsel.
- **Proof of the email.** `POST …/signing-code` emails a 6-digit code to
  the email on the contract (`signing_codes`, 0073; HMAC hash only,
  10 minutes, 5 tries, a new code replaces the old). It's bound to that
  address, so changing the email voids it. The code is checked after every
  other check on the signature, so a refusal for another reason doesn't
  spend it.

All of it goes into the signed snapshot as `evidence`, so the SHA-256
covers it. The PDF gets a last page, a signing certificate: the evidence,
the consent text, and the history (sent, opened from, code sent, signed
from, copies emailed). Both sides are emailed the signed PDF, and each
send is a `copy_sent` event with its Resend id.

**Rollout.** `SIGNING_REQUIRE_CODE` is off until the web page that asks
for the code ships. While off, an older page still signs; whatever it sends
is checked, and the evidence records what was missing
(`email_verified_at: null`, `consent: null`). `SIGNING_TEST_CODE` lets the
staging E2E suite sign without reading email, and is ignored when
`RAILWAY_ENVIRONMENT_NAME` is production.

**Why a code and not sign-in.** Sign-in to sign (claim-by-link) waits on
legal. A code works on today's link, proves control of the email the
signed copy goes to, and stays useful after sign-in ships.

**The vendor sees the evidence only in the PDF.** Their app shows no client
IPs.

**The typed name is checked, not enforced** (2026-10-13). The evidence also
has `name_on_contract` and `name_matches` (`signing_evidence.name_check`):
case, punctuation and spacing don't count, and the same first and last word
is a match, so a middle name or initial passes. A mismatch still signs. A
partner, a parent or a nickname is a real signature, and the code already
shows who controls the email. It's recorded instead: in the snapshot, on the
`signed` / `document_signed` event (`name_on_contract`), and as a line on the
certificate. The page warns before signing.

**The signing form can't move the terms** (same day). `fill_details` lets
the client fill in a venue or guest count the vendor left blank. Changing a
set value is a 409; it goes through negotiation (#23, #26), where the vendor
answers it. A change to the client's own name, email or phone, where one was
already set, is a `details_updated` event on the timeline and the
certificate. Otherwise, renaming yourself just before signing would hide a
mismatch.

## 28. What a contract can say, and how leads end (0074)

From the Oct 5 staging walkthrough and the product review (2026-10-14).

**Payment dates.** A payment due before today is refused (400) when the
vendor writes or edits a schedule, accepts a request with one, or first
sends a draft (`contract_service._check_schedule`, one day of UTC slack like
`_check_dates`). "400 days before" an event 250 days out had been saved and
signed with a due date months gone. A payment due *after* the event is
allowed: a final balance on delivery is ordinary, and the editor only warns.
Moving the date alone doesn't re-check a schedule, and neither does a
resend: "days before" payments move with the date, a client's date ask has
no say over the schedule, and a payment whose day has slipped past is due on
signing (`effective_due`). The vendor's usual schedule counts back no further
than today, so a request for an event next week isn't born overdue.

**Holds end at the event.** `_start_hold` caps the hold at the event's start.

**Negotiation values.** A price of $0, or a send that would leave the total
at $0 or less once every open value is agreed, is refused (400). Guest
counts were already limited to 1–100,000.

**Leads' details are checked.** An email that isn't one, a phone that isn't
7–15 digits, or a past event date is a 422 on `POST/PATCH /leads`.

**Whose turn, in one field.** Contract and booking payloads carry
`negotiation` (`field_negotiation_service.summary`): the turn, the round and
how many changes wait on each side. The pipeline reads it, so a contract
with the client's asks waiting is "needs you" on Leads, the contract page and
Overview alike. Before this, only the proposal flow's `proposal_status` was
read, and a field-by-field ask showed as "waiting on client".

**A decline can say why** (0074, `vendor_decline_note`). The vendor's
optional note goes to the client in an email and on their plan. Declining
always emailed nothing before; the client saw "Not going ahead" with no
context.

**Closed leads are archived, not dropped.** A declined request, a request
the client cancelled, a declined contract and a lead marked lost stay in the
pipeline, archived, with an `outcome`. They had vanished, so a vendor
couldn't look back at what they lost. Voided contracts and converted leads
still leave: those are the vendor's own undo or a booking.

**A client cancelling a sent request keeps the row.** Removing a booking from
a plan that has been sent now marks it `rejected` / `client_cancelled` with
`cancelled_at`, and voids an unsigned contract's link, instead of deleting
it. The vendor sees it archived; the client's plan shows it ended. Removing
it again, or removing one from a draft plan, deletes it as before.

**Price haggling stays retired.** `POST /negotiations` was already 410
(`NEW_NEGOTIATIONS_OPEN`). The web apps stop offering "open to offers" and
the plan's price panel; `Service.negotiable` and the vendor's
`open_to_price_negotiation` columns stay, unused.

## 29. The client's link asks for an email first; a new one is told to the old (2026-10-15)

The client's contract link now runs as steps — email, details, review, sign — in the web app
(its DECISIONS entry of 2026-10-15). The link is still the only credential (#13), and the
signing code is still asked at signing, every time (#27). What the backend adds:

- **A different email is allowed.** `PATCH /guest-bookings/{token}` takes it, and the
  `details_updated` event names it (`email_to`), so the vendor's timeline and the signing
  certificate say where the client now is.
- **The old address is told.** Anyone with the link can change the email, and the signing code
  then goes to the new inbox. A short notice to the old address — the new one masked — means
  the real client hears about a change they didn't make. Best-effort, like every email here.
  A first email, or the same one again, sends nothing.
- **A malformed email is refused** (422), the same loose check a lead's email gets (#28).
- Sending changes already required an email (field negotiation and proposals both 400 without
  one), so the steps change nothing there.

## 30. A signed contract is new to the vendor until they open it (0075)

A lead becomes a booking the moment the client signs — the vendor does nothing, so it's easy
to miss. `bookings.vendor_seen_at` records the first time the vendor opens it on their Bookings
page (`POST /bookings/{id}/seen`), and `GET /bookings/vendor/new-count` is the Bookings tab's
badge: signed, not yet opened, not voided.

- **Signed, not accepted.** A marketplace request the vendor accepts was their own act; only a
  client's signature arrives on its own.
- **Kept on the booking, not the browser**, so opening it on a phone clears the badge on the
  laptop.
- **Bookings signed before 0075** are marked seen as of their signature, so the badge starts
  at zero instead of lighting up with every booking a vendor has had.

## 31. Every package has a contract template; the vendor's edit is stored against it (0076)

Most contracts start from one package. The vendor's "Send a contract" now asks which template to
start from, and every package has one by default (web DECISIONS, 2026-10-15).

- **Built, not stored, until edited.** A package's template is made by the web from the package
  (its price, hours and what's included) and the vendor's contract defaults, so a price change
  reaches it with nothing to sync and nothing to backfill for existing vendors.
- **The edit is stored.** Once the vendor edits and saves it, it's a `contract_templates` row with
  `service_id` set, and stops following the package. One per package (a unique constraint on
  vendor + package; ordinary templates have none). Reset to package deletes it.
- **Only the vendor's own package, and only an agreement.** An addendum or cancellation template
  isn't a package's.
- **A deleted package** leaves its edited copy as an ordinary template (`ON DELETE SET NULL`), so
  the vendor's wording isn't lost. Archived packages are the web's to hide.

## 32. Anyone can send a vendor an inquiry, and it lands in Leads (0077)

`POST /vendors/{vendor_id}/inquiries` takes an inquiry with no account: from the vendor's Jorna
page or a link they share (user, 2026-10-17; backend #66). It makes a lead with
`source: inquiry_form`, status `new`, so it's "needs you" in the pipeline and counts on the Leads
badge with nothing else to wire up.

- **Same trust model as `/guest-bookings`**, but there's no token, so the guards are on the vendor
  and on abuse. A vendor with no package, or only archived ones, is a 404: still in setup, not
  taking inquiries. A hidden package counts.
- **Short form.** Name and email are required (the vendor has to be able to answer); phone, event
  date (blank is "not sure yet"), event type, rough guest count and a message are optional. The
  checks are the Add a client ones (#28), plus a guest count of 1–100,000. Leads gain
  `event_type`, `guest_count` and `source` (`manual` | `messages` | `inquiry_form`), which the
  vendor's own leads use too.
- **Spam: a honeypot, 5 a minute per IP, 50 a day per vendor.** A filled-in hidden `website` field
  gets the normal reply and nothing is saved or sent, so a bot learns nothing. The daily cap
  answers 429 "try again tomorrow". A CAPTCHA (Turnstile) waits until spam actually shows up.
- **The reply says nothing about the lead**: `{received, vendor_name}` only.
- **Both sides are emailed.** The vendor gets the inquiry with Reply-To set to the sender. The
  sender gets fixed wording and the vendor's name, never anything they typed (not even their own
  name), and at most once per address per vendor per day — otherwise the form would send any text
  to any inbox. Both are best effort; the lead is saved either way.
- **Not yet:** asking for a specific time (needs real availability), an embeddable widget, and a
  setting to turn the form off.

## 33. Earnings is a ledger: one row per payment, tied to its contract

The vendor's Earnings page becomes the record of every booking, past and present, and every
payment on it, each linked to the signed contract that set the amount (user, 2026-10-17; spec
https://claude.ai/artifact/3iXq72ZaidefdZZ2gbGvkm). `GET /vendors/me/ledger?year=` serves it
(`ledger_service.py`); no migration.

- **A row is a payment on a signed, unvoided contract.** A booking from before contracts (no
  `contract_token`, accepted by the vendor) is one "Full amount" row from its own payment fields,
  with no actions.
- **Status is decided once, on the server:** Received > Client says sent > Cancelled > Overdue >
  Due soon (7 days) > Scheduled, on `effective_due` and UTC days like the reminders (#18). A
  booking is cancelled when it has `cancelled_at`, status `rejected`, or a signed cancellation
  agreement; what was already received stays received.
- **A year is the payments due in it.** Totals add up the rows. `other_rows` carries the rest of
  each shown booking's schedule, so a by-booking view adds up.
- **How the client paid.** "I've sent this" takes an optional `paid_via` (venmo | zelle | other),
  kept on the payment in `payment_schedule` and in the `payment_marked` event.
- **Remind client, by hand:** overdue payments only, one a day per payment, an email to the client
  with the amount and the vendor's Venmo/Zelle (the automatic overdue notice goes to the vendor).
  Recorded as `payment_reminder` with `reminder: "manual"`.
- **Undo is one step at a time** (`…/not-received`). Client says sent → the mark is cleared and the
  client is emailed. Received → the confirmation is cleared; the client's mark stays only if they
  made one. A confirmation fills in `marked_paid_at` too, so "they made one" is read from the
  `payment_marked` events, not the field. Each step is a `payment_not_received` event, and the
  booking-level payment fields are re-synced through the same path as every other change.


## 34. The vendor countersigns after the client; it's booked only then (0078)

Until this, only the client signed: the PDF's vendor line was blank under "Provider — issued this
agreement", and sending the contract stood for the vendor's agreement (user, 2026-10-19). Now:

- **The client signs first, then the vendor.** The client's signature (`client_signed_at`,
  `signer_name`, the #27 evidence) freezes the contract and sets `contract_status`
  `awaiting_vendor`. `signed_at` now means *both* have signed: it's set by the vendor's
  countersignature, so everything that means "booked" — the date check, payments and their
  reminders, the ledger, Bookings, the deposit's "on signing" date — starts there, unchanged.
- **Frozen from the client's signature.** Nobody can edit, resend, void or archive it, or propose
  changes; the client can't change details, sign again or decline. Those checks read
  `client_signed_at`.
- **The date stays held, with no expiry.** `commits_vendor_date` counts a client-signed contract;
  `contract_state` never calls `awaiting_vendor` expired. Reminders go to the vendor a day and
  three days after the client signed (`countersign_reminder` events, from the payment-reminder
  sweep), then stop.
- **The vendor signs signed in:** typed full legal name, plus the e-records consent — asked once per
  wording (`vendors.esign_consent_version`) and copied into each signature — plus IP and browser.
  No emailed code: the account is the proof. `POST /contracts/{id}/countersign`.
- **Both signatures in one fingerprint.** The countersignature is added to the client's snapshot
  under `countersignature` (name, provider, time, evidence, and `client_sha256`, the fingerprint
  the vendor signed), and the snapshot is hashed again. The PDF shows both signature lines and a
  "Countersigned by the provider" block on the certificate.
- **The vendor may decline to sign**, with a reason (`POST /contracts/{id}/decline-signature`): the
  contract is voided (`rejected_reason` `vendor_declined_signature`, the reason in
  `vendor_decline_note`), the date is free, and the client is emailed the reason.
- **The signed PDF goes out once, when both have signed.** The client's signature gets them a
  "waiting for <vendor>" email with no attachment; the vendor gets a notification.
- **Addenda and cancellation agreements follow the same rule** (`contract_documents`, same columns,
  status `awaiting_vendor`, `POST /contract-documents/{id}/countersign` and `…/decline-signature`).
- **Contracts signed before this are left as they were.** The migration sets `client_signed_at =
  signed_at` and no vendor signature; their PDF keeps the "issued this agreement" line.
- A contract the vendor countersigns isn't "new" on the Bookings badge (#30): countersigning sets
  `vendor_seen_at`.

## 35. Pickers choose a service from a three-level taxonomy (no migration yet)

Category/subcategory meant different things per category ("DJ" was a subcategory, "Photographer" a
whole category; Attire's subcategories described the outfit), and every picker showed the raw list
(user, 2026-10-18/19; draft list https://claude.ai/code/artifact/3ec21a59-77f9-491a-9c84-b18f6e6bc0c6).

- **Group → service → attribute.** A group is for browsing. A service is what someone books, and
  every picker selects one. An attribute is a filter on a service and replaces free-form tags. The
  test: if a client would book it separately, it's a service.
- **Data in `app/taxonomy/data.py`, served by `GET /taxonomy`** (public, ETag). It holds 17 groups
  and 73 services. Keys are unique across groups and services and never change once shipped.
  `app/taxonomy` checks the data on import.
- **Decided while editing the list (2026-10-20):**
  - Instruments are separate services.
  - Music and Entertainment are separate groups, with DJ in Entertainment.
  - Venue type is an attribute of Venue, not its own service.
  - The bundle builder fills 11 services: today's slots with Floral & Decor split, plus Bar &
    bartending. The old Cultural Services slot becomes the Entertainment group.
  - A vendor saved as plain `planning` (or any category with several services) is asked to pick,
    not mapped.
- **Today's data maps on.** `from_legacy(category, subcategory)` gives a stored pair's service, or
  None where the vendor has to choose. `legacy_search` gives what an old `?category=` key now
  covers, so old links keep finding the same vendors. Tests check every category, subcategory and
  bundle slot in use today.
- **Step 1 changed nothing users see.** `/vendors/categories` keeps serving the old list for older
  clients.
- **Step 2 (0080) stores it.**
  - Vendors have `specialties` (service keys). Packages have `service_key` and `attributes`.
  - Writes take either shape: service keys derive the old category/subcategory pairs, and old pairs
    derive the keys. A package that names a service joins its vendor's specialties.
  - The backfill maps exact pairs first, then a vague category through the vendor's one specialty in
    it, with the mapping frozen in the migration. Anything still ambiguous stays null, and the vendor
    sees it in `service_picks_needed` ("Pick your services").
  - Search takes `?service=` (service or group keys).
  - Bundle slots match by service, with the old category as the fallback for packages with no
    service_key. So the Cultural Services slot now draws from the whole Entertainment group.
  - The slot keys in the API are unchanged until the web picker ships. That's also when Bar &
    bartending becomes a default slot.
