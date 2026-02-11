#!/usr/bin/env python3
"""
DesiConnect TikTok Vendor Scraper – CLI entry point.

Usage
─────
  # Without token (may be limited):
  python -m app.tiktok_scraper.run

  # With ms_token (recommended):
  python -m app.tiktok_scraper.run --ms-token YOUR_MS_TOKEN

  # Custom limits:
  python -m app.tiktok_scraper.run --ms-token TOKEN --max-posts 20 --output vendors_tt.json

How to get ms_token
───────────────────
  1. Log into TikTok in your browser.
  2. Open DevTools (F12) → Application → Cookies → tiktok.com.
  3. Find the cookie named "msToken" and copy its value.
  4. Pass it via --ms-token flag or set TT_MS_TOKEN env var.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Ensure the server package is importable when running from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.instagram_scraper.hashtags import HASHTAGS  # reuse same hashtags
from app.tiktok_scraper.scraper import TikTokScraper
from app.scraper_common.export import to_json, to_csv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape TikTok for South-Asian wedding vendors."
    )
    parser.add_argument(
        "--ms-token",
        default=os.getenv("TT_MS_TOKEN"),
        help="TikTok msToken cookie value (or set TT_MS_TOKEN env var)",
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=30,
        help="Max videos to inspect per hashtag (default: 30)",
    )
    parser.add_argument(
        "-o", "--output",
        default="scraped_vendors_tt.json",
        help="Output file path (supports .json or .csv)",
    )
    parser.add_argument(
        "--tags",
        nargs="*",
        help="Override hashtag list (space-separated, without #)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show debug-level logs",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    tags = args.tags if args.tags else HASHTAGS
    logging.info("Will scrape %d hashtags, up to %d videos each.", len(tags), args.max_posts)

    scraper = TikTokScraper(
        ms_token=args.ms_token,
        max_posts_per_tag=args.max_posts,
    )

    vendors = scraper.scrape_hashtags(tags)

    # ── export results ──
    output = Path(args.output)
    if output.suffix == ".csv":
        dest = to_csv(vendors, output)
    else:
        dest = to_json(vendors, output)

    logging.info("Results written to %s  (%d vendors)", dest, len(vendors))


if __name__ == "__main__":
    main()
