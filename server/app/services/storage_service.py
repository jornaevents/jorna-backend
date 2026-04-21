"""Supabase Storage helpers for user avatar uploads."""

import os
import mimetypes
from typing import Optional

import httpx

AVATAR_BUCKET = "avatars"
SERVICE_IMAGE_BUCKET = "service-images"
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}


class StorageError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _storage_headers() -> dict:
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not key:
        raise StorageError(500, "Server is not configured for file uploads (missing SUPABASE_SERVICE_KEY)")
    return {
        "Authorization": f"Bearer {key}",
        "apikey": key,
    }


def upload_avatar(*, user_id: str, file_bytes: bytes, content_type: str) -> str:
    """Upload avatar bytes to Supabase Storage and return the public URL."""
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not supabase_url:
        raise StorageError(500, "Server is not configured for file uploads (missing SUPABASE_URL)")

    if content_type not in ALLOWED_MIME_TYPES:
        raise StorageError(400, f"File type not allowed. Accepted: jpeg, png, webp")

    if len(file_bytes) > MAX_FILE_SIZE:
        raise StorageError(400, "File too large. Maximum size is 5 MB")

    ext = mimetypes.guess_extension(content_type) or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"
    object_path = f"{user_id}/avatar{ext}"

    url = f"{supabase_url}/storage/v1/object/{AVATAR_BUCKET}/{object_path}"
    headers = _storage_headers()
    headers["Content-Type"] = content_type
    headers["x-upsert"] = "true"  # overwrite on re-upload

    response = httpx.put(url, content=file_bytes, headers=headers, timeout=30)

    if response.status_code not in (200, 201):
        raise StorageError(502, f"Storage upload failed: {response.text}")

    return f"{supabase_url}/storage/v1/object/public/{AVATAR_BUCKET}/{object_path}"


def upload_service_image(*, service_id: str, image_index: int, file_bytes: bytes, content_type: str) -> str:
    """Upload a service image to Supabase Storage and return the public URL."""
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not supabase_url:
        raise StorageError(500, "Server is not configured for file uploads (missing SUPABASE_URL)")

    if content_type not in ALLOWED_MIME_TYPES:
        raise StorageError(400, "File type not allowed. Accepted: jpeg, png, webp")

    if len(file_bytes) > MAX_FILE_SIZE:
        raise StorageError(400, "File too large. Maximum size is 5 MB")

    ext = mimetypes.guess_extension(content_type) or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"
    object_path = f"{service_id}/{image_index}{ext}"

    url = f"{supabase_url}/storage/v1/object/{SERVICE_IMAGE_BUCKET}/{object_path}"
    headers = _storage_headers()
    headers["Content-Type"] = content_type
    headers["x-upsert"] = "true"

    response = httpx.put(url, content=file_bytes, headers=headers, timeout=30)
    if response.status_code not in (200, 201):
        raise StorageError(502, f"Storage upload failed: {response.text}")

    return f"{supabase_url}/storage/v1/object/public/{SERVICE_IMAGE_BUCKET}/{object_path}"


def delete_service_image(image_url: str) -> None:
    """Delete a service image from Supabase Storage. Silently ignores non-storage URLs."""
    if not image_url or "/storage/v1/object/public/" not in image_url:
        return
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not supabase_url or not os.environ.get("SUPABASE_SERVICE_KEY", ""):
        return
    try:
        marker = f"/storage/v1/object/public/{SERVICE_IMAGE_BUCKET}/"
        idx = image_url.find(marker)
        if idx == -1:
            return
        object_path = image_url[idx + len(marker):]
        url = f"{supabase_url}/storage/v1/object/{SERVICE_IMAGE_BUCKET}/{object_path}"
        httpx.delete(url, headers=_storage_headers(), timeout=10)
    except Exception:
        pass


def delete_avatar(pfp_url: Optional[str]) -> None:
    """Delete a previously uploaded avatar. Silently ignores non-storage URLs."""
    if not pfp_url or "/storage/v1/object/public/" not in pfp_url:
        return
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not supabase_url or not os.environ.get("SUPABASE_SERVICE_KEY", ""):
        return

    try:
        # Extract the object path after the bucket name
        marker = f"/storage/v1/object/public/{AVATAR_BUCKET}/"
        idx = pfp_url.find(marker)
        if idx == -1:
            return
        object_path = pfp_url[idx + len(marker):]

        url = f"{supabase_url}/storage/v1/object/{AVATAR_BUCKET}/{object_path}"
        httpx.delete(url, headers=_storage_headers(), timeout=10)
    except Exception:
        pass  # best-effort cleanup — don't fail the request if delete fails
