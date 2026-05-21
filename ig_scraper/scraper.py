#!/usr/bin/env python3
"""
Instagram enrichment scraper for Desiconnect.

Fetches vendors who have linked their Instagram account from the backend,
scrapes their profiles via Apify, then posts enriched data (tags + images)
back to each vendor's existing profile.

Instagram-scraped tags are stored separately from user-inputted tags so
vendors retain full control over their curated tags.

Usage:
    export APIFY_API_TOKEN=your_apify_token
    export API_BASE_URL=https://your-railway-domain.railway.app
    export ADMIN_EMAIL=admin@example.com
    export ADMIN_PASSWORD=YourPassword1

    python scraper.py              # enrich all linked vendors
    python scraper.py --dry-run    # preview without posting back
"""

import argparse
import os
import re
import sys
import time
from typing import Optional, List, Dict, Any

import httpx
from apify_client import ApifyClient


API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
REQUEST_TIMEOUT = 30.0

# Tags that align with _STYLE_TAG_KEYWORDS in chatbot_service.py.
# Vendors tagged with these score higher when users select matching preferences.
BUNDLE_SCORING_TAGS = {
    "elegant", "luxury", "premium", "traditional", "modern", "fusion",
    "cultural", "heritage", "contemporary", "vibrant", "energetic",
    "bhangra", "bollywood", "desi", "punjabi", "south asian", "gujarati",
    "sangeet", "baraat", "garba", "navratri",
    "wedding", "engagement", "reception", "bridal", "anniversary",
    "professional", "experienced",
    "nj", "nyc", "new jersey", "new york", "tristate",
}


# ── API helpers ───────────────────────────────────────────────────────


def login(email: str, password: str) -> str:
    resp = httpx.post(
        f"{API_BASE_URL}/auth/login",
        json={"identifier": email, "password": password},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_instagram_linked_vendors(token: str) -> List[Dict]:
    resp = httpx.get(
        f"{API_BASE_URL}/vendors/instagram-linked",
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def post_enrichment(token: str, vendor_id: str, tags: List[str], images: List[str], bio: str) -> None:
    resp = httpx.post(
        f"{API_BASE_URL}/vendors/{vendor_id}/instagram-enrich",
        json={"tags": tags, "images": images, "bio": bio},
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()


# ── Scraping helpers ──────────────────────────────────────────────────


def scrape_profile(apify_client: ApifyClient, username: str) -> Optional[Dict[str, Any]]:
    try:
        run_input = {
            "directUrls": [f"https://www.instagram.com/{username}/"],
            "resultsType": "posts",
            "resultsLimit": 20,
            "addParentData": True,
        }
        run = apify_client.actor("apify/instagram-scraper").call(run_input=run_input)
        if run["status"] == "SUCCEEDED":
            items = list(apify_client.dataset(run["defaultDatasetId"]).iterate_items())
            if items:
                profile = items[0].copy()
                profile["posts"] = [i for i in items if i.get("caption") is not None or i.get("id")]
                return profile
        return None
    except Exception as e:
        print(f"    Scrape error: {e}")
        return None


def extract_images(profile: Dict) -> List[str]:
    posts = profile.get("posts", []) or profile.get("latestPosts", []) or []
    images = []
    for post in posts[:9]:
        url = (
            post.get("imgDisplayUrl") or post.get("displayUrl")
            or post.get("imageUrl") or post.get("src")
        )
        if url and isinstance(url, str):
            images.append(url)
    return images


def extract_tags(profile: Dict, bio: str, username: str) -> List[str]:
    """
    Extract tags that align with the bundle creator's scoring keywords.
    These are stored as instagram_tags (separate from user-inputted tags).
    """
    tag_set: set = set()
    posts = profile.get("posts", []) or profile.get("latestPosts", []) or []

    for post in posts:
        caption = post.get("caption") or post.get("text") or ""
        for word in caption.split():
            if word.startswith("#"):
                tag = re.sub(r"[^a-z0-9 ]", "", word[1:].lower()).strip()
                if tag:
                    tag_set.add(tag)

    # Scan bio and username for bundle scoring keywords
    combined = f"{bio} {username}".lower()
    for keyword in BUNDLE_SCORING_TAGS:
        if keyword in combined:
            tag_set.add(keyword)

    # Keep only meaningful tags
    filtered = {
        t for t in tag_set
        if t in BUNDLE_SCORING_TAGS or (3 <= len(t) <= 30 and not t.isdigit())
    }
    return sorted(filtered)[:20]


# ── Main ──────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich Desiconnect vendors from Instagram")
    parser.add_argument("--dry-run", action="store_true", help="Scrape but don't post back to the API")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between scrapes (default: 1.0)")
    args = parser.parse_args()

    apify_token = os.getenv("APIFY_API_TOKEN")
    if not apify_token:
        print("Error: APIFY_API_TOKEN not set")
        return 1

    admin_identifier = os.getenv("ADMIN_USERNAME") or os.getenv("ADMIN_EMAIL")
    admin_password = os.getenv("ADMIN_PASSWORD")
    if not admin_identifier or not admin_password:
        print("Error: ADMIN_USERNAME (or ADMIN_EMAIL) and ADMIN_PASSWORD must be set")
        return 1

    apify_client = ApifyClient(apify_token)

    print(f"Logging in to {API_BASE_URL}...")
    try:
        token = login(admin_identifier, admin_password)
        print("Logged in.")
    except Exception as e:
        print(f"Login failed: {e}")
        return 1

    try:
        vendors = get_instagram_linked_vendors(token)
    except Exception as e:
        print(f"Failed to fetch vendors: {e}")
        return 1

    if not vendors:
        print("No vendors have linked their Instagram account yet.")
        return 0

    print(f"\nFound {len(vendors)} vendor(s) with Instagram linked.\n")

    success = 0
    failed = 0

    for i, vendor in enumerate(vendors, 1):
        vendor_id = vendor["vendor_id"]
        username = vendor["instagram_username"]
        print(f"[{i}/{len(vendors)}] @{username}  ({vendor['f_name']} {vendor['l_name']})")

        profile = scrape_profile(apify_client, username)
        if not profile:
            print(f"    SKIP — could not scrape")
            failed += 1
            continue

        bio = profile.get("biography") or profile.get("bio") or ""
        images = extract_images(profile)
        tags = extract_tags(profile, bio, username)

        print(f"    tags={len(tags)}  images={len(images)}")

        if args.dry_run:
            print(f"    DRY RUN — would post {len(tags)} tags, {len(images)} images")
            success += 1
        else:
            try:
                post_enrichment(token, vendor_id, tags, images, bio)
                print(f"    Enriched.")
                success += 1
            except Exception as e:
                print(f"    Failed to post: {e}")
                failed += 1

        if i < len(vendors):
            time.sleep(args.delay)

    print(f"\n{'='*60}")
    print(f"Done.  Enriched: {success}  Failed: {failed}")
    print(f"{'='*60}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
