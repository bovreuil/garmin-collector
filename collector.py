#!/usr/bin/env python3
"""
Garmin Data Collector

This module handles polling the rehab-platform server for jobs and collecting
Garmin data when jobs are available.
"""

import json
import logging
import os
import re
import requests
import subprocess
import sys
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Project root (directory containing collector.py); used for default token path and relative GARMINTOKENS.
_PROJECT_ROOT = Path(__file__).resolve().parent


def resolve_tokenstore_path() -> Path:
    """Garmin token directory: GARMINTOKENS or <project>/.garmin-tokens; relative paths are under project root."""
    raw = os.getenv("GARMINTOKENS")
    if not raw:
        return _PROJECT_ROOT / ".garmin-tokens"
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (_PROJECT_ROOT / p).resolve()
    return p


def _browser_login_enabled() -> bool:
    """When True, collector may run scripts/garmin_playwright_login.py for browser token seeding.

    - GARMIN_BROWSER_LOGIN=0|off|false: never
    - GARMIN_BROWSER_LOGIN=1|on|true: always
    - unset: if stdin is a TTY (set GARMIN_BROWSER_LOGIN=1 if recovery never runs on your host)
    """
    v = os.getenv("GARMIN_BROWSER_LOGIN", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    return sys.stdin.isatty()


_PLAYWRIGHT_SCRIPT = _PROJECT_ROOT / "scripts" / "garmin_playwright_login.py"


def _token_json_path(tokenstore: Path) -> Path:
    """Same file resolution as garminconnect.client.Client.load/dump."""
    if tokenstore.is_dir() or not tokenstore.name.endswith(".json"):
        return tokenstore / "garmin_tokens.json"
    return tokenstore


class TransientGarminNetworkError(Exception):
    """TLS/socket drop or similar from Garmin edge — collector retries once with a fresh client."""


def _transient_garmin_network_error(exc: BaseException) -> bool:
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    parts: list[str] = []
    e: BaseException | None = exc
    for _ in range(8):
        if e is None:
            break
        parts.append(str(e).lower())
        parts.append(type(e).__name__.lower())
        e = e.__cause__
    text = " ".join(parts)
    return any(
        n in text
        for n in (
            "connection aborted",
            "connection reset",
            "forcibly closed",
            "10054",
            "broken pipe",
            "remote end closed",
            "remote disconnected",
            "protocolerror",
            "read timed out",
            "timed out",
        )
    )


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

        self._tokenstore_path = resolve_tokenstore_path()
        self._garmin: Optional[Garmin] = None
        self._garmin_lock = threading.Lock()

        self.keepalive_interval = int(os.getenv("GARMIN_KEEPALIVE_INTERVAL", "0"))
        self.health_interval = int(os.getenv("COLLECTOR_HEALTH_INTERVAL", "60"))
        self.collector_id = os.getenv("COLLECTOR_ID", socket.gethostname())
        self.health_endpoint = os.getenv("COLLECTOR_HEALTH_ENDPOINT", "/api/collector/health")

        self._current_task = "idle"
        self._last_garmin_ok_utc: Optional[str] = None
        self._last_auth_refresh_utc: Optional[str] = None
        self._last_browser_reseed_utc: Optional[str] = None
        self._last_error: Dict[str, Optional[str]] = {
            "kind": "none",
            "message": None,
            "at_utc": None,
        }

        self._next_keepalive_monotonic = time.monotonic() + max(self.keepalive_interval, 1)
        self._next_health_monotonic = time.monotonic() + max(self.health_interval, 1)

        self._keepalive_last_run_utc: Optional[str] = None
        self._keepalive_last_duration_ms: Optional[int] = None
        self._keepalive_last_result: Optional[str] = None
        self._keepalive_events: List[Dict[str, str]] = []

        self._collection_last_run_utc: Optional[str] = None
        self._collection_last_duration_ms: Optional[int] = None
        self._collection_last_result: Optional[str] = None
        self._collection_events: List[Dict[str, str]] = []
        logger.info("Garmin token storage directory: %s", self._tokenstore_path)
        if self.keepalive_interval > 0:
            logger.info("Garmin keepalive enabled every %ss", self.keepalive_interval)
        else:
            logger.info("Garmin keepalive disabled (GARMIN_KEEPALIVE_INTERVAL=0)")

    def _clear_stored_garmin_tokens(self) -> None:
        """Remove garmin_tokens.json so login does not reload an expired session."""
        p = _token_json_path(self._tokenstore_path)
        try:
            if p.is_file():
                p.unlink()
                logger.warning(
                    "Removed stale token file %s (e.g. expired JWT); next login will re-authenticate",
                    p,
                )
        except OSError as err:
            logger.warning("Could not remove token file %s: %s", p, err)

    def _reseed_via_browser_after_token_clear(self, reason: str) -> None:
        """If browser seed is allowed, do it now and skip a password login that often returns 429."""
        if not _browser_login_enabled():
            return
        logger.info(
            "Opening browser to seed tokens (%s); skipping programmatic login",
            reason,
        )
        self._invoke_playwright_seeding()

    def _invoke_playwright_seeding(self) -> None:
        """Run browser login helper; writes garmin_tokens.json under GARMINTOKENS."""
        if not _PLAYWRIGHT_SCRIPT.is_file():
            raise FileNotFoundError(
                f"Playwright helper missing: {_PLAYWRIGHT_SCRIPT}. "
                "Install with: pip install -r requirements-browser.txt && "
                "python -m playwright install chromium"
            )
        cmd = [sys.executable, str(_PLAYWRIGHT_SCRIPT), "--verify"]
        if os.getenv("GARMIN_PLAYWRIGHT_CHROME", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            cmd.append("--chrome")
        logger.info("Running browser login: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            cwd=str(_PROJECT_ROOT),
            env=os.environ.copy(),
        )
        if result.returncode != 0:
            raise GarminConnectConnectionError(
                "Browser login helper exited with code "
                f"{result.returncode}. Install browser deps and run manually: "
                "python scripts/garmin_playwright_login.py --verify"
            )
        self._last_browser_reseed_utc = self._now_utc()

    def _now_utc(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _trim_events_24h(self, events: List[Dict[str, str]]) -> None:
        cutoff = time.time() - 86400
        events[:] = [e for e in events if e.get("ts_epoch", 0) >= cutoff]

    def _record_keepalive_event(self, result: str) -> None:
        self._keepalive_events.append({"result": result, "ts_epoch": time.time()})
        self._trim_events_24h(self._keepalive_events)

    def _record_collection_event(self, duration_ms: int, result: str) -> None:
        self._collection_events.append(
            {
                "result": result,
                "duration_ms": duration_ms,
                "ts_epoch": time.time(),
            }
        )
        self._trim_events_24h(self._collection_events)

    def _set_last_error(self, kind: str, message: str) -> None:
        self._last_error = {
            "kind": kind,
            "message": message[:300],
            "at_utc": self._now_utc(),
        }

    def _set_ok(self) -> None:
        self._last_garmin_ok_utc = self._now_utc()
        self._last_error = {"kind": "none", "message": None, "at_utc": None}

    def _garmin_session_state(self) -> str:
        if self._current_task == "keepalive":
            return "refreshing"
        if self._current_task == "reseed":
            return "reseeding"
        if self._last_error.get("kind") not in (None, "none"):
            return "error"
        if self._last_garmin_ok_utc:
            return "warm"
        return "cold"

    def _build_health_payload(self) -> Dict:
        self._trim_events_24h(self._keepalive_events)
        self._trim_events_24h(self._collection_events)

        keepalive_counts = {"runs": 0, "ok": 0, "refreshed": 0, "reseeded": 0, "failed": 0}
        for ev in self._keepalive_events:
            keepalive_counts["runs"] += 1
            key = ev.get("result", "failed")
            if key in keepalive_counts:
                keepalive_counts[key] += 1

        collection_counts = {"runs": 0, "fast_lt_10s": 0, "slow_ge_10s": 0, "failed": 0}
        for ev in self._collection_events:
            collection_counts["runs"] += 1
            if ev.get("result") == "failed":
                collection_counts["failed"] += 1
            if int(ev.get("duration_ms", 0)) < 10000:
                collection_counts["fast_lt_10s"] += 1
            else:
                collection_counts["slow_ge_10s"] += 1

        next_keepalive_utc = None
        if self.keepalive_interval > 0:
            eta = max(0, self._next_keepalive_monotonic - time.monotonic())
            next_keepalive_utc = datetime.fromtimestamp(
                time.time() + eta, tz=timezone.utc
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        return {
            "collector_id": self.collector_id,
            "timestamp_utc": self._now_utc(),
            "version": os.getenv("COLLECTOR_VERSION", "unknown"),
            "garmin": {
                "session_state": self._garmin_session_state(),
                "last_ok_utc": self._last_garmin_ok_utc,
                "last_auth_refresh_utc": self._last_auth_refresh_utc,
                "last_browser_reseed_utc": self._last_browser_reseed_utc,
                "last_error": self._last_error,
            },
            "keepalive": {
                "enabled": self.keepalive_interval > 0,
                "interval_sec": self.keepalive_interval,
                "next_due_utc": next_keepalive_utc,
                "running": self._current_task == "keepalive",
                "last_run_utc": self._keepalive_last_run_utc,
                "last_duration_ms": self._keepalive_last_duration_ms,
                "last_result": self._keepalive_last_result,
                "counters_24h": keepalive_counts,
            },
            "collections": {
                "last_run_utc": self._collection_last_run_utc,
                "last_duration_ms": self._collection_last_duration_ms,
                "last_result": self._collection_last_result,
                "counters_24h": collection_counts,
            },
            "concurrency": {
                "garmin_lock_held": self._garmin_lock.locked(),
                "current_task": self._current_task,
            },
        }

    def send_health_heartbeat(self, *, force: bool = False) -> None:
        if self.health_interval <= 0:
            return
        now_mono = time.monotonic()
        if not force and now_mono < self._next_health_monotonic:
            return
        self._next_health_monotonic = now_mono + self.health_interval

        endpoint = self.health_endpoint if self.health_endpoint.startswith("/") else f"/{self.health_endpoint}"
        payload = self._build_health_payload()
        try:
            resp = self.session.post(f"{self.server_url}{endpoint}", json=payload, timeout=10)
            if resp.status_code not in (200, 201, 202, 204, 404):
                logger.warning("Collector health heartbeat returned HTTP %s", resp.status_code)
        except requests.RequestException as e:
            logger.debug("Collector health heartbeat failed: %s", e)

    def run_keepalive_once(self) -> None:
        if self.keepalive_interval <= 0:
            return
        started = time.monotonic()
        result = "failed"
        token_path = _token_json_path(self._tokenstore_path)
        before_mtime = token_path.stat().st_mtime if token_path.exists() else None
        browser_before = self._last_browser_reseed_utc
        retried_auth = False
        retried_network = False

        with self._garmin_lock:
            self._current_task = "keepalive"
            try:
                while True:
                    try:
                        api = self.get_garmin_api()
                        api.connectapi(api.garmin_connect_user_settings_url)
                        break
                    except GarminConnectAuthenticationError as e:
                        if retried_auth:
                            raise
                        retried_auth = True
                        logger.warning("Keepalive auth failed; clearing tokens and retrying once: %s", e)
                        self._clear_stored_garmin_tokens()
                        self.invalidate_garmin_client()
                        self._current_task = "reseed"
                        self._reseed_via_browser_after_token_clear("keepalive auth failure")
                        self._current_task = "keepalive"
                        continue
                    except TransientGarminNetworkError:
                        if retried_network:
                            raise
                        retried_network = True
                        self.invalidate_garmin_client()
                        time.sleep(2)
                        continue

                self._set_ok()
                after_mtime = token_path.stat().st_mtime if token_path.exists() else None
                browser_after = self._last_browser_reseed_utc
                if browser_after and browser_after != browser_before:
                    result = "reseeded"
                elif before_mtime is not None and after_mtime is not None and after_mtime > before_mtime:
                    result = "refreshed"
                    self._last_auth_refresh_utc = self._now_utc()
                else:
                    result = "ok"
            except GarminConnectTooManyRequestsError as e:
                self._set_last_error("garmin_rate_limit", str(e))
            except GarminConnectAuthenticationError as e:
                self._set_last_error("garmin_auth", str(e))
            except Exception as e:
                self._set_last_error("garmin_network" if _transient_garmin_network_error(e) else "garmin_unknown", str(e))
                logger.warning("Keepalive failed: %s", e)
            finally:
                self._current_task = "idle"

        # Schedule next keepalive before emitting heartbeat so next_due is accurate.
        self._next_keepalive_monotonic = time.monotonic() + self.keepalive_interval
        self._keepalive_last_run_utc = self._now_utc()
        self._keepalive_last_duration_ms = int((time.monotonic() - started) * 1000)
        self._keepalive_last_result = result
        self._record_keepalive_event(result)
        self.send_health_heartbeat(force=True)

    def invalidate_garmin_client(self) -> None:
        """Drop cached Garmin client so the next job performs token-first login again."""
        self._garmin = None

    def get_garmin_api(self) -> Garmin:
        """Return a logged-in Garmin client, reusing the in-process session when possible."""
        if self._garmin is not None:
            return self._garmin
        self._garmin = self._perform_garmin_login()
        return self._garmin

    def _perform_garmin_login(self) -> Garmin:
        return self._perform_garmin_login_once(allow_browser_fallback=True)

    def _perform_garmin_login_once(self, *, allow_browser_fallback: bool) -> Garmin:
        """
        Log in with Garth-free python-garminconnect (upstream ``react`` client): load tokens
        from ``path_str`` when present, otherwise use email/password. Persist session to disk
        after success via ``client.dump`` (JWT / garmin_tokens.json layout).

        On HTTP 429 from programmatic login, optionally runs Playwright once (see
        ``_browser_login_enabled``) to seed tokens, then retries login without a second
        browser attempt.
        """
        path = self._tokenstore_path
        path_str = str(path)

        env_backup = os.environ.pop("GARMINTOKENS", None)
        try:
            api = Garmin(self.garmin_email, self.garmin_password)
            try:
                api.login(path_str)
            except GarminConnectTooManyRequestsError:
                if allow_browser_fallback and _browser_login_enabled():
                    logger.warning(
                        "Garmin SSO rate limited (429) on password login. "
                        "Opening browser to seed tokens under %s",
                        path_str,
                    )
                    self._invoke_playwright_seeding()
                    return self._perform_garmin_login_once(
                        allow_browser_fallback=False
                    )
                logger.error(
                    "Garmin login rate limited (429). Save tokens under %s (run "
                    "scripts/garmin_playwright_login.py), wait, or set "
                    "GARMIN_BROWSER_LOGIN=1 if your environment has no TTY for recovery.",
                    path_str,
                )
                raise
            except GarminConnectConnectionError as e:
                # Defensive: 429 may appear as a wrapped connection error
                if (
                    allow_browser_fallback
                    and _browser_login_enabled()
                    and ("429" in str(e) or "rate limit" in str(e).lower())
                ):
                    logger.warning(
                        "Garmin login failed with rate limit message; "
                        "trying browser seed under %s",
                        path_str,
                    )
                    self._invoke_playwright_seeding()
                    return self._perform_garmin_login_once(
                        allow_browser_fallback=False
                    )
                raise
        finally:
            if env_backup is not None:
                os.environ["GARMINTOKENS"] = env_backup

        path.parent.mkdir(parents=True, exist_ok=True)
        api.client.dump(path_str)
        logger.info("Garmin session established; tokens saved to %s", path_str)
        return api

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

        retried_auth = False
        retried_login_after_clear = False
        retried_network = False

        while True:
            try:
                api = self.get_garmin_api()
            except GarminConnectTooManyRequestsError as e:
                logger.error("Garmin login rate limited: %s", e, exc_info=True)
                return {
                    'success': False,
                    'failure_kind': 'garmin_rate_limit',
                    'message': (
                        "Garmin SSO or OAuth rate limited (429). Wait before retry; "
                        "use saved tokens under the configured GARMINTOKENS directory "
                        "to avoid a full sign-in on every job."
                    ),
                    'data_found': False,
                }
            except GarminConnectAuthenticationError as e:
                if retried_login_after_clear:
                    logger.error(
                        "Garmin login failed again after clearing token file: %s",
                        e,
                        exc_info=True,
                    )
                    return {
                        'success': False,
                        'failure_kind': 'garmin_login',
                        'message': f"Error connecting to Garmin: {e}",
                        'data_found': False,
                    }
                logger.warning(
                    "Garmin authentication failed at login (e.g. expired tokens on disk); "
                    "removing saved session and retrying once"
                )
                retried_login_after_clear = True
                self._clear_stored_garmin_tokens()
                self.invalidate_garmin_client()
                self._reseed_via_browser_after_token_clear(
                    "session on disk was rejected at login"
                )
                continue
            except Exception as e:
                logger.error(f"Error connecting to Garmin: {e}", exc_info=True)
                return {
                    'success': False,
                    'failure_kind': 'garmin_login',
                    'message': f"Error connecting to Garmin: {str(e)}",
                    'data_found': False,
                }

            try:
                return self._collect_garmin_data_body(api, target_date)
            except GarminConnectAuthenticationError as e:
                if retried_auth:
                    logger.error(
                        "Garmin authentication failed again after re-login: %s", e, exc_info=True
                    )
                    return {
                        'success': False,
                        'failure_kind': 'garmin_auth_during_fetch',
                        'message': f"Garmin authentication failed during data fetch: {e}",
                        'data_found': False,
                    }
                logger.warning(
                    "Garmin authentication error during fetch (e.g. expired JWT); "
                    "clearing saved tokens and session, retrying once"
                )
                retried_auth = True
                self._clear_stored_garmin_tokens()
                self.invalidate_garmin_client()
                self._reseed_via_browser_after_token_clear(
                    "session rejected during data fetch"
                )
                continue
            except GarminConnectTooManyRequestsError as e:
                logger.error("Garmin API rate limited during fetch: %s", e, exc_info=True)
                return {
                    'success': False,
                    'failure_kind': 'garmin_rate_limit',
                    'message': (
                        f"Garmin rate limited during data fetch (429): {e}. "
                        "Retry later with a longer interval if this persists."
                    ),
                    'data_found': False,
                }
            except TransientGarminNetworkError as e:
                if retried_network:
                    logger.error(
                        "Garmin connection still failing after retry: %s", e, exc_info=True
                    )
                    return {
                        'success': False,
                        'failure_kind': 'garmin_network',
                        'message': (
                            f"Garmin connection failed (network reset or timeout): {e}. "
                            "Retry the job later."
                        ),
                        'data_found': False,
                    }
                retried_network = True
                logger.warning(
                    "Transient network error talking to Garmin; invalidating session and retrying once (%s)",
                    e,
                )
                time.sleep(2)
                self.invalidate_garmin_client()
                continue

    def _collect_garmin_data_body(self, api: Garmin, target_date: str) -> Dict:
        """Fetch all series for target_date using an already-logged-in client."""
        try:
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
            # Handle None values for older data (Garmin may return None instead of empty lists)
            if hr_series is None:
                hr_series = []
            elif not isinstance(hr_series, list):
                logger.warning(f"heartRateValues is not a list: {type(hr_series)}, converting to empty list")
                hr_series = []
            
            logger.info(f"Collected {len(hr_series)} heart rate points")
            
            # Get activities for the date
            logger.info(f"Fetching activities for {target_date}")
            activities = self.collect_activities_for_date(api, target_date)
            
            # Get all-day stress data (includes stress + body battery series)
            logger.info(f"Fetching all-day stress data for {target_date}")
            all_day_stress_data = None
            try:
                all_day_stress_data = api.get_all_day_stress(target_date)
                if all_day_stress_data:
                    # Handle None values for older data (Garmin may return None instead of empty lists)
                    stress_array = all_day_stress_data.get('stressValuesArray') or []
                    body_battery_array = all_day_stress_data.get('bodyBatteryValuesArray') or []
                    stress_count = len(stress_array) if isinstance(stress_array, list) else 0
                    body_battery_count = len(body_battery_array) if isinstance(body_battery_array, list) else 0
                    logger.info(f"Collected all-day stress data: {stress_count} stress points, {body_battery_count} body battery points")
            except Exception as e:
                logger.warning(f"Failed to fetch all-day stress data: {e}")
                # Don't fail the whole collection if stress data is unavailable
            
            # Get resting heart rate data
            logger.info(f"Fetching resting heart rate data for {target_date}")
            resting_hr_data = None
            try:
                resting_hr_data = api.get_rhr_day(target_date)
                if resting_hr_data:
                    logger.info(f"Collected resting heart rate data for {target_date}")
            except Exception as e:
                logger.warning(f"Failed to fetch resting heart rate data: {e}")
            
            # Get HRV (Heart Rate Variability) data
            logger.info(f"Fetching HRV data for {target_date}")
            hrv_data = None
            try:
                hrv_data = api.get_hrv_data(target_date)
                if hrv_data:
                    # Remove hrvReadings array to reduce payload size (can be very large)
                    if isinstance(hrv_data, dict) and 'hrvReadings' in hrv_data:
                        hrv_readings = hrv_data.get('hrvReadings')
                        hrv_readings_count = len(hrv_readings) if isinstance(hrv_readings, list) else 0
                        hrv_data = hrv_data.copy()  # Create a copy to avoid modifying the original
                        del hrv_data['hrvReadings']
                        logger.info(f"Collected HRV data for {target_date} (removed {hrv_readings_count} readings from array)")
                    else:
                        logger.info(f"Collected HRV data for {target_date}")
            except Exception as e:
                logger.warning(f"Failed to fetch HRV data: {e}")
                # HRV data may not be available for all dates (API returns 204 No Content)
            
            # Get respiration data
            logger.info(f"Fetching respiration data for {target_date}")
            respiration_data = None
            try:
                respiration_data = api.get_respiration_data(target_date)
                if respiration_data:
                    # Remove respirationValuesArray to reduce payload size (can be very large)
                    # Keep respirationAveragesValuesArray as it's needed
                    if isinstance(respiration_data, dict) and 'respirationValuesArray' in respiration_data:
                        respiration_values = respiration_data.get('respirationValuesArray')
                        respiration_values_count = len(respiration_values) if isinstance(respiration_values, list) else 0
                        respiration_data = respiration_data.copy()  # Create a copy to avoid modifying the original
                        del respiration_data['respirationValuesArray']
                        logger.info(f"Collected respiration data for {target_date} (removed {respiration_values_count} values from respirationValuesArray)")
                    else:
                        logger.info(f"Collected respiration data for {target_date}")
            except Exception as e:
                logger.warning(f"Failed to fetch respiration data: {e}")
            
            # Get training readiness data
            logger.info(f"Fetching training readiness data for {target_date}")
            training_readiness_data = None
            try:
                training_readiness_data = api.get_training_readiness(target_date)
                if training_readiness_data:
                    logger.info(f"Collected training readiness data for {target_date}")
            except Exception as e:
                logger.warning(f"Failed to fetch training readiness data: {e}")
            
            # Prepare data for upload to server
            result_data = {
                'success': True,
                'data_found': True,
                'heart_rate_data': {
                    'date': target_date,
                    'heartRateValues': hr_series
                },
                'all_day_stress_data': all_day_stress_data,  # Includes stress + body battery with catalogs
                'resting_hr_data': resting_hr_data,
                'hrv_data': hrv_data,
                'respiration_data': respiration_data,
                'training_readiness_data': training_readiness_data,
                'activities': activities,
                'message': f"Successfully collected data for {target_date}"
            }
            
            logger.info(f"Data collection completed for {target_date}")
            return result_data

        except (GarminConnectAuthenticationError, GarminConnectTooManyRequestsError):
            raise
        except GarminConnectConnectionError as e:
            # Legacy wraps or missed 401 text — treat like auth for outer retry
            err_s = str(e).lower()
            if (
                re.search(r"API Error\s+401\b", str(e))
                or re.search(r"API Error\s+403\b", str(e))
                or "not authenticated" in err_s
            ):
                raise GarminConnectAuthenticationError(str(e)) from e
            if _transient_garmin_network_error(e):
                raise TransientGarminNetworkError(str(e)) from e
            import traceback
            error_traceback = traceback.format_exc()
            logger.error(f"Error collecting data for {target_date}: {e}")
            logger.error(f"Traceback: {error_traceback}")
            return {
                'success': False,
                'message': f"Error collecting data: {str(e)}",
                'data_found': False,
                'traceback': error_traceback
            }
        except Exception as e:
            if _transient_garmin_network_error(e):
                raise TransientGarminNetworkError(str(e)) from e
            import traceback
            error_traceback = traceback.format_exc()
            logger.error(f"Error collecting data for {target_date}: {e}")
            logger.error(f"Traceback: {error_traceback}")
            return {
                'success': False,
                'message': f"Error collecting data: {str(e)}",
                'data_found': False,
                'traceback': error_traceback
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
            
            # Handle None values - convert to empty list
            if activities is None:
                logger.info(f"Activities is None for {target_date}")
                return []
            
            # Ensure activities is a list
            if not isinstance(activities, list):
                logger.warning(f"Activities is not a list: {type(activities)}, treating as empty")
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
                        
                        # Ensure series are lists (handle None values)
                        if hr_series is None:
                            hr_series = []
                        if breathing_series is None:
                            breathing_series = []
                        
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
        
        # Handle None values
        if metric_descriptors is None:
            logger.warning("metricDescriptors is None")
            return None, None
        
        if not isinstance(metric_descriptors, list):
            logger.warning(f"metricDescriptors is not a list: {type(metric_descriptors)}")
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
                # Handle None values
                if activity_metrics is None:
                    logger.warning("activityDetailMetrics is None")
                    return []
                if not isinstance(activity_metrics, list):
                    logger.warning(f"activityDetailMetrics is not a list: {type(activity_metrics)}")
                    return []
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
                        if 'metrics' in entry and entry.get('metrics') is not None and isinstance(entry['metrics'], list) and len(entry['metrics']) > max(hr_pos, ts_pos):
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
                        if 'metrics' in entry and entry.get('metrics') is not None and isinstance(entry['metrics'], list) and len(entry['metrics']) > max(breathing_pos, ts_pos):
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
        
        started = time.monotonic()
        result_state = "failed"
        with self._garmin_lock:
            self._current_task = "collect"
            try:
                # Collect data
                result = self.collect_garmin_data(target_date, job_id)

                if result['success']:
                    # Upload data to server
                    upload_success = self.upload_data_to_server(job_id, result)

                    if upload_success:
                        self.update_job_status(job_id, 'completed', result)
                        logger.info(f"Job {job_id} completed successfully")
                        result_state = "success"
                    else:
                        self.update_job_status(job_id, 'failed', error_message="Failed to upload data")
                        logger.error(f"Job {job_id} failed to upload data")
                else:
                    fk = result.get('failure_kind')
                    if fk in (
                        'garmin_login',
                        'garmin_rate_limit',
                        'garmin_auth_during_fetch',
                        'garmin_network',
                    ):
                        msg = result.get('message', 'Garmin collection failed')
                        self.update_job_status(job_id, 'failed', error_message=msg)
                        logger.error(f"Job {job_id} failed: {msg}")
                    else:
                        self.update_job_status(job_id, 'completed', result)
                        logger.info(f"Job {job_id} completed with no data found")
                        result_state = "success"
                
            except Exception as e:
                logger.error(f"Job {job_id} failed with error: {e}")
                self.update_job_status(job_id, 'failed', error_message=str(e))
            finally:
                self._current_task = "idle"
                duration_ms = int((time.monotonic() - started) * 1000)
                self._collection_last_run_utc = self._now_utc()
                self._collection_last_duration_ms = duration_ms
                self._collection_last_result = result_state
                self._record_collection_event(duration_ms, result_state)
                if result_state == "success" and self.keepalive_interval > 0:
                    # A successful collection confirms the Garmin session is warm;
                    # postpone keepalive to avoid an immediate redundant warm-up call.
                    self._next_keepalive_monotonic = time.monotonic() + self.keepalive_interval
                self.send_health_heartbeat(force=True)
    
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
                now_mono = time.monotonic()
                if self.keepalive_interval > 0 and now_mono >= self._next_keepalive_monotonic:
                    self.run_keepalive_once()

                # Poll for jobs
                jobs = self.poll_for_jobs()
                
                if jobs:
                    logger.info(f"Found {len(jobs)} pending jobs")
                    
                    # Process each job
                    for job in jobs:
                        self.run_job(job)
                else:
                    logger.debug("No pending jobs found")

                self.send_health_heartbeat()

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
