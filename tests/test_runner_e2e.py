"""End-to-end run flow in local-only mode with a scripted agent and a stub verifier."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness import runner as runner_mod
from harness.agents.base import AgentInvocation, RawWriter
from harness.bundle import list_bundles
from harness.config import HarnessConfig
from harness.models import AgentTurnResult, CorpusEntry, RunSpec, VerifyResult
from harness.pack import ToolInstall
from harness.referee import backends
from harness.store import Store
from harness_dagger.pack import DaggerPack


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                               "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}).stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path) -> CorpusEntry:
    repo = tmp_path / "upstream"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\n')
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/tests.yaml").write_text("jobs:\n  tests:\n    steps:\n      - run: pytest\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return CorpusEntry(name="toy", url=str(repo), sha=git(repo, "rev-parse", "HEAD"),
                       expected_coverage=["dagger.io/python/pytest"])


class StubEnv:
    def install(self, version, ctx):
        return ToolInstall(version="v1.0.0-test")

    def prepare(self, ctx):
        return {}

    def reset(self, ctx):
        pass


class StubVerifier:
    """Fails until dagger.toml has a module, like a real first attempt might."""

    def verify(self, ctx, attempt):
        toml = ctx.workdir / "dagger.toml"
        ok = toml.exists() and "[modules." in toml.read_text()
        (ctx.run_dir / "verify.log").write_text("ok" if ok else "Error: no checks")
        return VerifyResult(passed=ok, exit_code=0 if ok else 1, checks=["pytest:test"] if ok else [],
                            error_excerpt="" if ok else "Error: no checks", attempt=attempt)


def fake_invocation(inv: AgentInvocation) -> AgentTurnResult:
    """Scripted agent: an empty dagger.toml first, the module on retry, a report at the end."""
    out = RawWriter(inv)
    out.write("harness_prompt", {"prompt": inv.prompt})
    wd = Path(inv.workdir)
    assert Path(inv.env["HOME"]) == Path(inv.home)  # isolation: fake HOME
    assert "baseline" not in inv.home  # blinding: agent-visible paths don't name the variant
    if inv.phase == "task":
        (wd / "dagger.toml").write_text("")
        cmd, code = "dagger init", 0
    elif inv.phase == "retry":
        (wd / "dagger.toml").write_text('[modules.pytest]\nsource = "dagger.io/python/pytest"\n')
        cmd, code = "dagger module install dagger.io/python/pytest", 0
        env = {**inv.env, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a",
               "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@a"}
        for args in (["switch", "-qc", "adopt"], ["add", "-A"], ["commit", "-qm", "dagger"],
                     ["push", "-q", "origin", "adopt"]):
            subprocess.run(["git", *args], cwd=wd, env=env, check=True)
    else:
        report = {"summary": "fine", "difficulty": 2, "what_i_set_up": ["pytest module"]}
        out.write("ResultMessage", {"subtype": "success", "num_turns": 1, "result": json.dumps(report),
                                    "session_id": "s1"})
        out.close()
        return AgentTurnResult(phase=inv.phase, prompt=inv.prompt, session_id="s1",
                               final_text=json.dumps(report), turns=1)
    out.write("AssistantMessage", {"content": [
        {"_type": "ToolUseBlock", "id": f"t{inv.attempt}", "name": "Bash", "input": {"command": cmd}}]})
    out.write("UserMessage", {"content": [
        {"_type": "ToolResultBlock", "tool_use_id": f"t{inv.attempt}", "content": "ok", "is_error": code != 0}]})
    out.close()
    return AgentTurnResult(phase=inv.phase, attempt=inv.attempt, prompt=inv.prompt, session_id="s1",
                           turns=2, cost_usd=0.1)


def test_full_loop(tmp_path, upstream, monkeypatch):
    config = HarnessConfig(state_dir=str(tmp_path / "state"), retries=2)
    pack = DaggerPack({})
    pack.environment = StubEnv()
    pack.verifier = StubVerifier()
    monkeypatch.setattr(pack, "corpus", lambda: {"toy": upstream})
    monkeypatch.setattr(runner_mod, "run_invocation", fake_invocation)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")

    def fake_referee(config, system, prompt, schema):
        assert "baseline" not in prompt.split("# Transcript")[1]  # blinded
        return json.dumps({
            "scores": [{"criterion": "idiomatic", "score": 4, "rationale": "dagger.toml",
                        "evidence": [{"path": "dagger.toml", "line": 1}]},
                       {"criterion": "fidelity", "score": 2, "rationale": "no evidence", "evidence": []}],
            "ci_gaps": [], "friction": [], "claims": [], "issue_candidates": [],
        })

    monkeypatch.setattr(backends, "complete", fake_referee)

    bundle = runner_mod.Runner(config, pack, log=lambda m: None).run(
        RunSpec(repo="toy", bundle="baseline", agent="claude", cloud=False)
    )
    meta = bundle.meta()
    assert meta.status == "completed", meta.error
    assert [v.passed for v in meta.verifies] == [False, True]
    assert len(meta.nudges) == 1 and "no checks" in meta.nudges[0].message
    assert [t.phase for t in meta.turns] == ["task", "retry", "feedback"]
    assert "dagger.toml" in bundle.diff()
    assert len(meta.pushed_shas) == 1  # local-only slot: pushed to the bare stand-in origin
    assert meta.integration[0].error.startswith("local-only")
    assert (bundle.path / "final" / "dagger.toml").exists()
    assert bundle.feedback().difficulty == 2
    facts = {f["key"]: f["value"] for f in bundle.read_json("analysis.json")}
    assert facts["retries_needed"] == 1
    assert facts["dagger.module_strategy"] == "official_only"
    card = bundle.scorecard()
    assert [s.criterion for s in card.scores] == ["idiomatic"]
    assert card.rejected and card.verdicts["module_strategy_verdict"] == "good"
    assert not list((tmp_path / "state" / "slots").glob("*.lease.json"))  # lease released
    with Store(config.db) as store:
        assert store.query("SELECT passed, retries_needed FROM runs") == [{"passed": True, "retries_needed": 1}]
    assert len(list_bundles(config.runs)) == 1


def test_crash_recovery(tmp_path, upstream, monkeypatch):
    config = HarnessConfig(state_dir=str(tmp_path / "state"))
    pack = DaggerPack({})
    monkeypatch.setattr(pack, "corpus", lambda: {"toy": upstream})
    from harness.slots import SlotPool

    pool = SlotPool(config)
    lease = pool.lease("ghost-run")
    data = json.loads(lease.path.read_text())
    data["pid"] = 999_999_999  # no such process
    lease.path.write_text(json.dumps(data))
    cleaned = runner_mod.Runner(config, pack, log=lambda m: None).recover()
    assert cleaned == ["ghost-run"]
    assert not lease.path.exists()
