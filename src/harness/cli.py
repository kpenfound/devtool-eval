"""``harness`` CLI."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from harness.config import load_config
from harness.models import AgentKind, RunSpec

app = typer.Typer(no_args_is_help=True, help="Dev tools onboarding harness.")
corpus_app = typer.Typer(no_args_is_help=True, help="Inspect and capture the corpus.")
slots_app = typer.Typer(no_args_is_help=True, help="Slot pool status and cleanup.")
store_app = typer.Typer(no_args_is_help=True, help="DuckDB store.")
docs_app = typer.Typer(no_args_is_help=True, help="Docs map sources.")
github_app = typer.Typer(no_args_is_help=True, help="GitHub App plumbing.")
sweep_app = typer.Typer(no_args_is_help=True, help="Named, resumable sweeps over the scenario grid.")
app.add_typer(corpus_app, name="corpus")
app.add_typer(slots_app, name="slots")
app.add_typer(store_app, name="store")
app.add_typer(docs_app, name="docs")
app.add_typer(github_app, name="github")
app.add_typer(sweep_app, name="sweep")

ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="harness.toml path")]
PackOpt = Annotated[str | None, typer.Option("--pack", help="Tool pack (default from config)")]


def _ctx(config_path: Path | None, pack_name: str | None = None):
    from harness.pack import load_pack

    config = load_config(config_path)
    name = pack_name or config.default_pack
    return config, load_pack(name, config.pack_settings(name))


def _bundle(config, run_id: str):
    from harness.bundle import find_bundle

    return find_bundle(config.runs, run_id)


@app.command()
def run(
    repo: Annotated[str, typer.Option(help="Corpus repo name, e.g. flask")],
    bundle: Annotated[str, typer.Option(help="Variant bundle, e.g. baseline")],
    agent: Annotated[AgentKind, typer.Option(help="claude or codex")],
    tool_version: Annotated[str | None, typer.Option(help="Pin the tool version (default: installed)")] = None,
    cloud: Annotated[bool, typer.Option("--cloud/--no-cloud", help="Ambient tool-cloud login")] = True,
    retries: Annotated[int | None, typer.Option(help="Scripted retries (default from config)")] = None,
    slot: Annotated[str | None, typer.Option(help="Use this slot")] = None,
    model: Annotated[str | None, typer.Option(help="Agent model override")] = None,
    referee: Annotated[bool, typer.Option("--referee/--no-referee")] = True,
    config: ConfigOpt = None,
    pack: PackOpt = None,
) -> None:
    """Run one scenario end to end."""
    from harness.runner import Runner

    cfg, p = _ctx(config, pack)
    spec = RunSpec(repo=repo, bundle=bundle, agent=agent, pack=p.name, tool_version=tool_version,
                   cloud=cloud, retries=retries, slot=slot, model=model)
    result = Runner(cfg, p, log=lambda m: typer.echo(m, err=True)).run(spec, referee=referee)
    from harness.analysis import passed

    meta = result.meta()
    typer.echo(json.dumps({
        "run_id": meta.run_id,
        "status": meta.status,
        "passed": passed(meta, result.read_json("git_state.json") or {}),
        "bundle": str(result.path),
        "error": (meta.error or "").splitlines()[0] if meta.error else None,
    }, indent=2))
    if meta.status != "completed":
        raise typer.Exit(1)


@app.command()
def normalize(run_id: str, config: ConfigOpt = None) -> None:
    """Re-derive events.jsonl from the raw transcript."""
    cfg = load_config(config)
    events = _bundle(cfg, run_id).renormalize()
    typer.echo(f"{len(events)} events")


@app.command()
def analyze(run_id: str, config: ConfigOpt = None, pack: PackOpt = None) -> None:
    """Re-run deterministic analyzers and the docs map for a run bundle."""
    from harness.analysis import analyze_bundle
    from harness.store import Store

    cfg, p = _ctx(config, pack)
    b = _bundle(cfg, run_id)
    facts = analyze_bundle(b, p)
    with Store(cfg.db) as store:
        store.ingest(b)
    for f in facts:
        typer.echo(f"{f.key}: {json.dumps(f.value, default=str)}")


@app.command()
def referee(
    run_id: str,
    force: Annotated[bool, typer.Option(help="Overwrite an existing scorecard")] = False,
    dry_run: Annotated[bool, typer.Option(help="Print the blinded prompt; don't call the model")] = False,
    config: ConfigOpt = None,
    pack: PackOpt = None,
) -> None:
    """Score a run bundle with the referee."""
    from harness.referee.framework import ScorecardExists, referee_bundle
    from harness.store import Store

    cfg, p = _ctx(config, pack)
    b = _bundle(cfg, run_id)
    try:
        out = referee_bundle(b, p, cfg, force=force, dry_run=dry_run)
    except ScorecardExists as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    if dry_run:
        typer.echo(out)
        return
    with Store(cfg.db) as store:
        store.ingest(b)
    typer.echo(out.model_dump_json(indent=2, exclude={"facts"}))


@app.command()
def report(
    repo: str | None = None,
    bundle: str | None = None,
    agent: str | None = None,
    sweep: Annotated[str | None, typer.Option(help="Only runs from this named sweep")] = None,
    since: Annotated[str | None, typer.Option(help="Only runs started at or after this date/time, e.g. 2026-10-29")] = None,
    output: Annotated[Path | None, typer.Option("-o", "--output")] = None,
    config: ConfigOpt = None,
    pack: PackOpt = None,
) -> None:
    """Markdown report across runs."""
    from harness import report as rep
    from harness.store import Store

    cfg, p = _ctx(config, pack)
    facts = getattr(p, "report_facts", None) or [
        "dagger.module_strategy", "dagger.ci_choice", "integration.cloud_checks.green",
    ]
    with Store(cfg.db) as store:
        text = rep.build(store, report_facts=facts, repo=repo, bundle=bundle, agent=agent, sweep=sweep, since=since)
    if output:
        output.write_text(text)
        typer.echo(str(output))
    else:
        typer.echo(text)


@app.command()
def calibrate(config: ConfigOpt = None) -> None:
    """Compare human-scores.yaml files against referee scorecards."""
    from harness.bundle import list_bundles
    from harness.report import calibration_table
    from harness.store import Store

    cfg = load_config(config)
    with Store(cfg.db) as store:
        for b in list_bundles(cfg.runs):
            if b.human_scores():
                store.ingest(b)
        typer.echo(calibration_table(store))


@app.command()
def parity(
    bundle: str = "baseline",
    repo: Annotated[str | None, typer.Option(help="Corpus repo to run in (default: empty dir)")] = None,
    config: ConfigOpt = None,
    pack: PackOpt = None,
) -> None:
    """Diff a Claude CLI run against an SDK run (init frames)."""
    from harness.parity import claude_parity

    cfg, p = _ctx(config, pack)
    workdir = cfg.state / "parity-work"
    workdir.mkdir(exist_ok=True)
    if repo:
        from harness.slots import SlotPool

        entry = p.corpus()[repo]
        pool = SlotPool(cfg)
        lease = pool.lease("parity")
        try:
            workdir = pool.reset(pool.get(lease.slot.name).model_copy(update={"repo": None}), entry, None)
        finally:
            pool.release(lease)
    out = claude_parity(cfg, p.bundle_dir(bundle), workdir)
    typer.echo(json.dumps(out, indent=2))
    if not out["match"]:
        raise typer.Exit(1)


@app.command()
def doctor(config: ConfigOpt = None, pack: PackOpt = None) -> None:
    """Check that the host is ready for runs."""
    import os
    import shutil

    cfg, p = _ctx(config, pack)
    rows = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        rows.append((name, ok, detail))

    check("state dir", True, str(cfg.state))
    for tool in ("git", "docker", "dagger", "claude", "codex", "gh"):
        check(f"{tool} on PATH", bool(shutil.which(tool)), shutil.which(tool) or "")
    from harness.sweep import agent_auth

    for kind in AgentKind:
        ok, where = agent_auth(cfg, kind)
        check(f"{kind} auth ({getattr(cfg.agents, kind.value).auth})", ok, where)
    check("github app", cfg.github.enabled, "local-only runs" if not cfg.github.enabled else str(cfg.github.app_id))
    cl = cfg.cloud
    check("cloud login", bool((cl.token_env and os.environ.get(cl.token_env)) or cl.config_dir),
          "needed for --cloud runs")
    corpus = p.corpus()
    check("corpus", bool(corpus), ", ".join(f"{n}{'' if e.ready else ' (unpinned)'}" for n, e in corpus.items()))
    check("slots", bool(cfg.slots), ", ".join(f"{s.name}→{s.repo or 'local'}" for s in cfg.slots))
    for name, ok, detail in rows:
        typer.echo(f"{'ok ' if ok else 'XX '} {name:28} {detail}")


@app.command()
def bundles(pack: PackOpt = None, config: ConfigOpt = None) -> None:
    """List variant bundles."""
    from harness.bundle import resolve_variant

    _, p = _ctx(config, pack)
    for name in p.bundles():
        rb = resolve_variant(p.bundle_dir(name))
        typer.echo(f"{name}@{rb.spec.version} {rb.content_hash}  {rb.spec.description}")


@app.command(name="runs")
def list_runs(config: ConfigOpt = None) -> None:
    """List run bundles."""
    from harness.analysis import passed as run_passed
    from harness.bundle import list_bundles

    cfg = load_config(config)
    for b in list_bundles(cfg.runs):
        m = b.meta()
        passed = run_passed(m, b.read_json("git_state.json") or {}) if m.final_verify else None
        typer.echo(f"{m.run_id}  {m.status:9}  passed={passed}  scored={b.scorecard() is not None}")


# --- corpus ---------------------------------------------------------------------------------


@corpus_app.command("list")
def corpus_list(config: ConfigOpt = None, pack: PackOpt = None) -> None:
    _, p = _ctx(config, pack)
    for name, e in p.corpus().items():
        typer.echo(f"{name:18} tier {e.tier:4} {(e.sha or 'UNPINNED')[:12]:12} "
                   f"ci={len(e.ci)} coverage={len(e.expected_coverage)}  {e.tests}")


@corpus_app.command("show")
def corpus_show(name: str, config: ConfigOpt = None, pack: PackOpt = None) -> None:
    _, p = _ctx(config, pack)
    typer.echo(p.corpus()[name].model_dump_json(indent=2))


@corpus_app.command("capture")
def corpus_capture(
    name: str,
    write: Annotated[bool, typer.Option(help="Write results back to the corpus YAML")] = False,
    config: ConfigOpt = None,
    pack: PackOpt = None,
) -> None:
    """Capture CI inventory and expected official coverage at the pinned SHA."""
    from harness.corpus import capture, write_entry

    cfg, p = _ctx(config, pack)
    entry = capture(p.corpus()[name], p, cfg, log=lambda m: typer.echo(m, err=True))
    if write:
        write_entry(p.corpus_path(name), entry)
        typer.echo(f"wrote {p.corpus_path(name)}")
    else:
        typer.echo(entry.model_dump_json(indent=2))


# --- slots ----------------------------------------------------------------------------------


@slots_app.command("status")
def slots_status(config: ConfigOpt = None) -> None:
    from harness.slots import SlotPool

    for s in SlotPool(load_config(config)).status():
        typer.echo(json.dumps(s, default=str))


@slots_app.command("recover")
def slots_recover(config: ConfigOpt = None, pack: PackOpt = None) -> None:
    """Clean up after crashed runs (also done automatically before every run)."""
    from harness.runner import Runner

    cfg, p = _ctx(config, pack)
    cleaned = Runner(cfg, p, log=lambda m: typer.echo(m, err=True)).recover()
    typer.echo(f"recovered {len(cleaned)} run(s)")


@slots_app.command("release")
def slots_release(slot: str, config: ConfigOpt = None) -> None:
    """Drop a lease by hand. Doesn't tear anything down: prefer `recover`."""
    from harness.slots import SlotPool

    typer.echo(json.dumps(SlotPool(load_config(config)).force_release(slot)))


@slots_app.command("init")
def slots_init(config: ConfigOpt = None) -> None:
    """Create slot repos (private) in the test org and disable Actions on them."""
    from harness import github

    cfg = load_config(config)
    gh = github.GitHubApp(cfg.github)
    for s in cfg.slots:
        if not s.repo:
            continue
        token = gh.harness_token()
        org, name = s.repo.split("/", 1)
        github.create_private_repo(org, name, token, "")
        github.Repo(s.repo, gh.harness_token([s.repo])).disable_actions()
        typer.echo(f"{s.repo}: ready (Actions disabled)")


# --- store / docs / github -----------------------------------------------------------------


@store_app.command("rebuild")
def store_rebuild(config: ConfigOpt = None) -> None:
    """Re-ingest every run bundle."""
    from harness.bundle import list_bundles
    from harness.store import Store

    cfg = load_config(config)
    n = 0
    with Store(cfg.db) as store:
        for b in list_bundles(cfg.runs):
            if b.file("analysis.json").exists():
                store.ingest(b)
                n += 1
    typer.echo(f"ingested {n} run(s) into {cfg.db}")


@store_app.command("sql")
def store_sql(sql: str, config: ConfigOpt = None) -> None:
    """Run a SQL query against the store."""
    from harness.store import Store

    with Store(load_config(config).db) as store:
        for row in store.query(sql):
            typer.echo(json.dumps(row, default=str))


@docs_app.command("sync")
def docs_sync(config: ConfigOpt = None, pack: PackOpt = None) -> None:
    """Fetch the pack's docs sitemaps into the store (for never-visited pages)."""
    from harness.docsmap import fetch_sitemap
    from harness.store import Store

    cfg, p = _ctx(config, pack)
    with Store(cfg.db) as store:
        for src in p.docs_sources():
            if src.sitemap:
                urls = fetch_sitemap(src.sitemap)
                store.set_sitemap(src.name, urls)
                typer.echo(f"{src.name}: {len(urls)} pages")


@github_app.command("mirror")
def github_mirror(name: str, config: ConfigOpt = None, pack: PackOpt = None) -> None:
    """Create ref-<name>: a private, non-fork copy of a corpus repo at its pinned SHA."""
    from harness import github
    from harness.isolation import run as sh
    from harness.slots import SlotPool

    cfg, p = _ctx(config, pack)
    entry = p.corpus()[name]
    gh = github.GitHubApp(cfg.github)
    org = cfg.github.org
    if not org:
        raise typer.BadParameter("github.org is not configured")
    repo = f"{org}/ref-{name}"
    github.create_private_repo(org, f"ref-{name}", gh.harness_token(), f"mirror of {entry.url} @ {entry.sha}")
    pool = SlotPool(cfg)
    cache = pool.cached_repo(entry, upstream=True)
    token = gh.harness_token([repo])
    sh(["git", "push", "-q", "--force", f"https://github.com/{repo}.git",
        f"refs/pins/{entry.sha}:refs/heads/main"], cwd=cache, env=pool.git_env(token))
    github.Repo(repo, token).disable_actions()
    typer.echo(f"{repo} @ {entry.sha} (set `github.mirrors = true` in harness.toml to use mirrors)")


if __name__ == "__main__":
    app()


# --- sweep ----------------------------------------------------------------------------------


class CloudMode(StrEnum):
    on = "on"
    off = "off"
    both = "both"


@sweep_app.command("run")
def sweep_run(
    name: Annotated[str, typer.Argument(help="Sweep name; run it again to resume")],
    repo: Annotated[list[str] | None, typer.Option(help="Repeatable (default: every pinned corpus repo)")] = None,
    bundle: Annotated[list[str] | None, typer.Option(help="Repeatable (default: every bundle)")] = None,
    agent: Annotated[list[AgentKind] | None, typer.Option(help="Repeatable (default: every agent)")] = None,
    tool_version: Annotated[list[str] | None, typer.Option(help="Repeatable (default: installed)")] = None,
    cloud: Annotated[CloudMode | None, typer.Option(help="on, off or both (default: on)")] = None,
    reps: Annotated[int | None, typer.Option(help="Runs per cell (default: 1)")] = None,
    model: Annotated[str | None, typer.Option(help="Model for every agent (default: from config)")] = None,
    retries: Annotated[int | None, typer.Option(help="Scripted retries (default from config)")] = None,
    referee: Annotated[bool | None, typer.Option("--referee/--no-referee")] = None,
    max_attempts: Annotated[int, typer.Option(help="Tries per cell on harness failures")] = 2,
    max_consecutive_failures: Annotated[int, typer.Option(help="Stop after this many harness failures in a row")] = 3,
    allow_drift: Annotated[bool, typer.Option(help="Resume even if bundles or the tool version changed")] = False,
    dry_run: Annotated[bool, typer.Option(help="Print the cells and checks, run nothing")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask before creating the sweep")] = False,
    config: ConfigOpt = None,
    pack: PackOpt = None,
) -> None:
    """Run every cell of a grid, saving as it goes. The same command resumes it."""
    from harness.sweep import Sweep, SweepError, build_plan

    cfg, p = _ctx(config, pack)
    log = lambda m: typer.echo(m, err=True)  # noqa: E731
    sw = Sweep(cfg, p, name, log=log)
    grid_flags = any(v is not None for v in (repo, bundle, agent, tool_version, cloud, reps, model,
                                              retries, referee))
    try:
        new = None
        if grid_flags or not sw.exists:
            modes = {"on": [True], "off": [False], "both": [True, False]}[(cloud or CloudMode.on).value]
            new = build_plan(cfg, p, name, repos=repo, bundles=bundle, agents=agent,
                             tool_versions=tool_version, cloud=modes, reps=reps or 1, model=model,
                             retries=retries, referee=True if referee is None else referee)
        if sw.exists:
            plan = sw.load()
            if new and new.grid() != plan.grid():
                raise SweepError(f"sweep {name} exists with a different grid; run "
                                 f"`harness sweep run {name}` with no grid flags to resume it, "
                                 "or pick a new name")
            creating = False
        else:
            plan, creating = new, True

        done = sum(s.state != "pending" for s in sw.status(plan, max_attempts=max_attempts))
        typer.echo(f"sweep {name}: {len(plan.cells)} cells, {done} finished, "
                   f"{len(plan.cells) - done} to run", err=True)
        blocking = sw.preflight(plan)
        drift = [] if creating else sw.drift(plan)
        for prob in blocking + drift:
            typer.echo(f"XX {prob}", err=True)
        if drift and not allow_drift:
            typer.echo("(--allow-drift resumes anyway)", err=True)
            blocking += drift
        if dry_run:
            for c in plan.cells:
                typer.echo(c.key)
            raise typer.Exit(1 if blocking else 0)
        if blocking:
            raise typer.Exit(1)
        if creating:
            if not yes:
                typer.confirm(f"create sweep {name} and start {len(plan.cells)} runs?", abort=True)
            sw.save(plan)
        final = sw.run(plan, max_attempts=max_attempts, allow_drift=allow_drift,
                       max_consecutive_failures=max_consecutive_failures)
    except SweepError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        typer.echo(f"\ninterrupted; resume with `harness sweep run {name}`", err=True)
        raise typer.Exit(130) from None
    _sweep_summary(final)
    if any(s.state != "done" for s in final):
        raise typer.Exit(1)


def _sweep_summary(statuses) -> None:
    from collections import Counter

    counts = Counter(s.state.value for s in statuses)
    typer.echo(", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


@sweep_app.command("status")
def sweep_status(
    name: str,
    max_attempts: Annotated[int, typer.Option(help="Tries per cell on harness failures")] = 2,
    config: ConfigOpt = None,
    pack: PackOpt = None,
) -> None:
    """Each cell's state and runs."""
    from harness.analysis import passed as run_passed
    from harness.bundle import find_bundle
    from harness.sweep import Sweep, SweepError

    cfg, p = _ctx(config, pack)
    sw = Sweep(cfg, p, name)
    try:
        plan = sw.load()
    except SweepError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    statuses = sw.status(plan, max_attempts=max_attempts)
    width = max(len(s.cell.key) for s in statuses)
    for s in statuses:
        last = s.runs[-1] if s.runs else None
        passed = None
        if last and last.status == "completed":
            b = find_bundle(cfg.runs, last.run_id)
            passed = run_passed(last, b.read_json("git_state.json") or {})
        typer.echo(f"{s.cell.key:{width}}  {s.state:8}  runs={len(s.runs)}  "
                   f"passed={'-' if passed is None else passed}  {last.run_id if last else ''}")
    for prob in sw.drift(plan):
        typer.echo(f"XX {prob}", err=True)
    _sweep_summary(statuses)


@sweep_app.command("list")
def sweep_list(config: ConfigOpt = None) -> None:
    """Named sweeps."""
    from harness.sweep import list_sweeps

    for name in list_sweeps(load_config(config)):
        typer.echo(name)
