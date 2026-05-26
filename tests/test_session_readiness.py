"""Session readiness: postpone cap, staleness gate, refresh+HR probe, pre-warm hook."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from collector import GarminCollector


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch) -> GarminCollector:
    monkeypatch.setenv("GARMIN_EMAIL", "test@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "secret")
    monkeypatch.setenv("GARMIN_KEEPALIVE_INTERVAL", "2700")
    monkeypatch.setenv("GARMIN_KEEPALIVE_POSTPONE_AFTER_COLLECT_SEC", "900")
    monkeypatch.setenv("GARMIN_SESSION_STALE_SEC", "1200")
    monkeypatch.setenv("COLLECTOR_HEALTH_INTERVAL", "0")
    return GarminCollector("http://example.com", "shared-secret")


def test_postpone_cap_after_collect(collector: GarminCollector) -> None:
    collector._next_keepalive_monotonic = time.monotonic()
    collector._postpone_keepalive_after_collect()
    eta = collector._next_keepalive_monotonic - time.monotonic()
    assert 895 <= eta <= 905


def test_should_run_readiness_when_overdue(collector: GarminCollector) -> None:
    collector._next_keepalive_monotonic = time.monotonic() - 1
    collector._collection_ready = True
    collector._last_garmin_ok_utc = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    assert collector._should_run_readiness_before_jobs() is True


def test_should_run_readiness_when_not_collection_ready(collector: GarminCollector) -> None:
    collector._next_keepalive_monotonic = time.monotonic() + 3600
    collector._collection_ready = False
    assert collector._should_run_readiness_before_jobs() is True


def test_should_run_readiness_when_stale(collector: GarminCollector) -> None:
    collector._next_keepalive_monotonic = time.monotonic() + 3600
    collector._collection_ready = True
    stale = datetime.now(timezone.utc) - timedelta(minutes=25)
    collector._last_garmin_ok_utc = stale.replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    assert collector._should_run_readiness_before_jobs() is True


def test_should_not_run_readiness_when_fresh(collector: GarminCollector) -> None:
    collector._next_keepalive_monotonic = time.monotonic() + 3600
    collector._collection_ready = True
    collector._last_garmin_ok_utc = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    assert collector._should_run_readiness_before_jobs() is False


def test_readiness_calls_refresh_and_heart_rates(collector: GarminCollector) -> None:
    mock_api = MagicMock()
    mock_api.display_name = "bovreuil"
    mock_client = MagicMock()
    mock_api.client = mock_client

    with patch.object(collector, "get_garmin_api", return_value=mock_api):
        result = collector._run_session_readiness(reason="test")

    assert result == "ok"
    mock_client._refresh_session.assert_called_once()
    mock_api.get_heart_rates.assert_called_once()
    assert collector._collection_ready is True


def test_readiness_refreshed_when_token_mtime_increases(
    collector: GarminCollector, tmp_path: Path
) -> None:
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    token_file = token_dir / "garmin_tokens.json"
    token_file.write_text('{"jwt_web":"a","csrf_token":"b","cookies":{}}')
    collector._tokenstore_path = token_dir

    mock_api = MagicMock()
    mock_api.display_name = "bovreuil"

    def touch_token() -> None:
        token_file.write_text('{"jwt_web":"c","csrf_token":"d","cookies":{}}')

    mock_api.client._refresh_session.side_effect = touch_token

    with patch.object(collector, "get_garmin_api", return_value=mock_api):
        result = collector._run_session_readiness(reason="test")

    assert result == "refreshed"
    assert collector._last_auth_refresh_utc is not None


def test_prewarm_runs_before_run_job(collector: GarminCollector) -> None:
    collector._collection_ready = False
    jobs = [{"job_id": "j1", "target_date": "2026-05-26"}]
    run_job = MagicMock()
    run_keepalive = MagicMock()

    with patch.object(collector, "poll_for_jobs", return_value=jobs):
        with patch.object(collector, "run_keepalive_once", run_keepalive):
            with patch.object(collector, "run_job", run_job):
                with patch.object(collector, "send_health_heartbeat"):
                    with patch("collector.time.sleep", side_effect=KeyboardInterrupt):
                        collector.run_polling_loop(poll_interval=1)

    run_keepalive.assert_called_once_with(reason="pending_jobs")
    run_job.assert_called_once_with(jobs[0])
