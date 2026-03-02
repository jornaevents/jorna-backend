# Desiconnect — Event Planning Marketplace

A full-stack event planning and vendor booking platform connecting South Asian event vendors (Mehndi artists, DJs, photographers, decorators, etc.) with clients. Built with a **FastAPI** (Python) backend, **SQLite** database, and a **Swift** iOS frontend (separate repo).

> **Figma Design:** [Event Planning Marketplace](https://www.figma.com/design/q3OJLFcovQaYim0P0kDAhR/Event-Planning-Marketplace)

---

## Table of Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
- [API Reference](#api-reference)
- [Push Notifications (Firebase)](#push-notifications-firebase)
- [Running Tests](#running-tests)
- [Frontend (Vite)](#frontend-vite)

---

## Features

- **User Authentication** — Registration with password hashing (bcrypt) and JWT login
- **Vendor Search** — Location-based vendor discovery using the Haversine formula with configurable travel radius (default 30 miles)
- **Booking System** — Full booking lifecycle: create → pending → approved/rejected → payment confirmed
- **Push Notifications** — Firebase Cloud Messaging (FCM) integration for real-time booking status updates
- **Google Calendar Integration** — Vendors can connect their Google Calendar; availability is computed by merging baseline working hours, internal bookings, and Google Calendar busy blocks
- **Geo-Fenced Check-In** — GPS-verified venue check-in for both clients and vendors (within 0.2 miles)
- **Vendor Availability** — Weekly working hours with smart conflict detection

---

## Tech Stack

| Layer       | Technology                                     |
| ----------- | ---------------------------------------------- |
| Backend     | Python 3.13, FastAPI, Uvicorn                  |
| Database    | SQLite + SQLAlchemy ORM                        |
| Auth        | bcrypt (password hashing), PyJWT (tokens)      |
| Notifications | Firebase Admin SDK (FCM push notifications)  |
| Calendar    | Google Calendar API (OAuth 2.0, FreeBusy)      |
| Frontend    | Vite + TypeScript (web), Swift/SwiftUI (iOS)   |
| Testing     | pytest, httpx, pytest-mock                     |

---

## Project Structure

```
Desiconnect/
├── server/                          # FastAPI backend
│   ├── main.py                      # App entry point, auth routes, vendor search
│   ├── requirements.txt             # Python dependencies
│   ├── run.sh                       # Quick start script
│   ├── app/
│   │   ├── db/
│   │   │   ├── database.py          # SQLite engine & session factory
│   │   │   └── models.py            # SQLAlchemy models (User, Vendor, Service, Booking, VendorAvailability)
│   │   ├── models/
│   │   │   └── schemas.py           # Pydantic/dataclass schemas & BookingStatus enum
│   │   ├── routers/
│   │   │   ├── bookings.py          # Booking CRUD, status updates, check-in
│   │   │   ├── calendar.py          # Google Calendar OAuth & vendor availability
│   │   │   └── notifications.py     # FCM token registration & management
│   │   └── utils/
│   │       ├── calendar.py          # Google Calendar API helpers
│   │       ├── location.py          # Haversine distance calculation
│   │       └── notifications.py     # Firebase push notification wrapper
│   ├── tests/
│   │   ├── test_api.py              # Core API tests (auth, search, check-in)
│   │   ├── test_bookings.py         # Booking lifecycle tests
│   │   ├── test_calendar_and_location.py  # Calendar, OAuth, location tests
│   │   └── test_notifications.py    # Notification system tests
│   └── live_test.sh                 # Comprehensive live API test script (57 tests)
├── src/                             # Vite frontend
├── package.json
├── vite.config.ts
└── README.md
```

---

## Getting Started

### Prerequisites

- Python 3.12+
- Node.js 18+ (for the Vite frontend)

### Backend Setup

```bash
cd server

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Start the development server
uvicorn main:app --reload --port 8000
```

The API will be available at `http://localhost:8000`. Visit `http://localhost:8000/docs` for the interactive Swagger UI.

### Database

SQLite is used with auto-creation — the database file `desiconnect.db` is created automatically on first startup. If you need to reset the schema after model changes:

```bash
rm desiconnect.db
# Restart the server — tables are recreated automatically
```

---

## API Reference

### Health

| Method | Endpoint     | Description              |
| ------ | ------------ | ------------------------ |
| GET    | `/`          | Root — returns API name & status |
| GET    | `/db-check`  | Verify database connection |

### Authentication

| Method | Endpoint          | Description                        |
| ------ | ----------------- | ---------------------------------- |
| POST   | `/auth/register`  | Create a new user account          |
| POST   | `/auth/login`     | Login and receive a JWT token      |

**Register body:**
```json
{
  "email": "user@example.com",
  "password": "SecurePass123",
  "username": "user123",
  "phone": "5551234567",
  "f_name": "First",
  "l_name": "Last",
  "age": 25,
  "location": "10001",
  "gender": "Female",
  "language": "English"
}
```

### Vendor Search

| Method | Endpoint           | Description                                      |
| ------ | ------------------ | ------------------------------------------------ |
| GET    | `/vendors/search`  | Search vendors by service name & proximity        |

**Query params:** `service_name`, `latitude`, `longitude`

```
GET /vendors/search?service_name=Mehndi&latitude=34.06&longitude=-118.25
```

Returns vendors within their `travel_radius_miles`, sorted by distance.

### Bookings

| Method | Endpoint                           | Description                          |
| ------ | ---------------------------------- | ------------------------------------ |
| POST   | `/bookings`                        | Create a booking (status: pending)   |
| PUT    | `/bookings/{id}/status`            | Approve, reject, or confirm payment  |
| GET    | `/bookings/user/{user_id}`         | Get all bookings for a client        |
| GET    | `/bookings/vendor/{vendor_id}`     | Get all bookings for a vendor        |
| POST   | `/bookings/{id}/check-in`          | GPS-verified venue check-in          |

**Booking statuses:** `pending` → `approved` / `rejected` → `payment_confirmed`

**Status update body:**
```json
{
  "user_id": "uuid",
  "is_vendor": true,
  "status": "approved"
}
```

> ⚡ All booking mutations (create, status update, check-in) automatically dispatch push notifications to the relevant parties.

### Calendar & Availability

| Method | Endpoint                              | Description                          |
| ------ | ------------------------------------- | ------------------------------------ |
| GET    | `/vendors/{id}/google-auth`           | Get Google OAuth redirect URL        |
| GET    | `/vendors/auth/callback`              | Google OAuth token exchange callback |
| GET    | `/vendors/{id}/availability`          | Compute vendor's free time slots     |

**Availability query params:** `start_date`, `end_date` (ISO format)

```
GET /vendors/{id}/availability?start_date=2026-03-01T00:00:00Z&end_date=2026-03-07T23:59:59Z
```

Returns baseline working hours, internal busy times (Desiconnect bookings), and Google Calendar busy times.

### Notifications

| Method | Endpoint                                | Description                          |
| ------ | --------------------------------------- | ------------------------------------ |
| POST   | `/notifications/register-token`         | Register/update an FCM device token  |
| DELETE | `/notifications/remove-token/{user_id}` | Remove FCM token (opt out)           |
| GET    | `/notifications/token-status/{user_id}` | Check if user has a registered token |

**Register token body:**
```json
{
  "user_id": "uuid",
  "fcm_token": "device_fcm_token_string"
}
```

---

## Push Notifications (Firebase)

Desiconnect uses **Firebase Cloud Messaging (FCM)** to send real-time push notifications to iOS/Android devices.

### How It Works

1. **Token Registration** — The mobile app calls `POST /notifications/register-token` on launch (and whenever the FCM token refreshes) to associate the device with the user.
2. **Automatic Dispatch** — Notifications are sent automatically when:
   - A **booking is created** → vendor receives "New Booking Request", client receives "Booking Submitted"
   - A **booking is approved** → client receives "Booking Approved!"
   - A **booking is rejected** → client receives "Booking Declined"
   - **Payment is confirmed** → both parties are notified
   - A **check-in occurs** → the other party is notified of arrival
3. **Graceful Degradation** — If Firebase is not configured, the API endpoints still work normally; notification results simply return `success: false`.

### Setup

1. Go to [Firebase Console](https://console.firebase.google.com/) → Project Settings → Service Accounts
2. Click **"Generate new private key"** to download the credentials JSON
3. Place it in the `server/` directory as `firebase_credentials.json`
   - Or set the environment variable: `FIREBASE_CREDENTIALS_PATH=/path/to/creds.json`

### Notification Templates

| Status              | Vendor Notification          | Client Notification             |
| ------------------- | ---------------------------- | ------------------------------- |
| `pending`           | 📥 New Booking Request       | ⏳ Booking Submitted            |
| `approved`          | ✅ Booking Confirmed          | 🎉 Booking Approved!            |
| `rejected`          | ❌ Booking Declined           | 😔 Booking Declined             |
| `payment_confirmed` | 💰 Payment Received          | 💳 Payment Confirmed            |
| Check-in (vendor)   | —                            | 📍 Vendor Checked In            |
| Check-in (client)   | 📍 Client Checked In         | —                               |

---

## Running Tests

### Unit / Integration Tests (pytest)

```bash
cd server
source venv/bin/activate
python -m pytest tests/ -v
```

**Test coverage (38 tests):**

| File                              | Tests | Covers                                                  |
| --------------------------------- | ----- | ------------------------------------------------------- |
| `test_api.py`                     | 7     | Root, DB check, auth, vendor search, Google auth, check-in |
| `test_bookings.py`                | 5     | Booking CRUD, approve/reject, fetch by user/vendor       |
| `test_calendar_and_location.py`   | 7     | OAuth callbacks, availability aggregation, check-in edge cases |
| `test_notifications.py`           | 19    | FCM token endpoints, notification utils (mocked), booking integration |

### Live API Tests (curl)

With the server running (`uvicorn main:app --reload`), run:

```bash
cd server
bash live_test.sh
```

This executes **57 end-to-end tests** across 13 categories:
- Root & Health, Auth (register/login), Notification Token Management, Vendor Search, Booking Creation, Status Updates with Notification Verification, Booking Fetch, Geo-Fenced Check-In, Vendor Availability, Google OAuth, and Edge Cases.

---

## Frontend (Vite)

The web frontend is built with Vite:

```bash
npm install
npm run dev
```

The iOS frontend is in a separate repository using Swift/SwiftUI.

---

## License

Private repository — all rights reserved.