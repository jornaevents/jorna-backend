# Draft PR descriptions: inbox AI summaries

Scratch for the session that opens the two PRs (DECISIONS #42). Update the
model and test numbers from the latest commits, then delete this file in
the same change that opens the PRs.

---

## jorna-backend

### Inbox AI summaries: on in staging, off in production (DECISIONS #42)

**Branch:** `claude/festive-fermi-4oqbv9` → `main` · **Pairs with:** jornaevents/jorna-website `claude/festive-fermi-4oqbv9`

## What

A busy thread's inbox row only shows its last message. Behind a new `SUMMARIES_ENABLED` flag (**on by default only in Railway's staging environment; off in production, local and CI**, and an explicit setting overrides either way), a background sweep writes one short AI line per thread. `GET /conversations` returns it as `summary` to a reader with 3+ unread, and the inbox shows it in place of the last message.

Where the flag is off, the sweep isn't started, nothing is written or sent to a model, and every `summary` is `null`. The only change you can see is the migration, plus the always-null field and the `ai_summaries_enabled` user setting appearing in API responses.

## How

- **`app/services/summary_service.py`** (new)
  - **The sweep:** `run_pass`, every 30s from `main.py`, in a worker thread so a model call can't stall requests.
  - **The per-reader read path:** `inbox_summaries`, called from `list_conversations`. At most 3 queries for the whole inbox.
  - **Upkeep:** `forget` and `mark_stale`.
- **No hook on message send.** The sweep compares each thread's newest message with what its row last covered. That covers every message writer (app, offers, system lines, the contract-link client), and turning the flag on needs no backfill.
- **When a thread is summarized:**
  - 90s after it goes quiet, or 5 minutes after the first uncovered message.
  - At most every 2 minutes and 40 times a day per thread, and `SUMMARY_DAILY_LIMIT` (default **50**) model calls a day across all threads.
  - Skipped below 3 meaningful messages. If only "ok"s or emoji arrived since the last line, the line is extended without a model call.
- **Model:** `SUMMARY_MODEL`, default `google/gemma-4-31b-it:free` on OpenRouter (free Llama 3.3 has no providers left), using the existing `OPENROUTER_API_KEY`.
- **Reply checks** (`check_line`), applied before anything is stored:
  - 80 characters at most.
  - No email, phone number, @handle, link or emoji.
  - Every amount must match an offer in the window.
  - "Agreed"/"accepted" only when a negotiation is accepted.
  - Offer lines in the transcript carry the negotiation's current status.
- **Who sees a line:** never a reader who blocked someone it quotes (blocks are otherwise only filtered client-side), or who turned off "Show AI summaries" (`users.ai_summaries_enabled`, on `GET`/`PATCH /me`).
- **Reports:** reporting a message (`POST /reports`) hides its thread's line at once and forces a rewrite that leaves the message out.
- **Privacy:**
  - Prompts and replies are never logged.
  - By default only providers that don't keep prompts are used. `SUMMARY_ALLOW_PROVIDER_LOGGING` opts into ones that do. It defaults on in staging only, because the free Gemma's only provider (Google AI Studio) may keep prompts.
- **`scripts/eval_summaries.py`:** 15 made-up threads through the real model and the reply checks, to try a model or key before turning it on.
- **Live test:** RESULTS GO HERE (from the session that runs the eval).
- **Migration `0084_conversation_summaries`:**
  - The `conversation_summaries` table, with no foreign keys on purpose: the delete cascades in `bundle_service`/`user_service` call `forget()` instead. A key would break any cascade that missed it.
  - `users.ai_summaries_enabled`, default true.
  - An index on `group_messages.created_at`.
- **Docs:** `API.md`, `ARCHITECTURE.md`, `MODULE_MAP.md`, `STAGING.md`, `DECISIONS.md` #42, `.env.example`.

## Testing

- `pytest`: **1333 passed, 21 skipped**. That's the 1247 passing before this change plus 86 new tests in `tests/test_conversation_summaries.py`.
  - **Defaults:** on for staging, off for production/local/CI, explicit override both ways; limit/model defaults; no sweep, no writes; null field; no writes on send.
  - **The setting.**
  - **Display rules:** unread threshold, out-of-date line, blocks, opening the thread, failed rows.
  - **The inbox query count.**
  - **Timing:** busy, 90s, 5-minute ceiling, 2-minute gap, 40/day, 24h lookback.
  - **Run outcomes:** skip and extend paths, NONE, the retry, failures, missing key, a message arriving mid-call, a deleted thread.
  - **Transcript:** labels, the contract-link client, injection escaping, offer status.
  - **Upkeep:** reports, account deletion, the daily cap, full passes, one broken thread not stopping the rest, every `check_line` rule.
- `ruff check . --select E9,F821,F822,F823` (CI's selection): clean.
- **Migration on real Postgres 16**, same as CI's job: `alembic upgrade head` → `downgrade -1` → `upgrade head` all clean. `scripts.check_migration_state` accepts 0084. `alembic check` shows only differences that were already there, none for the new objects.
- **Smoke test on Postgres:** `run_pass` → `inbox_summaries` → `mark_stale` → `forget` against the migrated DB, with a fake model.
- **App start:** with `SUMMARIES_ENABLED` unset, the sweep task isn't created; with it `true`, it runs. `/health` is 200 in both cases.

## Before turning it on

- Update the Privacy Policy as DECISIONS #42 says, and say that providers may keep the text if `SUMMARY_ALLOW_PROVIDER_LOGGING` is turned on.
- Confirm the free model's slug and OpenRouter's free-tier request limits for our account. 50/day plus at most one retry per thread should fit.
- Staging has it on already; give staging its own OpenRouter key with a $0 credit limit (free models only), then run the evaluation threads.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01DAhizv8mz4YJiZQimnkdNw

---

## jorna-website

### Show AI summaries in the Messages inbox

**Branch:** `claude/festive-fermi-4oqbv9` → `main` · **Pairs with:** jornaevents/jorna-backend `claude/festive-fermi-4oqbv9` (DECISIONS #42)

## What

Draws the backend's new `summary` field: a one-line AI description of a busy thread, shown in place of its last message. Adds a "Show AI summaries" account setting. The backend sends `summary: null` until `SUMMARIES_ENABLED` is turned on, and older servers don't send it at all, so this is safe to merge before or after the backend PR. Until then, the inbox looks and behaves exactly as today.

## How

- **`@jorna/shared/components/SummaryPreview`** (new)
  - A sparkle icon and the line, clamped to 2 lines instead of the preview's one-line truncate, since the line is written to fit 80 characters.
  - Labelled "AI summary, may miss details" for screen readers and on hover.
  - A `<span>`, so it's valid inside the vendor hub's button rows.
- **Inbox rows:**
  - Client app `app/messages/page.tsx`, the vendor app's client-side list in `app/messages/page.tsx`, and the vendor hub (`components/vendor/MessagesHub.tsx`) render it when `summary.text` is set **and the row still has unread**.
  - The hub zeroes a thread's count when it's opened without refetching, so the line goes with it.
- **`@jorna/shared/components/AiSummarySetting`** (new)
  - A "Messages" card with the checkbox, in the client's `/account` and the vendor's `AccountSettings` (`/account` and `/settings`).
  - Saves with `updateMe({ ai_summaries_enabled })`. The box moves at once and goes back if the save fails.
  - A user from a server without the field reads as on.
  - The copy says it only hides summaries for you, matching the Privacy Policy.
- **Types:** `summary` on `ConversationSummary` (both apps), `ai_summaries_enabled` on the shared `User` and on both apps' `MeUpdate`.
- **Docs:** a decision entry in both apps' `docs/DECISIONS.md`, and the shared README.

## Testing

- **Lint and typecheck:** both apps and `@jorna/shared` typecheck clean. Lint has no errors, and no new warnings in changed files.
- **Vitest:** client 69, vendor 181, shared 109, all passing.
- **`next build`** passes for both apps.
- **Playwright, client:** full suite **53/53**, including the new `e2e/messages-summaries.spec.ts`:
  - A summary replaces the last message, with its label.
  - An old server with no field changes nothing.
  - A summary on a row with nothing unread isn't shown.
  - The setting is on by default, turns off with the right PATCH body, and reverts on a failed save.
- **Playwright, vendor:** full suite **217/217** (both the escrow-on and escrow-off projects), including new tests in `vendor-messages.spec.ts` (hub summary, which disappears when the thread is opened) and `vendor-settings.spec.ts` (the setting turns off).

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01DAhizv8mz4YJiZQimnkdNw
