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

## Backend endpoints

| Method | Path | Auth required | Body | Returns |
|---|---|---|---|---|
| `POST` | `/auth/login` | No | `{ identifier, password }` | `{ access_token, refresh_token, token_type }` |
| `POST` | `/auth/google/lookup` | No | `{ access_token }` | `{ access_token, refresh_token, token_type, user_id, email, is_new_user }` |
| `POST` | `/auth/refresh` | No | `{ refresh_token }` | `{ access_token, refresh_token, token_type }` |
| `POST` | `/auth/logout` | Yes | `{ refresh_token? }` | `{ message }` |

---

## Security notes

- **Replay detection**: if the server sees a refresh token from a family that has already been rotated, it wipes all refresh tokens for that user and returns `401`. This means a stolen token that gets replayed will force a re-login.
- **Parallel requests**: the `refreshPromise` lock in step 3 ensures multiple simultaneous `401`s only trigger one refresh call — the rest wait and reuse the result.
- **Token rotation**: every call to `/auth/refresh` returns a brand new `refresh_token`. The previous one is permanently invalidated. Always update localStorage with the new value.
