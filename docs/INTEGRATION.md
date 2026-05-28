# Rehab-platform integration

This document describes how **garmin-collector** talks to **rehab-platform**: architecture, authentication, job flow, and the JSON upload contract.

**Source of truth for the HTTP contract** is rehab-platform’s `app.py` (search for `GARMIN COLLECTOR API ENDPOINTS` and `process_garmin_data_from_collector`). If anything here disagrees with that code, treat the Python implementation as authoritative.

For running the collector locally or on a mini-ITX, see the project [README](../README.md).

---

## 1. Why this service exists (split from rehab-platform)

- **rehab-platform** is the main Flask app: auth, UI, PostgreSQL, O2Ring/Dropbox, TRIMP and chart logic. It deliberately has **no** Garmin Connect client dependencies.
- **garmin-collector** is a **separate repo**, historically forked from [python-garminconnect](https://github.com/cyberjunky/python-garminconnect), so upstream Garmin API work stays isolated from application code.
- **Production constraint:** When rehab-platform was deployed to **Render**, Garmin Connect **did not accept API traffic from that environment** (suspected cloud/hosting IP blocking or similar). Collection therefore runs on a **trusted egress path** (e.g. home Windows mini-ITX) and **pushes** results to rehab-platform over HTTPS.

**Typical production topology:**

```text
User (phone/laptop) → rehab-platform (e.g. Render) → PostgreSQL
                              ↑
Mini-ITX garmin-collector → Garmin Connect API
```

---

## 2. What rehab-platform expects from the collector

### 2.1 Responsibilities

- Poll rehab-platform for **pending** `collect_data` jobs.
- Authenticate every call with the **shared secret** (see below).
- For each job, use **`target_date`** (calendar day `YYYY-MM-DD`) when calling Garmin APIs.
- Upload a single JSON document to **`POST /api/jobs/<job_id>/data`**.
- Update job lifecycle via **`POST /api/jobs/<job_id>/status`** (`running` → `completed` or `failed`).

The **`target_date` for storage is always taken from the job row**, not from the JSON body. The upload handler loads `target_date` from `background_jobs` and passes it to ingestion.

### 2.2 Calendar day and timestamps

- A **day** in rehab-platform is a **UK calendar day** (BST or GMT depending on date); Garmin jobs are **one day per job**, aligned to that notion. See rehab-platform **ARCHITECTURE.md** → “Calendar day definition”.
- Time series in the database use **Unix timestamps in milliseconds (UTC)**. The collector should send data consistent with how Garmin APIs return times; rehab-platform merges and rebuilds day-level HR where needed (`build_daily_hr_timeseries` after activity ingest).

### 2.3 Data preservation (backfill / old dates)

If Garmin returns empty or partial data for a date that already has rows in `daily_data`, rehab-platform **merges** and **preserves** existing HR, stress, body battery, resting HR, HRV, respiration, and training readiness when new payloads omit them. This supports safe backfill when Garmin has dropped old series until the user “reloads” data in the Garmin Connect app.

### 2.4 Activities vs manual / FIT

On each successful ingest for a date, rehab-platform **deletes Garmin-sourced activities** for that date and re-inserts from the payload. It **keeps** activities with `activity_type` **`manual`** or **`fit`** (Wahoo). New Garmin activities are stored with normal Garmin metadata; **`exercise`** defaults to `true` on insert.

---

## 3. Authentication (all collector → platform calls)

Use header:

```http
Authorization: Bearer <SHARED_SECRET>
```

The same `SHARED_SECRET` must be configured on **both** services (rehab-platform `API_CONFIG['SHARED_SECRET']` / env; collector env e.g. `SHARED_SECRET`). Mismatch produces **401 Unauthorized**.

**Note:** These routes are **not** user session cookies; they are **machine-to-machine** only.

---

## 4. HTTP API contract

Base URL: whatever hosts rehab-platform (e.g. `REHAB_PLATFORM_URL` in production). Paths below are **relative**.

### 4.1 `GET /api/jobs/pending`

- **Auth:** Bearer shared secret (required).
- **Response:** JSON **array** of job objects (may be `[]`).

Each job object includes (from DB):

| Field         | Meaning |
|---------------|---------|
| `job_id`      | Opaque string; use in subsequent URLs. |
| `job_type`    | e.g. `collect_data` for Garmin collection. |
| `status`      | For listed rows, always `pending`. |
| `created_at`  | Creation time. |
| `target_date` | **`YYYY-MM-DD`** — primary date to collect. |
| `start_date` / `end_date` | May be present; collection for current integration is driven by **`target_date`**. |

**Ordering:** Oldest `created_at` first.

### 4.2 `POST /api/jobs/<job_id>/status`

- **Auth:** Bearer shared secret.
- **Body:** JSON object:

```json
{
  "status": "running | completed | failed",
  "result": null,
  "error_message": null
}
```

- **Valid `status` values:** `running`, `completed`, `failed` only.
- **`result`:** Optional; may be a JSON object or a JSON **string**. If `status` is `completed` and `result` parses to an object with `"success": false`, rehab-platform **forces** `status` to **`failed`** and uses `message` / `error` / `error_message` for the UI/logs (e.g. Garmin **429** handling).

**Recommended flow:**

1. `running` when work starts (optional but good for UX).
2. After successful `POST .../data`, `completed` with optional `result`.
3. On failure before or after upload, `failed` with `error_message` set.

### 4.3 `POST /api/jobs/<job_id>/data`

- **Auth:** Bearer shared secret.
- **Body:** JSON object — **ingestion contract** (section 5).
- **Success:** `200` with e.g. `{"message": "Data uploaded and processed successfully"}`.
- **Errors:** `401` bad secret; `404` unknown `job_id`; `400` empty body; `500` processing error (collector should mark job `failed`).

**Important:** Successful processing **does not** automatically set the job row to `completed`. The collector should still call **`/status`**.

---

## 5. Upload JSON payload (`POST /api/jobs/<job_id>/data`)

All top-level keys are **optional** unless you need that data; omitted or empty sections may cause existing DB values to be preserved (see preservation rules in §2.3).

### 5.1 `heart_rate_data` (daily HR)

```json
{
  "heart_rate_data": {
    "heartRateValues": [[1700000000000, 72], ...]
  }
}
```

- **`heartRateValues`:** Array of points. Each point is typically **`[timestamp_ms, heart_rate_bpm]`** (same shape Garmin uses; rehab-platform also supports dict-shaped elements with a `timestamp` key in some code paths).
- If **any** value in the series is **`null`**, rehab-platform **drops the entire daily HR series** for that upload (defensive).

### 5.2 `all_day_stress_data` (stress + body battery)

```json
{
  "all_day_stress_data": {
    "stressValuesArray": [[ts_ms, stressLevel], ...],
    "bodyBatteryValuesArray": [[ts_ms, status, level, version], ...]
  }
}
```

- **`stressValuesArray`:** `[timestamp_ms, stressLevel]` pairs. Stress level **-1** means no reading (downstream charts exclude these from bucket math).
- **`bodyBatteryValuesArray`:** Raw Garmin-like tuples; rehab-platform maps **`[0]=timestamp`, `[2]=level`, `[1]=status`** into stored **`[timestamp, level, status]`** triplets.

### 5.3 `resting_hr_data`

Flexible JSON; platform extracts a single integer **resting HR** per day. Common path:

- `allMetrics.metricsMap.WELLNESS_RESTING_HEART_RATE[0].value`

Fallbacks include top-level `value`, `restingHeartRate`, or `wellness.restingHeartRate`.

### 5.4 `hrv_data`

Arbitrary HRV JSON from Garmin. If a top-level **`hrvReadings`** array is present, rehab-platform **strips it** before storage (size reduction). Prefer sending summary fields the UI needs without huge arrays.

### 5.5 `respiration_data`

Arbitrary respiration JSON. If **`respirationValuesArray`** is present, it is **stripped** before storage; **`respirationAveragesValuesArray`** is kept.

### 5.6 `training_readiness_data`

Stored as JSON blob for metrics/charts.

### 5.7 `activities` (array)

Each activity object should include fields the platform reads (defaults shown):

| Field | Required | Notes |
|-------|----------|--------|
| `activity_id` | **Yes** | String; stable Garmin id; **UPSERT** key. |
| `activity_name` | No | String. |
| `activity_type` | No | String, or **`{"typeKey": "..."}`** dict. |
| `start_time_local` | No | ISO string (with `Z` allowed) or datetime; stored on `activity_data`. |
| `duration_seconds` | No | Integer; default `0`. |
| `distance_meters`, `elevation_gain`, `average_hr`, `max_hr` | No | Numeric; optional. |
| `heart_rate_series` | No | JSON array of **`[timestamp_ms, bpm]`** (serialized to DB). |
| `breathing_rate_series` | No | Same general time-series shape as HR. |

Per-activity TRIMP is computed inside rehab-platform from `heart_rate_series` when present. After all activities are stored, rehab-platform **rebuilds** the **day-level** HR series (`build_daily_hr_timeseries`) and **recomputes daily TRIMP** from that combined series.

---

## 6. Operational notes (from rehab-platform docs)

- **Polling interval:** Example production setup uses **~30 seconds** between polls (`POLL_INTERVAL`).
- **Platform URL:** Collector env often named like `REHAB_PLATFORM_URL` pointing at the public HTTPS origin (no trailing slash issues — use consistent URL building in collector code).
- **Jobs creation:** Admins trigger collection from rehab-platform **`/data`** (`POST /collect-data`), which inserts `background_jobs` with `job_type='collect_data'` and `status='pending'`.
- **Troubleshooting 401:** Align `SHARED_SECRET` and base URL; confirm `Authorization: Bearer ...` spelling.
- **Garmin session files:** The collector persists JWT session data on disk (see `GARMINTOKENS` in `env.example`) — e.g. **`garmin_tokens.json`** under **`.garmin-tokens/`** — inside each project clone (dev vs prod mini-ITX). That avoids repeating full login on every job. If **programmatic** password login returns **429** or sessions expire, use **`scripts/garmin_playwright_login.py`** or collector-triggered Playwright (**`GARMIN_BROWSER_LOGIN`** / TTY heuristic — see README). Treat the token directory like secrets; restrict permissions where the OS allows.
- **Job status on Garmin login / rate limits:** If the collector cannot authenticate or hits **429** on Garmin (SSO or Connect API), it should set the job to **`failed`** with `error_message` set (not `completed` with empty data). Missing heart rate data for a date without a transport error may still be reported as `completed` with `success: false` in `result` per existing behavior.

For full production checklist (Gunicorn workers, Dropbox, OAuth, etc.), see rehab-platform **OPERATIONS.md** — most of it applies only to the web app, not the collector.

---

## 7. Upstream library

Garmin API access in the collector aligns with **[python-garminconnect](https://github.com/cyberjunky/python-garminconnect)**. The vendored library tracks **`react`** (**JWT / `garmin_tokens.json`**, not classic Garth oauth files). Upstream changes to response shapes may require updates to the **payload mapping** in the collector so section 5 still holds.

**Maintainer-oriented auth notes (Playwright, 429, env, sync):** **[MAINTAINERS.md](MAINTAINERS.md)** — **§6** documents **failure modes**, log signatures, **`failure_kind`**, and **“completed with no data”** misclassification. **Garth**, issue threads (#332, #337), and **garth#217:** **[GARMIN_AUTH_LANDSCAPE.md](GARMIN_AUTH_LANDSCAPE.md)**.

---

## 8. Quick reference — endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/jobs/pending` | List jobs with `status = 'pending'`. |
| `POST` | `/api/jobs/<job_id>/status` | Set `running` / `completed` / `failed`. |
| `POST` | `/api/jobs/<job_id>/data` | Upload Garmin JSON for `target_date` of that job. |

**Auth:** `Authorization: Bearer <SHARED_SECRET>` on all three.

### Optional heartbeat endpoint (recommended for remote operations)

Collector can also send a periodic health payload (`COLLECTOR_HEALTH_INTERVAL`) to:

- `POST /api/collector/health` (or another path via `COLLECTOR_HEALTH_ENDPOINT`)

Expected behavior for compatibility:

- Accept JSON payload and return `200/201/202/204`.
- If endpoint is absent (`404`), collector continues without failing job polling.

Field semantics and mini-itx operating notes (which counters matter, scheduled reseed vs failed jobs): [MAINTAINERS.md §6.5](MAINTAINERS.md#65-production-operations-session-readiness-health-mini-itx).
