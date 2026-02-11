"""
Facebook scraper backend using the **facebook-scraper** library.

Why facebook-scraper?
─────────────────────
•  No API key required — scrapes public Facebook pages and posts directly.
•  Supports page search, post extraction, and metadata retrieval.
•  Lightweight and actively maintained.

Strategy
────────
1.  Search Facebook for pages matching vendor-related keywords.
2.  For each matching page, extract profile metadata (name, category,
    followers, about, website, etc.).
3.  Build ScrapedVendor records with relevance scoring.

Anti-blocking
─────────────
•  Randomised delays between requests (human-like cadence).
•  Cookies from a logged-in session can be provided for better access.
•  Configurable page cap per keyword to keep volume low.

Usage
─────
    from app.facebook_scraper.scraper import FacebookScraper

    scraper = FacebookScraper()
    scraper = FacebookScraper(cookies="path/to/cookies.txt")
    vendors = scraper.scrape_keywords(["Punjabi DJ", "Mehndi Artist"])
"""

from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Optional

import facebook_scraper as fb  # pip install facebook-scraper

from app.scraper_common.vendor_result import ScrapedVendor

logger = logging.getLogger(__name__)

# ── Tunables ─────────────────────────────────────────────────────────
DEFAULT_MAX_PAGES_PER_KEYWORD = 10    # max pages to inspect per keyword
DEFAULT_MAX_POSTS_PER_PAGE = 5        # posts to sample from each page
MIN_DELAY_SECONDS = 3.0               # minimum pause between requests
MAX_DELAY_SECONDS = 8.0               # maximum pause (FB is stricter)


class FacebookScraper:
    """Scrapes Facebook pages for potential vendor profiles."""

    def __init__(
        self,
        cookies: Optional[str] = None,
        *,
        max_pages_per_keyword: int = DEFAULT_MAX_PAGES_PER_KEYWORD,
        max_posts_per_page: int = DEFAULT_MAX_POSTS_PER_PAGE,
    ) -> None:
        self.max_pages_per_keyword = max_pages_per_keyword
        self.max_posts_per_page = max_posts_per_page

        # ── optional cookie-based auth ──
        if cookies:
            self._load_cookies(cookies)
        else:
            logger.warning(
                "Running without cookies – Facebook may limit or block "
                "unauthenticated requests.  Provide a cookies file for "
                "more reliable scraping."
            )

    # ──────────────────────────────────────────────────────────────────
    #  Cookie helpers
    # ──────────────────────────────────────────────────────────────────
    def _load_cookies(self, cookies_path: str) -> None:
        """Load cookies from a Netscape-format cookies.txt file."""
        path = Path(cookies_path)
        if not path.exists():
            logger.error("Cookies file not found: %s", path)
            raise FileNotFoundError(f"Cookies file not found: {path}")
        fb.set_cookies(str(path))
        logger.info("Loaded Facebook cookies from %s", path)

    # ──────────────────────────────────────────────────────────────────
    #  Core scraping
    # ──────────────────────────────────────────────────────────────────
    def _random_sleep(self) -> None:
        """Human-like delay between requests."""
        delay = random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
        logger.debug("Sleeping %.1fs …", delay)
        time.sleep(delay)

    def scrape_keywords(
        self,
        keywords: list[str],
        *,
        max_pages_per_keyword: int | None = None,
    ) -> list[ScrapedVendor]:
        """
        Search Facebook for pages matching each keyword, scrape their
        profile info, and collect unique vendor pages.

        Returns a list of `ScrapedVendor` sorted by relevance score
        (descending).
        """
        cap = max_pages_per_keyword or self.max_pages_per_keyword
        seen_page_ids: set[str] = set()
        vendors: dict[str, ScrapedVendor] = {}  # page_id -> vendor

        for keyword in keywords:
            logger.info("Searching Facebook for '%s' (max %d pages) …", keyword, cap)
            try:
                self._scrape_single_keyword(keyword, cap, seen_page_ids, vendors)
            except Exception:
                logger.exception("Error searching for '%s' – skipping.", keyword)

        result = sorted(vendors.values(), key=lambda v: v.relevance_score, reverse=True)
        logger.info("Scraping complete – %d unique vendors found.", len(result))
        return result

    def _scrape_single_keyword(
        self,
        keyword: str,
        cap: int,
        seen_page_ids: set[str],
        vendors: dict[str, ScrapedVendor],
    ) -> None:
        """Search for pages matching a keyword and extract vendor info."""
        try:
            # Get posts matching the keyword to discover pages
            posts = fb.get_posts(
                post_urls=None,
                group=None,
                pages=cap,
                options={"allow_extra_requests": False},
                extra_info=True,
            )
        except Exception:
            # Fallback: try to get posts by searching for pages directly
            logger.debug("Post search not available, trying page-based approach.")
            posts = []

        # Alternative approach: scrape known vendor-style pages
        # by searching for pages with the keyword in their name
        self._search_pages_by_keyword(keyword, cap, seen_page_ids, vendors)

    def _search_pages_by_keyword(
        self,
        keyword: str,
        cap: int,
        seen_page_ids: set[str],
        vendors: dict[str, ScrapedVendor],
    ) -> None:
        """
        Discover vendor pages by scraping posts from Facebook search results.

        facebook-scraper doesn't have a direct page search, so we scrape
        posts from the keyword and extract the poster's page info.
        """
        found = 0

        try:
            # Search by scraping posts that mention the keyword
            for post in fb.get_posts(keyword, pages=3, options={"allow_extra_requests": False}):
                if found >= cap:
                    break

                page_id = post.get("user_id") or post.get("username")
                if not page_id or str(page_id) in seen_page_ids:
                    continue

                seen_page_ids.add(str(page_id))
                self._random_sleep()

                # Try to get page info
                page_name = post.get("username", "")
                if not page_name:
                    continue

                try:
                    page_info = self._get_page_info(page_name)
                except Exception:
                    logger.debug("Could not fetch page info for %s – skipping.", page_name)
                    continue

                vendor = ScrapedVendor(
                    username=page_name,
                    full_name=page_info.get("name", page_name),
                    platform="facebook",
                    profile_url=f"https://www.facebook.com/{page_name}/",
                    bio=page_info.get("about"),
                    followers=page_info.get("followers"),
                    post_count=None,  # not easily available from FB
                    hashtags_matched=[keyword],
                    sample_post_urls=[post.get("post_url", "")] if post.get("post_url") else [],
                    profile_pic_url=page_info.get("profile_pic_url"),
                    is_business_account=True,  # FB pages are business by nature
                    external_url=page_info.get("website"),
                )

                vendors[page_name] = vendor
                found += 1
                logger.info(
                    "  ✓ %s  (%s followers)",
                    page_name,
                    page_info.get("followers", "?"),
                )

        except Exception:
            logger.exception("Error during keyword search for '%s'.", keyword)

    def _get_page_info(self, page_name: str) -> dict:
        """
        Fetch metadata for a Facebook page.

        Returns a dict with keys: name, about, followers, website,
        profile_pic_url, category.
        """
        info: dict = {}

        try:
            page_info = fb.get_page_info(page_name)
            info["name"] = page_info.get("name", page_name)
            info["about"] = page_info.get("about")
            info["followers"] = page_info.get("followers")
            info["website"] = page_info.get("website")
            info["profile_pic_url"] = page_info.get("profile_pic_url")
            info["category"] = page_info.get("category")
        except Exception:
            logger.debug("get_page_info failed for %s, using minimal info.", page_name)
            info["name"] = page_name

        return info
