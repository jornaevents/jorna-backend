"""Thin router for service (offering) endpoints — delegates to service_service."""

import asyncio
import uuid
from typing import List, Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import Service as ServiceModel, User, Vendor
from app.dependencies import get_current_user, get_optional_user
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
    remove_service_video,
    media_url,
)
from app.services.storage_service import (
    StorageError,
    upload_service_image,
    delete_service_image,
    upload_service_video,
    delete_service_video,
)

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


class MediaItem(BaseModel):
    """One photo or video on a service. Older rows predate this shape and
    store a bare URL string instead — a one-time migration backfills those
    to {"url", "type": "image", "thumbnail_url": None} on deploy, so every
    row read through the API is this shape by the time a client sees it."""
    url: str
    type: str  # "image" | "video"
    thumbnail_url: Optional[str] = None


class AddOn(BaseModel):
    """An optional extra on top of a package's base price."""

    # Stable across edits, so a contract that picked "Extra hour" (Phase 2)
    # can still say which one it was after the vendor renames it.
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12], max_length=40)
    name: str = Field(min_length=1, max_length=120)
    price: float = Field(gt=0)
    # A subset of the package units: an add-on is flat, per guest, or per hour.
    price_unit: Literal["event", "person", "hour"] = "event"


class PackageDetails(BaseModel):
    """Phase 1 package fields, shared by create and update. Everything is
    optional so older clients that never send them keep working."""

    status: Optional[Literal["active", "hidden", "archived"]] = None
    included_hours: Optional[float] = Field(default=None, ge=0, le=24 * 14)
    inclusions: Optional[list[str]] = Field(default=None, max_length=30)
    add_ons: Optional[list[AddOn]] = Field(default=None, max_length=20)
    deposit_percent: Optional[int] = Field(default=None, ge=0, le=100)
    cancellation_window_hours: Optional[int] = Field(default=None, ge=0)
    overtime_rate_cents: Optional[int] = Field(default=None, ge=0)
    sort_order: Optional[int] = None

    @field_validator("inclusions")
    @classmethod
    def _clean_inclusions(cls, v):
        # Blank lines from a textarea aren't inclusions.
        if v is None:
            return v
        cleaned = [item.strip()[:200] for item in v if item and item.strip()]
        return cleaned


class UpdateServiceRequest(PackageDetails):
    name: Optional[str] = None
    price: Optional[float] = None
    duration_minutes: Optional[int] = None
    experience: Optional[str] = None
    media: Optional[list[MediaItem]] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    price_unit: Optional[str] = None
    description: Optional[str] = None
    negotiable: Optional[bool] = None
    # Venue location (required for venue-category services; enforced in service layer).
    location: Optional[str] = None
    venue_latitude: Optional[float] = None
    venue_longitude: Optional[float] = None
    # Opt-in extras — demand a guest/performer count even when price_unit
    # doesn't itself need one. Additive only; see Service model comment.
    require_guest_count: Optional[bool] = None
    require_performer_count: Optional[bool] = None

    @field_validator("price_unit")
    @classmethod
    def _check_price_unit(cls, v):
        # Read forgivingly, stored canonical — see canonical_price_unit. The
        # column holds one of four strings so the function that decides a
        # request may be sent and the one that works out what to charge can
        # never again disagree about what a vendor typed.
        from app.services.booking_service import canonical_price_unit

        return canonical_price_unit(v)

    @field_validator("category")
    @classmethod
    def _check_category(cls, v):
        return _validate_category(v)

    @field_validator("subcategory")
    @classmethod
    def _check_subcategory(cls, v, info):
        return _validate_subcategory(v, info)


class CreateServiceRequest(PackageDetails):
    name: str
    price: float
    duration_minutes: Optional[int] = None
    # Optional since 0063: years in business belong to the vendor. Left out,
    # it's filled from Vendor.years_experience.
    experience: Optional[str] = None
    media: Optional[list[MediaItem]] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    price_unit: Optional[str] = None
    description: Optional[str] = None
    negotiable: bool = False   # per-service price negotiation; default off
    # Venue location (required for venue-category services; enforced in service layer).
    location: Optional[str] = None
    venue_latitude: Optional[float] = None
    venue_longitude: Optional[float] = None
    # Opt-in extras — demand a guest/performer count even when price_unit
    # doesn't itself need one. Additive only; see Service model comment.
    require_guest_count: bool = False
    require_performer_count: bool = False

    @field_validator("price_unit")
    @classmethod
    def _check_price_unit(cls, v):
        # Read forgivingly, stored canonical — see canonical_price_unit. The
        # column holds one of four strings so the function that decides a
        # request may be sent and the one that works out what to charge can
        # never again disagree about what a vendor typed.
        from app.services.booking_service import canonical_price_unit

        return canonical_price_unit(v)

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
            media=[m.model_dump() for m in body.media] if body.media else None,
            category=body.category,
            subcategory=body.subcategory,
            price_unit=body.price_unit,
            description=body.description,
            negotiable=body.negotiable,
            location=body.location,
            venue_latitude=body.venue_latitude,
            venue_longitude=body.venue_longitude,
            require_guest_count=body.require_guest_count,
            require_performer_count=body.require_performer_count,
            status=body.status or "active",
            included_hours=body.included_hours,
            inclusions=body.inclusions,
            add_ons=[a.model_dump() for a in body.add_ons] if body.add_ons is not None else None,
            deposit_percent=body.deposit_percent,
            cancellation_window_hours=body.cancellation_window_hours,
            overtime_rate_cents=body.overtime_rate_cents,
            sort_order=body.sort_order,
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

MAX_VIDEOS_PER_SERVICE = 3
# A single video is capped at 50 MB (storage_service.MAX_VIDEO_FILE_SIZE);
# this is the per-request ceiling across however many are sent at once.
MAX_VIDEO_TOTAL_UPLOAD_BYTES = 50 * 1024 * 1024


def _require_owning_vendor(service: ServiceModel, user_id: str, db: Session) -> None:
    vendor = db.query(Vendor).filter(Vendor.vendor_id == service.vendor_id).first()
    if not vendor or vendor.user_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this service")


def _count_media(service: ServiceModel, media_type: str) -> int:
    # An entry with no real URL behind it — a failed upload, a delete that
    # didn't fully clean up, or (historically) an Instagram-scraped bare
    # string the frontend can't render, see media_url's docstring — isn't a
    # real photo or video and shouldn't eat into the cap it can't be seen
    # occupying. Mirrors the frontend's own usableMedia() filter.
    return sum(
        1 for m in (service.media or [])
        if media_url(m) and (m.get("type") if isinstance(m, dict) else "image") == media_type
    )


async def _read_capped(file: UploadFile, max_bytes: int, what: str) -> bytes:
    """Read a file in chunks, aborting as soon as it's clearly over the cap
    instead of buffering an oversized file fully before checking — the video
    cap is 10x the old photo one, so a client sending something huge
    shouldn't get to push the whole thing into memory first."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"{what} exceeds the {max_bytes // (1024 * 1024)} MB limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


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
    try:
        service = db.query(ServiceModel).filter(ServiceModel.service_id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="Service not found")
        _require_owning_vendor(service, current_user.user_id, db)

        existing_count = _count_media(service, "image")
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
        uploaded: list[dict] = []
        try:
            for i, (data, content_type) in enumerate(uploads):
                url = upload_service_image(
                    service_id=service_id,
                    image_index=existing_count + i,
                    file_bytes=data,
                    content_type=content_type,
                )
                uploaded.append({"url": url, "type": "image", "thumbnail_url": None})
        except StorageError:
            for item in uploaded:
                delete_service_image(item["url"])
            raise

        # All uploads succeeded — commit to DB in one shot
        service.media = list(service.media or []) + uploaded
        db.commit()
        db.refresh(service)
        return {"media": service.media}

    except StorageError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post(
    "/{service_id}/videos",
    summary="Upload one or more videos to a service",
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
async def upload_service_video_route(
    service_id: str,
    files: List[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload one or more videos and append them to the service's media list. Vendors only.

    Each video is transcoded into nothing — stored as uploaded — but does get
    a server-generated poster frame and a server-side duration check, since
    both need ffmpeg and the client can't be trusted to have applied either.
    """
    try:
        service = db.query(ServiceModel).filter(ServiceModel.service_id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="Service not found")
        _require_owning_vendor(service, current_user.user_id, db)

        existing_count = _count_media(service, "video")
        if existing_count + len(files) > MAX_VIDEOS_PER_SERVICE:
            raise HTTPException(
                status_code=400,
                detail=f"A service may have at most {MAX_VIDEOS_PER_SERVICE} videos. "
                       f"This service already has {existing_count}.",
            )

        uploads: list[tuple[bytes, str]] = []
        total_bytes = 0
        for file in files:
            data = await _read_capped(file, MAX_VIDEO_TOTAL_UPLOAD_BYTES, "A video")
            total_bytes += len(data)
            if total_bytes > MAX_VIDEO_TOTAL_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Total upload size exceeds {MAX_VIDEO_TOTAL_UPLOAD_BYTES // (1024 * 1024)} MB",
                )
            uploads.append((data, file.content_type or ""))

        uploaded: list[dict] = []
        try:
            for i, (data, content_type) in enumerate(uploads):
                # ffmpeg/ffprobe are blocking subprocess calls — off the event
                # loop so one big video doesn't stall every other request.
                video_url, thumbnail_url = await asyncio.to_thread(
                    upload_service_video,
                    service_id=service_id,
                    video_index=existing_count + i,
                    file_bytes=data,
                    content_type=content_type,
                )
                uploaded.append({"url": video_url, "type": "video", "thumbnail_url": thumbnail_url})
        except StorageError:
            for item in uploaded:
                delete_service_video(item["url"])
                if item.get("thumbnail_url"):
                    delete_service_image(item["thumbnail_url"])
            raise

        service.media = list(service.media or []) + uploaded
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


@router.delete("/{service_id}/videos", summary="Remove a video from a service", status_code=200)
def delete_service_video_route(
    service_id: str,
    video_url: str = Query(..., description="The exact URL of the video to remove"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a video URL from the service and delete it (and its thumbnail) from storage. Vendors only."""
    try:
        result, thumbnail_url = remove_service_video(
            user_id=current_user.user_id,
            service_id=service_id,
            video_url=video_url,
            db=db,
        )
        delete_service_video(video_url)
        if thumbnail_url:
            delete_service_image(thumbnail_url)
        return result
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("", summary="List services")
def list_services_route(
    vendor_id: Optional[str] = Query(None, description="Filter by vendor ID"),
    category: Optional[str] = Query(None, description="Filter by service category (e.g. a bundle slot's category)"),
    subcategory: Optional[str] = Query(None, description="Filter by service subcategory (e.g. dj vs dhol)"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    include_unlisted: bool = Query(
        False, description="Also return hidden/archived packages — only honoured for the vendor's own list",
    ),
    current_user: Optional[User] = Depends(get_optional_user),
    db: Session = Depends(get_db),
):
    """Return a list of services (with vendor info), filterable by vendor_id
    and/or category + subcategory. No auth required; only active packages
    unless the signed-in owner asks for their own with include_unlisted."""
    owner = False
    if include_unlisted and vendor_id and current_user:
        vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
        owner = bool(vendor and vendor.user_id == current_user.user_id)
    return list_services(
        vendor_id=vendor_id, category=category, subcategory=subcategory,
        limit=limit, offset=offset, include_unlisted=owner, db=db,
    )
