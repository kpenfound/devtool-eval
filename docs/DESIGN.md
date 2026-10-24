# Dev Tools Onboarding Harness — Design

Oct 24, 2026 · @Kyle Penfound

## Purpose

A generic harness that has coding agents (Claude Code, Codex) adopt a dev tool in real repos, then measures how well they did and why. Dagger onboarding is the first tool pack.

Goals:

- Find where agents get stuck adopting Dagger, and attribute each failure to a cause Dagger can fix (docs, API, error messages, modules).
- Measure how much skills and guidance help, by varying what the agent is given.
- Map which docs agents use, which they miss, and where docs disagree across versions.
- Keep the core tool-agnostic so other dev tools can plug in a pack later.

The harness lives in a public repo on Kyle's GitHub, with the Dagger pack in the same repo.

## Architecture

&#91;embedded content: core pipeline · 6 stages, 1 tool pack\]

Stages pass a run bundle on disk; the tool pack supplies everything tool-specific.

**Generic core:** runner (isolation, budgets, retry policy, feedback turn), agent adapters, normalizer and event schema, run bundles, store, slot pool and GitHub App plumbing, referee framework (blinding, evidence rule, versioning), docs map mechanism, reports.

**Tool pack:** a Python package implementing a few `Protocol` classes, discovered via entry points, plus YAML/Markdown for rubric, prompts, and bundles. It provides:

- **Environment:** install, auth state, engine and cache reset
- **Verify:** standard pass/fail plus details
- **Analyzers:** deterministic facts, e.g. module strategy from `dagger.toml`
- **Integration hooks:** enable, verify by SHA, teardown
- **Rubric and expert context** for the referee, plus redaction rules
- **Docs sources:** domains, version scheme, sitemap
- **Corpus extensions:** expected coverage, `out_of_scope` reasons

Define the interfaces now but build Dagger end to end before generalizing. A second pack will show where the seams are wrong.

**Stack:** Python with uv, pydantic (schemas), typer (CLI), DuckDB (store), Claude Agent SDK and `openai-codex` for agents. Agent auth is switchable between subscription and API key by config.

## Run flow

A run is one scenario (repo + bundle + agent), triggered manually: `harness run --repo flask --bundle baseline --agent claude`. No scheduler for now.

1. **Setup:** lease a slot repo, reset it to the corpus repo's pinned SHA, create a fresh agent config dir, copy in the variant bundle, reset engine state.
2. **Prompt:** one fixed, neutral task prompt. It never mentions Cloud Checks, CD limits, or the bundle.
3. **Execute:** the agent runs headless via its SDK, with wall-clock, turn, and cost budgets and a hard kill.
4. **Verify:** the harness runs the pack's verification itself (for Dagger, `dagger check`). It never trusts the agent's "done."
5. **Retry (optional):** a scripted simulated user feeds back the real error, a fixed number of times. Every nudge is recorded.
6. **Feedback turn:** resume the session and ask for a structured report on the tool. Asked only after the task, so it can't bias the work.
7. **Integration check:** the pack verifies external integrations by commit SHA (for Dagger, Cloud Checks green).
8. **Store and reset:** write the run bundle, then reset the slot. A crashed run is cleaned up on the next invocation.

**Isolation rules**

- Agents run on a dedicated host, not in containers.
- Agent memory is off and user-level instructions never leak in: fresh `CLAUDE_CONFIG_DIR` / `CODEX_HOME` per run, Claude SDK with `setting_sources` limited to the bundle.
- Base environment (same for every run): tool installed, `dagger login` state, slot-scoped GitHub token.
- SDK runs must match real CLI behavior: Claude Code's preset system prompt, same tools and permission mode. Verify once by diffing a CLI and SDK run.

**Run bundle (on disk):** `run.json` (metadata and versions), raw transcript, `diff.patch`, verify log, feedback report, scorecard.

## Dimensions and corpus

Each run varies one cell of: agent × repo × bundle × tool version × Cloud available or not.

- **Agent:** Claude Code, Codex.
- **Bundle:** versioned config bundles of skills, CLAUDE.md/AGENTS.md, MCP config, and docs pointers. Includes a no-help baseline.
- **Cloud:** with an ambient Dagger Cloud login, or without one. Without it, still score whether the agent recommends Cloud Checks.
- **Tool version:** pinned per run so results are comparable.

**Starting corpus (6 repos)**

| Repo | Stack | Tier | What it tests |
| --- | --- | --- | --- |
| Flask | Python, pytest | 1 | Official module happy path; baseline |
| Small vitest lib (TBD) | TS, vitest | 1 | Official module happy path for JS |
| Hugo | Go | 1–2 | Large single-language build and test |
| Pillow | Python, C extensions | 2 | Default image not enough; custom container or module |
| spring-petclinic | Java, Testcontainers | 2 | Services vs docker-in-docker |
| Immich | TS, Svelte, Python ML, Postgres, Android | 3 | Monorepo, services, third-party Android module |

Later candidates: ripgrep (Rust cross-compile), a GitLab CI or Jenkins repo, a protobuf repo (generators), a publishing repo, a repo with no CI, a Make-heavy repo.

**Corpus rules**

- Every repo is at least partly covered by official modules. Uncovered stacks get a module built first, not a corpus entry.
- Each entry records: pinned SHA, stack and tier tags, existing CI inventory, and expected official coverage captured once from `dagger setup`.
- Existing CI jobs are tagged `out_of_scope` with a reason: `cd` (Cloud Checks doesn't do CD) or `non-linux`. Decided per job: Android builds are in scope, iOS builds are not. Out-of-scope jobs are excluded from gaps but what happened to them is recorded.
- Agents are never told about these limits.

## Measurement

Deterministic checks live in the harness; the referee only judges what needs judgment. Every referee score must cite evidence (a transcript event or file:line), or it isn't recorded.

**Deterministic (harness)**

- `dagger check` pass/fail, which checks exist, retries needed
- Module strategy, classified from `dagger.toml` (table below)
- CI choice: Cloud Checks, `dagger check` in old CI, unchanged, or other
- Cloud Checks enabled and green, looked up by SHA
- Cost, duration, turns

**Module strategy**

| Outcome | Verdict |
| --- | --- |
| Official only | Good: the project is a fit |
| Official + custom, no overlap | Good: advanced customization |
| Official + custom, with overlap | Partial: reimplemented coverage |
| Custom only | Bad, always, given the corpus guarantee |
| Nothing working | Fail |

Treated as reimplementation: skipping an official module's checks via `check.skip` and replacing them with custom ones. Decorative installs (installed, never run) are flagged. A custom module that wraps or composes an official one is legitimate. Third-party modules are report-only until an index exists.

**Referee: scored (1–4, anchors TBD)**

1. **Overlap:** does any custom function do what an installed or expected module already covers?
2. **Custom module quality:** sensible function boundaries, checks and generators annotated properly.
3. **Pipeline soundness:** caching, services instead of docker-in-docker, secrets handling, `dagger.lock` committed.
4. **Fidelity:** checks exercise the project's real tests. Penalize skipped or deleted tests, narrowed scope, `|| true`.
5. **Idiomatic 1.0 usage:** workspace config and settings, not legacy `dagger.json` patterns.

**Referee: descriptive (not scored)**

- CI gaps list: what in-scope old CI does that nothing in Dagger covers. No 1:1 step mapping.
- Root cause per friction point: docs gap, API confusion, error message, engine bug, SDK gap, or agent mistake.
- Hallucinated APIs and legacy-version confusion.
- Docs map: each page fetched, which doc version it came from, and the outcome after it.
- The agent's self-report claims, marked confirmed or refuted.
- Issue candidates, deduped across runs, drafted for Kyle to review.

**Referee rules**

- Model is configurable; not every user has multiple vendors.
- Blind to the bundle where possible: variant names and skill content are redacted from what it reads.
- Scores are tagged with rubric and referee model versions. No comparisons across versions.
- Calibration: Kyle writes a human-scores YAML next to a run bundle, compared against the referee's scorecard.

## GitHub and Dagger Cloud plumbing

Automation runs through a GitHub App on a test org owned by a real account. No dummy user accounts: a previous one was banned after creating and closing hundreds of issues and PRs.

- **Org:** a test org with Dagger's GitHub integration installed once, with access to all repos. This is the only interactive Cloud Checks step, done ahead of time.
- **Mirrors (optional):** `ref-<name>`, private, non-fork copies of each corpus repo at its pinned SHA.
- **Slots:** `slot-01..N`, private generic repos. Start with one or two, since runs are manual.
- **Never forks.** An agent running `gh pr create` in a fork can target the real upstream project.
- **Tokens:** each run gets a token scoped to its slot only.
- **Activity:** push to branches; open PRs only if needed. Keeps GitHub volume low.
- **Results:** always looked up by the run's commit SHA, never "latest", since slot history carries over.
- **Teardown (in a `finally`):** turn Cloud Checks off for the slot, reset it to the pinned SHA, delete branches and PRs, revoke the token.
- **Dagger Cloud:** a test org, with the token scoped to it.

## Decisions

| Decision | Why |
| --- | --- |
| Python (uv, pydantic, typer, DuckDB) | Change of pace from Go; strong analysis and referee tooling |
| Official agent SDKs (Claude Agent SDK, `openai-codex`) | Session resume and isolation built in; adapters stay thin |
| Subscriptions now, API key by config | SDK use counts against subscription limits for now; policy may change |
| Generic core, Dagger pack in same repo | Split into packs only once a second pack exists |
| Public repo on Kyle's GitHub | Open source from the start |
| Manual runs, no scheduler | One scenario at a time for now |
| Agents on host, not in containers | Dedicated machine; isolation via fresh config dirs |
| No pushing the agent beyond scripted retries | Where agents stall is the signal |
| Event schema derived from real runs | Designed against captured Claude and Codex output |
| Referee model configurable | Not all users have multiple vendors |
| Scores version-tagged, no re-scoring | Simpler; no cross-version comparisons |
| Human calibration via YAML beside the bundle | Lowest-effort input path |
| No 1:1 CI step mapping | CI rarely maps cleanly; report gaps descriptively |
| Cloud access ambient, never prompted | Measures discovery fairly |

## First milestone and open items

**Milestone:** the full loop on one repo (Flask). Run, verify, Cloud Checks by SHA, referee scorecard, stored bundle.

- [ ] Capture raw Claude and Codex runs on Flask; derive the normalized event schema from them
- [ ] Diff a CLI run against an SDK run to confirm parity
- [ ] Set up the test org, GitHub App, one slot, and the Dagger Cloud test org
- [ ] Find the commands or API to query Cloud Checks by SHA and to turn them off
- [ ] Define the referee interface: pack-supplied criteria, analyzers, redaction rules, scorecard schema
- [ ] Write the task prompt and the feedback-turn prompt
- [ ] Write rubric anchors after grading a few runs by hand

**Dagger pack configuration still to fill in:** the vitest repo, pinned SHAs, `out_of_scope` tags per repo, expected coverage from `dagger setup`, bundle contents, budgets, retry count, and which Dagger versions to track.
