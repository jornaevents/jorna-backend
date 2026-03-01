import bcrypt
import jwt
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import Base, engine, get_db
from app.db import models  # noqa: F401 -- registers tables with Base
from app.db.models import User, Vendor, Service, Booking
from app.utils.location import calculate_distance_miles
from app.routers import calendar
from datetime import datetime, timezone

SECRET_KEY = "your-secret-key-change-in-production"
ALGORITHM = "HS256"


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


class CheckInRequest(BaseModel):
    user_id: str
    is_vendor: bool
    latitude: float
    longitude: float


app = FastAPI()
app.include_router(calendar.router)


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


@app.get("/vendors/search")
def search_vendors(
    service_name: str,
    latitude: float,
    longitude: float,
    db: Session = Depends(get_db)
):
    """
    Search for vendors offering a specific service within their travel radius.
    """
    # Join Service, Vendor, and User to get the matching vendors and their coordinates
    results = db.query(Vendor, Service, User).join(
        Service, Vendor.vendor_id == Service.vendor_id
    ).join(
        User, Vendor.user_id == User.user_id
    ).filter(
        Service.name.ilike(f"%{service_name}%")
    ).all()
    
    nearby_vendors = []
    
    for vendor, service, user in results:
        # Skip if the vendor's user profile hasn't set coordinates
        if user.latitude is None or user.longitude is None:
            continue
            
        distance_miles = calculate_distance_miles(
            latitude, longitude, user.latitude, user.longitude
        )
        
        # Only include if vendor is willing to travel that distance
        if distance_miles <= vendor.travel_radius_miles:
            nearby_vendors.append({
                "vendor_id": vendor.vendor_id,
                "user_id": user.user_id,
                "first_name": user.f_name,
                "last_name": user.l_name,
                "service_name": service.name,
                "service_price": service.price,
                "distance_miles": round(distance_miles, 2),
                "rating": vendor.rating,
                "travel_radius_miles": vendor.travel_radius_miles
            })
            
    # Sort the results so the closest vendors are at the top
    nearby_vendors.sort(key=lambda x: x["distance_miles"])
    
    return {"vendors": nearby_vendors}


@app.post("/bookings/{booking_id}/check-in")
def booking_check_in(
    booking_id: str,
    body: CheckInRequest,
    db: Session = Depends(get_db)
):
    """
    Verify the user is at the venue (within ~0.1 miles / ~528 feet) and successfully check them in.
    """
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
        
    if booking.venue_latitude is None or booking.venue_longitude is None:
        raise HTTPException(status_code=400, detail="Booking has no venue coordinates set")
        
    distance = calculate_distance_miles(
        body.latitude, body.longitude, 
        booking.venue_latitude, booking.venue_longitude
    )
    
    # Allow a margin of error of 0.2 miles (approx 1036 feet)
    if distance > 0.2:
        raise HTTPException(
            status_code=400, 
            detail=f"You must be at the venue to check in. You are currently {round(distance, 2)} miles away."
        )
        
    current_time = datetime.now(timezone.utc).isoformat()
    
    if body.is_vendor:
        # Verify the user checking in is actually the vendor for this booking
        vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
        if not vendor or vendor.user_id != body.user_id:
             raise HTTPException(status_code=403, detail="Unauthorized vendor")
        booking.vendor_checked_in_at = current_time
    else:
        # Verify the user checking in is exactly the client who booked it
        if booking.user_id != body.user_id:
             raise HTTPException(status_code=403, detail="Unauthorized user")
        booking.client_checked_in_at = current_time
        
    db.commit()
    
    return {
        "message": "Check-in successful", 
        "distance_miles": round(distance, 2), 
        "check_in_time": current_time
    }
