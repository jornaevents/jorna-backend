"""Usage events (DECISIONS #57): the web app counts a few named actions; an
admin reads the totals."""


from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.dependencies import get_current_admin, get_current_user
from app.services.usage_service import UsageError, record, summary

router = APIRouter(tags=["usage"])


class UsageEventRequest(BaseModel):
    name: str = Field(..., max_length=40)
    props: dict | None = None


@router.post("/usage-events", summary="Count a use of a feature")
def record_usage_route(
    body: UsageEventRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return record(name=body.name, props=body.props, caller_user_id=current_user.user_id, db=db)
    except UsageError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/admin/usage-summary", summary="Usage totals over recent days (admin)")
def usage_summary_route(
    days: int = Query(30, ge=1, le=365),
    _admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    return summary(days=days, db=db)
