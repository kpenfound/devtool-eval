"""Named sweeps: grid expansion, incremental resume, retries, drift and locking."""

from __future__ import annotations

import json
import os
import shutil

import pytest
from test_runner_e2e import StubEnv, StubVerifier, fake_invocation, upstream  # noqa: F401

from harness import runner as runner_mod
from harness.bundle import RunBundle, list_bundles
from harness.config import HarnessConfig
from harness.models import AgentKind, RunMeta, RunStatus
from harness.store import Store
from harness.sweep import Sweep, SweepError, build_plan
from harness_dagger.pack import DaggerPack


@pytest.fixture
def setup(tmp_path, upstream, monkeypatch):  # noqa: F811
    config = HarnessConfig(state_dir=str(tmp_path / "state"), retries=0)
    pack = DaggerPack({})
    pack.environment = StubEnv()
    pack.verifier = StubVerifier()
    monkeypatch.setattr(pack, "corpus", lambda: {"toy": upstream})
    monkeypatch.setattr(runner_mod, "run_invocation", fake_invocation)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    return config, pack


def plan_for(config, pack, name="s1", **kw):
    args = dict(repos=None, bundles=["baseline", "docs-pointers"], agents=[AgentKind.claude],
                tool_versions=None, cloud=[False], reps=1, referee=False)
    return build_plan(config, pack, name, **{**args, **kw})


def test_grid_order_and_defaults(setup):
    config, pack = setup
    plan = plan_for(config, pack, bundles=None, agents=None, reps=2)
    keys = [c.key for c in plan.cells]
    assert len(keys) == 2 * len(pack.bundles()) * len(AgentKind)
    assert len(set(keys)) == len(keys)
    # reps are the outer loop: the whole grid once before any repeat
    assert all(k.endswith("/r1") for k in keys[: len(keys) // 2])
    assert keys[0].startswith("claude/toy/") and keys[0].endswith("/installed/local/r1")
    assert plan.bundles["baseline"].hash and plan.retries == 0


def test_runs_every_cell_then_resume_is_a_noop(setup):
    config, pack = setup
    sw = Sweep(config, pack, "s1", log=lambda m: None)
    plan = plan_for(config, pack)
    assert sw.preflight(plan) == []
    sw.save(plan)
    final = sw.run(plan)
    assert [s.state for s in final] == ["done", "done"]
    runs = list_bundles(config.runs)
    assert len(runs) == 2
    assert {r.meta().spec.sweep_cell for r in runs} == {c.key for c in plan.cells}

    Sweep(config, pack, "s1", log=lambda m: None).run(sw.load())
    assert len(list_bundles(config.runs)) == 2  # nothing re-ran
    with Store(config.db) as store:
        assert store.query("SELECT DISTINCT sweep FROM runs") == [{"sweep": "s1"}]


def test_harness_failure_is_retried_until_done(setup, monkeypatch):
    config, pack = setup
    calls = {"n": 0}

    def flaky(inv):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("engine fell over")
        return fake_invocation(inv)

    monkeypatch.setattr(runner_mod, "run_invocation", flaky)
    sw = Sweep(config, pack, "s1", log=lambda m: None)
    plan = plan_for(config, pack)
    final = sw.run(plan)
    assert [s.state for s in final] == ["done", "done"]
    assert [m.status for m in final[0].runs] == [RunStatus.failed, RunStatus.completed]


def test_gives_up_and_stops_on_repeated_failures(setup, monkeypatch):
    config, pack = setup

    def broken(inv):
        raise RuntimeError("no")

    monkeypatch.setattr(runner_mod, "run_invocation", broken)
    plan = plan_for(config, pack)
    final = Sweep(config, pack, "s1", log=lambda m: None).run(plan, max_consecutive_failures=99)
    assert [s.state for s in final] == ["gave_up", "gave_up"]
    assert all(len(s.runs) == 2 for s in final)

    with pytest.raises(SweepError, match="in a row"):
        Sweep(config, pack, "s2", log=lambda m: None).run(
            plan_for(config, pack, name="s2"), max_consecutive_failures=1)


def test_crashed_run_counts_as_an_attempt(setup):
    config, pack = setup
    plan = plan_for(config, pack, bundles=["baseline"])
    cell = plan.cells[0]
    RunBundle.create(config.runs, RunMeta(run_id="dead", spec=cell.spec(plan)))  # status: running
    sw = Sweep(config, pack, "s1", log=lambda m: None)
    assert sw.status(plan)[0].harness_failures == 1
    final = sw.run(plan)
    assert final[0].state == "done" and len(final[0].runs) == 2


def test_bundle_drift_stops_resume(setup, tmp_path, monkeypatch):
    config, pack = setup
    src = pack.bundle_dir("baseline")
    copy = tmp_path / "bundles" / "baseline"
    shutil.copytree(src, copy)
    orig = pack.bundle_dir
    monkeypatch.setattr(pack, "bundle_dir", lambda n: copy if n == "baseline" else orig(n))
    sw = Sweep(config, pack, "s1", log=lambda m: None)
    plan = plan_for(config, pack, bundles=["baseline"])
    (copy / "bundle.yaml").write_text((copy / "bundle.yaml").read_text() + "\n# edited\n")
    assert any("baseline changed" in p for p in sw.drift(plan))
    with pytest.raises(SweepError, match="drifted"):
        sw.run(plan)
    assert sw.run(plan, allow_drift=True)[0].state == "done"


def test_installed_tool_version_drift(setup):
    config, pack = setup
    sw = Sweep(config, pack, "s1", log=lambda m: None)
    plan = plan_for(config, pack)
    sw.run(plan)
    b = list_bundles(config.runs)[0]
    meta = b.meta()
    meta.versions["tool"] = "v9.9.9"
    b.write_meta(meta)
    assert any("tool version changed" in p for p in sw.drift(plan))


def test_lock_refuses_a_second_live_sweep(setup):
    config, pack = setup
    sw = Sweep(config, pack, "s1", log=lambda m: None)
    sw.dir.mkdir(parents=True)
    (sw.dir / "lock").write_text(json.dumps({"pid": os.getppid()}))
    with pytest.raises(SweepError, match="already running"):
        sw.run(plan_for(config, pack))
    (sw.dir / "lock").write_text(json.dumps({"pid": 999_999_999}))  # stale
    assert sw.run(plan_for(config, pack))[0].state == "done"
    assert not (sw.dir / "lock").exists()


def test_preflight_reports_missing_pieces(setup, monkeypatch):
    config, pack = setup
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    plan = plan_for(config, pack, cloud=[True])
    problems = Sweep(config, pack, "s1").preflight(plan)
    assert any("GitHub slot" in p for p in problems)
    assert any("claude auth" in p for p in problems)
