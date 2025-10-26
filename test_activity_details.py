#!/usr/bin/env python3
"""
Test script to run on prod to diagnose activity details issue
Run this on prod with: python test_activity_details.py
"""

import json
import sys
import os
from dotenv import load_dotenv

load_dotenv()

print("=== Environment Check ===")
print(f"Python version: {sys.version}")
print(f"Python executable: {sys.executable}")

try:
    import garminconnect
    print(f"garminconnect imported successfully")
    print(f"garminconnect location: {garminconnect.__file__}")
except Exception as e:
    print(f"Failed to import garminconnect: {e}")
    sys.exit(1)

try:
    import pip
    pkgs = sorted([f"{p.key}=={p.version}" for p in pip.get_installed_distributions() 
                   if 'garmin' in p.key.lower() or 'garth' in p.key.lower()])
    if pkgs:
        print("\nInstalled packages:")
        for pkg in pkgs:
            print(f"  {pkg}")
    else:
        print("\nNo garmin/garth packages found")
except:
    pass

print("\n=== Testing Activity Details ===")

try:
    from garminconnect import Garmin
    
    api = Garmin(os.getenv('GARMIN_EMAIL'), os.getenv('GARMIN_PASSWORD'))
    api.login()
    print("✓ Login successful")
    
    # Test activity ID from your example
    activity_id = "20802404048"
    print(f"\nFetching activity details for ID: {activity_id}")
    
    activity_details = api.get_activity_details(activity_id)
    
    print(f"\n✓ Retrieved activity details")
    print(f"Top-level keys: {list(activity_details.keys())[:10]}")
    
    # Check for activityDetailMetrics
    has_metrics = 'activityDetailMetrics' in activity_details
    print(f"\nHas 'activityDetailMetrics': {has_metrics}")
    
    if has_metrics:
        metrics = activity_details['activityDetailMetrics']
        print(f"  Type: {type(metrics)}")
        print(f"  Length: {len(metrics) if isinstance(metrics, (list, dict)) else 'N/A'}")
        
        if isinstance(metrics, list) and len(metrics) > 0:
            print(f"  ✓ Has data entries")
            print(f"  First entry has 'metrics': {'metrics' in metrics[0]}")
            if 'metrics' in metrics[0]:
                print(f"  First metrics array length: {len(metrics[0]['metrics'])}")
        elif isinstance(metrics, list):
            print(f"  ⚠ Empty list!")
        elif isinstance(metrics, dict):
            print(f"  Type is dict (unexpected)")
    
    # Check metricDescriptors
    has_descriptors = 'metricDescriptors' in activity_details
    print(f"\nHas 'metricDescriptors': {has_descriptors}")
    
    if has_descriptors:
        descriptors = activity_details['metricDescriptors']
        print(f"  Count: {len(descriptors)}")
        print(f"  First 5 descriptors:")
        for desc in descriptors[:5]:
            key = desc.get('key', 'unknown')
            idx = desc.get('metricsIndex', 'unknown')
            print(f"    - {key} at index {idx}")
    
    # Save to file for inspection
    with open('prod_activity_details.json', 'w') as f:
        json.dump(activity_details, f, indent=2, default=str)
    print(f"\n✓ Saved full response to prod_activity_details.json")
    
    print("\n=== Result ===")
    if has_metrics and isinstance(metrics, list) and len(metrics) > 0:
        print("✓ activityDetailMetrics is populated - issue is likely in extraction logic")
    else:
        print("✗ activityDetailMetrics is missing/empty - API is returning incomplete data")
        print("  Possible causes:")
        print("    1. Different garminconnect version on prod")
        print("    2. Garmin API rate limiting")
        print("    3. Network/firewall issues")
        print("    4. Authentication issues")
        
except Exception as e:
    print(f"\n✗ Error: {e}")
    import traceback
    traceback.print_exc()
