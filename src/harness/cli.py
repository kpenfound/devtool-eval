"""``harness`` CLI."""

from __future__ import annotations

import json
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
app.add_typer(corpus_app, name="corpus")
app.add_typer(slots_app, name="slots")
app.add_typer(store_app, name="store")
app.add_typer(docs_app, name="docs")
app.add_typer(github_app, name="github")

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
    meta = result.meta()
    typer.echo(json.dumps({
        "run_id": meta.run_id,
        "status": meta.status,
        "passed": bool(meta.final_verify and meta.final_verify.passed),
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

    cfg, p = _ctx(config, pack)
    for f in analyze_bundle(_bundle(cfg, run_id), p):
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
        text = rep.build(store, report_facts=facts, repo=repo, bundle=bundle, agent=agent)
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
    c = cfg.agents.claude
    env = c.oauth_token_env if c.auth == "subscription" else c.api_key_env
    check(f"claude auth ({c.auth})", bool(os.environ.get(env)), f"${env}")
    x = cfg.agents.codex
    if x.auth == "subscription":
        check("codex auth (subscription)", Path(os.path.expanduser(x.auth_json)).exists(), x.auth_json)
    else:
        check("codex auth (api_key)", bool(os.environ.get(x.api_key_env)), f"${x.api_key_env}")
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
    from harness.bundle import list_bundles

    cfg = load_config(config)
    for b in list_bundles(cfg.runs):
        m = b.meta()
        passed = m.final_verify.passed if m.final_verify else None
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
    cache = pool.cached_repo(entry)
    token = gh.harness_token([repo])
    sh(["git", "push", "-q", "--force", f"https://github.com/{repo}.git",
        f"refs/pins/{entry.sha}:refs/heads/main"], cwd=cache, env=pool.git_env(token))
    github.Repo(repo, token).disable_actions()
    typer.echo(f"{repo} @ {entry.sha} (set `mirror: {repo}` in the corpus entry to use it)")


if __name__ == "__main__":
    app()
