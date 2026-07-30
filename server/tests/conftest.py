import os

# No geocoding in tests. Reading a plan or saving an address would otherwise
# reach a government server on every call — slow, and a suite that fails when
# the wifi drops is one people learn to ignore. "No pin" is a state every
# caller already handles, so the tests exercise the same paths without it.
os.environ.setdefault("JORNA_DISABLE_GEOCODE", "1")

# Ensure test_api setup (env vars, DB, client) runs before any test file is collected.
from tests.test_api import client, TestingSessionLocal, make_auth_headers, make_auth_headers_from_parts  # noqa: F401

# live_llm_test.py is a manual integration script (not a pytest module): it runs
# top-level HTTP calls against a locally-running server at import time, which
# aborts collection when no server is up. Exclude it from automated runs — run
# it directly with `python tests/live_llm_test.py` against a live server.
collect_ignore = ["live_llm_test.py"]
