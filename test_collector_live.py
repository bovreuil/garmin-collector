#!/usr/bin/env python3
"""
Test the actual collector.py with live data on prod
"""

import sys
import os
import logging
from dotenv import load_dotenv

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

# Import collector
from collector import GarminCollector

# Create collector instance
collector = GarminCollector(
    server_url=os.getenv('REHAB_PLATFORM_URL', 'http://localhost:5001'),
    shared_secret=os.getenv('SHARED_SECRET', 'dummy')
)

# Test collecting data for the date we know has data
target_date = "2025-10-26"
print(f"\n=== Testing collector.collect_garmin_data for {target_date} ===\n")

result = collector.collect_garmin_data(target_date, job_id="test")

print(f"\n=== RESULT ===")
print(f"Success: {result.get('success')}")
print(f"Data found: {result.get('data_found')}")

if 'activities' in result:
    print(f"\nActivities: {len(result['activities'])}")
    for i, activity in enumerate(result['activities']):
        hr_series_len = len(activity.get('heart_rate_series', []))
        print(f"  Activity {i+1}: {activity.get('activity_name')}")
        print(f"    HR series points: {hr_series_len}")
        print(f"    Activity ID: {activity.get('activity_id')}")

