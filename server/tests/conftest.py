# Ensure test_api setup (env vars, DB, client) runs before any test file is collected.
from tests.test_api import client, TestingSessionLocal, make_auth_headers, make_auth_headers_from_parts  # noqa: F401
