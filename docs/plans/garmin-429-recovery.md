# Restore Garmin collection after SSO 429

**Plan status:** Collector code changes (token-first login, client reuse, 429 handling) are **not** implemented yet; repo documentation and `env.example` for project-local tokens are in place.

**Open work**

- [ ] Token-first login + `garth.dump` after credential login; default `.garmin-tokens` next to `collector.py`
- [ ] Reuse Garmin client across jobs; re-auth only on failure
- [x] Document `GARMINTOKENS`, README, INTEGRATION, `.gitignore` for `.garmin-tokens/`
- [ ] Map login 429 to clear job failure / backoff (`GarminConnectTooManyRequestsError`)
- [ ] Align install story and merge upstream `garminconnect` / `garth` as needed

---

## Runtime context (confirmed)

- **No containers.** Only two runs:
  - **Dev:** MacBook, this repo folder, `python collector.py --poll` (occasional jobs while investigating).
  - **Prod:** Windows mini-ITX, clone of this repo, same command, **5–10 collections per day** as needed.
- **Token storage:** Use a directory **inside each clone** (e.g. `.garmin-tokens/` at the project root), not `~/.garminconnect`, so dev and prod each have isolated tokens and everything lives with the repo. **`.garmin-tokens/` is gitignored** so OAuth material is never committed.

## What changed (diagnosis)

- Your stack trace shows **429 on** `https://sso.garmin.com/sso/signin?...gauth-widget...embed...` — that is the **credential / embed SSO step**, not the wellness/activity APIs. Others report similar breakage on adjacent OAuth steps ([python-garminconnect#337](https://github.com/cyberjunky/python-garminconnect/issues/337), [python-garminconnect#332](https://github.com/cyberjunky/python-garminconnect/issues/332)). That fits **Garmin tightening limits on automated login/OAuth traffic** in mid–late March 2026, not your per-day chart pulls.
- In [`collector.py`](../../collector.py), every job does:

```113:115:collector.py
            api = Garmin(self.garmin_email, self.garmin_password)
            api.login()
```

- In [`garminconnect/__init__.py`](../../garminconnect/__init__.py), `login()` uses `tokenstore = tokenstore or os.getenv("GARMINTOKENS")`. **If `GARMINTOKENS` is unset**, `tokenstore` is falsy and the code **always** runs `self.garth.login(username, password)` — i.e. **a full SSO flow for every successful job**. ~5–10 jobs/day ⇒ ~5–10 full logins/day, which can stay under an old threshold until Garmin lowers it. Waiting 12 hours does not help if the **next attempt is still another full login**.
- Your traceback mixes **repo-local** `garminconnect` with **site-packages** `garth` — keep `garth` and the vendored library in sync with upstream when you merge/fix.

**Fork delta (since upstream merge):** History shows ~19 commits on top of the merged upstream base; they touch **collector behavior, docs, and data shaping** — not the core OAuth stack. The important gap vs upstream **examples** is **login/token handling in the collector**, not a mysterious fork-only SSO bug.

## Target outcome

- **5–10 collections per day** on prod using **one long-lived session** (tokens on disk under the project clone + in-process reuse), with **rare** password/MFA logins (token expiry, revocation, or Garmin forcing re-auth).
- **Dev** uses the same mechanism; tokens stay under the dev clone.
- **Unchanged contract** with rehab-platform: still poll [GET /api/jobs/pending](../INTEGRATION.md), [POST .../data](../INTEGRATION.md), [POST .../status](../INTEGRATION.md), same JSON shapes ([INTEGRATION.md](../INTEGRATION.md)).
- **Deployment:** Collector stays on **residential/trusted egress** (mini-ITX) per [README](../../README.md) / [INTEGRATION.md](../INTEGRATION.md); Render remains a **risk** for Garmin blocking cloud IPs unless you re-verify.

## Implementation plan

### 1. Token-first login + persist tokens (highest impact)

- Add a small helper (or inline) modeled on [`example.py`](../../example.py) `init_api()`:
  - **Default token directory:** resolve to **project root** (directory containing `collector.py`), e.g. `Path(__file__).resolve().parent / ".garmin-tokens"`. If `GARMINTOKENS` is set, use that path (expanduser); otherwise use that default. Create the directory on first successful credential login if needed.
  - **Try** `Garmin().login(str(tokenstore_path))` first; on `FileNotFoundError` / auth / connection errors, fall back to `Garmin(email, password).login()` then `api.garth.dump(str(tokenstore_path))` so the next job does not hit SSO.
- **MFA:** If the account uses MFA, mirror the `return_on_mfa` / `resume_login` pattern from `example.py`, or document a one-time interactive login (run `example.py` on the same machine with `GARMINTOKENS` pointing at `.garmin-tokens`) to seed tokens, then run the collector headless.
- **Permissions:** Restrict the token directory (`chmod 700` on Unix; on Windows, rely on user profile ACLs) and treat `.garmin-tokens` like secrets — same spirit as [upstream README](https://github.com/cyberjunky/python-garminconnect).

### 2. Reuse the Garmin client across jobs in the polling loop

- Hold an optional `self._garmin: Garmin | None` on [`GarminCollector`](../../collector.py). On each job: if client exists, **skip login** and call APIs; on **401 / auth errors**, clear client and re-run token-first login. This avoids redundant work when tokens are valid in memory and prevents **N logins** when several jobs dequeue in one poll cycle.

### 3. 429 handling and observability

- Catch `GarminConnectTooManyRequestsError` (already mapped for some paths in [`garminconnect/__init__.py`](../../garminconnect/__init__.py)) and treat like a **transient** failure: log clearly, set job `failed` with a message that distinguishes **“SSO rate limit / login throttled”** vs data API limits, optionally **longer backoff** before retry (platform or collector-side).
- Log **whether the session used tokenstore or password** (without logging secrets) so future incidents are obvious from logs.

### 4. Dependencies and upstream sync

- **Reconcile versions:** [`pyproject.toml`](../../pyproject.toml) pins package metadata at `0.2.30` with `garth>=0.5.17,<0.6.0`; [`requirements.txt`](../../requirements.txt) only has `garminconnect>=0.1.61`. Decide one install story (editable install from this repo vs PyPI) and document it so prod does not accidentally mix an old PyPI `garminconnect` with a new `garth` or vice versa.
- **Merge/cherry-pick** recent commits from [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect) into the vendored [`garminconnect/`](../../garminconnect/) tree (upstream is at **0.2.40** per their releases) and bump `garth` if upstream did — in case Garmin changes require library updates beyond token discipline.

### 5. Render / “entirely new solution”

- There is **no official drop-in** that avoids Garmin’s Connect OAuth surface for the same private wellness APIs; alternatives are **different products** (export files, partner APIs, etc.) and would break or replace your current integration shape.
- Practical split remains: **jobs + ingestion on rehab-platform**; **Garmin client on trusted egress** pushing the same payloads. Moving collection onto Render only if you **confirm** login + API calls succeed from that IP range (historically they did not per [INTEGRATION.md §1](../INTEGRATION.md)).

## Documentation (aligned with project-local tokens) — applied in repo

- [`env.example`](../../env.example): `GARMINTOKENS=.garmin-tokens` (use project root as cwd when running `python collector.py --poll`, or set an absolute path).
- [`README.md`](../../README.md): dev vs prod table; setup + troubleshooting for tokens, 429, MFA.
- [`docs/INTEGRATION.md`](../INTEGRATION.md): operational bullet on `GARMINTOKENS` / `.garmin-tokens/`.
- [`.gitignore`](../../.gitignore): `.garmin-tokens/`.

## Verification

- After deploy: confirm logs show **token login** for routine jobs; password path only after wipe/expiry.
- On prod, run a day of **5–10 jobs** and confirm **no** `sso/signin` traffic except after intentional token delete or expiry.
- Spot-check one uploaded day on rehab-platform matches pre-incident behavior.
