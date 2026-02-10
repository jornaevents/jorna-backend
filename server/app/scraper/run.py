#!/usr/bin/env python3
"""
DesiConnect Vendor Scraper – CLI entry point.

Usage
─────
  # Anonymous (limited – Instagram will throttle quickly):
  python -m app.scraper.run

  # Authenticated (recommended):
  python -m app.scraper.run --username YOUR_IG_USER --password YOUR_IG_PASS

  # Custom limits:
  python -m app.scraper.run -u USER -p PASS --max-posts 50 --output vendors.json

Environment Variables (alternative to CLI flags)
────────────────────────────────────────────────
  IG_USERNAME   – Instagram username
  IG_PASSWORD   – Instagram password
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Ensure the server package is importable when running from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.scraper.hashtags import HASHTAGS
from app.scraper.instagram_scraper import InstagramScraper
from app.scraper.export import to_json, to_csv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape Instagram for South-Asian wedding vendors."
    )
    parser.add_argument(
        "-u", "--username",
        default=os.getenv("IG_USERNAME"),
        help="Instagram username (or set IG_USERNAME env var)",
    )
    parser.add_argument(
        "-p", "--password",
        default=os.getenv("IG_PASSWORD"),
        help="Instagram password (or set IG_PASSWORD env var)",
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=30,
        help="Max posts to inspect per hashtag (default: 30)",
    )
    parser.add_argument(
        "-o", "--output",
        default="scraped_vendors.json",
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
    logging.info("Will scrape %d hashtags, up to %d posts each.", len(tags), args.max_posts)

    scraper = InstagramScraper(
        username=args.username,
        password=args.password,
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
