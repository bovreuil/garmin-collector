"""State-of-the-art authentication engine for Garmin Connect."""

import json
import logging
import re
from pathlib import Path
from typing import Any

import requests

_LOGGER = logging.getLogger(__name__)


def _browser_hint_headers(user_agent: str) -> dict[str, str]:
    """Sec-CH-UA / Sec-Fetch-* like a CORS XHR from connect.garmin.com."""
    ua = user_agent or ""
    low = ua.lower()
    m = re.search(r"Chrome/(\d+)", ua)
    ver = m.group(1) if m else "131"
    if "android" in low:
        sec_ch = (
            f'"Not:A-Brand";v="99", "Google Chrome";v="{ver}", "Chromium";v="{ver}"'
        )
        return {
            "sec-ch-ua": sec_ch,
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }
    if "macintosh" in low or "mac os x" in low:
        sec_ch = (
            f'"Google Chrome";v="{ver}", "Chromium";v="{ver}", "Not_A Brand";v="24"'
        )
        return {
            "sec-ch-ua": sec_ch,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }
    sec_ch = (
        f'"Google Chrome";v="{ver}", "Chromium";v="{ver}", "Not_A Brand";v="24"'
    )
    return {
        "sec-ch-ua": sec_ch,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


CLIENT_ID = "GarminConnect"
SSO_SERVICE_URL = "https://connect.garmin.com/app/"

# Shipped with gc-api calls: browser-like UA (python-requests default is often blocked).
# Auth for ``connect.garmin.com/gc-api/...`` matches upstream ``react``: **cookies**
# (JWT_WEB) + CSRF — not ``Authorization: Bearer``, which can yield HTTP 403 when a
# cookie session is also present.
#
# gc-api Referer: Playwright lands on ``/app/home``; some WAF checks match that.
# Keep identical to ``scripts/garmin_playwright_login.py`` ``_DEFAULT_UA``: Garmin
# often couples JWT/cookies to the User-Agent that created the browser session;
# a mismatched UA on ``gc-api`` returns HTTP 403 for some accounts.
_GC_API_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


from .exceptions import (  # noqa: E402
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)


class Client:
    """A client to communicate with Garmin Connect."""

    def __init__(self, domain: str = "garmin.com", **kwargs: Any) -> None:
        self.domain = domain
        self._sso = f"https://sso.{domain}"
        self._connect = f"https://connect.{domain}"

        self.jwt_web: str | None = None
        self.csrf_token: str | None = None
        #: Garmin Connect ``gc-api`` social profile path suffix (e.g. ``bovreuil``).
        #: Persisted in ``garmin_tokens.json``; required for ``/socialProfile/{name}`` (2026).
        self.profile_display_name: str | None = None
        #: Optional; when set (e.g. from ``garmin_tokens.json``), overrides
        #: ``_GC_API_USER_AGENT`` for ``get_api_headers`` to match the seeding browser.
        self.gc_api_user_agent: str | None = None

        # Garth backward compatibility properties
        self.profile: dict | None = None

        self.cs: requests.Session = requests.Session()
        pool_connections = kwargs.get("pool_connections", 20)
        pool_maxsize = kwargs.get("pool_maxsize", 20)

        adapter = requests.adapters.HTTPAdapter(
            pool_connections=pool_connections,
            pool_maxsize=pool_maxsize,
        )
        self.cs.mount("https://", adapter)
        self.cs.mount("http://", adapter)

        self._tokenstore_path: str | None = None

    @property
    def is_authenticated(self) -> bool:
        return bool(self.jwt_web and self.csrf_token)

    def get_api_headers(self) -> dict[str, str]:
        if not self.is_authenticated:
            raise GarminConnectAuthenticationError("Not authenticated")
        ua = self.gc_api_user_agent or _GC_API_USER_AGENT
        base = {
            # Connect SPA uses ``*/*`` on socialProfile; strict JSON-only Accept can 403.
            "Accept": "*/*",
            "User-Agent": ua,
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "connect-csrf-token": str(self.csrf_token),
            "Origin": self._connect,
            "Referer": f"{self._connect}/app/home",
            "DI-Backend": f"connectapi.{self.domain}",
        }
        base.update(_browser_hint_headers(ua))
        return base

    def login(
        self,
        email: str,
        password: str,
        prompt_mfa: Any = None,
        return_on_mfa: bool = False,
    ) -> tuple[str | None, Any]:
        """Logs into Mobile API to perfectly bypass CF, then trades for Web JWT."""
        sess: requests.Session = requests.Session()
        sess.headers = {
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 13; Pixel 6 Build/TQ3A.230901.001) GarminConnect/4.74.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        sess.get(
            f"{self._sso}/mobile/sso/en/sign-in",
            params={"clientId": CLIENT_ID},
        )

        r = sess.post(
            f"{self._sso}/mobile/api/login",
            params={
                "clientId": CLIENT_ID,
                "locale": "en-US",
                "service": SSO_SERVICE_URL,
            },
            json={
                "username": email,
                "password": password,
                "rememberMe": False,
                "captchaToken": "",
            },
        )

        try:
            res = r.json()
        except Exception as err:
            raise GarminConnectConnectionError(
                f"Login failed (Not JSON): HTTP {r.status_code}"
            ) from err

        resp_type = res.get("responseStatus", {}).get("type")

        if resp_type == "MFA_REQUIRED":
            self._mfa_method = res.get("customerMfaInfo", {}).get(
                "mfaLastMethodUsed", "email"
            )
            self._mfa_session = sess

            if return_on_mfa:
                return "needs_mfa", self._mfa_session

            if prompt_mfa:
                mfa_code = prompt_mfa()
                self._complete_mfa(mfa_code)
                return None, None
            raise GarminConnectAuthenticationError(
                "MFA Required but no prompt_mfa mechanism supplied"
            )

        if resp_type == "SUCCESSFUL":
            ticket = res["serviceTicketId"]
            self._establish_session(ticket)
            return None, None

        if (
            "status-code" in res.get("error", {})
            and res["error"]["status-code"] == "429"
        ):
            raise GarminConnectTooManyRequestsError("429 Rate Limit")

        if resp_type == "INVALID_USERNAME_PASSWORD":
            raise GarminConnectAuthenticationError(
                "401 Unauthorized (Invalid Username or Password)"
            )

        raise GarminConnectAuthenticationError(
            f"Unhandled Garmin Login JSON, Login failed: {res}"
        )

    def _complete_mfa(self, mfa_code: str) -> None:
        r = self._mfa_session.post(
            f"{self._sso}/mobile/api/mfa/verifyCode",
            params={
                "clientId": CLIENT_ID,
                "locale": "en-US",
                "service": SSO_SERVICE_URL,
            },
            json={
                "mfaMethod": getattr(self, "_mfa_method", "email"),
                "mfaVerificationCode": mfa_code,
                "rememberMyBrowser": False,
                "reconsentList": [],
                "mfaSetup": False,
            },
        )
        res = r.json()
        if res.get("responseStatus", {}).get("type") == "SUCCESSFUL":
            ticket = res["serviceTicketId"]
            self._establish_session(ticket)
            return
        raise GarminConnectAuthenticationError(f"MFA Verification failed: {res}")

    def _establish_session(self, ticket: str) -> None:
        if not hasattr(self, "cs") or self.cs is None:
            self.cs = requests.Session()
        self.cs.headers = {
            "User-Agent": _GC_API_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        self.cs.get(SSO_SERVICE_URL, params={"ticket": ticket}, allow_redirects=True)
        r_tok = self.cs.post(
            f"{self._connect}/services/auth/token/di-oauth/refresh",
            headers={
                "Accept": "application/json",
                "NK": "NT",
                "Referer": f"{self._connect}/app/home",
            },
        )

        if r_tok.status_code not in (200, 201):
            raise GarminConnectConnectionError("Failed JWT extraction")

        jwt_data = r_tok.json()
        self.jwt_web = jwt_data.get("encryptedToken")
        self.csrf_token = jwt_data.get("csrfToken")

        if not self.jwt_web or not self.csrf_token:
            raise GarminConnectAuthenticationError(
                "Missing required JWT or CSRF tokens in response payload."
            )

        self.cs.cookies.set("JWT_WEB", self.jwt_web, domain=f".{self.domain}", path="/")

    def _refresh_session(self) -> None:
        """Silently grab fresh JWT behind the scenes."""
        if not self.is_authenticated:
            return
        try:
            r_tok = self.cs.post(
                f"{self._connect}/services/auth/token/di-oauth/refresh",
                headers={
                    "Accept": "application/json",
                    "NK": "NT",
                    "connect-csrf-token": self.csrf_token,
                    "Referer": f"{self._connect}/app/home",
                },
                timeout=10,
            )
            if r_tok.status_code in (200, 201):
                jwt_data = r_tok.json()
                if not isinstance(jwt_data, dict):
                    return
                new_jwt = jwt_data.get("encryptedToken")
                new_csrf = jwt_data.get("csrfToken")
                # Refresh can return 200 with an empty or alternate-shaped body; do not
                # overwrite in-memory tokens with None (breaks the next gc-api call with
                # Not authenticated) — see _run_request 401/403 retry path.
                if not new_jwt or not new_csrf:
                    _LOGGER.debug(
                        "di-oauth refresh returned no encryptedToken/csrfToken; "
                        "keeping existing session material"
                    )
                    return
                self.jwt_web = new_jwt
                self.csrf_token = new_csrf
                self.cs.cookies.set(
                    "JWT_WEB", self.jwt_web, domain=f".{self.domain}", path="/"
                )
                if self._tokenstore_path:
                    try:
                        self.dump(self._tokenstore_path)
                        _LOGGER.debug(
                            f"Seamlessly auto-saved refreshed API tokens proactively to {self._tokenstore_path}"
                        )
                    except Exception as dump_err:
                        _LOGGER.exception(
                            f"Proactive refresh auto-saving tokens failed gracefully natively: {dump_err}"
                        )
        except Exception as err:
            _LOGGER.debug(f"Refresh silently failed: {err}")

    def dumps(self) -> str:
        """Drop-in implementation for saving native payload cleanly."""
        data: dict[str, Any] = {
            "jwt_web": self.jwt_web,
            "csrf_token": self.csrf_token,
            "cookies": self.cs.cookies.get_dict(),
        }
        if self.profile_display_name:
            data["profile_display_name"] = self.profile_display_name
        if self.gc_api_user_agent:
            data["gc_api_user_agent"] = self.gc_api_user_agent
        return json.dumps(data)

    def dump(self, path: str) -> None:
        """Write tokens safely natively to disk format."""
        p = Path(path).expanduser()
        if p.is_dir() or not p.name.endswith(".json"):
            p = p / "garmin_tokens.json"

        # Ensure parent directories exist
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.dumps())

    def load(self, path: str) -> None:
        try:
            self._tokenstore_path = path
            p = Path(path).expanduser()
            if p.is_dir() or not p.name.endswith(".json"):
                p = p / "garmin_tokens.json"
            self.loads(p.read_text())
        except Exception as e:
            raise GarminConnectConnectionError(
                f"Token path not loading cleanly: {e}"
            ) from e

    def loads(self, tokenstore: str) -> None:
        try:
            data = json.loads(tokenstore)
            self.jwt_web = data.get("jwt_web")
            self.csrf_token = data.get("csrf_token")
            pd = data.get("profile_display_name")
            if isinstance(pd, str) and pd.strip():
                self.profile_display_name = pd.strip()
            else:
                self.profile_display_name = None
            ua = data.get("gc_api_user_agent")
            if isinstance(ua, str) and ua.strip():
                self.gc_api_user_agent = ua.strip()
            else:
                self.gc_api_user_agent = None
            raw_cookies = data.get("cookies", {})
            for k, v in raw_cookies.items():
                self.cs.cookies.set(k, v, domain=f".{self.domain}", path="/")

            if not self.is_authenticated:
                raise GarminConnectAuthenticationError("Missing tokens from dict load")
        except Exception as e:
            raise GarminConnectConnectionError(
                f"Token extraction loads() structurally failed: {e}"
            ) from e

    def connectapi(self, path: str, **kwargs: Any) -> Any:
        return self._run_request("GET", path, **kwargs).json()

    def request(self, method: str, _domain: str, path: str, **kwargs: Any) -> Any:
        # Legacy garth used this to distinguish API vs WEB
        kwargs.pop("api", None)
        return self._run_request(method, path, **kwargs)

    def post(self, _domain: str, path: str, **kwargs: Any) -> Any:
        api = kwargs.pop("api", False)
        resp = self._run_request("POST", path, **kwargs)
        if api:
            return resp.json() if hasattr(resp, "json") else None
        return resp

    def put(self, _domain: str, path: str, **kwargs: Any) -> Any:
        api = kwargs.pop("api", False)
        resp = self._run_request("PUT", path, **kwargs)
        if api:
            return resp.json() if hasattr(resp, "json") else None
        return resp

    def delete(self, _domain: str, path: str, **kwargs: Any) -> Any:
        api = kwargs.pop("api", False)
        resp = self._run_request("DELETE", path, **kwargs)
        if api:
            return resp.json() if hasattr(resp, "json") else None
        return resp

    def resume_login(self, client_state: Any, mfa_code: str) -> tuple[str | None, Any]:
        _ = client_state
        self._complete_mfa(mfa_code)
        return None, None

    def download(self, path: str, **kwargs: Any) -> bytes:
        if "headers" not in kwargs:
            kwargs["headers"] = {}
        # Ensure we politely accept any binary format Garmin transmits
        kwargs["headers"].update({"Accept": "*/*"})
        return self._run_request("GET", path, **kwargs).content

    def _run_request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not path.startswith("/gc-api"):
            path = f"/gc-api{path if path.startswith('/') else '/' + path}"

        stripped = path.removeprefix("/gc-api")
        if not stripped.startswith("/"):
            stripped = f"/{stripped}"
        primary_url = f"{self._connect}{path}"
        alternate_url = f"https://connectapi.{self.domain}{stripped}"

        if "timeout" not in kwargs:
            kwargs["timeout"] = 15

        custom_headers = kwargs.pop("headers", {})

        def _do(url: str) -> requests.Response:
            merged = self.get_api_headers()
            merged.update(custom_headers)
            return self.cs.request(method, url, headers=merged, **kwargs)

        resp = _do(primary_url)

        # Refresh once on 401 or 403 (expired JWT / edge quirks) then retry the call.
        if resp.status_code in (401, 403):
            self._refresh_session()
            resp = _do(primary_url)

        # VCR cassettes hit ``connectapi.<domain>/userprofile-service`` without the
        # ``/gc-api`` prefix on the host. Some sessions return 403 only on one host.
        if resp.status_code == 403:
            _LOGGER.debug(
                "HTTP 403 on %s; retrying %s",
                primary_url,
                alternate_url,
            )
            resp = _do(alternate_url)
            if resp.status_code in (401, 403):
                self._refresh_session()
                resp = _do(alternate_url)

        if resp.status_code == 204:

            class EmptyJSONResp:
                status_code = 204
                content = b""

                def json(self) -> Any:
                    return {}

                def __repr__(self) -> str:
                    return "{}"

                def __str__(self) -> str:
                    return "{}"

            return EmptyJSONResp()

        if resp.status_code >= 400:
            error_msg = f"API Error {resp.status_code}"
            try:
                error_data = resp.json()
                if isinstance(error_data, dict):
                    msg = (
                        error_data.get("message")
                        or error_data.get("content")
                        or error_data.get("detailedImportResult", {})
                        .get("failures", [{}])[0]
                        .get("messages", [""])[0]
                    )
                    if msg:
                        error_msg += f" - {msg}"
                    else:
                        error_msg += f" - {error_data}"
            except Exception:
                # If it's short, just attach the text
                if len(resp.text) < 500:
                    error_msg += f" - {resp.text}"
            raise GarminConnectConnectionError(error_msg)

        return resp
