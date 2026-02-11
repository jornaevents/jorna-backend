#!/usr/bin/env python3
"""
DesiConnect Facebook Vendor Scraper – CLI entry point.

Usage
─────
  # Without cookies (limited):
  python -m app.facebook_scraper.run

  # With cookies (recommended):
  python -m app.facebook_scraper.run --cookies path/to/cookies.txt

  # Custom limits:
  python -m app.facebook_scraper.run --cookies cookies.txt --max-pages 20 --output vendors_fb.json

How to get cookies
──────────────────
  1. Log into Facebook in your browser.
  2. Use the "Get cookies.txt LOCALLY" browser extension (or similar)
     to export cookies in Netscape format.
  3. Save as cookies.txt and pass via --cookies flag.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Ensure the server package is importable when running from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.facebook_scraper.keywords import KEYWORDS
from app.facebook_scraper.scraper import FacebookScraper
from app.scraper_common.export import to_json, to_csv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape Facebook for South-Asian wedding vendors."
    )
    parser.add_argument(
        "--cookies",
        default=os.getenv("FB_COOKIES"),
        help="Path to cookies.txt file (or set FB_COOKIES env var)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Max pages to inspect per keyword (default: 10)",
    )
    parser.add_argument(
        "-o", "--output",
        default="scraped_vendors_fb.json",
        help="Output file path (supports .json or .csv)",
    )
    parser.add_argument(
        "--keywords",
        nargs="*",
        help="Override keyword list (space-separated, quote multi-word)",
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

    kw_list = args.keywords if args.keywords else KEYWORDS
    logging.info("Will search %d keywords, up to %d pages each.", len(kw_list), args.max_pages)

    scraper = FacebookScraper(
        cookies=args.cookies,
        max_pages_per_keyword=args.max_pages,
    )

    vendors = scraper.scrape_keywords(kw_list)

    # ── export results ──
    output = Path(args.output)
    if output.suffix == ".csv":
        dest = to_csv(vendors, output)
    else:
        dest = to_json(vendors, output)

    logging.info("Results written to %s  (%d vendors)", dest, len(vendors))


if __name__ == "__main__":
    main()
