# Current Task

> Template for handing off in-progress work to a fresh Claude Code session.
> When a task gets complex or the context window is filling up, fill this in
> and tell the user; a new session can then be started with:
> "Read `.claude/context/current-task.md` and continue the task."
>
> Keep the whole file under ~2,000 tokens. This is scratch state for the
> *current* task, not permanent documentation — don't duplicate content that
> belongs in `docs/ARCHITECTURE.md`, `docs/MODULE_MAP.md`, `docs/API.md`, or
> `docs/DECISIONS.md`; link to those instead of restating them. Overwrite this
> file for each new task rather than appending to its history.

## Goal

Inbox AI summaries (DECISIONS #42): a one-line AI summary per busy thread,
shown in the Messages inbox in place of the last message. Backend here,
UI in jorna-website. Both on branch `claude/festive-fermi-4oqbv9`.

## Status

- Both branches are **pushed, no PRs**. The user approved the push only so
  a fresh session (with the OpenRouter key) can run a live test. **Don't
  open the PRs until the live test passes and the user agrees.**
- Code, tests (1333 passed, 21 skipped), Postgres migration check and docs
  are done. Website: lint/typecheck/vitest/builds clean, Playwright client
  53/53 and vendor 217/217.
- Defaults: `SUMMARIES_ENABLED` and `SUMMARY_ALLOW_PROVIDER_LOGGING` are on
  in Railway staging and off everywhere else; `SUMMARY_MODEL` is
  `google/gemma-4-31b-it:free` (free Llama 3.3 has no providers left);
  `SUMMARY_DAILY_LIMIT` 50.

## Next step: the live test

The user saved the staging OpenRouter key as a **network secret**: the
agent proxy adds `Authorization: Bearer <key>` to requests to
`openrouter.ai/api/v1/`. Nobody should paste the key into chat. The code
needs `OPENROUTER_API_KEY` set to *something* before it calls out, so use a
dummy value and let the proxy supply the real header:

```bash
cd server
python3 -m venv venv && venv/bin/pip install -q -r requirements.txt   # fresh container
curl -sS https://openrouter.ai/api/v1/key | head -c 300                # 200 + key info = secret works
OPENROUTER_API_KEY=dummy venv/bin/python -m scripts.eval_summaries --allow-logging
```

If the key call still says "Missing Authentication header", the secret
isn't reaching the session; tell the user (fallback: the key as a plain
env var `OPENROUTER_API_KEY`). If the model call fails for routing/privacy
reasons even with `--allow-logging`, the OpenRouter account's privacy
setting may block free providers that keep prompts (Settings → privacy).

Report to the user: how many of the 15 threads produced a line the checks
accept, the lines themselves against each thread's "wanted", any refusals
and why, rate-limit errors, and latency. Fix real problems (prompt, checks),
re-run, commit, push. The free tier has a small daily request allowance;
one full run is ~15–20 calls.

## Where things are

- Backend: `server/app/services/summary_service.py` (sweep, read path,
  checks, prompt), `server/scripts/eval_summaries.py`, migration
  `0084_conversation_summaries`, tests `server/tests/test_conversation_summaries.py`.
- Website: `packages/shared/components/{SummaryPreview,AiSummarySetting}.tsx`,
  both apps' `app/messages/page.tsx`, vendor `components/vendor/MessagesHub.tsx`.
- Proposal doc (Claude Docs): https://claude.ai/artifact/UusFztY6jcqB8hSDw2zqJ9
- After the test: open both PRs (bodies drafted in the earlier session;
  rewrite from the commits if missing). Rollout: merge → staging has it on;
  production stays off until the Privacy Policy update.
