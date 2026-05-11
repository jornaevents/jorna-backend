"""Admin-only endpoints for user management."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User
from app.dependencies import get_current_admin

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/users/{user_id}/make-admin", summary="Promote a user to admin")
def make_admin(
    user_id: str,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Grant admin privileges to a user. Requires admin auth."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.is_admin:
        return {"message": f"{user.email} is already an admin"}
    user.is_admin = True
    db.commit()
    return {"message": f"{user.email} has been promoted to admin", "user_id": user.user_id}


@router.post("/users/{user_id}/revoke-admin", summary="Revoke admin privileges from a user")
def revoke_admin(
    user_id: str,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Revoke admin privileges from a user. Admins cannot revoke their own access."""
    if user_id == current_admin.user_id:
        raise HTTPException(status_code=400, detail="You cannot revoke your own admin access")
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.is_admin:
        return {"message": f"{user.email} is not an admin"}
    user.is_admin = False
    db.commit()
    return {"message": f"{user.email} has had admin access revoked", "user_id": user.user_id}


@router.get("/users", summary="List all admin users")
def list_admins(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Return all users with admin privileges."""
    admins = db.query(User).filter(User.is_admin == True).all()
    return [{"user_id": u.user_id, "email": u.email, "f_name": u.f_name, "l_name": u.l_name} for u in admins]
