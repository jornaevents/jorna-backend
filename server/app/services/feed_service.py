"""Business logic for the short-form video feed via YouTube Data API v3."""

import logging
import time
from typing import Optional

import httpx

from app.config import YOUTUBE_API_KEY

logger = logging.getLogger(__name__)

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

# Search terms that surface South Asian wedding performance content.
# Combined with OR so a single API call covers all of them.
_SEARCH_QUERY = (
    "south asian wedding dance performance "
    "OR sangeet performance "
    "OR bhangra wedding dance "
    "OR bollywood wedding performance "
    "OR desi wedding dance "
    "OR mehndi night dance "
    "OR garba wedding performance"
)

# Simple in-memory cache: {cache_key: (timestamp, payload)}
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 600  # 10 minutes


class FeedError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _cache_key(page_token: str, max_results: int) -> str:
    return f"{page_token}:{max_results}"


def get_shorts_feed(
    *,
    page_token: str = "",
    max_results: int = 10,
) -> dict:
    """Search YouTube for South Asian wedding short-form videos.

    Uses a 10-minute in-memory cache per page to avoid burning API quota.
    Returns video metadata ready for the mobile client to embed.
    """
    if not YOUTUBE_API_KEY:
        raise FeedError(503, "YouTube API is not configured on this server.")

    key = _cache_key(page_token, max_results)
    cached = _cache.get(key)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        return cached[1]

    params: dict = {
        "part": "snippet",
        "q": _SEARCH_QUERY,
        "type": "video",
        "videoDuration": "short",   # under 4 minutes (YouTube API's shortest filter)
        "videoEmbeddable": "true",
        "safeSearch": "moderate",
        "relevanceLanguage": "en",
        "maxResults": max_results,
        "key": YOUTUBE_API_KEY,
    }
    if page_token:
        params["pageToken"] = page_token

    try:
        response = httpx.get(YOUTUBE_SEARCH_URL, params=params, timeout=10.0)
        response.raise_for_status()
    except httpx.TimeoutException:
        raise FeedError(504, "YouTube API request timed out.")
    except httpx.HTTPStatusError as e:
        logger.error("YouTube API error %s: %s", e.response.status_code, e.response.text)
        if e.response.status_code == 403:
            raise FeedError(503, "YouTube API quota exceeded or key invalid.")
        raise FeedError(502, "YouTube API returned an error.")

    data = response.json()

    items = [
        {
            "video_id": item["id"]["videoId"],
            "title": item["snippet"]["title"],
            "description": item["snippet"]["description"],
            "thumbnail": (
                item["snippet"]["thumbnails"].get("high", {}).get("url")
                or item["snippet"]["thumbnails"].get("default", {}).get("url")
            ),
            "channel_name": item["snippet"]["channelTitle"],
            "published_at": item["snippet"]["publishedAt"],
        }
        for item in data.get("items", [])
        if item.get("id", {}).get("videoId")
    ]

    result = {
        "items": items,
        "next_page_token": data.get("nextPageToken"),
        "prev_page_token": data.get("prevPageToken"),
        "total_results": data.get("pageInfo", {}).get("totalResults", 0),
    }

    _cache[key] = (time.time(), result)
    return result
