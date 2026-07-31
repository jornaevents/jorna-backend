"""What ``stripe_onboarding_complete`` is allowed to mean.

It gates whether a vendor can be paid for at all, so it has to mean "Stripe will
pay this person", not "this person reached the end of the form". Those came
apart on an account that submitted its details and was then asked for an ID
number it never supplied: ``details_submitted`` stayed true, payouts were
disabled, and the vendor read as fully onboarded everywhere in the product while
Stripe would not move a cent to their bank.
"""

import uuid

from app.db.models import User, Vendor
from app.services.stripe_service import (
    _onboarding_complete,
    _on_account_updated,
    _payability,
    get_vendor_stripe_status,
)
from tests.test_api import TestingSessionLocal


def _acct(**over) -> dict:
    """A Connect account Stripe is willing to pay out on."""
    account = {
        "details_submitted": True,
        "payouts_enabled": True,
        "capabilities": {"transfers": "active"},
    }
    account.update(over)
    return account


def test_a_payable_account_is_complete():
    assert _onboarding_complete(_acct())


def test_a_finished_form_is_not_enough_if_payouts_are_disabled():
    """The real one: requirements.past_due on an account that filled everything
    in weeks ago. Escrow released here lands somewhere the vendor can see it and
    cannot have it — worse for them than escrow, which at least has a way out."""
    assert not _onboarding_complete(_acct(payouts_enabled=False))


def test_an_abandoned_onboarding_is_not_complete():
    assert not _onboarding_complete(_acct(details_submitted=False))


def test_the_transfers_capability_has_to_be_live():
    """This integration charges on the platform and moves the vendor's share
    with a Transfer, so ``transfers`` is the capability that decides it."""
    assert not _onboarding_complete(_acct(capabilities={"transfers": "pending"}))
    assert not _onboarding_complete(_acct(capabilities={}))


def test_charges_enabled_is_not_the_question():
    """It governs charges a connected account makes for itself, which no vendor
    here ever does. Requiring it would block vendors who are perfectly payable."""
    assert _onboarding_complete(_acct(charges_enabled=False))


def test_a_missing_field_reads_as_not_complete():
    assert not _onboarding_complete({})


def _vendor_with_account(complete: bool, has_account: bool = True):
    db = TestingSessionLocal()
    uid = uuid.uuid4().hex[:8]
    user = User(email=f"vp_{uid}@t.com", username=f"vp_{uid}", password="pw", phone="1",
                f_name="V", l_name="U", age=30, location="NJ", gender="F",
                language="EN", token_version=0)
    db.add(user); db.commit(); db.refresh(user)
    vendor = Vendor(user_id=user.user_id, bio="b", rating=4.5, num_events=1,
                    stripe_account_id=(f"acct_{uid}" if has_account else None),
                    stripe_onboarding_complete=complete)
    db.add(vendor); db.commit(); db.refresh(vendor)
    return db, vendor


def test_the_webhook_revokes_a_vendor_stripe_has_stopped_paying():
    """account.updated fires when a capability is withdrawn, not only when one
    is granted, and that is the transition worth catching: the vendor stops
    being bookable now, rather than at the point their client's money is stuck."""
    db, vendor = _vendor_with_account(complete=True)

    _on_account_updated(
        _acct(id=vendor.stripe_account_id, payouts_enabled=False), db
    )
    db.refresh(vendor)

    assert vendor.stripe_onboarding_complete is False
    db.close()


def test_the_webhook_still_grants_a_vendor_who_finishes():
    db, vendor = _vendor_with_account(complete=False)

    _on_account_updated(_acct(id=vendor.stripe_account_id), db)
    db.refresh(vendor)

    assert vendor.stripe_onboarding_complete is True
    db.close()


# ── Naming what Stripe is waiting on ──────────────────────────────────
#
# Knowing a vendor is unpayable is not enough to tell them anything useful. The
# product said "payment setup incomplete" to someone who completed it months
# earlier and was missing one field nobody had named.


def test_what_is_owed_merges_and_dedupes():
    """past_due is the subset of currently_due that's already late. To the
    vendor it's one list of errands, not two."""
    out = _payability(_acct(requirements={
        "currently_due": ["individual.id_number", "external_account"],
        "past_due": ["individual.id_number"],
        "pending_verification": [],
        "disabled_reason": "requirements.past_due",
    }))
    assert out["requirements_due"] == ["external_account", "individual.id_number"]
    assert out["disabled_reason"] == "requirements.past_due"
    assert out["pending_verification"] is False


def test_being_checked_is_not_being_asked():
    """Stripe verifying a document it already has is nothing the vendor can act
    on, so it must not read as an outstanding errand."""
    out = _payability(_acct(requirements={
        "currently_due": [],
        "past_due": [],
        "pending_verification": ["individual.verification.document"],
        "disabled_reason": None,
    }))
    assert out["requirements_due"] == []
    assert out["pending_verification"] is True


def test_an_account_with_no_requirements_block_is_not_an_error():
    out = _payability(_acct())
    assert out["requirements_due"] == []
    assert out["details_submitted"] is True


def test_the_status_endpoint_names_the_missing_field(mocker):
    """The real account behind the stranded booking: submitted long ago, and
    now past due on an ID number."""
    db, vendor = _vendor_with_account(complete=True)
    mocker.patch(
        "app.services.stripe_service.stripe.Account.retrieve",
        return_value=_acct(
            payouts_enabled=False,
            requirements={
                "currently_due": ["individual.id_number"],
                "past_due": ["individual.id_number"],
                "pending_verification": [],
                "disabled_reason": "requirements.past_due",
            },
        ),
    )

    status = get_vendor_stripe_status(
        vendor_id=vendor.vendor_id, caller_user_id=vendor.user_id, db=db
    )

    assert status["stripe_onboarding_complete"] is False
    assert status["details_submitted"] is True   # the bit that made it confusing
    assert status["requirements_due"] == ["individual.id_number"]
    assert status["disabled_reason"] == "requirements.past_due"
    # and the flag is written through, so every other screen agrees
    db.refresh(vendor)
    assert vendor.stripe_onboarding_complete is False
    db.close()


def test_a_vendor_who_never_started_gets_the_same_shape():
    """Callers tell "never started" from "stalled" by reading
    stripe_account_id, not by finding out which keys exist."""
    db, vendor = _vendor_with_account(complete=False, has_account=False)

    status = get_vendor_stripe_status(
        vendor_id=vendor.vendor_id, caller_user_id=vendor.user_id, db=db
    )

    assert status["stripe_account_id"] is None
    assert status["stripe_onboarding_complete"] is False
    assert status["details_submitted"] is False
    assert status["requirements_due"] == []
    assert status["pending_verification"] is False
    db.close()
