"""Supabase Storage helpers for user avatar and service media uploads."""

import os
import mimetypes
import subprocess
import tempfile
from typing import Optional

import httpx

AVATAR_BUCKET = "avatars"
SERVICE_IMAGE_BUCKET = "service-images"
# Video files live in their own bucket rather than service-images: they're
# fetched and billed differently (range-request streaming vs. a single GET),
# and keeping them separate means a runaway video upload can't be mistaken
# for one of a service's photos by anything that just lists the bucket.
SERVICE_VIDEO_BUCKET = "service-videos"
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}

MAX_VIDEO_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_VIDEO_DURATION_SECONDS = 30  # 30 seconds
ALLOWED_VIDEO_MIME_TYPES = {"video/mp4", "video/quicktime", "video/webm"}
# How long ffmpeg/ffprobe get before we give up on a file — a corrupt or
# adversarial upload shouldn't be able to tie up a worker indefinitely.
_FFMPEG_TIMEOUT_SECONDS = 30


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


def upload_service_image(*, service_id: str, image_index: "int | str", file_bytes: bytes, content_type: str) -> str:
    """Upload a service image to Supabase Storage and return the public URL.

    image_index is usually the photo's position, but a video's thumbnail also
    goes through here with a string suffix (e.g. "2-video-thumb") so it lands
    beside the service's photos under a name that can't collide with one.
    """
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


def _probe_duration_seconds(video_path: str) -> float:
    """Read a video's duration via ffprobe without decoding the whole stream.

    The frontend already caps recording/selection length before upload, but
    that's a courtesy, not a control — a client can send anything, so the
    server re-checks before it commits to processing or storing the file.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_SECONDS,
        )
        return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        raise StorageError(400, "Couldn't read that video file — it may be corrupt or an unsupported format.") from e


def _extract_thumbnail_bytes(video_path: str, duration_seconds: float) -> bytes:
    """Grab a single poster frame partway into the clip via ffmpeg.

    Seeking to a fixed 1s fails on anything shorter, so the seek point scales
    with the clip instead — halfway in, capped at 1s for longer videos, which
    avoids both a black opening frame and seeking past a short clip's end.
    """
    seek = min(1.0, duration_seconds / 2)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as thumb_file:
        thumb_path = thumb_file.name
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", f"{seek:.3f}",
                "-i", video_path,
                "-frames:v", "1",
                "-q:v", "3",
                "-f", "image2",
                thumb_path,
            ],
            capture_output=True, timeout=_FFMPEG_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise StorageError(400, "Couldn't generate a thumbnail for that video.")
        with open(thumb_path, "rb") as f:
            return f.read()
    except subprocess.TimeoutExpired as e:
        raise StorageError(400, "That video took too long to process.") from e
    finally:
        try:
            os.unlink(thumb_path)
        except OSError:
            pass


def upload_service_video(
    *, service_id: str, video_index: int, file_bytes: bytes, content_type: str,
) -> tuple[str, str]:
    """Upload a service video to Supabase Storage and return (video_url, thumbnail_url).

    The thumbnail is a jpeg frame pulled from the video with ffmpeg, stored
    alongside the service's photos in SERVICE_IMAGE_BUCKET — it's a still
    image like any other, so it doesn't need a bucket of its own.
    """
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not supabase_url:
        raise StorageError(500, "Server is not configured for file uploads (missing SUPABASE_URL)")

    if content_type not in ALLOWED_VIDEO_MIME_TYPES:
        raise StorageError(400, "File type not allowed. Accepted: mp4, mov, webm")

    if len(file_bytes) > MAX_VIDEO_FILE_SIZE:
        raise StorageError(400, f"File too large. Maximum size is {MAX_VIDEO_FILE_SIZE // (1024 * 1024)} MB")

    ext = mimetypes.guess_extension(content_type) or ".mp4"
    if content_type == "video/quicktime":
        ext = ".mov"  # mimetypes maps this to .qt on some platforms

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as video_file:
        video_file.write(file_bytes)
        video_path = video_file.name

    try:
        duration = _probe_duration_seconds(video_path)
        if duration > MAX_VIDEO_DURATION_SECONDS:
            raise StorageError(
                400,
                f"Video is too long ({duration:.0f}s). Maximum is {MAX_VIDEO_DURATION_SECONDS}s.",
            )
        thumbnail_bytes = _extract_thumbnail_bytes(video_path, duration)
    finally:
        try:
            os.unlink(video_path)
        except OSError:
            pass

    object_path = f"{service_id}/{video_index}{ext}"
    url = f"{supabase_url}/storage/v1/object/{SERVICE_VIDEO_BUCKET}/{object_path}"
    headers = _storage_headers()
    headers["Content-Type"] = content_type
    headers["x-upsert"] = "true"
    response = httpx.put(url, content=file_bytes, headers=headers, timeout=60)
    if response.status_code not in (200, 201):
        raise StorageError(502, f"Storage upload failed: {response.text}")
    video_url = f"{supabase_url}/storage/v1/object/public/{SERVICE_VIDEO_BUCKET}/{object_path}"

    thumbnail_url = upload_service_image(
        service_id=service_id,
        image_index=f"{video_index}-video-thumb",
        file_bytes=thumbnail_bytes,
        content_type="image/jpeg",
    )

    return video_url, thumbnail_url


def delete_service_video(video_url: str) -> None:
    """Delete a service video from Supabase Storage. Silently ignores non-storage URLs."""
    if not video_url or "/storage/v1/object/public/" not in video_url:
        return
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not supabase_url or not os.environ.get("SUPABASE_SERVICE_KEY", ""):
        return
    try:
        marker = f"/storage/v1/object/public/{SERVICE_VIDEO_BUCKET}/"
        idx = video_url.find(marker)
        if idx == -1:
            return
        object_path = video_url[idx + len(marker):]
        url = f"{supabase_url}/storage/v1/object/{SERVICE_VIDEO_BUCKET}/{object_path}"
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
