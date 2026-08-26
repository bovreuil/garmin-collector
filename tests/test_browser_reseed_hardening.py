"""Browser reseed cooldown, fail-fast jobs, and stale-profile detection."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from collector import (
    GarminCollector,
    GarminConnectConnectionError,
    _playwright_stderr_is_transient_nav_failure,
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


def test_transient_nav_failure_detected_from_stderr() -> None:
    stderr = (
        "Page.goto: Navigation to portal sign-in is interrupted by another navigation "
        "to https://connect.garmin.com/signin/"
    )
    assert _playwright_stderr_is_transient_nav_failure(stderr) is True
    assert _playwright_stderr_is_transient_nav_failure("di-oauth/refresh: HTTP 500") is False


def test_invoke_playwright_sets_cooldown_on_failure(collector: GarminCollector) -> None:
    fail = MagicMock(returncode=1, stdout="", stderr="di-oauth/refresh: HTTP 500")
    with patch("collector._PLAYWRIGHT_SCRIPT") as script:
        script.is_file.return_value = True
        with patch("collector.subprocess.run", return_value=fail):
            with pytest.raises(GarminConnectConnectionError):
                collector._invoke_playwright_seeding(force_sso=True)
    assert collector._browser_reseed_in_cooldown()


def test_invoke_playwright_no_cooldown_on_transient_nav(collector: GarminCollector) -> None:
    stderr = (
        "Page.goto: Navigation interrupted by another navigation to "
        "https://connect.garmin.com/signin/"
    )
    fail = MagicMock(returncode=1, stdout="", stderr=stderr)
    with patch("collector._PLAYWRIGHT_SCRIPT") as script:
        script.is_file.return_value = True
        with patch("collector.subprocess.run", return_value=fail):
            with pytest.raises(GarminConnectConnectionError, match="redirect"):
                collector._invoke_playwright_seeding(force_sso=True)
    assert not collector._browser_reseed_in_cooldown()


def test_invoke_playwright_skipped_during_cooldown(collector: GarminCollector) -> None:
    collector._browser_reseed_cooldown_until_monotonic = time.monotonic() + 600
    with pytest.raises(GarminConnectConnectionError, match="cooldown"):
        collector._invoke_playwright_seeding()


def test_reseed_after_token_clear_uses_force_sso(collector: GarminCollector) -> None:
    with patch.object(collector, "_invoke_playwright_seeding") as invoke:
        collector._reseed_via_browser_after_token_clear("test reason")
    invoke.assert_called_once_with(force_sso=True)


def test_perform_garmin_login_429_invokes_force_sso(collector: GarminCollector) -> None:
    from garminconnect import GarminConnectTooManyRequestsError

    first_api = MagicMock()
    second_api = MagicMock()
    first_api.login.side_effect = GarminConnectTooManyRequestsError("429")

    with patch("collector.Garmin", side_effect=[first_api, second_api]):
        with patch.object(collector, "_invoke_playwright_seeding") as invoke:
            result = collector._perform_garmin_login_once(allow_browser_fallback=True)
    invoke.assert_called_once_with(force_sso=True)
    assert result is second_api


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
