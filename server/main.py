"""Desiconnect FastAPI application entry point.

Registers routers and exposes auth / vendor-search routes that
delegate to the service layer.
"""

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import Base, engine, get_db
from app.db import models  # noqa: F401 -- registers tables with Base
from app.routers import calendar, bookings, notifications, users, vendors, services
from app.services.auth_service import AuthError, register_user, login_user
from app.services.vendor_service import search_vendors


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


# ── App setup ─────────────────────────────────────────────────────────

app = FastAPI()
app.include_router(calendar.router)
app.include_router(bookings.router)
app.include_router(notifications.router)
app.include_router(users.router)
app.include_router(vendors.router)
app.include_router(services.router)

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
