# Jorna — Development Flow, CI/CD, and Sentry Overview

Cross-repo reference covering all three Jorna repos: this backend
(`Desiconnect`), the iOS client (`front_end_desiconnect`), and the web app
(`jorna-website`). Each repo's own `CLAUDE.md`/`docs/` is the source of
truth if this drifts — this file is a snapshot for orientation, not a
replacement.

## Repo map

| Repo | Stack | Deploy target | Role |
|---|---|---|---|
| `Desiconnect` (this repo) | FastAPI + SQLAlchemy, Alembic, Postgres | Railway | Backend — schema/business-logic source of truth for both clients |
| `front_end_desiconnect` | iOS SwiftUI | App Store (TestFlight/manual) | Native mobile client |
| `jorna-website` | Next.js 16 / React 19 / TS, static export | Cloudflare Pages (`jornaevents.com`) | Web client, `/app` route (`basePath: "/app"`) |

There is no staging environment on any of the three. All three branch per
change and merge to `main` to deploy; a booking/pricing/escrow change
usually needs coordinated PRs across the backend plus one or both clients.

---

## Development flow

### Desiconnect (backend)
```bash
cd server
venv/bin/pip install -r requirements.txt
venv/bin/python -m uvicorn main:app --reload   # local dev, needs .env (see app/config.py)
venv/bin/python -m pytest -q                    # tests — local sqlite, no DB needed
venv/bin/ruff check .                           # lint
venv/bin/alembic upgrade head                   # migrations — server/alembic/, NOT the stale root alembic.ini
```
Two Alembic setups exist in the repo; only `server/alembic.ini` +
`server/alembic/` is real (45 migrations). The repo root also has a stale
`src/`/`vite.config.ts`/`package.json` prototype from an old Figma Make
import — not the production web app.

### front_end_desiconnect (iOS)
No local build/run — dev machine is Windows with no Xcode. All iOS changes
are verified via CI (push a branch, or open a PR to `main`); Xcode's
synchronized file groups mean any `.swift` file placed under `Jorna/Jorna/`
auto-joins the build target (never use Xcode's "Add Files to…" for a file
already on disk).

### jorna-website (web)
```bash
npm run install:app          # first time: installs web/ deps
npm --prefix web run dev     # localhost:3000/app
npm --prefix web run lint
npm --prefix web run typecheck
npm --prefix web run test    # vitest — pure-logic unit tests only (pricing, planning rules)
npm run test:e2e             # playwright — component/user-flow coverage lives here instead
npm run build                # next build + static export into public/app
```
A husky pre-commit hook runs eslint (staged files) + full `typecheck` before
every commit — fast local gate, doesn't run Vitest/Playwright.

---

## CI/CD pipeline

### Desiconnect — `.github/workflows/ci.yml` ("Backend CI")
Runs on every push/PR to `main`, two jobs:
- **Lint + test** — `ruff check . --select E9,F821,F822,F823` (errors only by
  design — the full default ruleset flags ~830 pre-existing style issues
  that aren't bugs) + `pytest` (sqlite-backed).
- **Migration chain (Postgres)** — spins up a real Postgres 16 container and
  runs `alembic upgrade head` against it from scratch. This is the only
  place a broken `down_revision` link, duplicate/missing head, or bad
  migration SQL gets caught before it reaches production.

**Deploy:** `main` auto-deploys to **Railway** (`railway.toml`: Dockerfile
build, `preDeployCommand = "alembic upgrade head"`, health check on `/`,
restart on failure ×3). Railway's `checkSuites` flag makes deploy wait for
the `Backend CI` GitHub check to go green before it even attempts the
migration against production — **there is no staging environment and no
manual approval gate**, so a bad migration runs directly against
production Postgres (hosted on Supabase, reached via `DATABASE_URL`) if it
slips past CI.

### front_end_desiconnect — `.github/workflows/build.yml` ("iOS Build Check")
Runs on `macos-15` for every push/PR to `main`: resolves Swift packages,
builds the `Jorna` scheme for iOS Simulator (Debug, unsigned), then runs
`xcodebuild test`. There's no meaningful test suite yet beyond unmodified
Xcode scaffolding — CI compiling successfully is effectively the gate.
App Store distribution is a manual/TestFlight process, not part of this
workflow.

### jorna-website — `.github/workflows/ci.yml` ("CI")
Runs on every push/PR to `main`, two jobs:
- **build** — lint, typecheck, Vitest, then `next build` (static export).
- **e2e** — Playwright against a real Chromium driving `next dev`, with
  every backend call intercepted at the network layer (a fake
  `NEXT_PUBLIC_API_BASE_URL` host makes any unmocked route 404 loudly
  instead of silently hitting production).

**Deploy:** `npm run deploy` — `npm ci` (not local `node_modules`), builds
into `public/app`, `wrangler pages deploy public`, then polls every route on
`https://jornaevents.com` and re-deploys until three consecutive sweeps all
return 200. `npm run deploy:once` skips the `npm ci` + verification loop.
Deploy is **not gated on CI** — it's a manually invoked script, so a green
CI run and an actual deploy are two separate steps. Dependabot opens weekly
update PRs for `web/`, root (wrangler), and GitHub Actions.

`*.pages.dev` is **not** a usable staging URL — backend CORS only allows
`https://jornaevents.com`, so every sign-in/booking/listing call fails CORS
on the `.pages.dev` preview host.

---

## Sentry pipeline

All three clients share one pattern, explicitly mirrored across repos:
**entirely a no-op until a DSN is configured**, so unconfigured local dev
never touches Sentry, and a privacy stance of no default PII plus explicit
scrubbing of auth material.

| Repo | SDK | Init location | DSN source |
|---|---|---|---|
| Desiconnect | `sentry-sdk` (Python) | `app/observability.py`, called first in `main.py`, before router imports | `SENTRY_DSN` env var |
| front_end_desiconnect | Sentry Cocoa SDK | `SentryReporting.start()`, called from `AppDelegate.didFinishLaunchingWithOptions` | `SentryConfig.swift` (hardcoded DSN constant) |
| jorna-website | `@sentry/browser` (not `@sentry/nextjs` — app is fully static-exported, no Next server/edge runtime) | `initSentry()` in `web/src/lib/sentry.ts`, run from `SentryRuntime.tsx`, mounted in `web/src/app/layout.tsx` | `NEXT_PUBLIC_SENTRY_DSN` build-time env var (must be baked in at build time via Cloudflare Pages build env — a runtime-only var won't reach the client bundle) |

**Desiconnect (backend) detail:**
- `environment` auto-inferred (`production` if `DATABASE_URL` isn't sqlite,
  else `development`), overridable via `SENTRY_ENVIRONMENT`.
- `release` = Railway's injected `RAILWAY_GIT_COMMIT_SHA` — automatic, no
  manual release tagging.
- `traces_sample_rate` defaults to `0.0` (errors only, no perf tracing, to
  stay within free-tier volume).
- `before_send` hook (`_scrub`) strips `Authorization`/`Cookie`/`X-Api-Key`
  headers and all cookies from outgoing events, on top of
  `send_default_pii=False` — defense in depth against a bearer token or
  session cookie leaking into an error report.

**front_end_desiconnect (iOS) detail:**
- `environment` is `"debug"` in DEBUG builds, `"production"` otherwise.
- `releaseName` = `<bundle id>@<marketing version>+<build number>`.
- `attachScreenshot` / `attachViewHierarchy` disabled, `tracesSampleRate =
  0.0` — matches the backend's conservative privacy/volume stance.
- **Doc drift note:** `front_end_desiconnect/docs/ARCHITECTURE.md` states
  the DSN is "currently a blank placeholder" awaiting a real Sentry
  project — but `SentryConfig.swift` in the repo now holds a live-looking
  DSN (`https://733e...@o4511701082701824.ingest.us.sentry.io/...`). Code
  is ground truth here; the doc is stale and should be updated to reflect
  that Sentry is actually wired up.

**jorna-website (web) detail:**
- `environment` = `NEXT_PUBLIC_SENTRY_ENVIRONMENT` if set, else `NODE_ENV`.
- `sendDefaultPii` left off, matching the backend/iOS stance.
- Setting the real DSN needs a Sentry project (out of repo scope — dashboard
  access) plus a Cloudflare Pages **build-time** env var.

---

## CI/CD hardening status (as of 2026-08-31)

A follow-up review found CI existed in all three repos but wasn't reliably
enforced or connected to deploy. Status of closing those gaps:

| Gap | jorna-website | Desiconnect | front_end_desiconnect |
|---|---|---|---|
| Branch protection on `main` (PR required, status checks required, no force-push/delete) | **Done** — `required_pull_request_reviews` (0 approvals required, solo dev) + `required_status_checks` on `Lint, typecheck, test, build` and `E2E (Playwright)` | **Not done** | **Not done** |
| Deploy coupled to CI | **In progress** — PR #12 adds a `deploy` job gated on CI passing on a push to `main`; open, not yet merged (waiting on `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID` repo secrets so the first run succeeds cleanly) | Already existed — Railway's `checkSuites` flag gates `alembic upgrade head` on `Backend CI` going green | N/A — no CD pipeline exists for iOS; App Store/TestFlight release stays manual |
| Failure alerting | GitHub-native only (no new webhook/integration by design) — relies on the account's GitHub notification settings for failed workflow runs; not yet independently confirmed as configured | same | same |

**Why Desiconnect and front_end_desiconnect are still unprotected:** both
are owned by the `knag9753` GitHub account; the session that did this work
was authenticated as `dabkeyanik`, which has push access but not admin on
those two repos — the branch-protection API call 404s (GitHub masks a
permission error as "not found" on this endpoint) without admin. Setting it
there needs either a `gh auth` session actually authenticated as `knag9753`,
or someone with admin access running the equivalent `gh api -X PUT
repos/knag9753/<repo>/branches/main/protection` call directly. Until then,
**a direct `git push origin main` on either repo still bypasses CI** —
`CLAUDE.md`'s "merge to main only when told" rule is convention only, not
enforced, on these two.

## Sources
- `Desiconnect/.github/workflows/ci.yml`, `railway.toml`,
  `server/app/observability.py`, `server/app/config.py`,
  `docs/ARCHITECTURE.md`, `CLAUDE.md`
- `front_end_desiconnect/.github/workflows/build.yml`,
  `Jorna/Jorna/SentryConfig.swift`, `SentryReporting.swift`,
  `docs/ARCHITECTURE.md`, `CLAUDE.md`
- `jorna-website/.github/workflows/ci.yml`, `DEPLOY.md`,
  `web/src/lib/sentry.ts`, `web/src/components/SentryRuntime.tsx`,
  `docs/ARCHITECTURE.md`, `CLAUDE.md`

Snapshot as of 2026-08-31 — re-verify against each repo's own docs/code
before relying on specifics (env vars, sample rates, CI job names) for a
change.
