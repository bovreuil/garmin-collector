#!/usr/bin/env python3
"""
Test extraction logic with prod data
"""

import json
import os
from dotenv import load_dotenv
from garminconnect import Garmin

load_dotenv()

# Load the prod data
with open('prod_activity_details.json', 'r') as f:
    activity_details = json.load(f)

print("Testing extraction logic...")

# Detect positions
def detect_positions(activity_details):
    metric_descriptors = activity_details.get('metricDescriptors', [])
    
    hr_position = None
    ts_position = None
    
    for descriptor in metric_descriptors:
        key = descriptor.get('key')
        if key == 'directHeartRate':
            hr_position = descriptor.get('metricsIndex')
        elif key == 'directTimestamp':
            ts_position = descriptor.get('metricsIndex')
    
    return hr_position, ts_position

hr_pos, ts_pos = detect_positions(activity_details)
print(f"HR position: {hr_pos}, Timestamp position: {ts_pos}")

# Extract data
if hr_pos is not None and ts_pos is not None:
    activity_metrics = activity_details.get('activityDetailMetrics', [])
    print(f"Total metrics entries: {len(activity_metrics)}")
    
    hr_series = []
    hr_factor = 1.0
    
    # Get factor
    for descriptor in activity_details.get('metricDescriptors', []):
        if descriptor.get('key') == 'directHeartRate':
            hr_factor = descriptor.get('unit', {}).get('factor', 1.0)
            break
    
    print(f"HR factor: {hr_factor}")
    
    # Extract
    for entry in activity_metrics:
        if 'metrics' in entry and len(entry['metrics']) > max(hr_pos, ts_pos):
            metrics = entry['metrics']
            timestamp = metrics[ts_pos]
            hr_value = metrics[hr_pos]
            
            if timestamp is not None and hr_value is not None:
                actual_hr = hr_value * hr_factor
                if actual_hr <= 200:  # Filter
                    hr_series.append([timestamp, int(actual_hr)])
    
    print(f"Extracted {len(hr_series)} HR points")
    if hr_series:
        print(f"First 5: {hr_series[:5]}")
        print(f"Last 5: {hr_series[-5:]}")
else:
    print("Could not find positions!")

