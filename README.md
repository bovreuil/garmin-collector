# Garmin Data Collector

This is a standalone service that polls the rehab-platform server for Garmin data collection jobs and fetches data from Garmin Connect.

## Overview

The garmin-collector runs independently from the rehab-platform and handles all Garmin API interactions. It:

1. Polls the rehab-platform server for pending jobs
2. Connects to Garmin Connect using stored credentials
3. Fetches heart rate, stress, body battery, and activity data for requested dates
4. Uploads the collected data back to the server

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Copy the example environment file and fill in your details:

```bash
cp env.example .env
```

Edit `.env` with your configuration:

```env
# Garmin Connect credentials
GARMIN_EMAIL=your.email@example.com
GARMIN_PASSWORD=your_password

# Rehab Platform server configuration
REHAB_PLATFORM_URL=http://localhost:5001
SHARED_SECRET=your_shared_secret_here

# Polling configuration (seconds between polls)
POLL_INTERVAL=60
```

**Important**: Make sure the `SHARED_SECRET` matches the one configured in the rehab-platform's `config.py` file.

### 3. Run the Collector

```bash
python collector.py --poll
```

The collector will start polling the server every 60 seconds (or whatever you set in `POLL_INTERVAL`).

## How It Works

### Job Polling

The collector periodically checks the rehab-platform server for pending jobs by calling:

```
GET /api/jobs/pending
```

### Data Collection

When a job is found, the collector:

1. Updates the job status to "running"
2. Connects to Garmin Connect
3. Fetches heart rate data for the target date (2-minute intervals)
4. Fetches all-day stress and body battery data for the target date (3-minute intervals, via `get_all_day_stress()` endpoint)
5. Fetches activity data for the target date (HR, breathing rate, metadata)
6. Uploads the collected data to the server
7. Updates the job status to "completed" or "failed"

**Data Collection Details**:
- **Heart Rate**: Daily whole-day data at 2-minute intervals
- **Stress**: Daily stress level time series at 3-minute intervals (typically ~480 points per day)
- **Body Battery**: Daily body battery level and status time series at 3-minute intervals (typically ~480 points per day)
- **Activities**: Per-activity data including HR time series, breathing rate, and metadata

**Error Handling**:
- If stress/body battery data collection fails, the collector logs a warning but continues processing heart rate and activity data
- Collection failures don't block the upload of other successfully collected data

### Data Upload

Collected data is uploaded to the server via:

```
POST /api/jobs/{job_id}/data
```

## API Endpoints

The collector communicates with the rehab-platform using these endpoints:

* `GET /api/jobs/pending` - Get pending jobs
* `POST /api/jobs/{job_id}/status` - Update job status
* `POST /api/jobs/{job_id}/data` - Upload collected data

All requests require authentication using the shared secret in the `Authorization: Bearer {secret}` header.

## Deployment

This collector is designed to run on a local machine (like your Windows 11 mini-ITX) where it can access Garmin Connect without IP restrictions.

### Development Environment

**Local Development (macOS)**:
- Run manually: `python collector.py --poll`
- Configure `.env` to point to local development server
- Used for testing and development

### Production Environment

**Windows 11 mini-ITX Setup**:

1. **Clone Repository**:
   ```cmd
   cd C:\Users\Pete\
   git clone https://github.com/bovreuil/garmin-collector.git
   cd garmin-collector
   ```

2. **Install Dependencies**:
   ```cmd
   pip install -r requirements.txt
   ```

3. **Configure Environment**:
   Create `.env` file:
   ```env
   # Garmin Connect credentials
   GARMIN_EMAIL=
   GARMIN_PASSWORD=

   # Rehab Platform server configuration (Render)
   REHAB_PLATFORM_URL=
   SHARED_SECRET=

   # Polling configuration
   POLL_INTERVAL=30
   ```

4. **Create Startup Script**:
   Create `C:\Users\Pete\scripts\start-garmin-collector.bat`:
   ```batch
   cd C:\Users\Pete\garmin-collector
   python collector.py --poll
   ```

5. **Set Up Windows Scheduled Task**:
   - Open Task Scheduler
   - Create Basic Task: "Garmin Collector Startup"
   - Trigger: "When the computer starts" (or "At log on" for consistency)
   - Action: Start program `C:\Users\Pete\scripts\start-garmin-collector.bat`
   - Settings: Run whether user is logged on or not, run with highest privileges
   - Allow task to be run on demand
   - If task fails, restart every 1 minute (up to 3 times)

6. **Configure Auto-login & Auto-lock** (for unattended operation):
   - **Auto-login**: Use `netplwiz` to enable automatic login
   - **Auto-lock**: Create scheduled task to run `rundll32.exe user32.dll,LockWorkStation` at log on

### Running as a Service

On Windows, you can also create a service using tools like:

* NSSM (Non-Sucking Service Manager)
* Windows Service Wrapper
* Or simply run it in a scheduled task (recommended approach)

### Example NSSM Setup (Alternative)

```cmd
# Install the service
nssm install GarminCollector python C:\path\to\garmin-collector\collector.py

# Set working directory
nssm set GarminCollector AppDirectory C:\path\to\garmin-collector

# Start the service
nssm start GarminCollector
```

## Logging

The collector logs all activities to stdout. For production deployment, consider redirecting logs to a file:

```bash
python collector.py --poll >> collector.log 2>&1
```

## Troubleshooting

### Authentication Issues

* Verify your Garmin credentials are correct
* Check that the shared secret matches between collector and server
* Ensure the server URL is accessible from the collector machine

### Network Issues

* The collector needs outbound HTTPS access to Garmin Connect
* The collector needs outbound HTTP/HTTPS access to your rehab-platform server
* No inbound connections are required (the collector initiates all connections)

### Garmin API Issues

* Garmin may rate limit requests - the collector includes basic error handling
* Stress and body battery collection failures are logged as warnings but don't block other data collection
* If you encounter frequent authentication failures, Garmin may have flagged your IP
* Consider running the collector from a different network if issues persist

### Windows Scheduled Task Issues

* Ensure the task is set to run whether user is logged on or not
* Check that the batch file path is correct
* Verify Python is in the system PATH
* Test the batch file manually before setting up the scheduled task

### Production Environment Issues

* **401 Unauthorized**: Check that `SHARED_SECRET` matches between collector and Render web service
* **Connection refused**: Verify `REHAB_PLATFORM_URL` is correct
* **Task not starting**: Check Task Scheduler logs and ensure auto-login is configured

## Security Notes

* Store credentials securely in the `.env` file
* Use a strong shared secret for server communication
* Consider running the collector in a restricted environment
* Monitor logs for any suspicious activity
* Never commit `.env` files to version control

## Production Architecture

**Current Production Setup**:

```
User (laptop/phone) → Render Web App → Render PostgreSQL
                                    ↑
Mini-ITX Collector → Garmin Connect → Render API
```

**Key Benefits**:
- ✅ **No inbound connections** to home network
- ✅ **Garmin credentials** only on mini-ITX
- ✅ **Automatic data collection** 24/7
- ✅ **Cloud-hosted web app** accessible from anywhere
- ✅ **Simple deployment** via GitHub

## About

Python 3 API wrapper for Garmin Connect to get statistics and set activities. Forked from [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect) and customized for the rehab-platform project.

### Resources

- **Main Application**: [rehab-platform](https://github.com/bovreuil/rehab-platform)
- **Original Project**: [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect)

### License

MIT license