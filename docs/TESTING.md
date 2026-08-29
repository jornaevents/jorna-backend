# Testing

## Running the suite

```bash
cd server
venv/bin/python -m pytest -q
```

No setup needed — tests run against a local SQLite file (`test.db`, created
fresh via `tests/test_api.py`), no `DATABASE_URL` or external services
required. Currently 762 passing, 21 skipped (the skips are mostly
integration tests gated on real credentials — e.g. `test_google_register.py`
skips without a `client_secret.json`), runs in under 10 seconds.

`tests/live_llm_test.py` is a manual integration script, not a pytest
module — it makes real HTTP calls against a locally running server and is
excluded from collection (`conftest.py`'s `collect_ignore`). Run it directly
with a server already up: `venv/bin/python tests/live_llm_test.py`.

## Convention

One test file per feature area under `server/tests/`, named to match the
router/service it covers (e.g. `test_bookings.py`, `test_negotiations.py`,
`test_vendor_categories.py`) — see `docs/MODULE_MAP.md` for the full
router/service list this mirrors. `conftest.py` imports shared fixtures
(`client`, `TestingSessionLocal`, `make_auth_headers*`) from `test_api.py`
before any test module is collected, and sets
`JORNA_DISABLE_GEOCODE=1` so tests never hit a real geocoding service.

When adding a feature: put its tests in a new or matching file rather than
appending to an unrelated one, and reuse `make_auth_headers`/
`make_auth_headers_from_parts` from `test_api.py` rather than re-deriving a
JWT by hand.

## CI

`.github/workflows/ci.yml` runs on every push/PR to `main`: install deps,
lint, then the full suite. Lint is intentionally narrow —
`ruff check . --select E9,F821,F822,F823` (syntax errors + undefined names
only), because the full default rule set currently flags ~830 pre-existing
style issues that aren't bugs (see `docs/DECISIONS.md` #9). No formatter is
enforced yet for the same reason — introducing one now would produce a
repo-wide reformat diff unrelated to whatever change triggered it.

If you want to clean up the existing style debt: `ruff check .` (no
`--select`) shows the full list, and `ruff check . --fix` auto-fixes most of
it (~480 of ~830 as of this writing) — worth doing as its own dedicated PR,
not mixed into a feature change.
