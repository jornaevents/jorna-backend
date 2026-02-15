from fastapi import FastAPI

from app.db.database import Base, engine
from app.db import models  # noqa: F401 -- registers tables with Base

app = FastAPI()


@app.on_event("startup")
def startup():
    """Create all SQLite tables if they do not exist."""
    Base.metadata.create_all(bind=engine)


@app.get("/")
def root():
    return {"message": "Jorna API", "status": "ok"}
