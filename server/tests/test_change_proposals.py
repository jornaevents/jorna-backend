"""Change proposals on an unsigned contract (docs/DECISIONS.md #23): the
client suggests edits by their link; the vendor accepts, declines or
revises."""

from datetime import datetime, timedelta

import pytest

from app.db.models import Booking, ContractDraft, ContractProposal, ContractRevision
from tests.test_api import TestingSessionLocal, client
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _create_contract,
    _created,
    _isolate,
    _setup_vendor,
)

LAYOUT = [
    {"id": "scope", "type": "terms", "title": "Scope", "body": "DJ and MC for the reception."},
    {"id": "e", "type": "event"},
    {"id": "i", "type": "items"},
    {"id": "travel", "type": "terms", "title": "Travel", "body": "30 miles included."},
    {"id": "s", "type": "schedule"},
    {"id": "sig", "type": "signature"},
]


@pytest.fixture(autouse=True)
def _whole_proposals(monkeypatch):
    """These contracts use the whole-proposal flow, which field negotiation
    replaces by default; contracts created with the flag off still use it."""
    monkeypatch.setattr("app.config.FIELD_NEGOTIATION", False)


@pytest.fixture(autouse=True)
def _clean_proposals():
    yield
    db = TestingSessionLocal()
    if _created["bookings"]:
        for model in (ContractDraft, ContractProposal, ContractRevision):
            db.query(model).filter(model.booking_id.in_(_created["bookings"])).delete(synchronize_session=False)
        db.commit()
    db.close()


def _contract(v, **overrides) -> dict:
    """A builder-made contract: two lines, a deposit and a balance, two clauses."""
    body = dict(
        line_items=[
            {"id": "pkg", "kind": "package", "service_id": v["service_id"], "unit_price_cents": 140_000, "quantity": 1},
            {"id": "lights", "kind": "custom", "name": "Uplighting", "unit_price_cents": 10_000, "quantity": 2},
        ],
        payment_schedule=[
            {"id": "dep", "label": "Deposit", "amount_cents": 50_000, "due_type": "on_signing"},
            {"id": "bal", "label": "Final balance", "amount_cents": 110_000, "due_type": "before_event", "due_days": 14},
        ],
        document_layout=LAYOUT,
        amount_cents=None,
        deposit_percent=None,
    )
    body.update(overrides)
    c = _create_contract(v, **body)
    client.patch(f"/guest-bookings/{c['contract_token']}", json={"guest_email": "meera@example.com"})
    return c


def _changes(**extra) -> dict:
    """Three lights instead of two, the schedule to match, and a longer travel radius."""
    out = {
        "line_items": [
            {"id": "pkg", "kind": "package", "service_id": None, "unit_price_cents": 140_000, "quantity": 1},
            {"id": "lights", "kind": "custom", "name": "Uplighting", "unit_price_cents": 10_000, "quantity": 3},
        ],
        "payment_schedule": [
            {"id": "dep", "label": "Deposit", "amount_cents": 50_000, "due_type": "on_signing"},
            {"id": "bal", "label": "Final balance", "amount_cents": 120_000, "due_type": "before_event", "due_days": 14},
        ],
        "terms_clauses": [
            {"key": "scope", "title": "Scope", "body": "DJ and MC for the reception."},
            {"key": "travel", "title": "Travel", "body": "50 miles included."},
        ],
    }
    out.update(extra)
    return out


def _propose(c, v, revision=1, message="Could we add a light?", **changes):
    body = _changes(**changes)
    for item in body["line_items"]:
        if item["kind"] == "package":
            item["service_id"] = v["service_id"]
    return client.post(
        f"/guest-bookings/{c['contract_token']}/proposals",
        json={"base_revision": revision, "changes": body, "message": message},
    )


def _open_id(c) -> str:
    return client.get(f"/guest-bookings/{c['contract_token']}/proposals").json()["open_proposal"]["proposal_id"]


# ── Proposing ────────────────────────────────────────────────────────

def test_client_proposes_and_the_vendor_sees_it_needs_them():
    v = _setup_vendor()
    c = _contract(v)
    r = _propose(c, v)
    assert r.status_code == 201, r.text
    history = r.json()
    p = history["open_proposal"]
    assert p["status"] == "open" and p["base_revision"] == 1 and p["message"] == "Could we add a light?"
    assert p["proposed"]["amount_cents"] == 170_000
    assert p["proposed"]["terms_clauses"][1]["body"] == "50 miles included."
    # The version it was made against is kept for the comparison.
    assert [x["revision"] for x in history["revisions"]] == [1]
    assert history["revisions"][0]["terms"]["amount_cents"] == 160_000

    vendor_view = client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()
    assert vendor_view["proposal_status"] == "open"
    assert any(e["kind"] == "proposal_sent" for e in vendor_view["timeline"])
    item = next(
        i for i in client.get("/leads/pipeline", headers=v["headers"]).json()["items"]
        if i["booking_id"] == c["booking_id"]
    )
    assert (item["attention"], item["attention_reason"], item["stage"]) == ("needs_you", "changes_proposed", "negotiation")
    assert client.get(f"/guest-bookings/{c['contract_token']}").json()["proposal_status"] == "open"


def test_a_proposal_must_be_against_the_latest_version():
    v = _setup_vendor()
    c = _contract(v)
    client.patch(f"/contracts/{c['booking_id']}", json={"guest_count": 200}, headers=v["headers"])
    r = _propose(c, v, revision=1)
    assert r.status_code == 409
    assert _propose(c, v, revision=2).status_code == 201


@pytest.mark.parametrize("bad, why", [
    ({"payment_schedule": [{"label": "All", "amount_cents": 1, "due_type": "on_signing"}]}, "add up"),
    ({"terms_clauses": [{"key": "travel", "title": "Travel", "body": ""}]}, "title and some text"),
    ({"line_items": []}, "at least one package"),
    ({"guest_name": "Someone else"}, "Can't propose"),
])
def test_a_proposal_gets_the_same_checks_as_a_vendor_edit(bad, why):
    v = _setup_vendor()
    c = _contract(v)
    r = _propose(c, v, **bad)
    assert r.status_code == 400
    assert why in r.json()["detail"]


def test_a_proposal_has_to_change_something():
    v = _setup_vendor()
    c = _contract(v)
    r = client.post(
        f"/guest-bookings/{c['contract_token']}/proposals",
        json={"base_revision": 1, "changes": {"guest_count": None}},
    )
    assert r.status_code == 400
    assert "Change something" in r.json()["detail"]


def test_a_total_change_needs_the_schedule_to_follow():
    v = _setup_vendor()
    c = _contract(v)
    r = client.post(
        f"/guest-bookings/{c['contract_token']}/proposals",
        json={"base_revision": 1, "changes": {"discount_cents": 10_000}},
    )
    assert r.status_code == 400
    assert "payment schedule" in r.json()["detail"]


def test_no_proposals_once_signed_expired_or_voided():
    v = _setup_vendor()
    signed = _contract(v)
    client.post(f"/guest-bookings/{signed['contract_token']}/sign", json={"signer_name": "Meera Shah"})
    assert _propose(signed, v).status_code == 400

    expired = _contract(v, date_iso="2027-12-04")
    db = TestingSessionLocal()
    db.query(Booking).filter(Booking.booking_id == expired["booking_id"]).update(
        {"hold_expires_at": datetime.utcnow() - timedelta(hours=1)},
    )
    db.commit()
    db.close()
    assert _propose(expired, v).status_code == 410

    voided = _contract(v, date_iso="2027-12-11")
    client.post(f"/contracts/{voided['booking_id']}/void", headers=v["headers"])
    assert _propose(voided, v).status_code == 410


def test_a_new_proposal_replaces_the_open_one_and_can_be_withdrawn():
    v = _setup_vendor()
    c = _contract(v)
    first = _propose(c, v).json()["open_proposal"]["proposal_id"]
    second = _propose(c, v, message="Actually, 60 miles", terms_clauses=[
        {"key": "scope", "title": "Scope", "body": "DJ and MC for the reception."},
        {"key": "travel", "title": "Travel", "body": "60 miles included."},
    ]).json()
    assert second["open_proposal"]["proposal_id"] != first
    assert {p["proposal_id"]: p["status"] for p in second["proposals"]}[first] == "superseded"

    token = c["contract_token"]
    r = client.post(f"/guest-bookings/{token}/proposals/{second['open_proposal']['proposal_id']}/withdraw")
    assert r.status_code == 200
    assert r.json()["open_proposal"] is None
    assert client.post(f"/guest-bookings/{token}/proposals/{first}/withdraw").status_code == 400


def test_signing_withdraws_an_open_proposal():
    v = _setup_vendor()
    c = _contract(v)
    _propose(c, v)
    r = client.post(f"/guest-bookings/{c['contract_token']}/sign", json={"signer_name": "Meera Shah", "revision": 1})
    assert r.status_code == 200
    statuses = [p["status"] for p in client.get(f"/guest-bookings/{c['contract_token']}/proposals").json()["proposals"]]
    assert statuses == ["withdrawn"]
    # What was signed is the vendor's version, not the proposal.
    assert r.json()["amount_cents"] == 160_000


# ── Answering ────────────────────────────────────────────────────────

def test_accept_makes_the_proposal_the_next_version_and_resends_it():
    v = _setup_vendor()
    c = _contract(v)
    _propose(c, v)
    pid = _open_id(c)
    before = client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()

    r = client.post(
        f"/contracts/{c['booking_id']}/proposals/{pid}/accept", json={"note": "Happy to."}, headers=v["headers"],
    )
    assert r.status_code == 200, r.text
    after = r.json()
    assert after["revision"] == 2
    assert after["amount_cents"] == 170_000
    assert [i["quantity"] for i in after["line_items"]] == [1, 3]
    assert [i["amount_cents"] for i in after["payment_schedule"]] == [50_000, 120_000]
    assert after["terms_clauses"][1]["body"] == "50 miles included."
    assert after["proposal_status"] == "accepted"
    assert after["hold_expires_at"] >= before["hold_expires_at"]
    kinds = [e["kind"] for e in after["timeline"]]
    assert "proposal_accepted" in kinds and "resent" in kinds

    history = client.get(f"/contracts/{c['booking_id']}/proposals", headers=v["headers"]).json()
    p = history["proposals"][0]
    assert (p["status"], p["result_revision"], p["response_note"]) == ("accepted", 2, "Happy to.")
    assert [x["revision"] for x in history["revisions"]] == [2, 1]

    item = next(
        i for i in client.get("/leads/pipeline", headers=v["headers"]).json()["items"]
        if i["booking_id"] == c["booking_id"]
    )
    assert (item["attention"], item["attention_reason"]) == ("waiting", "revised")

    # The client signs the new version.
    signed = client.post(f"/guest-bookings/{c['contract_token']}/sign", json={"signer_name": "Meera Shah", "revision": 2})
    assert signed.status_code == 200
    assert signed.json()["amount_cents"] == 170_000


def test_accepting_a_new_clause_keeps_the_editor_layout_in_step():
    v = _setup_vendor()
    c = _contract(v)
    _propose(c, v, terms_clauses=[
        {"key": "travel", "title": "Travel", "body": "30 miles included."},
        {"title": "Song list", "body": "We'll send a do-not-play list."},
    ])
    after = client.post(
        f"/contracts/{c['booking_id']}/proposals/{_open_id(c)}/accept", headers=v["headers"],
    ).json()
    layout = after["document_layout"]
    terms = [b["id"] for b in layout if b["type"] == "terms"]
    # Scope was removed; the new clause sits just before the signature.
    assert terms[0] == "travel" and "scope" not in terms
    assert [b["type"] for b in layout][-2:] == ["terms", "signature"]
    assert [x["key"] for x in after["terms_clauses"]] == terms


def test_accepting_a_date_that_is_taken_is_refused_and_leaves_the_proposal_open():
    v = _setup_vendor()
    _contract(v, date_iso="2027-12-18")
    c = _contract(v)
    _propose(c, v, date_iso="2027-12-18")
    pid = _open_id(c)
    r = client.post(f"/contracts/{c['booking_id']}/proposals/{pid}/accept", headers=v["headers"])
    assert r.status_code == 409
    assert client.get(f"/guest-bookings/{c['contract_token']}/proposals").json()["open_proposal"]["proposal_id"] == pid
    assert client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()["revision"] == 1


def test_decline_leaves_the_version_standing():
    v = _setup_vendor()
    c = _contract(v)
    _propose(c, v)
    pid = _open_id(c)
    r = client.post(
        f"/contracts/{c['booking_id']}/proposals/{pid}/decline",
        json={"note": "Travel is fixed, sorry."}, headers=v["headers"],
    )
    assert r.status_code == 200
    assert (r.json()["revision"], r.json()["amount_cents"]) == (1, 160_000)
    p = client.get(f"/guest-bookings/{c['contract_token']}/proposals").json()["proposals"][0]
    assert (p["status"], p["response_note"]) == ("declined", "Travel is fixed, sorry.")
    # Answered once is answered.
    again = client.post(f"/contracts/{c['booking_id']}/proposals/{pid}/accept", headers=v["headers"])
    assert again.status_code == 409


def test_revise_answers_with_the_vendors_own_version():
    v = _setup_vendor()
    c = _contract(v)
    _propose(c, v)
    pid = _open_id(c)
    r = client.patch(
        f"/contracts/{c['booking_id']}",
        json={"proposal_id": pid, "proposal_note": "Meet you at 40?", "terms_clauses": [
            {"key": "scope", "title": "Scope", "body": "DJ and MC for the reception."},
            {"key": "travel", "title": "Travel", "body": "40 miles included."},
        ]},
        headers=v["headers"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2
    assert r.json()["proposal_status"] == "revised"
    p = client.get(f"/guest-bookings/{c['contract_token']}/proposals").json()["proposals"][0]
    assert (p["status"], p["result_revision"], p["response_note"]) == ("revised", 2, "Meet you at 40?")
    timeline = client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()["timeline"]
    kinds = [e["kind"] for e in timeline]
    assert "proposal_revised" in kinds and "resent" in kinds


def test_an_edit_without_answering_supersedes_the_proposal():
    v = _setup_vendor()
    c = _contract(v)
    _propose(c, v)
    pid = _open_id(c)
    client.patch(f"/contracts/{c['booking_id']}", json={"guest_count": 150}, headers=v["headers"])
    p = client.get(f"/guest-bookings/{c['contract_token']}/proposals").json()["proposals"][0]
    assert p["status"] == "superseded"
    assert client.post(f"/contracts/{c['booking_id']}/proposals/{pid}/accept", headers=v["headers"]).status_code == 409


def test_only_the_contracts_vendor_can_see_or_answer():
    v = _setup_vendor()
    other = _setup_vendor()
    c = _contract(v)
    _propose(c, v)
    pid = _open_id(c)
    assert client.get(f"/contracts/{c['booking_id']}/proposals", headers=other["headers"]).status_code == 403
    for action in ("accept", "decline"):
        r = client.post(f"/contracts/{c['booking_id']}/proposals/{pid}/{action}", headers=other["headers"])
        assert r.status_code == 403
    assert client.patch(
        f"/contracts/{c['booking_id']}", json={"guest_count": 10, "proposal_id": pid}, headers=other["headers"],
    ).status_code == 403


# ── Drafts (0069, DECISIONS #24) ─────────────────────────────────────

def _guest_history(c) -> dict:
    return client.get(f"/guest-bookings/{c['contract_token']}/proposals").json()


def _vendor_history(c, v) -> dict:
    return client.get(f"/contracts/{c['booking_id']}/proposals", headers=v["headers"]).json()


def test_the_client_saves_a_draft_and_sending_spends_it():
    v = _setup_vendor()
    c = _contract(v)
    half_done = {"guest_count": 0, "location": ""}  # not valid to send, fine to keep
    r = client.put(
        f"/guest-bookings/{c['contract_token']}/proposals/draft",
        json={"base_revision": 1, "changes": half_done, "message": "Still thinking"},
    )
    assert r.status_code == 200, r.text
    assert (r.json()["changes"], r.json()["message"], r.json()["stale"]) == (half_done, "Still thinking", False)
    assert _guest_history(c)["draft"]["changes"] == half_done
    # Each side sees only its own.
    assert _vendor_history(c, v)["draft"] is None

    # Saving again replaces it.
    client.put(
        f"/guest-bookings/{c['contract_token']}/proposals/draft",
        json={"base_revision": 1, "changes": {"guest_count": 120}},
    )
    assert _guest_history(c)["draft"]["changes"] == {"guest_count": 120}

    assert _propose(c, v).status_code == 201
    assert _guest_history(c)["draft"] is None


def test_a_client_draft_can_be_discarded_and_goes_stale_after_a_vendor_edit():
    v = _setup_vendor()
    c = _contract(v)
    client.put(
        f"/guest-bookings/{c['contract_token']}/proposals/draft",
        json={"base_revision": 1, "changes": {"guest_count": 120}},
    )
    client.patch(f"/contracts/{c['booking_id']}", json={"guest_count": 150}, headers=v["headers"])
    assert _guest_history(c)["draft"]["stale"] is True
    assert client.delete(f"/guest-bookings/{c['contract_token']}/proposals/draft").status_code == 204
    assert _guest_history(c)["draft"] is None


def test_the_vendor_saves_a_revision_draft_and_answering_spends_it():
    v = _setup_vendor()
    c = _contract(v)
    _propose(c, v)
    pid = _open_id(c)
    url = f"/contracts/{c['booking_id']}/proposals/draft"
    r = client.put(
        url, json={"base_revision": 1, "changes": {"overtime_rate_cents": 30_000}, "proposal_id": pid},
        headers=v["headers"],
    )
    assert r.status_code == 200, r.text
    draft = _vendor_history(c, v)["draft"]
    assert (draft["proposal_id"], draft["changes"]) == (pid, {"overtime_rate_cents": 30_000})
    assert _guest_history(c)["draft"] is None

    client.post(f"/contracts/{c['booking_id']}/proposals/{pid}/decline", headers=v["headers"])
    assert _vendor_history(c, v)["draft"] is None


@pytest.mark.parametrize("answer", ["accept", "revise", "edit"])
def test_every_vendor_send_spends_their_draft(answer):
    v = _setup_vendor()
    c = _contract(v)
    _propose(c, v)
    pid = _open_id(c)
    client.put(
        f"/contracts/{c['booking_id']}/proposals/draft",
        json={"base_revision": 1, "changes": {"guest_count": 90}, "proposal_id": pid}, headers=v["headers"],
    )
    if answer == "accept":
        r = client.post(f"/contracts/{c['booking_id']}/proposals/{pid}/accept", headers=v["headers"])
    else:
        body = {"guest_count": 90, **({"proposal_id": pid} if answer == "revise" else {})}
        r = client.patch(f"/contracts/{c['booking_id']}", json=body, headers=v["headers"])
    assert r.status_code == 200, r.text
    assert _vendor_history(c, v)["draft"] is None


def test_drafts_are_checked_for_shape_and_owner():
    v = _setup_vendor()
    other = _setup_vendor()
    c = _contract(v)
    token_url = f"/guest-bookings/{c['contract_token']}/proposals/draft"
    assert client.put(token_url, json={"base_revision": 1, "changes": {"vendor_id": "x"}}).status_code == 400
    big = {"terms_clauses": [{"key": "k", "title": "t", "body": "x" * 200_000}]}
    assert client.put(token_url, json={"base_revision": 1, "changes": big}).status_code == 413
    url = f"/contracts/{c['booking_id']}/proposals/draft"
    assert client.put(url, json={"base_revision": 1, "changes": {}}, headers=other["headers"]).status_code in (403, 404)
    assert client.delete(url, headers=other["headers"]).status_code in (403, 404)


def test_no_drafts_once_signed():
    v = _setup_vendor()
    c = _contract(v)
    db = TestingSessionLocal()
    b = db.query(Booking).filter(Booking.booking_id == c["booking_id"]).first()
    b.signed_at = datetime.utcnow()
    db.commit()
    db.close()
    r = client.put(
        f"/guest-bookings/{c['contract_token']}/proposals/draft", json={"base_revision": 1, "changes": {}},
    )
    assert r.status_code == 400

