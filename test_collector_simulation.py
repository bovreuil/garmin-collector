#!/usr/bin/env python3
"""
Test collector.py logic with prod data to find the issue
"""

import json
import logging
import os
from dotenv import load_dotenv
from garminconnect import Garmin

# Setup logging to match collector
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

# Load prod data
with open('prod_activity_details.json', 'r') as f:
    prod_activity_details = json.load(f)

print("\n=== Simulating Collector Logic ===\n")

# Simulate what collector.py does
def extract_heart_rate_series(activity_details):
    """Copy of collector.py logic"""
    hr_series = []
    
    if 'activityDetailMetrics' in activity_details:
        activity_metrics = activity_details['activityDetailMetrics']
        if activity_metrics:
            logger.info(f"Found activityDetailMetrics with {len(activity_metrics)} entries")
            
            # Detect positions
            hr_pos, ts_pos = detect_hr_and_timestamp_positions(activity_details)
            
            if hr_pos is not None and ts_pos is not None:
                logger.info(f"Selected HR position {hr_pos}, Timestamp position {ts_pos}")
                
                # Get factor
                hr_factor = 1.0
                for descriptor in activity_details.get('metricDescriptors', []):
                    if descriptor.get('key') == 'directHeartRate':
                        hr_factor = descriptor.get('unit', {}).get('factor', 1.0)
                        logger.info(f"Using HR factor: {hr_factor}")
                        break
                
                # Extract
                hr_values_checked = 0
                hr_values_filtered = 0
                max_hr = 200
                
                for entry in activity_metrics:
                    if 'metrics' in entry and len(entry['metrics']) > max(hr_pos, ts_pos):
                        metrics = entry['metrics']
                        timestamp = metrics[ts_pos]
                        hr_value = metrics[hr_pos]
                        
                        if timestamp is not None and hr_value is not None:
                            hr_values_checked += 1
                            actual_hr_value = hr_value * hr_factor
                            
                            if hr_values_checked <= 5:
                                logger.info(f"Sample HR value {hr_values_checked}: raw={hr_value}, actual={actual_hr_value} (factor={hr_factor})")
                            
                            if actual_hr_value > max_hr:
                                if hr_values_checked <= 10:
                                    logger.info(f"Filtering HR reading {actual_hr_value} above max HR {max_hr}")
                                hr_values_filtered += 1
                                continue
                            
                            hr_series.append([timestamp, int(actual_hr_value)])
                
                logger.info(f"Checked {hr_values_checked} HR values, filtered {hr_values_filtered}, extracted {len(hr_series)}")
            else:
                logger.warning("Could not find HR and timestamp positions")
        else:
            logger.warning("No activityDetailMetrics data")
    else:
        logger.warning("No activityDetailMetrics in activity details")
    
    return hr_series

def detect_hr_and_timestamp_positions(activity_details):
    """Copy of collector.py logic"""
    if not activity_details:
        return None, None
    
    metric_descriptors = activity_details.get('metricDescriptors', [])
    if not metric_descriptors:
        logger.warning("No metricDescriptors found in activity details")
        return None, None
    
    logger.info(f"Found {len(metric_descriptors)} metric descriptors")
    
    hr_position = None
    ts_position = None
    
    for descriptor in metric_descriptors:
        metrics_index = descriptor.get('metricsIndex')
        key = descriptor.get('key')
        unit = descriptor.get('unit', {})
        unit_key = unit.get('key', 'unknown')
        factor = unit.get('factor', 1.0)
        
        logger.info(f"Index {metrics_index}: {key} ({unit_key}, factor={factor})")
        
        if key == 'directHeartRate':
            hr_position = metrics_index
            logger.info(f"Found HR at position {hr_position} (unit: {unit_key}, factor: {factor})")
        elif key == 'directTimestamp':
            ts_position = metrics_index
            logger.info(f"Found timestamp at position {ts_position} (unit: {unit_key}, factor: {factor})")
    
    if hr_position is None:
        logger.warning("No directHeartRate found in metricDescriptors")
    
    if ts_position is None:
        logger.warning("No directTimestamp found in metricDescriptors")
    
    return hr_position, ts_position

# Test with prod data
hr_series = extract_heart_rate_series(prod_activity_details)
print(f"\n=== RESULT ===")
print(f"Extracted {len(hr_series)} HR points from prod data")

