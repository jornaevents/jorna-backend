# Jorna backend (`Desiconnect`)

The API behind **Jorna**, a marketplace for planning South Asian weddings and
celebrations: clients put together a team of vendors (venue, catering, DJ,
dhol, mehndi, photography, …) and vendors sell to them. FastAPI + SQLAlchemy,
Postgres in production, deployed on Railway.

It's the source of truth for three clients, each in its own repo:

| Client | Repo | Serves |
| --- | --- | --- |
| Vendor web app | `jorna-vendor` | [jornaevents.com](https://jornaevents.com) — vendors, and the no-login contract signing page |
| Client web app | `jorna-website` | [book.jornaevents.com](https://book.jornaevents.com) — hosts planning and booking |
| iOS app | `front_end_desiconnect` | both sides, in one app |

If a client repo's docs describe backend behaviour differently from the code
here, the code here wins.

## What it does

- **Accounts** — email/password with Jorna's own JWTs (rotating refresh
  tokens); Google sign-in through Supabase as an identity provider only.
- **Vendors and packages** — listings, packages with inclusions and add-ons,
  public / private / archived packages, per-package terms, availability,
  Google Calendar sync, location search.
- **Planning and requests** — clients build a plan (by hand or with the AI
  bundle builder) and send requests to vendors; messaging, price
  negotiation and date-change requests between them.
- **Contracts** — a vendor's proposal: several packages, add-ons and custom
  lines, a discount, a payment schedule, and clause text. It's sent as a
  no-login link the client signs by typing their name. A sent contract holds
  the date for a set number of days (7 by default); signing makes it final
  and freezes a SHA-256-fingerprinted copy of exactly what was signed. Every
  contract has a timeline. Accepting a marketplace request turns it into a
  contract the client signs the same way.
- **Payments** — Jorna doesn't hold money for now: clients pay vendors
  directly (Venmo/Zelle) and both sides mark each payment sent / received.
  The Stripe Connect escrow track still exists behind `ESCROW_ENABLED`,
  which is off in production.
- **Background jobs** (in `main.py`) — payment reminders (the client before
  and on each due date, the vendor when a payment is overdue), check-in
  reminder emails, unread-message digests, Google Calendar renewal, token
  cleanup.
- Day-of check-in (GPS), reviews, guest lists / RSVPs, admin and moderation.

Why things are built the way they are is in
[`docs/DECISIONS.md`](docs/DECISIONS.md) — the contract model is #13 and
#15–#18.

## Layout

```
server/
  main.py              app, routers, background jobs
  app/routers/         HTTP layer — parse, call a service, map errors
  app/services/        business logic
  app/db/models.py     SQLAlchemy models
  alembic/versions/    migrations (the live chain — always run alembic from server/)
  scripts/predeploy.py migration guard + `alembic upgrade head`, run before each deploy
  tests/               pytest suite (SQLite, no external services)
docs/                  architecture, module map, API surface, decisions, testing
.github/workflows/     CI and the production deploy
ig_scraper/            standalone Instagram scraper, not part of the app
src/, index.html, vite.config.ts, package.json
                       an old Figma-generated prototype — not the real web app
```

Start with [`docs/MODULE_MAP.md`](docs/MODULE_MAP.md) to find where a change
belongs, and [`docs/API.md`](docs/API.md) for the endpoints (or
`/docs` on a running server for the live OpenAPI spec).

## Running it locally

```bash
cd server
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill in SECRET_KEY at least
venv/bin/python -m uvicorn main:app --reload
```

The API is at `http://localhost:8000`, docs at `/docs`. With no
`DATABASE_URL` it uses a local SQLite file and creates the tables on start.

- **Keep `ESCROW_ENABLED=false`** in `.env`, as production does — with escrow
  on, the server refuses to start without Stripe keys.
- **Never point a local server at production.** If your `.env` has the real
  `DATABASE_URL` or `RESEND_API_KEY`, a local run reads and writes production
  data and sends real email.
- Email (`RESEND_API_KEY`), push (`FIREBASE_CREDENTIALS_PATH`), Google
  Calendar (`GOOGLE_CLIENT_SECRET_PATH`) and Sentry (`SENTRY_DSN`) are all
  optional: without them those features no-op and everything else works.
- To run the web apps against it, start them with
  `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000` and add their origins to
  `ALLOWED_ORIGINS`.

## Tests

```bash
cd server
venv/bin/python -m pytest -q          # ~1,000 tests, about 25 seconds
venv/bin/ruff check . --select E9,F821,F822,F823
```

See [`docs/TESTING.md`](docs/TESTING.md). Migrations are checked in CI
against a clean Postgres 16, since SQLite can't replay the chain.

## Deploying

**Merging to `main` is deploying.** CI (`.github/workflows/ci.yml`) runs
`Lint + test` and `Migration chain (Postgres)`; when both pass on a push to
`main`, the `Deploy to Railway` job deploys that exact commit and waits for
it to go live. Railway runs `scripts/predeploy.py` first — a guard that
refuses to deploy if production's migration state is unknown, then
`alembic upgrade head` against production Postgres. There's no staging.

- Branch for every change, and be especially careful with migrations.
- A failed deploy shows as a red `Deploy to Railway` check. Re-run that job
  from the Actions tab rather than redeploying by hand, so what's live is
  always a commit CI tested.
- The deploy needs the `RAILWAY_TOKEN` Actions secret (a Railway project
  token). Railway's own automatic GitHub deploys are off on purpose — see
  "Diagnosing a failed Railway deploy" in [`CLAUDE.md`](CLAUDE.md).
- Production data lives in **Supabase** Postgres, reached through the
  service's `DATABASE_URL` — not the empty `Postgres` service in the same
  Railway project.

## License

Private repository — all rights reserved.
