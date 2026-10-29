"""The vendor's countersignature, for tests about what happens once a
contract is booked (docs/DECISIONS.md #34). The client signing by link no
longer books it on its own; these sign as the booking's vendor directly
through the service, so a test needn't hold that vendor's auth headers.
test_countersign.py covers the endpoints themselves."""

from app.db.models import Booking, ContractDocument, Vendor
from app.services import countersign_service
from tests.test_api import TestingSessionLocal


def _vendor_user_id(db, vendor_id: str) -> str:
    return db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first().user_id


def countersign(booking_id: str, name: str = "Vendor Name") -> dict:
    with TestingSessionLocal() as db:
        b = db.query(Booking).filter(Booking.booking_id == booking_id).first()
        return countersign_service.countersign_contract(
            booking_id=booking_id, caller_user_id=_vendor_user_id(db, b.vendor_id), signer_name=name,
            consent=True, consent_version=countersign_service.esign_consent.VERSION, name_confirmed=True, db=db,
        )


def countersign_token(contract_token: str, name: str = "Vendor Name") -> dict:
    with TestingSessionLocal() as db:
        booking_id = db.query(Booking).filter(Booking.contract_token == contract_token).first().booking_id
    return countersign(booking_id, name)


def countersign_document(document_id: str, name: str = "Vendor Name") -> dict:
    with TestingSessionLocal() as db:
        d = db.query(ContractDocument).filter(ContractDocument.document_id == document_id).first()
        return countersign_service.countersign_document(
            document_id=document_id, caller_user_id=_vendor_user_id(db, d.vendor_id), signer_name=name,
            consent=True, consent_version=countersign_service.esign_consent.VERSION, name_confirmed=True, db=db,
        )
