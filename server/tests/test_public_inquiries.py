"""The public inquiry form, POST /vendors/{id}/inquiries (docs/DECISIONS.md
#32): no account, lands in Leads, emails both sides, and the abuse guards."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.db.models import Lead, Service, User, Vendor
from app.limiter import limiter
from tests.test_api import TestingSessionLocal, client, make_auth_headers

_created = {"users": [], "vendors": [], "services": []}


@pytest.fixture(autouse=True)
def _isolate():
    """No rate limit except where a test turns it on (the suite shares one
    client address), and everything made here is removed afterwards."""
    limiter.enabled = False
    yield
    limiter.enabled = True
    db = TestingSessionLocal()
    if _created["vendors"]:
        db.query(Lead).filter(Lead.vendor_id.in_(_created["vendors"])).delete(synchronize_session=False)
        db.query(Service).filter(Service.vendor_id.in_(_created["vendors"])).delete(synchronize_session=False)
        db.query(Vendor).filter(Vendor.vendor_id.in_(_created["vendors"])).delete(synchronize_session=False)
    if _created["users"]:
        db.query(User).filter(User.user_id.in_(_created["users"])).delete(synchronize_session=False)
    db.commit()
    db.close()
    for v in _created.values():
        v.clear()


@pytest.fixture
def emails():
    with patch("app.services.inquiry_service.send_email", return_value={"success": True, "id": "e1"}) as sent:
        yield sent


def _vendor(package_status: str | None = "active") -> dict:
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    user = User(
        email=f"inq_v_{uid}@test.com", username=f"inq_v_{uid}", password="pw", phone="1",
        f_name="Arjun", l_name="Kapoor", age=30, location="NJ", gender="M", language="EN", token_version=0,
    )
    db.add(user); db.commit(); db.refresh(user)
    vendor = Vendor(user_id=user.user_id, bio="b", category="photography", rating=0.0, num_events=0, payment_method="manual")
    db.add(vendor); db.commit(); db.refresh(vendor)
    if package_status:
        db.add(Service(
            name="Sangeet coverage", price=2500.0, price_unit="event", vendor_id=vendor.vendor_id,
            experience="e", category="photography", negotiable=False, status=package_status,
        ))
        db.commit()
    out = {"vendor_id": vendor.vendor_id, "email": user.email, "headers": make_auth_headers(user)}
    _created["users"].append(user.user_id)
    _created["vendors"].append(vendor.vendor_id)
    db.close()
    return out


def _future(days=200) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()


def _send(vendor_id: str, **overrides):
    body = {
        "name": "Priya Shah", "email": "priya@example.com", "phone": "(732) 555-0100",
        "event_date_iso": _future(), "event_type": "Sangeet", "guest_count": 180,
        "note": "Looking for someone for our sangeet in Edison.",
    }
    body.update(overrides)
    return client.post(f"/vendors/{vendor_id}/inquiries", json=body)


def _leads(vendor_id: str) -> list[Lead]:
    db = TestingSessionLocal()
    try:
        return db.query(Lead).filter(Lead.vendor_id == vendor_id).all()
    finally:
        db.close()


def test_an_inquiry_lands_in_leads_as_needing_the_vendor(emails):
    v = _vendor()
    resp = _send(v["vendor_id"])
    assert resp.status_code == 201, resp.text
    # Nothing about the lead comes back to a public caller.
    assert resp.json() == {"received": True, "vendor_name": "Arjun Kapoor"}

    [lead] = _leads(v["vendor_id"])
    assert (lead.source, lead.status, lead.event_type, lead.guest_count) == ("inquiry_form", "new", "Sangeet", 180)
    assert lead.phone == "(732) 555-0100" and lead.note.startswith("Looking for")

    pipe = client.get("/leads/pipeline", headers=v["headers"]).json()
    item = next(i for i in pipe["items"] if i["id"] == f"lead:{lead.lead_id}")
    assert item["attention"] == "needs_you" and item["attention_reason"] == "new_lead"
    assert (item["lead_source"], item["guest_count"], item["event_name"]) == ("inquiry_form", 180, "Sangeet")

    listed = client.get("/leads", headers=v["headers"]).json()["items"]
    assert listed[0]["source"] == "inquiry_form" and listed[0]["guest_count"] == 180


def test_the_vendor_is_emailed_and_the_sender_gets_a_fixed_confirmation(emails):
    v = _vendor()
    _send(v["vendor_id"], note="<script>x</script> Call me after 6")
    to_vendor, to_sender = (c.kwargs for c in emails.call_args_list)

    assert to_vendor["to"] == v["email"]
    assert to_vendor["reply_to"] == "priya@example.com"
    assert to_vendor["subject"] == "New inquiry from Priya Shah"
    assert "&lt;script&gt;" in to_vendor["html"] and "<script>" not in to_vendor["html"]

    assert to_sender["to"] == "priya@example.com"
    assert to_sender["subject"] == "Your message reached Arjun Kapoor"
    # Nothing the sender typed, so the form can't send text to any inbox.
    for typed in ("Call me", "Priya", "Sangeet", "script"):
        assert typed not in to_sender["html"]


def test_one_confirmation_per_address_per_vendor_per_day(emails):
    v = _vendor()
    _send(v["vendor_id"])
    _send(v["vendor_id"], email="PRIYA@example.com", note="One more thing")
    recipients = [c.kwargs["to"] for c in emails.call_args_list]
    assert recipients == [v["email"], "priya@example.com", v["email"]]
    assert len(_leads(v["vendor_id"])) == 2


def test_unknown_vendors_and_vendors_still_in_setup_are_not_found(emails):
    assert _send(str(uuid.uuid4())).status_code == 404
    assert _send(_vendor(package_status=None)["vendor_id"]).status_code == 404
    assert _send(_vendor(package_status="archived")["vendor_id"]).status_code == 404
    # A hidden package still counts: the vendor is set up, just selective.
    assert _send(_vendor(package_status="hidden")["vendor_id"]).status_code == 201
    assert emails.call_count == 2


@pytest.mark.parametrize("field,value,message", [
    ("name", "  ", "Add your name"),
    ("email", "", "Add your email"),
    ("email", "priya@", "doesn't look right"),
    ("phone", "12", "phone number doesn't look right"),
    ("event_date_iso", "2020-01-01", "already passed"),
    ("event_date_iso", "next fall", "YYYY-MM-DD"),
    ("guest_count", 0, "between 1 and 100,000"),
    ("guest_count", 200_000, "between 1 and 100,000"),
])
def test_each_field_is_checked(emails, field, value, message):
    v = _vendor()
    resp = _send(v["vendor_id"], **{field: value})
    assert resp.status_code == 422, resp.text
    assert message in resp.json()["detail"]
    assert _leads(v["vendor_id"]) == [] and emails.call_count == 0


def test_optional_fields_can_be_left_out(emails):
    v = _vendor()
    resp = client.post(f"/vendors/{v['vendor_id']}/inquiries", json={"name": "Neha", "email": "neha@example.com"})
    assert resp.status_code == 201, resp.text
    [lead] = _leads(v["vendor_id"])
    assert (lead.event_date_iso, lead.guest_count, lead.event_type, lead.phone) == (None, None, None, None)
    assert "Not sure yet" in emails.call_args_list[0].kwargs["html"]


def test_a_filled_honeypot_gets_the_normal_reply_and_nothing_else(emails):
    v = _vendor()
    resp = _send(v["vendor_id"], website="http://spam.example")
    assert resp.status_code == 201
    assert resp.json() == {"received": True, "vendor_name": "Arjun Kapoor"}
    assert _leads(v["vendor_id"]) == [] and emails.call_count == 0


def test_a_vendor_takes_at_most_fifty_inquiries_a_day(emails):
    v = _vendor()
    db = TestingSessionLocal()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for n in range(50):
        db.add(Lead(vendor_id=v["vendor_id"], name=f"L{n}", email=f"l{n}@example.com", status="new",
                    source="inquiry_form", created_at=now - timedelta(hours=1), updated_at=now))
    # Older ones, and ones the vendor added, don't count.
    db.add(Lead(vendor_id=v["vendor_id"], name="Old", status="new", source="inquiry_form",
                created_at=now - timedelta(days=2), updated_at=now))
    db.commit(); db.close()

    resp = _send(v["vendor_id"])
    assert resp.status_code == 429
    assert "try again tomorrow" in resp.json()["detail"]
    assert emails.call_count == 0


def test_five_a_minute_from_one_address():
    v = _vendor()
    limiter.reset()
    limiter.enabled = True
    with patch("app.services.inquiry_service.send_email", return_value={"success": True}):
        codes = [_send(v["vendor_id"], email=f"p{n}@example.com").status_code for n in range(6)]
    assert codes == [201] * 5 + [429]


def test_vendor_added_leads_take_the_new_fields_too():
    v = _vendor()
    made = client.post("/leads", json={"name": "Iyer Family", "event_type": "Wedding", "guest_count": 250}, headers=v["headers"])
    assert made.status_code == 201, made.text
    assert (made.json()["source"], made.json()["event_type"], made.json()["guest_count"]) == ("manual", "Wedding", 250)

    lead_id = made.json()["lead_id"]
    changed = client.patch(f"/leads/{lead_id}", json={"guest_count": 300}, headers=v["headers"])
    assert changed.json()["guest_count"] == 300
    bad = client.patch(f"/leads/{lead_id}", json={"guest_count": -4}, headers=v["headers"])
    assert bad.status_code == 422
