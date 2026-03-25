# Garmin 429 / token reuse (archived plan)

The work described here is **implemented** on **`master`**: project-local **`garmin_tokens.json`**, **`client.dump`**, Playwright helper, optional collector auto-run on 429, and **`Garmin.login`** propagating **`GarminConnectTooManyRequestsError`**.

**Current architecture and rationale:** [MAINTAINERS.md](../MAINTAINERS.md) (especially §2).

**Background** (Garth, #332/#337, garth#217, upstream `react`): [GARMIN_AUTH_LANDSCAPE.md](../GARMIN_AUTH_LANDSCAPE.md).

**Diagnosis (March 2026, still relevant):** repeating **full programmatic login** per job invites **429**; **persisting** a browser-obtainable session and reusing it for **`gc-api`** calls is the practical fix when Garmin blocks `/mobile/api/login` for an account.
