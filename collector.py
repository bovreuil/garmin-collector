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
                    activity_details = api.get_activity(activity_id)
                    
                    # Extract key data
                    activity_data = {
                        'activity_id': activity_id,
                        'date': target_date,
                        'activity_name': activity.get('activityName', ''),
                        'activity_type': activity.get('activityType', {}).get('typeKey', ''),
                        'start_time_local': activity.get('startTimeLocal'),
                        'duration_seconds': activity.get('elapsedDuration'),
                        'distance_meters': activity.get('distance'),
                        'elevation_gain': activity.get('elevationGain'),
                        'average_hr': activity.get('averageHR'),
                        'max_hr': activity.get('maxHR'),
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
    
    def extract_heart_rate_series(self, activity_details: Dict) -> List[List]:
        """Extract heart rate series from activity details."""
        # This is a simplified version - you may need to adapt based on your specific needs
        hr_series = []
        
        if 'activityDetailMetrics' in activity_details:
            metrics = activity_details['activityDetailMetrics']
            if 'metricDescriptors' in metrics and 'metrics' in metrics:
                # Find heart rate position
                hr_pos = None
                for descriptor in metrics['metricDescriptors']:
                    if descriptor.get('key') == 'directHeartRate':
                        hr_pos = descriptor.get('metricsIndex')
                        break
                
                if hr_pos is not None and 'metrics' in metrics:
                    # Extract heart rate data
                    for metric in metrics['metrics']:
                        if len(metric) > hr_pos:
                            timestamp = metric[0]  # Assuming first column is timestamp
                            hr_value = metric[hr_pos]
                            hr_series.append([timestamp, hr_value])
        
        return hr_series
    
    def extract_breathing_rate_series(self, activity_details: Dict) -> List[List]:
        """Extract breathing rate series from activity details."""
        breathing_series = []
        
        if 'activityDetailMetrics' in activity_details:
            metrics = activity_details['activityDetailMetrics']
            if 'metricDescriptors' in metrics and 'metrics' in metrics:
                # Find breathing rate position
                breathing_pos = None
                for descriptor in metrics['metricDescriptors']:
                    if descriptor.get('key') == 'directRespirationRate':
                        breathing_pos = descriptor.get('metricsIndex')
                        break
                
                if breathing_pos is not None and 'metrics' in metrics:
                    # Extract breathing rate data
                    for metric in metrics['metrics']:
                        if len(metric) > breathing_pos:
                            timestamp = metric[0]  # Assuming first column is timestamp
                            breathing_value = metric[breathing_pos]
                            breathing_series.append([timestamp, breathing_value])
        
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
