"""The run flow: setup, prompt, execute, verify, retry, feedback, integration check, store.

One run is one scenario (repo + variant bundle + agent + tool version + cloud on/off).
Teardown always happens in ``finally``; a run whose process died is cleaned up by
``recover()`` at the start of the next invocation.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import socket
import traceback
from dataclasses import dataclass
from pathlib import Path

from harness import __version__, github
from harness.agents.base import AgentInvocation, get_adapter, run_invocation
from harness.analysis import analyze_bundle
from harness.bundle import (
    DIFF,
    FEEDBACK,
    SNAPSHOT,
    RunBundle,
    find_bundle,
    new_run_id,
    resolve_variant,
)
from harness.config import HarnessConfig
from harness.isolation import base_env, git_credential_env, make_home, neutral_python, run
from harness.models import (
    AgentTurnResult,
    Budget,
    FeedbackReport,
    Nudge,
    Phase,
    RunMeta,
    RunSpec,
    RunStatus,
    utcnow,
)
from harness.pack import RunContext, ToolPack
from harness.schema import drop_nulls, strict
from harness.slots import Lease, SlotPool

RETRY_ERROR_LINES = 80


@dataclass
class _Used:
    seconds: float = 0.0
    turns: int = 0
    cost: float = 0.0

    def add(self, r: AgentTurnResult) -> None:
        self.seconds += r.duration_s
        self.turns += r.turns
        self.cost += r.cost_usd or 0.0

    def remaining(self, b: Budget) -> Budget | None:
        left = Budget(
            wall_clock_s=int(b.wall_clock_s - self.seconds),
            max_turns=b.max_turns - self.turns,
            max_cost_usd=b.max_cost_usd - self.cost,
        )
        if left.wall_clock_s <= 30 or left.max_turns <= 0 or left.max_cost_usd <= 0:
            return None
        return left


class Runner:
    def __init__(self, config: HarnessConfig, pack: ToolPack, log=print):
        self.config = config
        self.pack = pack
        self.pool = SlotPool(config)
        self.log = log
        self.app = github.GitHubApp(config.github) if config.github.enabled else None

    # --- crash recovery -------------------------------------------------------------------

    def recover(self) -> list[str]:
        cleaned = []
        for slot, lease_data in self.pool.stale_leases():
            self._recover_one(slot, lease_data)
            cleaned.append(lease_data.get("run_id", "?"))
        return cleaned

    def _recover_one(self, slot, lease_data: dict) -> None:
        run_id = lease_data.get("run_id", "?")
        self.log(f"recovering crashed run {run_id} on {slot.name}")
        try:
            bundle = find_bundle(self.config.runs, run_id)
            meta = bundle.meta()
            if meta.status == RunStatus.running:
                meta.status = RunStatus.crashed
                meta.finished_at = utcnow()
                bundle.write_meta(meta)
            entry = self.pack.corpus().get(meta.spec.repo)
        except (FileNotFoundError, ValueError):
            bundle, entry = None, None
        token = self._harness_token(slot.repo)
        if entry is not None:
            ctx = self._bare_ctx(bundle, slot, entry, token)
            for integ in self.pack.integrations:
                _quietly(lambda integ=integ: integ.teardown(ctx), self.log)
            _quietly(lambda: self.pool.teardown(slot, entry, token), self.log)
        if lease_data.get("agent_token"):
            _quietly(lambda: github.revoke(lease_data["agent_token"]), self.log)
        if token:
            _quietly(lambda: github.revoke(token), self.log)
        self.pool.force_release(slot.name)

    def _harness_token(self, repo: str | None) -> str | None:
        if not (self.app and repo):
            return None
        return self.app.harness_token([repo])

    def _bare_ctx(self, bundle, slot, entry, token) -> RunContext:
        run_dir = bundle.path if bundle else self.config.state / "recovery"
        run_dir.mkdir(parents=True, exist_ok=True)
        meta = bundle.meta() if bundle else RunMeta(run_id="recovery", spec=RunSpec(repo=entry.name, bundle="?", agent="claude"))
        home = make_home(Path(meta.home).parent if meta.home else run_dir, self.config)
        gh = self.config.github
        ctx = RunContext(
            run_id=meta.run_id,
            run_dir=run_dir,
            workdir=self.pool.workdir(slot, entry),
            home=home,
            entry=entry,
            meta=meta,
            config=self.config,
            env=base_env(home, self.config, git_name=gh.bot_name, git_email=gh.bot_email),
            slot_repo=slot.repo,
            harness_token=token,
        )
        ctx.env.update(self.pack.environment.prepare(ctx))
        return ctx

    # --- the run --------------------------------------------------------------------------

    def run(self, spec: RunSpec, *, referee: bool = True) -> RunBundle:
        self.recover()
        corpus = self.pack.corpus()
        if spec.repo not in corpus:
            raise KeyError(f"repo {spec.repo!r} is not in the {self.pack.name} corpus")
        entry = corpus[spec.repo]
        if not entry.ready:
            raise ValueError(f"corpus entry {entry.name} has no pinned SHA yet")
        variant = resolve_variant(self.pack.bundle_dir(spec.bundle))
        retries = self.config.retries if spec.retries is None else spec.retries

        meta = RunMeta(
            run_id=new_run_id(spec),
            spec=spec,
            repo_sha=entry.sha,
            bundle_version=variant.spec.version,
            bundle_hash=variant.content_hash,
            budget=self.config.budget,
            host=socket.gethostname(),
        )
        bundle = RunBundle.create(self.config.runs, meta)
        bundle.write_json("variant.json", variant.model_dump())
        self.log(f"run {meta.run_id}")
        try:
            lease = self.pool.lease(meta.run_id, spec.slot)
        except BaseException as e:
            meta.status, meta.error, meta.finished_at = RunStatus.failed, str(e), utcnow()
            bundle.write_meta(meta)
            raise
        meta.slot = lease.slot.name
        harness_token: str | None = None
        ctx: RunContext | None = None
        try:
            harness_token = self._harness_token(lease.slot.repo)
            ctx = self._setup(bundle, meta, lease, entry, variant, harness_token)
            bundle.write_meta(meta)
            self._execute(bundle, meta, ctx, retries)
            meta.status = RunStatus.completed
        except BaseException as e:  # noqa: BLE001 - recorded, then re-raised for ^C
            meta.status = RunStatus.failed
            meta.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            self.log(f"run failed: {type(e).__name__}: {e}")
            if isinstance(e, KeyboardInterrupt):
                raise
        finally:
            meta.finished_at = utcnow()
            bundle.write_meta(meta)
            self._teardown(lease, entry, ctx, harness_token)
        if meta.status == RunStatus.completed:
            try:
                self._store(bundle, referee=referee)
            except Exception as e:  # noqa: BLE001 - the bundle is intact; stages can be re-run
                self.log(f"post-run processing failed: {type(e).__name__}: {e} "
                         f"(re-run `harness analyze {bundle.run_id}`)")
        return bundle

    def _setup(self, bundle, meta, lease: Lease, entry, variant, harness_token) -> RunContext:
        self.log(f"setup: slot {lease.slot.name}, {entry.name}@{entry.sha[:12]}")
        workdir = self.pool.reset(lease.slot, entry, harness_token)
        home = make_home(self.config.state / "homes" / secrets.token_hex(6), self.config)
        meta.home = str(home)
        gh = self.config.github
        env = base_env(home, self.config, git_name=gh.bot_name, git_email=gh.bot_email)
        ctx = RunContext(
            run_id=meta.run_id,
            run_dir=bundle.path,
            workdir=workdir,
            home=home,
            entry=entry,
            meta=meta,
            config=self.config,
            env=env,
            slot_repo=lease.slot.repo,
            harness_token=harness_token,
        )
        install = self.pack.environment.install(meta.spec.tool_version, ctx)
        if install.path_prepend:
            env["PATH"] = os.pathsep.join([*install.path_prepend, env["PATH"]])
        env.update(install.env)
        if self.app and lease.slot.repo:
            token = self.app.slot_token(lease.slot.repo)
            lease.record_token(token)
            env.update(git_credential_env(token))
        env.update(self.pack.environment.prepare(ctx))
        self.pack.environment.reset(ctx)

        adapter = get_adapter(meta.spec.agent)
        agent_cfg = getattr(self.config.agents, meta.spec.agent.value)
        adapter.setup_home(home, variant, agent_cfg)
        self._agent_env = {**env, **adapter.auth_env(home, agent_cfg)}
        self._agent_cfg = agent_cfg.model_dump()
        self._mcp = variant.spec.mcp
        meta.versions = {
            "harness": __version__,
            f"pack.{self.pack.name}": self.pack.version,
            "tool": install.version,
            "python": platform.python_version(),
            **adapter.versions(),
        }
        return ctx

    def _invocation(self, ctx: RunContext, phase: Phase, attempt: int, prompt: str, budget: Budget,
                    resume: str | None, schema: dict | None = None) -> AgentInvocation:
        return AgentInvocation(
            agent=ctx.meta.spec.agent,
            phase=phase,
            attempt=attempt,
            prompt=prompt,
            resume_session=resume,
            workdir=str(ctx.workdir),
            home=str(ctx.home),
            run_dir=str(ctx.run_dir),
            env=self._agent_env,
            budget=budget,
            model=ctx.meta.spec.model,
            agent_config=self._agent_cfg,
            mcp_servers=self._mcp,
            output_schema=schema,
            python=neutral_python(self.config.state),
        )

    def _execute(self, bundle: RunBundle, meta: RunMeta, ctx: RunContext, retries: int) -> None:
        used = _Used()
        self.log("execute: task prompt")
        res = run_invocation(
            self._invocation(ctx, Phase.task, 0, self.pack.prompt("task"), meta.budget, None)
        )
        self._record(bundle, meta, res, used)

        self.log("verify")
        v = self.pack.verifier.verify(ctx, attempt=0)
        meta.verifies.append(v)
        bundle.write_meta(meta)
        for attempt in range(1, retries + 1):
            if v.passed:
                break
            left = used.remaining(meta.budget)
            if left is None or not meta.session_id:
                self.log("retry skipped: budget exhausted or no session to resume")
                break
            message = self.pack.prompt("retry").format(error=v.error_excerpt)
            meta.nudges.append(Nudge(attempt=attempt, message=message, verify_exit_code=v.exit_code))
            self.log(f"retry {attempt}/{retries}")
            res = run_invocation(
                self._invocation(ctx, Phase.retry, attempt, message, left, meta.session_id)
            )
            self._record(bundle, meta, res, used)
            v = self.pack.verifier.verify(ctx, attempt=attempt)
            meta.verifies.append(v)
            bundle.write_meta(meta)

        # The diff and final tree are captured before the feedback turn, which must not
        # change the work; any changes it does make are recorded separately.
        before = capture_diff(ctx.workdir, meta.repo_sha, bundle.path)
        bundle.file(DIFF).write_text(before)
        snapshot(ctx.workdir, bundle.file(SNAPSHOT), self.pack.snapshot_paths())
        bundle.write_json("git_state.json", git_state(ctx.workdir))

        if meta.session_id:
            self.log("feedback turn")
            fb = run_invocation(
                self._invocation(
                    ctx, Phase.feedback, 0, self.pack.prompt("feedback"),
                    self.config.feedback_budget, meta.session_id, feedback_schema(),
                )
            )
            meta.turns.append(fb)
            bundle.write_model(FEEDBACK, parse_feedback(fb.final_text))
            after = capture_diff(ctx.workdir, meta.repo_sha, bundle.path)
            if after != before:
                bundle.file("diff.feedback-turn.patch").write_text(after)
        bundle.write_meta(meta)

        self.log("integration check")
        heads = self.pool.pushed_shas(self.pool.get(meta.slot), ctx.entry, ctx.harness_token)
        meta.pushed_shas = list(dict.fromkeys(heads.values()))
        bundle.write_json("pushed.json", heads)
        for integ in self.pack.integrations:
            try:
                meta.integration.append(integ.verify(ctx, meta.pushed_shas))
            except Exception as e:  # noqa: BLE001
                from harness.models import IntegrationResult

                meta.integration.append(IntegrationResult(name=integ.name, error=str(e)))
        bundle.write_meta(meta)

    def _record(self, bundle: RunBundle, meta: RunMeta, res: AgentTurnResult, used: _Used) -> None:
        meta.turns.append(res)
        used.add(res)
        meta.session_id = res.session_id or meta.session_id
        bundle.write_meta(meta)
        self.log(
            f"  {res.phase}{res.attempt or ''}: {res.status} turns={res.turns} "
            f"cost={res.cost_usd} {res.duration_s:.0f}s"
        )

    def _teardown(self, lease: Lease, entry, ctx: RunContext | None, harness_token) -> None:
        self.log("teardown")
        if ctx is not None:
            for integ in self.pack.integrations:
                _quietly(lambda integ=integ: integ.teardown(ctx), self.log)
            # Agent session files in the fake HOME are kept as raw data; credentials aren't.
            (ctx.home / ".codex" / "auth.json").unlink(missing_ok=True)
        _quietly(lambda: self.pool.teardown(lease.slot, entry, harness_token), self.log)
        if lease.agent_token:
            _quietly(lambda: github.revoke(lease.agent_token), self.log)
        if harness_token:
            _quietly(lambda: github.revoke(harness_token), self.log)
        self.pool.release(lease)

    def _store(self, bundle: RunBundle, *, referee: bool) -> None:
        self.log("store: normalize, analyze, docs map")
        bundle.renormalize()
        analyze_bundle(bundle, self.pack)
        if referee:
            from harness.referee.framework import referee_bundle

            self.log("referee")
            try:
                referee_bundle(bundle, self.pack, self.config)
            except Exception as e:  # noqa: BLE001 - the run itself is still good
                self.log(f"referee failed: {e} (re-run with `harness referee {bundle.run_id}`)")
        from harness.store import Store

        with Store(self.config.db) as store:
            store.ingest(bundle)


# --- helpers ---------------------------------------------------------------------------------


def capture_diff(workdir: Path, base: str, scratch: Path) -> str:
    """Committed and uncommitted changes (including untracked files) relative to ``base``."""
    index = scratch / ".diff-index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    run(["git", "read-tree", "HEAD"], cwd=workdir, env=env, check=False)
    run(["git", "add", "-A"], cwd=workdir, env=env, check=False)
    out = run(["git", "diff", "--cached", "--no-color", base], cwd=workdir, env=env, check=False)
    index.unlink(missing_ok=True)
    return out.stdout


def git_state(workdir: Path) -> dict:
    def g(*args: str) -> str:
        return run(["git", *args], cwd=workdir, check=False).stdout.strip()

    return {
        "head": g("rev-parse", "HEAD"),
        "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
        "status": g("status", "--porcelain"),
        "log": g("log", "--oneline", "-20"),
        "tracked": g("ls-files").splitlines(),
    }


MAX_SNAPSHOT_BYTES = 256_000
MAX_SNAPSHOT_FILES = 400
# Generated or vendored code: noise for the referee, and potentially huge.
SNAPSHOT_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "sdk", "internal", "vendor"}
SNAPSHOT_SKIP_FILES = {"dagger.gen.go", "go.sum", "package-lock.json", "uv.lock", "yarn.lock"}


def snapshot(workdir: Path, dest: Path, patterns: list[str]) -> None:
    copied = 0
    for pattern in patterns:
        for p in sorted(workdir.glob(pattern)):
            rel = p.relative_to(workdir)
            if not p.is_file() or SNAPSHOT_SKIP_DIRS & set(rel.parts[:-1]) or p.name in SNAPSHOT_SKIP_FILES:
                continue
            if p.stat().st_size > MAX_SNAPSHOT_BYTES or copied >= MAX_SNAPSHOT_FILES:
                continue
            copied += 1
            target = dest / p.relative_to(workdir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)


def feedback_schema() -> dict:
    schema = FeedbackReport.model_json_schema()
    for k in ("raw_text", "parse_error"):
        schema["properties"].pop(k, None)
    return strict(schema)


def parse_feedback(text: str | None) -> FeedbackReport:
    if not text:
        return FeedbackReport(parse_error="no feedback text")
    candidate = text.strip()
    if "```" in candidate:
        parts = candidate.split("```")
        blocks = [p.removeprefix("json").strip() for p in parts[1::2]]
        candidate = next((b for b in blocks if b.startswith("{")), candidate)
    start, end = candidate.find("{"), candidate.rfind("}")
    try:
        data = drop_nulls(json.loads(candidate[start : end + 1]))
        report = FeedbackReport.model_validate(data)
        report.raw_text = text
        return report
    except Exception as e:  # noqa: BLE001
        return FeedbackReport(raw_text=text, parse_error=str(e))


def _quietly(fn, log) -> None:
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - teardown must keep going
        log(f"  teardown step failed: {type(e).__name__}: {e}")
