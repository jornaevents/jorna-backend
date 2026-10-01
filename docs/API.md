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

## Vendor categories

Some categories require a subcategory at vendor registration (e.g. `music_entertainment`
→ `dj`, `dhol`, `tabla`, `sitar`, `vocalist`, `mc`, …); others rely on tags +
price filters for discovery instead of a fixed subcategory list. Check
`app/models/schemas.py`'s `VendorCategory` enum and `app/services/vendor_service.py`
for the current authoritative list rather than hand-maintaining one here —
categories get added often enough that a prose copy would drift immediately.
