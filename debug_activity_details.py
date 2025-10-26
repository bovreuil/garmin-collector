#!/usr/bin/env python3
"""
Debug script to examine activity details structure
"""

import json
import logging
import os
from dotenv import load_dotenv
from garminconnect import Garmin

# Setup logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

load_dotenv()

def main():
    # Connect to Garmin
    api = Garmin(os.getenv('GARMIN_EMAIL'), os.getenv('GARMIN_PASSWORD'))
    api.login()
    
    # Get activities for a specific date (2025-10-26 from your example)
    target_date = "2025-10-26"
    activities = api.get_activities_fordate(target_date)
    
    # Handle new API structure
    if isinstance(activities, dict) and 'ActivitiesForDay' in activities:
        afd = activities['ActivitiesForDay']
        if isinstance(afd, dict) and 'payload' in afd:
            activities = afd['payload']
    
    if not activities:
        print("No activities found")
        return
    
    print(f"\nFound {len(activities)} activities for {target_date}\n")
    
    # Process first activity
    activity = activities[0]
    activity_id = activity.get('activityId')
    print(f"Processing activity ID: {activity_id}")
    print(f"Activity Name: {activity.get('activityName')}")
    print(f"Duration: {activity.get('duration')} seconds")
    
    # Get activity details
    print(f"\nFetching activity details...")
    activity_details = api.get_activity_details(activity_id)
    
    # Save full response to file for inspection
    with open('debug_activity_details.json', 'w') as f:
        json.dump(activity_details, f, indent=2, default=str)
    
    print(f"\nSaved full response to debug_activity_details.json")
    
    # Analyze key fields
    print(f"\n=== Analysis ===")
    print(f"Has 'activityDetailMetrics': {'activityDetailMetrics' in activity_details}")
    
    if 'activityDetailMetrics' in activity_details:
        metrics = activity_details['activityDetailMetrics']
        print(f"  Type: {type(metrics)}")
        print(f"  Length: {len(metrics) if isinstance(metrics, (list, dict)) else 'N/A'}")
        
        if isinstance(metrics, list) and len(metrics) > 0:
            print(f"  First entry keys: {list(metrics[0].keys()) if isinstance(metrics[0], dict) else 'N/A'}")
            if isinstance(metrics[0], dict) and 'metrics' in metrics[0]:
                print(f"  First metrics length: {len(metrics[0]['metrics'])}")
    
    print(f"\nHas 'metricDescriptors': {'metricDescriptors' in activity_details}")
    if 'metricDescriptors' in activity_details:
        descriptors = activity_details['metricDescriptors']
        print(f"  Count: {len(descriptors)}")
        print(f"  Descriptors:")
        for desc in descriptors[:10]:  # First 10
            key = desc.get('key', 'unknown')
            index = desc.get('metricsIndex', 'unknown')
            print(f"    - {key} at index {index}")
    
    # Check for other potential metric fields
    print(f"\nOther top-level keys in activity_details:")
    for key in list(activity_details.keys())[:20]:
        print(f"  - {key}")

if __name__ == '__main__':
    main()
