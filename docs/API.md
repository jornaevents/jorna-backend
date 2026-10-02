# API

This is the canonical contract — both client repos' own `docs/API.md`
describe this from the outside and can lag. For the full, always-current
endpoint list with request/response schemas, run the server locally and open
`/docs` (Swagger UI, FastAPI's auto-generated docs) or `/redoc`; this file
covers the conventions and the flows that aren't obvious from the schema
alone. For which router owns which resource, see `docs/MODULE_MAP.md`.

## Auth

- `POST /auth/register`, `/auth/login`, `/auth/google/lookup`,
  `/auth/google/register` return `{ access_token, refresh_token, ... }`.
- **`access_token`**: JWT, 60 min lifetime (`ACCESS_TOKEN_EXPIRE_MINUTES`).
  Sent as `Authorization: Bearer <token>` on every authenticated request.
  Clients should hold this in memory only, never persist it — it's
  short-lived by design so a leak (XSS, disk access) has a small blast
  radius.
- **`refresh_token`**: 30 days (`REFRESH_TOKEN_EXPIRE_DAYS`), used against
  `POST /auth/refresh` to get a new access token (and a rotated refresh
  token — the old one is invalidated on use). Longer-lived, so clients
  persist this one (e.g. iOS Keychain, web `localStorage`) — but a leaked
  refresh token is a bigger deal than a leaked access token, since it's
  usable until logout/expiry, hence rotation on every use.
- `/auth/google/lookup` vs `/auth/google/register`: `lookup` never creates an
  account (a client showing a "complete your profile" form after a lookup
  miss must keep calling `lookup`, not switch to `register`, or it will try
  to create an account that already exists mid-flow); `register` creates the
  account on first use if one doesn't exist, for a one-tap sign-up flow with
  no separate form. Both answer a bad Supabase token (bad signature, wrong
  audience, or a key id the project's JWKS doesn't have) with `401`, and
  `503` when Supabase's JWKS can't be fetched.
- `POST /auth/forgot-password` always returns `200` regardless of whether the
  email exists — an intentional anti-enumeration measure, not a bug.
- `POST /auth/logout` invalidates all access tokens; pass `refresh_token` in
  the body to log out only that device, or omit it to invalidate every
  refresh token for the user (all devices).

## Error shape

Handled errors (`AuthError` and similar per-domain exceptions) map to
`HTTPException` with a real status code and a `detail` string. Unhandled
exceptions in `/auth/register` are caught, logged via
`logger.exception(...)`, rolled back, and mapped to a generic 400 (unique
constraint violation → "Email or username already taken") or 500 — this
pattern (catch domain errors specifically, log-and-rollback-and-generic-500
for the rest) is the convention to follow in new routes, not per-route ad hoc
error handling.

## Chatbot / bundle builder

Stateless — see `docs/ARCHITECTURE.md`. The client owns and round-trips the
`state` object; nothing is persisted server-side until a bundle is actually
selected/booked.

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /chatbot/start` | none | Begin a session, returns the opening prompt + empty state |
| `POST /chatbot/step` | Bearer JWT | Advance one step; returns next prompt + updated state. `current_step` in the request is the step being *answered* — i.e. the `next_step` from the response you're replying to |
| `POST /chatbot/bundle` | none | Single-shot bundle from all inputs at once; **not persisted**, just a preview the user can refine via `/chatbot/step` |
| `POST /chatbot/bundles` | Bearer JWT | Generates and **persists** three draft bundles (Budget, Top Rated, Balanced) as pending bookings with vendor notifications held |
| `POST /bundles/{bundle_id}/select` | Bearer JWT | Keep one bundle from a `/chatbot/bundles` comparison, discard the other two |

## Bookings & bundles

Every booking belongs to a bundle (a bundle groups one or more per-vendor
bookings under one event; each is negotiated/paid/checked-in independently —
changing one never affects siblings in the same bundle). Three entry points
create bookings: `POST /bookings` (direct, single-booking bundle unless
`bundle_id` given), the chatbot step flow, and the 3-bundle compare flow
above. See `docs/ARCHITECTURE.md` for what happens to a booking's payment
after it's created.

## Leads pipeline (vendor)

`GET /leads/pipeline` returns everything before a signed contract in one
list (`pipeline_service.py`, DECISIONS #20): marketplace requests, unsigned
contracts and informal leads. Each item has `stage` (`inquiry` until the
contract link is sent, then `negotiation`), `source` (`request` | `contract`
| `lead`), `attention` (`needs_you` | `waiting` | null) with an
`attention_reason` (`changes_proposed`, `new_request`, `new_lead`, `draft`,
`counter_offer`, `declined`, `expired`; `sent`, `viewed`, `revised`,
`counter_sent`, or the lead's own status while waiting), `archived`, and `created_at`/`updated_at`. `counts`
summarises the unarchived items.

- `POST /bookings/{id}/archive` `{archived: bool}` hides or restores a
  request or unsigned contract. It declines, voids and notifies nothing;
  signed bookings 400. Leads archive via `PATCH /leads/{id}` `{archived}`.
- `POST /conversations/{id}/lead` makes a lead for the couple in a
  two-person thread — 201 new, 200 when the thread already has an open lead.
- `POST /conversations/{id}/unread` counts the thread as unread for the
  caller until they next open it (`GET …/messages` at offset 0).
- Vendor booking payloads now carry `created_at` (null before 0066),
  `sent_at` and `vendor_archived_at`. Copying a contract link should call
  `POST /contracts/{id}/send` (no `email_client`) — that's what marks it
  sent and starts the hold.

## Contract documents (vendor + public)

Contracts (`POST/PATCH /contracts`, and `POST /bookings/{id}/propose`) also take `document_title` (≤200) and
`document_layout`: up to 40 blocks `{id?, type}`, where type is `parties`,
`event`, `items`, `schedule` or `signature` (each at most once), or `terms`
with `title`/`body`. Terms blocks become `terms_clauses`. The guest payload carries both. Templates take
`kind` (`agreement` default, `addendum`, `cancellation`). DECISIONS #21.

Addenda and cancellations attached to an agreed booking:

- `GET /contracts/{booking_id}/documents` → `{items, total}`.
- `POST /contracts/{booking_id}/documents` `{kind, title?, sections:[{title, body}], send?, email_client?}` → 201.
- `PATCH /contract-documents/{id}` `{title?, sections?}`; this is refused once signed, declined or voided.
- `POST /contract-documents/{id}/send` `{email_client?}`.
- `POST /contract-documents/{id}/void`; a signed document gets a 400.
- Public routes (no auth, rate-limited):
  - `GET /guest-documents/{token}?preview=`: a draft gets a 404, and the first open marks it viewed.
  - `POST …/sign` `{signer_name}`.
  - `POST …/decline` `{reason?}`.
  - Voided or declined documents get a 410; an already-signed document gets a 400 on sign.

## Emailing the client their link

`POST /contracts`, `POST /leads/{lead_id}/convert`, `POST /contracts/{id}/send` and
`POST /bookings/{id}/propose` (accepting a request with a proposal) return the contract plus
`email_sent`: `true` when the email provider accepted the client's link email, `false` when it
didn't (no email configured, provider error), and `null` when the vendor didn't ask us to email.
Accepting a request with the vendor's usual terms (`PUT /bookings/{id}/status` to `approved`) returns `email_sent` the same way. The timeline records `emailed` or `email_failed`. The vendor app says "We emailed the link" only
on `true`.

## Change proposals (vendor + public)

The client suggests edits to an unsigned contract; the vendor accepts,
declines or revises (DECISIONS #23). Each list endpoint returns
`{current_revision, open_proposal, proposals[], revisions[]}`:

- A **proposal** is `{proposal_id, base_revision, status, message,
  response_note, result_revision, created_at, responded_at, proposed}`.
  `proposed` is the whole set of proposed terms. `status` is `open`,
  `accepted`, `declined`, `revised`, `superseded` or `withdrawn`.
- A **revision** is `{revision, created_at, terms}`.
- `terms` and `proposed` share one shape: `date_iso`, `date_end`,
  `time_start`, `time_end`, `location`, `guest_count`, `line_items`,
  `discount_cents`, `amount_cents`, `payment_schedule` (without payment
  marks), `terms_clauses`, `cancellation_window_hours` and
  `overtime_rate_cents`.

Public routes, by the contract's token:

- `GET /guest-bookings/{token}/proposals` (30/minute).
- `POST /guest-bookings/{token}/proposals` `{base_revision, changes, message?}` → 201 (10/minute).
  - `changes` holds any of the terms fields except `amount_cents`, which is
    derived. Anything left out stays as it is.
  - Errors:
    - 400 if it changes nothing, fails a check, or names an unknown field.
    - 400 if there's no `guest_email` yet, or the contract is signed.
    - 409 if `base_revision` isn't the current one.
    - 410 if the contract is expired, voided or declined.
  - It replaces the client's open proposal, and the vendor gets a push and
    an email.
- `POST /guest-bookings/{token}/proposals/{id}/withdraw`. A 400 once it's
  been answered.

Vendor routes, which return 403 for another vendor's contract:

- `GET /contracts/{booking_id}/proposals`.
- `POST /contracts/{booking_id}/proposals/{id}/accept` `{note?}` → the contract.
  - The proposed terms become the next revision, and the contract is resent
    with its hold restarted.
  - 409 if the new date overlaps another booking (the proposal stays open)
    or the proposal is no longer open.
- `POST /contracts/{booking_id}/proposals/{id}/decline` `{note?}` → the
  contract, unchanged.
- `PATCH /contracts/{booking_id}` with `proposal_id` (and an optional
  `proposal_note` for the client) revises: the edit is
  the answer, and it's resent like Accept. A `PATCH` without `proposal_id`
  while a proposal is open marks it `superseded`.

The client hears about Accept, Decline and Revise by email.

**Drafts** (DECISIONS #24). Each side can save what it's about to send.
Each list endpoint also returns `draft`, the caller's side's draft or null:
`{base_revision, changes, message, proposal_id, updated_at, stale}`.
`stale` is true once the contract has moved past `base_revision`.

- `PUT /guest-bookings/{token}/proposals/draft` `{base_revision, changes, message?}` (60/minute).
- `DELETE /guest-bookings/{token}/proposals/draft` → 204 (30/minute).
- `PUT /contracts/{booking_id}/proposals/draft` `{base_revision, changes, message?, proposal_id?}`.
- `DELETE /contracts/{booking_id}/proposals/draft` → 204.

A draft isn't validated beyond its shape, since it can be half done:
`changes` may hold only terms fields (400 otherwise) and must stay under
100 KB (413). Saving is refused once the contract is signed (400), or on a
voided or declined link (410). Proposing drops the client's draft. Any
vendor answer or edit drops the vendor's.

Contract payloads (`GET /contracts/{id}`, the guest payload, the vendor's
booking list and bundle bookings) carry `proposal_status`: the latest
proposal's status on an unsigned contract, or null. Pipeline items carry it
too, with the attention reasons `changes_proposed` (needs you) and
`revised` (waiting).

`POST /negotiations` (starting a price counter) now returns 410. Open
counters can still be answered.

## PDF downloads

`application/pdf`, sent as an attachment with a filename built from the title and date (DECISIONS #22).
A signed record is drawn from its snapshot and includes the SHA-256.

- `GET /contracts/{booking_id}/pdf` (vendor). Another vendor gets 403.
- `GET /contract-documents/{document_id}/pdf` (vendor). Drafts are included.
- `GET /guest-bookings/{token}/pdf` (public, 10/minute). A draft gets 404.
- `GET /guest-documents/{token}/pdf` (public, 10/minute). A draft gets 404.

## Vendor categories

Some categories require a subcategory at vendor registration (e.g. `music_entertainment`
→ `dj`, `dhol`, `tabla`, `sitar`, `vocalist`, `mc`, …); others rely on tags +
price filters for discovery instead of a fixed subcategory list. Check
`app/models/schemas.py`'s `VendorCategory` enum and `app/services/vendor_service.py`
for the current authoritative list rather than hand-maintaining one here —
categories get added often enough that a prose copy would drift immediately.
