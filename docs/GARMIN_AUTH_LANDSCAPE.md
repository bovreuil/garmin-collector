# Garmin authentication landscape (Garth, upstream issues, `react` branch)

This document summarizes **how this project talks to Garmin**, what **Garth** is, what the community has reported in **March 2026**, and **practical options** (including the upstream **`react`** branch). It complements [INTEGRATION.md](INTEGRATION.md) (rehab-platform contract) and [plans/garmin-429-recovery.md](plans/garmin-429-recovery.md) (collector token reuse and implementation notes).

**Concrete test timeline, SHAs, and next-agent checklist:** [AGENT_HANDOFF_GARMIN_MARCH_2026.md](AGENT_HANDOFF_GARMIN_MARCH_2026.md).

**This repository:** On branch **`experiment/react-garmin`**, the vendored **`garminconnect/`** package and root **`pyproject.toml`** track **[cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect) `upstream/react`** (no **Garth** dependency; JWT / `garmin_tokens.json` session files). **`collector.py`** uses **`api.client.dump()`** after login. Merge to **`master`** when you are satisfied with real-world runs; stay on **`master`** for the older Garth-based vendor until then.

### Staying aligned with `upstream/react`

After `git remote add upstream https://github.com/cyberjunky/python-garminconnect.git` (once), refresh and compare:

```bash
git fetch upstream
git diff upstream/react -- garminconnect/ pyproject.toml
```

An **empty diff** means your vendored library already matches the tip of **`react`**. To **reset** those paths to upstream:

```bash
git checkout upstream/react -- garminconnect/ pyproject.toml
pip install -r requirements.txt
```

If **connect.garmin.com works in a normal browser** but **`429 Rate Limit`** still appears when the collector calls **`/mobile/api/login`**, Garmin is blocking that **programmatic** path for your account or IP. Waiting does not always clear it; the practical fallback is **browser-based authentication** (e.g. Playwright) to obtain tokens, then **`load` / `dump`** in the same **`GARMINTOKENS`** directory the collector uses — see discussion in **[matin/garth#217](https://github.com/matin/garth/issues/217)**.

---

## How garmin-collector authenticates today

- The collector uses the **`garminconnect` Python package** (vendored in this repo, forked from [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect)).
- That package historically relied on **[Garth](https://github.com/matin/garth)** for **SSO / OAuth-style login** and token persistence.
- This repo **persists tokens** under a project-local directory (default **`.garmin-tokens/`**, configurable via **`GARMINTOKENS`**) so routine jobs avoid repeating a full SSO sign-in on every run. See the README and `env.example`.

If Garmin blocks or changes the **programmatic login** endpoints, **no amount of polling discipline** fixes it until login succeeds again or the stack is updated (e.g. upstream `react` branch or another workaround).

---

## What is Garth?

**[Garth](https://github.com/matin/garth)** (`matin/garth` on GitHub) is a Python library that implements Garmin’s **SSO + token exchange** flow used to obtain and refresh credentials for **Garmin Connect / Connect API**. It stores **oauth-related token files** on disk and provides an HTTP client aimed at `connectapi.garmin.com`.

**python-garminconnect** uses Garth internally: `Garmin.login()` delegates to Garth for sign-in. Therefore, **Garth-only projects** and **classic python-garminconnect** users often see **the same class of failures** when Garmin or Cloudflare tightens the **login surface**.

---

## Upstream python-garminconnect issues (March 2026)

Worth tracking on GitHub (state may change after this doc was written):

- **[#332 – Did Garmin change authentication API?](https://github.com/cyberjunky/python-garminconnect/issues/332)** — **401** and related failures on OAuth-style steps (e.g. `preauthorized`).
- **[#337 – 429 Too Many Requests during login (OAuth Preauthorized)](https://github.com/cyberjunky/python-garminconnect/issues/337)** — **429** during login/OAuth; maintainer linked it to **#332**. Thread includes:
  - Reports that **only the global** Connect stack fails for some users while **China (`--cn`)** paths behave differently (not applicable if your account is global).
  - Maintainer **@cyberjunky** describing a **`react` branch** experiment: move toward the **new Connect web app** flow (**`/gc-api`**, **JWT**), **`garmin_tokens.json`** instead of classic oauth1/oauth2 files, while keeping **load/dump**-style persistence. **Not guaranteed** to be the final direction; some users reported **success**, others later reported **regressions** as Garmin or the branch evolved.

Always read the **latest comments** on those issues before deciding on a long-term approach.

---

## Garth issue #217 — central discussion for SSO / 429

**[matin/garth#217 – 429 on login (incl. curl from residential IPs)](https://github.com/matin/garth/issues/217)**

Themes from that thread (summary, not exhaustive):

- **429 on SSO-related URLs** appears for **many people and regions**, sometimes even with **plain `curl`** and **no app code** — suggesting **Cloudflare / edge** involvement, not only “bad scripts.”
- **Existing saved tokens** often still work for **API calls**; **fresh** `login(email, password)` is what fails for many.
- **Mixed results** on fixes such as pinning **[PR #218](https://github.com/matin/garth/pull/218)** — some report improvement, others do not.
- **Separate problems** are discussed: **SSO 429** vs **401 from cloud/datacenter IPs** on Connect API even with valid tokens (e.g. **GitHub Actions**); **residential** egress often behaves differently.
- **Workarounds people discuss**: **browser automation (e.g. Playwright)** to log in like a human and obtain tokens without hitting the same programmatic endpoints; generating tokens **at home** and **copying** them to another machine (works for some, not all; tokens may be short-lived or IP-sensitive).
- **Reverse-engineering notes** (e.g. portal / **CAS**-style flows, **Cloudflare challenges**) implying **full parity without a real browser** may get harder over time.
- **python-garminconnect** maintainer participation aligning with **JWT / new web stack** rather than assuming the **old Garth OAuth path** will return to “set and forget.”

Use **#217** as the main **Garth-side** index; it links to related projects (e.g. running_page, withings-sync) that hit the same Garmin changes.

---

## Optional path: experiment with upstream `react` branch

The **`react` branch** lives on **[cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect)**. It is **experimental**: different internal client, **JWT** / **`gc-api`**, different token file layout (**e.g. `garmin_tokens.json`**). The maintainer has aimed for **similar high-level method names** for data access, but **you should expect import, exception, and `login()` differences** until you verify against your collector.

**High-level steps if you adopt it in this project:**

1. Read the **latest** instructions in that branch’s README / demo.
2. In a clean venv, install that branch editable (`pip install -e .`) and run **`demo.py`** until **one successful login** and token files appear under your chosen directory.
3. **Replace or merge** the vendored **`garminconnect/`** tree in **garmin-collector** with the **`react`** branch sources (or depend on an editable install and remove the duplicate vendored copy so only one package resolves).
4. Point **`GARMINTOKENS`** at the directory the **`react`** branch expects (align with its `load`/`dump` behavior — often the same **`.garmin-tokens`** path is fine if the library writes into that folder).
5. Run **`python collector.py --poll`** and fix any **API or import mismatches**.
6. Expect **churn**: issue **#337** includes reports of **`react` working then breaking** after Garmin or branch updates. For a **home** deployment with modest frequency, you may still be acceptable risk.

This repo **does not** pin you to `react` by default; documenting it here is so you can **choose** when to try it.

---

## Where to test `react`: branch in this repo vs a separate clone

Both are valid; pick based on how messy you want your working tree during the spike.

| Approach | Pros | Cons |
|----------|------|------|
| **Git branch in this repo** (e.g. `experiment/react-garmin`) | Single place: **collector + vendored `garminconnect`** evolve together; easy **`git merge`** when happy; one remote. | Replacing **`garminconnect/`** is a **large diff**; switching branches swaps the whole vendor tree; easy to confuse if you jump branches mid-debug. |
| **Separate clone** of `python-garminconnect` only | **Zero risk** to garmin-collector until you copy files or merge; ideal for **“does `demo.py` login at all?”** on `react`. | Two folders to open; you must **manually copy or subtree-merge** into garmin-collector when promoting. |
| **Two full clones** of garmin-collector | Side-by-side **master vs react-integrated** collectors on disk. | More disk; duplicate `.env` / secrets discipline. |

**Practical recommendation**

- **First spike (“does login work?”):** a **separate clone** of **python-garminconnect** on **`react`**, venv + **`demo.py`** only — fastest feedback, no collector changes yet.
- **Integration (“make collector work on `react`”):** a **feature branch in garmin-collector** where you replace **`garminconnect/`** (or switch install strategy) and adjust **`collector.py`** as needed, then merge to **`master`** when stable enough for your mini-ITX.

---

## Related links (bookmark)

| Resource | URL |
|----------|-----|
| Garth | https://github.com/matin/garth |
| Garth SSO / 429 discussion | https://github.com/matin/garth/issues/217 |
| python-garminconnect | https://github.com/cyberjunky/python-garminconnect |
| Issue #332 (auth change) | https://github.com/cyberjunky/python-garminconnect/issues/332 |
| Issue #337 (429 login) | https://github.com/cyberjunky/python-garminconnect/issues/337 |

---

## Disclaimer

Garmin’s terms and technical measures may restrict unofficial API access. This document describes **community-reported behavior** and **engineering options** for a **personal** integration; it is not legal or contractual advice.
