"""Business logic for user profile management."""

from sqlalchemy.orm import Session
from sqlalchemy import delete as sql_delete

from app.db.models import User, Vendor, Service, Booking, VendorAvailability, vendor_tags


class UserError(Exception):
    """Raised when a user operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _user_dict(user: User) -> dict:
    return {
        "user_id": user.user_id,
        "email": user.email,
        "username": user.username,
        "phone": user.phone,
        "f_name": user.f_name,
        "l_name": user.l_name,
        "age": user.age,
        "location": user.location,
        "gender": user.gender,
        "language": user.language,
        "pfp_url": user.pfp_url,
        # False for a Google-only account (users.password is NULL), so account
        # settings can offer "set a password" instead of asking for a current one
        # that does not exist. Never the hash itself.
        "has_password": user.password is not None,
        "open_to_price_negotiation": user.open_to_price_negotiation,
        "flexible_on_location": user.flexible_on_location,
    }


def get_user(*, user_id: str, db: Session) -> dict:
    """Return the profile for *user_id*. Raises 404 if not found."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise UserError(404, "User not found")
    return _user_dict(user)


def delete_user(*, user_id: str, db: Session) -> None:
    """Permanently delete a user and all their associated data."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise UserError(404, "User not found")

    # Delete bookings where this user is the customer
    db.execute(sql_delete(Booking).where(Booking.user_id == user_id))

    # Handle vendor-side cleanup if user is a vendor
    vendor = db.query(Vendor).filter(Vendor.user_id == user_id).first()
    if vendor:
        vendor_id = vendor.vendor_id
        # Delete bookings where this user is the vendor
        db.execute(sql_delete(Booking).where(Booking.vendor_id == vendor_id))
        # Delete vendor availability slots
        db.execute(sql_delete(VendorAvailability).where(VendorAvailability.vendor_id == vendor_id))
        # Delete services
        db.execute(sql_delete(Service).where(Service.vendor_id == vendor_id))
        # Delete vendor_tags join table entries
        db.execute(vendor_tags.delete().where(vendor_tags.c.vendor_id == vendor_id))
        # Delete vendor
        db.delete(vendor)

    db.delete(user)
    db.commit()


def update_user(*, user_id: str, update_data: dict, db: Session) -> dict:
    """Apply *update_data* (partial) to the user and return the updated profile."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise UserError(404, "User not found")
    if "email" in update_data:
        conflict = db.query(User).filter(
            User.email == update_data["email"],
            User.user_id != user_id,
        ).first()
        if conflict:
            raise UserError(400, "Email is already in use by another account")
    for field, value in update_data.items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return _user_dict(user)
