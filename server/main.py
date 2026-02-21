import bcrypt
import jwt
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import Base, engine, get_db
from app.db import models  # noqa: F401 -- registers tables with Base
from app.db.models import User

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


app = FastAPI()


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
