#!/usr/bin/env python3
"""
Sign in to Garmin Connect with a real browser, then write garmin_tokens.json
compatible with garminconnect.client.Client.load / dump (upstream react stack).

Use when programmatic login hits HTTP 429. Requires:
  pip install -r requirements-browser.txt
  python -m playwright install chromium

Env (same as collector): GARMIN_EMAIL, GARMIN_PASSWORD, optional GARMINTOKENS,
optional .env via python-dotenv.

After login, tokens are resolved from: ``JWT_WEB`` cookie and intercepted
``connect-csrf-token`` requests; then ``localStorage["Token"].access_token`` with CSRF
from storage/cookies; then ``di-oauth/refresh`` JSON if needed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from garminconnect.client import Client

_LOGGER = logging.getLogger(__name__)

_CONNECT_URL_RE = re.compile(r".*connect\.garmin\.com.*", re.I)

# Current Connect web lands on /app/home; /modern/ often redirects there.
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
            "Referer": "https://connect.garmin.com/app/home",
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
            """async () => {
                const r = await fetch('https://connect.garmin.com/services/auth/token/di-oauth/refresh', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Accept': 'application/json',
                        'NK': 'NT',
                        'Referer': 'https://connect.garmin.com/app/home',
                        'Origin': 'https://connect.garmin.com',
                    },
                });
                const text = await r.text();
                let json = null;
                try { json = JSON.parse(text); } catch (e) {}
                const headers = Object.fromEntries(r.headers.entries());
                return { status: r.status, text, json, headers };
            }"""
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
        "Referer": "https://connect.garmin.com/app/home",
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
) -> Client:
    client = Client()
    client.jwt_web = jwt
    client.csrf_token = csrf
    for name, value in cookie_dict.items():
        client.cs.cookies.set(name, value, domain=".garmin.com", path="/")
    client.cs.cookies.set("JWT_WEB", jwt, domain=".garmin.com", path="/")
    return client


def _verify_session(token_dir: Path) -> None:
    from garminconnect import Garmin

    path_str = str(token_dir)
    api = Garmin()
    api.login(path_str)
    prof = api.client.connectapi("/userprofile-service/socialProfile")
    if not isinstance(prof, dict) or "displayName" not in prof:
        raise RuntimeError(f"Unexpected profile response: {prof!r}")


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

    launch_kwargs: dict[str, Any] = {
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled"],
        "ignore_default_args": ["--enable-automation"],
    }
    if use_chrome:
        launch_kwargs["channel"] = "chrome"

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(**launch_kwargs)
        except Exception as e:
            if use_chrome:
                _LOGGER.warning("Launch with channel=chrome failed (%s); using Chromium.", e)
                launch_kwargs.pop("channel", None)
                browser = p.chromium.launch(**launch_kwargs)
            else:
                raise

        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=_DEFAULT_UA,
            locale="en-GB",
            timezone_id="Europe/London",
            color_scheme="light",
        )
        context.add_init_script(_STEALTH_INIT)
        page = context.new_page()
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

        start_url = _MODERN_ENTRY if entry == "modern" else _PORTAL_SIGNIN
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
            email_box = page.locator(
                'input[type="email"], input[name="email"], input#email, '
                'input[name="username"], input[autocomplete="username"], '
                'input[autocomplete="email"]'
            ).first
            email_box.wait_for(state="visible", timeout=120000)
            email_box.click()
            time.sleep(0.15)
            email_box.fill(email)
            time.sleep(0.2)
            pw_box = page.locator(
                'input[type="password"], input[name="password"], input#password'
            ).first
            pw_box.wait_for(state="visible", timeout=30000)
            pw_box.fill(password)
            time.sleep(0.45)

            if no_submit:
                _LOGGER.info(
                    "Filled credentials. Click “Sign in” in the browser when ready."
                )
            else:
                role_btn = page.get_by_role(
                    "button", name=re.compile(r"sign\s*in|log\s*in", re.I)
                )
                if role_btn.count() > 0:
                    role_btn.first.click()
                else:
                    page.locator('button[type="submit"]').first.click()

                for _ in range(25):
                    time.sleep(1)
                    if "connect.garmin.com" in page.url:
                        break
                    if _sso_error_banner_visible(page):
                        dbg = _debug_shot(page, token_dir)
                        raise RuntimeError(
                            "Garmin SSO showed an error (common when automation is detected). "
                            f"Screenshot: {dbg}. Try: --chrome (install Google Chrome), "
                            "--no-submit (you click Sign in), --entry portal, or --manual "
                            "(complete login yourself)."
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

        def _capture_from_session() -> bool:
            """True if hook or cookies/storage already have a full JWT+CSRF pair."""
            ref = captured.get("refresh")
            if (
                isinstance(ref, dict)
                and ref.get("encryptedToken")
                and ref.get("csrfToken")
            ):
                return True
            tok = _tokens_from_connect_session(
                page, context.cookies(), csrf_from_request
            )
            if tok:
                captured["refresh"] = tok
                _LOGGER.info(
                    "Captured session (JWT_WEB or localStorage Token + CSRF from "
                    "requests/storage/cookies)"
                )
                return True
            return False

        # Poll as soon as we hit Connect — JWT_WEB / CSRF often appear within seconds.
        # (An older version waited ~45s only for di-oauth JSON in on_response, which often
        # has an empty body; session tokens are detected below instead.)
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
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("Reload: %s", e)
            reload_deadline = time.time() + 30.0
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

        cookies_raw = context.cookies()
        browser.close()

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
            "or --no-submit."
        )

    client = _apply_tokens_to_client(jwt, csrf, cookie_dict)
    client._tokenstore_path = str(token_dir)
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

    try:
        out = run_login(
            email,
            password,
            token_dir,
            headless=args.headless,
            verify=args.verify,
            use_chrome=args.chrome,
            entry=args.entry,
            manual=args.manual,
            no_submit=args.no_submit,
        )
    except Exception as e:
        _LOGGER.error("%s", e)
        sys.exit(1)

    print(f"OK: wrote {out}")


if __name__ == "__main__":
    main()
