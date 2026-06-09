# Booking, Bundle & Payment Flow

## Overview

Every booking belongs to a bundle. A bundle groups one or more vendor bookings under a single event. Each vendor within a bundle is booked, negotiated, paid, and checked in independently — changing one booking never affects the others.

---

## Entry Points

There are three ways to create bookings:

| Method | Endpoint | Use case |
|---|---|---|
| Direct booking | `POST /bookings` | Client books a single vendor directly |
| Chatbot flow | `POST /chatbot/step` | Guided multi-step flow, builds a full bundle |
| 3-bundle compare | `POST /chatbot/bundles` → `POST /bundles/{bundle_id}/select` | Compare 3 pre-built bundles, then pick one |

All three auto-create a `Bundle` record. The chatbot can create multiple bookings in one bundle; the direct endpoint creates a single-booking bundle unless a `bundle_id` is provided.

---

## 3-Bundle Comparison Flow

`POST /chatbot/bundles` (requires auth) generates three bundles — Budget, Top Rated, and Balanced — and immediately **persists all three to the DB** as drafts. All bookings are created in `pending` status but vendor notifications are held.

Each option in the response includes a `bundle_id`:

```json
{
  "options": [
    { "label": "Budget Bundle",    "bundle_id": "uuid-1", "bundle": {...} },
    { "label": "Top Rated Bundle", "bundle_id": "uuid-2", "bundle": {...} },
    { "label": "Balanced Bundle",  "bundle_id": "uuid-3", "bundle": {...} }
  ]
}
```

Once the user picks one, call `POST /bundles/{bundle_id}/select`. This:

1. Deletes the other two bundles and their bookings
2. Clears the comparison group from the chosen bundle
3. Fires `pending` push notifications to each vendor in the chosen bundle
4. Returns the chosen bundle as a normal draft ready to modify

After selection, use the standard booking endpoints to edit individual bookings before confirming.

```
POST /chatbot/bundles               → 3 draft bundles created in DB (no vendor notifications yet)
POST /bundles/{bundle_id}/select    → pick one, delete others, notify vendors
PATCH /bookings/{booking_id}        → edit date / time / location per vendor
DELETE /bundles/{id}/bookings/{id}  → drop a vendor from the bundle
PATCH /bundles/{bundle_id}/status   → { "status": "confirmed" } to lock in and create group chats
```

---

## Bundle Lifecycle

```
draft → confirmed → completed
                 → cancelled
```

| Status | When |
|---|---|
| `draft` | Created automatically — bookings can still be added, removed, or edited |
| `confirmed` | Client confirms the bundle — group chat conversations are created for each vendor |
| `completed` | Event is done |
| `cancelled` | Bundle cancelled |

### What's shared across a bundle
- `event_name` — the name of the event (e.g. "Yanik's Wedding")

### What's independent per booking
- Date, time, location, venue coordinates
- Vendor and service
- Approval status
- Price and payment
- Negotiation
- Check-in

---

## Booking Lifecycle

```
pending → negotiation_ongoing → approved → payment_confirmed
       → negotiation_ongoing → rejected
       → approved            → payment_confirmed
       → rejected
```

### Status definitions

| Status | Set by | Meaning |
|---|---|---|
| `pending` | System | Booking created — waiting for vendor response |
| `negotiation_ongoing` | Either party | Price or terms being discussed |
| `approved` | Vendor | Vendor accepts the booking |
| `rejected` | Vendor | Vendor declines |
| `payment_confirmed` | System | Payment processed successfully |

### Editing a booking
While a booking is `pending` or `negotiation_ongoing`, the client can update:
- `date_iso` / `date_end` — event date(s)
- `time_start` / `time_end` — event times
- `location` — venue location
- `venue_latitude` / `venue_longitude` — for GPS check-in

`PATCH /bookings/{booking_id}` — client only, locked once approved.

---

## Negotiation Flow

Either party (client or vendor) can open a negotiation at any point while the booking is `pending` or `approved` and unpaid.

```
POST /negotiations               → opens negotiation, booking → negotiation_ongoing
POST /negotiations/{id}/offer    → counter-offer (other party's turn only)
POST /negotiations/{id}/accept   → accepts current offer, booking.amount_cents updated
POST /negotiations/{id}/reject   → closes negotiation, price unchanged
```

**Rules:**
- Only one negotiation per booking
- Turns alternate — you cannot counter your own offer
- You cannot accept your own offer
- Once accepted, the agreed price is locked onto the booking
- Once rejected, the booking price stays at the original service price and the vendor can approve/reject normally

### Negotiation status

| Status | Meaning |
|---|---|
| `open` | Back and forth in progress |
| `accepted` | Price agreed — `booking.amount_cents` updated |
| `rejected` | Closed without agreement |

---

## Payment Flow

Payment begins after a booking is `approved`.

```
unpaid → processing → paid → released
                    → refunded
                    → disputed → refunded
                              → released
```

| Status | Meaning |
|---|---|
| `unpaid` | Default — no payment initiated |
| `processing` | Stripe payment in flight |
| `paid` | Stripe confirmed payment — `paid_at` stamped |
| `released` | Funds released to vendor — `funds_released_at` stamped |
| `refunded` | Client refunded |
| `disputed` | Client raised a dispute — admin resolves |

Payment is per booking. Each vendor is paid out separately.

---

## Check-in

After a booking is `approved`, both the client and vendor can GPS check-in on the day of the event.

`POST /bookings/{booking_id}/check-in` — must be within 0.2 miles of `venue_latitude` / `venue_longitude`.

- `client_checked_in_at` — stamped when the client checks in
- `vendor_checked_in_at` — stamped when the vendor checks in

The other party receives a push notification when either side checks in.

---

## Full Flow Diagram

```
Client
  │
  ├── Direct booking (POST /bookings)
  ├── Chatbot (POST /chatbot/step)
  └── 3-Bundle compare (POST /chatbot/bundles)
           │
           ▼
     Bundle created (draft)
           │
    ┌──────┴──────────────────────┐
    │                             │
  Booking 1                   Booking 2         (each independent)
  status: pending              status: pending
    │                             │
    ├── Negotiation?              ├── Negotiation?
    │     ↓ accept                │
    │   price locked              │
    │                             │
    ├── Vendor approves           ├── Vendor rejects
    │   status: approved          │   status: rejected ❌
    │                             │
    ├── Client pays               │
    │   payment: paid             │
    │   status: payment_confirmed │
    │                             │
    ├── GPS check-in              │
    │                             │
    └── Funds released            │
        payment: released ✅      │
```

---

## Key Rules Summary

- Every booking belongs to a bundle — direct bookings auto-create a single-booking bundle
- Each booking is fully independent within a bundle — approvals, payments, negotiations, and check-ins are per vendor
- `event_name` is the only field shared across a bundle
- Clients can edit a booking's date/time/location while it is `pending` or `negotiation_ongoing`
- Vendors can only approve or reject from `pending` or `negotiation_ongoing`
- Payment can only start after a booking is `approved`
- Negotiations lock the price on accept; rejecting leaves the original price
