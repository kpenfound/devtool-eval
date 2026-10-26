# devtool-eval

A harness that has coding agents (Claude Code, Codex) adopt a dev tool in real repos, then
measures how well they did and why. Dagger onboarding is the first tool pack.

See [docs/DESIGN.md](docs/DESIGN.md) for the design and [docs/SETUP.md](docs/SETUP.md)
for the one-time host, GitHub, and Dagger Cloud setup.

## Quick start

```sh
uv sync
cp harness.example.toml harness.toml   # local-only as copied; see docs/SETUP.md
uv run harness doctor
uv run harness run --repo zod --bundle baseline --agent claude --no-cloud
uv run harness report -o reports/latest.md
```

The example config has one local-only slot (no `repo`), so runs need `--no-cloud` until the
GitHub org and Dagger Cloud are set up.

## What a run does

`harness run --repo flask --bundle baseline --agent claude [--tool-version v1.0.0-beta.20] [--no-cloud]`

1. **Setup:** recovers any crashed run, then leases a slot. Resets the slot to the corpus
   repo's pinned SHA, builds a fresh fake `HOME` (with `CLAUDE_CONFIG_DIR` and `CODEX_HOME`
   inside it), copies in the variant bundle, pins the tool version, and resets the engine.
2. **Prompt:** the pack's fixed, neutral task prompt.
3. **Execute:** the agent runs headless through its SDK in a worker process group. The
   wall-clock budget is enforced by killing the whole group; turn and cost budgets are
   enforced inside the worker.
4. **Verify:** the harness runs `dagger check` itself.
5. **Retry:** a scripted user pastes the real error back, up to `retries` times. Each
   nudge is recorded.
6. **Feedback turn:** resumes the session and asks for a structured self-report. The diff
   is captured before this turn.
7. **Integration check:** Cloud Checks state and results, by pushed commit SHA.
8. **Store and reset:** normalizes events, runs the analyzers, builds the docs map, runs
   the referee, ingests into DuckDB, and tears down the slot (in a `finally`).

## Run bundle

`<state_dir>/runs/<run_id>/`:

| File | What |
| --- | --- |
| `run.json` | metadata, versions, budgets, every agent turn, verify attempts, nudges, pushed SHAs, integration results |
| `transcript.raw.jsonl` | raw SDK messages (source of truth) |
| `events.jsonl` | normalized events (regenerate with `harness normalize`) |
| `diff.patch` | committed + uncommitted changes vs the pinned SHA |
| `final/` | snapshot of `dagger.toml`, `dagger.lock`, `.dagger/`, CI files |
| `verify.log`, `verify.N.log` | harness verification output per attempt |
| `feedback.json` | the agent's self-report |
| `analysis.json` | deterministic facts (core + pack analyzers) |
| `docs_map.json` | docs pages visited, doc version, what happened next |
| `scorecard.json` | referee scores and findings, version-tagged |
| `human-scores.yaml` | optional, written by a person for calibration |

## Commands

| Command | |
| --- | --- |
| `run` | one scenario end to end |
| `normalize` / `analyze` / `referee` | re-derive one stage for an existing bundle (`referee --dry-run` prints the blinded prompt) |
| `report` | markdown across runs: cells, scores per rubric version, friction, issues, docs map, calibration |
| `calibrate` | human-scores.yaml vs referee |
| `parity` | Claude CLI vs SDK init-frame diff |
| `corpus list/show/capture` | inventory CI and capture expected coverage (`dagger module recommend`) at the pinned SHA |
| `slots status/recover/release/init` | slot pool |
| `docs sync` | load docs sitemaps |
| `github mirror` | create a `ref-<name>` mirror |
| `store rebuild/sql` | DuckDB index |
| `doctor`, `bundles`, `runs` | |

## Layout

- `src/harness/`: the generic core. Runner, agent adapters (`agents/`), event schema and
  normalizers, run bundles, slots and GitHub App plumbing, referee framework
  (`referee/`), docs map, store, reports.
- `src/harness_dagger/`: the Dagger pack (environment, verify, analyzers, Cloud Checks
  hooks) and its data: rubric, expert context, prompts, docs sources, corpus, and
  variant bundles in `data/`.

Packs implement the `Protocol`s in `harness/pack.py` and register under the
`harness.packs` entry point.

## Adding a variant bundle

`src/harness_dagger/data/bundles/<name>/bundle.yaml`:

```yaml
name: my-variant
version: "1"
description: ...
instructions: instructions.md   # becomes user-level CLAUDE.md / AGENTS.md
skills_dir: skills              # skills/<skill>/SKILL.md
mcp: {}                         # name -> MCP server config
docs_pointers: []               # appended to the instructions
```

Bump `version` whenever the content changes. Runs record the version and a content hash.
The bundle's name and distinctive lines are redacted from everything the referee sees.
