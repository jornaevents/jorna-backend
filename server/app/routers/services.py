"""Thin router for service (offering) endpoints — delegates to service_service."""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User
from app.dependencies import get_current_user
from app.models.schemas import VendorCategory, VENDOR_SUBCATEGORIES
from app.services.service_service import (
    ServiceError,
    create_service,
    get_service,
    delete_service,
    list_services,
    update_service,
    add_service_image,
    remove_service_image,
)
from app.services.storage_service import StorageError, upload_service_image, delete_service_image

router = APIRouter(prefix="/services", tags=["services"])


# ── Request schemas ───────────────────────────────────────────────────

# Category is validated against the shared vendor taxonomy. It stays optional on
# the wire (older clients may omit it) — the service layer then defaults it to
# the vendor's own category so no service is ever left uncategorized. Services
# use the same category/subcategory taxonomy as vendors.
_VALID_CATEGORIES = {c.value for c in VendorCategory}


def _validate_category(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    if v not in _VALID_CATEGORIES:
        raise ValueError(f"Invalid category '{v}'. Valid: {sorted(_VALID_CATEGORIES)}")
    return v


def _validate_subcategory(v: Optional[str], info) -> Optional[str]:
    if v is None:
        return v
    category = info.data.get("category")
    valid = VENDOR_SUBCATEGORIES.get(category, []) if category else []
    if valid and v not in valid:
        raise ValueError(f"Invalid subcategory '{v}' for category '{category}'. Valid: {valid}")
    return v


class UpdateServiceRequest(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    duration_minutes: Optional[int] = None
    experience: Optional[str] = None
    media: Optional[list[str]] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    price_unit: Optional[str] = None
    description: Optional[str] = None

    @field_validator("category")
    @classmethod
    def _check_category(cls, v):
        return _validate_category(v)

    @field_validator("subcategory")
    @classmethod
    def _check_subcategory(cls, v, info):
        return _validate_subcategory(v, info)


class CreateServiceRequest(BaseModel):
    name: str
    price: float
    duration_minutes: Optional[int] = None
    experience: str
    media: Optional[list[str]] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    price_unit: Optional[str] = None
    description: Optional[str] = None

    @field_validator("category")
    @classmethod
    def _check_category(cls, v):
        return _validate_category(v)

    @field_validator("subcategory")
    @classmethod
    def _check_subcategory(cls, v, info):
        return _validate_subcategory(v, info)


# ── Routes ────────────────────────────────────────────────────────────


@router.post("", summary="Create a service")
def create_service_route(
    body: CreateServiceRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Add a new service offering. Only vendors can call this."""
    try:
        return create_service(
            user_id=current_user.user_id,
            name=body.name,
            price=body.price,
            duration_minutes=body.duration_minutes,
            experience=body.experience,
            media=body.media,
            category=body.category,
            subcategory=body.subcategory,
            price_unit=body.price_unit,
            description=body.description,
            db=db,
        )
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/{service_id}", summary="Get a single service")
def get_service_route(service_id: str, db: Session = Depends(get_db)):
    """Return a single service by ID. No auth required."""
    try:
        return get_service(service_id=service_id, db=db)
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.patch("/{service_id}", summary="Update a service")
def update_service_route(
    service_id: str,
    body: UpdateServiceRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Partially update a service. Only the owning vendor may call this."""
    try:
        return update_service(
            user_id=current_user.user_id,
            service_id=service_id,
            update_data=body.model_dump(exclude_unset=True),
            db=db,
        )
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/{service_id}", summary="Delete a service", status_code=204)
def delete_service_route(
    service_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a service. Only the owning vendor may call this."""
    try:
        delete_service(user_id=current_user.user_id, service_id=service_id, db=db)
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


MAX_IMAGES_PER_SERVICE = 10
MAX_TOTAL_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB per request


@router.post(
    "/{service_id}/images",
    summary="Upload one or more images to a service",
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "files": {
                                "type": "array",
                                "items": {"type": "string", "format": "binary"},
                            }
                        },
                        "required": ["files"],
                    }
                }
            },
            "required": True,
        }
    },
)
async def upload_service_image_route(
    service_id: str,
    files: List[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload one or more images and append them to the service's media list. Vendors only."""
    from app.db.models import Service as ServiceModel
    try:
        service = db.query(ServiceModel).filter(ServiceModel.service_id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="Service not found")

        existing_count = len(service.media or [])
        if existing_count + len(files) > MAX_IMAGES_PER_SERVICE:
            raise HTTPException(
                status_code=400,
                detail=f"A service may have at most {MAX_IMAGES_PER_SERVICE} images. "
                       f"This service already has {existing_count}.",
            )

        # Read all files into memory first so we can validate before touching storage
        uploads: list[tuple[bytes, str]] = []
        total_bytes = 0
        for file in files:
            data = await file.read()
            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_UPLOAD_BYTES:
                raise HTTPException(status_code=400, detail="Total upload size exceeds 20 MB")
            uploads.append((data, file.content_type or ""))

        # Upload all files to storage; on any failure roll back already-uploaded files
        uploaded_urls: list[str] = []
        try:
            for i, (data, content_type) in enumerate(uploads):
                url = upload_service_image(
                    service_id=service_id,
                    image_index=existing_count + i,
                    file_bytes=data,
                    content_type=content_type,
                )
                uploaded_urls.append(url)
        except StorageError:
            for url in uploaded_urls:
                delete_service_image(url)
            raise

        # All uploads succeeded — commit to DB in one shot
        service.media = list(service.media or []) + uploaded_urls
        db.commit()
        db.refresh(service)
        return {"media": service.media}

    except StorageError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/{service_id}/images", summary="Remove an image from a service", status_code=200)
def delete_service_image_route(
    service_id: str,
    image_url: str = Query(..., description="The exact URL of the image to remove"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove an image URL from the service and delete it from storage. Vendors only."""
    try:
        result = remove_service_image(
            user_id=current_user.user_id,
            service_id=service_id,
            image_url=image_url,
            db=db,
        )
        delete_service_image(image_url)
        return result
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("", summary="List services")
def list_services_route(
    vendor_id: Optional[str] = Query(None, description="Filter by vendor ID"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Return a list of services, optionally filtered by vendor_id. No auth required."""
    return list_services(vendor_id=vendor_id, limit=limit, offset=offset, db=db)
