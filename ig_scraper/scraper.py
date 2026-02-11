#!/usr/bin/env python3
"""
Instagram Vendor Scraper for NJ/NYC South Asian Service Providers
Uses Apify's instagram-scraper actor to collect public profile data
"""

import json
import os
import sys
import time
from typing import Optional, List, Dict, Any
from apify_client import ApifyClient


# Hardcoded list of Instagram usernames
INSTAGRAM_USERNAMES = [
    # DJs & Entertainment
    "djsuhel",
    "eliteentertainmentnj",
    "litdjentertainment",
    "dnseventsolutions",
    "getdjohm",
    "djshilpa",
    # Dhol Players
    "dholi.jazz",
    "paulsinghsidhu",
    "jshah104",
    "dholi_meher",
    # Event Planning & Multiservice
    "tumhihoevents",
    "events_by_heena",
    "partyshartyplanners"
]

# Category keywords for classification
CATEGORY_KEYWORDS = {
    "DJ": ["dj", "djing", "music", "beats", "mixing", "turntable"],
    "Catering": ["catering", "chef", "food", "cuisine", "restaurant", "tandoor", "chaat", "bombay"],
    "Mehndi": ["mehndi", "henna", "henna art", "mehendi", "bridal"],
    "Dhol": ["dhol", "dholi", "drummer", "tabla", "percussion", "dholbeats"],
    "Singer": ["singer", "vocalist", "singing", "singer-songwriter", "live music"],
    "Dancer": ["dancer", "dancing", "bhangra", "bollywood moves", "dance crew", "garba"],
    "Decorator": ["decorator", "decor", "decoration", "floral", "event design", "mandap"],
    "Venue": ["venue", "banquet", "hall", "ballroom", "wedding venue"]
}


class InstagramVendorScraper:
    """Handles scraping and processing of Instagram vendor profiles"""
    
    def __init__(self):
        """Initialize Apify client with API token from environment"""
        self.api_token = os.getenv("APIFY_API_TOKEN")
        if not self.api_token:
            raise ValueError(
                "APIFY_API_TOKEN environment variable not set. "
                "Please set it before running this script."
            )
        self.client = ApifyClient(self.api_token)
        self.vendors = []
        self.failed_usernames = []
    
    def scrape_profile(self, username: str) -> Optional[Dict[str, Any]]:
        """
        Scrape a single Instagram profile using Apify actor
        
        Args:
            username: Instagram username to scrape
            
        Returns:
            Raw profile data or None if failed
        """
        print(f"  Scraping {username}…")
        
        try:
            # Prepare actor input with directUrls
            run_input = {
                "directUrls": [f"https://www.instagram.com/{username}/"],
                "resultsType": "posts",
                "resultsLimit": 20,
                "addParentData": True
            }
            
            # Execute the Apify actor
            run = self.client.actor("apify/instagram-scraper").call(run_input=run_input)
            
            # Extract data from actor results
            if run["status"] == "SUCCEEDED":
                dataset_id = run["defaultDatasetId"]
                items = []
                for item in self.client.dataset(dataset_id).iterate_items():
                    items.append(item)
                
                if items:
                    # Combine profile data with posts array
                    # First item should have profile data (from addParentData)
                    profile_data = items[0].copy() if items else {}
                    
                    # Create posts array from all items
                    posts = []
                    for item in items:
                        # Check if this is a post item (has caption or post id)
                        if item.get("caption") is not None or item.get("id"):
                            posts.append(item)
                    
                    profile_data["posts"] = posts
                    return profile_data
            
            return None
        
        except Exception as e:
            print(f"    ✗ Failed to scrape {username}: {str(e)}")
            return None
    
    @staticmethod
    def classify_vendor(username: str, bio: str, name: str) -> str:
        """
        Classify vendor into category based on keyword matching
        
        Args:
            username: Instagram username
            bio: Profile biography text
            name: Full name
            
        Returns:
            Category string
        """
        combined_text = f"{username} {bio} {name}".lower()
        
        # Count keyword matches per category
        scores = {}
        for category, keywords in CATEGORY_KEYWORDS.items():
            score = sum(combined_text.count(keyword) for keyword in keywords)
            scores[category] = score
        
        # Return category with highest score, default to first match
        best_category = max(scores, key=scores.get)
        return best_category if scores[best_category] > 0 else "Other"
    
    @staticmethod
    def extract_images(profile_data: Dict) -> List[str]:
        """
        Extract first 6-9 recent post image URLs from profile data
        
        Args:
            profile_data: Raw profile data from Apify
            
        Returns:
            List of image URLs
        """
        images = []
        
        # Try multiple possible locations for post data
        posts = (
            profile_data.get("posts", []) or
            profile_data.get("biographyPosts", []) or
            profile_data.get("latestPosts", []) or
            []
        )
        
        for post in posts[:9]:  # Limit to 9
            # Try different field names for image URL
            image_url = (
                post.get("imgDisplayUrl") or
                post.get("displayUrl") or
                post.get("imageUrl") or
                post.get("src")
            )
            
            if image_url and isinstance(image_url, str):
                images.append(image_url)
        
        return images[:9]  # Return max 9 images
    
    @staticmethod
    def extract_hashtags(profile_data: Dict) -> List[str]:
        """
        Extract hashtags from captions in posts (indicates skills/services)
        
        Args:
            profile_data: Raw profile data from Apify
            
        Returns:
            List of unique hashtags used
        """
        hashtags_set = set()
        
        # Get posts
        posts = (
            profile_data.get("posts", []) or
            profile_data.get("biographyPosts", []) or
            profile_data.get("latestPosts", []) or
            []
        )
        
        # Extract hashtags from all post captions
        for post in posts:
            caption = (
                post.get("caption") or
                post.get("text") or
                post.get("description") or
                ""
            )
            
            if caption:
                # Find all words starting with #
                words = caption.split()
                for word in words:
                    if word.startswith("#"):
                        # Clean hashtag (remove punctuation at end)
                        hashtag = word.rstrip(".,!?;:)")
                        hashtags_set.add(hashtag.lower())
        
        return sorted(list(hashtags_set))
    
    @staticmethod
    def extract_top_posts(profile_data: Dict) -> List[Dict[str, Any]]:
        """
        Extract most-liked posts with engagement metrics
        
        Args:
            profile_data: Raw profile data from Apify
            
        Returns:
            List of top posts with image, likes, caption, date
        """
        posts = (
            profile_data.get("posts", []) or
            profile_data.get("biographyPosts", []) or
            profile_data.get("latestPosts", []) or
            []
        )
        
        # Sort by likes (descending)
        posts_with_likes = []
        for post in posts:
            likes = post.get("likeCount") or post.get("likesCount") or post.get("likes") or 0
            posts_with_likes.append((post, likes))
        
        posts_with_likes.sort(key=lambda x: x[1], reverse=True)
        
        # Extract top 3 posts
        top_posts = []
        for post, likes in posts_with_likes[:3]:
            image_url = (
                post.get("imgDisplayUrl") or
                post.get("displayUrl") or
                post.get("imageUrl") or
                post.get("src") or
                ""
            )
            
            caption = (
                post.get("caption") or
                post.get("text") or
                post.get("description") or
                ""
            )
            
            top_posts.append({
                "image": image_url,
                "likes": likes,
                "caption": caption[:200] if caption else "",  # First 200 chars
                "timestamp": post.get("timestamp") or post.get("date") or ""
            })
        
        return top_posts
    
    def normalize_vendor(self, username: str, raw_data: Dict) -> Dict[str, Any]:
        """
        Normalize raw profile data into vendor JSON schema
        
        Args:
            username: Instagram username
            raw_data: Raw profile data from Apify
            
        Returns:
            Normalized vendor data
        """
        bio = (
            raw_data.get("biography") or 
            raw_data.get("bio") or 
            ""
        )
        
        name = (
            raw_data.get("fullName") or
            raw_data.get("full_name") or
            raw_data.get("name") or
            username
        )
        
        followers = (
            raw_data.get("followersCount") or
            raw_data.get("followers_count") or
            0
        )
        
        website = (
            raw_data.get("website") or
            raw_data.get("external_url") or
            ""
        )
        
        profile_pic = (
            raw_data.get("profilePictureUrl") or
            raw_data.get("profile_pic_url") or
            ""
        )
        
        return {
            "business_name": name,
            "instagram_username": username,
            "category": self.classify_vendor(username, bio, name),
            "location": {
                "city": "New Jersey",
                "state": "NJ",
                "country": "USA"
            },
            "bio": bio,
            "profile_picture": profile_pic,
            "images": self.extract_images(raw_data),
            "hashtags": self.extract_hashtags(raw_data),
            "top_posts": self.extract_top_posts(raw_data),
            "website": website,
            "followers": int(followers) if followers else 0,
            "claimed": False,
            "status": "draft",
            "data_sources": ["instagram"]
        }
    
    def scrape_all(self) -> None:
        """Scrape all usernames and save results"""
        print(f"\n🚀 Starting scrape of {len(INSTAGRAM_USERNAMES)} Instagram profiles…\n")
        
        for idx, username in enumerate(INSTAGRAM_USERNAMES, 1):
            print(f"[{idx}/{len(INSTAGRAM_USERNAMES)}] {username}")
            
            raw_data = self.scrape_profile(username)
            
            if raw_data:
                vendor = self.normalize_vendor(username, raw_data)
                self.vendors.append(vendor)
                print(f"    ✓ Classified as {vendor['category']}")
            else:
                self.failed_usernames.append(username)
                print(f"    ✗ Skipped")
            
            # Rate limiting between requests
            if idx < len(INSTAGRAM_USERNAMES):
                time.sleep(1)
        
        self._print_summary()
    
    def _print_summary(self) -> None:
        """Print scraping summary"""
        print(f"\n{'='*60}")
        print(f"✓ Successfully scraped: {len(self.vendors)} profiles")
        print(f"✗ Failed: {len(self.failed_usernames)} profiles")
        
        if self.failed_usernames:
            print(f"\nFailed usernames:")
            for username in self.failed_usernames:
                print(f"  - {username}")
        
        print(f"{'='*60}\n")
    
    def save_vendors(self, filename: str = "vendors.json") -> None:
        """
        Save vendor data to JSON file
        
        Args:
            filename: Output filename
        """
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(self.vendors, f, indent=2, ensure_ascii=False)
        
        print(f"💾 Saved {len(self.vendors)} vendors to {filename}")
    
    def get_category_summary(self) -> Dict[str, int]:
        """Get count of vendors by category"""
        summary = {}
        for vendor in self.vendors:
            category = vendor["category"]
            summary[category] = summary.get(category, 0) + 1
        return summary


def main():
    """Main entry point"""
    try:
        scraper = InstagramVendorScraper()
        scraper.scrape_all()
        scraper.save_vendors()
        
        # Print category distribution
        summary = scraper.get_category_summary()
        print("📊 Vendor Distribution by Category:")
        for category, count in sorted(summary.items(), key=lambda x: x[1], reverse=True):
            print(f"  {category}: {count}")
        
        return 0
    
    except KeyboardInterrupt:
        print("\n\n⚠️  Scraping interrupted by user")
        return 1
    except Exception as e:
        print(f"\n\n❌ Fatal error: {str(e)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
