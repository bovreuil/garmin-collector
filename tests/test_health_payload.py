"""Health heartbeat payload: summary roll-up and heartbeat_interval_sec."""

from __future__ import annotations

import pytest

from collector import GarminCollector


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch) -> GarminCollector:
    monkeypatch.setenv("GARMIN_EMAIL", "test@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "secret")
    monkeypatch.setenv("COLLECTOR_HEALTH_INTERVAL", "60")
    monkeypatch.setenv("GARMIN_KEEPALIVE_INTERVAL", "0")
    return GarminCollector("http://example.com", "shared-secret")


@pytest.mark.parametrize(
    "collection_ready,last_error_kind,collections_failed,keepalive_failed,expected",
    [
        (True, "none", 0, 0, "ok"),
        (True, None, 0, 0, "ok"),
        (False, "none", 0, 0, "degraded"),
        (True, "auth", 0, 0, "degraded"),
        (True, "none", 1, 0, "degraded"),
        (True, "none", 0, 1, "degraded"),
    ],
)
def test_health_summary(
    collector: GarminCollector,
    collection_ready: bool,
    last_error_kind: str | None,
    collections_failed: int,
    keepalive_failed: int,
    expected: str,
) -> None:
    assert (
        collector._health_summary(
            collection_ready=collection_ready,
            last_error_kind=last_error_kind,
            collections_failed_24h=collections_failed,
            keepalive_failed_24h=keepalive_failed,
        )
        == expected
    )


def test_build_health_payload_summary_and_interval(collector: GarminCollector) -> None:
    collector._collection_ready = True
    collector._last_error = {"kind": "none", "message": None, "at_utc": None}
    payload = collector._build_health_payload()
    assert payload["summary"] == "ok"
    assert payload["heartbeat_interval_sec"] == 60
    assert payload["garmin"]["collection_ready"] is True


def test_build_health_payload_omits_interval_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GARMIN_EMAIL", "test@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "secret")
    monkeypatch.setenv("COLLECTOR_HEALTH_INTERVAL", "0")
    c = GarminCollector("http://example.com", "shared-secret")
    c._collection_ready = True
    c._last_error = {"kind": "none", "message": None, "at_utc": None}
    payload = c._build_health_payload()
    assert "heartbeat_interval_sec" not in payload
