import bcrypt
import jwt
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from typing import List, Optional

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import Base, engine, get_db
from app.db import models  # noqa: F401 -- registers tables with Base
from app.db.models import Booking, Service, User, Vendor

SECRET_KEY = "your-secret-key-change-in-production"
ALGORITHM = "HS256"

security = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    """Extract JWT from Authorization header, decode it, and return the User. Raises 401 if invalid."""
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


class CreateBookingRequest(BaseModel):
    vendor_id: str
    service_id: str
    event_name: str
    time_start: str
    time_end: str
    location: str
    date_iso: str


class UpdateBookingRequest(BaseModel):
    status: str  # "pending", "approved", "rejected", "payment_confirmed"


class UpdateMeRequest(BaseModel):
    f_name: Optional[str] = None
    l_name: Optional[str] = None
    phone: Optional[str] = None
    age: Optional[int] = None
    location: Optional[str] = None
    gender: Optional[str] = None
    language: Optional[str] = None
    pfp_url: Optional[str] = None


app = FastAPI()

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
    existing = db.query(User).filter(
        (User.email == body.email) | (User.username == body.username)
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email or username already taken")
    hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    user = User(
        email=body.email,
        username=body.username,
        phone=body.phone,
        password=hashed,
        f_name=body.f_name,
        l_name=body.l_name,
        age=body.age,
        location=body.location,
        gender=body.gender,
        language=body.language,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"user_id": user.user_id, "email": user.email}


@app.post("/auth/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """Verify email/password and return a JWT."""
    user = db.query(User).filter(User.email == body.email).first()
    if not user or not bcrypt.checkpw(body.password.encode(), user.password.encode()):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = jwt.encode(
        {"sub": user.user_id, "email": user.email},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    return {"access_token": token, "token_type": "bearer"}


@app.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    """Return the current user's profile. Requires valid JWT in Authorization header."""
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
    """Update the current user's profile. Only sends fields you want to change."""
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
    """List all vendors with their user profile info. No auth required."""
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
    """List all services. Optionally filter by vendor_id. No auth required."""
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


@app.post("/bookings")
def create_booking(
    body: CreateBookingRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a booking. User books a vendor's service for an event."""
    service = db.query(Service).filter(Service.service_id == body.service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    if service.vendor_id != body.vendor_id:
        raise HTTPException(status_code=400, detail="Service does not belong to this vendor")
    vendor = db.query(Vendor).filter(Vendor.vendor_id == body.vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    booking = Booking(
        user_id=current_user.user_id,
        vendor_id=body.vendor_id,
        service_id=body.service_id,
        event_name=body.event_name,
        time_start=body.time_start,
        time_end=body.time_end,
        location=body.location,
        date_iso=body.date_iso,
        status="pending",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    return {
        "booking_id": booking.booking_id,
        "user_id": booking.user_id,
        "vendor_id": booking.vendor_id,
        "service_id": booking.service_id,
        "event_name": booking.event_name,
        "time_start": booking.time_start,
        "time_end": booking.time_end,
        "location": booking.location,
        "date_iso": booking.date_iso,
        "status": booking.status,
    }


@app.get("/bookings")
def list_bookings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List bookings for the current user: as customer (their bookings) or as vendor (bookings for their services)."""
    vendor = db.query(Vendor).filter(Vendor.user_id == current_user.user_id).first()
    if vendor:
        bookings = db.query(Booking).filter(
            (Booking.user_id == current_user.user_id) | (Booking.vendor_id == vendor.vendor_id)
        ).all()
    else:
        bookings = db.query(Booking).filter(Booking.user_id == current_user.user_id).all()
    return [
        {
            "booking_id": b.booking_id,
            "user_id": b.user_id,
            "vendor_id": b.vendor_id,
            "service_id": b.service_id,
            "event_name": b.event_name,
            "time_start": b.time_start,
            "time_end": b.time_end,
            "location": b.location,
            "date_iso": b.date_iso,
            "status": b.status,
        }
        for b in bookings
    ]


@app.patch("/bookings/{booking_id}")
def update_booking(
    booking_id: str,
    body: UpdateBookingRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update booking status. Only the vendor of that booking can approve/reject."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    vendor = db.query(Vendor).filter(Vendor.user_id == current_user.user_id).first()
    if not vendor or vendor.vendor_id != booking.vendor_id:
        raise HTTPException(status_code=403, detail="Only the vendor can update this booking")
    valid_statuses = ("pending", "approved", "rejected", "payment_confirmed")
    if body.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Status must be one of: {valid_statuses}")
    booking.status = body.status
    db.commit()
    db.refresh(booking)
    return {
        "booking_id": booking.booking_id,
        "user_id": booking.user_id,
        "vendor_id": booking.vendor_id,
        "service_id": booking.service_id,
        "event_name": booking.event_name,
        "time_start": booking.time_start,
        "time_end": booking.time_end,
        "location": booking.location,
        "date_iso": booking.date_iso,
        "status": booking.status,
    }
