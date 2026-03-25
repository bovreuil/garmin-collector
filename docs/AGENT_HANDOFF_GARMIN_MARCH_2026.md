# Agent handoff: Garmin Connect auth (March 2026)

**Purpose:** Single place for the **next session** to pick up: what we changed, what we tested (with timestamps), **exact repo/upstream versions**, and **recommended next steps**.  
**Related docs:** [GARMIN_AUTH_LANDSCAPE.md](GARMIN_AUTH_LANDSCAPE.md) (Garth, issues, `react` branch theory), [INTEGRATION.md](INTEGRATION.md) (rehab-platform contract), [plans/garmin-429-recovery.md](plans/garmin-429-recovery.md) (original token-reuse plan).

---

## 1. Problem summary

- **Symptom:** Data collection fails at **Garmin login** with **HTTP 429** / **“429 Rate Limit”**, not because of high-frequency wellness API calls.
- **Earlier path (pre-`react`):** Failures on **Garmin SSO** embed sign-in, e.g. `https://sso.garmin.com/sso/signin?...gauth-widget...` — full credential flow on **every job** if tokens were not persisted (`GARMINTOKENS` unset). Community: [python-garminconnect#337](https://github.com/cyberjunky/python-garminconnect/issues/337), [#332](https://github.com/cyberjunky/python-garminconnect/issues/332), [matin/garth#217](https://github.com/matin/garth/issues/217).
- **Current path (`react` branch vendored here):** Login uses **`POST https://sso.garmin.com/mobile/api/login`** with a **Dalvik / GarminConnect** `User-Agent`. A **429** is raised when the **JSON body** contains `error["status-code"] == "429"` (see `garminconnect/client.py` login handler).
- **User constraint:** Acceptable load is **~5–10 collections/day** (even **1/day**). Waiting **24+ hours** between attempts **did not** clear the block for programmatic login.
- **Sanity check:** **Normal browser** login to **Garmin Connect** works; data and widgets are visible. So the limitation is **automation-facing endpoints**, not a dead account.

---

## 2. Repository state (as of documentation commit)

| Item | Value |
|------|--------|
| **Active integration branch** | **`experiment/react-garmin`** |
| **Latest commit on that branch (handoff baseline)** | `f7c4e8e` — *docs: upstream/sync commands; browser vs mobile login 429 note* |
| **Vendored package** | `garminconnect/` from **cyberjunky/python-garminconnect `react`** |
| **`pyproject.toml` package version** | **0.2.41** |
| **Dependencies (library)** | **`requests` only** (no **Garth** on this branch) |
| **Upstream `react` tip verified (empty diff)** | **`d3428adf8ef48b1e18a62ceca35c190114741f4a`** — *feat: use Dalvik Android User-Agent to evade login rate limit* |
| **`master` branch** | May still reflect **older Garth-based** vendor until **`experiment/react-garmin`** is merged intentionally |

**Collector behaviour (`collector.py` on `experiment/react-garmin`):**

- Resolves token directory: **`GARMINTOKENS`** or default **`<repo>/.garmin-tokens`** (see `resolve_tokenstore_path()`).
- Login: **`Garmin(email, password).login(path_str)`** (react `login` loads tokens when valid, else uses credentials).
- Persists session: **`api.client.dump(path_str)`** after successful login (JWT / `garmin_tokens.json` layout — not Garth oauth files).
- Reuses in-process **`Garmin`** client across jobs; **one retry** on auth errors during fetch; maps some failures to **`failed`** job status.

**Re-verify sync with upstream:**

```bash
git fetch upstream
git diff upstream/react -- garminconnect/ pyproject.toml   # expect empty when fully aligned
```

---

## 3. Test runs and logs (chronological, user-reported)

Timestamps are **local to the user** (UK-style logs implied). Use as evidence only — your environment may differ.

| When (approx) | Environment | What ran | Result |
|---------------|-------------|----------|--------|
| **2026-03-23** ~19:03 / **22:28** | Dev / Docker-style path in traces | **`collector`** (older stack: local `garminconnect` + site-packages **Garth**) | **429** on **`sso.garmin.com/.../signin` (embed)**; job completed/failed per earlier logic |
| **2026-03-24** ~15:01 | MacBook, **home broadband** | **`collector`** after token-first changes | No `oauth1_token.json` → password login → **429** on **SSO signin** |
| **2026-03-24** ~15:11 | MacBook, **phone tether** | Same | **Same 429** on SSO signin → suggests **not only** home IP |
| **2026-03-24** ~15:39 | **`example.py`** | Default token dir **`~/.garminconnect`** (no `.env` loaded by script) | **429 Rate Limit** after credential login |
| **2026-03-24** ~15:39 | **`collector.py --poll`**, `POLL_INTERVAL=5s`, **`GARMINTOKENS` → repo `.garmin-tokens`** | Rehab-platform **localhost:5001**, one job | **429 Rate Limit** from **`client.login`** → wrapped **`GarminConnectConnectionError`**; job **`failed`** |
| **2026-03-25** ~18:54 | After **>24h** since last attempt | **`collector.py`**, single job | **Still 429** — JSON **`error.status-code` 429** from **mobile/api/login** path (`react` client) |
| **Ongoing** | **Browser** | Manual **connect.garmin.com** | **Works** — account OK |

**Conclusion from tests:** Programmatic login remains **blocked (429)** for this user despite **wait**, **network change (tether)**, and **`react`** + **latest upstream/react (Dalvik UA)**. **Browser** login succeeds.

---

## 4. What was implemented (high level)

1. **`master` (earlier commits):** Token-first login, **`api.garth.dump`** (Garth era), env **`GARMINTOKENS`**, `.gitignore` **`.garmin-tokens/`**, docs, `requirements.txt` **`-e .`**.
2. **`experiment/react-garmin`:** Replaced vendored **`garminconnect/`** + **`pyproject.toml`** with **`upstream/react`**; **`collector`** switched to **`api.client.dump`**, unified **`Garmin(email,pw).login(path)`**.
3. **Docs:** [GARMIN_AUTH_LANDSCAPE.md](GARMIN_AUTH_LANDSCAPE.md), README links, INTEGRATION notes, plan file updates, **sync instructions** + browser-vs-programmatic 429 note (`f7c4e8e`).

**Known gaps / tech debt:**

- **`example.py`** in this repo may still reference **Garth** (`garth.exc`, `garmin.garth.dump`) — **not** updated for **`react`**. Prefer **`collector.py`** or sync **`example.py`** from upstream **`react`**.
- **429** is sometimes wrapped as **`GarminConnectConnectionError`**, so **`failure_kind`** may be **`garmin_login`** instead of **`garmin_rate_limit`** — cosmetic for rehab-platform.

---

## 5. Instructions for the **next agent**

### 5.1 Read first

1. This file (**AGENT_HANDOFF_GARMIN_MARCH_2026.md**).
2. [GARMIN_AUTH_LANDSCAPE.md](GARMIN_AUTH_LANDSCAPE.md).
3. `collector.py` (`_perform_garmin_login`, `collect_garmin_data`, `run_job`).
4. `garminconnect/client.py` **`login()`** and **`load` / `dump`** (token file layout).

### 5.2 Likely next implementation: browser-based token seeding

**Goal:** Obtain a **valid session** (JWT + CSRF + cookies) **without** calling **`/mobile/api/login`** from Python, then write the same files **`client.load()`** expects under **`GARMINTOKENS`** (e.g. **`.garmin-tokens/`**).

**Pointers:**

- [matin/garth#217](https://github.com/matin/garth/issues/217) — Playwright / “real browser” discussion, [gist](https://gist.github.com/coleman8er/5c8e192d2aa3c8a3a6220c5702e8a5e6) referenced there.
- User is **comfortable** with **Playwright**-style flows (mini-ITX already uses browser auth for **activities.decathlon.net**).

**Deliverable ideas:**

- **`scripts/garmin_playwright_login.py`** (or similar): headed/headless browser login to **`connect.garmin.com`**, extract ticket/tokens, call **`garminconnect.client.Client`Dump** or copy equivalent **`garmin_tokens.json`** into **`.garmin-tokens/`**.
- Document: run **interactively** when tokens expire; keep **`collector`** headless.

**Constraints:**

- Respect **`.gitignore`** on **`.garmin-tokens/`** — never commit secrets.
- Align with **rehab-platform** contract unchanged ([INTEGRATION.md](INTEGRATION.md)).

### 5.3 If browser path is deferred

- Periodically **`git fetch upstream`** and **`git diff upstream/react -- garminconnect/`** for new login mitigations.
- Optionally **merge `experiment/react-garmin` → `master`** only after **proven** collection on dev + mini-ITX.

### 5.4 Quick validation after any auth change

1. **`GARMINTOKENS`** points at **`<repo>/.garmin-tokens`** in **`.env`**.
2. One **pending** `collect_data` job; **`python collector.py --poll`** (or single-job test harness).
3. Confirm **files appear** under **`.garmin-tokens/`** and **`INFO`** logs show session reuse **without** hitting mobile login every time.

---

## 6. Disclaimer

Garmin **terms of use** may restrict unofficial access. This project is for **personal** use; document **engineering context** only, not legal advice.

---

*Document generated to capture the March 2026 investigation and handoff. Update this file when the auth story changes materially.*
