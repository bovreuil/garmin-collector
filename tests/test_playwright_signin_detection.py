"""Playwright URL helpers: sign-in vs Connect app home."""

from __future__ import annotations

from scripts.garmin_playwright_login import _on_connect_app, _on_sso_signin_page


class _FakePage:
    def __init__(self, url: str) -> None:
        self.url = url


def test_signin_url_is_not_connect_app() -> None:
    signin = _FakePage(
        "https://connect.garmin.com/signin/?service=https%3A%2F%2Fconnect.garmin.com%2Fapp%2Fhome"
    )
    assert _on_sso_signin_page(signin) is True
    assert _on_connect_app(signin) is False


def test_connect_app_home_is_connect_app() -> None:
    home = _FakePage("https://connect.garmin.com/app/home")
    assert _on_sso_signin_page(home) is False
    assert _on_connect_app(home) is True


def test_sso_portal_is_not_connect_app() -> None:
    portal = _FakePage(
        "https://sso.garmin.com/portal/sso/en-US/sign-in?clientId=GarminConnect"
    )
    assert _on_sso_signin_page(portal) is True
    assert _on_connect_app(portal) is False
