"""Playwright browser selection (Chrome vs Chromium) for persistent profile."""

from __future__ import annotations

import sys

import pytest

from scripts import garmin_playwright_login as gpl


def test_resolve_use_chrome_windows_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GARMIN_PLAYWRIGHT_CHROME", raising=False)
    monkeypatch.setattr(gpl.platform, "system", lambda: "Windows")
    assert gpl.resolve_use_chrome(cli_chrome_flag=False) is True


def test_resolve_use_chrome_mac_default_chromium(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GARMIN_PLAYWRIGHT_CHROME", raising=False)
    monkeypatch.setattr(gpl.platform, "system", lambda: "Darwin")
    assert gpl.resolve_use_chrome(cli_chrome_flag=False) is False


def test_resolve_use_chrome_env_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GARMIN_PLAYWRIGHT_CHROME", "0")
    monkeypatch.setattr(gpl.platform, "system", lambda: "Windows")
    assert gpl.resolve_use_chrome(cli_chrome_flag=False) is False


def test_resolve_use_chrome_cli_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GARMIN_PLAYWRIGHT_CHROME", "0")
    monkeypatch.setattr(gpl.platform, "system", lambda: "Darwin")
    assert gpl.resolve_use_chrome(cli_chrome_flag=True) is True
