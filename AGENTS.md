# Agent notes (Cursor)

**Source of truth:** this file lives in **git**—Cursor loads it for this project. **Do not paste** its contents into Cursor **User rules** or other settings (you would maintain two copies).

Human expectations (commit/review/tests/docs): **[`.cursor/WORKING-AGREEMENT.md`](.cursor/WORKING-AGREEMENT.md)**.

**Documentation:** **[`.cursor/rules/documentation-hygiene.mdc`](.cursor/rules/documentation-hygiene.mdc)** — stewardship (correct / consolidate / prune); additive Markdown only when the bar is met.

**Guidance stack:** thin Cursor user rules → this file → `.cursor/rules/` → `docs/` for depth.

## Quick verify

From repo root (PDM installed):

```bash
pdm run test
```

Details, VCR cassettes, and Garmin API constraints: **[`.cursor/rules/python-agent-verify.mdc`](.cursor/rules/python-agent-verify.mdc)**.

**Auth / Garmin API changes:** read **[`docs/MAINTAINERS.md`](docs/MAINTAINERS.md)** before editing `collector.py`, `garminconnect/`, or `scripts/garmin_playwright_login.py`. See also **[`.cursor/rules/auth-and-garmin-api.mdc`](.cursor/rules/auth-and-garmin-api.mdc)**.

## Full policy index

| Topic | Where |
|--------|--------|
| Agent workflow, commit, testing policy | [`.cursor/WORKING-AGREEMENT.md`](.cursor/WORKING-AGREEMENT.md) |
| Docs stewardship | [`.cursor/rules/documentation-hygiene.mdc`](.cursor/rules/documentation-hygiene.mdc) |
| Python test commands (agent sandbox) | [`.cursor/rules/python-agent-verify.mdc`](.cursor/rules/python-agent-verify.mdc) |
| When tests are required | [`.cursor/rules/testing-policy.mdc`](.cursor/rules/testing-policy.mdc) |
| Auth, 429/401, failure modes, upstream sync | [`docs/MAINTAINERS.md`](docs/MAINTAINERS.md) |
| Runbook, deployment, troubleshooting | [`README.md`](README.md) |
| HTTP + JSON contract with rehab-platform | [`docs/INTEGRATION.md`](docs/INTEGRATION.md) |
| Garth, upstream issues, `react` branch | [`docs/GARMIN_AUTH_LANDSCAPE.md`](docs/GARMIN_AUTH_LANDSCAPE.md) |
| rehab-platform calendar day, ingest, TRIMP | [rehab-platform ARCHITECTURE.md](https://github.com/bovreuil/rehab-platform/blob/main/ARCHITECTURE.md) |
