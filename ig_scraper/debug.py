#!/usr/bin/env python3
import json
from apify_client import ApifyClient
import os

api_token = os.getenv('APIFY_API_TOKEN')
client = ApifyClient(api_token)

run_input = {
    'directUrls': ['https://www.instagram.com/djsuhel/'],
    'resultsType': 'posts',
    'resultsLimit': 20,
    'addParentData': True,
}

run = client.actor('apify/instagram-scraper').call(run_input=run_input)

# Get items
all_items = []
for item in client.dataset(run['defaultDatasetId']).iterate_items():
    all_items.append(item)

print(f'Total items returned: {len(all_items)}')
for i, item in enumerate(all_items[:3]):
    print(f'\nItem {i}:')
    if item.get('biography'):
        print(f'  Profile: {item.get("fullName")} - {item.get("followersCount")} followers')
        print(f'  Has posts key: {"posts" in item}')
    else:
        print(f'  Post: {item.get("caption")[:50] if item.get("caption") else "No caption"}')
        print(f'  Likes: {item.get("likesCount")}')
        print(f'  Has hashtags: {item.get("hashtags")}')
