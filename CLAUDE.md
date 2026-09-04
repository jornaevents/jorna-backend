# Desiconnect backend — Agent Guide

Desiconnect (product name **Jorna**) is a **marketplace for planning South
Asian events** — weddings and large celebrations. Clients assemble a team of
vendors (venue, catering, DJ, dhol, mehndi, photography, …); vendors list
services and get booked. Payments run through **Stripe Connect escrow**: a
client's money is held until the event happens and both sides confirm, then
it's released (or auto-released after a deadline — see `docs/DECISIONS.md`).
A chatbot ("bundle builder") can assemble a full vendor bundle for a client
conversationally.

This repo (`knag9753/Desiconnect`) is the **FastAPI + SQLAlchemy** backend,
deployed on **Railway** against **Postgres**. It is the schema/business-logic
source of truth for two sibling client repos, each with its own `CLAUDE.md`:

- `knag9753/front_end_desiconnect` (`…/GitHub/front_end_desiconnect`) — native
  iOS SwiftUI client.
- `jornaevents-commits/jorna-website` (`…/GitHub/jorna-website`) — Next.js
  web app. Transferred from `dabkeyanik/jorna-website` in 2026-08;
  `jornaevents-commits` is the account driving development on it going
  forward (old URLs still redirect).

A booking/pricing/escrow change almost always touches this repo plus one or
both clients. If a client repo's docs describe backend behavior differently
than this repo's code, **this repo's code wins** — those are client-side
descriptions of a contract they don't own.

## Read this first, then navigate — don't read the whole repo

1. [docs/MODULE_MAP.md](docs/MODULE_MAP.md) maps every subpackage under
   `server/app/` to its responsibility — start here to find where a change
   belongs.
2. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) covers app structure, the
   SQLAlchemy models, the Alembic migration workflow, the Stripe escrow flow,
   and observability (Sentry).
3. [docs/API.md](docs/API.md) is the canonical HTTP API surface — the two
   client repos' own `docs/API.md` describe this from the outside and can
   drift; this one is ground truth.
4. [docs/DECISIONS.md](docs/DECISIONS.md) explains *why* things are built the
   way they are — check it before assuming something is an oversight.
5. If `.claude/context/current-task.md` is non-empty, read it first — it's a
   handoff summary of unfinished work from a prior session.
6. Only then read source. Source is the ultimate ground truth — if a doc and
   the code disagree, trust the code and fix the doc.
7. [docs/TESTING.md](docs/TESTING.md) for how to run/extend the test suite.

## Critical rules

- **`main` auto-deploys to Railway on every push, and the deploy runs Alembic
  migrations against production Postgres first** (`railway.toml`'s
  `preDeployCommand = "alembic upgrade head"`, `server/alembic/`). There is no
  staging environment and no manual approval gate. Branch for all changes;
  merge to `main` only when the user says to deploy, and be especially
  careful with any migration — a bad one runs against production data with no
  in-between check.
- **There are two alembic setups in this repo and only one is real.** The
  root `alembic.ini` + `migrations/` directory is a stale leftover (4 old
  migrations, last touched early in the project) — **don't use it**. The live
  one is `server/alembic.ini` + `server/alembic/` (45 migrations, this is
  what Railway runs). Always `cd server` before any `alembic` command.
- **`src/`, `index.html`, `vite.config.ts`, and `package.json` at the repo
  root are a stale Figma-Make-generated Vite prototype** ("Event Planning
  Marketplace"), not the production web app. The real, deployed web frontend
  is the separate `jorna-website` repo (Next.js on Cloudflare Pages). Don't
  edit these root files expecting them to affect anything users see.
- **Tests use a local SQLite file** (`server/tests/test_api.py` sets
  `sqlite:///./test.db`), not Postgres — no external DB or `DATABASE_URL`
  needed to run the suite.
- **Use the repo's venv, not system Python**, for anything backend-related:
  `server/venv/bin/python`. (`server/venv/bin/pip install ruff` etc.)
- Error monitoring is already wired: `server/app/observability.py` calls
  Sentry's SDK when `SENTRY_DSN` is set, tagging events with `environment`
  and a `release` (Railway's commit SHA) automatically. No-op locally unless
  you set `SENTRY_DSN`.
- `ig_scraper/` is a standalone Instagram vendor-data scraper, unrelated to
  the FastAPI app's runtime — it doesn't get imported by `server/`.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## Diagnosing a failed Railway deploy

The production database is **Supabase Postgres**, reached only via the
`DATABASE_URL` env var on the `Desiconnect` service — **not** the `Postgres`
plugin sitting in the same Railway project (`superb-encouragement`), which is
an unused/empty leftover and easy to check by mistake.

Railway's `checkSuites` deploy-trigger flag is **on**: a push to `main` now
waits for the `Backend CI` GitHub check suite (`Lint + test` +
`Migration chain (Postgres)`, `.github/workflows/ci.yml`) to go green before
Railway even attempts `alembic upgrade head` against production. The
migration-chain CI job catches a broken `down_revision` link, a duplicate
head, or bad migration SQL — it does **not** catch a migration that was run
directly against prod without ever being committed (that's what caused the
2026-08-30 incident: `alembic_version` was stamped to a revision, `0045_
vendor_specializations`, that existed nowhere in git — fix was restamping to
the real current head).

```bash
railway login                                  # first time in a fresh session; opens a browser
railway link --project superb-encouragement    # picks workspace/env interactively — run these two yourself
railway service Desiconnect                    # link the service (not the Postgres plugin)

railway status                                 # is the current deploy Online / Building / Deploy failed
railway logs --deployment --latest --lines 100 # deploy-phase logs even if the deploy failed
railway redeploy --service Desiconnect --yes   # retry after a fix

# One-off read against the real prod DB, using Railway's own injected
# DATABASE_URL rather than a value copy-pasted (and possibly mistyped) by hand:
railway run --service Desiconnect bash -c 'psql "$DATABASE_URL" -c "SELECT version_num FROM alembic_version;"'
```

**Alembic gotcha:** a migration's filename and its actual `revision = "..."`
string inside the file are not always the same (e.g.
`0044_add_booking_client_note.py` internally has
`revision = "0044_booking_client_note"`, no "add_"). When restamping
`alembic_version` by hand, use `alembic heads` / grep the file's `revision =`
line — never assume the filename is the ID.

## Commands

```bash
cd server
venv/bin/pip install -r requirements.txt      # install/refresh deps
venv/bin/python -m pytest -q                  # run the full test suite
venv/bin/pip install ruff && venv/bin/ruff check .   # lint (see docs/TESTING.md for scope)
venv/bin/python -m uvicorn main:app --reload  # run locally (needs a .env — see app/config.py)
venv/bin/alembic upgrade head                 # apply migrations (server/alembic/, NOT root alembic.ini)
```

CI (`.github/workflows/ci.yml`, job `Backend CI`) runs two jobs on every PR
into `main`: `Lint + test` (ruff + pytest, sqlite-backed) and
`Migration chain (Postgres)` (applies the full Alembic chain to a clean
Postgres 16 container). Railway waits for both to pass before deploying —
see "Diagnosing a failed Railway deploy" above.

## Keeping this doc layer current

- Keep this file short (index + rules, not a repo dump). Longer explanation
  belongs in `docs/ARCHITECTURE.md` or `docs/MODULE_MAP.md`, linked from here.
- When you make a meaningful architectural change (new subsystem, changed
  data flow, a decision worth remembering, a new migration convention),
  update the relevant `docs/*.md` in the same change — don't let them drift.
- **When a task gets complex or context is filling up**, write a concise
  handoff (goal, status, files changed, key decisions, remaining work,
  blockers, verification status — under ~2,000 tokens) to
  `.claude/context/current-task.md` so a fresh session can resume with "Read
  `.claude/context/current-task.md` and continue the task." Overwrite it per
  task rather than accumulating history.
