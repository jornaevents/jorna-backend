# Instagram Vendor Scraper

A Python script to scrape public Instagram profiles of South Asian service vendors in the NJ/NYC metro area. Uses the specialized Apify Instagram scraper for reliable profile and post data extraction.

## Full workflow

```
scraper.py  →  vendors.json  →  import_to_db.py  →  Desiconnect DB  →  Bundle creator
```

1. `scraper.py` — scrapes Instagram profiles via Apify and outputs `vendors.json`
2. `import_to_db.py` — reads `vendors.json` and registers each vendor in the backend API so they appear in the AI bundle creator

## Features

✅ **No Instagram login required** - Uses only publicly available data  
✅ **Robust error handling** - Continues if individual profiles fail  
✅ **Category classification** - Categories map directly to DB VendorCategory enum  
✅ **Bundle-ready tags** - Extracted tags align with chatbot bundle scoring keywords  
✅ **DB import script** - `import_to_db.py` registers vendors so the bundle creator can find them  
✅ **Progress tracking** - Real-time feedback on scraping status  
✅ **Normalized output** - Standardized JSON schema for vendor profiles  
✅ **Post image extraction** - Captures recent post images for portfolios  
✅ **Batch-ready** - Designed for marketplace integration  

## Setup

### Option 1: Quick Setup (Recommended)

```bash
# Set your API token
export APIFY_API_TOKEN="your_token_here"

# Install dependencies
pip install -r requirements.txt

# Run the scraper
python scraper.py
```

### Option 2: Using Setup Script

```bash
./setup.sh your_api_token_here
source .env
python scraper.py
```

### Finding Your API Token

1. Go to [Apify Dashboard](https://my.apify.com)
2. Click **Settings** → **API & Integrations**
3. Copy your **Personal API token** (starts with `apify_api_`)
4. Export it: `export APIFY_API_TOKEN="your_token"`

## Usage

```bash
python scraper.py
```

Output: **vendors.json** with enriched vendor profiles

### Example Output

```json
[
  {
    "business_name": "Nick DJ",
    "instagram_username": "djnicknyc",
    "category": "DJ",
    "location": {
      "city": "New Jersey",
      "state": "NJ",
      "country": "USA"
    },
    "bio": "🎵 NYC/NJ based DJ | Weddings • Sangeet • Events",
    "profile_picture": "https://scontent-ord5-1.cdninstagram.com/...",
    "images": [
      "https://scontent-ord5-1.cdninstagram.com/post1...",
      "https://scontent-ord5-1.cdninstagram.com/post2..."
    ],
    "hashtags": ["#dj", "#wedding", "#sangeet", "#nj", "#nyc", "#events"],
    "top_posts": [
      {
        "image": "https://scontent-ord5-1.cdninstagram.com/top1...",
        "likes": 342,
        "caption": "Just wrapped an amazing wedding at the Taj Hotel! #wedding #dj #sangeet",
        "timestamp": "2024-02-10T15:30:00Z"
      },
      {
        "image": "https://scontent-ord5-1.cdninstagram.com/top2...",
        "likes": 289,
        "caption": "New setup for 2024! Ready for all your celebrations 🎉",
        "timestamp": "2024-01-28T12:00:00Z"
      }
    ],
    "website": "https://djnicknyc.com",
    "followers": 1250,
    "claimed": false,
    "status": "draft",
    "data_sources": ["instagram"]
  }
]
```

## Categories

The script classifies vendors into:

- **DJ** - DJs, music producers
- **Catering** - Restaurants, caterers, food services
- **Mehndi** - Henna artists, mehndi services
- **Dhol** - Dhol players, drummers, tabla musicians
- **Singer** - Vocalists, live singers
- **Dancer** - Dance crews, choreographers, Bhangra
- **Decorator** - Event decorators, florists
- **Venue** - Banquet halls, wedding venues

Classification uses keyword matching on username, bio, and full name.

## Script Architecture

- **InstagramVendorScraper** - Main scraper class
  - `scrape_profile()` - Calls Apify actor for single user
  - `classify_vendor()` - Category classification logic
  - `normalize_vendor()` - Converts raw data to schema
  - `scrape_all()` - Main orchestration loop with error handling
  - `save_vendors()` - Exports to JSON

## Error Handling

- ✓ Continues if individual profile fails
- ✓ Tracks failed usernames
- ✓ Prints progress and summary stats
- ✓ Graceful shutdown on Ctrl+C
- ✓ Flexible field name parsing for different API responses

## Performance

- ~1 second delay between requests to respect rate limits
- Processes all 35 profiles in ~2-3 minutes depending on API speed
- Free tier supports ~1000 API calls/month

## Dependencies

- **apify-client** - Official Apify Python SDK

## Troubleshooting

### "APIFY_API_TOKEN not set"
Make sure you've exported the environment variable:
```bash
export APIFY_API_TOKEN="apify_api_xxxxx"
```

### Script too slow
The Apify free tier has request limits (~1000/month). Upgrade to Starter plan for faster processing.

### Missing social media data
Some accounts have restricted public data. The script handles missing fields gracefully and continues.

### "Failed to scrape" for specific usernames
- Account may be private or deleted
- Username might be inactive
- Instagram may have rate-limited the API temporarily
Check the failed usernames list in the console output.

## Future Enhancements

- Batch API calls for faster processing
- Schedule periodic re-scraping
- Add phone/location extraction
- Post frequency & engagement analysis
- Store in database instead of JSON
- Web dashboard for vendor review

---

**MVP Status**: ✅ Production-ready for vendor discovery & intake
