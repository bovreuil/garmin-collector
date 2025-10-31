#!/usr/bin/env python3
"""
Compare collection results between dev and prod environments
"""

import json
import logging
import os
import sys
from dotenv import load_dotenv
from collector import GarminCollector

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

# Create collector
server_url = os.getenv('REHAB_PLATFORM_URL', 'http://localhost:5001')
shared_secret = os.getenv('SHARED_SECRET', 'dummy')

if not shared_secret or shared_secret == 'dummy':
    logger.error("SHARED_SECRET must be set")
    sys.exit(1)

collector = GarminCollector(server_url, shared_secret)

# Test date
target_date = "2025-10-26"

print(f"=== Collecting data for {target_date} ===")

result = collector.collect_garmin_data(target_date, job_id="comparison_test")

# Save result
output_file = f"collection_result.json"
with open(output_file, 'w') as f:
    json.dump(result, f, indent=2, default=str)

print(f"\n=== Collection Complete ===")
print(f"Saved to: {output_file}")
print(f"Success: {result.get('success')}")
print(f"Data found: {result.get('data_found')}")

# Analyze
if 'heart_rate_data' in result:
    hr_data = result['heart_rate_data']
    hr_points = len(hr_data.get('heartRateValues', []))
    print(f"Day HR points: {hr_points}")

if 'activities' in result:
    print(f"\nActivities: {len(result['activities'])}")
    for i, activity in enumerate(result['activities']):
        hr_len = len(activity.get('heart_rate_series', []))
        breathing_len = len(activity.get('breathing_rate_series', []))
        print(f"  {i+1}. {activity.get('activity_name')}")
        print(f"     HR series: {hr_len} points")
        print(f"     Breathing series: {breathing_len} points")

