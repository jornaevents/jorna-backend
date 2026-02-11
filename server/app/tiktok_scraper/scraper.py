"""
TikTok scraper backend using the **TikTokApi** library.

Why TikTokApi?
──────────────
•  Most popular Python TikTok scraping library.
•  Uses Playwright (headless Chromium) to interact with TikTok's API,
   bypassing many anti-bot measures.
•  Supports hashtag search, user profiles, and video metadata.

Anti-blocking strategy
──────────────────────
1.  **Headless browser** – Playwright renders pages like a real user.
2.  **Randomised delays** between requests (human-like cadence).
3.  **ms_token** from browser cookies for authenticated access.
4.  **Configurable post cap** per hashtag to keep request volume low.

Usage
─────
    from app.tiktok_scraper.scraper import TikTokScraper

    scraper = TikTokScraper()
    scraper = TikTokScraper(ms_token="your_ms_token_here")
    vendors = scraper.scrape_hashtags(["PunjabiDJ", "MehndiArtist"])
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

from TikTokApi import TikTokApi

from app.scraper_common.vendor_result import ScrapedVendor

logger = logging.getLogger(__name__)

# ── Tunables ─────────────────────────────────────────────────────────
DEFAULT_MAX_POSTS_PER_TAG = 30        # keep requests low per tag
MIN_DELAY_SECONDS = 2.0               # minimum pause between requests
MAX_DELAY_SECONDS = 6.0               # maximum pause between requests


class TikTokScraper:
    """Scrapes TikTok hashtags for potential vendor profiles."""

    def __init__(
        self,
        ms_token: Optional[str] = None,
        *,
        max_posts_per_tag: int = DEFAULT_MAX_POSTS_PER_TAG,
    ) -> None:
        self.max_posts_per_tag = max_posts_per_tag
        self.ms_token = ms_token

        if not ms_token:
            logger.warning(
                "Running without ms_token – TikTok may limit or block "
                "requests.  Provide an ms_token from your browser cookies "
                "for more reliable scraping."
            )

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
        Iterate over each hashtag, inspect recent videos, and collect
        unique vendor profiles.

        Returns a list of `ScrapedVendor` sorted by relevance score
        (descending).
        """
        # TikTokApi is async — run the async method from sync context
        return asyncio.run(
            self._scrape_hashtags_async(hashtags, max_posts_per_tag=max_posts_per_tag)
        )

    async def _scrape_hashtags_async(
        self,
        hashtags: list[str],
        *,
        max_posts_per_tag: int | None = None,
    ) -> list[ScrapedVendor]:
        """Async implementation of hashtag scraping."""
        cap = max_posts_per_tag or self.max_posts_per_tag
        seen_usernames: set[str] = set()
        vendors: dict[str, ScrapedVendor] = {}

        ms_tokens = [self.ms_token] if self.ms_token else []

        async with TikTokApi() as api:
            await api.create_sessions(
                ms_tokens=ms_tokens,
                num_sessions=1,
                sleep_after=3,
                headless=False,
                browser="webkit",
                suppress_resource_load_types=["image", "media", "font", "stylesheet"],
            )

            for tag_name in hashtags:
                logger.info("Scraping #%s (max %d posts) …", tag_name, cap)
                try:
                    await self._scrape_single_tag(
                        api, tag_name, cap, seen_usernames, vendors
                    )
                except Exception:
                    logger.exception(
                        "Error on #%s – skipping.", tag_name
                    )

        result = sorted(vendors.values(), key=lambda v: v.relevance_score, reverse=True)
        logger.info("Scraping complete – %d unique vendors found.", len(result))
        return result

    async def _scrape_single_tag(
        self,
        api: TikTokApi,
        tag_name: str,
        cap: int,
        seen_usernames: set[str],
        vendors: dict[str, ScrapedVendor],
    ) -> None:
        """Process up to `cap` recent videos for a single hashtag."""
        tag = api.hashtag(name=tag_name)
        count = 0

        async for video in tag.videos(count=cap):
            if count >= cap:
                break
            count += 1

            try:
                author_info = video.author
                if author_info is None:
                    continue

                username = author_info.username
                if not username:
                    continue
            except (AttributeError, KeyError):
                continue

            if username in vendors:
                # Already seen — record the additional hashtag match
                if tag_name not in vendors[username].hashtags_matched:
                    vendors[username].hashtags_matched.append(tag_name)
                video_url = f"https://www.tiktok.com/@{username}/video/{video.id}"
                if video_url not in vendors[username].sample_post_urls:
                    vendors[username].sample_post_urls.append(video_url)
                continue

            if username in seen_usernames:
                continue
            seen_usernames.add(username)

            self._random_sleep()

            # Extract user info from the video's author data
            try:
                user_data = await self._get_user_info(api, username)
            except Exception:
                logger.debug("Could not fetch profile @%s – using basic info.", username)
                user_data = {}

            full_name = (
                user_data.get("nickname")
                or getattr(author_info, "nickname", None)
                or username
            )
            bio = user_data.get("signature") or getattr(author_info, "signature", None)
            followers = user_data.get("follower_count") or getattr(
                getattr(author_info, "stats", None), "follower_count", None
            )
            video_count = user_data.get("video_count") or getattr(
                getattr(author_info, "stats", None), "video_count", None
            )

            video_url = f"https://www.tiktok.com/@{username}/video/{video.id}"
            vendor = ScrapedVendor(
                username=username,
                full_name=full_name,
                platform="tiktok",
                profile_url=f"https://www.tiktok.com/@{username}",
                bio=bio,
                followers=followers,
                post_count=video_count,
                hashtags_matched=[tag_name],
                sample_post_urls=[video_url],
                profile_pic_url=user_data.get("avatar_url"),
                is_business_account=user_data.get("is_business", None),
                external_url=user_data.get("bio_link"),
            )
            vendors[username] = vendor
            logger.info(
                "  ✓ @%s  (%s followers)",
                username,
                followers or "?",
            )

            self._random_sleep()

    async def _get_user_info(self, api: TikTokApi, username: str) -> dict:
        """
        Fetch detailed user info for a TikTok username.

        Returns a dict with keys: nickname, signature, follower_count,
        video_count, avatar_url, is_business, bio_link.
        """
        info: dict = {}
        try:
            user = api.user(username=username)
            user_data = await user.info()

            # user_data structure varies — extract what we can
            user_info = user_data.get("user_info", {}).get("user", {})
            stats = user_data.get("user_info", {}).get("stats", {})

            if not user_info:
                user_info = user_data.get("user", {})
                stats = user_data.get("stats", {})

            info["nickname"] = user_info.get("nickname")
            info["signature"] = user_info.get("signature")
            info["follower_count"] = stats.get("followerCount") or stats.get("follower_count")
            info["video_count"] = stats.get("videoCount") or stats.get("video_count")
            info["avatar_url"] = user_info.get("avatarLarger") or user_info.get("avatar_url")
            info["is_business"] = user_info.get("commerceUserInfo", {}).get("commerceUser", False)

            bio_link = user_info.get("bioLink", {})
            if isinstance(bio_link, dict):
                info["bio_link"] = bio_link.get("link")
            elif isinstance(bio_link, str):
                info["bio_link"] = bio_link

        except Exception:
            logger.debug("Failed to get detailed info for @%s", username)

        return info
