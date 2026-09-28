# Staging

Every merge to `main` deploys to **staging** first, then waits for a
reviewer to approve **production**. Staging is a second Railway environment
of the same project (`superb-encouragement` → `staging`) with its own empty
Postgres, so migrations and new code run somewhere real before they touch
customer data.

```
merge to main ─► Lint + test ─┐
                Migration chain ┴─► Deploy to staging ─► (approve) ─► Deploy to production
```

- **Staging API:** the `Desiconnect` service's domain in the `staging`
  environment (stored as `STAGING_API_BASE_URL` in jorna-website's Actions
  variables). Both web apps' PR previews and `staging` Pages branches call it.
- **Staging DB:** the `Postgres` service in the `staging` environment — not
  Supabase. It starts empty; sign up test accounts through a staging web app.
- **Approving production:** Actions tab → the run → "Review deployments" →
  `production` → Approve. Check the change on staging first.
- **Rolling back:** revert the PR. Its merge deploys to staging, then
  production after approval. Never deploy by hand.

## What staging doesn't do

These are left unconfigured on purpose, so staging can't reach real people or
real files:

| Service | Env var left unset | Effect on staging |
| --- | --- | --- |
| Email (Resend) | `RESEND_API_KEY` | No emails sent; flows that email still succeed |
| Push (Firebase) | `FIREBASE_CREDENTIALS_PATH` file | No push notifications |
| File uploads (Supabase Storage) | `SUPABASE_SERVICE_KEY` | Uploads return a 500 "not configured" error |
| Google Calendar | `GOOGLE_CLIENT_SECRET_PATH` file | Calendar connect fails |
| Stripe | `STRIPE_*`, `ESCROW_ENABLED=false` | Same as production: no escrow |

`SUPABASE_URL` is set, so Google sign-in still verifies tokens. If staging
needs uploads later, give it its own Supabase project rather than
production's service key.

## One-time setup

Run these yourself; `railway login` and `link` open a browser or prompt.

1. **Create the environment.** Railway dashboard → `superb-encouragement` →
   environment switcher → New Environment → **Duplicate** `production`, name
   it `staging`. Duplicating copies services (including an empty `Postgres`)
   and variables; the next step replaces the ones that must differ.
2. **Point staging at its own database and keep it isolated.** In `staging`,
   on the `Desiconnect` service:
   ```bash
   railway link --project superb-encouragement --environment staging
   railway variables --service Desiconnect \
     --set 'DATABASE_URL=${{Postgres.DATABASE_URL}}' \
     --set "SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
     --set ESCROW_ENABLED=false \
     --set SENTRY_ENVIRONMENT=staging \
     --set 'ALLOWED_ORIGINS=http://localhost:3000' \
     --set 'ALLOWED_ORIGIN_REGEX=^https://([a-z0-9-]+\.)?(jorna-events|jorna-vendor)\.pages\.dev$' \
     --set WEB_APP_URL=https://staging.jorna-events.pages.dev/app \
     --set FRONTEND_URL=https://staging.jorna-events.pages.dev
   ```
   Then **delete** from staging's `Desiconnect` variables anything copied from
   production that reaches real people or data: `RESEND_API_KEY`,
   `SUPABASE_SERVICE_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
   `INITIAL_ADMIN_EMAIL`, and any Google/Firebase credential variables.
   Double-check `DATABASE_URL` no longer mentions `supabase`:
   `railway variables --service Desiconnect | grep DATABASE_URL`.
3. **Turn off Railway's own GitHub autodeploy** for staging's `Desiconnect`
   (Settings → Source), the same as production — CI deploys it.
4. **Give it a domain.** Staging `Desiconnect` → Settings → Networking →
   Generate Domain. Note the URL.
5. **Create a staging project token.** Project Settings → Tokens → New token,
   environment `staging`. Note the environment ID too:
   `railway status --json | jq -r .environments` (or the `environmentId=`
   in the dashboard URL).
6. **Wire up GitHub** (jornaevents/jorna-backend → Settings → Environments):
   - `staging`: secret `RAILWAY_TOKEN` = the staging token; variable
     `RAILWAY_ENVIRONMENT_ID` = staging's environment ID.
   - `production`: secret `RAILWAY_TOKEN` = the existing production token
     (move it here from the repo-level secret, then delete the repo-level
     one); required reviewers = the backend leads; deployment branches =
     `main` only.

   ```bash
   pbpaste | gh secret set RAILWAY_TOKEN --repo jornaevents/jorna-backend --env staging
   gh variable set RAILWAY_ENVIRONMENT_ID --repo jornaevents/jorna-backend --env staging --body <id>
   ```
   (Pipe secrets in: `gh secret set` run through Claude Code's `!` prefix
   saves an empty value.)
7. **Tell the web apps.** In jornaevents/jorna-website → Settings → Secrets
   and variables → Actions → Variables, set `STAGING_API_BASE_URL` to the
   domain from step 4 (no trailing slash).

Required reviewers on an environment need a public repo or a paid GitHub
plan; on a private repo under GitHub Free the `production` job runs without
waiting.
