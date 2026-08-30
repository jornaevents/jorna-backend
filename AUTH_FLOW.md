> **Superseded.** This file is kept for history only — current, maintained content lives in docs/API.md's Auth section and docs/DECISIONS.md #5-6. If this file says something different, trust the newer doc.

# Auth Flow — Frontend Implementation Guide

## Token Overview

| Token | Lifetime | Storage | Purpose |
|---|---|---|---|
| `access_token` | 60 min | **Memory only** (JS variable) | Sent as Bearer on every API call |
| `refresh_token` | 30 days | `localStorage` | Silently gets a new access token when it expires |

Never store the access token in localStorage — it lives in memory only so XSS can't steal it.

---

## 1. Login / Google sign-in

Both `/auth/login` and `/auth/google/lookup` now return a `refresh_token` alongside `access_token`.

```js
const { access_token, refresh_token } = await api.post('/auth/login', { identifier, password })

setAccessToken(access_token)                     // in-memory state (Zustand / React context)
localStorage.setItem('refresh_token', refresh_token)
```

---

## 2. Making API requests

Attach the access token as a Bearer header. On a `401`, try to refresh once before giving up:

```js
async function apiFetch(url, options = {}) {
  let res = await fetch(url, {
    ...options,
    headers: { ...options.headers, Authorization: `Bearer ${getAccessToken()}` },
  })

  if (res.status === 401) {
    const refreshed = await tryRefresh()
    if (!refreshed) { redirectToLogin(); return }

    // Retry the original request once with the new token
    res = await fetch(url, {
      ...options,
      headers: { ...options.headers, Authorization: `Bearer ${getAccessToken()}` },
    })
  }

  return res
}
```

---

## 3. Refreshing the token

Calls `POST /auth/refresh`. Always stores the **new** refresh token — the old one is dead immediately after rotation.

```js
let refreshPromise = null  // prevents parallel refresh races

async function tryRefresh() {
  if (refreshPromise) return refreshPromise  // reuse in-flight call
  refreshPromise = doRefresh().finally(() => { refreshPromise = null })
  return refreshPromise
}

async function doRefresh() {
  const refresh_token = localStorage.getItem('refresh_token')
  if (!refresh_token) return false

  try {
    const res = await fetch('/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token }),
    })
    if (!res.ok) {
      localStorage.removeItem('refresh_token')
      return false
    }
    const { access_token, refresh_token: new_refresh } = await res.json()
    setAccessToken(access_token)
    localStorage.setItem('refresh_token', new_refresh)  // always rotate
    return true
  } catch {
    return false
  }
}
```

---

## 4. App startup / page reload

The access token is gone after a reload (it was in memory). Restore the session eagerly on mount using the stored refresh token:

```js
useEffect(() => {
  const stored = localStorage.getItem('refresh_token')
  if (stored) {
    tryRefresh().then(ok => { if (!ok) redirectToLogin() })
  }
}, [])
```

---

## 5. Logout

Pass the current `refresh_token` in the body to revoke just this device. Omit it to revoke all devices.

```js
async function logout() {
  const refresh_token = localStorage.getItem('refresh_token')
  await fetch('/auth/logout', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${getAccessToken()}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ refresh_token }),
  })
  setAccessToken(null)
  localStorage.removeItem('refresh_token')
  redirectToLogin()
}
```

---

## 6. Forgot / reset password

Two-step, email-based. The user requests a link, receives an email, and lands on a frontend page that collects the new password and submits the token.

```js
// Step 1 — user submits their email on the "forgot password" screen
async function forgotPassword(email) {
  await fetch('/auth/forgot-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  })
  // Always show the same confirmation — the API never reveals whether the email exists
  showMessage('If that email is registered, a reset link has been sent.')
}

// Step 2 — the reset link opens /reset-password?token=XYZ in your app.
// Read the token from the URL, collect a new password, then:
async function resetPassword(token, newPassword) {
  const res = await fetch('/auth/reset-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, new_password: newPassword }),
  })
  if (!res.ok) throw new Error('Reset link is invalid or expired')
  // Password changed — all old sessions are now dead. Send the user to login.
  redirectToLogin()
}
```

**Notes:**
- The reset link points at `{FRONTEND_URL}/reset-password?token=...` — make sure that route exists in your app.
- The token is **single-use** and expires in 60 minutes. Requesting a new link invalidates any previous one.
- A successful reset **invalidates all existing sessions** (access + refresh tokens), so the user must log in again afterward.
- `new_password` must meet the same complexity rules as registration (8+ chars, upper, lower, digit) or the API returns `422`.

---

## 7. Email verification

Registering with a password starts the account **unverified**. A Google
sign-up (direct or linked during registration) starts **verified**
immediately — Google already proved the address, so there's nothing to send.

An unverified account can still sign in and browse normally. What it
**can't** do until verified: create a booking, pay (PaymentIntent, Checkout
Session, save a card), start Stripe Connect vendor onboarding, open or act on
a negotiation, or send a message (booking chat, group chat, or the
vendor-enquiry endpoint). Any of those return **403** with a human-readable
`detail` — show it, and offer a way to resend the link.

```js
// After registering, or any time GET /me shows email_verified: false —
// give the user a way to get a fresh link.
async function resendVerification() {
  await fetch('/auth/resend-verification?client=ios', {
    method: 'POST',
    headers: { Authorization: `Bearer ${getAccessToken()}` },
  })
}
```

**The verification link itself is not an API call your client makes.** It's
emailed to the user and opens `GET /auth/verify-email?token=...` directly in
a browser — the backend verifies the token and renders its own small HTML
success/failure page (there's no frontend page to hand this off to, unlike
password reset). The page includes a plain `jorna://` link, which opens the
iOS app via its registered URL scheme without any special deep-link handling
on that end.

**Notes:**
- The token is single-use and expires in 24 hours (longer than password
  reset's 60 minutes — verifying is lower-urgency, and people don't always
  check their inbox right away). Requesting a resend invalidates any
  previous link, same as forgot-password.
- Check `GET /me`'s `email_verified` field to know whether to show a
  "verify your email" banner — don't infer it from a 403, since that only
  tells you *after* the user already tried and failed to do something.

---

## Backend endpoints

| Method | Path | Auth required | Body | Returns |
|---|---|---|---|---|
| `POST` | `/auth/login` | No | `{ identifier, password }` | `{ access_token, refresh_token, token_type }` |
| `POST` | `/auth/google/lookup` | No | `{ access_token }` | `{ access_token, refresh_token, token_type, user_id, email, is_new_user }` |
| `POST` | `/auth/refresh` | No | `{ refresh_token }` | `{ access_token, refresh_token, token_type }` |
| `POST` | `/auth/logout` | Yes | `{ refresh_token? }` | `{ message }` |
| `POST` | `/auth/forgot-password` | No | `{ email }` | `{ message }` (always 200) |
| `POST` | `/auth/reset-password` | No | `{ token, new_password }` | `{ message }` |
| `GET` | `/auth/verify-email?token=...` | No | — | HTML result page (not JSON — see below) |
| `POST` | `/auth/resend-verification?client=ios\|web` | Yes | — | `{ message, already_verified }` |

---

## Security notes

- **Replay detection**: if the server sees a refresh token from a family that has already been rotated, it wipes all refresh tokens for that user and returns `401`. This means a stolen token that gets replayed will force a re-login.
- **Parallel requests**: the `refreshPromise` lock in step 3 ensures multiple simultaneous `401`s only trigger one refresh call — the rest wait and reuse the result.
- **Token rotation**: every call to `/auth/refresh` returns a brand new `refresh_token`. The previous one is permanently invalidated. Always update localStorage with the new value.
