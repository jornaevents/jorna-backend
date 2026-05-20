#!/usr/bin/env python3
"""
Import scraped Instagram vendors into the Desiconnect database.

Reads vendors.json (output from scraper.py) and registers each vendor
in the backend via the API so they appear in the bundle creator.

For each vendor the script:
  1. Creates a placeholder user account
  2. Creates a vendor profile with bio, category, and tags
  3. Creates one service entry with scraped images
  4. Adds normalized tags (which feed into bundle scoring)

Usage:
    export API_BASE_URL=https://your-railway-domain.railway.app
    python import_to_db.py                  # import all from vendors.json
    python import_to_db.py --dry-run        # preview without creating records
    python import_to_db.py --file out.json  # use a different input file
"""

import argparse
import json
import os
import secrets
import sys
import time
from typing import Optional

import httpx

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
DEFAULT_PASSWORD = "Desiconnect@2026!"  # placeholder password for imported accounts
REQUEST_TIMEOUT = 30.0


def _post(client: httpx.Client, path: str, payload: dict, token: Optional[str] = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = client.post(f"{API_BASE_URL}{path}", json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _get(client: httpx.Client, path: str, token: str) -> dict:
    resp = client.get(f"{API_BASE_URL}{path}",
                      headers={"Authorization": f"Bearer {token}"},
                      timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def import_vendor(client: httpx.Client, vendor: dict, dry_run: bool) -> bool:
    """
    Import a single vendor. Returns True on success.

    Steps:
      1. Register a placeholder user (email derived from instagram username)
      2. Login to get a JWT
      3. Create vendor profile
      4. Create a service with scraped images
      5. Add tags
    """
    username = vendor.get("instagram_username", "unknown")
    name = vendor.get("business_name", username)
    bio = vendor.get("bio", f"{name} — South Asian event vendor.")
    category = vendor.get("category", "other")
    images = vendor.get("images", [])
    tags = vendor.get("tags", [])
    website = vendor.get("website", "")

    # Generate a deterministic but unique email for this vendor
    email = f"{username}@desiconnect-import.com"
    # Split business name into first/last for the user record
    name_parts = name.strip().split(" ", 1)
    f_name = name_parts[0] if name_parts else username
    l_name = name_parts[1] if len(name_parts) > 1 else "Vendor"

    print(f"\n  [{username}]  category={category}  tags={len(tags)}")

    if dry_run:
        print(f"    DRY RUN — would register {email}")
        return True

    # Step 1: Register user
    try:
        register_payload = {
            "email": email,
            "password": DEFAULT_PASSWORD,
            "username": username[:30],
            "f_name": f_name[:50],
            "l_name": l_name[:50],
            "age": 30,
            "location": vendor.get("location", {}).get("city", "New Jersey"),
            "gender": "not specified",
            "language": "English",
        }
        _post(client, "/auth/register", register_payload)
        print(f"    Registered user: {email}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400 and "already taken" in e.response.text.lower():
            print(f"    User already exists, continuing...")
        else:
            print(f"    Registration failed: {e.response.status_code} {e.response.text[:100]}")
            return False

    # Step 2: Login
    try:
        login_resp = _post(client, "/auth/login", {"identifier": email, "password": DEFAULT_PASSWORD})
        token = login_resp["access_token"]
        print(f"    Logged in")
    except Exception as e:
        print(f"    Login failed: {e}")
        return False

    # Step 3: Create vendor profile
    try:
        vendor_resp = _post(client, "/vendors", {
            "bio": bio[:500] if bio else f"South Asian {category} vendor based in NJ/NYC.",
            "category": category,
        }, token=token)
        vendor_id = vendor_resp["vendor_id"]
        print(f"    Created vendor profile: {vendor_id}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400 and "already" in e.response.text.lower():
            # Vendor profile already exists — fetch it
            try:
                me = _get(client, "/vendors/me", token)
                vendor_id = me["vendor_id"]
                print(f"    Vendor profile already exists: {vendor_id}")
            except Exception:
                print(f"    Could not fetch existing vendor profile")
                return False
        else:
            print(f"    Vendor creation failed: {e.response.status_code} {e.response.text[:100]}")
            return False

    # Step 4: Create a service
    try:
        service_name = f"{name} — {category.title()} Services"
        experience_text = bio[:300] if bio else f"Professional {category} services for South Asian events."
        _post(client, "/services", {
            "name": service_name[:255],
            "price": 0.0,           # placeholder — vendor sets real price after claiming
            "experience": experience_text,
            "media": images[:9],
            "category": category,
            "description": f"Scraped from Instagram @{username}. {website}".strip(),
        }, token=token)
        print(f"    Created service with {len(images[:9])} images")
    except Exception as e:
        print(f"    Service creation failed (non-fatal): {e}")

    # Step 5: Add tags
    added = 0
    for tag in tags[:20]:
        try:
            _post(client, f"/vendors/{vendor_id}/tags", {"tag": tag}, token=token)
            added += 1
        except Exception:
            pass
    if added:
        print(f"    Added {added} tags")

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Import scraped vendors into Desiconnect")
    parser.add_argument("--file", default="vendors.json", help="Input JSON file (default: vendors.json)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without creating any records")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between vendors (default: 0.5)")
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"Error: {args.file} not found. Run scraper.py first.")
        return 1

    with open(args.file, encoding="utf-8") as f:
        vendors = json.load(f)

    print(f"Loaded {len(vendors)} vendors from {args.file}")
    print(f"Target: {API_BASE_URL}")
    if args.dry_run:
        print("DRY RUN mode — no records will be created\n")

    success = 0
    failed = 0

    with httpx.Client() as client:
        for i, vendor in enumerate(vendors, 1):
            print(f"\n[{i}/{len(vendors)}]", end="")
            ok = import_vendor(client, vendor, dry_run=args.dry_run)
            if ok:
                success += 1
            else:
                failed += 1
            if i < len(vendors):
                time.sleep(args.delay)

    print(f"\n{'='*60}")
    print(f"Done.  Imported: {success}  Failed: {failed}")
    print(f"{'='*60}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
