"""Round-trip tests for react client garmin_tokens.json persistence."""

import json
from pathlib import Path

from garminconnect.client import Client


def test_client_dump_load_roundtrip(tmp_path: Path) -> None:
    """Token file shape matches Client.dump / load (used by collector + Playwright seed)."""
    jwt = "fake-jwt"
    csrf = "fake-csrf"
    cookies = {"SESSION": "abc", "OTHER": "1"}

    c1 = Client()
    c1.jwt_web = jwt
    c1.csrf_token = csrf
    for k, v in cookies.items():
        c1.cs.cookies.set(k, v, domain=".garmin.com", path="/")
    c1.cs.cookies.set("JWT_WEB", jwt, domain=".garmin.com", path="/")

    token_dir = tmp_path / "store"
    token_dir.mkdir()
    c1.dump(str(token_dir))

    out = token_dir / "garmin_tokens.json"
    assert out.is_file()
    payload = json.loads(out.read_text())
    assert payload["jwt_web"] == jwt
    assert payload["csrf_token"] == csrf
    assert "cookies" in payload and payload["cookies"]["SESSION"] == "abc"

    c2 = Client()
    c2.load(str(token_dir))
    assert c2.jwt_web == jwt
    assert c2.csrf_token == csrf
    assert c2.is_authenticated
