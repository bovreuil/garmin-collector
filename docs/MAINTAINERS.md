# Maintainer guide (Garmin collector + auth)

**Audience:** Engineers and coding agents changing `collector.py`, the vendored `garminconnect/` package, or `scripts/garmin_playwright_login.py`.

**Agent workflow (commit, review, testing):** [`.cursor/WORKING-AGREEMENT.md`](../.cursor/WORKING-AGREEMENT.md). **Entry index:** [AGENTS.md](../AGENTS.md).

**Operational setup:** [README.md](../README.md). **HTTP contract with rehab-platform:** [INTEGRATION.md](INTEGRATION.md). **Garth / community / upstream context:** [GARMIN_AUTH_LANDSCAPE.md](GARMIN_AUTH_LANDSCAPE.md).

---

## 1. What this service does

The collector runs on a **trusted machine** (dev laptop or home mini-ITX), polls rehab-platform for `collect_data` jobs, pulls wellness and activity data from **Garmin Connect**, and **uploads** JSON back over HTTPS. The platform intentionally does not talk to Garmin from cloud hosts (e.g. Render); see INTEGRATION §1.

---

## 2. Why the auth stack looks like this

### 2.1 Vendored `react`-style `garminconnect` (JWT, no Garth)

**What:** This repo vendors **[python-garminconnect](https://github.com/cyberjunky/python-garminconnect) `react`** (or a line equivalent to it): session material is **JWT + CSRF** for Connect’s **`gc-api`**, persisted as **`garmin_tokens.json`** under `GARMINTOKENS`, not classic Garth oauth files.

**Why:** Garmin tightened **programmatic** login (SSO embed and mobile-like login). The upstream maintainer is moving toward the **same surfaces the Connect web app** uses. Aligning with that stack is more sustainable than staying on Garth-only login for this integration.

### 2.2 Token-first login in `collector.py`

**What:** `Garmin(email, password).login(tokenstore_path)` loads tokens when valid; otherwise it uses **`/mobile/api/login`** (Dalvik-style client) and persists with **`api.client.dump(tokenstore_path)`** after success. The in-process `Garmin` instance is reused across jobs; one re-login path on auth errors during fetch.

**Why:** Calling full login on **every** job increases **429** risk and burns user trust with Garmin. A project-local directory (default `.garmin-tokens/`) keeps dev and prod sessions separate.

### 2.3 Playwright browser seeding

**What:** `scripts/garmin_playwright_login.py` signs in with a **real browser**, captures **`JWT_WEB`** (or `localStorage` token) and **`connect-csrf-token`** from SPA traffic, and writes **`garmin_tokens.json`** in the shape **`garminconnect.client.Client.load`** expects. By default it uses a **persistent** Chromium user-data directory (``.garmin-browser-profile/``, override **`GARMIN_PLAYWRIGHT_PROFILE`**) via `launch_persistent_context`, not a throwaway context each run.

**Why:** For several accounts (including this project’s operator), **password login from Python** returns **HTTP 429** even at low frequency and after long waits, while **browser** login still works. Tokens obtained in the browser are then reused for **API** calls via `requests` with the same headers/cookies model as the web app. A persistent profile keeps Garmin/Cloudflare cookies and “remember this device” state on the collector host, which often reduces captchas and SSO friction on later reseeds. Set **`GARMIN_PLAYWRIGHT_EPHEMERAL=1`** or **`--ephemeral`** to restore the old one-shot context behaviour.

### 2.4 Optional auto-run from the collector

**What:** On **`GarminConnectTooManyRequestsError`** from programmatic login, `collector.py` may **`subprocess`** the Playwright script once and retry login. Gated by **`GARMIN_BROWSER_LOGIN`** and TTY (see `env.example`).

**Why:** Lets a single job recover without manually running the script first. **`GARMIN_BROWSER_LOGIN=1`** forces browser allow when stdin is not a TTY; **unset** relies on a TTY heuristic (often enough under Task Scheduler).

### 2.5 Exception propagation for 429

**What:** `Garmin.login` in `garminconnect/__init__.py` re-raises **`GarminConnectTooManyRequestsError`** instead of wrapping it in **`GarminConnectConnectionError`**, so the collector can classify rate limits and trigger browser fallback.

**Why:** A prior bug hid 429 behind a generic “Login failed” type and broke targeted handling.

### 2.6 Expired session (401 on wellness / `gc-api`)

**What:** Tokens can expire after some hours. `Client._run_request` raises **`GarminConnectConnectionError("API Error 401 …")`** without attaching **`response`**, so status code was missing. **`Garmin.connectapi`** now treats that message as **401** and raises **`GarminConnectAuthenticationError`**. The collector deletes **`garmin_tokens.json`**, clears the cached client, and retries once. When **`GARMIN_BROWSER_LOGIN`** is enabled or stdin is a TTY, it runs **Playwright immediately** after clearing stale tokens (skipping a password attempt that often returns **429** for some accounts); otherwise it tries programmatic login first.

**Why:** Otherwise the first **`get_heart_rates`** could fail with 401, get swallowed inside **`_collect_garmin_data_body`** as a generic error, and the job could be marked **completed** with “no data” while the on-disk JWT was still stale on the next run.

**Related:** If **`Client.get_api_headers()`** raises **`GarminConnectAuthenticationError("Not authenticated")`** (JWT/CSRF missing before any HTTP call), **`Garmin.connectapi`** must **re-raise** that type instead of wrapping it as **`Connection error: …`**, or the same wrong “completed / no data” path runs on Windows.

**Transient TCP/TLS drops** (e.g. Windows **10054** “forcibly closed by remote host”, **`Connection aborted`**) are classified in **`collector.py`** as **`TransientGarminNetworkError`**: **one** retry after **`invalidate_garmin_client()`** and a short sleep; then **`failure_kind: garmin_network`** and job **`failed`** if it persists — not “completed with no data”.

---

## 3. Important files

| Path | Role |
|------|------|
| `collector.py` | Polling, job lifecycle, `get_garmin_api`, Playwright fallback |
| `garminconnect/client.py` | Mobile login, `load`/`dump`, gc-api: cookie + CSRF + browser `User-Agent`, `Sec-CH-UA`/`Sec-Fetch-*`, `Referer` → `/app/home`; `connectapi.<domain>` fallback if `connect` host returns 403 |
| `garminconnect/__init__.py` | `Garmin.connectapi` (401 detection), `Garmin.login`, 429 re-raise |
| `scripts/garmin_playwright_login.py` | Headed login, token capture, `--verify` |
| `requirements.txt` | App + editable vendored package |
| `requirements-browser.txt` | Playwright |

---

## 4. Environment variables (auth-related)

| Variable | Purpose |
|----------|---------|
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | Required for programmatic path and for Playwright auto-fill |
| `GARMINTOKENS` | Directory for `garmin_tokens.json` (default `.garmin-tokens` under repo root) |
| `GARMIN_KEEPALIVE_INTERVAL` | Seconds between in-process keepalive runs (`0` disables). Keepalive uses a lightweight Garmin API call to keep sessions warm and reduce browser reseed during manual collections. |
| `GARMIN_BROWSER_LOGIN` | `1` = always allow collector-triggered Playwright; `0` = never; **unset** = allow when stdin is a **TTY** (heuristic; set `1` only if recovery never opens a browser on your host) |
| `GARMIN_PLAYWRIGHT_CHROME` | When set truthy, collector passes `--chrome` to the helper (system Google Chrome) |
| `GARMIN_PLAYWRIGHT_PROFILE` | Persistent browser user-data dir (default `.garmin-browser-profile/`); `0`/`ephemeral` disables |
| `GARMIN_PLAYWRIGHT_EPHEMERAL` | `1` = fresh context each run (no persistent profile) |
| `COLLECTOR_HEALTH_INTERVAL` | Seconds between collector health heartbeats to rehab-platform (`0` disables). |
| `COLLECTOR_HEALTH_ENDPOINT` | Relative path for heartbeat POST JSON payloads (default `/api/collector/health`; `404` is tolerated). |
| `COLLECTOR_ID` | Optional stable identifier included in heartbeat payloads (defaults to hostname). |

`GARMINTOKENS` is temporarily **removed** from the environment around `api.login` inside the collector so the library resolves the path argument explicitly (avoids double-application of env defaults).

---

## 5. Staying aligned with upstream `react`

```bash
git remote add upstream https://github.com/cyberjunky/python-garminconnect.git   # once
git fetch upstream
git diff upstream/react -- garminconnect/ pyproject.toml
```

Empty diff ⇒ vendored tree matches upstream `react` for those paths. Borrow updates deliberately: re-run integration tests and a live collection when bumping.

---

## 6. Failure modes, log signatures, and job status

This section lists **real failures** seen in production/dev (March 2026) and how **`collector.py`** / **`run_job`** should treat them. Rehab-platform jobs should end **`failed`** with `error_message` when Garmin or transport failed—not **`completed`** with an empty result unless there truly was no HR data.

### 6.1 `failure_kind` → platform job

| `failure_kind` (in `collect_garmin_data` result) | `run_job` sets platform status | Typical cause |
|--------------------------------------------------|-------------------------------|---------------|
| `garmin_login` | **`failed`** | Cannot establish session (credentials, MFA, repeated auth failure after clearing tokens). |
| `garmin_rate_limit` | **`failed`** | **429** from SSO/mobile login or from **`gc-api`** during fetch. |
| `garmin_auth_during_fetch` | **`failed`** | Auth still bad after **one** retry (clear tokens + browser / re-login). |
| `garmin_network` | **`failed`** | Transient **TLS/socket** drop (**two** attempts); still broken (e.g. WinError **10054**, **Connection aborted**). |
| *(absent)* + `success: False` | **`completed`** * | **Only** for “soft” outcomes (e.g. **no heart rate** for that date without a transport/auth exception). Do not use this path for wrapped connection/auth errors. |

\* **Historical footgun:** Before hardened handling, **`API Error 401`**, **`Not authenticated`**, and **connection resets** were sometimes returned as generic `success: False` **without** `failure_kind`, so **`run_job`** misclassified as **“completed with no data”**. Current code maps these to auth retry, **`garmin_network`**, or re-raised types as appropriate.

### 6.2 Symptom → meaning → collector behaviour

| Log / exception pattern | Meaning | Expected recovery |
|-------------------------|---------|-------------------|
| **`429`** / **`GarminConnectTooManyRequestsError`** on **`mobile/api/login`** (or wrapped login message) | Programmatic login throttled. | Collector may run **Playwright** (if `GARMIN_BROWSER_LOGIN` or TTY); writes **`garmin_tokens.json`**; retries login. |
| **`API Error 401`** on **`dailyHeartRate`** (etc.) | Expired or rejected **JWT**/session for **`gc-api`**. | **`Garmin.connectapi`** → **`GarminConnectAuthenticationError`**. Collector **deletes** token file, invalidates client, may open browser **without** a doomed password hop, retries **once**. |
| **`API Error 403`** on **`dailyHeartRate`** (etc.) | Often the same session class as **401** (edge/WAF or Garmin rejecting the JWT for that route); **not** a reliable “permanent privacy” signal for your own wellness data. | Same as **401**: treated as **`GarminConnectAuthenticationError`** so tokens are cleared and the **one** fetch retry runs (then **`garmin_auth_during_fetch`** if still broken). Previously this fell through to **“completed with no data”** because there was no **`failure_kind`**. |
| **`Not authenticated`** from **`get_api_headers()`** (before HTTP) | **`jwt_web`** / **`csrf_token`** missing in memory (e.g. bad refresh, partial load). | Must **not** be wrapped as generic **`Connection error`** in **`Garmin.connectapi`**; must propagate **`GarminConnectAuthenticationError`** so the same **clear + reseed** path runs (March 2026 Windows). |
| **`Connection error:`** / **`Connection aborted`** / **`ConnectionResetError`** / **`10054`** / **`ProtocolError`** | Remote or edge **closed the socket**; often transient. | **`TransientGarminNetworkError`**: **invalidate** client, **sleep ~2s**, retry **once**; if still failing → **`garmin_network`**, job **`failed`**. |
| Playwright **`OK: wrote … garmin_tokens.json`** | Browser session captured. | Next **`login(path)`** loads file; collection continues. |

### 6.3 Operator checklist when jobs look wrong

1. Confirm rehab-platform job is **`failed`** with a useful **`error_message`** when Garmin said no—not **`completed`** with empty payload for a transport/auth error.
2. If **browser never opens** on recovery, see **`GARMIN_BROWSER_LOGIN`** / TTY notes in **§4** and `env.example`.
3. If failures persist after token reseed, check **home IP / VPN**, Garmin account status, and `garmin-login-debug.png` from Playwright.

### 6.4 Debugging persistent **`socialProfile` / gc-api 403** (browser vs Python)

Avoid changing headers or hosts ad hoc until you have **one** failing request compared in both stacks.

1. **Python side:** from the repo root, with `garmin_tokens.json` in place:
   - `python scripts/debug_garmin_social_profile.py` — prints **URLs**, **header names** (values redacted), and **cookie names** our `Client` would use.
   - `python scripts/debug_garmin_social_profile.py --probe` — performs the GETs and prints **status**, **Content-Type**, and a **short body** (confirms Cloudflare HTML vs JSON API error).
2. **Browser side:** normal Chrome (same account, already on Connect if possible), F12 → **Network**, filter **`socialProfile`** or **`userprofile-service`**, trigger a navigation that refetches profile if needed. Open the request → **Headers**:
   - Note full **Request URL** (host + path).
   - Compare **Request headers** to the script output: **Referer**, **Origin**, **connect-csrf-token**, **DI-Backend**, **User-Agent**, **Cookie** (names; do not paste secrets).
   - Right-click the request → **Copy → Copy as cURL** for a precise diff against what Python sends.
3. **Interpretation:** mismatched **host/path** (e.g. `connect…/gc-api/…` vs `connectapi…/…`), missing **header**, or a **403 HTML** body from Cloudflare points to edge/session rules, not “random library bugs.” If the **browser** call is **403** too, the problem is Garmin/account/session, not the collector.
4. **`gc-api` social profile URL:** the Connect SPA calls **`/gc-api/userprofile-service/socialProfile/{displayName}`** (e.g. `…/bovreuil`). The bare **`…/socialProfile`** path can return **403**. The collector persists **`profile_display_name`** in **`garmin_tokens.json`**; Playwright seeds it from the same XHR when possible.
5. **`gc-api` User-Agent / Accept:** sessions are often bound to the **browser User-Agent** that obtained **JWT_WEB**. **`garmin_tokens.json`** may include **`gc_api_user_agent`** (set by Playwright). **`Accept: */*`** matches the SPA’s socialProfile request better than **`Accept: application/json` alone** for some accounts.

---

## 7. Validation after auth or library changes

1. `.env` has `GARMINTOKENS` and matching rehab-platform `SHARED_SECRET` / URL.
2. Delete or keep `garmin_tokens.json` depending on what you are testing (fresh login vs reuse).
3. `python collector.py --poll` with a pending job; confirm logs show token reuse on later jobs, and platform job `completed` or a clear `failed` + `error_message`.
4. Optional: `python scripts/garmin_playwright_login.py --verify` after dependency changes.
5. If heartbeat is enabled, confirm rehab-platform receives collector health payloads (session state, keepalive counters, fast/slow collection stats) at `COLLECTOR_HEALTH_ENDPOINT`.

---

## 8. Investigation history (short)

March 2026: **429** on Garth/SSO and on **`react`** `mobile/api/login` for this account; waiting and network changes did not restore programmatic login. **Browser** login remained viable. Response: ship Playwright seeding, JWT persistence, collector auto-fallback, faster post-login polling in the script (no fixed 45s wait on empty `di-oauth` JSON). Older dated logs and SHAs were in a handoff doc; **this file** is the living substitute.

**If this stack breaks again:** see [GARMIN_AUTH_LANDSCAPE.md — When the current stack stops working](GARMIN_AUTH_LANDSCAPE.md#when-the-current-stack-stops-working-contingency-checklist) (upstream sync order, issue links, and **browser-only / webhook** alternatives such as [garmin-data-bridge](https://github.com/Flo976/garmin-data-bridge)).

---

## 9. Disclaimer

Garmin terms may restrict unofficial API access. This project is for **personal** engineering; not legal advice.
