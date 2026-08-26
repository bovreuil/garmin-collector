#!/usr/bin/env python3
"""
Sign in to Garmin Connect with a real browser, then write garmin_tokens.json
compatible with garminconnect.client.Client.load / dump (upstream react stack).

Use when programmatic login hits HTTP 429. Requires:
  pip install -r requirements-browser.txt
  python -m playwright install chromium

Env (same as collector): GARMIN_EMAIL, GARMIN_PASSWORD, optional GARMINTOKENS,
optional GARMIN_PLAYWRIGHT_PROFILE (persistent Chrome user data; default
``.garmin-browser-profile/``), optional GARMIN_PLAYWRIGHT_EPHEMERAL=1,
optional .env via python-dotenv.

After login, tokens are resolved from: ``JWT_WEB`` cookie and intercepted
``connect-csrf-token`` requests; then ``localStorage["Token"].access_token`` with CSRF
from storage/cookies; then ``di-oauth/refresh`` JSON if needed.

A complete di-oauth token pair from the network hook is accepted as soon as it
arrives; cookie/localStorage pairs are polled without an artificial post-load delay.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import platform
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from garminconnect.client import Client

_LOGGER = logging.getLogger(__name__)

_CONNECT_URL_RE = re.compile(r".*connect\.garmin\.com.*", re.I)

# Navigate here after SSO; gc-api/di-oauth Referer matches client.py (app shell, not /modern/).
_CONNECT_APP_HOME = "https://connect.garmin.com/app/home"
_MODERN_ENTRY = _CONNECT_APP_HOME

# XHR used by the SPA (see Network → Host.connectApiHost).
_CONNECT_API_HOST_PREF = (
    "https://connect.garmin.com/modern/system-service/preference/Host.connectApiHost"
)

# Deep-link to portal (sometimes stricter bot checks than arriving via Connect).
_PORTAL_SIGNIN = (
    "https://sso.garmin.com/portal/sso/en-US/sign-in"
    "?clientId=GarminConnect"
    "&service=https%3A%2F%2Fconnect.garmin.com%2Fapp%2Fhome"
)

# macOS Chrome UA — closer to a typical MacBook than the Windows UA we used before.
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""

def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _header_get_ci(headers: dict[str, str], name: str) -> str | None:
    """Playwright header keys may vary in casing."""
    want = name.lower()
    for k, v in headers.items():
        if k.lower() == want:
            return v
    return None


def resolve_token_dir(explicit: str | None = None) -> Path:
    """Match collector.resolve_tokenstore_path semantics (paths relative to repo root)."""
    root = _project_root()
    raw = explicit or os.getenv("GARMINTOKENS")
    if not raw:
        return root / ".garmin-tokens"
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (root / p).resolve()
    return p


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _falsy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("0", "false", "off", "no")


def resolve_use_chrome(*, cli_chrome_flag: bool = False) -> bool:
    """
    Use system Google Chrome (``channel=chrome``) instead of Playwright Chromium.

    On Windows the default is Chrome so a profile seeded with ``--chrome`` matches
    collector subprocess recovery (same binary + user-data-dir).
    """
    if cli_chrome_flag:
        return True
    if _falsy_env("GARMIN_PLAYWRIGHT_CHROME"):
        return False
    if _truthy_env("GARMIN_PLAYWRIGHT_CHROME"):
        return True
    return platform.system() == "Windows"


def _clear_profile_singleton_locks(profile_dir: Path) -> None:
    """Remove stale Chromium/Chrome singleton files after a crashed browser."""
    candidates = [
        profile_dir / name
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket")
    ]
    default = profile_dir / "Default"
    if default.is_dir():
        candidates.extend(default / name for name in ("SingletonLock", "SingletonCookie"))
    for path in candidates:
        try:
            if path.exists() or path.is_symlink():
                path.unlink()
        except OSError as e:
            _LOGGER.debug("Could not remove profile lock %s: %s", path, e)


def resolve_browser_profile_dir(
    explicit: str | None = None,
    *,
    ephemeral: bool = False,
) -> Path | None:
    """
    Directory for ``launch_persistent_context`` (cookies, SSO trust, Cloudflare).

    Returns None when ephemeral (fresh context each run).
    """
    if ephemeral or _truthy_env("GARMIN_PLAYWRIGHT_EPHEMERAL"):
        return None
    root = _project_root()
    raw = explicit if explicit is not None else os.getenv("GARMIN_PLAYWRIGHT_PROFILE")
    if raw is not None and str(raw).strip().lower() in (
        "0",
        "false",
        "off",
        "no",
        "ephemeral",
        "none",
    ):
        return None
    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (root / p).resolve()
        return p
    return root / ".garmin-browser-profile"


def _sso_form_visible(page: Any, *, timeout_ms: int = 5000) -> bool:
    """True when Garmin SSO email field is on screen (needs password login)."""
    loc = page.locator(
        'input[type="email"], input[name="email"], input#email, '
        'input[name="username"], input[autocomplete="username"], '
        'input[autocomplete="email"]'
    ).first
    try:
        return loc.is_visible(timeout=timeout_ms)
    except Exception:  # noqa: BLE001
        return False


def _on_connect_app(page: Any) -> bool:
    return bool(_CONNECT_URL_RE.search(page.url))


def _navigate_to_fresh_sso(page: Any) -> None:
    """Sign out of Connect/SSO so a stale persistent profile must re-authenticate."""
    _LOGGER.info("Force SSO: clearing Connect session before sign-in")
    for url in (
        "https://connect.garmin.com/modern/logout",
        "https://sso.garmin.com/portal/sso/en-US/logout?clientId=GarminConnect",
    ):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            time.sleep(0.8)
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("Logout navigate %s: %s", url, e)
    page.goto(_PORTAL_SIGNIN, wait_until="load", timeout=120_000)
    time.sleep(1.0)


def _check_remember_me(page: Any) -> None:
    """Tick SSO 'Remember Me' when present (longer-lived SSO cookies)."""
    if "sso.garmin.com" not in page.url and "sign-in" not in page.url.lower():
        return
    short = 3000
    try:
        by_role = page.get_by_role("checkbox", name=re.compile(r"remember\s*me", re.I))
        if by_role.count() > 0:
            box = by_role.first
            if not box.is_checked(timeout=short):
                box.check(force=True, timeout=short)
                _LOGGER.info("Checked Remember Me (role=checkbox)")
                return
    except Exception:  # noqa: BLE001
        pass
    try:
        inp = page.locator('input[name="remember"][type="checkbox"]').first
        if inp.count() == 0:
            return
        if inp.is_checked(timeout=short):
            return
        label = page.locator(
            "fieldset.signin__form__input--remember label, "
            'g-checkbox:has-text("Remember Me") label'
        ).first
        if label.count() > 0:
            label.click(timeout=short)
            _LOGGER.info("Checked Remember Me (label click)")
            return
        inp.check(force=True, timeout=short)
        _LOGGER.info("Checked Remember Me (force check on input)")
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("Remember Me not set: %s", e)


def _cookies_list_to_dict(cookies: list[dict[str, Any]]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for c in cookies:
        merged[c["name"]] = c["value"]
    return merged


def _requests_session_from_playwright_cookies(
    cookies_raw: list[dict[str, Any]],
    *,
    user_agent: str,
) -> requests.Session:
    """Replay Connect API calls with the same cookie domains/paths the browser sent."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        }
    )
    jar = requests.cookies.RequestsCookieJar()
    for c in cookies_raw:
        domain = c.get("domain") or ".garmin.com"
        path = c.get("path") or "/"
        jar.set(c["name"], c["value"], domain=domain, path=path)
    session.cookies = jar
    return session


def _tokens_from_refresh_post(
    cookies_raw: list[dict[str, Any]],
    *,
    user_agent: str,
) -> tuple[str | None, str | None]:
    """POST di-oauth/refresh with browser cookies (fallback if network capture misses JSON)."""
    session = _requests_session_from_playwright_cookies(cookies_raw, user_agent=user_agent)
    r = session.post(
        "https://connect.garmin.com/services/auth/token/di-oauth/refresh",
        headers={
            "Accept": "application/json",
            "NK": "NT",
            "Referer": _CONNECT_APP_HOME,
        },
        timeout=60,
    )
    if r.status_code not in (200, 201):
        _LOGGER.debug("refresh POST returned %s: %s", r.status_code, r.text[:500])
        return None, None
    try:
        data = r.json()
    except json.JSONDecodeError:
        return None, None
    et, ct = _extract_refresh_tokens(data)
    return et, ct


def _extract_refresh_tokens(
    data: Any,
    _depth: int = 0,
) -> tuple[str | None, str | None]:
    """Find encryptedToken + csrfToken in Garmin refresh JSON (nested or snake_case)."""
    if _depth > 8 or data is None:
        return None, None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None, None
    if isinstance(data, list):
        for item in data:
            e2, c2 = _extract_refresh_tokens(item, _depth + 1)
            if e2 and c2:
                return e2, c2
        return None, None
    if not isinstance(data, dict):
        return None, None

    et = (
        data.get("encryptedToken")
        or data.get("encrypted_token")
        or data.get("encryptedAccessToken")
    )
    ct = data.get("csrfToken") or data.get("csrf_token") or data.get("csrf")
    if et and ct:
        return str(et), str(ct)

    for v in data.values():
        if isinstance(v, dict):
            e2, c2 = _extract_refresh_tokens(v, _depth + 1)
            if e2 and c2:
                return e2, c2
    return None, None


def _jwt_csrf_from_response_headers(headers: dict[str, str]) -> tuple[str | None, str | None]:
    """Parse JWT_WEB / csrf from Set-Cookie when the refresh body is empty."""
    parts: list[str] = []
    for k, v in headers.items():
        if k.lower() == "set-cookie":
            parts.append(v)
    if not parts:
        return None, None
    blob = "\n".join(parts)
    jwt_m = re.search(r"JWT_WEB=([^;\s]+)", blob)
    jwt = jwt_m.group(1) if jwt_m else None
    csrf = None
    for pat in (
        r"csrfToken=([^;\s]+)",
        r"csrf_token=([^;\s]+)",
        r"connect-csrf-token=([^;\s]+)",
        r"XSRF-TOKEN=([^;\s]+)",
    ):
        m = re.search(pat, blob, re.I)
        if m:
            csrf = m.group(1)
            break
    return (jwt, csrf)


def _di_oauth_refresh_in_page(page: Any, context: Any) -> dict[str, str] | None:
    """
    POST di-oauth/refresh from inside the page (same cookie jar as the SPA).
    Playwright's context.request can see a different Set-Cookie / jar than fetch().
    """
    try:
        result = page.evaluate(
            f"""async () => {{
                const r = await fetch('https://connect.garmin.com/services/auth/token/di-oauth/refresh', {{
                    method: 'POST',
                    credentials: 'include',
                    headers: {{
                        'Accept': 'application/json',
                        'NK': 'NT',
                        'Referer': '{_CONNECT_APP_HOME}',
                        'Origin': 'https://connect.garmin.com',
                    }},
                }});
                const text = await r.text();
                let json = null;
                try {{ json = JSON.parse(text); }} catch (e) {{}}
                const headers = Object.fromEntries(r.headers.entries());
                return {{ status: r.status, text, json, headers }};
            }}"""
        )
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("In-page refresh evaluate failed: %s", e)
        return None

    status = int(result.get("status") or 0)
    text = result.get("text") or ""
    _LOGGER.info("In-page di-oauth/refresh: HTTP %s", status)
    if status not in (200, 201):
        return None

    raw: Any = result.get("json")
    if raw is None and text.strip():
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            raw = None

    rh: dict[str, str] = dict(result.get("headers") or {})
    rh_lower = {str(k).lower(): v for k, v in rh.items()}

    et, ct = _extract_refresh_tokens(raw) if raw is not None else (None, None)
    hj, hc = _jwt_csrf_from_response_headers(rh)
    et = et or hj
    ct = ct or hc
    if not ct:
        hv = (
            rh_lower.get("connect-csrf-token")
            or rh_lower.get("x-csrf-token")
            or rh_lower.get("x-xsrf-token")
        )
        if isinstance(hv, str) and hv.strip():
            ct = hv.strip()
    cj, cc = _tokens_from_connect_cookies(context.cookies())
    et = et or cj
    ct = ct or cc
    if not et or not ct:
        time.sleep(0.5)
        cj2, cc2 = _tokens_from_connect_cookies(context.cookies())
        et = et or cj2
        ct = ct or cc2

    if et and ct:
        return {"encryptedToken": et, "csrfToken": ct}
    _LOGGER.info(
        "In-page refresh: still no token pair (body keys %s, text[:120]=%r)",
        list(raw.keys()) if isinstance(raw, dict) else raw,
        text[:120],
    )
    return None


def _di_oauth_refresh_playwright(context: Any) -> dict[str, str] | None:
    """
    POST di-oauth/refresh using Playwright's APIRequestContext — it shares the real
    browser cookie jar. Plain ``requests`` often fails (SameSite / host-only cookies).
    """
    url = "https://connect.garmin.com/services/auth/token/di-oauth/refresh"
    headers = {
        "Accept": "application/json",
        "NK": "NT",
        "Referer": _CONNECT_APP_HOME,
        "Origin": "https://connect.garmin.com",
    }
    try:
        resp = context.request.post(url, headers=headers)
        status = resp.status
        text = resp.text() or ""
        _LOGGER.info("Playwright cookie-jar refresh: HTTP %s", status)
        if status not in (200, 201):
            _LOGGER.warning("Refresh failed; body[:400]=%r", text[:400])
            return None

        raw: Any = None
        if text.strip():
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                _LOGGER.warning("Refresh body is not JSON: %r", text[:400])
        else:
            _LOGGER.warning(
                "Refresh HTTP %s has empty body; using Set-Cookie + browser cookies only.",
                status,
            )

        et, ct = _extract_refresh_tokens(raw) if raw is not None else (None, None)
        hj, hc = _jwt_csrf_from_response_headers(dict(resp.headers))
        et = et or hj
        ct = ct or hc
        lower_h = {str(k).lower(): v for k, v in resp.headers.items()}
        if not ct:
            hv = (
                lower_h.get("connect-csrf-token")
                or lower_h.get("x-csrf-token")
                or lower_h.get("x-xsrf-token")
            )
            if isinstance(hv, str) and hv.strip():
                ct = hv.strip()
        cj, cc = _tokens_from_connect_cookies(context.cookies())
        et = et or cj
        ct = ct or cc

        if not et or not ct:
            time.sleep(0.5)
            cj2, cc2 = _tokens_from_connect_cookies(context.cookies())
            et = et or cj2
            ct = ct or cc2

        if et and ct:
            return {"encryptedToken": et, "csrfToken": ct}

        hdr_dbg: dict[str, Any] = {}
        for k in (
            "connect-csrf-token",
            "x-csrf-token",
            "x-xsrf-token",
            "set-cookie",
        ):
            v = lower_h.get(k)
            if v:
                hdr_dbg[k] = (
                    (v[:120] + "…") if isinstance(v, str) and len(v) > 120 else v
                )
        _LOGGER.warning(
            "Refresh had no token pair after merges. JSON keys: %s; body[:240]=%r; "
            "csrf-like headers: %s",
            list(raw.keys()) if isinstance(raw, dict) else raw,
            text[:240],
            hdr_dbg,
        )
    except Exception as e:  # noqa: BLE001
        _LOGGER.warning("Playwright refresh error: %s", e)
    return None


def _tokens_from_connect_cookies(
    cookies_raw: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """JWT_WEB often mirrors encryptedToken; CSRF may appear in a *csrf* cookie name."""
    by_name = {c["name"]: c["value"] for c in cookies_raw}
    jwt = by_name.get("JWT_WEB")
    csrf: str | None = None
    for key in (
        "csrfToken",
        "CSRF-TOKEN",
        "XSRF-TOKEN",
        "_csrf",
        "connect-csrf-token",
    ):
        if key in by_name and by_name[key]:
            csrf = by_name[key]
            break
    if not csrf:
        for name, val in by_name.items():
            if "csrf" in name.lower() and val and len(val) > 4:
                csrf = val
                break
    return (str(jwt) if jwt else None, str(csrf) if csrf else None)


def _merge_token_pair(
    jwt: str | None,
    csrf: str | None,
    cookies_raw: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    cj, cc = _tokens_from_connect_cookies(cookies_raw)
    jwt = jwt or cj
    csrf = csrf or cc
    return jwt, csrf


def _csrf_from_web_storage(page: Any) -> str | None:
    """Scan storage for a Garmin API csrf UUID (plain or inside JSON blobs)."""
    try:
        v = page.evaluate(
            r"""() => {
                const re = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
                const tryObj = (o) => {
                    if (!o || typeof o !== 'object') return null;
                    for (const k of [
                        'csrfToken', 'connectCsrfToken', 'connect_csrf_token',
                        'csrf', 'diCsrf', 'connect-csrf-token',
                    ]) {
                        const x = o[k];
                        if (typeof x === 'string' && re.test(x)) return x;
                    }
                    return null;
                };
                for (const store of [localStorage, sessionStorage]) {
                    for (let i = 0; i < store.length; i++) {
                        const k = store.key(i);
                        const val = store.getItem(k);
                        if (!val) continue;
                        if (re.test(val) && val.length === 36) {
                            const kl = (k || '').toLowerCase();
                            if (kl.includes('csrf') || kl.includes('xsrf')) return val;
                        }
                        try {
                            const j = JSON.parse(val);
                            const hit = tryObj(j);
                            if (hit) return hit;
                        } catch (e) {}
                    }
                }
                return null;
            }"""
        )
        return str(v) if v else None
    except Exception:  # noqa: BLE001
        return None


def _access_token_from_local_storage(page: Any) -> str | None:
    """
    Connect SPA stores an OAuth-style bearer JWT under localStorage key ``Token``
    (field ``access_token``). Used when ``JWT_WEB`` is absent or incomplete.
    """
    try:
        raw = page.evaluate("() => localStorage.getItem('Token')")
        if not raw or not str(raw).strip():
            return None
        obj = json.loads(str(raw))
        at = obj.get("access_token")
        return str(at) if at else None
    except Exception:  # noqa: BLE001
        return None


def _tokens_from_connect_session(
    page: Any,
    cookies_raw: list[dict[str, Any]],
    csrf_from_request: dict[str, str],
) -> dict[str, str] | None:
    """
    Build encryptedToken + csrfToken for garmin_tokens.json.

    Prefer ``JWT_WEB`` cookie; fall back to ``Token.access_token`` in localStorage.
    CSRF: ``connect-csrf-token`` from intercepted requests, then cookies, then storage.
    """
    by_name = {c["name"]: c["value"] for c in cookies_raw}
    jwt = by_name.get("JWT_WEB")
    src = "JWT_WEB cookie"
    if not jwt:
        jwt = _access_token_from_local_storage(page)
        src = "localStorage Token.access_token" if jwt else src
    csrf = csrf_from_request.get("connect-csrf-token")
    if not csrf:
        _, cc = _tokens_from_connect_cookies(cookies_raw)
        csrf = cc
    if not csrf:
        csrf = _csrf_from_web_storage(page)

    if jwt and csrf:
        _LOGGER.info("Using JWT from: %s", src)
        return {"encryptedToken": jwt, "csrfToken": csrf}
    return None


def _apply_tokens_to_client(
    jwt: str,
    csrf: str,
    cookie_dict: dict[str, str],
    *,
    gc_api_user_agent: str | None = None,
) -> Client:
    client = Client()
    client.jwt_web = jwt
    client.csrf_token = csrf
    if gc_api_user_agent and gc_api_user_agent.strip():
        client.gc_api_user_agent = gc_api_user_agent.strip()
    for name, value in cookie_dict.items():
        client.cs.cookies.set(name, value, domain=".garmin.com", path="/")
    client.cs.cookies.set("JWT_WEB", jwt, domain=".garmin.com", path="/")
    return client


def _verify_session(token_dir: Path) -> None:
    from garminconnect import Garmin

    path_str = str(token_dir)
    api = Garmin()
    try:
        api.login(path_str)
    except Exception as e:
        _LOGGER.error(
            "Session verify failed (login or socialProfile): %s",
            e,
            exc_info=True,
        )
        raise RuntimeError(
            "Verify failed: could not load social profile with saved tokens. "
            "If the browser closed before the dashboard appeared, try again; "
            "otherwise check logs above for API Error status."
        ) from e
    if not api.display_name:
        raise RuntimeError(
            "Login succeeded but display_name is missing — check socialProfile URL capture."
        )


def _fill_sso_and_maybe_submit(
    page: Any,
    email: str,
    password: str,
    token_dir: Path,
    *,
    no_submit: bool,
) -> None:
    email_box = page.locator(
        'input[type="email"], input[name="email"], input#email, '
        'input[name="username"], input[autocomplete="username"], '
        'input[autocomplete="email"]'
    ).first
    email_box.click()
    email_box.fill(email)
    pw_box = page.locator(
        'input[type="password"], input[name="password"], input#password'
    ).first
    pw_box.wait_for(state="visible", timeout=30000)
    pw_box.fill(password)
    _check_remember_me(page)

    if no_submit:
        _LOGGER.info("Filled credentials. Click “Sign in” in the browser when ready.")
        return

    role_btn = page.get_by_role("button", name=re.compile(r"sign\s*in|log\s*in", re.I))
    if role_btn.count() > 0:
        role_btn.first.click()
    else:
        page.locator('button[type="submit"]').first.click()

    for _ in range(25):
        time.sleep(1)
        if _on_connect_app(page):
            break
        if _sso_error_banner_visible(page):
            dbg = _debug_shot(page, token_dir)
            raise RuntimeError(
                "Garmin SSO showed an error (common when automation is detected). "
                f"Screenshot: {dbg}. Try: --chrome (install Google Chrome), "
                "--no-submit (you click Sign in), --entry portal, or --manual "
                "(complete login yourself)."
            )


def _sso_error_banner_visible(page: Any) -> bool:
    try:
        for pattern in (
            r"unexpected error",
            r"something went wrong",
            r"unable to sign in",
            r"error occurred",
        ):
            loc = page.get_by_text(re.compile(pattern, re.I))
            if loc.count() > 0 and loc.first.is_visible():
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _debug_shot(page: Any, token_dir: Path) -> Path:
    path = token_dir / "garmin-login-debug.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), full_page=True)
    return path


def _launch_browser_context(
    p: Any,
    profile_dir: Path | None,
    *,
    headless: bool,
    use_chrome: bool,
) -> tuple[Any | None, Any, Any]:
    """Return (browser_or_none, context, page). browser is set only for ephemeral launch."""
    ignore_args = ["--enable-automation"]
    if use_chrome:
        # Playwright adds --no-sandbox by default; system Chrome warns it is unsupported.
        ignore_args.append("--no-sandbox")
    launch_kwargs: dict[str, Any] = {
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled"],
        "ignore_default_args": ignore_args,
    }
    if use_chrome:
        launch_kwargs["channel"] = "chrome"

    context_kwargs: dict[str, Any] = {
        "viewport": {"width": 1280, "height": 900},
        "user_agent": _DEFAULT_UA,
        "locale": "en-GB",
        "timezone_id": "Europe/London",
        "color_scheme": "light",
    }

    def _launch_persistent() -> Any:
        assert profile_dir is not None
        profile_dir.mkdir(parents=True, exist_ok=True)
        _clear_profile_singleton_locks(profile_dir)
        return p.chromium.launch_persistent_context(
            str(profile_dir),
            **launch_kwargs,
            **context_kwargs,
        )

    def _launch_ephemeral() -> tuple[Any, Any]:
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(**context_kwargs)
        return browser, context

    if profile_dir is not None:
        browser_label = "Google Chrome" if use_chrome else "Playwright Chromium"
        _LOGGER.info(
            "Using persistent browser profile (%s): %s",
            browser_label,
            profile_dir,
        )
        try:
            context = _launch_persistent()
        except Exception as e:
            if use_chrome:
                _LOGGER.warning(
                    "Persistent launch with channel=chrome failed (%s); using Chromium.",
                    e,
                )
                launch_kwargs.pop("channel", None)
                ignore_args_chrome = list(launch_kwargs.get("ignore_default_args") or [])
                if "--no-sandbox" in ignore_args_chrome:
                    ignore_args_chrome.remove("--no-sandbox")
                launch_kwargs["ignore_default_args"] = ignore_args_chrome
                context = _launch_persistent()
            else:
                _LOGGER.warning(
                    "Persistent Chromium launch failed (%s); retrying with channel=chrome.",
                    e,
                )
                launch_kwargs["channel"] = "chrome"
                if "--no-sandbox" not in launch_kwargs.get("ignore_default_args", []):
                    launch_kwargs.setdefault("ignore_default_args", ["--enable-automation"])
                    launch_kwargs["ignore_default_args"] = list(
                        launch_kwargs["ignore_default_args"]
                    ) + ["--no-sandbox"]
                context = _launch_persistent()
        context.add_init_script(_STEALTH_INIT)
        if not context.pages:
            time.sleep(0.5)
        if context.pages:
            page = context.pages[0]
        else:
            page = context.new_page()
        return None, context, page

    _LOGGER.info("Ephemeral browser context (no persistent profile)")
    try:
        browser, context = _launch_ephemeral()
    except Exception as e:
        if use_chrome:
            _LOGGER.warning("Launch with channel=chrome failed (%s); using Chromium.", e)
            launch_kwargs.pop("channel", None)
            browser, context = _launch_ephemeral()
        else:
            raise
    context.add_init_script(_STEALTH_INIT)
    page = context.new_page()
    return browser, context, page


def _close_browser_context(
    browser: Any | None,
    context: Any,
) -> None:
    context.close()
    if browser is not None:
        browser.close()


def run_login(
    email: str,
    password: str,
    token_dir: Path,
    *,
    headless: bool,
    verify: bool,
    use_chrome: bool,
    entry: str,
    manual: bool,
    no_submit: bool,
    profile_dir: Path | None,
    force_sso: bool = False,
) -> Path:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "Playwright is not installed. Run:\n"
            "  pip install -r requirements-browser.txt\n"
            "  python -m playwright install chromium"
        ) from e

    if manual and no_submit:
        raise ValueError("--manual and --no-submit both set; use only one.")

    token_dir.mkdir(parents=True, exist_ok=True)
    out_file = token_dir / "garmin_tokens.json"

    captured: dict[str, object] = {}

    def on_response(response: Any) -> None:
        try:
            u = response.url
            # SPA calls ``.../socialProfile/{displayName}``; persist that suffix for Python gc-api.
            if (
                response.status in (200, 201)
                and "connect.garmin.com" in u
                and "/socialProfile/" in u
                and "userprofile-service" in u
            ):
                m = re.search(r"/socialProfile/([^/?#]+)", u)
                if m and m.group(1):
                    captured["profile_display_name"] = m.group(1)
            if response.status >= 400 and (
                "sso.garmin.com" in u or "sign-in" in u or "/portal/sso" in u
            ):
                _LOGGER.debug("SSO HTTP %s %s", response.status, u[:160])
            if response.status not in (200, 201) or "connect.garmin.com" not in u:
                return
            if "refresh" in captured:
                return
            ct_hdr = response.headers.get("content-type", "")
            if "application/json" not in ct_hdr:
                return
            if not any(
                k in u.lower()
                for k in ("di-oauth", "di_oauth", "/auth/token", "token", "oauth")
            ):
                return
            j = response.json()
            if (
                isinstance(j, dict)
                and j.get("encryptedToken")
                and j.get("csrfToken")
            ):
                captured["refresh"] = j
        except Exception:  # noqa: BLE001
            pass

    with sync_playwright() as p:
        browser, context, page = _launch_browser_context(
            p,
            profile_dir,
            headless=headless,
            use_chrome=use_chrome,
        )
        page.on("response", on_response)

        csrf_from_request: dict[str, str] = {}

        def on_request(req: Any) -> None:
            try:
                if "garmin.com" not in req.url:
                    return
                tok = _header_get_ci(dict(req.headers), "connect-csrf-token")
                if tok:
                    csrf_from_request["connect-csrf-token"] = tok
            except Exception:  # noqa: BLE001
                pass

        page.on("request", on_request)

        skipped_sso_shortcut = False
        if force_sso and not manual:
            _navigate_to_fresh_sso(page)
        else:
            start_url = _PORTAL_SIGNIN if force_sso else (
                _MODERN_ENTRY if entry == "modern" else _PORTAL_SIGNIN
            )
            _LOGGER.info("Opening %s", start_url)
            page.goto(start_url, wait_until="load", timeout=120000)
            time.sleep(1.2)

        for sel in (
            "#onetrust-accept-btn-handler",
            "button:has-text('Accept All')",
            "button:has-text('I Agree')",
        ):
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=2500):
                    loc.click()
                    time.sleep(0.6)
            except Exception:  # noqa: BLE001
                pass

        if not manual:
            if _sso_form_visible(page, timeout_ms=8000):
                _fill_sso_and_maybe_submit(
                    page, email, password, token_dir, no_submit=no_submit
                )
            elif _on_connect_app(page):
                if force_sso:
                    _LOGGER.info(
                        "Force SSO: Connect loaded without SSO form; signing out and retrying portal"
                    )
                    _navigate_to_fresh_sso(page)
                    if _sso_form_visible(page, timeout_ms=120_000):
                        _fill_sso_and_maybe_submit(
                            page, email, password, token_dir, no_submit=no_submit
                        )
                    else:
                        dbg = _debug_shot(page, token_dir)
                        raise RuntimeError(
                            "Force SSO: portal sign-in did not appear after logout. "
                            f"Screenshot: {dbg}"
                        )
                else:
                    skipped_sso_shortcut = True
                    _LOGGER.info(
                        "Persistent profile already signed in; skipping SSO (exporting tokens)"
                    )
            else:
                _LOGGER.info("Waiting for SSO form or Connect redirect…")
                try:
                    page.wait_for_url(_CONNECT_URL_RE, timeout=60_000)
                except Exception:  # noqa: BLE001
                    pass
                if _on_connect_app(page):
                    _LOGGER.info("Reached Connect without SSO form")
                elif _sso_form_visible(page, timeout_ms=120_000):
                    _fill_sso_and_maybe_submit(
                        page, email, password, token_dir, no_submit=no_submit
                    )
                else:
                    dbg = _debug_shot(page, token_dir)
                    raise RuntimeError(
                        "Neither SSO sign-in nor Connect home appeared. "
                        f"Screenshot: {dbg}"
                    )
        else:
            _LOGGER.info(
                "Manual mode: complete sign-in in the browser. "
                "Waiting up to 10 minutes for connect.garmin.com."
            )

        nav_timeout = 600_000 if manual or no_submit else 180_000
        try:
            page.wait_for_url(_CONNECT_URL_RE, timeout=nav_timeout)
        except Exception as e:
            dbg = _debug_shot(page, token_dir)
            raise RuntimeError(
                f"Did not reach connect.garmin.com in time. Screenshot: {dbg}"
            ) from e

        try:
            page.wait_for_load_state("load", timeout=60_000)
        except Exception:  # noqa: BLE001
            pass

        connect_landed_at = time.time()

        def _refresh_from_di_oauth_hook() -> bool:
            ref = captured.get("refresh")
            return bool(
                isinstance(ref, dict)
                and ref.get("encryptedToken")
                and ref.get("csrfToken")
            )

        def _capture_from_session() -> bool:
            """True if hook or cookies/storage already have a full JWT+CSRF pair."""
            if _refresh_from_di_oauth_hook():
                return True
            tok = _tokens_from_connect_session(
                page, context.cookies(), csrf_from_request
            )
            if tok:
                captured["refresh"] = tok
                elapsed = time.time() - connect_landed_at
                _LOGGER.info(
                    "Captured session (JWT_WEB or localStorage Token + CSRF from "
                    "requests/storage/cookies; %.1fs after Connect load)",
                    elapsed,
                )
                return True
            return False

        _LOGGER.info(
            "Connect loaded; polling for tokens (di-oauth JSON when present; "
            "otherwise cookies/localStorage as soon as available).",
        )

        # Poll after Connect until JWT+CSRF are available or timeout.
        poll_interval = 0.2
        max_wait = 120.0 if (manual or no_submit) else 90.0
        deadline = time.time() + max_wait
        while time.time() < deadline:
            if _capture_from_session():
                break
            time.sleep(poll_interval)

        # Avoid networkidle here: Garmin SPA may keep connections open, burning 60–90s.

        if "refresh" not in captured:
            _LOGGER.info("Reloading dashboard to trigger XHRs with connect-csrf-token")
            try:
                page.goto(_CONNECT_APP_HOME, wait_until="load", timeout=120000)
                connect_landed_at = time.time()
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("Reload: %s", e)
            reload_deadline = time.time() + 45.0
            while time.time() < reload_deadline:
                if _capture_from_session():
                    _LOGGER.info("Captured session after dashboard reload")
                    break
                time.sleep(poll_interval)

        if "refresh" not in captured:
            ws = _csrf_from_web_storage(page)
            if ws:
                csrf_from_request.setdefault("connect-csrf-token", ws)
            tok = _tokens_from_connect_session(page, context.cookies(), csrf_from_request)
            if tok:
                captured["refresh"] = tok
                _LOGGER.info("Captured session after storage CSRF merge")

        if "refresh" not in captured:
            pw = _di_oauth_refresh_in_page(page, context)
            if not pw:
                pw = _di_oauth_refresh_playwright(context)
            if pw:
                captured["refresh"] = pw

        if "profile_display_name" not in captured:
            _LOGGER.info(
                "Waiting for socialProfile/{{displayName}} request (needed for gc-api path)…"
            )
            time.sleep(4.0)

        cookies_raw = context.cookies()
        _close_browser_context(browser, context)

    cookie_dict = _cookies_list_to_dict(cookies_raw)

    jwt: str | None = None
    csrf: str | None = None
    ref = captured.get("refresh")
    if isinstance(ref, dict):
        jwt = str(ref["encryptedToken"]) if ref.get("encryptedToken") else None
        csrf = str(ref["csrfToken"]) if ref.get("csrfToken") else None

    jwt, csrf = _merge_token_pair(jwt, csrf, cookies_raw)

    if not jwt or not csrf:
        _LOGGER.info("Trying di-oauth/refresh via requests (last resort)")
        jwt, csrf = _merge_token_pair(
            *_tokens_from_refresh_post(cookies_raw, user_agent=_DEFAULT_UA),
            cookies_raw,
        )

    if not jwt or not csrf:
        raise RuntimeError(
            "Could not obtain JWT/csrf after browser login. "
            "If you reached Connect in the browser, inspect DevTools → Network for "
            "POST services/auth/token/di-oauth/refresh. Otherwise try --chrome, --manual, "
            "--force-sso, or --no-submit."
        )

    client = _apply_tokens_to_client(
        jwt, csrf, cookie_dict, gc_api_user_agent=_DEFAULT_UA
    )
    client._tokenstore_path = str(token_dir)
    pd = captured.get("profile_display_name")
    if isinstance(pd, str) and pd.strip():
        client.profile_display_name = pd.strip()
        _LOGGER.info("Recorded profile display name for gc-api: %s", client.profile_display_name)
    client.dump(str(token_dir))
    _LOGGER.info("Wrote %s", out_file)

    if verify:
        _LOGGER.info("Verifying session via Connect API")
        _verify_session(token_dir)

    return out_file


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv(_project_root() / ".env")

    parser = argparse.ArgumentParser(
        description="Browser login to seed garmin_tokens.json for garmin-collector."
    )
    parser.add_argument(
        "--token-dir",
        type=Path,
        default=None,
        help="Directory for garmin_tokens.json (default: GARMINTOKENS or .garmin-tokens)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headless (headed is default; easier when Garmin shows challenges)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="After saving, load tokens and fetch social profile (sanity check)",
    )
    parser.add_argument(
        "--chrome",
        action="store_true",
        help="Use Google Chrome instead of Playwright Chromium (often fixes Garmin 'unexpected error')",
    )
    parser.add_argument(
        "--entry",
        choices=("modern", "portal"),
        default="modern",
        help="Start from Connect app home (default) or deep-link to SSO portal",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Do not fill the form; sign in yourself. Waits up to 10 minutes.",
    )
    parser.add_argument(
        "--no-submit",
        action="store_true",
        help="Fill email/password but you click Sign in (helps if auto-submit is blocked)",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=None,
        help="Persistent Chrome user data dir (default: GARMIN_PLAYWRIGHT_PROFILE or "
        ".garmin-browser-profile). Reuse reduces SSO/Cloudflare challenges.",
    )
    parser.add_argument(
        "--ephemeral",
        action="store_true",
        help="Fresh browser context each run (no persistent profile)",
    )
    parser.add_argument(
        "--force-sso",
        action="store_true",
        help="Sign out and use portal SSO (skip 'already signed in' token export shortcut)",
    )
    args = parser.parse_args()

    email = os.getenv("GARMIN_EMAIL", "")
    password = os.getenv("GARMIN_PASSWORD", "")
    if not args.manual and (not email or not password):
        print(
            "Set GARMIN_EMAIL and GARMIN_PASSWORD (e.g. in .env), "
            "or use --manual to log in yourself.",
            file=sys.stderr,
        )
        sys.exit(1)

    token_dir = resolve_token_dir(
        str(args.token_dir) if args.token_dir is not None else None
    )
    profile_dir = resolve_browser_profile_dir(
        str(args.profile_dir) if args.profile_dir is not None else None,
        ephemeral=args.ephemeral,
    )

    use_chrome = resolve_use_chrome(cli_chrome_flag=args.chrome)
    if use_chrome and not args.chrome and not _truthy_env("GARMIN_PLAYWRIGHT_CHROME"):
        _LOGGER.info("Using Google Chrome (default on Windows for persistent profile)")

    try:
        out = run_login(
            email,
            password,
            token_dir,
            headless=args.headless,
            verify=args.verify,
            use_chrome=use_chrome,
            entry=args.entry,
            manual=args.manual,
            no_submit=args.no_submit,
            profile_dir=profile_dir,
            force_sso=args.force_sso,
        )
    except Exception as e:
        _LOGGER.error("%s", e)
        sys.exit(1)

    print(f"OK: wrote {out}")


if __name__ == "__main__":
    main()
