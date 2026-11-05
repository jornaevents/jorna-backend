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

- Both branches are **pushed, no PRs**. **Don't open the PRs until the user
  agrees.**
- Live test done 2026-11-05 with the staging key (it's a plain
  `OPENROUTER_API_KEY` env var in the session, not a proxy secret). Free
  Gemma 4 31B (and 26B) returned 429 all day: Google AI Studio's shared free
  pool is their only provider. At the user's request staging now defaults to
  OpenRouter's Free Models Router, `SUMMARY_MODEL=openrouter/free`; the
  router is asked again (up to 3×) when it lands on a model that refuses
  `reasoning: {enabled: false}`; `clean_line` drops a trailing "NONE".
  Result: 11/15 accepted, no errors (details in the PR draft). Outside
  staging the default is still Gemma; **production needs a pinned model**
  (DECISIONS #42), also listed in the proposal doc.
- Tests 1342 passed, 21 skipped; CI's ruff selection clean.
- Defaults: `SUMMARIES_ENABLED` and `SUMMARY_ALLOW_PROVIDER_LOGGING` on in
  Railway staging, off elsewhere; `SUMMARY_DAILY_LIMIT` 50.

## Starting a new session

Check out `claude/festive-fermi-4oqbv9` in **both** repos first (`git fetch
origin claude/festive-fermi-4oqbv9 && git checkout claude/festive-fermi-4oqbv9`);
this note and the code only exist on that branch.

## Next step

When the user agrees: open both PRs from
`.claude/context/pr-drafts-inbox-summaries.md` (results already filled in;
delete that file and empty this note in the same change). Re-run the eval
first if the code changed: `cd server && RAILWAY_ENVIRONMENT_NAME=staging
venv/bin/python -m scripts.eval_summaries` (~20 free calls).

## Where things are

- Backend: `server/app/services/summary_service.py` (sweep, read path,
  checks, prompt), `server/scripts/eval_summaries.py`, migration
  `0084_conversation_summaries`, tests `server/tests/test_conversation_summaries.py`.
- Website: `packages/shared/components/{SummaryPreview,AiSummarySetting}.tsx`,
  both apps' `app/messages/page.tsx`, vendor `components/vendor/MessagesHub.tsx`.
- Proposal doc (Claude Docs): https://claude.ai/artifact/UusFztY6jcqB8hSDw2zqJ9
- After the test: open both PRs. Their bodies are drafted in
  `.claude/context/pr-drafts-inbox-summaries.md` (fill in the live-test
  results, delete that file in the same change). Rollout: merge → staging has it on;
  production stays off until the Privacy Policy update.
