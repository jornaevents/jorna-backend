#!/usr/bin/env python3
"""
Instagram Vendor Scraper for NJ/NYC South Asian Service Providers
Uses Apify's instagram-scraper actor to collect public profile data.
Output is designed to feed directly into the Desiconnect bundle creator.
"""

import json
import os
import re
import sys
import time
from typing import Optional, List, Dict, Any
from apify_client import ApifyClient


INSTAGRAM_USERNAMES = [
    # DJs & Entertainment
    "djsuhel",
    "eliteentertainmentnj",
    "litdjentertainment",
    "dnseventsolutions",
    "getdjohm",
    "djshilpa",
    # Dhol Players
    "dholi.jazz",
    "paulsinghsidhu",
    "jshah104",
    "dholi_meher",
    # Event Planning & Multiservice
    "tumhihoevents",
    "events_by_heena",
    "partyshartyplanners",
]

# Keywords for category classification — keys match DB VendorCategory enum values
CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "dj":          ["dj", "djing", "music", "beats", "mixing", "turntable"],
    "catering":    ["catering", "chef", "food", "cuisine", "restaurant", "tandoor", "chaat"],
    "mehndi":      ["mehndi", "henna", "mehendi", "bridal henna"],
    "dhol":        ["dhol", "dholi", "drummer", "tabla", "percussion"],
    "decoration":  ["decorator", "decor", "decoration", "floral", "event design", "mandap"],
    "venue":       ["venue", "banquet", "hall", "ballroom", "wedding venue"],
    "photography": ["photographer", "photography", "videographer", "videography", "cinematography"],
    "planning":    ["planner", "planning", "coordinator", "event management"],
    "mua":         ["makeup", "mua", "beauty", "bridal makeup"],
    "other":       ["singer", "vocalist", "dancer", "dancing", "bhangra crew"],
}

# Tags that overlap with _STYLE_TAG_KEYWORDS in chatbot_service.py.
# Vendors tagged with these will score higher in bundle matching when
# users select matching style/preference buttons.
BUNDLE_SCORING_TAGS = {
    # Style
    "elegant", "luxury", "premium", "traditional", "modern", "fusion",
    "cultural", "heritage", "contemporary", "vibrant", "energetic",
    # Cultural (maps to pref_cultural preference)
    "bhangra", "bollywood", "desi", "punjabi", "south asian", "gujarati",
    "sangeet", "baraat", "garba", "navratri",
    # Event types
    "wedding", "engagement", "reception", "bridal", "anniversary",
    # Quality
    "professional", "experienced",
    # Location
    "nj", "nyc", "new jersey", "new york", "tristate",
}


class InstagramVendorScraper:

    def __init__(self):
        self.api_token = os.getenv("APIFY_API_TOKEN")
        if not self.api_token:
            raise ValueError("APIFY_API_TOKEN environment variable not set.")
        self.client = ApifyClient(self.api_token)
        self.vendors: List[Dict] = []
        self.failed_usernames: List[str] = []

    def scrape_profile(self, username: str) -> Optional[Dict[str, Any]]:
        print(f"  Scraping {username}...")
        try:
            run_input = {
                "directUrls": [f"https://www.instagram.com/{username}/"],
                "resultsType": "posts",
                "resultsLimit": 20,
                "addParentData": True,
            }
            run = self.client.actor("apify/instagram-scraper").call(run_input=run_input)
            if run["status"] == "SUCCEEDED":
                items = list(self.client.dataset(run["defaultDatasetId"]).iterate_items())
                if items:
                    profile_data = items[0].copy()
                    profile_data["posts"] = [
                        i for i in items if i.get("caption") is not None or i.get("id")
                    ]
                    return profile_data
            return None
        except Exception as e:
            print(f"    x Failed: {e}")
            return None

    @staticmethod
    def classify_vendor(username: str, bio: str, name: str) -> str:
        """Return a DB-compatible VendorCategory value."""
        text = f"{username} {bio} {name}".lower()
        scores = {
            cat: sum(text.count(kw) for kw in keywords)
            for cat, keywords in CATEGORY_KEYWORDS.items()
        }
        best = max(scores, key=scores.get)
        return best if scores[best] > 0 else "other"

    @staticmethod
    def extract_images(profile_data: Dict) -> List[str]:
        posts = (
            profile_data.get("posts", [])
            or profile_data.get("latestPosts", [])
            or []
        )
        images = []
        for post in posts[:9]:
            url = (
                post.get("imgDisplayUrl")
                or post.get("displayUrl")
                or post.get("imageUrl")
                or post.get("src")
            )
            if url and isinstance(url, str):
                images.append(url)
        return images

    @staticmethod
    def extract_tags(profile_data: Dict, bio: str, username: str) -> List[str]:
        """
        Extract normalized tags for bundle creator scoring.

        Pulls hashtags from captions + scans bio/username for keywords that
        match BUNDLE_SCORING_TAGS. These become vendor.tags in the DB and
        directly influence which vendors the chatbot selects when users
        pick style/preference buttons (e.g. 'Traditional', 'pref_cultural').
        """
        tag_set: set = set()
        posts = (
            profile_data.get("posts", [])
            or profile_data.get("latestPosts", [])
            or []
        )

        for post in posts:
            caption = post.get("caption") or post.get("text") or ""
            for word in caption.split():
                if word.startswith("#"):
                    tag = re.sub(r"[^a-z0-9 ]", "", word[1:].lower()).strip()
                    if tag:
                        tag_set.add(tag)

        # Add bundle scoring tags found in bio or username
        combined = f"{bio} {username}".lower()
        for keyword in BUNDLE_SCORING_TAGS:
            if keyword in combined:
                tag_set.add(keyword)

        # Keep only meaningful tags (not pure numbers, reasonable length)
        filtered = {
            t for t in tag_set
            if t in BUNDLE_SCORING_TAGS or (3 <= len(t) <= 30 and not t.isdigit())
        }
        return sorted(filtered)[:20]

    @staticmethod
    def extract_top_posts(profile_data: Dict) -> List[Dict[str, Any]]:
        posts = (
            profile_data.get("posts", [])
            or profile_data.get("latestPosts", [])
            or []
        )
        ranked = sorted(
            posts,
            key=lambda p: p.get("likeCount") or p.get("likesCount") or 0,
            reverse=True,
        )
        result = []
        for post in ranked[:3]:
            image = (
                post.get("imgDisplayUrl") or post.get("displayUrl")
                or post.get("imageUrl") or post.get("src") or ""
            )
            caption = post.get("caption") or post.get("text") or ""
            result.append({
                "image": image,
                "likes": post.get("likeCount") or post.get("likesCount") or 0,
                "caption": caption[:200],
                "timestamp": post.get("timestamp") or post.get("date") or "",
            })
        return result

    def normalize_vendor(self, username: str, raw_data: Dict) -> Dict[str, Any]:
        bio = raw_data.get("biography") or raw_data.get("bio") or ""
        name = (
            raw_data.get("fullName") or raw_data.get("full_name")
            or raw_data.get("name") or username
        )
        followers = raw_data.get("followersCount") or raw_data.get("followers_count") or 0
        profile_pic = raw_data.get("profilePictureUrl") or raw_data.get("profile_pic_url") or ""
        website = raw_data.get("website") or raw_data.get("external_url") or ""

        return {
            "business_name": name,
            "instagram_username": username,
            # DB-compatible category (dj, dhol, mehndi, catering, decoration, venue, etc.)
            "category": self.classify_vendor(username, bio, name),
            "location": {"city": "New Jersey", "state": "NJ", "country": "USA"},
            "bio": bio,
            "profile_picture": profile_pic,
            "images": self.extract_images(raw_data),
            # Normalized tags that feed directly into bundle creator scoring
            "tags": self.extract_tags(raw_data, bio, username),
            "top_posts": self.extract_top_posts(raw_data),
            "website": website,
            "followers": int(followers) if followers else 0,
            "claimed": False,
            "status": "draft",
            "data_sources": ["instagram"],
        }

    def scrape_all(self) -> None:
        print(f"\nStarting scrape of {len(INSTAGRAM_USERNAMES)} profiles...\n")
        for idx, username in enumerate(INSTAGRAM_USERNAMES, 1):
            print(f"[{idx}/{len(INSTAGRAM_USERNAMES)}] {username}")
            raw = self.scrape_profile(username)
            if raw:
                vendor = self.normalize_vendor(username, raw)
                self.vendors.append(vendor)
                print(f"    OK  category={vendor['category']}  tags={len(vendor['tags'])}")
            else:
                self.failed_usernames.append(username)
                print(f"    SKIP")
            if idx < len(INSTAGRAM_USERNAMES):
                time.sleep(1)
        self._print_summary()

    def _print_summary(self) -> None:
        print(f"\n{'='*60}")
        print(f"Scraped: {len(self.vendors)}  Failed: {len(self.failed_usernames)}")
        if self.failed_usernames:
            for u in self.failed_usernames:
                print(f"  - {u}")
        print(f"{'='*60}\n")

    def save_vendors(self, filename: str = "vendors.json") -> None:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(self.vendors, f, indent=2, ensure_ascii=False)
        print(f"Saved {len(self.vendors)} vendors to {filename}")

    def get_category_summary(self) -> Dict[str, int]:
        summary: Dict[str, int] = {}
        for v in self.vendors:
            cat = v["category"]
            summary[cat] = summary.get(cat, 0) + 1
        return summary


def main() -> int:
    try:
        scraper = InstagramVendorScraper()
        scraper.scrape_all()
        scraper.save_vendors()
        print("\nCategory breakdown:")
        for cat, count in sorted(scraper.get_category_summary().items(), key=lambda x: -x[1]):
            print(f"  {cat}: {count}")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted")
        return 1
    except Exception as e:
        print(f"\nFatal error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
