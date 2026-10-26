## Dagger 1.0 in brief (referee context)

- A **workspace** is a repo with a `dagger.toml` at its root. It records installed modules
  (`[modules.<name>] source = "..."`), module settings, and named env overlays. It is the
  source of truth; `dagger workspace config` and `dagger settings` edit it.
- **Official modules** come from `dagger.io/...` (vanity URLs for `github.com/dagger/...`),
  e.g. `dagger.io/python/pytest`, `dagger.io/python/ruff`, `dagger.io/python/mypy`,
  `dagger.io/python/uv`. `dagger module recommend` lists the official modules that fit a repo;
  the corpus entry's "expected official coverage" was captured from it.
- Dagger is both a local dev tool and a CI tool: `dagger check` runs the same checks on a
  laptop and in CI. Covering a dev tool the repo uses but its CI never ran (the corpus
  entry's "bonus coverage", e.g. a formatter only run by hand) brings that check into CI,
  and is credit, not overreach. Missing bonus coverage is not a gap.
- **Custom modules** live in the repo, by default under `.dagger/modules/<name>`, created with
  `dagger module init <sdk>`. A custom module that wraps or composes an official module is
  fine; one that reimplements what an official module already does is overlap.
- **Checks** are module functions annotated as checks. `dagger check` runs every check in the
  workspace (`dagger check -l` lists them). **Generators** produce files (`dagger generate`).
- **`dagger.lock`** pins images and git refs; it should be committed alongside `dagger.toml`.
- **Cloud Checks** run the workspace's checks on Dagger Cloud for each commit, enabled with
  `dagger cloud checks on`. Cloud Checks do not do CD (publishing, releases, deploys), and
  only run Linux workloads; those CI jobs are out of scope and are not gaps.
- **Legacy (pre-1.0) patterns:** a `dagger.json` at the repo root holding module
  dependencies and workspace settings, `dagger call` pipelines wired into CI YAML, `dagger
  develop`, and docs from older versions. `dagger workspace migrate` converts legacy config.
- Running services (databases etc.) should use Dagger services (`asService`, service
  bindings), not docker-in-docker.
