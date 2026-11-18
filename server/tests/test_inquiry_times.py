"""Start and end times on the inquiry form (0094, DECISIONS #58): optional,
both or neither, kept on the lead and shown on the card, the message and the
vendor's email."""

from tests.test_api import client
from tests.test_public_inquiries import (  # noqa: F401 (fixtures)
    _client,
    _isolate,
    _leads,
    _send,
    _thread_messages,
    _vendor,
    emails,
)


def test_times_are_kept_and_shown(emails):  # noqa: F811
    v, c = _vendor(), _client()
    resp = _send(v["vendor_id"], c, time_start="16:00", time_end="22:30")
    assert resp.status_code == 201, resp.text

    [lead] = _leads(v["vendor_id"])
    assert (lead.time_start, lead.time_end) == ("16:00", "22:30")

    [message] = _thread_messages(resp.json()["conversation_id"])
    assert "• Time: 4:00 PM – 10:30 PM" in message.content
    assert (message.meta["inquiry"]["time_start"], message.meta["inquiry"]["time_end"]) == ("16:00", "22:30")

    vendor_mail = next(m for m in (call.kwargs for call in emails.call_args_list) if m["to"] == v["email"])
    assert "4:00 PM – 10:30 PM" in vendor_mail["html"]

    listed = client.get("/leads", headers=v["headers"]).json()["items"]
    row = next(item for item in listed if item["lead_id"] == lead.lead_id)
    assert (row["time_start"], row["time_end"]) == ("16:00", "22:30")


def test_times_are_optional(emails):  # noqa: F811
    v, c = _vendor(), _client()
    resp = _send(v["vendor_id"], c)
    assert resp.status_code == 201, resp.text
    [lead] = _leads(v["vendor_id"])
    assert (lead.time_start, lead.time_end) == (None, None)
    [message] = _thread_messages(resp.json()["conversation_id"])
    assert "• Time:" not in message.content


def test_half_a_range_or_a_bad_time_is_refused(emails):  # noqa: F811
    v, c = _vendor(), _client()
    half = _send(v["vendor_id"], c, time_start="16:00")
    assert half.status_code == 422
    assert half.json()["detail"] == "Add both a start and an end time, or leave both out"
    assert _send(v["vendor_id"], c, time_start="4pm", time_end="22:00").status_code == 422
    assert _send(v["vendor_id"], c, time_start="24:00", time_end="22:00").status_code == 422
    assert _leads(v["vendor_id"]) == []


def test_a_second_inquiry_with_times_updates_the_lead(emails):  # noqa: F811
    v, c = _vendor(), _client()
    assert _send(v["vendor_id"], c).status_code == 201
    assert _send(v["vendor_id"], c, time_start="18:00", time_end="23:00").status_code == 201
    [lead] = _leads(v["vendor_id"])
    assert (lead.time_start, lead.time_end) == ("18:00", "23:00")
