#!/usr/bin/env python3
"""
Garmin Data Collector

This module handles polling the rehab-platform server for jobs and collecting
Garmin data when jobs are available.
"""

import json
import logging
import os
import requests
import time
from datetime import datetime
from typing import Dict, Optional, List
from garminconnect import Garmin
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class GarminCollector:
    """Handles Garmin data collection and job processing."""
    
    def __init__(self, server_url: str, shared_secret: str):
        """
        Initialize the Garmin collector.
        
        Args:
            server_url: URL of the rehab-platform server
            shared_secret: Shared secret for authentication
        """
        self.server_url = server_url.rstrip('/')
        self.shared_secret = shared_secret
        self.garmin_email = os.getenv('GARMIN_EMAIL')
        self.garmin_password = os.getenv('GARMIN_PASSWORD')
        
        if not self.garmin_email or not self.garmin_password:
            raise ValueError("GARMIN_EMAIL and GARMIN_PASSWORD must be set in environment")
        
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {shared_secret}',
            'Content-Type': 'application/json'
        })
    
    def poll_for_jobs(self) -> List[Dict]:
        """
        Poll the server for pending jobs.
        
        Returns:
            List of job dictionaries
        """
        try:
            response = self.session.get(f"{self.server_url}/api/jobs/pending")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to poll for jobs: {e}")
            return []
    
    def update_job_status(self, job_id: str, status: str, result: Optional[Dict] = None, error_message: Optional[str] = None):
        """
        Update job status on the server.
        
        Args:
            job_id: Job identifier
            status: New status (running, completed, failed)
            result: Optional result data
            error_message: Optional error message
        """
        try:
            payload = {
                'status': status,
                'updated_at': datetime.now().isoformat()
            }
            
            if result is not None:
                payload['result'] = json.dumps(result)
            
            if error_message is not None:
                payload['error_message'] = error_message
            
            response = self.session.post(f"{self.server_url}/api/jobs/{job_id}/status", json=payload)
            response.raise_for_status()
            logger.info(f"Updated job {job_id} status to {status}")
            
        except requests.RequestException as e:
            logger.error(f"Failed to update job status: {e}")
    
    def collect_garmin_data(self, target_date: str, job_id: str) -> Dict:
        """
        Collect Garmin data for a specific date.
        
        Args:
            target_date: Date to collect data for (YYYY-MM-DD)
            job_id: Job identifier
            
        Returns:
            Dictionary with collection results
        """
        logger.info(f"Starting data collection for {target_date}")
        
        try:
            # Connect to Garmin
            api = Garmin(self.garmin_email, self.garmin_password)
            api.login()
            logger.info(f"Connected to Garmin with email {self.garmin_email}")
            
            # Get heart rate data
            logger.info(f"Fetching heart rate data for {target_date}")
            heart_rate_data = api.get_heart_rates(target_date)
            
            if not heart_rate_data or 'heartRateValues' not in heart_rate_data:
                return {
                    'success': False,
                    'message': f"No heart rate data found for {target_date}",
                    'data_found': False
                }
            
            hr_series = heart_rate_data['heartRateValues']
            logger.info(f"Collected {len(hr_series)} heart rate points")
            
            # Get activities for the date
            logger.info(f"Fetching activities for {target_date}")
            activities = self.collect_activities_for_date(api, target_date)
            
            # Prepare data for upload to server
            result_data = {
                'success': True,
                'data_found': True,
                'heart_rate_data': {
                    'date': target_date,
                    'heartRateValues': hr_series
                },
                'activities': activities,
                'message': f"Successfully collected data for {target_date}"
            }
            
            logger.info(f"Data collection completed for {target_date}")
            return result_data
            
        except Exception as e:
            logger.error(f"Error collecting data for {target_date}: {e}")
            return {
                'success': False,
                'message': f"Error collecting data: {str(e)}",
                'data_found': False
            }
    
    def collect_activities_for_date(self, api: Garmin, target_date: str) -> List[Dict]:
        """
        Collect activities for a specific date.
        
        Args:
            api: Garmin API instance
            target_date: Date to collect activities for
            
        Returns:
            List of activity data
        """
        logger.info(f"Collecting activities for {target_date}")
        
        try:
            # Get activities for the date
            activities = api.get_activities_fordate(target_date)
            
            # Handle new API structure
            if isinstance(activities, dict) and 'ActivitiesForDay' in activities:
                afd = activities['ActivitiesForDay']
                if isinstance(afd, dict) and 'payload' in afd:
                    activities = afd['payload']
            
            if not activities:
                logger.info(f"No activities found for {target_date}")
                return []
            
            logger.info(f"Found {len(activities)} activities for {target_date}")
            
            # Process each activity
            processed_activities = []
            for activity in activities:
                activity_id = activity.get('activityId')
                if not activity_id:
                    continue
                
                logger.info(f"Processing activity {activity_id}")
                
                # Get detailed activity data
                try:
                    activity_details = api.get_activity_details(activity_id)
                    
                    # Extract key data (matching working version field names)
                    activity_data = {
                        'activity_id': activity_id,
                        'date': target_date,
                        'activity_name': activity.get('activityName', 'Unknown Activity'),
                        'activity_type': activity.get('activityType', 'unknown'),
                        'start_time_local': activity.get('startTimeLocal', ''),
                        'duration_seconds': activity.get('duration', 0),
                        'distance_meters': activity.get('distance', 0),
                        'elevation_gain': activity.get('elevationGain', 0),
                        'average_hr': activity.get('averageHR', 0),
                        'max_hr': activity.get('maxHR', 0),
                        'heart_rate_series': [],
                        'breathing_rate_series': [],
                        'trimp_data': {},
                        'total_trimp': 0.0
                    }
                    
                    # Extract heart rate series if available
                    if activity_details and 'activityDetailMetrics' in activity_details:
                        hr_series = self.extract_heart_rate_series(activity_details)
                        breathing_series = self.extract_breathing_rate_series(activity_details)
                        
                        activity_data['heart_rate_series'] = hr_series
                        activity_data['breathing_rate_series'] = breathing_series
                        
                        logger.info(f"Extracted {len(hr_series)} HR points and {len(breathing_series)} breathing points for activity {activity_id}")
                    
                    processed_activities.append(activity_data)
                    
                except Exception as e:
                    logger.warning(f"Failed to get details for activity {activity_id}: {e}")
                    continue
            
            return processed_activities
            
        except Exception as e:
            logger.error(f"Error collecting activities for {target_date}: {e}")
            return []
    
    def detect_hr_and_timestamp_positions(self, activity_details: Dict) -> tuple:
        """
        Detect HR and timestamp positions using metricDescriptors from activity details.
        Adapted from the working version in jobs.py.
        """
        if not activity_details:
            return None, None
        
        # Get metricDescriptors from activity details
        metric_descriptors = activity_details.get('metricDescriptors', [])
        if not metric_descriptors:
            logger.warning("No metricDescriptors found in activity details")
            return None, None
        
        logger.info(f"Found {len(metric_descriptors)} metric descriptors")
        
        # Find HR and timestamp positions using the key
        hr_position = None
        ts_position = None
        
        for descriptor in metric_descriptors:
            metrics_index = descriptor.get('metricsIndex')
            key = descriptor.get('key')
            unit = descriptor.get('unit', {})
            unit_key = unit.get('key', 'unknown')
            factor = unit.get('factor', 1.0)
            
            logger.info(f"Index {metrics_index}: {key} ({unit_key}, factor={factor})")
            
            # Look for heart rate data
            if key == 'directHeartRate':
                hr_position = metrics_index
                logger.info(f"Found HR at position {hr_position} (unit: {unit_key}, factor: {factor})")
            
            # Look for timestamp data
            elif key == 'directTimestamp':
                ts_position = metrics_index
                logger.info(f"Found timestamp at position {ts_position} (unit: {unit_key}, factor: {factor})")
        
        if hr_position is None:
            logger.warning("No directHeartRate found in metricDescriptors")
        
        if ts_position is None:
            logger.warning("No directTimestamp found in metricDescriptors")
        
        return hr_position, ts_position

    def detect_breathing_rate_position(self, activity_details: Dict) -> Optional[int]:
        """
        Detect breathing rate position in activity metrics.
        Adapted from the working version in jobs.py.
        """
        logger.info("Analyzing activity details for breathing rate")
        
        # Get metric descriptors
        metric_descriptors = activity_details.get('metricDescriptors', [])
        if not metric_descriptors:
            logger.warning("No metricDescriptors found")
            return None
        
        # Find breathing rate position
        breathing_position = None
        for descriptor in metric_descriptors:
            metrics_index = descriptor.get('metricsIndex')
            key = descriptor.get('key')
            
            if key == 'directRespirationRate':
                breathing_position = metrics_index
                logger.info(f"Found breathing rate at position {breathing_position}")
                break
        
        if breathing_position is None:
            logger.warning("No directRespirationRate found in metricDescriptors")
        
        return breathing_position

    def extract_heart_rate_series(self, activity_details: Dict) -> List[List]:
        """Extract heart rate series from activity details using sophisticated logic from working version."""
        hr_series = []
        
        if 'activityDetailMetrics' in activity_details:
            activity_metrics = activity_details['activityDetailMetrics']
            if activity_metrics:
                logger.info(f"Found activityDetailMetrics with {len(activity_metrics)} entries")
                
                # Use the sophisticated HR detection function
                hr_pos, ts_pos = self.detect_hr_and_timestamp_positions(activity_details)
                
                if hr_pos is not None and ts_pos is not None:
                    logger.info(f"Selected HR position {hr_pos}, Timestamp position {ts_pos}")
                    
                    # Get the factor for HR values from metricDescriptors
                    hr_factor = 1.0
                    for descriptor in activity_details.get('metricDescriptors', []):
                        if descriptor.get('key') == 'directHeartRate':
                            hr_factor = descriptor.get('unit', {}).get('factor', 1.0)
                            logger.info(f"Using HR factor: {hr_factor}")
                            break
                    
                    # Extract HR time series with filtering
                    hr_values_checked = 0
                    hr_values_filtered = 0
                    
                    # Get user's HR parameters for filtering (we'll use reasonable defaults)
                    max_hr = 200  # Default max HR for filtering
                    
                    for entry in activity_metrics:
                        if 'metrics' in entry and len(entry['metrics']) > max(hr_pos, ts_pos):
                            metrics = entry['metrics']
                            timestamp = metrics[ts_pos]
                            hr_value = metrics[hr_pos]
                            
                            if timestamp is not None and hr_value is not None:
                                hr_values_checked += 1
                                
                                # Apply the factor to get the actual HR value
                                actual_hr_value = hr_value * hr_factor
                                
                                # Log first few HR values for debugging
                                if hr_values_checked <= 5:
                                    logger.info(f"Sample HR value {hr_values_checked}: raw={hr_value}, actual={actual_hr_value} (factor={hr_factor})")
                                
                                # Skip HR readings above max HR (likely sensor artifacts)
                                if actual_hr_value > max_hr:
                                    if hr_values_checked <= 10:  # Log first 10 filtered values
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
    
    def extract_breathing_rate_series(self, activity_details: Dict) -> List[List]:
        """Extract breathing rate series from activity details using sophisticated logic."""
        breathing_series = []
        
        if 'activityDetailMetrics' in activity_details:
            activity_metrics = activity_details['activityDetailMetrics']
            if activity_metrics:
                # Detect breathing rate position
                breathing_pos = self.detect_breathing_rate_position(activity_details)
                hr_pos, ts_pos = self.detect_hr_and_timestamp_positions(activity_details)
                
                if breathing_pos is not None and ts_pos is not None:
                    logger.info(f"Selected breathing rate position {breathing_pos}")
                    
                    breathing_values_checked = 0
                    
                    for entry in activity_metrics:
                        if 'metrics' in entry and len(entry['metrics']) > max(breathing_pos, ts_pos):
                            metrics = entry['metrics']
                            timestamp = metrics[ts_pos]
                            breathing_value = metrics[breathing_pos]
                            
                            if timestamp is not None and breathing_value is not None:
                                breathing_values_checked += 1
                                
                                # Log first few breathing values for debugging
                                if breathing_values_checked <= 5:
                                    logger.info(f"Sample breathing value {breathing_values_checked}: {breathing_value}")
                                
                                breathing_series.append([timestamp, float(breathing_value)])
                    
                    logger.info(f"Checked {breathing_values_checked} breathing values, extracted {len(breathing_series)}")
                else:
                    logger.warning("Could not find breathing rate and timestamp positions")
            else:
                logger.warning("No activityDetailMetrics data")
        else:
            logger.warning("No activityDetailMetrics in activity details")
        
        return breathing_series
    
    def upload_data_to_server(self, job_id: str, data: Dict) -> bool:
        """
        Upload collected data to the server.
        
        Args:
            job_id: Job identifier
            data: Collected data
            
        Returns:
            True if successful, False otherwise
        """
        try:
            response = self.session.post(f"{self.server_url}/api/jobs/{job_id}/data", json=data)
            response.raise_for_status()
            logger.info(f"Successfully uploaded data for job {job_id}")
            return True
            
        except requests.RequestException as e:
            logger.error(f"Failed to upload data for job {job_id}: {e}")
            return False
    
    def run_job(self, job: Dict):
        """
        Run a single job.
        
        Args:
            job: Job dictionary with job details
        """
        job_id = job['job_id']
        target_date = job.get('target_date')
        
        logger.info(f"Starting job {job_id} for date {target_date}")
        
        # Update job status to running
        self.update_job_status(job_id, 'running')
        
        try:
            # Collect data
            result = self.collect_garmin_data(target_date, job_id)
            
            if result['success']:
                # Upload data to server
                upload_success = self.upload_data_to_server(job_id, result)
                
                if upload_success:
                    self.update_job_status(job_id, 'completed', result)
                    logger.info(f"Job {job_id} completed successfully")
                else:
                    self.update_job_status(job_id, 'failed', error_message="Failed to upload data")
                    logger.error(f"Job {job_id} failed to upload data")
            else:
                self.update_job_status(job_id, 'completed', result)
                logger.info(f"Job {job_id} completed with no data found")
                
        except Exception as e:
            logger.error(f"Job {job_id} failed with error: {e}")
            self.update_job_status(job_id, 'failed', error_message=str(e))
    
    def run_polling_loop(self, poll_interval: int = 60):
        """
        Run the main polling loop.
        
        Args:
            poll_interval: Seconds between polls
        """
        logger.info(f"Starting polling loop with {poll_interval}s interval")
        logger.info(f"Server URL: {self.server_url}")
        logger.info(f"Garmin email: {self.garmin_email}")
        
        while True:
            try:
                # Poll for jobs
                jobs = self.poll_for_jobs()
                
                if jobs:
                    logger.info(f"Found {len(jobs)} pending jobs")
                    
                    # Process each job
                    for job in jobs:
                        self.run_job(job)
                else:
                    logger.debug("No pending jobs found")
                
                # Wait before next poll
                time.sleep(poll_interval)
                
            except KeyboardInterrupt:
                logger.info("Polling loop interrupted by user")
                break
            except Exception as e:
                logger.error(f"Error in polling loop: {e}")
                time.sleep(poll_interval)


def main():
    """Main entry point."""
    server_url = os.getenv('REHAB_PLATFORM_URL', 'http://localhost:5001')
    shared_secret = os.getenv('SHARED_SECRET')
    poll_interval = int(os.getenv('POLL_INTERVAL', '60'))
    
    if not shared_secret:
        logger.error("SHARED_SECRET environment variable is required")
        return
    
    try:
        collector = GarminCollector(server_url, shared_secret)
        collector.run_polling_loop(poll_interval)
    except Exception as e:
        logger.error(f"Failed to start collector: {e}")


if __name__ == '__main__':
    main()
