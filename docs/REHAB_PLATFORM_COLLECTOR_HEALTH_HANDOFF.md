# rehab-platform: Collector Health admin UX (agent handoff)

**Audience:** Coding agent working in the **rehab-platform** repo (`/Users/pete/Documents/GitHub/rehab-platform` or your clone).

**Prerequisite:** Deploy or run a garmin-collector build that includes **`summary`**, **`heartbeat_interval_sec`**, and existing **`garmin.collection_ready`** in heartbeat JSON (garmin-collector `collector.py` `_build_health_payload()`). Older collectors omit the new fields; UI should degrade gracefully.

**Contract reference (collector side):** [INTEGRATION.md](INTEGRATION.md) heartbeat section, [MAINTAINERS.md §6.5](MAINTAINERS.md#65-production-operations-session-readiness-health-mini-itx).

---

## Problem

`/admin` **Collector Health** is a dense table that mixes low-signal fields (`session_state`, raw counters) with operational signals operators actually need. Stale detection defaults to **300s** because the payload did not include **`heartbeat_interval_sec`** (prod uses **60s**). Warnings on **`keepalive.counters_24h.reseeded >= 3`** are false positives on mini-itx (scheduled 401 → Playwright reseed every ~30m is normal). In-memory **`collections.counters_24h`** resets when the collector restarts; **`background_jobs`** is the durable source for user collection timing and failures.

---

## Goals

1. Replace the table with a **status card per collector** (Bootstrap card layout consistent with existing `/admin` sections).
2. Combine **heartbeat state** (`collector_health_state`) with **job facts** (`background_jobs`).
3. Fix stale/fresh logic using **`heartbeat_interval_sec`** from payload when present.
4. Surface **`garmin.collection_ready`**, **`version`**, **`summary`**, and plain-English keepalive/collection status.
5. Remove or replace misleading warnings (reseed count; optional keep low-signal fast-ratio warning only when `collections_runs` is meaningful).

---

## Files to touch (rehab-platform)

| File | Work |
|------|------|
| `app.py` | Extend `_load_collector_health_rows()` (or rename) to merge SQL job stats; expose richer `ui_rows` for template. Add `_load_collector_job_stats_24h(conn, cur)` (name up to you). |
| `templates/admin.html` | Replace Collector Health table (~lines 125–200) with status cards. |
| `tests/test_collector_health.py` | Update `EXAMPLE_PAYLOAD` with new fields; assert card copy / badges / job stats when faking SQL. |

Constants in `app.py` (review, may adjust):

- `COLLECTOR_HEALTH_MAX_HISTORY = 100` — at 60s heartbeats ≈ **100 minutes** of history only; consider **500–1000** if you want ~8–16h of sparkline/debug (optional, not required for cards).
- `COLLECTOR_HEALTH_STALE_ERROR_SEC = 600` — keep as hard “offline/error” threshold or align to `max(heartbeat_interval_sec * 10, 600)`.

---

## Heartbeat payload (use these fields)

```json
{
  "collector_id": "mini-itx-prod",
  "timestamp_utc": "...",
  "version": "be41df9",
  "summary": "ok",
  "heartbeat_interval_sec": 60,
  "garmin": {
    "collection_ready": true,
    "session_state": "warm",
    "last_ok_utc": "...",
    "last_browser_reseed_utc": "...",
    "last_error": { "kind": "none", "message": null, "at_utc": null }
  },
  "keepalive": {
    "enabled": true,
    "interval_sec": 1800,
    "last_result": "reseeded",
    "last_duration_ms": 42000,
    "next_due_utc": "...",
    "counters_24h": { "runs": 53, "ok": 0, "refreshed": 0, "reseeded": 22, "failed": 0 }
  },
  "collections": {
    "last_result": "success",
    "last_duration_ms": 3200,
    "counters_24h": { "runs": 7, "fast_lt_10s": 7, "slow_ge_10s": 0, "failed": 0 }
  }
}
```

**Display priority**

| Signal | Source | UI |
|--------|--------|-----|
| Overall | `summary` + server age | Badge: **OK** / **Degraded** / **Offline** (offline when `server_received_at` older than `COLLECTOR_HEALTH_STALE_ERROR_SEC` or missing) |
| Can collect now? | `garmin.collection_ready` | Prominent yes/no (not only `session_state`) |
| Deploy | `version` | Monospace small text |
| Heartbeat | `server_received_at`, `heartbeat_interval_sec` | “Last heartbeat Xs ago (every 60s)” — rename column concept from “Freshness” |
| Last user collect (platform) | `background_jobs` | Last completed `collect_data`, duration, failures 24h |
| Background session | `keepalive.last_result`, `last_duration_ms` | Plain English: “Last background check: reseeded (42s)” — **do not warn on reseed count** |
| Errors | `garmin.last_error` | Show when `kind` not `none`/null |

**Degraded / warning rules (implement on platform)**

- `summary == "degraded"` → yellow card border or badge.
- `garmin.collection_ready == false` → warning line.
- `collections.counters_24h.failed > 0` or `keepalive.counters_24h.failed > 0` → warning (heartbeat counters; note they reset on collector restart).
- `garmin.last_error.kind` not in (`none`, `null`, absent) → show error block.
- **Do not** warn on `keepalive.counters_24h.reseeded >= 3` (remove `warn_reseeded`).

**Optional:** footnote on card: “24h counters in heartbeat reset when collector restarts; job stats below are from the database.”

---

## SQL: `background_jobs` (24h user collections)

`background_jobs` schema (existing):

- `job_id`, `job_type`, `target_date`, `status`, `error_message`, `created_at`, `updated_at`

Filter **`job_type = 'collect_data'`** (verify exact string in `app.py` / `jobs.py` where jobs are created).

Suggested queries (PostgreSQL/SQLite-compatible patterns; adapt to your DB layer in `app.py`):

```sql
-- Last completed user collect
SELECT job_id, target_date, status,
       EXTRACT(EPOCH FROM (updated_at - created_at)) AS duration_sec,
       updated_at
FROM background_jobs
WHERE job_type = 'collect_data' AND status = 'completed'
ORDER BY updated_at DESC
LIMIT 1;

-- 24h completed / failed counts (use server TZ or UTC consistently)
SELECT
  COUNT(*) FILTER (WHERE status = 'completed') AS completed_24h,
  COUNT(*) FILTER (WHERE status = 'failed') AS failed_24h
FROM background_jobs
WHERE job_type = 'collect_data'
  AND created_at >= NOW() - INTERVAL '24 hours';

-- Last failure
SELECT job_id, target_date, error_message, updated_at
FROM background_jobs
WHERE job_type = 'collect_data' AND status = 'failed'
ORDER BY updated_at DESC
LIMIT 1;

-- Slow jobs (wall-clock wait: pending until completed)
SELECT job_id, target_date,
       EXTRACT(EPOCH FROM (updated_at - created_at)) AS duration_sec
FROM background_jobs
WHERE job_type = 'collect_data'
  AND status = 'completed'
  AND created_at >= NOW() - INTERVAL '24 hours'
  AND EXTRACT(EPOCH FROM (updated_at - created_at)) > 30
ORDER BY duration_sec DESC
LIMIT 5;
```

Expose to template e.g. `job_stats_24h: { completed, failed, last_completed, last_failed, slow_jobs }` keyed by collector if you ever have multiple collectors; today a single global stats block under the card is fine.

---

## UI sketch (status card)

One card per row in `collector_health_state`:

```
┌─ mini-itx-prod ──────────────────── [OK] ─┐
│ Heartbeat: 12s ago (every 60s)  v be41df9 │
│ Collection ready: Yes                     │
│ Last user job (DB): 2026-05-27 — 4.2s     │
│ 24h jobs: 12 completed, 0 failed          │
│ Background: reseeded (41s), next ~14:30   │
│ [no error]                                │
└───────────────────────────────────────────┘
```

Offline card: no row or `server_received_at` > 600s → red **Offline**, hide heartbeat-derived fields.

Use existing Bootstrap badges (`bg-success`, `bg-warning`, `bg-danger`) like the cache stats section.

---

## `app.py` implementation notes

1. In `admin()` route (~where `collector_health_rows = _load_collector_health_rows(conn, cur)`), also call new job-stats loader once per page load (not per collector unless you add `collector_id` to jobs later).

2. Extend `_load_collector_health_rows` to pass through:
   - `summary`, `version`, `heartbeat_interval_sec`
   - `collection_ready` from `garmin.collection_ready`
   - `overall_badge`: derive `offline` | `degraded` | `ok` (merge `summary` with stale logic)
   - Remove `warn_reseeded` or set always false.

3. Stale/fresh (existing logic ~lines 245–261):
   - Use `heartbeat_interval_sec` from payload (already attempted); with collector deployed, prod should get **60**.
   - `stale_badge_threshold_sec = max(expected_interval_sec * 2, COLLECTOR_HEALTH_STALE_ERROR_SEC)` — consider `* 3` for 60s → 180s “fresh” window if desired.

4. `POST /api/collector/health` — **no change required** unless you add server-side validation of new fields; store full JSON as today.

5. Update `tests/test_collector_health.py`:
   - Add `summary`, `heartbeat_interval_sec`, `garmin.collection_ready` to `EXAMPLE_PAYLOAD`.
   - Fake cursor: handle new SELECTs for `background_jobs` or mock job stats function.
   - Assert admin HTML contains “Collection ready” (or chosen label) and not “warning: reseeded”.

---

## Verification (rehab-platform)

```bash
# From rehab-platform repo root — see that repo's .cursor/rules/python-agent-verify.mdc
python -m unittest tests/test_collector_health.py -v
```

Manual: POST example payload with bearer token, open `/admin` as admin, confirm card layout and stale at 60s interval.

---

## Out of scope (unless user asks)

- Changing collector heartbeat frequency or keepalive behaviour.
- Graphs from `collector_health_history` (optional follow-up).
- Multi-collector job attribution (jobs table has no `collector_id` today).

---

## Acceptance checklist

- [ ] Admin shows **status card**, not the old six-column table.
- [ ] **`collection_ready`** and **`version`** visible.
- [ ] Stale/fresh uses **`heartbeat_interval_sec`** when present (60s prod).
- [ ] **No** reseed-count warning.
- [ ] **24h job stats** from `background_jobs` on the card with footnote about heartbeat counter reset.
- [ ] Tests updated and passing.
- [ ] Degraded/offline badges match rules above.
