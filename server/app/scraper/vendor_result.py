"""
Vendor dataclass used by every scraper backend.

Keeps vendor information decoupled from any one platform
so the same structure works whether data comes from Instagram,
TikTok, or another source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ScrapedVendor:
    """A vendor profile discovered by the scraper."""

    username: str
    full_name: str
    platform: str                       # e.g. "instagram", "tiktok"
    profile_url: str
    bio: Optional[str] = None
    followers: Optional[int] = None
    post_count: Optional[int] = None
    hashtags_matched: list[str] = field(default_factory=list)
    sample_post_urls: list[str] = field(default_factory=list)
    profile_pic_url: Optional[str] = None
    is_business_account: Optional[bool] = None
    external_url: Optional[str] = None   # link-in-bio

    # ---- helpers ----
    @property
    def relevance_score(self) -> float:
        """
        Quick heuristic: more matched hashtags + higher follower count
        means a more relevant vendor.
        """
        tag_score = len(self.hashtags_matched) * 10
        follower_score = min((self.followers or 0) / 1000, 50)  # cap at 50
        return tag_score + follower_score

    def __repr__(self) -> str:
        return (
            f"ScrapedVendor({self.platform}/@{self.username}, "
            f"matched={len(self.hashtags_matched)}, "
            f"followers={self.followers})"
        )
