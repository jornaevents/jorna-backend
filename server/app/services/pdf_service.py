"""Contracts and attached documents as PDF files (docs/DECISIONS.md #22).

A signed one is drawn from its frozen snapshot — the text the client signed,
not whatever the booking says now — with the signature and the snapshot's
SHA-256, so the file can be checked against the record. An unsigned one is
the current version, marked as not signed.

fpdf2 rather than an HTML renderer: pure Python, nothing to install on the
Railway image. DejaVu Sans (app/assets/fonts, its own licence alongside) is
bundled because the built-in PDF fonts are Latin-1 only, which can't print
a curly quote, an em dash or ₹, let alone most names. It doesn't shape
Indic scripts; a name typed in Devanagari prints its characters unjoined.
"""

import re
from datetime import date, datetime
from pathlib import Path

from fpdf import FPDF

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

INK = (40, 24, 28)
SOFT = (95, 80, 84)
FAINT = (140, 128, 130)
GOLD = (150, 110, 30)
LINE = (220, 210, 205)

STRUCTURED = ("parties", "event", "items", "schedule", "signature")
KIND_LABEL = {"addendum": "Service addendum", "cancellation": "Cancellation agreement"}


# ── Wording ──────────────────────────────────────────────────────────

def money(cents: int | None) -> str:
    return f"${(cents or 0) / 100:,.2f}"


def _parse_day(iso: str | None) -> date | None:
    try:
        return date.fromisoformat((iso or "")[:10])
    except ValueError:
        return None


def pretty_day(iso: str | None) -> str:
    d = _parse_day(iso)
    return f"{d:%A}, {d:%B} {d.day}, {d.year}" if d else (iso or "To be confirmed")


def short_day(iso: str | None) -> str:
    d = _parse_day(iso)
    return f"{d:%B} {d.day}, {d.year}" if d else (iso or "")


def pretty_time(hhmm: str | None) -> str:
    m = re.match(r"^(\d{1,2}):(\d{2})", hhmm or "")
    if not m:
        return hhmm or ""
    h = int(m.group(1))
    return f"{(h + 11) % 12 + 1}:{m.group(2)} {'PM' if h >= 12 else 'AM'}"


def signed_on(iso: str | None) -> str:
    try:
        at = datetime.fromisoformat(iso or "")
    except ValueError:
        return iso or ""
    return f"{at:%B} {at.day}, {at.year} at {pretty_time(at.strftime('%H:%M'))} UTC"


def describe_due(i: dict, event_iso: str | None) -> str:
    kind = i.get("due_type") or "on_signing"
    if kind == "on_signing":
        return "Due when signed"
    if kind == "date":
        return f"Due {short_day(i.get('due_date'))}" if i.get("due_date") else "Due on a date"
    days = i.get("due_days") or 0
    rule = "on the event day" if days == 0 else f"{days} day{'' if days == 1 else 's'} before the event"
    return f"Due {rule}"


def filename(title: str, day: str | None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "agreement"
    return f"{slug}{'-' + day[:10] if day else ''}.pdf"


# ── Drawing ──────────────────────────────────────────────────────────

class _Pdf(FPDF):
    def __init__(self, footer: str):
        super().__init__(format="Letter")
        self.footer_text = footer
        self.add_font("DejaVu", "", str(FONT_DIR / "DejaVuSans.ttf"))
        self.add_font("DejaVu", "B", str(FONT_DIR / "DejaVuSans-Bold.ttf"))
        self.set_margins(22, 20, 22)
        self.set_auto_page_break(True, 22)
        self.add_page()

    def footer(self):
        self.set_y(-14)
        self.font(7.5, color=FAINT)
        self.cell(0, 5, f"{self.footer_text}  ·  Page {self.page_no()} of {{nb}}", align="C")

    def font(self, size: float, bold: bool = False, color=INK):
        self.set_font("DejaVu", "B" if bold else "", size)
        self.set_text_color(*color)

    def text_block(self, text: str, size: float = 10, color=SOFT, bold: bool = False, gap: float = 1.5):
        self.font(size, bold, color)
        for para in re.split(r"\n\s*\n", text.strip()):
            self.multi_cell(0, size * 0.5, para.strip(), new_x="LMARGIN", new_y="NEXT")
            self.ln(gap)

    def eyebrow(self, text: str):
        self.ln(3)
        self.font(7.5, True, FAINT)
        self.cell(0, 5, text.upper(), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def rule(self):
        self.ln(2)
        self.set_draw_color(*LINE)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(3)

    def row(self, left: str, right: str, bold: bool = False, color=INK):
        width = self.w - self.l_margin - self.r_margin
        self.font(10, bold, color)
        y = self.get_y()
        self.multi_cell(width - 40, 5.5, left, new_x="RIGHT", new_y="TOP")
        self.set_xy(self.w - self.r_margin - 40, y)
        self.cell(40, 5.5, right, align="R")
        self.set_y(max(self.get_y() + 5.5, y + 5.5))


def _header(pdf: _Pdf, brand: str, title: str, subtitle: str, unsigned_note: str | None):
    pdf.font(8, True, GOLD)
    pdf.cell(0, 5, brand.upper(), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.font(20, True)
    pdf.multi_cell(0, 9, title, new_x="LMARGIN", new_y="NEXT")
    pdf.font(9.5, color=FAINT)
    pdf.multi_cell(0, 5, subtitle, new_x="LMARGIN", new_y="NEXT")
    if unsigned_note:
        pdf.ln(3)
        pdf.set_fill_color(248, 236, 214)
        pdf.font(9, True, (120, 80, 10))
        pdf.multi_cell(0, 6, f"  {unsigned_note}", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.rule()


def _signatures(pdf: _Pdf, client: str, vendor: str, signer: str | None, at: str | None, sha: str | None):
    # Kept together: a signature on a page of its own, away from what it
    # signs, reads as if it could belong to anything.
    if pdf.get_y() + 50 > pdf.h - pdf.b_margin:
        pdf.add_page()
    pdf.eyebrow("Signatures")
    width = (pdf.w - pdf.l_margin - pdf.r_margin - 10) / 2
    y = pdf.get_y() + 8
    pdf.font(13, True, SOFT)
    pdf.set_xy(pdf.l_margin, y - 7)
    pdf.cell(width, 7, signer or "")
    pdf.set_draw_color(*INK)
    pdf.line(pdf.l_margin, y, pdf.l_margin + width, y)
    pdf.line(pdf.l_margin + width + 10, y, pdf.w - pdf.r_margin, y)
    pdf.set_xy(pdf.l_margin, y + 1)
    pdf.font(9.5)
    pdf.cell(width, 5, client)
    pdf.set_x(pdf.l_margin + width + 10)
    pdf.cell(width, 5, vendor, new_x="LMARGIN", new_y="NEXT")
    pdf.font(8, color=FAINT)
    pdf.cell(width, 4.5, f"Signed electronically {signed_on(at)}" if signer else "Not signed yet")
    pdf.set_x(pdf.l_margin + width + 10)
    pdf.cell(width, 4.5, "Provider — issued this agreement", new_x="LMARGIN", new_y="NEXT")
    if sha:
        pdf.ln(6)
        pdf.font(7.5, color=FAINT)
        pdf.multi_cell(
            0, 4,
            f"Fingerprint of the signed record (SHA-256): {sha}. Any change to the signed text "
            "changes this fingerprint.",
            new_x="LMARGIN", new_y="NEXT",
        )


# ── Signing certificate (docs/DECISIONS.md #27) ──────────────────────

CONTRACT_EVENTS = ("sent", "resent", "emailed", "viewed", "code_sent", "signed", "copy_sent")
DOCUMENT_EVENTS = ("document_sent", "document_emailed", "document_viewed", "code_sent", "document_signed", "copy_sent")


def _event_line(e: dict) -> str | None:
    d = e.get("detail") or {}
    ip = f" from {d['ip']}" if d.get("ip") else ""
    kind = e.get("kind")
    if kind in ("sent", "document_sent"):
        return "Sent by the provider"
    if kind == "resent":
        return "Sent again by the provider"
    if kind in ("emailed", "document_emailed"):
        return "Link emailed to the client"
    if kind in ("viewed", "document_viewed"):
        return f"Opened by the client{ip}"
    if kind == "code_sent":
        return f"Signing code emailed to {d.get('email') or 'the client'}"
    if kind in ("signed", "document_signed"):
        return f"Signed by {d.get('signer_name') or 'the client'}{ip}"
    if kind == "copy_sent":
        return f"Signed copy emailed to the {d.get('to') or 'client'}"
    return None


def _certificate(pdf: _Pdf, title: str, signer: str, signed_at: str | None, sha: str | None,
                 evidence: dict, events: list[dict]):
    """The last page: how this agreement was signed, for anyone checking
    it later. Everything above the event log is from the signed snapshot,
    so the fingerprint covers it."""
    pdf.add_page()
    pdf.font(8, True, GOLD)
    pdf.cell(0, 5, "SIGNING CERTIFICATE", new_x="LMARGIN", new_y="NEXT")
    pdf.font(14, True)
    pdf.multi_cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
    pdf.rule()

    def field(label: str, value: str | None):
        pdf.font(8, True, FAINT)
        pdf.cell(42, 5.5, label)
        pdf.font(9.5)
        pdf.multi_cell(0, 5.5, value or "Not recorded", align="L", new_x="LMARGIN", new_y="NEXT")

    consent = evidence.get("consent") or {}
    field("Signed by", f"{signer} (typed full legal name)")
    field("Signed at", signed_on(signed_at))
    field("Email", evidence.get("email"))
    field(
        "Email confirmed",
        f"Yes, by a one-time code sent there, entered {signed_on(evidence['email_verified_at'])}"
        if evidence.get("email_verified_at") else "No",
    )
    field("IP address", evidence.get("ip"))
    field("Device", evidence.get("user_agent"))
    field("E-records consent", f"Given (version {consent['version']})" if consent else "Not given")
    if sha:
        field("Fingerprint", f"SHA-256 {sha}")
    if consent.get("text"):
        pdf.eyebrow("Consent the signer agreed to")
        pdf.text_block(f"“{consent['text']}”", size=9)
    lines = [(e.get("at"), _event_line(e)) for e in events]
    lines = [(at, text) for at, text in lines if text]
    if lines:
        pdf.eyebrow("History")
        for at, text in lines:
            pdf.font(8.5, color=FAINT)
            pdf.cell(62, 5, signed_on(at))
            pdf.font(9)
            pdf.multi_cell(0, 5, text, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.font(7.5, color=FAINT)
    pdf.multi_cell(
        0, 4,
        "Jorna provided the software used to send and sign this agreement and is not a party to it. "
        "Times are UTC. The fingerprint covers the agreement's text and every field above the history.",
        new_x="LMARGIN", new_y="NEXT",
    )


def _ordered_blocks(layout: list | None, clauses: list) -> list[tuple[str, dict | None]]:
    """The editor's order (backend 0067), or the classic one for a contract
    written before it: every clause appears once, each structured block
    once, the signature last."""
    by_key = {c.get("key"): c for c in clauses}
    out: list[tuple[str, dict | None]] = []
    seen: set[str] = set()
    for b in layout or [{"type": t} for t in ("parties", "event", "items", "schedule")]:
        kind = b.get("type")
        if kind == "terms":
            c = by_key.get(b.get("id"))
            if c and c.get("key") not in seen:
                seen.add(c["key"])
                out.append(("terms", c))
        elif kind in STRUCTURED and kind not in seen and kind != "signature":
            seen.add(kind)
            out.append((kind, None))
    for kind in STRUCTURED[:-1]:
        if kind not in seen:
            out.append((kind, None))
    out.extend(("terms", c) for c in clauses if c.get("key") not in seen)
    return out


# ── A contract ───────────────────────────────────────────────────────

def render_contract(a: dict, *, vendor_name: str, service_name: str | None,
                    signer: str | None, signed_at: str | None, sha: str | None,
                    events: list[dict] | None = None) -> tuple[bytes, str]:
    """`a` is contract_document.agreement()'s shape — a signed snapshot, or
    the live booking's for an unsigned one."""
    client = a.get("client") or {}
    lines = a.get("line_items") or []
    title = a.get("document_title") or (
        f"{lines[0]['name'] if lines else service_name or 'Services'} agreement"
    )
    when = pretty_day(a.get("date_iso"))
    if a.get("date_end") and a.get("date_end") != a.get("date_iso"):
        when += f" – {pretty_day(a['date_end'])}"
    if a.get("time_start") and a.get("time_end"):
        when += f" · {pretty_time(a['time_start'])} – {pretty_time(a['time_end'])}"

    pdf = _Pdf(f"{title} — {vendor_name}")
    _header(
        pdf, vendor_name, title, when,
        None if signer else "Not signed — this is the current version, not a signed agreement.",
    )

    for kind, clause in _ordered_blocks(a.get("document_layout"), a.get("terms_clauses") or []):
        if kind == "parties":
            pdf.eyebrow("Parties")
            pdf.row(f"Provider: {vendor_name}", "")
            who = " · ".join(x for x in (client.get("name"), client.get("email"), client.get("phone")) if x)
            pdf.row(f"Client: {who or 'To be filled in by the client'}", "")
        elif kind == "event":
            pdf.eyebrow("Event details")
            pdf.text_block(when, color=INK, gap=0.5)
            loc = a.get("location")
            pdf.text_block(f"Venue: {loc if loc and loc != 'TBD' else 'To be confirmed'}", gap=0.5)
            if a.get("guest_count"):
                pdf.text_block(f"Guests: {a['guest_count']}", gap=0.5)
        elif kind == "items":
            pdf.eyebrow("What's included")
            for item in lines:
                qty = item.get("quantity") or 1
                label = item.get("name") or "Item"
                if qty != 1:
                    label += f" × {qty:g} at {money(item.get('unit_price_cents'))}"
                pdf.row(label, money(item.get("total_cents")))
            subtotal = sum(i.get("total_cents") or 0 for i in lines) if lines else a.get("amount_cents")
            if a.get("discount_cents"):
                pdf.row("Subtotal", money(subtotal), color=SOFT)
                pdf.row("Discount", f"−{money(a['discount_cents'])}", color=SOFT)
            pdf.row("Total", money(a.get("amount_cents")), bold=True)
        elif kind == "schedule":
            pdf.eyebrow("Payment schedule")
            schedule = a.get("payment_schedule") or []
            if schedule:
                for i in schedule:
                    pdf.row(f"{i.get('label') or 'Payment'} — {describe_due(i, a.get('date_iso'))}", money(i.get("amount_cents")))
            elif a.get("deposit_amount_cents") is not None:
                pdf.row(f"Deposit ({a.get('deposit_percent')}%) — due to secure the date", money(a["deposit_amount_cents"]))
                pdf.row("Balance", money((a.get("amount_cents") or 0) - a["deposit_amount_cents"]))
            else:
                pdf.row("Payment in full", money(a.get("amount_cents")))
            pdf.ln(1)
            pdf.font(8, color=FAINT)
            pdf.multi_cell(0, 4, "Paid directly to the provider. Jorna doesn't handle the money.", new_x="LMARGIN", new_y="NEXT")
        elif kind == "terms" and clause:
            pdf.ln(2)
            pdf.font(11.5, True)
            pdf.multi_cell(0, 6, clause.get("title") or "", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(0.5)
            pdf.text_block(clause.get("body") or "")

    policies = []
    if a.get("cancellation_window_hours") is not None:
        policies.append(f"Cancellation window: {round(a['cancellation_window_hours'] / 24)} days before the event.")
    if a.get("overtime_rate_cents") is not None:
        policies.append(f"Overtime: {money(a['overtime_rate_cents'])} per hour, agreed in advance.")
    legacy = a.get("contract_terms") or {}
    for key, label in (("equipment_power", "Equipment & power"), ("travel", "Travel")):
        if legacy.get(key):
            policies.append(f"{label}: {legacy[key]}")
    if policies:
        pdf.eyebrow("Policies")
        for p in policies:
            pdf.text_block(p, gap=0.5)

    pdf.rule()
    _signatures(pdf, client.get("name") or "Client", vendor_name, signer, signed_at, sha)
    if signer and a.get("evidence"):
        _certificate(pdf, title, signer, signed_at, sha, a["evidence"], events or [])
    return bytes(pdf.output()), filename(title, a.get("date_iso"))


# ── An addendum or cancellation agreement ────────────────────────────

def render_document(d: dict, *, vendor_name: str, client_name: str | None, agreement_title: str | None,
                    signer: str | None, signed_at: str | None, sha: str | None,
                    events: list[dict] | None = None) -> tuple[bytes, str]:
    """`d` has kind, title, sections and date_iso — the signed snapshot, or
    the live document."""
    kind = KIND_LABEL.get(d.get("kind"), "Document")
    title = d.get("title") or kind
    attached = f"{kind} to “{agreement_title}”" if agreement_title else kind
    subtitle = f"{attached} between {vendor_name} and {client_name or 'the client'}, for {pretty_day(d.get('date_iso'))}."

    pdf = _Pdf(f"{title} — {vendor_name}")
    _header(pdf, vendor_name, title, subtitle, None if signer else "Not signed yet.")
    for s in d.get("sections") or []:
        pdf.ln(1)
        pdf.font(11.5, True)
        pdf.multi_cell(0, 6, s.get("title") or "", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(0.5)
        pdf.text_block(s.get("body") or "")
    pdf.ln(2)
    pdf.font(8.5, color=FAINT)
    pdf.multi_cell(
        0, 4.5,
        "This document is part of the written agreement for the booking above. Signing it doesn't by "
        "itself change the booking's price, date or payments.",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.rule()
    _signatures(pdf, client_name or "Client", vendor_name, signer, signed_at, sha)
    if signer and d.get("evidence"):
        _certificate(pdf, title, signer, signed_at, sha, d["evidence"], events or [])
    return bytes(pdf.output()), filename(title, d.get("date_iso"))


# ── From the database ────────────────────────────────────────────────
# Who may download is the caller's business (contract_service,
# guest_booking_service, document_service); these only draw.

def response(rendered: tuple[bytes, str]):
    """A download, not a page: the browser saves it under its own name."""
    from fastapi import Response

    content, name = rendered
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )


def _vendor_name(booking, db) -> str:
    from app.db.models import User, Vendor

    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    user = db.query(User).filter(User.user_id == vendor.user_id).first() if vendor else None
    return (f"{user.f_name or ''} {user.l_name or ''}".strip() if user else "") or "Your vendor"


def _service_name(booking, db) -> str | None:
    from app.db.models import Service

    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    return service.name if service else None


def contract_pdf(booking, db) -> tuple[bytes, str]:
    from app.services import contract_document as doc

    snap = booking.signed_snapshot
    return render_contract(
        snap or doc.agreement(booking),
        vendor_name=_vendor_name(booking, db),
        service_name=_service_name(booking, db),
        signer=snap.get("signer_name") if snap else None,
        signed_at=snap.get("signed_at") if snap else None,
        sha=booking.signed_snapshot_sha256 if snap else None,
        events=[
            e for e in doc.timeline(booking, db, None)
            if e["kind"] in CONTRACT_EVENTS and not (e.get("detail") or {}).get("document_id")
        ] if snap else None,
    )


def document_pdf(d, booking, db, client_name: str | None) -> tuple[bytes, str]:
    from app.services import contract_document as doc

    snap = d.signed_snapshot
    live = {"kind": d.kind, "title": d.title, "sections": d.sections, "date_iso": booking.date_iso}
    agreement_title = booking.document_title or (
        f"{_service_name(booking, db)} agreement" if booking.service_id else None
    )
    return render_document(
        snap or live,
        vendor_name=_vendor_name(booking, db),
        client_name=client_name,
        agreement_title=agreement_title,
        signer=snap.get("signer_name") if snap else None,
        signed_at=snap.get("signed_at") if snap else None,
        sha=d.signed_snapshot_sha256 if snap else None,
        events=[
            e for e in doc.timeline(booking, db, None)
            if e["kind"] in DOCUMENT_EVENTS and (e.get("detail") or {}).get("document_id") == d.document_id
        ] if snap else None,
    )
