"""Browser reseed cooldown, fail-fast jobs, and stale-profile detection."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from collector import (
    GarminCollector,
    GarminConnectConnectionError,
    _playwright_failure_suggests_stale_profile,
)


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch) -> GarminCollector:
    monkeypatch.setenv("GARMIN_EMAIL", "test@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "secret")
    monkeypatch.setenv("GARMIN_KEEPALIVE_INTERVAL", "0")
    monkeypatch.setenv("COLLECTOR_HEALTH_INTERVAL", "0")
    monkeypatch.setenv("GARMIN_BROWSER_RESEED_COOLDOWN_SEC", "900")
    monkeypatch.setenv("GARMIN_BROWSER_LOGIN", "1")
    return GarminCollector("http://example.com", "shared-secret")


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("In-page di-oauth/refresh: HTTP 500\nRefresh failed", True),
        ("Could not obtain JWT/csrf after browser login", True),
        (
            "Persistent profile already signed in; skipping SSO\nHTTP 500",
            True,
        ),
        ("Browser deps missing", False),
    ],
)
def test_playwright_failure_suggests_stale_profile(stderr: str, expected: bool) -> None:
    assert _playwright_failure_suggests_stale_profile(stderr) is expected


def test_invoke_playwright_sets_cooldown_on_failure(collector: GarminCollector) -> None:
    fail = MagicMock(returncode=1, stdout="", stderr="di-oauth/refresh: HTTP 500")
    with patch("collector._PLAYWRIGHT_SCRIPT") as script:
        script.is_file.return_value = True
        with patch("collector.subprocess.run", return_value=fail):
            with pytest.raises(GarminConnectConnectionError):
                collector._invoke_playwright_seeding(force_sso=True)
    assert collector._browser_reseed_in_cooldown()


def test_invoke_playwright_skipped_during_cooldown(collector: GarminCollector) -> None:
    collector._browser_reseed_cooldown_until_monotonic = time.monotonic() + 600
    with pytest.raises(GarminConnectConnectionError, match="cooldown"):
        collector._invoke_playwright_seeding()


def test_invoke_playwright_retries_force_sso_on_500(collector: GarminCollector) -> None:
    fail = MagicMock(returncode=1, stdout="", stderr="In-page di-oauth/refresh: HTTP 500")
    ok = MagicMock(returncode=0, stdout="OK", stderr="")
    with patch("collector._PLAYWRIGHT_SCRIPT") as script:
        script.is_file.return_value = True
        with patch("collector.subprocess.run", side_effect=[fail, ok]) as run:
            collector._invoke_playwright_seeding()
    assert run.call_count == 2
    assert "--force-sso" in run.call_args_list[1].args[0]


def test_should_not_run_readiness_when_cooldown_and_not_ready(
    collector: GarminCollector,
) -> None:
    collector._next_keepalive_monotonic = time.monotonic() + 3600
    collector._collection_ready = False
    collector._browser_reseed_cooldown_until_monotonic = time.monotonic() + 600
    assert collector._should_run_readiness_before_jobs() is False


def test_session_recovery_blocked_during_cooldown(collector: GarminCollector) -> None:
    collector._collection_ready = False
    collector._browser_reseed_cooldown_until_monotonic = time.monotonic() + 600
    blocked, msg = collector._session_recovery_blocked()
    assert blocked is True
    assert msg is not None and "cooldown" in msg.lower()


def test_run_job_fail_fast_during_cooldown(collector: GarminCollector) -> None:
    collector._collection_ready = False
    collector._browser_reseed_cooldown_until_monotonic = time.monotonic() + 600
    job = {"job_id": "j1", "target_date": "2026-08-26"}
    with patch.object(collector, "update_job_status") as update:
        with patch.object(collector, "collect_garmin_data") as collect:
            with patch.object(collector, "send_health_heartbeat"):
                collector.run_job(job)
    collect.assert_not_called()
    update.assert_called_once()
    assert update.call_args[0][1] == "failed"
    assert "cooldown" in (update.call_args[1].get("error_message") or "").lower()
