"""An event's gift registry (0089): funds with optional goals, and the gifts
the host records against them. Tracking only — nothing is paid through Jorna.
Every route is the event owner's alone."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import Event, RegistryFund, RegistryGift, User
from app.dependencies import get_current_user

router = APIRouter(tags=["registry"])

MAX_FUNDS = 20
MAX_CENTS = 100_000_000  # $1,000,000 — a typo guard, not a business rule


class FundBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    goal_cents: Optional[int] = Field(default=None, ge=1, le=MAX_CENTS)


class FundPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    goal_cents: Optional[int] = Field(default=None, ge=1, le=MAX_CENTS)


class GiftBody(BaseModel):
    from_name: str = Field(min_length=1, max_length=120)
    amount_cents: int = Field(ge=1, le=MAX_CENTS)
    thanked: bool = False


class GiftPatch(BaseModel):
    from_name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    amount_cents: Optional[int] = Field(default=None, ge=1, le=MAX_CENTS)
    thanked: Optional[bool] = None


def _own_event(event_id: str, user: User, db: Session) -> Event:
    event = db.query(Event).filter(Event.event_id == event_id).first()
    # 404 either way: someone else's event isn't confirmed to exist.
    if not event or event.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


def _own_fund(fund_id: str, user: User, db: Session) -> RegistryFund:
    fund = db.query(RegistryFund).filter(RegistryFund.fund_id == fund_id).first()
    if not fund:
        raise HTTPException(status_code=404, detail="Fund not found")
    _own_event(fund.event_id, user, db)
    return fund


def _own_gift(gift_id: str, user: User, db: Session) -> RegistryGift:
    gift = db.query(RegistryGift).filter(RegistryGift.gift_id == gift_id).first()
    if not gift:
        raise HTTPException(status_code=404, detail="Gift not found")
    _own_fund(gift.fund_id, user, db)
    return gift


def _registry(event_id: str, db: Session) -> dict:
    funds = db.query(RegistryFund).filter(RegistryFund.event_id == event_id).order_by(RegistryFund.created_at).all()
    gifts = (
        db.query(RegistryGift)
        .filter(RegistryGift.fund_id.in_([f.fund_id for f in funds]))
        .order_by(RegistryGift.created_at.desc())
        .all()
    ) if funds else []
    by_fund: dict[str, list[RegistryGift]] = {}
    for g in gifts:
        by_fund.setdefault(g.fund_id, []).append(g)
    return {
        "event_id": event_id,
        "funds": [
            {
                "fund_id": f.fund_id,
                "name": f.name,
                "goal_cents": f.goal_cents,
                "raised_cents": sum(g.amount_cents for g in by_fund.get(f.fund_id, [])),
                "gifts": [
                    {"gift_id": g.gift_id, "from_name": g.from_name, "amount_cents": g.amount_cents,
                     "thanked": bool(g.thanked), "created_at": g.created_at.isoformat()}
                    for g in by_fund.get(f.fund_id, [])
                ],
            }
            for f in funds
        ],
        "total_cents": sum(g.amount_cents for g in gifts),
        "gift_count": len(gifts),
        "thanked_count": sum(1 for g in gifts if g.thanked),
    }


@router.get("/events/{event_id}/registry", summary="An event's registry")
def get_registry(event_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _own_event(event_id, user, db)
    return _registry(event_id, db)


@router.post("/events/{event_id}/registry/funds", summary="Add a fund", status_code=201)
def add_fund(event_id: str, body: FundBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _own_event(event_id, user, db)
    if db.query(RegistryFund).filter(RegistryFund.event_id == event_id).count() >= MAX_FUNDS:
        raise HTTPException(status_code=400, detail=f"A registry can have up to {MAX_FUNDS} funds")
    db.add(RegistryFund(event_id=event_id, name=body.name.strip(), goal_cents=body.goal_cents))
    db.commit()
    return _registry(event_id, db)


@router.patch("/registry/funds/{fund_id}", summary="Rename a fund or change its goal")
def edit_fund(fund_id: str, body: FundPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    fund = _own_fund(fund_id, user, db)
    data = body.model_dump(exclude_unset=True)
    if data.get("name"):
        fund.name = data["name"].strip()
    # An explicit null clears the goal.
    if "goal_cents" in data:
        fund.goal_cents = data["goal_cents"]
    db.commit()
    return _registry(fund.event_id, db)


@router.delete("/registry/funds/{fund_id}", summary="Delete a fund and its gifts")
def delete_fund(fund_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    fund = _own_fund(fund_id, user, db)
    event_id = fund.event_id
    # Deleted by hand as well as by the FK: SQLite (the test database) doesn't
    # enforce ON DELETE CASCADE unless asked to.
    db.query(RegistryGift).filter(RegistryGift.fund_id == fund_id).delete()
    db.delete(fund)
    db.commit()
    return _registry(event_id, db)


@router.post("/registry/funds/{fund_id}/gifts", summary="Record a gift", status_code=201)
def add_gift(fund_id: str, body: GiftBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    fund = _own_fund(fund_id, user, db)
    db.add(RegistryGift(fund_id=fund_id, from_name=body.from_name.strip(), amount_cents=body.amount_cents, thanked=body.thanked))
    db.commit()
    return _registry(fund.event_id, db)


@router.patch("/registry/gifts/{gift_id}", summary="Edit a gift, or mark the giver thanked")
def edit_gift(gift_id: str, body: GiftPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    gift = _own_gift(gift_id, user, db)
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(gift, field, value.strip() if isinstance(value, str) else value)
    db.commit()
    fund = db.query(RegistryFund).filter(RegistryFund.fund_id == gift.fund_id).first()
    return _registry(fund.event_id, db)


@router.delete("/registry/gifts/{gift_id}", summary="Remove a gift")
def delete_gift(gift_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    gift = _own_gift(gift_id, user, db)
    fund = db.query(RegistryFund).filter(RegistryFund.fund_id == gift.fund_id).first()
    db.delete(gift)
    db.commit()
    return _registry(fund.event_id, db)
