#!/usr/bin/env python3
"""
Compare what the vendored Client sends for socialProfile vs what the browser sends.

Usage (from repo root, with garmin_tokens.json in GARMINTOKENS or --token-dir):

  python scripts/debug_garmin_social_profile.py
  python scripts/debug_garmin_social_profile.py --probe

Without --probe: print URLs, redacted headers, and cookie names only (safe to paste).
With --probe: run GET requests and print status + short body (may contain account hints).

For browser comparison:
  1. Chrome → Connect (logged in) → DevTools → Network → filter ``socialProfile``.
  2. Click the request → Headers: note URL, method, Request headers.
  3. Right-click request → Copy → Copy as cURL.
  4. Diff cURL Host, path, Referer, Cookie (names), connect-csrf-token, DI-Backend, User-Agent.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# repoRoot/scripts → repo root
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from garminconnect.client import Client  # noqa: E402


def _redact(s: str | None, keep: int = 10) -> str:
    if not s:
        return "(empty)"
    if len(s) <= keep + 6:
        return f"{s[:4]}…({len(s)} chars)"
    return f"{s[:keep]}…{s[-4:]} ({len(s)} chars)"


def _urls_for_path(client: Client, api_path: str) -> tuple[str, str]:
    path = api_path if api_path.startswith("/") else f"/{api_path}"
    if not path.startswith("/gc-api"):
        path = f"/gc-api{path}"
    stripped = path.removeprefix("/gc-api")
    if not stripped.startswith("/"):
        stripped = f"/{stripped}"
    primary = f"{client._connect}{path}"
    alternate = f"https://connectapi.{client.domain}{stripped}"
    return primary, alternate


def main() -> None:
    load_dotenv(_ROOT / ".env")
    parser = argparse.ArgumentParser(
        description="Dump socialProfile request shape from saved tokens."
    )
    parser.add_argument(
        "--token-dir",
        type=Path,
        default=None,
        help="Directory with garmin_tokens.json (default: GARMINTOKENS or .garmin-tokens)",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Perform GETs to primary then alternate URL; print status and body snippet",
    )
    parser.add_argument(
        "--path",
        default="/userprofile-service/socialProfile",
        help="API path as passed to connectapi()",
    )
    args = parser.parse_args()

    raw = args.token_dir or os.getenv("GARMINTOKENS")
    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (_ROOT / p).resolve()
    else:
        p = _ROOT / ".garmin-tokens"

    if not p.exists():
        print(f"Token path does not exist: {p}", file=sys.stderr)
        sys.exit(1)

    client = Client()
    client.load(str(p))

    api_path = args.path
    if client.profile_display_name and api_path.rstrip("/").endswith(
        "/userprofile-service/socialProfile"
    ):
        api_path = f"/userprofile-service/socialProfile/{client.profile_display_name}"

    primary, alternate = _urls_for_path(client, api_path)
    h = client.get_api_headers()

    print("=== URLs (same logic as Client._run_request) ===")
    if client.profile_display_name:
        print(f"(using profile_display_name from token file: {client.profile_display_name})")
    print("primary:  ", primary)
    print("alternate:", alternate)
    print()

    print("=== Request headers (values redacted) ===")
    for k in sorted(h.keys()):
        print(f"  {k}: {_redact(h[k])}")
    print()

    jar = client.cs.cookies.get_dict()
    print(f"=== Session cookie names ({len(jar)} keys; values redacted) ===")
    for name in sorted(jar.keys()):
        print(f"  {name}: {_redact(jar[name])}")

    if not args.probe:
        print()
        print("Run with --probe to GET these URLs from Python and see status/body.")
        print("In the browser, compare with DevTools → Network → socialProfile → Headers.")
        return

    print("\n=== Probe: GET primary then alternate ===\n")

    def one(url: str) -> None:
        r = client.cs.request("GET", url, headers=client.get_api_headers(), timeout=15)
        print(f"URL: {url}")
        print(f"Status: {r.status_code}")
        print(f"Content-Type: {r.headers.get('Content-Type', '?')}")
        snippet = (r.text or "")[:600].replace("\n", " ")
        print(f"Body[:600]: {snippet!r}")
        print()

    one(primary)
    one(alternate)


if __name__ == "__main__":
    main()
