# AI working agreement (garmin-collector)

Lean expectations for **this repo**. Detailed TDD / red–green behaviour lives in the **tdd-working-cycle** (and **bdd-example-workshop**) Cursor skills—do not duplicate them here. Use those skills only when the user explicitly asks for example-first or test-first work.

**Do not paste `AGENTS.md` or this file into Cursor User rules**—Cursor reads these files from git; copies in settings go stale.

## Global vs project (Cursor)

| Layer | Location | Scope |
|--------|----------|--------|
| **Cursor User rules** | **Settings → Rules** (User) | All workspaces—keep **thin**; point at this repo’s files when in garmin-collector |
| **`AGENTS.md`** | Repo **root** | Entry index—loaded from disk; **no duplicate paste** in User rules |
| **This file** | `.cursor/WORKING-AGREEMENT.md` | garmin-collector-only workflow and product habits |
| **`.cursor/rules/*.mdc`** | `.cursor/rules/` | Always-on / glob rules (doc hygiene, testing policy, Python verify, auth) |
| **`docs/`** | `docs/` | Auth design, integration contract, upstream context |

## Workflow

- **Commit** only when the user explicitly asks.
- Prefer **review** and **test** before commit when that is the active flow; summarise what to review after substantive changes.
- Prefer **small, atomic** changes with natural stop points.
- For multi-step work, stop at natural commit boundaries and hand off for review—no need for heavy phased-plan templates unless the user asks.

### Commit messages

Use the **commit-prep** and **commit-after-approval** Cursor skills when those flows apply. Do **not** infer message style from **`git log`**.

## Tests and TDD

Follow [`.cursor/rules/testing-policy.mdc`](rules/testing-policy.mdc). Summary:

- Tests are expected when changing **transformation, classification, or protocol** logic (auth error types, payload shaping, token load/dump, retry semantics).
- Tests are **not** required for pure polling loop wiring or README-only changes.
- Use **tdd-working-cycle** only when the user explicitly requests test-first work.

Do **not** change tests to pass without **explicit** user approval.

## Product and code

- Prefer **self-documenting code** and **sparse** comments for non-obvious **why**; routine “what changed” belongs in the **commit message**.
- Read **[`docs/MAINTAINERS.md`](../docs/MAINTAINERS.md)** before auth or vendored `garminconnect/` changes.

## Agent operations (this service)

- Do **not** run **`collector.py --poll`** against production or live Garmin without **explicit user approval**.
- Do **not** run **Playwright login** or re-record **VCR cassettes** against live Garmin without user approval.
- **Never commit** `.env`, `garmin_tokens.json`, or anything under `.garmin-tokens/`.
- Live collection and browser reseed on the mini-ITX are **user-operated** unless the user asks the agent to run a specific command.

## Documentation

Follow [`.cursor/rules/documentation-hygiene.mdc`](rules/documentation-hygiene.mdc): **stewardship** (correct / consolidate / prune); **additive** Markdown only when that rule’s high bar is met.

## Suggested Cursor **User** rules (global, thin)

Use **Cursor → Settings → Rules → User**. Keep this block small; everything garmin-collector-specific stays in git (`AGENTS.md`, this file, `.cursor/rules/`).

```markdown
# AI

Be concise; ground answers in repo files, tests, and diffs—not guesses.

If the workspace root contains **`collector.py`** and **`docs/MAINTAINERS.md`** (garmin-collector): follow **`AGENTS.md`**, **`.cursor/WORKING-AGREEMENT.md`**, and **`.cursor/rules/`**. Do not paste or paraphrase those as a substitute for reading them.

Otherwise: follow that project’s **`AGENTS.md`** / **`.cursor/rules/`** if present; otherwise prefer small, reversible changes and ask when scope is unclear.

For commits: use **commit-prep** / **commit-after-approval** skills when those flows apply. Do not infer commit message style from **git log**.
```
