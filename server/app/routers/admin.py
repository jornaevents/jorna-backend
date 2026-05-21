"""Admin-only endpoints for user management and scraper control."""

import logging
import os
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User, Vendor, Service
from app.dependencies import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# Tags that align with bundle scoring keywords
_BUNDLE_SCORING_TAGS = {
    "elegant", "luxury", "premium", "traditional", "modern", "fusion",
    "cultural", "heritage", "contemporary", "vibrant", "energetic",
    "bhangra", "bollywood", "desi", "punjabi", "south asian", "gujarati",
    "sangeet", "baraat", "garba", "navratri",
    "wedding", "engagement", "reception", "bridal", "anniversary",
    "professional", "experienced",
    "nj", "nyc", "new jersey", "new york", "tristate",
}


def _scrape_profile(apify_client, username: str):
    try:
        run = apify_client.actor("apify/instagram-scraper").call(run_input={
            "directUrls": [f"https://www.instagram.com/{username}/"],
            "resultsType": "posts",
            "resultsLimit": 20,
            "addParentData": True,
        })
        if run["status"] == "SUCCEEDED":
            items = list(apify_client.dataset(run["defaultDatasetId"]).iterate_items())
            if items:
                profile = items[0].copy()
                profile["posts"] = [i for i in items if i.get("caption") is not None or i.get("id")]
                return profile
        return None
    except Exception as exc:
        logger.warning("Apify scrape failed for @%s: %s", username, exc)
        return None


def _extract_images(profile: dict) -> list[str]:
    posts = profile.get("posts", []) or profile.get("latestPosts", []) or []
    images = []
    for post in posts[:9]:
        url = (post.get("imgDisplayUrl") or post.get("displayUrl")
               or post.get("imageUrl") or post.get("src"))
        if url and isinstance(url, str):
            images.append(url)
    return images


def _extract_tags(profile: dict, bio: str, username: str) -> list[str]:
    tag_set: set = set()
    posts = profile.get("posts", []) or profile.get("latestPosts", []) or []
    for post in posts:
        caption = post.get("caption") or post.get("text") or ""
        for word in caption.split():
            if word.startswith("#"):
                tag = re.sub(r"[^a-z0-9 ]", "", word[1:].lower()).strip()
                if tag:
                    tag_set.add(tag)
    combined = f"{bio} {username}".lower()
    for keyword in _BUNDLE_SCORING_TAGS:
        if keyword in combined:
            tag_set.add(keyword)
    filtered = {t for t in tag_set if t in _BUNDLE_SCORING_TAGS or (3 <= len(t) <= 30 and not t.isdigit())}
    return sorted(filtered)[:20]


@router.post("/users/{user_id}/make-admin", summary="Promote a user to admin")
def make_admin(
    user_id: str,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Grant admin privileges to a user. Requires admin auth."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.is_admin:
        return {"message": f"{user.email} is already an admin"}
    user.is_admin = True
    db.commit()
    return {"message": f"{user.email} has been promoted to admin", "user_id": user.user_id}


@router.post("/users/{user_id}/revoke-admin", summary="Revoke admin privileges from a user")
def revoke_admin(
    user_id: str,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Revoke admin privileges from a user. Admins cannot revoke their own access."""
    if user_id == current_admin.user_id:
        raise HTTPException(status_code=400, detail="You cannot revoke your own admin access")
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.is_admin:
        return {"message": f"{user.email} is not an admin"}
    user.is_admin = False
    db.commit()
    return {"message": f"{user.email} has had admin access revoked", "user_id": user.user_id}


@router.get("/users", summary="List all admin users")
def list_admins(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Return all users with admin privileges."""
    admins = db.query(User).filter(User.is_admin == True).all()
    return [{"user_id": u.user_id, "email": u.email, "f_name": u.f_name, "l_name": u.l_name} for u in admins]


@router.post("/scraper/run", summary="Run Instagram enrichment scraper (admin only)")
def run_scraper(
    dry_run: bool = Query(False, description="Preview without writing to the database"),
    delay: float = Query(1.0, description="Seconds to wait between vendor scrapes"),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Scrape Instagram profiles for all vendors who have linked their account
    and enrich their profiles with tags and images.

    Requires APIFY_API_TOKEN to be set as an environment variable.
    Use dry_run=true to preview results without writing anything.
    """
    apify_token = os.getenv("APIFY_API_TOKEN")
    if not apify_token:
        raise HTTPException(status_code=400, detail="APIFY_API_TOKEN is not configured on the server")

    try:
        from apify_client import ApifyClient
    except ImportError:
        raise HTTPException(status_code=500, detail="apify_client package is not installed on the server")

    rows = (
        db.query(Vendor, User)
        .join(User, Vendor.user_id == User.user_id)
        .filter(Vendor.instagram_username.isnot(None))
        .all()
    )

    if not rows:
        return {"message": "No vendors have linked their Instagram account.", "results": []}

    apify_client = ApifyClient(apify_token)
    results = []

    for i, (vendor, user) in enumerate(rows):
        username = vendor.instagram_username
        entry = {
            "vendor_id": vendor.vendor_id,
            "username": username,
            "name": f"{user.f_name} {user.l_name}",
        }

        profile = _scrape_profile(apify_client, username)
        if not profile:
            entry["status"] = "failed"
            entry["tags"] = []
            entry["images"] = 0
            results.append(entry)
            continue

        bio = profile.get("biography") or profile.get("bio") or ""
        images = _extract_images(profile)
        tags = _extract_tags(profile, bio, username)

        entry["tags"] = tags
        entry["images"] = len(images)

        if dry_run:
            entry["status"] = "dry_run"
        else:
            vendor.instagram_tags = tags
            if bio and (not vendor.bio or not vendor.bio.strip()):
                vendor.bio = bio[:500]
            db.commit()

            service = db.query(Service).filter(Service.vendor_id == vendor.vendor_id).first()
            if service and images:
                existing = list(service.media or [])
                new_images = [img for img in images if img not in existing]
                service.media = (existing + new_images)[:9]
                db.commit()

            entry["status"] = "enriched"

        results.append(entry)

        if i < len(rows) - 1:
            time.sleep(delay)

    enriched = sum(1 for r in results if r["status"] == "enriched")
    failed = sum(1 for r in results if r["status"] == "failed")

    return {
        "dry_run": dry_run,
        "total": len(results),
        "enriched": enriched,
        "failed": failed,
        "results": results,
    }
