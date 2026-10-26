# Host and org setup

One-time steps the harness can't do for you. Local-only runs (a slot with no `repo`, plus
`--no-cloud`) need only steps 1-3 and 6.

## 1. Host

- A dedicated machine. Runs execute agents directly on the host, and with
  `engine_reset = "prune"` every run deletes the Dagger engine container and its cache.
- Docker, git, `dagger`, `claude`, `codex`, `gh`, and `uv` installed (macOS or Linux).
- Clone this repo and run `uv sync`.
- `uv run harness doctor` shows what's missing.

## 2. Agent auth

Each run uses a fresh `HOME`, so agents can't find your normal logins. That keeps your
memory and instructions out of the run.

- **Claude, subscription:** `claude setup-token`, then export `CLAUDE_CODE_OAUTH_TOKEN`
  in your shell profile (or wherever `harness` runs from), not just the current shell.
- **Claude, API key:** `agents.claude.auth = "api_key"` and export `ANTHROPIC_API_KEY`.
- **Codex, subscription:** `codex login` once; the harness copies `~/.codex/auth.json` into
  each run (`agents.codex.auth_json`). Refresh tokens are single-use, so when a run
  refreshes its copy the harness writes it back. Don't share that `auth.json` with another
  machine or a Codex you use by hand: whichever refreshes first logs the other out.
- **Codex, API key:** `agents.codex.auth = "api_key"` and export `OPENAI_API_KEY`.

## 3. Config

`cp harness.example.toml harness.toml` (or point `HARNESS_CONFIG` at a copy). As copied, it
has one local-only slot and no GitHub or Cloud settings, so it's ready for `--no-cloud`
runs. Check:

- `agents.claude.model` is pinned. Runs are only comparable on the same model.
- `packs.dagger.engine_reset`: `prune` (the default) deletes the Dagger engine and its cache
  every run. Use `none` on a machine you also use for other Dagger work.
- `isolation.home_passthrough`: drop `.orbstack` on Linux.

## 4. GitHub test org and App

Never use a dummy user account (a previous one was banned) and never use forks.

Agents can see the org name, slot repo names (in `origin`'s URL), the App's bot account and
the Dagger Cloud org, so give them neutral names that don't mention an eval, a harness or
a test (e.g. org `acme-labs`, slots `project-01`). The referee redacts the patterns in the
rubric's redaction list, but the agent itself is not shielded.

1. Create a test org owned by your real account.
2. Create a GitHub App owned by the org. It needs no webhook. Repository permissions:
   - Administration: read & write (create slot/mirror repos, disable Actions)
   - Contents: read & write
   - Pull requests: read & write
   - Workflows: read & write
   - Checks: read, Commit statuses: read, Metadata: read
3. Install the App on the org for all repositories. Note the App ID and installation ID,
   and download a private key. Uncomment and fill the `[github]` block in `harness.toml`.
4. Add `[[slots]]` entries (`project-01`, `project-02`, ...) and run `harness slots init`. This
   creates private repos and disables Actions on them, because slots carry the upstream
   repo's workflow files.
5. Optional mirrors: `harness github mirror <repo>` creates a private, non-fork `ref-<name>`
   at the pinned SHA. Then set `mirror:` in the corpus entry.

Each run gets an installation token scoped to its slot only. It's revoked in teardown.

## 5. Dagger Cloud

1. Create a Dagger Cloud test org.
2. Install Dagger's GitHub integration on the GitHub test org once, with access to all repos.
   This is the only interactive Cloud Checks step.
3. Create a Cloud token scoped to the test org and export it as `DAGGER_CLOUD_TOKEN`, or
   point `cloud.config_dir` at a dagger config dir logged into that org. Uncomment the
   `[cloud]` block in `harness.toml`.

`--cloud` runs get this login ambiently and the agent is never told about it. `--no-cloud`
runs get no login.

## 6. Before the first real run

- `harness doctor`: everything you need is `ok` (`github app` and `cloud login` can stay
  `XX` for local-only runs).
- `harness parity` diffs a Claude CLI init frame against an SDK one. It's a one-off check;
  nothing reads its output.
- `harness docs sync` loads docs sitemaps for the "never visited" report.
- Smoke run: `harness run --repo zod --bundle baseline --agent claude --no-cloud`, the
  smallest corpus repo.

The corpus entries (pinned SHAs, expected coverage, hand-tagged `out_of_scope` jobs) are
committed, so a new host doesn't capture them again. `harness corpus capture <repo> --write`
is only for adding or re-pinning a repo; it keeps hand edits.

## Moving to another host

Everything in the repo carries over. Past runs live under `state_dir` (`runs/` and
`harness.duckdb`); copy them across to keep them, or start fresh. The rest of `state_dir`
(work trees, caches, the neutral runtime) is rebuilt on demand. Redo steps 2-3 on the new
host; steps 4-5 are per org, not per host, apart from the App's private key file.
