"""
Export helpers – write scraping results to JSON / CSV.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from .vendor_result import ScrapedVendor


def to_json(
    vendors: Sequence[ScrapedVendor],
    output_path: str | Path = "scraped_vendors.json",
) -> Path:
    """Serialise vendor list to a pretty-printed JSON file."""
    path = Path(output_path)
    data = [asdict(v) for v in vendors]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path.resolve()


def to_csv(
    vendors: Sequence[ScrapedVendor],
    output_path: str | Path = "scraped_vendors.csv",
) -> Path:
    """Serialise vendor list to a CSV file."""
    path = Path(output_path)
    if not vendors:
        path.write_text("", encoding="utf-8")
        return path.resolve()

    fieldnames = [
        "username",
        "full_name",
        "platform",
        "profile_url",
        "bio",
        "followers",
        "post_count",
        "hashtags_matched",
        "sample_post_urls",
        "profile_pic_url",
        "is_business_account",
        "external_url",
    ]

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for v in vendors:
            row = asdict(v)
            # flatten lists into semicolon-separated strings for CSV
            row["hashtags_matched"] = "; ".join(row["hashtags_matched"])
            row["sample_post_urls"] = "; ".join(row["sample_post_urls"])
            writer.writerow(row)

    return path.resolve()
