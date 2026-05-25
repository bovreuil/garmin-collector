"""Keepalive classifies Garmin connection drops for retry (parity with jobs)."""

from __future__ import annotations

from collector import TransientGarminNetworkError, _garmin_network_error_for_retry
from garminconnect.exceptions import GarminConnectConnectionError


def test_maps_connection_error_10054_to_transient() -> None:
    exc = GarminConnectConnectionError(
        "Connection error: ('Connection aborted.', ConnectionResetError(10054, ...))"
    )
    mapped = _garmin_network_error_for_retry(exc)
    assert isinstance(mapped, TransientGarminNetworkError)


def test_non_transient_connection_error_not_mapped() -> None:
    exc = GarminConnectConnectionError("API Error 500 - server error")
    assert _garmin_network_error_for_retry(exc) is None
