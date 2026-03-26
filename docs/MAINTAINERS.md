# Maintainer guide (Garmin collector + auth)

**Audience:** Engineers and coding agents changing `collector.py`, the vendored `garminconnect/` package, or `scripts/garmin_playwright_login.py`.

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

**What:** `scripts/garmin_playwright_login.py` signs in with a **real browser**, captures **`JWT_WEB`** (or `localStorage` token) and **`connect-csrf-token`** from SPA traffic, and writes **`garmin_tokens.json`** in the shape **`garminconnect.client.Client.load`** expects.

**Why:** For several accounts (including this project’s operator), **password login from Python** returns **HTTP 429** even at low frequency and after long waits, while **browser** login still works. Tokens obtained in the browser are then reused for **API** calls via `requests` with the same headers/cookies model as the web app.

### 2.4 Optional auto-run from the collector

**What:** On **`GarminConnectTooManyRequestsError`** from programmatic login, `collector.py` may **`subprocess`** the Playwright script once and retry login. Gated by **`GARMIN_BROWSER_LOGIN`** and TTY (see `env.example`).

**Why:** Lets a single job recover without manually SSHing to run the script first; **non-interactive** hosts (Windows Task Scheduler, systemd) need **`GARMIN_BROWSER_LOGIN=1`**.

### 2.5 Exception propagation for 429

**What:** `Garmin.login` in `garminconnect/__init__.py` re-raises **`GarminConnectTooManyRequestsError`** instead of wrapping it in **`GarminConnectConnectionError`**, so the collector can classify rate limits and trigger browser fallback.

**Why:** A prior bug hid 429 behind a generic “Login failed” type and broke targeted handling.

### 2.6 Expired session (401 on wellness / `gc-api`)

**What:** Tokens can expire after some hours. `Client._run_request` raises **`GarminConnectConnectionError("API Error 401 …")`** without attaching **`response`**, so status code was missing. **`Garmin.connectapi`** now treats that message as **401** and raises **`GarminConnectAuthenticationError`**. The collector deletes **`garmin_tokens.json`**, clears the cached client, and retries once (fresh password login; if that returns **429**, the usual Playwright path applies when enabled).

**Why:** Otherwise the first **`get_heart_rates`** could fail with 401, get swallowed inside **`_collect_garmin_data_body`** as a generic error, and the job could be marked **completed** with “no data” while the on-disk JWT was still stale on the next run.

---

## 3. Important files

| Path | Role |
|------|------|
| `collector.py` | Polling, job lifecycle, `get_garmin_api`, Playwright fallback |
| `garminconnect/client.py` | Mobile login, `load`/`dump`, `connectapi` headers (`Referer` → `/app/home`) |
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
| `GARMIN_BROWSER_LOGIN` | `1` = always allow collector-triggered browser seed; `0` = never; unset = only if stdin is a TTY |
| `GARMIN_PLAYWRIGHT_CHROME` | When set truthy, collector passes `--chrome` to the helper (system Google Chrome) |

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

## 6. Validation after auth or library changes

1. `.env` has `GARMINTOKENS` and matching rehab-platform `SHARED_SECRET` / URL.
2. Delete or keep `garmin_tokens.json` depending on what you are testing (fresh login vs reuse).
3. `python collector.py --poll` with a pending job; confirm logs show token reuse on later jobs, and platform job `completed` or a clear `failed` + `error_message`.
4. Optional: `python scripts/garmin_playwright_login.py --verify` after dependency changes.

---

## 7. Investigation history (short)

March 2026: **429** on Garth/SSO and on **`react`** `mobile/api/login` for this account; waiting and network changes did not restore programmatic login. **Browser** login remained viable. Response: ship Playwright seeding, JWT persistence, collector auto-fallback, faster post-login polling in the script (no fixed 45s wait on empty `di-oauth` JSON). Older dated logs and SHAs were in a handoff doc; **this file** is the living substitute.

---

## 8. Disclaimer

Garmin terms may restrict unofficial API access. This project is for **personal** engineering; not legal advice.
