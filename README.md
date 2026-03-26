# Garmin Data Collector

This is a standalone service that polls the rehab-platform server for Garmin data collection jobs and fetches data from Garmin Connect.

## Documentation

| Document | Contents |
|----------|----------|
| This README | Setup, runtime behavior, deployment, troubleshooting |
| [docs/MAINTAINERS.md](docs/MAINTAINERS.md) | **Maintainers / coding agents:** auth design (JWT, Playwright, 429), key files, env, upstream sync |
| [AGENTS.md](AGENTS.md) | Short pointer to MAINTAINERS + other docs |
| [docs/INTEGRATION.md](docs/INTEGRATION.md) | Architecture vs rehab-platform, full HTTP + JSON contract, auth, operational notes |
| [docs/GARMIN_AUTH_LANDSCAPE.md](docs/GARMIN_AUTH_LANDSCAPE.md) | Garth, upstream issues (#332/#337, garth#217), `react` branch, how to test |

The integration guide is the reference for **machine-to-machine** calls (`GET /api/jobs/pending`, status updates, upload payload shape). Rehab-platform’s `app.py` is authoritative if anything diverges.

## Overview

**garmin-collector** is a separate repository from [rehab-platform](https://github.com/bovreuil/rehab-platform) so Garmin API and token handling stay out of the main app. In production, Garmin often blocks cloud egress (e.g. Render); this service runs on a **trusted path** (such as a home mini-ITX) and **pushes** data to the platform over HTTPS.

The garmin-collector runs independently and handles all Garmin API interactions. It:

1. Polls the rehab-platform server for pending jobs
2. Connects to Garmin Connect using stored credentials
3. Fetches heart rate, stress, body battery, and activity data for requested dates
4. Uploads the collected data back to the server

### Where it runs

Typical setups (no containers):

| Environment | Machine | Command | Load |
|-------------|---------|---------|------|
| **Dev** | MacBook, this repo folder | `python collector.py --poll` | Occasional jobs while debugging |
| **Prod** | Windows mini-ITX, clone of this repo | `python collector.py --poll` | About **5–10** collections per day as needed |

Garmin OAuth tokens should live **inside each clone** (see `GARMINTOKENS` below) so dev and prod do not share or overwrite each other’s sessions.

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

This installs dependencies and the **local** `garminconnect` package from this repository (`-e .` in `requirements.txt`), so the vendored **`garminconnect`** matches `pyproject.toml` (upstream **`react`** uses JWT on disk — **no Garth**).

**Browser login (HTTP 429 on password login):** install Playwright extras, then Chromium:

```bash
pip install -r requirements-browser.txt
python -m playwright install chromium
python scripts/garmin_playwright_login.py --verify
```

(`python -m playwright install …` avoids needing `playwright` on `PATH` on Windows.)

That writes `garmin_tokens.json` under `GARMINTOKENS` (e.g. `.garmin-tokens/`) in the same shape `collector.py` expects. Run it on the **same machine** as the collector when sessions may be IP-bound.

**`GARMIN_BROWSER_LOGIN` — when the collector may open a browser:** Playwright is used to refresh Garmin sessions when password login returns **429**, when on-disk tokens are **rejected or expired (401)**, and in related recovery paths. That only happens if **`GARMIN_BROWSER_LOGIN=1`** (or another truthy value) **or** the process stdin is a **TTY** (e.g. you started `python collector.py --poll` yourself in a terminal). **Scheduled tasks and most background launches are not a TTY** — on a Windows mini-ITX using Task Scheduler + a `.bat` that only does `cd` + `python collector.py --poll`, set **`GARMIN_BROWSER_LOGIN=1`** in **`.env`** next to `collector.py` so recovery can run unattended when tokens need reseeding (you still need to be at the machine if the browser shows a Garmin challenge). **`GARMIN_PLAYWRIGHT_CHROME=1`** passes **`--chrome`**. See **Deployment → Windows 11 mini-ITX** and `env.example`. Install browser deps as above.

If the SSO page shows a red **“unexpected error”** banner (or JWT capture still fails), try in order: **`python scripts/garmin_playwright_login.py --chrome --verify`** (requires [Google Chrome](https://www.google.com/chrome/) installed), **`--no-submit`** (script fills the form; you click Sign in), **`--entry portal`**, or **`--manual`** (you sign in entirely by hand; waits up to 10 minutes). Check `garmin-login-debug.png` under your token directory if the script saves a screenshot.

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

# Garmin session directory (gitignored); holds garmin_tokens.json on react vendor. See env.example.
GARMINTOKENS=.garmin-tokens

# Rehab Platform server configuration
REHAB_PLATFORM_URL=http://localhost:5001
SHARED_SECRET=your_shared_secret_here

# Polling configuration (seconds between polls; production often ~30)
POLL_INTERVAL=60

# If the collector runs under Task Scheduler (non-interactive), enable browser recovery:
# GARMIN_BROWSER_LOGIN=1
```

**Important**: `SHARED_SECRET` must match rehab-platform on both sides (e.g. `API_CONFIG['SHARED_SECRET']` / environment variables there and `SHARED_SECRET` here). Mismatches return **401 Unauthorized**.

**Garmin tokens:** Set `GARMINTOKENS` to a directory path under the project (default `.garmin-tokens/`). The collector will use it to reuse OAuth sessions instead of performing a full Garmin SSO sign-in on every job (which can trigger **429 Too Many Requests** on `sso.garmin.com`). The directory is listed in `.gitignore` — never commit token files. Restrict permissions on that folder where your OS allows (e.g. `chmod 700 .garmin-tokens` on macOS/Linux).

### 3. Run the Collector

```bash
python collector.py --poll
```

The collector will start polling the server every 60 seconds (or whatever you set in `POLL_INTERVAL`).

## How It Works

### Job Polling

The collector periodically checks the rehab-platform server for pending jobs by calling `GET /api/jobs/pending`. Jobs are ordered by oldest `created_at` first. Each job includes a **`target_date`** (`YYYY-MM-DD`): that calendar day (UK timezone semantics on the platform—see [docs/INTEGRATION.md](docs/INTEGRATION.md)) drives what Garmin data is fetched; storage uses the job’s `target_date`, not a date inside the JSON body.

### Data Collection

When a job is found, the collector:

1. Updates the job status to "running"
2. Connects to Garmin Connect (reuses saved OAuth tokens in `GARMINTOKENS` when valid; otherwise logs in with credentials and saves tokens)
3. Fetches heart rate data for the target date (2-minute intervals)
4. Fetches all-day stress and body battery data for the target date (3-minute intervals, via `get_all_day_stress()` endpoint)
5. Fetches resting heart rate data for the target date (via `get_rhr_day()` endpoint)
6. Fetches HRV (Heart Rate Variability) data for the target date (via `get_hrv_data()` endpoint)
7. Fetches respiration data for the target date (via `get_respiration_data()` endpoint)
8. Fetches training readiness data for the target date (via `get_training_readiness()` endpoint)
9. Fetches activity data for the target date (HR, breathing rate, metadata)
10. Uploads the collected data to the server (`POST /api/jobs/{job_id}/data`)
11. Updates the job status to `completed` or `failed` via `POST /api/jobs/{job_id}/status`

A successful data upload **does not** mark the job complete on the server by itself; the collector must still call the status endpoint (see [docs/INTEGRATION.md](docs/INTEGRATION.md) §4).

**Data Collection Details**:
- **Heart Rate**: Daily whole-day data at 2-minute intervals
- **Stress**: Daily stress level time series at 3-minute intervals (typically ~480 points per day)
- **Body Battery**: Daily body battery level and status time series at 3-minute intervals (typically ~480 points per day)
- **Resting Heart Rate**: Daily resting heart rate value
- **HRV (Heart Rate Variability)**: Daily HRV summary data including weekly averages (large hrvReadings array removed to reduce payload size)
- **Respiration**: Daily respiration metrics including average sleep respiration values (large respirationValuesArray removed to reduce payload size, respirationAveragesValuesArray retained)
- **Training Readiness**: Daily training readiness score
- **Activities**: Per-activity data including HR time series, breathing rate, and metadata

**Error Handling**:
- If stress/body battery data collection fails, the collector logs a warning but continues processing other data
- If health metrics (resting HR, HRV, respiration, training readiness) collection fails, the collector logs a warning but continues processing
- HRV data may not be available for all dates (API returns 204 No Content) - this is expected and handled gracefully
- Collection failures don't block the upload of other successfully collected data
- **Older Data Handling**: For dates older than ~6 months, Garmin may return `None` instead of empty lists for some fields. The collector handles this gracefully by converting `None` to empty lists and validating data types before processing. If data appears missing, you may need to "reload chart" in the Garmin Connect mobile app first to make it available via API.

### Data Upload

Collected data is uploaded with `POST /api/jobs/{job_id}/data`. Optional or empty sections in the payload can let rehab-platform **preserve** existing day data when merging or backfilling; full field shapes and semantics are in [docs/INTEGRATION.md](docs/INTEGRATION.md) §5.

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/jobs/pending` | Pending jobs (`collect_data`, etc.) |
| `POST` | `/api/jobs/{job_id}/status` | `running` → `completed` or `failed` |
| `POST` | `/api/jobs/{job_id}/data` | Upload JSON for that job’s `target_date` |

All requests use **`Authorization: Bearer <SHARED_SECRET>`** (machine-to-machine; not browser cookies). Request/response bodies, `result` / `error_message` behavior, and activity rules are documented in [docs/INTEGRATION.md](docs/INTEGRATION.md).

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
   pip install -r requirements-browser.txt
   python -m playwright install chromium
   ```

3. **Configure Environment**:
   Create **`C:\Users\Pete\garmin-collector\.env`** (same folder as `collector.py` so `python-dotenv` loads it when the batch file `cd`s into the repo). At minimum:
   ```env
   GARMIN_EMAIL=
   GARMIN_PASSWORD=
   REHAB_PLATFORM_URL=
   SHARED_SECRET=
   POLL_INTERVAL=30

   # Required for Task Scheduler + .bat: no interactive terminal → browser recovery is off unless set
   GARMIN_BROWSER_LOGIN=1
   ```
   Without **`GARMIN_BROWSER_LOGIN=1`**, the collector assumes it may only auto-launch Playwright when **stdin is a TTY**. A typical **“At log on”** task that **starts a program** (`start-garmin-collector.bat`) does **not** provide a TTY, so **429 / expired-token recovery would skip the browser** and jobs could fail until you run `scripts/garmin_playwright_login.py` manually.

4. **Create Startup Script**:
   Example: `C:\Users\Pete\scripts\start-garmin-collector.bat`:
   ```batch
   cd C:\Users\Pete\garmin-collector
   python collector.py --poll
   ```
   The **`cd`** line is important so `.env` and `GARMINTOKENS` resolve next to the clone.

5. **Set Up Windows Scheduled Task** (example matching a mini-ITX setup):
   - **Trigger:** **At log on** (after Windows auto-logs in the collector user).
   - **Action:** **Start a program** → `C:\Users\Pete\scripts\start-garmin-collector.bat` (or `python` with arguments and **“Start in”** set to `C:\Users\Pete\garmin-collector` if you prefer not to use a `.bat`).
   - **Run only when user is logged on** is typical when Garmin/Chromium need an interactive desktop and auto-login + lock screen is configured.
   - Allow run on demand for testing; optional restart on failure.

6. **Auto-login and lock screen** (common for “runs at log on” but screen locked):
   - **Auto-login:** e.g. `netplwiz` so the PC boots into the collector user session.
   - **Lock at log on:** optional separate task for `rundll32.exe user32.dll,LockWorkStation` so the machine locks immediately while the collector still runs in the session.

   When Garmin opens a browser for reseed, the session must be able to show UI (unattended kiosk-style); test **Run on demand** after a reboot.

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
* Ensure `GARMINTOKENS` points at a **persistent directory inside the project clone** (see `env.example`). Without saved tokens, every job can trigger a full Garmin login and hit **429** throttling on SSO even at modest daily volume
* If programmatic login hits **429** or tokens expire but **no browser window** appears, check **`.env`**: for Task Scheduler / `.bat` runs, set **`GARMIN_BROWSER_LOGIN=1`** (stdin is usually not a TTY). Or run `scripts/garmin_playwright_login.py` manually (see setup above)
* If you later enable MFA, use a headed browser flow or Garmin’s prompts; this repo assumes password-only accounts for the Playwright helper

### Network Issues

* The collector needs outbound HTTPS access to Garmin Connect
* The collector needs outbound HTTP/HTTPS access to your rehab-platform server
* No inbound connections are required (the collector initiates all connections)

### Garmin API Issues

* Garmin may rate limit requests - the collector includes basic error handling
* Stress and body battery collection failures are logged as warnings but don't block other data collection
* If you encounter frequent authentication failures, Garmin may have flagged your IP
* Consider running the collector from a different network if issues persist
* For **SSO 429**, **Garth**, upstream **`react`** context, see [docs/GARMIN_AUTH_LANDSCAPE.md](docs/GARMIN_AUTH_LANDSCAPE.md); for auth design and env, see [docs/MAINTAINERS.md](docs/MAINTAINERS.md)

### Windows Scheduled Task Issues

* Ensure the task is set to run whether user is logged on or not
* Check that the batch file path is correct
* Verify Python is in the system PATH
* Test the batch file manually before setting up the scheduled task

### Production Environment Issues

* **401 Unauthorized**: Align `SHARED_SECRET` with rehab-platform (`API_CONFIG` / env), base URL, and `Authorization: Bearer ...` spelling ([docs/INTEGRATION.md](docs/INTEGRATION.md) §3)
* **Connection refused**: Verify `REHAB_PLATFORM_URL` is correct
* **Task not starting**: Check Task Scheduler logs and ensure auto-login is configured

### Validation after auth or token changes

Cross-check with [docs/INTEGRATION.md](docs/INTEGRATION.md): shared secret on `GET /api/jobs/pending`, `POST .../status`, and `POST .../data`.

1. `GARMINTOKENS` points at a persistent directory (e.g. `.garmin-tokens/`) and contains **`garmin_tokens.json`** after `scripts/garmin_playwright_login.py` or a successful login.
2. Run `python collector.py --poll` (or process one job): logs should show routine **token reuse**, not a full credential login on every job.
3. On rehab-platform, the job reaches **`completed`** with expected data, or **`failed`** with a clear `error_message` if Garmin still rejects the session.

## Security Notes

* Store credentials securely in the `.env` file
* Use a strong shared secret for server communication
* Consider running the collector in a restricted environment
* Monitor logs for any suspicious activity
* Never commit `.env` files to version control

## Production Architecture

**Current production setup** (matches [docs/INTEGRATION.md](docs/INTEGRATION.md) topology):

```text
User (laptop/phone) → Render Web App → Render PostgreSQL
                                    ↑
Mini-ITX Collector → Garmin Connect → Render API
```

Admins create jobs from rehab-platform **`/data`** (`POST /collect-data`), which queues `background_jobs` with `job_type='collect_data'` and `status='pending'`.

**Key Benefits**:
- ✅ **No inbound connections** to home network
- ✅ **Garmin credentials** only on mini-ITX
- ✅ **Automatic data collection** 24/7
- ✅ **Cloud-hosted web app** accessible from anywhere
- ✅ **Simple deployment** via GitHub

## About

Python 3 API wrapper for Garmin Connect to get statistics and set activities. Forked from [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect) and customized for the rehab-platform project.

**`master`** vendors upstream’s **`react`**-style stack (JWT / `garmin_tokens.json`, no Garth) and adds Playwright-based token seeding when programmatic login hits **429**. See [docs/MAINTAINERS.md](docs/MAINTAINERS.md) and [docs/GARMIN_AUTH_LANDSCAPE.md](docs/GARMIN_AUTH_LANDSCAPE.md).

### Resources

- **Main Application**: [rehab-platform](https://github.com/bovreuil/rehab-platform)
- **Original Project**: [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect)

### License

MIT license