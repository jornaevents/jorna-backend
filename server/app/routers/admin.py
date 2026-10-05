"""Admin-only endpoints for user management and scraper control."""

import hmac
import logging
import os
import re
import threading
import time

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db.database import SessionLocal, get_db
from app.db.models import User, Vendor
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


def _scrape_profile(apify_token: str, username: str) -> tuple[dict | None, str | None]:
    """Scrape an Instagram profile via the Apify HTTP API directly.

    Uses run-sync-get-dataset-items to avoid the apify_client Pydantic
    validation bug on ActorResponse pricing fields.
    Returns (profile, error_message). error_message is None on success.
    """
    import httpx

    url = (
        "https://api.apify.com/v2/acts/apify~instagram-scraper"
        "/run-sync-get-dataset-items"
        f"?token={apify_token}&timeout=120&memory=256"
    )
    payload = {
        "directUrls": [f"https://www.instagram.com/{username}/"],
        "resultsType": "posts",
        "resultsLimit": 20,
        "addParentData": True,
    }
    try:
        resp = httpx.post(url, json=payload, timeout=150.0)
        if resp.status_code != 201:
            return None, f"Apify returned HTTP {resp.status_code}: {resp.text[:200]}"
        items = resp.json()
        if not items:
            return None, "Apify returned no items — account may be private or empty"
        profile = items[0].copy()
        profile["posts"] = [i for i in items if i.get("caption") is not None or i.get("id")]
        return profile, None
    except Exception as exc:
        logger.warning("Apify scrape failed for @%s: %s", username, exc)
        return None, str(exc)


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


@router.get("/reports", summary="List content reports (admin only)")
def list_reports(
    status: str | None = Query(None, description="Filter: open | reviewed | dismissed"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    from app.db.models import ContentReport
    q = db.query(ContentReport)
    if status:
        q = q.filter(ContentReport.status == status)
    rows = q.order_by(ContentReport.created_at.desc()).offset(offset).limit(limit).all()
    return [
        {
            "report_id": r.report_id,
            "reporter_user_id": r.reporter_user_id,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "reason": r.reason,
            "details": r.details,
            "status": r.status,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.patch("/reports/{report_id}", summary="Update a report's status (admin only)")
def update_report_status(
    report_id: str,
    status: str = Query(..., description="open | reviewed | dismissed"),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    from app.db.models import ContentReport
    if status not in {"open", "reviewed", "dismissed"}:
        raise HTTPException(status_code=400, detail="status must be open, reviewed, or dismissed")
    report = db.query(ContentReport).filter(ContentReport.report_id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    report.status = status
    db.commit()
    return {"message": "Updated", "report_id": report_id, "status": status}


@router.post("/cleanup/bundle-event-data", summary="One-off cleanup of legacy bundle/event data (admin only)")
def cleanup_bundle_event_data(
    dry_run: bool = Query(True, description="Preview what would be deleted without modifying anything"),
    stale_days: int = Query(7, ge=1, le=365, description="Draft comparison bundles older than this many days are considered abandoned"),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Remove data artifacts from before the 2026-07 bundle→event linkage fix:
    orphan AI-created events, abandoned comparison-draft bundles, and duplicate
    bookings. Run with dry_run=true first and review the report; paid bookings
    are never touched either way."""
    from app.services.bundle_service import cleanup_legacy_bundle_event_data
    return cleanup_legacy_bundle_event_data(db=db, dry_run=dry_run, stale_days=stale_days)


def _require_scraper_auth(
    x_scraper_key: str | None = Header(None, alias="X-Scraper-Key", description="SCRAPER_API_KEY value for cron job access"),
    credentials=Depends(__import__("fastapi.security", fromlist=["HTTPBearer"]).HTTPBearer(auto_error=False)),
    db: Session = Depends(get_db),
):
    """Accept the SCRAPER_API_KEY in the X-Scraper-Key header, or an admin JWT.

    Header only: the old ?api_key= query param put the key in every access
    log and in the cron service's run history, so it's no longer accepted."""
    scraper_key = os.getenv("SCRAPER_API_KEY")
    if x_scraper_key and scraper_key and hmac.compare_digest(x_scraper_key, scraper_key):
        return  # authenticated via static API key

    # Try JWT admin auth
    if credentials:
        import jwt
        from app.config import ALGORITHM, SECRET_KEY
        try:
            payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
            user = db.query(User).filter(User.user_id == payload.get("sub")).first()
            if user and user.is_admin and payload.get("tv") == user.token_version:
                return  # authenticated via admin JWT
        except Exception:
            pass

    raise HTTPException(status_code=401, detail="Provide a valid X-Scraper-Key header or an admin Bearer token")


# One scrape at a time per process: each one is a paid Apify run per linked
# vendor, and a second cron call (or an admin's manual one) while the weekly
# run is still going would pay for every vendor twice.
_scraper_lock = threading.Lock()

# The background run can't use the request's session — it's closed as soon as
# the response goes out — so it opens its own. A module attribute so tests can
# point it at their database.
_session_factory = SessionLocal


def _linked_vendors(db: Session) -> list:
    return (
        db.query(Vendor, User)
        .join(User, Vendor.user_id == User.user_id)
        .filter(Vendor.instagram_username.isnot(None))
        .all()
    )


def _scrape_all(db: Session, apify_token: str, *, dry_run: bool, delay: float) -> dict:
    """Scrape every linked vendor and (unless dry_run) write tags and an empty
    bio back — never photos. Takes up to ~2.5 minutes per vendor, all Apify."""
    rows = _linked_vendors(db)
    results = []

    for i, (vendor, user) in enumerate(rows):
        username = vendor.instagram_username
        entry = {
            "vendor_id": vendor.vendor_id,
            "username": username,
            "name": f"{user.f_name} {user.l_name}",
        }

        profile, error = _scrape_profile(apify_token, username)
        if not profile:
            entry["status"] = "failed"
            entry["error"] = error
            entry["tags"] = []
            results.append(entry)
            continue

        bio = profile.get("biography") or profile.get("bio") or ""
        tags = _extract_tags(profile, bio, username)

        entry["tags"] = tags

        if dry_run:
            entry["status"] = "dry_run"
        else:
            vendor.instagram_tags = tags
            if bio and (not vendor.bio or not vendor.bio.strip()):
                vendor.bio = bio[:500]
            # Tags and an empty bio only. Packages are left alone: it used to
            # add post images to the vendor's first package, which the vendor
            # never chose, and Instagram's CDN URLs expire within days, so
            # they turned into broken images.
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


def _scrape_all_in_background(apify_token: str, dry_run: bool, delay: float) -> None:
    """The scheduled run. Its outcome only reaches the logs — the caller got
    its 202 long ago — so it logs a summary line either way, and the reason
    for each vendor that failed."""
    try:
        with _session_factory() as db:
            summary = _scrape_all(db, apify_token, dry_run=dry_run, delay=delay)
        for r in summary["results"]:
            if r["status"] == "failed":
                logger.warning("Instagram scraper: @%s failed: %s", r["username"], r.get("error"))
        logger.info(
            "Instagram scraper finished: %d vendors, %d enriched, %d failed%s",
            summary["total"], summary["enriched"], summary["failed"],
            " (dry run)" if dry_run else "",
        )
    except Exception:
        logger.exception("Instagram scraper run crashed")
    finally:
        _scraper_lock.release()


@router.post("/scraper/run", summary="Run Instagram enrichment scraper (admin only)")
def run_scraper(
    background_tasks: BackgroundTasks,
    response: Response,
    dry_run: bool = Query(False, description="Preview without writing to the database"),
    delay: float = Query(1.0, description="Seconds to wait between vendor scrapes"),
    wait: bool = Query(False, description="Run inline and return per-vendor results instead of starting it in the background"),
    db: Session = Depends(get_db),
    _auth=Depends(_require_scraper_auth),
):
    """Scrape Instagram profiles for all vendors who have linked their account
    and enrich their profiles with tags (and a bio, if they have none). Never
    touches their packages or photos.

    Authenticate with either a Bearer admin JWT or the SCRAPER_API_KEY value
    in the X-Scraper-Key header (for cron jobs). A key in the query string is
    rejected.

    By default this starts the run in the background and answers 202 straight
    away: each vendor is a synchronous Apify call of up to ~2.5 minutes, so
    the weekly cron job timed out waiting for the whole run. The outcome is
    logged ("Instagram scraper finished: …"). Pass wait=true to run inline and
    get the per-vendor results back instead. A run already in progress
    answers 409.

    Requires APIFY_API_TOKEN to be set as an environment variable.
    Use dry_run=true to preview results without writing anything.
    """
    apify_token = os.getenv("APIFY_API_TOKEN")
    if not apify_token:
        raise HTTPException(status_code=400, detail="APIFY_API_TOKEN is not configured on the server")

    # Counted before taking the lock, so nothing between acquiring it and
    # handing it to the background task can raise and leave it held.
    vendors = None if wait else len(_linked_vendors(db))

    if not _scraper_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A scraper run is already in progress")

    if wait:
        try:
            summary = _scrape_all(db, apify_token, dry_run=dry_run, delay=delay)
        finally:
            _scraper_lock.release()
        if not summary["results"]:
            return {"message": "No vendors have linked their Instagram account.", "results": []}
        return summary

    background_tasks.add_task(_scrape_all_in_background, apify_token, dry_run, delay)
    response.status_code = 202
    return {"started": True, "dry_run": dry_run, "vendors": vendors}
