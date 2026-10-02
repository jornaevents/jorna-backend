"""SQLAlchemy table definitions for User, Vendor, Service, Booking, Tag."""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, Float, Text, ForeignKey, JSON, Table, Boolean, DateTime, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import relationship

from .database import Base


def uuid_str():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    user_id = Column(String(36), primary_key=True, default=uuid_str)
    username = Column(String(255), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    phone = Column(String(50), nullable=True)
    # NULL = no password has ever been set, i.e. a Google-only account. Readers
    # must guard on it (see login_user / change_password) rather than assume a
    # hash is present.
    password = Column(String(255), nullable=True)
    f_name = Column(String(255), nullable=False)
    l_name = Column(String(255), nullable=False)
    age = Column(Integer, nullable=True)
    location = Column(String(100), nullable=True)
    gender = Column(String(50), nullable=True)
    language = Column(String(50), nullable=True)
    pfp_url = Column(String(512), nullable=True)

    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    city = Column(String(100), nullable=True)
    state = Column(String(50), nullable=True)

    # Push device tokens live in the push_tokens table (one user → many devices:
    # a phone plus one or more browsers). See PushToken.

    # Supabase Auth user id (UUID) when this Jorna account is linked to Google sign-in
    supabase_user_id = Column(String(36), unique=True, nullable=True)

    # Incremented on logout or password change to invalidate all previously issued tokens
    token_version = Column(Integer, nullable=False, default=0)

    is_admin = Column(Boolean, nullable=False, default=False)

    # The card kept for this client, and the Stripe Customer it belongs to.
    # Collected once when a plan is sent and charged when a vendor accepts —
    # money moves at the same moment it always did, but nobody has to come back
    # for it. Null until they send their first plan.
    stripe_customer_id = Column(String(255), nullable=True)
    stripe_payment_method_id = Column(String(255), nullable=True)
    # Shown back to the client so they know which card is on file. Stripe holds
    # the card itself; these two are all we keep.
    card_brand = Column(String(40), nullable=True)
    card_last4 = Column(String(4), nullable=True)

    open_to_price_negotiation = Column(Boolean, nullable=False, default=False)
    flexible_on_location = Column(Boolean, nullable=False, default=False)

    # Where the message-digest sweep left off — a message is only ever in one
    # digest, the next one after it arrives, so the sweep needs to know per
    # user what "since last time" means. Null until their first digest.
    last_message_digest_at = Column(DateTime, nullable=True)


class PushToken(Base):
    """A device's FCM registration token for one user.

    One user has many — a phone and one or more browsers — so notifications fan
    out to every registered device. FCM tokens are the same string shape for
    native and web (FCM for Web), so a single send path (utils.notifications)
    delivers to all. Replaces the old single users.fcm_token column.
    """
    __tablename__ = "push_tokens"

    id = Column(String(36), primary_key=True, default=uuid_str)
    # A device belongs to one user at a time; a token is globally unique, so
    # re-registering a device (e.g. after a re-login) reassigns it.
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    token = Column(String(512), unique=True, nullable=False)
    platform = Column(String(20), nullable=False, default="ios")  # ios | web | android
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=False, default=datetime.utcnow)


# Many-to-many join table: one vendor has many tags, one tag belongs to many vendors.
vendor_tags = Table(
    "vendor_tags",
    Base.metadata,
    Column("vendor_id", String(36), ForeignKey("vendors.vendor_id"), primary_key=True),
    Column("tag_id",    String(36), ForeignKey("tags.tag_id"),    primary_key=True),
)


class Tag(Base):
    __tablename__ = "tags"

    tag_id = Column(String(36), primary_key=True, default=uuid_str)
    # Normalized (lowercase, stripped) tag name — unique across the whole table.
    name = Column(String(100), unique=True, nullable=False, index=True)


class Vendor(Base):
    __tablename__ = "vendors"

    vendor_id = Column(String(36), primary_key=True, default=uuid_str)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    bio = Column(Text, nullable=False)
    category = Column(String(50), nullable=False, default="other")
    subcategory = Column(String(100), nullable=True)
    # Every category(+subcategory) a vendor sells under, not just one — the
    # frontend has offered a multi-select here for a while, but this is what
    # actually persists it. category/subcategory above still mirror the first
    # entry, since search and the older list endpoints filter on those, not
    # this. A list of {"category": ..., "subcategory": ...} dicts, or null for
    # a vendor created before this column existed.
    specializations = Column(JSON, nullable=True)
    rating = Column(Float, nullable=False)
    num_events = Column(Integer, nullable=False)

    travel_radius_miles = Column(Integer, default=30)
    open_to_long_distance = Column(Boolean, nullable=False, default=False)
    open_to_price_negotiation = Column(Boolean, nullable=False, default=False)
    open_to_location_negotiation = Column(Boolean, nullable=False, default=False)
    google_access_token = Column(String(512), nullable=True)
    google_refresh_token = Column(String(512), nullable=True)
    calendar_id = Column(String(255), nullable=True)
    # Space-separated scope string Google actually granted on the OAuth
    # callback — not assumed from what was requested. Google doesn't
    # silently widen a standing grant when the app starts asking for more,
    # so a vendor who connected before a scope existed has this missing it;
    # every write path checks for "calendar.events" here before attempting
    # anything; a connect since then reads this as the source of truth.
    google_granted_scopes = Column(Text, nullable=True)
    # A cached copy of this vendor's Google busy blocks (list of {start, end}
    # ISO strings), covering roughly the next several months — kept fresh by
    # a push-notification channel plus a periodic sweep, rather than fetched
    # live from Google on every /availability request the way it used to be.
    # Null/stale is never fatal to a read: get_vendor_availability falls back
    # to a live fetch when the cache is empty or the request reaches past it.
    google_busy_cache = Column(JSON, nullable=True)
    google_busy_synced_at = Column(DateTime, nullable=True)
    # Google Calendar push-notification channel bookkeeping. google_channel_id
    # doubles as the channel's bearer credential, not just its identifier —
    # high-entropy and never exposed anywhere else, so a webhook POST quoting
    # it back is itself sufficient proof it came from the channel this vendor
    # owns (see calendar_service.watch_calendar). resource_id is what Google's
    # API needs to address the specific watched resource; expires_at is when
    # the channel needs renewing (~7 days, Google's own cap for this resource
    # type).
    google_channel_id = Column(String(64), nullable=True, unique=True)
    google_channel_resource_id = Column(String(255), nullable=True)
    google_channel_expires_at = Column(DateTime, nullable=True)

    # Stripe Connect — set during vendor onboarding
    stripe_account_id = Column(String(255), nullable=True)
    stripe_onboarding_complete = Column(Boolean, nullable=False, default=False)

    # Payment track the vendor has chosen — "stripe" keeps the existing
    # card-charge/escrow flow; "manual" means the vendor is paid directly via
    # Venmo/Zelle and Jorna never touches the money. Defaults to "stripe" so
    # every existing vendor's behavior is unchanged until they opt in.
    payment_method = Column(String(20), nullable=False, default="stripe")
    venmo_handle = Column(String(255), nullable=True)
    zelle_contact = Column(String(255), nullable=True)

    # Instagram integration
    instagram_username = Column(String(100), nullable=True, unique=True)
    # Auto-populated by the scraper — kept separate from user-inputted tags
    instagram_tags = Column(JSON, nullable=True)

    # Contract defaults — pure seed values read once by the frontend
    # Contracts builder when a vendor starts a new booking/contract; nothing
    # here is consumed by backend logic beyond being returned on GET
    # /vendors/me. Same shape/purpose as Booking.contract_terms above.
    default_deposit_percent = Column(Integer, nullable=True)
    default_cancellation_window_hours = Column(Integer, nullable=True)
    default_overtime_rate_cents = Column(Integer, nullable=True)
    default_addon_rate_cents = Column(Integer, nullable=True)
    default_contract_terms = Column(JSON, nullable=True)
    # "required" | "optional" | "not_applicable"
    default_guest_count_mode = Column(String(20), nullable=True)

    # Years in business — a fact about the vendor, not about any one package.
    # Service.experience (free text, required) predates this and is kept for
    # older clients; new packages copy this into it (see create_service).
    years_experience = Column(Integer, nullable=True)

    # How many days a sent-but-unsigned contract holds its date before the
    # hold lapses and the date opens up again. Null means the default
    # (contract_service.DEFAULT_HOLD_DAYS). See docs/DECISIONS.md #15.
    contract_hold_days = Column(Integer, nullable=True)

    tags = relationship("Tag", secondary=vendor_tags, backref="vendors")


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (
        # The column used to be free text, and two functions read it with
        # different ideas of what counted as per person — see 0039. Declared on
        # the model as well as in the migration so the schema the tests build
        # from Base.metadata is the schema production runs, and a value that
        # would be refused in production is refused in a test too.
        CheckConstraint(
            "price_unit IS NULL OR price_unit IN ('person', 'hour', 'day', 'event', 'performer')",
            name="ck_services_price_unit",
        ),
        CheckConstraint(
            "status IN ('active', 'hidden', 'archived')",
            name="ck_services_status",
        ),
    )

    service_id = Column(String(36), primary_key=True, default=uuid_str)
    name = Column(String(255), nullable=False)
    price = Column(Float, nullable=False)
    duration_minutes = Column(Integer, nullable=True)
    vendor_id = Column(String(36), ForeignKey("vendors.vendor_id"), nullable=False, index=True)
    experience = Column(Text, nullable=False)
    media = Column(JSON, nullable=True)
    category = Column(String(50), nullable=True)
    subcategory = Column(String(50), nullable=True)
    price_unit = Column(String(50), nullable=True)
    description = Column(Text, nullable=True)
    # This listing's own reviews, averaged. Kept alongside the vendor's rating
    # rather than replacing it: a decorator who is excellent at mandap work and
    # ordinary at florals has two honest numbers, and showing the blended one on
    # both pages tells a client something untrue about the one they're reading.
    # Denormalised the way Vendor.rating is, and recomputed by the same two
    # writers (create_review, delete_review) — there is no third way in.
    #
    # num_reviews counts reviews, and is named for what it counts. Vendor calls
    # the same quantity num_events, which it is not.
    rating = Column(Float, nullable=False, default=0.0)
    num_reviews = Column(Integer, nullable=False, default=0)
    # Whether the vendor allows price negotiation on THIS service (default off).
    # Replaces the vendor-wide open_to_price_negotiation for booking negotiation.
    negotiable = Column(Boolean, nullable=False, default=False)
    # Physical venue location — required for venue-category services (enforced in
    # the router). A booked venue anchors the event's venue and supplies the GPS
    # coordinates traveling vendors check in against.
    location = Column(String(255), nullable=True)
    venue_latitude = Column(Float, nullable=True)
    venue_longitude = Column(Float, nullable=True)
    # Opt-in extras: a vendor can demand a guest/performer count even when the
    # price unit itself doesn't need one to compute a total (e.g. a flat-rate
    # caterer who still wants a headcount before deciding to accept). Additive
    # only — booking_gaps() ORs these with the price-unit-driven requirement,
    # never loosens it.
    require_guest_count = Column(Boolean, nullable=False, default=False)
    require_performer_count = Column(Boolean, nullable=False, default=False)

    # Where this package can be seen and booked (0063):
    #   active   — listed publicly and bookable (every package before 0063).
    #   hidden   — off the public listing, search and bundles, but the vendor
    #              can still put it in a contract: a private/custom package.
    #   archived — retired. Nowhere new; bookings that already reference it
    #              keep working. A package with bookings is archived instead
    #              of deleted, since those bookings can't lose their row.
    status = Column(String(20), nullable=False, default="active", server_default="active")
    # What the price covers, so a client isn't left to guess from free text.
    included_hours = Column(Float, nullable=True)
    inclusions = Column(JSON, nullable=True)        # list[str]
    # Optional extras on top of the base price: [{id, name, price, price_unit}]
    # with price_unit one of event/person/hour. JSON rather than a table: a
    # contract snapshots the ones it uses, so nothing joins back to these.
    add_ons = Column(JSON, nullable=True)
    # Per-package contract terms. Null means "use the vendor's default_*".
    deposit_percent = Column(Integer, nullable=True)
    cancellation_window_hours = Column(Integer, nullable=True)
    overtime_rate_cents = Column(Integer, nullable=True)
    # The vendor's own ordering of their packages; null sorts last.
    sort_order = Column(Integer, nullable=True)


class Booking(Base):
    __tablename__ = "bookings"

    booking_id = Column(String(36), primary_key=True, default=uuid_str)
    # Nullable: a guest/contract booking (see guest_name/contract_token
    # below) has no client account at all.
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=True, index=True)
    vendor_id = Column(String(36), ForeignKey("vendors.vendor_id"), nullable=False, index=True)
    service_id = Column(String(36), ForeignKey("services.service_id"), nullable=False, index=True)
    time_start = Column(String(50), nullable=False)
    time_end = Column(String(50), nullable=False)
    location = Column(String(255), nullable=False)
    date_iso = Column(String(50), nullable=False)
    date_end = Column(String(50), nullable=True)   # null means single-day event
    # Number of guests for the event. Persisted so a per-person service's total
    # (rate x guests) can be (re)computed and audited at booking + checkout time
    # instead of silently falling back to the bare per-person rate.
    guest_count = Column(Integer, nullable=True)
    # Same idea as guest_count, for a per-performer service (entertainment
    # groups charging by how many performers they're asked to provide).
    performer_count = Column(Integer, nullable=True)
    # Free text the client leaves when requesting the booking ("Anything the
    # vendor should know?"), shown to the vendor alongside the request before
    # they accept/decline. Optional; most bookings carry none.
    client_note = Column(Text, nullable=True)
    status = Column(String(50), nullable=False, default="pending")

    bundle_id = Column(String(36), ForeignKey("bundles.bundle_id"), nullable=True, index=True)

    venue_latitude = Column(Float, nullable=True)
    venue_longitude = Column(Float, nullable=True)
    client_checked_in_at = Column(String(50), nullable=True)
    vendor_checked_in_at = Column(String(50), nullable=True)

    # Payment — populated when the customer pays after booking is confirmed
    payment_intent_id = Column(String(255), nullable=True, index=True)
    # Stripe Checkout Session id — stored so the app can reconcile payment status
    # straight from Stripe on return from checkout (safety net for a delayed or
    # misconfigured payment_intent.succeeded webhook). See sync_booking_payment.
    checkout_session_id = Column(String(255), nullable=True)
    # unpaid | processing | paid | released | refunded | cancelled | disputed
    #   | marked_paid | confirmed_paid | deposit_marked_paid | deposit_confirmed_paid
    # 'cancelled' is distinct from 'refunded': it's a post-grace client
    # cancellation split between the platform and the vendor, not a 100%
    # refund — see cancelled_at / refund_cents / vendor_cancellation_cents.
    # marked_paid/confirmed_paid are the manual-track's own lifecycle (see
    # manual_payment_marked_at/manual_payment_confirmed_at below) — kept
    # distinct from 'paid', which specifically means Stripe processed a
    # charge and Jorna is holding funds. Reusing 'paid' for a self-report
    # would wrongly enable the held-funds release UI for money Jorna never
    # touched. deposit_marked_paid/deposit_confirmed_paid are that same
    # self-report idea applied to just the deposit — see
    # deposit_marked_paid_at/deposit_confirmed_received_at below.
    payment_status = Column(String(50), nullable=False, default="unpaid")
    amount_cents = Column(Integer, nullable=True)       # total charged to customer
    platform_fee_cents = Column(Integer, nullable=True) # Desiconnect's cut
    currency = Column(String(10), nullable=False, default="usd")
    confirmed_at = Column(DateTime, nullable=True)      # when the vendor approved the request — the 24h cancellation grace window runs from here (see stripe_service.cancellation_split)
    paid_at = Column(DateTime, nullable=True)           # when Stripe payment succeeded
    customer_confirmed_at = Column(DateTime, nullable=True)
    vendor_confirmed_at = Column(DateTime, nullable=True)
    funds_released_at = Column(DateTime, nullable=True)
    # When the vendor was emailed their half-hour warning. The sweep passes
    # through the window several times, so the send is recorded and the window
    # filters on it — otherwise a vendor gets the same email every five minutes
    # for half an hour.
    checkin_reminder_sent_at = Column(DateTime, nullable=True)
    # When the client last asked for that email to go again. Deliberately not
    # the column above: the sweep sends only where that one is null, so writing
    # a manual resend into it would mean a client nudging a vendor early had
    # switched off the real reminder by doing them a favour.
    checkin_reminder_resent_at = Column(DateTime, nullable=True)
    # This booking's event on the vendor's own Google Calendar, if one has
    # been written back — null until approval creates it, cleared again once
    # the booking ends (declined, cancelled, refunded). The one field
    # calendar_service needs to tell "create" from "update" from "nothing to
    # delete."
    google_event_id = Column(String(255), nullable=True)

    # Set only by stripe_service.cancel_booking — a client cancelling a paid,
    # accepted booking. Kept apart from platform_fee_cents (the ordinary
    # transaction fee) because a cancellation splits the money on its own
    # schedule: refund_cents is what went back to the client (0, or the full
    # amount_cents, never in between — see cancellation_split), and
    # vendor_cancellation_cents is what was transferred to the vendor as
    # their share of a booking that didn't get a full refund. Whatever's left
    # of amount_cents after both is the platform's share, implicitly (never
    # transferred anywhere, so not worth its own column).
    cancelled_at = Column(DateTime, nullable=True)
    refund_cents = Column(Integer, nullable=True)
    vendor_cancellation_cents = Column(Integer, nullable=True)

    # Why `status` became "rejected" — that one value covers a vendor
    # declining outright, a vendor withdrawing after already approving, and a
    # failed reschedule's refund, with nothing before this to tell them apart
    # (a client-initiated cancellation is already distinguishable via
    # cancelled_at above). Nullable, and left null on every row written
    # before this column existed rather than guessed at after the fact — see
    # RejectionReason in app/models/schemas.py.
    rejected_reason = Column(String(30), nullable=True)

    # Snapshot of the vendor's payment_method at the moment this booking was
    # created, so a vendor changing tracks later never rewrites the terms of
    # a booking already in flight. Null for any booking created before this
    # column existed — treated as "stripe" everywhere it's read, since every
    # such booking predates the manual track entirely.
    payment_method = Column(String(20), nullable=True)

    # The manual track's own two-sided attestation — set by
    # stripe_service.mark_booking_paid / confirm_payment_received. Neither
    # is proof of anything; Jorna never touches this money, so these are
    # just what each side told the app happened, timestamped for the record.
    manual_payment_marked_at = Column(DateTime, nullable=True)
    manual_payment_confirmed_at = Column(DateTime, nullable=True)

    # user_id is nullable to support a guest/contract booking: a vendor
    # authors the whole thing (event, price, terms) for a client who has
    # never signed up for Jorna, and that client fills in their own contact
    # info and e-signs via a public link with no login at all. These three
    # are that client's own words, not a User row's — see guest_name/email/
    # phone below and contract_token for the link's credential. See
    # docs/DECISIONS.md's guest-booking entry for what a guest booking can't
    # do (messaging, negotiation, change requests, GPS check-in) and why.
    guest_name = Column(String(255), nullable=True)
    guest_email = Column(String(255), nullable=True)
    guest_phone = Column(String(50), nullable=True)
    # The public link's whole credential, same pattern as Guest.token
    # (guest_service._token()) — unguessable, not derived from booking_id,
    # only ever set on a guest/contract booking.
    contract_token = Column(String(64), unique=True, index=True, nullable=True)

    # Contract terms, snapshotted at creation from the vendor's own defaults
    # (Vendor.default_* below) or overridden per booking — never recomputed
    # later, same "snapshot the deal" discipline as payment_method above.
    # Typed columns for anything ever compared/computed against; free-form
    # prose terms (equipment/power, travel, custom clauses) live in
    # contract_terms instead rather than one column per clause.
    deposit_percent = Column(Integer, nullable=True)
    deposit_amount_cents = Column(Integer, nullable=True)
    cancellation_window_hours = Column(Integer, nullable=True)
    overtime_rate_cents = Column(Integer, nullable=True)
    addon_rate_cents = Column(Integer, nullable=True)
    contract_terms = Column(JSON, nullable=True)

    # Presence of signed_at *is* "signed" — no separate boolean, same
    # pattern as manual_payment_marked_at above. Once set, the contract is
    # immutable (see contract_service's guard).
    signer_name = Column(String(255), nullable=True)
    signed_at = Column(DateTime, nullable=True)

    # Where a contract stands as an offer — only ever set on a contract
    # (contract_token) booking. draft / sent / viewed / signed / declined /
    # voided. "expired" is never stored: it's a sent or viewed contract whose
    # hold_expires_at has passed, derived on read (contract_service.
    # contract_state) so nothing has to sweep it. Only sent/viewed with a
    # future hold_expires_at — or signed — commits the vendor's date (see
    # booking_service.commits_vendor_date). docs/DECISIONS.md #15.
    contract_status = Column(String(20), nullable=True)
    sent_at = Column(DateTime, nullable=True)        # the most recent send
    viewed_at = Column(DateTime, nullable=True)      # first time the client opened it
    hold_expires_at = Column(DateTime, nullable=True)
    declined_at = Column(DateTime, nullable=True)
    decline_reason = Column(String(500), nullable=True)
    voided_at = Column(DateTime, nullable=True)

    # What the agreement says (0065, docs/DECISIONS.md #16), all snapshots —
    # nothing here joins back to the package it came from, so editing or
    # archiving a package never rewrites a contract. See contract_document.
    #   line_items: [{id, kind: package|addon|custom, service_id, addon_id,
    #                 name, description, unit, unit_price_cents, quantity,
    #                 total_cents}]; amount_cents stays the grand total.
    #   payment_schedule: [{id, label, amount_cents, due_type: on_signing|
    #                 date|before_event, due_date, due_days, marked_paid_at,
    #                 confirmed_at}]. Null on a contract written before
    #                 schedules — those keep the single deposit fields.
    #   terms_clauses: [{key, title, body}], the clause text as sent.
    line_items = Column(JSON, nullable=True)
    discount_cents = Column(Integer, nullable=True)
    payment_schedule = Column(JSON, nullable=True)
    terms_clauses = Column(JSON, nullable=True)
    # Bumped by every edit to a contract that's already been sent, so a
    # signature can say which version it was for (sign 409s on a mismatch).
    revision = Column(Integer, nullable=True)
    # Exactly what was signed, frozen at signing, and its SHA-256.
    signed_snapshot = Column(JSON, nullable=True)
    signed_snapshot_sha256 = Column(String(64), nullable=True)

    # A second self-attestation pair, alongside manual_payment_marked_at/
    # manual_payment_confirmed_at above — that existing pair keeps meaning
    # "the full/remaining balance"; these two mean "the deposit
    # specifically." A booking with deposit_percent unset skips this pair
    # entirely and behaves exactly as before this column existed.
    deposit_marked_paid_at = Column(DateTime, nullable=True)
    deposit_confirmed_received_at = Column(DateTime, nullable=True)

    # When the request or contract was made (0066). Null on every booking
    # from before the column existed — nothing on those says when.
    created_at = Column(DateTime, nullable=True, default=datetime.utcnow)
    # The vendor hid it from their active leads (0066). Not a status: an
    # archived request is still pending to its client, and unarchiving puts
    # it back exactly as it was. Only ever set on something not yet signed.
    vendor_archived_at = Column(DateTime, nullable=True)

    # The document editor's view of the agreement (0067): its title, and its
    # blocks in order — terms sections with their text, and where the
    # structured blocks (event, items, schedule, signature) sit. Terms are
    # also mirrored into terms_clauses, which the signing page reads.
    document_title = Column(String(200), nullable=True)
    document_layout = Column(JSON, nullable=True)


class ContractTemplate(Base):
    """A vendor's reusable starting point for a contract — items, schedule
    rule, clauses, terms. Stored per account (0065) instead of per browser.
    The body is the builder's own shape, opaque here: nothing on the server
    computes from a template, it only hands it back."""

    __tablename__ = "contract_templates"

    template_id = Column(String(36), primary_key=True, default=uuid_str)
    vendor_id = Column(String(36), ForeignKey("vendors.vendor_id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    body = Column(JSON, nullable=False)
    # agreement | addendum | cancellation (0067) — which gallery card it is.
    kind = Column(String(20), nullable=False, default="agreement", server_default="agreement")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ContractDocument(Base):
    """An addendum or cancellation agreement attached to a signed booking
    (0067, docs/DECISIONS.md #21). Text only — it never changes the
    booking's price, date or hold (that's v2) — with its own no-login link
    and typed signature, frozen and hashed at signing like a contract."""

    __tablename__ = "contract_documents"

    document_id = Column(String(36), primary_key=True, default=uuid_str)
    booking_id = Column(String(36), ForeignKey("bookings.booking_id", ondelete="CASCADE"), nullable=False, index=True)
    vendor_id = Column(String(36), ForeignKey("vendors.vendor_id"), nullable=False)
    kind = Column(String(20), nullable=False)  # addendum | cancellation
    title = Column(String(200), nullable=False)
    sections = Column(JSON, nullable=False)  # [{key, title, body}]
    token = Column(String(64), unique=True, index=True, nullable=False)
    # draft | sent | viewed | signed | declined | voided
    status = Column(String(20), nullable=False, default="draft", server_default="draft")
    sent_at = Column(DateTime, nullable=True)
    viewed_at = Column(DateTime, nullable=True)
    signed_at = Column(DateTime, nullable=True)
    signer_name = Column(String(255), nullable=True)
    declined_at = Column(DateTime, nullable=True)
    decline_reason = Column(String(500), nullable=True)
    voided_at = Column(DateTime, nullable=True)
    signed_snapshot = Column(JSON, nullable=True)
    signed_snapshot_sha256 = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ContractRevision(Base):
    """One version of a contract's terms (0068): what the booking said at
    that revision number, so a change proposal or a revision can be shown
    side by side with the version before it. Written whenever the revision
    changes; contracts from before 0068 have rows only from then on."""

    __tablename__ = "contract_revisions"
    __table_args__ = (UniqueConstraint("booking_id", "revision", name="uq_contract_revisions_booking_revision"),)

    revision_id = Column(String(36), primary_key=True, default=uuid_str)
    booking_id = Column(String(36), ForeignKey("bookings.booking_id", ondelete="CASCADE"), nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    terms = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ContractProposal(Base):
    """A client's suggested edits to an unsigned contract (0068,
    docs/DECISIONS.md #23): the whole proposed terms, based on one revision.
    The vendor accepts (the terms become the next revision), declines (the
    current one stands) or revises (they send their own next revision). The
    contract is always the vendor's position — there's no counter-proposal.

    status: open | accepted | declined | revised | superseded | withdrawn.
    At most one is open per contract."""

    __tablename__ = "contract_proposals"

    proposal_id = Column(String(36), primary_key=True, default=uuid_str)
    booking_id = Column(String(36), ForeignKey("bookings.booking_id", ondelete="CASCADE"), nullable=False, index=True)
    base_revision = Column(Integer, nullable=False)
    proposed = Column(JSON, nullable=False)
    message = Column(String(2000), nullable=True)
    status = Column(String(20), nullable=False, default="open", server_default="open")
    response_note = Column(String(1000), nullable=True)
    result_revision = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    responded_at = Column(DateTime, nullable=True)


class ContractEvent(Base):
    """One line of a contract's timeline — sent, viewed, edited, signed, a
    payment marked or confirmed. Append-only. Contracts from before 0065 have
    none; their timeline is derived from the booking's own timestamps
    (contract_document.timeline)."""

    __tablename__ = "contract_events"

    event_id = Column(String(36), primary_key=True, default=uuid_str)
    booking_id = Column(String(36), ForeignKey("bookings.booking_id", ondelete="CASCADE"), nullable=False, index=True)
    at = Column(DateTime, nullable=False, default=datetime.utcnow)
    kind = Column(String(40), nullable=False)
    actor = Column(String(20), nullable=False)  # vendor | client | system
    detail = Column(JSON, nullable=True)


class Lead(Base):
    """An informal, off-platform prospect a vendor wants to track before it
    becomes a real Booking — no client account, no committed date/price yet.
    A Booking requires a service_id/date/time/location; a lead usually has
    none of that ("might be October, no venue yet"), so it isn't shoehorned
    into Booking with everything nullable. Converting a lead creates a real
    Booking and sets converted_booking_id — the lead row is kept afterward
    as CRM history of how that client was won, not deleted.
    """
    __tablename__ = "leads"

    lead_id = Column(String(36), primary_key=True, default=uuid_str)
    vendor_id = Column(String(36), ForeignKey("vendors.vendor_id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    phone = Column(String(50), nullable=True)
    email = Column(String(255), nullable=True)
    # Free-text, not date_iso's strict format — a lead often doesn't have a
    # real date yet ("fall 2026", "TBD").
    event_date_iso = Column(String(50), nullable=True)
    note = Column(Text, nullable=True)
    # new | contacted | quoted | won | lost — vendor-set, purely descriptive
    status = Column(String(20), nullable=False, default="new")
    converted_booking_id = Column(String(36), ForeignKey("bookings.booking_id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    # Hidden from the vendor's active leads (0066); see Booking.vendor_archived_at.
    archived_at = Column(DateTime, nullable=True)
    # Set when the lead came from a Messages thread ("Add to leads", 0066):
    # the client it's about, and where the conversation is.
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    conversation_id = Column(String(36), ForeignKey("conversations.conversation_id"), nullable=True, index=True)


class Bundle(Base):
    __tablename__ = "bundles"

    bundle_id = Column(String(36), primary_key=True, default=uuid_str)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    event_id = Column(String(36), ForeignKey("events.event_id"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    event_name = Column(String(255), nullable=True)
    # draft | active | completed | cancelled
    status = Column(String(20), nullable=False, default="draft")
    # Set when 3 comparison bundles are generated together — used to delete the unchosen ones
    bundle_group_id = Column(String(36), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class Negotiation(Base):
    __tablename__ = "negotiations"

    negotiation_id = Column(String(36), primary_key=True, default=uuid_str)
    booking_id = Column(String(36), ForeignKey("bookings.booking_id"), nullable=False, unique=True, index=True)
    # open | accepted | rejected
    status = Column(String(20), nullable=False, default="open")
    current_offer_cents = Column(Integer, nullable=False)
    proposed_by = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class NegotiationOffer(Base):
    __tablename__ = "negotiation_offers"

    offer_id = Column(String(36), primary_key=True, default=uuid_str)
    negotiation_id = Column(String(36), ForeignKey("negotiations.negotiation_id"), nullable=False, index=True)
    proposed_by = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    # offer | counter | accept | reject
    action = Column(String(20), nullable=False)
    amount_cents = Column(Integer, nullable=True)   # None for accept/reject
    message = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False)


class ChangeRequest(Base):
    """A proposal to move a booking that has already been agreed.

    Once a request reaches a vendor, plan_readiness.COMMITTED_FIELDS freezes what
    they were told — correctly, since that is what they agreed to. But events
    move, and a paid booking had no route to a new date at all: no cancel button,
    and remove_booking_from_bundle refuses once money is involved. The only exits
    were a dispute, which is adversarial and wrong for "the venue flooded", or
    nothing, which leaves a vendor turning up on a dead date.

    Turn-based, like a negotiation: the client proposes, the vendor answers, and
    only a resolution touches the booking. Escrow does not move on a proposal —
    so a client cannot free their money by proposing an impossible date, and a
    vendor cannot strand it by ignoring one.

    There is no plan_id. A proposal is n rows created in one transaction, one per
    live booking, read back by joining on the bundle those bookings already
    belong to. Nothing new owns the grouping.
    """

    __tablename__ = "change_requests"

    change_request_id = Column(String(36), primary_key=True, default=uuid_str)
    booking_id = Column(
        String(36), ForeignKey("bookings.booking_id"), nullable=False, index=True
    )
    # Always the client in v1. The column allows the other direction — a vendor
    # who needs to move asks in the chat for now.
    proposed_by = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    # pending | accepted | declined | withdrawn | expired
    status = Column(String(20), nullable=False, default="pending", index=True)

    # What is being asked for. Null means "leave this as it is" — a proposal may
    # move only the date, only the hours, or both.
    date_iso = Column(String(10), nullable=True)
    date_end = Column(String(10), nullable=True)
    time_start = Column(String(5), nullable=True)
    time_end = Column(String(5), nullable=True)

    # Set when the vendor accepted and the new dates cost more than the old ones.
    # The move is held here until the client agrees to the difference — nobody
    # should be charged more by a flow they started to solve a scheduling
    # problem.
    repriced_amount_cents = Column(Integer, nullable=True)
    client_consented_at = Column(DateTime, nullable=True)

    message = Column(Text, nullable=True)
    # What the vendor said when declining, or why the server refused.
    response_message = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False)
    resolved_at = Column(DateTime, nullable=True)


class StripeWebhookEvent(Base):
    """Tracks processed Stripe webhook event IDs to prevent duplicate processing."""

    __tablename__ = "stripe_webhook_events"

    event_id = Column(String(255), primary_key=True)  # Stripe's evt_... ID
    processed_at = Column(DateTime, nullable=False)


class VendorAvailability(Base):
    __tablename__ = "vendor_availability"

    availability_id = Column(String(36), primary_key=True, default=uuid_str)
    vendor_id = Column(String(36), ForeignKey("vendors.vendor_id"), nullable=False, index=True)
    day_of_week = Column(Integer, nullable=False)
    start_time = Column(String(5), nullable=False)
    end_time = Column(String(5), nullable=False)


class Message(Base):
    __tablename__ = "messages"

    message_id = Column(String(36), primary_key=True, default=uuid_str)
    booking_id = Column(String(36), ForeignKey("bookings.booking_id"), nullable=False, index=True)
    sender_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    receiver_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)
    is_read = Column(Boolean, nullable=False, default=False)


class Conversation(Base):
    __tablename__ = "conversations"

    conversation_id = Column(String(36), primary_key=True, default=uuid_str)
    # What this conversation is about: bundle | booking | enquiry. Exactly one
    # of the three ids below is set, and which one is decided by this.
    subject_type = Column(String(16), nullable=False, default="bundle")
    # Nullable since 0042. A conversation used to need a plan to exist, which
    # is why a client could not ask a vendor a question before making one.
    bundle_id = Column(String(36), ForeignKey("bundles.bundle_id"), nullable=True, index=True)
    booking_id = Column(String(36), ForeignKey("bookings.booking_id"), nullable=True, index=True)
    vendor_id = Column(String(36), ForeignKey("vendors.vendor_id"), nullable=True, index=True)
    # The client half of a two-person thread, denormalised so an enquiry can be
    # found — and constrained to one per pair — without walking the membership
    # table. Null on a bundle chat, which has no single client-shaped side.
    client_user_id = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    # vendors_only | all_parties | direct
    type = Column(String(20), nullable=False)
    name = Column(String(255), nullable=False)
    created_at = Column(DateTime, nullable=False)


class ConversationMember(Base):
    __tablename__ = "conversation_members"
    __table_args__ = (UniqueConstraint("conversation_id", "user_id", name="uq_conv_member"),)

    member_id = Column(String(36), primary_key=True, default=uuid_str)
    conversation_id = Column(String(36), ForeignKey("conversations.conversation_id"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    joined_at = Column(DateTime, nullable=False)
    # "Mark as unread" (0066): the thread counts as unread for this member
    # until they next open it, whatever their read receipts say.
    marked_unread_at = Column(DateTime, nullable=True)


class GroupMessage(Base):
    __tablename__ = "group_messages"

    message_id = Column(String(36), primary_key=True, default=uuid_str)
    conversation_id = Column(String(36), ForeignKey("conversations.conversation_id"), nullable=False, index=True)
    sender_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)
    # text | offer | system. `content` is always a human sentence, so a client
    # that doesn't know a kind still renders something true — the kind only
    # decides whether there is a card around it.
    kind = Column(String(16), nullable=False, default="text")
    # Whatever that card needs: an offer_id and an amount, a service_id for a
    # reference card, nothing at all for text. Never queried into.
    meta = Column(JSON, nullable=True)


class GroupMessageRead(Base):
    __tablename__ = "group_message_reads"
    __table_args__ = (UniqueConstraint("message_id", "user_id", name="uq_msg_read"),)

    read_id = Column(String(36), primary_key=True, default=uuid_str)
    message_id = Column(String(36), ForeignKey("group_messages.message_id"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    read_at = Column(DateTime, nullable=False)


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("booking_id", name="uq_review_booking"),)

    review_id = Column(String(36), primary_key=True, default=uuid_str)
    booking_id = Column(String(36), ForeignKey("bookings.booking_id"), nullable=False, index=True)
    vendor_id = Column(String(36), ForeignKey("vendors.vendor_id"), nullable=False, index=True)
    # Which listing was reviewed. Copied from the booking at write time rather
    # than read through it, because a review is displayed on the service page
    # and joining two tables to find out which page it belongs on is a join too
    # many. Nullable: it is set for every review written since 0040, and the
    # backfill filled every row that existed, but a review outliving its service
    # should lose its page rather than disappear from the vendor's record.
    service_id = Column(String(36), ForeignKey("services.service_id"), nullable=True, index=True)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    rating = Column(Float, nullable=False)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    token_id = Column(String(36), primary_key=True, default=uuid_str)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    # SHA-256 hex digest of the raw token — never store the raw value
    token_hash = Column(String(64), unique=True, nullable=False)
    # Rotation family UUID — all tokens in a chain share the same family.
    # Used to detect replay attacks: if a family exists but the hash is wrong,
    # someone is replaying a rotated token → wipe all tokens for the user.
    family = Column(String(36), nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    token_id = Column(String(36), primary_key=True, default=uuid_str)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    # SHA-256 hex digest of the raw token — never store the raw value
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False)


class ContentReport(Base):
    """A user-filed report against content or another user (App Store guideline
    1.2 requires a reporting mechanism for user-generated content)."""

    __tablename__ = "content_reports"

    report_id = Column(String(36), primary_key=True, default=uuid_str)
    reporter_user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    # user | vendor | review | message | conversation
    target_type = Column(String(20), nullable=False)
    target_id = Column(String(36), nullable=False, index=True)
    reason = Column(String(50), nullable=False)   # spam | inappropriate | harassment | scam | other
    details = Column(Text, nullable=True)
    # open | reviewed | dismissed
    status = Column(String(20), nullable=False, default="open")
    created_at = Column(DateTime, nullable=False)


class UserBlock(Base):
    """blocker no longer wants to see blocked's content (messages, reviews,
    listings). Enforced client-side from GET /users/me/blocks."""

    __tablename__ = "user_blocks"
    __table_args__ = (UniqueConstraint("blocker_user_id", "blocked_user_id", name="uq_user_block"),)

    block_id = Column(String(36), primary_key=True, default=uuid_str)
    blocker_user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    blocked_user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False)


class Event(Base):
    __tablename__ = "events"

    event_id = Column(String(36), primary_key=True, default=uuid_str)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    date_iso = Column(String(50), nullable=False)
    location = Column(String(255), nullable=False)
    event_type = Column(String(100), nullable=True)
    description = Column(Text, nullable=True)
    guest_count = Column(Integer, nullable=True)
    budget = Column(Float, nullable=True)
    services_needed = Column(JSON, nullable=True)
    # The event's venue anchor — source of truth for GPS check-in. Synced from the
    # live venue-category booking (sync_event_venue); cleared when no venue is
    # booked, so removing/refunding the venue can't leave dependents checking in
    # against a venue the client no longer has.
    venue_latitude = Column(Float, nullable=True)
    venue_longitude = Column(Float, nullable=True)
    # Where the client says it is: their own address, geocoded. Kept apart from
    # the pair above because that pair is derived and is cleared along with the
    # booking it came from — this one is the client's, and outlives any venue.
    # Used for check-in only when no venue is booked (see check_in).
    address_latitude = Column(Float, nullable=True)
    address_longitude = Column(Float, nullable=True)
    # The open invite link — one a host can drop in a family group chat, where
    # whoever opens it adds themselves. Minted on first use, so an event that
    # never has a guest list never has one.
    invite_token = Column(String(64), nullable=True, unique=True, index=True)


class EventFunction(Base):
    """One gathering within a celebration — a mehndi, a sangeet, a reception.

    A wedding here is rarely a single event, and the guest lists differ between
    its parts, as do the per-person totals: a caterer bills against the headcount
    for the function they're working, not for the week.

    An event with no functions behaves exactly as it always did. One is created
    when a host starts a guest list, so the simple case is a celebration with a
    single function whose name is the event's.
    """
    __tablename__ = "event_functions"

    function_id = Column(String(36), primary_key=True, default=uuid_str)
    event_id = Column(String(36), ForeignKey("events.event_id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    # Its own day and hours — the reason functions exist at all. Null falls back
    # to the event's.
    date_iso = Column(String(50), nullable=True)
    time_start = Column(String(50), nullable=True)
    time_end = Column(String(50), nullable=True)
    location = Column(String(255), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Guest(Base):
    """Somebody invited to a celebration — a person or a household.

    One row per invitation rather than per body. "The Kapoor family, 4" is how a
    guest list is actually kept, and counting invitations rather than names is
    what makes the headcount add up.
    """
    __tablename__ = "guests"

    guest_id = Column(String(36), primary_key=True, default=uuid_str)
    event_id = Column(String(36), ForeignKey("events.event_id"), nullable=False, index=True)
    # The only required field. A list you can't start until you have everybody's
    # email is a list nobody starts.
    name = Column(String(200), nullable=False)
    email = Column(String(255), nullable=True)
    phone = Column(String(50), nullable=True)
    # How many the host expects under this name; the guest corrects it when they
    # reply.
    party_size = Column(Integer, nullable=False, default=1)
    # The credential on a page with no login, so it's random rather than derived
    # from the id.
    token = Column(String(64), nullable=False, unique=True, index=True)
    note = Column(Text, nullable=True)
    # Arrived through the open link rather than being added by the host. Worth
    # being able to tell apart when a headcount grows on its own.
    self_added = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class GuestInvite(Base):
    """Which functions a guest is asked to, and what they said about each.

    Being in this table is the invitation. The status is the answer, and
    attending_count is what they actually committed to — which is not always
    what the host put down for them.
    """
    __tablename__ = "guest_invites"
    __table_args__ = (UniqueConstraint("guest_id", "function_id", name="uq_guest_function"),)

    invite_id = Column(String(36), primary_key=True, default=uuid_str)
    guest_id = Column(String(36), ForeignKey("guests.guest_id"), nullable=False, index=True)
    function_id = Column(String(36), ForeignKey("event_functions.function_id"), nullable=False, index=True)
    # no_reply | attending | declined
    status = Column(String(20), nullable=False, default="no_reply")
    attending_count = Column(Integer, nullable=True)
    responded_at = Column(DateTime, nullable=True)
