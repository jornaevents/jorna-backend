# Instagram Enrichment Scraper

Enriches registered Desiconnect vendor profiles with data scraped from their Instagram accounts.

Vendors link their own Instagram username through the app. The scraper then fetches those accounts, extracts tags, and posts them back to each vendor's existing profile.

Instagram-generated tags are stored separately from vendor-curated tags so vendors keep full control over their profile.

## Flow

```
Vendor links @username in app
        ↓
scraper.py fetches linked vendors from API
        ↓
Apify scrapes each Instagram profile
        ↓
Tags (+ bio if empty) posted back via /vendors/{id}/instagram-enrich
        ↓
Bundle creator uses instagram_tags in scoring
```

## Setup

```bash
export APIFY_API_TOKEN=apify_api_xxxxx
export API_BASE_URL=https://your-railway-domain.railway.app
export ADMIN_USERNAME=youradminusername
export ADMIN_PASSWORD=YourAdminPassword1

pip install -r requirements.txt
```

## Usage

```bash
# Preview without changing anything
python scraper.py --dry-run

# Enrich all linked vendors
python scraper.py
```

## How vendors link their Instagram

In the app, vendors call:
```
PATCH /vendors/me
{ "instagram_username": "djsuhel" }
```

The scraper then picks them up automatically on the next run.

## What gets enriched

- **instagram_tags** — normalized tags extracted from post hashtags and bio,
  aligned with the bundle creator's scoring keywords (e.g. `bhangra`, `wedding`,
  `traditional`). Kept separate from user-inputted tags.
- **Bio** — only set if the vendor hasn't written one yet.

It **doesn't touch packages or photos.** It used to add recent post images to
the vendor's first package, but vendors never chose those, and Instagram's
image URLs expire within days, so they turned into broken images.

## Bundle creator integration

The chatbot bundle scoring checks both `tags` (user-inputted) and
`instagram_tags` (scraped) when matching vendors to user style preferences.
A vendor tagged `bhangra` from Instagram will score higher when a user
selects "Cultural experience" in the bundle builder.

## Automated scheduling

The scraper runs automatically every **Sunday at 3am** via a cron-job.org job that calls:

```
POST https://your-railway-domain.railway.app/admin/scraper/run?api_key=<SCRAPER_API_KEY>
```

The `SCRAPER_API_KEY` environment variable must be set in Railway. The cron job is configured at cron-job.org with schedule `0 3 * * 0`.

## Environment variables

| Variable | Description |
|---|---|
| `APIFY_API_TOKEN` | Apify personal API token |
| `API_BASE_URL` | Backend URL (default: http://localhost:8000) |
| `ADMIN_USERNAME` | Username of an admin account in Desiconnect (or use `ADMIN_EMAIL`) |
| `ADMIN_PASSWORD` | Password for that admin account |
