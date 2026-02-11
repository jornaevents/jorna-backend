"""
Instagram scraper backend using **instaloader**.

Why instaloader?
─────────────────
•  Handles session cookies / login to avoid 403 on public endpoints.
•  Automatic rate-limit back-off (it sleeps when Instagram throttles).
•  Supports hashtag iteration, profile metadata, and media download.
•  Stays current with Instagram's private API changes (actively maintained).

Anti-blocking strategy
──────────────────────
1.  **Session login** – authenticated requests get far more headroom
    before 429/403 kicks in.
2.  **Randomised delays** between requests (human-like cadence).
3.  **Persistent session file** – reuses cookies so we don't trigger
    "new device" challenges on every run.
4.  **Configurable post cap** per hashtag to keep request volume low.

Usage
─────
    from app.instagram_scraper.scraper import InstagramScraper

    scraper = InstagramScraper()            # anonymous (limited)
    scraper = InstagramScraper("user", "pass")  # logged-in (recommended)
    vendors = scraper.scrape_hashtags(["PunjabiDJ", "MehndiArtist"])
"""

from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Optional

import instaloader  # pip install instaloader

from app.scraper_common.vendor_result import ScrapedVendor

logger = logging.getLogger(__name__)

# ── Tunables ─────────────────────────────────────────────────────────
DEFAULT_MAX_POSTS_PER_TAG = 30        # keep requests low per tag
MIN_DELAY_SECONDS = 2.0               # minimum pause between requests
MAX_DELAY_SECONDS = 6.0               # maximum pause between requests
SESSION_DIR = Path(__file__).resolve().parent / ".sessions"


class InstagramScraper:
    """Scrapes Instagram hashtags for potential vendor profiles."""

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        *,
        max_posts_per_tag: int = DEFAULT_MAX_POSTS_PER_TAG,
    ) -> None:
        self.max_posts_per_tag = max_posts_per_tag

        # ── build an Instaloader context ──
        self._loader = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            quiet=True,
        )

        # ── optional login (highly recommended) ──
        if username and password:
            self._login(username, password)
        else:
            logger.warning(
                "Running without login – Instagram will severely "
                "rate-limit anonymous requests.  Pass credentials for "
                "more reliable scraping."
            )

    # ──────────────────────────────────────────────────────────────────
    #  Session / login helpers
    # ──────────────────────────────────────────────────────────────────
    def _session_file(self, username: str) -> Path:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        return SESSION_DIR / f"{username}.session"

    def _login(self, username: str, password: str) -> None:
        """Login *or* resume a cached session to avoid repeated challenges."""
        session_path = self._session_file(username)
        try:
            if session_path.exists():
                self._loader.load_session_from_file(username, str(session_path))
                logger.info("Resumed saved session for @%s", username)
            else:
                self._loader.login(username, password)
                self._loader.save_session_to_file(str(session_path))
                logger.info("Logged in and saved session for @%s", username)
        except instaloader.exceptions.TwoFactorAuthRequiredException:
            logger.error(
                "Two-factor auth required for @%s – provide an app password "
                "or handle 2FA interactively.",
                username,
            )
            raise
        except instaloader.exceptions.ConnectionException as exc:
            logger.error("Login failed for @%s: %s", username, exc)
            raise

    # ──────────────────────────────────────────────────────────────────
    #  Core scraping
    # ──────────────────────────────────────────────────────────────────
    def _random_sleep(self) -> None:
        """Human-like delay between requests."""
        delay = random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
        logger.debug("Sleeping %.1fs …", delay)
        time.sleep(delay)

    def scrape_hashtags(
        self,
        hashtags: list[str],
        *,
        max_posts_per_tag: int | None = None,
    ) -> list[ScrapedVendor]:
        """
        Iterate over each hashtag, inspect recent posts, and collect
        unique vendor profiles.

        Returns a list of `ScrapedVendor` sorted by relevance score
        (descending).
        """
        cap = max_posts_per_tag or self.max_posts_per_tag
        seen_usernames: set[str] = set()
        vendors: dict[str, ScrapedVendor] = {}       # username -> vendor

        for tag in hashtags:
            logger.info("Scraping #%s (max %d posts) …", tag, cap)
            try:
                self._scrape_single_tag(tag, cap, seen_usernames, vendors)
            except instaloader.exceptions.QueryReturnedNotFoundException:
                logger.warning("Hashtag #%s not found – skipping.", tag)
            except instaloader.exceptions.ConnectionException as exc:
                logger.error(
                    "Connection error on #%s: %s – pausing 60s then continuing.",
                    tag,
                    exc,
                )
                time.sleep(60)  # long pause on hard block
            except Exception:
                logger.exception("Unexpected error on #%s – skipping.", tag)

        result = sorted(vendors.values(), key=lambda v: v.relevance_score, reverse=True)
        logger.info("Scraping complete – %d unique vendors found.", len(result))
        return result

    def _scrape_single_tag(
        self,
        tag: str,
        cap: int,
        seen_usernames: set[str],
        vendors: dict[str, ScrapedVendor],
    ) -> None:
        """Process up to `cap` recent posts for a single hashtag."""
        # Construct Hashtag directly — bypasses _obtain_metadata() which
        # calls the blocked api/v1/tags/web_info/ endpoint.
        # Then use get_posts_resumable() which queries via GraphQL instead.
        hashtag_obj = instaloader.Hashtag(self._loader.context, {"name": tag.lower()})
        posts = hashtag_obj.get_posts_resumable()

        for idx, post in enumerate(posts):
            if idx >= cap:
                break

            owner_username = post.owner_username

            if owner_username in vendors:
                # Already seen — just record the additional hashtag match
                if tag not in vendors[owner_username].hashtags_matched:
                    vendors[owner_username].hashtags_matched.append(tag)
                if post.url and post.url not in vendors[owner_username].sample_post_urls:
                    vendors[owner_username].sample_post_urls.append(post.url)
                continue

            if owner_username in seen_usernames:
                continue
            seen_usernames.add(owner_username)

            # Fetch full profile (extra request – sleep first)
            self._random_sleep()

            try:
                profile = instaloader.Profile.from_username(
                    self._loader.context, owner_username
                )
            except instaloader.exceptions.ProfileNotExistsException:
                logger.debug("Profile @%s no longer exists – skipping.", owner_username)
                continue

            vendor = ScrapedVendor(
                username=owner_username,
                full_name=profile.full_name or owner_username,
                platform="instagram",
                profile_url=f"https://www.instagram.com/{owner_username}/",
                bio=profile.biography,
                followers=profile.followers,
                post_count=profile.mediacount,
                hashtags_matched=[tag],
                sample_post_urls=[post.url] if post.url else [],
                profile_pic_url=profile.profile_pic_url,
                is_business_account=profile.is_business_account,
                external_url=profile.external_url,
            )
            vendors[owner_username] = vendor
            logger.info(
                "  ✓ @%s  (%d followers, business=%s)",
                owner_username,
                profile.followers,
                profile.is_business_account,
            )

            self._random_sleep()
