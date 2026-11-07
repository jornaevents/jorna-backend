"""GET /vendors/me/overview (app/services/vendor_overview.py): the vendor web
app's Overview in one request, each section what its own route returns."""

from app.services import vendor_overview as vo
from tests.test_api import client, make_auth_headers
from tests.test_conversation_subjects import _ask, world  # noqa: F401 — world is a fixture


def _get(user, path):
    resp = client.get(path, headers=make_auth_headers(user))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_each_section_is_what_its_own_route_returns(world):  # noqa: F811
    vendor_user = world["vendor_user"]
    vendor_id = world["vendor"].vendor_id
    _ask(world)  # a client's question, so the inbox has something in it

    overview = _get(vendor_user, "/vendors/me/overview")

    assert overview["vendor"] == _get(vendor_user, "/vendors/me")
    assert overview["bookings"] == _get(vendor_user, f"/bookings/vendor/{vendor_id}?limit=100")
    assert overview["leads"] == _get(vendor_user, "/leads")
    assert overview["conversations"] == _get(vendor_user, "/conversations")
    assert overview["pipeline"] == _get(vendor_user, "/leads/pipeline")
    assert overview["conversations"], "the question should be in the inbox"
    assert overview["bookings"]["items"], "the vendor's booking should be listed"


def test_someone_who_isnt_a_vendor_gets_404(world):  # noqa: F811
    resp = client.get("/vendors/me/overview", headers=make_auth_headers(world["client_user"]))
    assert resp.status_code == 404


def test_signed_out_gets_401():
    assert client.get("/vendors/me/overview").status_code == 401


def test_a_failing_section_is_null_and_the_rest_still_come(world, monkeypatch):  # noqa: F811
    def broken(**_):
        raise RuntimeError("leads are down")

    monkeypatch.setattr(vo, "list_leads", broken)
    overview = _get(world["vendor_user"], "/vendors/me/overview")

    assert overview["leads"] is None
    assert overview["vendor"]["vendor_id"] == world["vendor"].vendor_id
    assert overview["bookings"] is not None
    assert overview["conversations"] is not None
    assert overview["pipeline"] is not None


def test_timings_come_back_in_the_body_and_as_server_timing(world):  # noqa: F811
    resp = client.get("/vendors/me/overview", headers=make_auth_headers(world["vendor_user"]))
    assert resp.status_code == 200
    names = ["vendor", "bookings", "leads", "conversations", "pipeline"]
    assert list(resp.json()["timings"]) == names
    header = resp.headers["server-timing"]
    assert [part.split(";")[0] for part in header.split(", ")] == names
    assert all(";dur=" in part for part in header.split(", "))


def test_server_timing_format():
    assert vo.server_timing({"vendor": 1.5, "bookings": 12.0}) == "vendor;dur=1.5, bookings;dur=12.0"
