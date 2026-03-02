"""Desiconnect FastAPI application entry point.

Registers routers and exposes auth / vendor-search routes that
delegate to the service layer.
"""

import jwt
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from typing import List, Optional
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import Base, engine, get_db
from app.db import models  # noqa: F401 -- registers tables with Base
from app.db.models import User, Vendor, Service
from app.routers import calendar, bookings, notifications
from app.services.auth_service import (
    AuthError, register_user, login_user, SECRET_KEY, ALGORITHM,
)
from app.services.vendor_service import search_vendors


# ── Auth dependency ───────────────────────────────────────────────────

security = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    """Extract JWT from Authorization header, decode it, and return the User."""
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


# ── Request schemas ───────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    email: str
    password: str
    username: str
    phone: str
    f_name: str
    l_name: str
    age: int
    location: str
    gender: str
    language: str


class LoginRequest(BaseModel):
    email: str
    password: str


class CreateVendorRequest(BaseModel):
    bio: str


class CreateServiceRequest(BaseModel):
    name: str
    price: float
    duration_minutes: int
    experience: str
    media: Optional[List[str]] = None


class UpdateMeRequest(BaseModel):
    f_name: Optional[str] = None
    l_name: Optional[str] = None
    phone: Optional[str] = None
    age: Optional[int] = None
    location: Optional[str] = None
    gender: Optional[str] = None
    language: Optional[str] = None
    pfp_url: Optional[str] = None


# ── App setup ─────────────────────────────────────────────────────────

app = FastAPI()
app.include_router(calendar.router)
app.include_router(bookings.router)
app.include_router(notifications.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    """Create all SQLite tables if they do not exist."""
    Base.metadata.create_all(bind=engine)


# ── Routes ────────────────────────────────────────────────────────────


@app.get("/")
def root():
    return {"message": "Jorna API", "status": "ok"}


@app.get("/db-check")
def db_check(db: Session = Depends(get_db)):
    """Test route: proves we can inject a DB session into a route."""
    return {"db": "connected"}


@app.post("/auth/register")
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    """Create a new user. Password is hashed before storage."""
    try:
        return register_user(
            email=body.email,
            password=body.password,
            username=body.username,
            phone=body.phone,
            f_name=body.f_name,
            l_name=body.l_name,
            age=body.age,
            location=body.location,
            gender=body.gender,
            language=body.language,
            db=db,
        )
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.post("/auth/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """Verify email/password and return a JWT."""
    try:
        return login_user(email=body.email, password=body.password, db=db)
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.get("/vendors/search")
def vendor_search(
    service_name: str,
    latitude: float,
    longitude: float,
    db: Session = Depends(get_db),
):
    """Search for vendors offering a specific service within their travel radius."""
    vendors = search_vendors(
        service_name=service_name,
        latitude=latitude,
        longitude=longitude,
        db=db,
    )
    return {"vendors": vendors}


@app.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    """Return the current user's profile. Requires valid JWT."""
    return {
        "user_id": current_user.user_id,
        "email": current_user.email,
        "username": current_user.username,
        "phone": current_user.phone,
        "f_name": current_user.f_name,
        "l_name": current_user.l_name,
        "age": current_user.age,
        "location": current_user.location,
        "gender": current_user.gender,
        "language": current_user.language,
        "pfp_url": current_user.pfp_url,
    }


@app.patch("/me")
def update_me(
    body: UpdateMeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update the current user's profile."""
    user = db.query(User).filter(User.user_id == current_user.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
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
    }


@app.post("/vendors")
def create_vendor(
    body: CreateVendorRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Become a vendor. Creates a vendor profile linked to the current user."""
    existing = db.query(Vendor).filter(Vendor.user_id == current_user.user_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="You already have a vendor profile")
    vendor = Vendor(
        user_id=current_user.user_id,
        bio=body.bio,
        rating=0.0,
        num_events=0,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return {
        "vendor_id": vendor.vendor_id,
        "user_id": vendor.user_id,
        "bio": vendor.bio,
        "rating": vendor.rating,
        "num_events": vendor.num_events,
    }


@app.get("/vendors")
def list_vendors(db: Session = Depends(get_db)):
    """List all vendors with their user profile info."""
    rows = db.query(Vendor, User).join(User, Vendor.user_id == User.user_id).all()
    return [
        {
            "vendor_id": v.vendor_id,
            "user_id": v.user_id,
            "bio": v.bio,
            "rating": v.rating,
            "num_events": v.num_events,
            "f_name": u.f_name,
            "l_name": u.l_name,
            "location": u.location,
            "pfp_url": u.pfp_url,
        }
        for v, u in rows
    ]


@app.post("/services")
def create_service(
    body: CreateServiceRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Add a service. Only vendors can add services."""
    vendor = db.query(Vendor).filter(Vendor.user_id == current_user.user_id).first()
    if not vendor:
        raise HTTPException(status_code=403, detail="You must be a vendor to add services")
    service = Service(
        vendor_id=vendor.vendor_id,
        name=body.name,
        price=body.price,
        duration_minutes=body.duration_minutes,
        experience=body.experience,
        media=body.media,
    )
    db.add(service)
    db.commit()
    db.refresh(service)
    return {
        "service_id": service.service_id,
        "vendor_id": service.vendor_id,
        "name": service.name,
        "price": service.price,
        "duration_minutes": service.duration_minutes,
        "experience": service.experience,
        "media": service.media,
    }


@app.get("/services")
def list_services(
    db: Session = Depends(get_db),
    vendor_id: Optional[str] = Query(None, description="Filter by vendor ID"),
):
    """List all services. Optionally filter by vendor_id."""
    query = db.query(Service)
    if vendor_id:
        query = query.filter(Service.vendor_id == vendor_id)
    services = query.all()
    return [
        {
            "service_id": s.service_id,
            "vendor_id": s.vendor_id,
            "name": s.name,
            "price": s.price,
            "duration_minutes": s.duration_minutes,
            "experience": s.experience,
            "media": s.media,
        }
        for s in services
    ]
