"""A package's own contract template (docs/DECISIONS.md #31): every package
has one, built by the web from the package until the vendor edits it. The
edited copy is stored linked to the package — one per package, only the
vendor's own, only an agreement."""

from app.db.models import ContractTemplate
from tests.test_api import TestingSessionLocal, client
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _isolate,
    _setup_vendor,
)

BODY = {"version": 1, "title": "Full day agreement", "lines": [{"kind": "package", "name": "Full day"}]}


def _templates(v):
    return client.get("/contract-templates", headers=v["headers"]).json()["items"]


def _cleanup(*template_ids):
    db = TestingSessionLocal()
    db.query(ContractTemplate).filter(ContractTemplate.template_id.in_(template_ids)).delete(synchronize_session=False)
    db.commit()
    db.close()


def test_an_edited_package_template_is_stored_against_its_package():
    v = _setup_vendor()
    r = client.post(
        "/contract-templates", json={"name": "Full day", "body": BODY, "service_id": v["service_id"]},
        headers=v["headers"],
    )
    assert r.status_code == 201, r.text
    made = r.json()
    assert made["service_id"] == v["service_id"]
    assert [t["service_id"] for t in _templates(v)] == [v["service_id"]]

    # Editing it again is the same row.
    again = client.post(
        "/contract-templates", json={"name": "Full day", "body": BODY, "service_id": v["service_id"]},
        headers=v["headers"],
    )
    assert again.status_code == 409
    edited = client.patch(
        f"/contract-templates/{made['template_id']}", json={"body": {**BODY, "title": "Wedding day"}},
        headers=v["headers"],
    )
    assert edited.json()["body"]["title"] == "Wedding day"
    assert edited.json()["service_id"] == v["service_id"]

    # Reset to the package is deleting the copy.
    assert client.delete(f"/contract-templates/{made['template_id']}", headers=v["headers"]).status_code == 200
    assert _templates(v) == []


def test_ordinary_templates_have_no_package():
    v = _setup_vendor()
    a = client.post("/contract-templates", json={"name": "Sangeet", "body": BODY}, headers=v["headers"]).json()
    b = client.post("/contract-templates", json={"name": "Mehndi", "body": BODY}, headers=v["headers"]).json()
    assert a["service_id"] is None and b["service_id"] is None
    _cleanup(a["template_id"], b["template_id"])


def test_only_your_own_package_and_only_a_contract():
    v = _setup_vendor()
    other = _setup_vendor()
    theirs = client.post(
        "/contract-templates", json={"name": "x", "body": BODY, "service_id": other["service_id"]},
        headers=v["headers"],
    )
    assert theirs.status_code == 404
    addendum = client.post(
        "/contract-templates",
        json={"name": "x", "body": BODY, "kind": "addendum", "service_id": v["service_id"]},
        headers=v["headers"],
    )
    assert addendum.status_code == 400

    made = client.post(
        "/contract-templates", json={"name": "x", "body": BODY, "service_id": v["service_id"]}, headers=v["headers"],
    ).json()
    turned = client.patch(f"/contract-templates/{made['template_id']}", json={"kind": "addendum"}, headers=v["headers"])
    assert turned.status_code == 400
    _cleanup(made["template_id"])
