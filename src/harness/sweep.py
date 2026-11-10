"""Named sweeps: every cell of a scenario grid, run one at a time, resumable.

``harness sweep run NAME [grid flags]`` writes ``<state>/sweeps/NAME/plan.json`` the first
time and then runs each pending cell. Running the same name again resumes it.

Progress is not kept in the plan. Every run is tagged with its sweep and cell
(``RunSpec.sweep`` / ``RunSpec.sweep_cell``), so the run bundles are the record: a crash
can't lose or corrupt it, and ``harness store rebuild`` brings the tags back too.

What counts as done: a ``completed`` run, whether or not the agent passed (a failed task is
a result). A ``failed`` or ``crashed`` run is a harness problem; it is retried until it has
had ``max_attempts`` tries, and then the cell counts as given up.

The plan freezes what has to stay the same for cells to be comparable: the model per agent,
retries, referee on/off, and each variant bundle's version and content hash. A resume stops
when a bundle's hash has changed, or when cells that use the installed tool version (no
``--tool-version``) have run on different versions. ``--allow-drift`` continues anyway.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from itertools import product
from pathlib import Path

from pydantic import BaseModel, Field

from harness.bundle import list_bundles, resolve_variant
from harness.config import HarnessConfig
from harness.models import AgentKind, RunMeta, RunSpec, RunStatus, utcnow
from harness.pack import ToolPack
from harness.slots import SlotPool, _pid_alive

HARNESS_FAILURES = (RunStatus.failed, RunStatus.crashed, RunStatus.running)


class SweepError(Exception):
    pass


class Cell(BaseModel):
    agent: AgentKind
    repo: str
    bundle: str
    tool_version: str | None = None  # None: whatever is installed
    cloud: bool = True
    rep: int = 1

    @property
    def key(self) -> str:
        return "/".join([self.agent.value, self.repo, self.bundle, self.tool_version or "installed",
                         "cloud" if self.cloud else "local", f"r{self.rep}"])

    def spec(self, plan: SweepPlan) -> RunSpec:
        return RunSpec(repo=self.repo, bundle=self.bundle, agent=self.agent, pack=plan.pack,
                       tool_version=self.tool_version, cloud=self.cloud, retries=plan.retries,
                       model=plan.models.get(self.agent.value), sweep=plan.name, sweep_cell=self.key)


class BundlePin(BaseModel):
    version: str
    hash: str


class SweepPlan(BaseModel):
    name: str
    pack: str
    created_at: datetime = Field(default_factory=utcnow)
    cells: list[Cell]
    models: dict[str, str | None]  # agent -> model, frozen from config/--model at creation
    bundles: dict[str, BundlePin]
    retries: int
    referee: bool = True

    def grid(self) -> dict:
        """Everything that defines the sweep, for comparing against flags given on a resume."""
        return self.model_dump(mode="json", exclude={"created_at"})


class CellState(StrEnum):
    pending = "pending"
    done = "done"
    gave_up = "gave_up"


@dataclass
class CellStatus:
    cell: Cell
    state: CellState
    runs: list[RunMeta] = field(default_factory=list)

    @property
    def harness_failures(self) -> int:
        return sum(r.status in HARNESS_FAILURES for r in self.runs)


def build_plan(config: HarnessConfig, pack: ToolPack, name: str, *, repos: list[str] | None,
               bundles: list[str] | None, agents: list[AgentKind] | None,
               tool_versions: list[str] | None, cloud: list[bool], reps: int,
               model: str | None = None, retries: int | None = None,
               referee: bool = True) -> SweepPlan:
    """Expand the grid. Omitted dimensions mean all of them: every pinned corpus repo, every
    bundle, every agent. Reps are the outer loop, so a partial sweep covers the whole grid
    once before it starts repeating, and bundles are the inner loop, so the variants being
    compared run close together in time."""
    if reps < 1:
        raise SweepError("--reps must be at least 1")
    corpus = pack.corpus()
    repos = repos or [n for n, e in corpus.items() if e.ready]
    bundles = bundles or pack.bundles()
    agents = agents or list(AgentKind)
    versions: list[str | None] = list(tool_versions) if tool_versions else [None]
    cells = [
        Cell(agent=a, repo=r, bundle=b, tool_version=v, cloud=c, rep=rep)
        for rep, v, c, a, r, b in product(range(1, reps + 1), versions, cloud, agents, repos, bundles)
    ]
    pins = {}
    for b in bundles:
        rb = resolve_variant(pack.bundle_dir(b))
        pins[b] = BundlePin(version=rb.spec.version, hash=rb.content_hash)
    models = {a.value: model or getattr(config.agents, a.value).model for a in agents}
    return SweepPlan(name=name, pack=pack.name, cells=cells, models=models, bundles=pins,
                     retries=config.retries if retries is None else retries, referee=referee)


def agent_auth(config: HarnessConfig, agent: AgentKind) -> tuple[bool, str]:
    """Whether the agent's configured auth is present, and where it was looked for."""
    if agent == AgentKind.claude:
        c = config.agents.claude
        env = c.oauth_token_env if c.auth == "subscription" else c.api_key_env
        return bool(os.environ.get(env)), f"${env}"
    x = config.agents.codex
    if x.auth == "subscription":
        return Path(os.path.expanduser(x.auth_json)).exists(), x.auth_json
    return bool(os.environ.get(x.api_key_env)), f"${x.api_key_env}"


class Sweep:
    def __init__(self, config: HarnessConfig, pack: ToolPack, name: str, log: Callable[[str], None] = print):
        if not name or "/" in name or name.startswith("."):
            raise SweepError(f"invalid sweep name {name!r}")
        self.config, self.pack, self.name, self.log = config, pack, name, log
        self.dir = config.state / "sweeps" / name
        self.plan_path = self.dir / "plan.json"
        # Cells whose run failed before it got a run bundle, so the bundles don't count them.
        self._unrecorded: dict[str, int] = defaultdict(int)

    # --- plan -------------------------------------------------------------------------------

    @property
    def exists(self) -> bool:
        return self.plan_path.exists()

    def load(self) -> SweepPlan:
        if not self.exists:
            raise SweepError(f"no sweep named {self.name!r}; give the grid flags to create it")
        return SweepPlan.model_validate_json(self.plan_path.read_text())

    def save(self, plan: SweepPlan) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.plan_path.with_suffix(".json.tmp")
        tmp.write_text(plan.model_dump_json(indent=2))
        tmp.replace(self.plan_path)

    # --- state ------------------------------------------------------------------------------

    def runs(self) -> dict[str, list[RunMeta]]:
        by_cell: dict[str, list[RunMeta]] = defaultdict(list)
        for b in list_bundles(self.config.runs):
            try:
                meta = b.meta()
            except Exception:  # noqa: BLE001 - one unreadable bundle shouldn't stop the sweep
                continue
            if meta.spec.sweep == self.name and meta.spec.sweep_cell:
                by_cell[meta.spec.sweep_cell].append(meta)
        for metas in by_cell.values():
            metas.sort(key=lambda m: m.started_at)
        return by_cell

    def status(self, plan: SweepPlan, *, max_attempts: int = 2) -> list[CellStatus]:
        runs = self.runs()
        out = []
        for cell in plan.cells:
            metas = runs.get(cell.key, [])
            st = CellStatus(cell, CellState.pending, metas)
            if any(m.status == RunStatus.completed for m in metas):
                st.state = CellState.done
            elif st.harness_failures + self._unrecorded[cell.key] >= max_attempts:
                st.state = CellState.gave_up
            out.append(st)
        return out

    # --- checks -----------------------------------------------------------------------------

    def preflight(self, plan: SweepPlan) -> list[str]:
        """Problems that would fail cells later, found before the first run starts."""
        problems = []
        corpus = self.pack.corpus()
        for repo in sorted({c.repo for c in plan.cells}):
            if repo not in corpus:
                problems.append(f"repo {repo!r} is not in the {self.pack.name} corpus")
            elif not corpus[repo].ready:
                problems.append(f"repo {repo!r} has no pinned SHA yet")
        for b in sorted({c.bundle for c in plan.cells}):
            try:
                resolve_variant(self.pack.bundle_dir(b))
            except FileNotFoundError as e:
                problems.append(str(e))
        pool = SlotPool(self.config)
        for cloud in sorted({c.cloud for c in plan.cells}):
            try:
                pool.candidates(remote_only=cloud)
            except ValueError as e:
                problems.append(str(e))
        for agent in sorted({c.agent for c in plan.cells}):
            ok, where = agent_auth(self.config, agent)
            if not ok:
                problems.append(f"{agent} auth not found ({where})")
        return problems

    def drift(self, plan: SweepPlan, runs: dict[str, list[RunMeta]] | None = None) -> list[str]:
        """Ways the sweep's conditions have changed since it was planned."""
        problems = []
        for name, pin in plan.bundles.items():
            try:
                rb = resolve_variant(self.pack.bundle_dir(name))
            except FileNotFoundError:
                problems.append(f"bundle {name} no longer exists")
                continue
            if rb.content_hash != pin.hash:
                problems.append(f"bundle {name} changed: planned {pin.version}/{pin.hash[:12]}, "
                                f"now {rb.spec.version}/{rb.content_hash[:12]}")
        runs = self.runs() if runs is None else runs
        installed = {c.key for c in plan.cells if c.tool_version is None}
        seen = sorted({m.versions["tool"] for k, ms in runs.items() if k in installed
                       for m in ms if m.status == RunStatus.completed and m.versions.get("tool")})
        if len(seen) > 1:
            problems.append(f"installed tool version changed during the sweep: {', '.join(seen)} "
                            "(pin it with --tool-version)")
        return problems

    # --- running ----------------------------------------------------------------------------

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / "lock"
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                break
            except FileExistsError:
                try:
                    pid = json.loads(path.read_text()).get("pid", -1)
                except (OSError, ValueError):
                    pid = -1
                if _pid_alive(pid):
                    raise SweepError(f"sweep {self.name} is already running (pid {pid})") from None
                path.unlink(missing_ok=True)  # stale: its process is gone
        else:
            raise SweepError(f"could not lock sweep {self.name}")
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "started_at": utcnow().isoformat()}, f)
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    def run(self, plan: SweepPlan, *, max_attempts: int = 2, allow_drift: bool = False,
            max_consecutive_failures: int = 3) -> list[CellStatus]:
        """Run every pending cell, retrying harness failures in later passes until each cell
        is done or out of attempts. Returns the final status."""
        from harness.runner import Runner

        with self.lock():
            runner = Runner(self.config, self.pack, log=self.log)
            runner.recover()  # marks runs that died mid-flight as crashed, so they count
            consecutive = 0
            while True:
                pending = [s for s in self.status(plan, max_attempts=max_attempts)
                           if s.state == CellState.pending]
                if not pending:
                    break
                for i, st in enumerate(pending, 1):
                    if not allow_drift and (problems := self.drift(plan)):
                        raise SweepError("sweep conditions drifted (use --allow-drift to continue):\n  "
                                         + "\n  ".join(problems))
                    attempt = st.harness_failures + self._unrecorded[st.cell.key] + 1
                    self.log(f"[sweep {self.name}] {i}/{len(pending)} {st.cell.key}"
                             + (f" (attempt {attempt}/{max_attempts})" if attempt > 1 else ""))
                    ok = self._run_cell(runner, plan, st.cell)
                    consecutive = 0 if ok else consecutive + 1
                    if consecutive >= max_consecutive_failures:
                        raise SweepError(f"{consecutive} harness failures in a row; stopping. "
                                         f"Fix the cause and resume with `harness sweep run {self.name}`.")
            return self.status(plan, max_attempts=max_attempts)

    def _run_cell(self, runner, plan: SweepPlan, cell: Cell) -> bool:
        before = len(self.runs().get(cell.key, []))
        try:
            bundle = runner.run(cell.spec(plan), referee=plan.referee)
        except Exception as e:  # noqa: BLE001 - one cell's failure shouldn't end the sweep
            self.log(f"[sweep {self.name}] {cell.key}: {type(e).__name__}: {e}")
            if len(self.runs().get(cell.key, [])) == before:
                self._unrecorded[cell.key] += 1
            return False
        meta = bundle.meta()
        self.log(f"[sweep {self.name}] {cell.key}: {meta.status} ({meta.run_id})")
        return meta.status == RunStatus.completed


def list_sweeps(config: HarnessConfig) -> list[str]:
    root = config.state / "sweeps"
    return sorted(p.parent.name for p in root.glob("*/plan.json")) if root.exists() else []
