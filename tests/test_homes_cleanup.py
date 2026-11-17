"""Fake-HOME cleanup: retention of session records, removal on every outcome, recovery.

These drive the real ``Runner.run`` / ``Runner.recover`` with the scripted agent from
test_runner_e2e. The slot reset is stubbed (a plain directory instead of a git clone), so the
tests need no network and no git: they are about what happens to ``<state>/homes/``.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from test_runner_e2e import StubEnv, StubVerifier, fake_invocation

from harness import homes as homes_mod
from harness import runner as runner_mod
from harness.agents.claude import ClaudeAdapter
from harness.bundle import RunBundle, list_bundles
from harness.config import HarnessConfig
from harness.homes import HomeClaim, remove_entry
from harness.isolation import make_home
from harness.models import CorpusEntry, RunMeta, RunSpec, RunStatus
from harness.slots import SlotPool
from harness_dagger.pack import DaggerPack

CLAUDE_REL = ".claude/projects/-work-proj/s1.jsonl"
CODEX_REL = ".codex/sessions/2026/01/02/rollout-s1.jsonl"
ARCHIVE_REL = ".codex/archived_sessions/rollout-old.jsonl"
SECRET = "super-secret-credential"


# --- fixtures and helpers ----------------------------------------------------------------


class World:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self.tmp = tmp_path
        self.config = HarnessConfig(state_dir=str(tmp_path / "state"), retries=0)
        self.entry = CorpusEntry(
            name="toy", url="unused", sha="a" * 40, expected_coverage=["dagger.io/python/pytest"]
        )
        self.pack = DaggerPack({})
        self.pack.environment = StubEnv()
        self.pack.verifier = StubVerifier()
        self.pack.integrations = []
        monkeypatch.setattr(self.pack, "corpus", lambda: {"toy": self.entry})
        self.logs: list[str] = []
        self.monkeypatch = monkeypatch
        self.real_home = tmp_path / "realhome"
        (self.real_home / ".docker").mkdir(parents=True)
        (self.real_home / ".docker" / "config.json").write_text('{"auths": {}}')
        monkeypatch.setenv("HOME", str(self.real_home))
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.setattr(runner_mod, "run_invocation", self.agent)
        monkeypatch.setattr(SlotPool, "reset", self.fake_reset)
        self.plant = lambda home: None  # extra mutations of the home done by the agent

    @property
    def state(self) -> Path:
        return Path(self.config.state)

    @property
    def homes(self) -> Path:
        return self.state / "homes"

    def left_in_homes(self) -> list[str]:
        return sorted(p.name for p in self.homes.iterdir()) if self.homes.exists() else []

    def runner(self) -> runner_mod.Runner:
        return runner_mod.Runner(self.config, self.pack, log=self.logs.append)

    def spec(self, agent: str = "claude") -> RunSpec:
        return RunSpec(repo="toy", bundle="baseline", agent=agent, cloud=False)

    def run(self, **kw) -> RunBundle:
        return self.runner().run(self.spec(**kw), referee=False)

    def bundles(self) -> list[RunBundle]:
        return list_bundles(self.config.runs)

    def leases(self) -> list[Path]:
        return list((self.state / "slots").glob("*.lease.json"))

    @staticmethod
    def fake_reset(pool, slot, entry, token):
        wd = pool.workdir(slot, entry)
        wd.mkdir(parents=True, exist_ok=True)
        return wd

    def agent(self, inv):
        """The scripted agent, which also leaves session records and credentials in its HOME."""
        home = Path(inv.home)
        if inv.phase == "task":
            for rel in (CLAUDE_REL, CODEX_REL, ARCHIVE_REL):
                (home / rel).parent.mkdir(parents=True, exist_ok=True)
                (home / rel).write_text(json.dumps({"session": rel}) + "\n")
            (home / ".codex").mkdir(exist_ok=True)
            (home / ".codex" / "auth.json").write_text(SECRET)
            (home / ".config" / "dagger").mkdir(parents=True, exist_ok=True)
            (home / ".config" / "dagger" / "cloud.json").write_text(SECRET)
            (home / "tmp" / "scratch.txt").write_text("scratch")
            self.plant(home)
        return fake_invocation(inv)


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    return World(tmp_path, monkeypatch)


def tree(root: Path) -> dict[str, str]:
    """Every path under ``root`` (links not followed) with its content or link target."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for n in dirnames + filenames:
            p = Path(dirpath) / n
            rel = str(p.relative_to(root))
            out[rel] = (
                ("-> " + os.readlink(p))
                if p.is_symlink()
                else ("<dir>" if p.is_dir() else p.read_text())
            )
    return out


def bundle_files(bundle: RunBundle) -> list[Path]:
    return [p for p in bundle.path.rglob("*") if not p.is_dir()]


def assert_clean_exit(world: World) -> None:
    assert world.left_in_homes() == []
    assert world.leases() == []


def kill_owner(pool: SlotPool, lease) -> None:
    data = json.loads(lease.path.read_text())
    data["pid"] = 999_999_999  # no such process
    lease.path.write_text(json.dumps(data))


# --- a run that completes ---------------------------------------------------------------


def test_completed_run_keeps_session_records_and_removes_the_home(world):
    bundle = world.run()
    meta = bundle.meta()
    assert meta.status == RunStatus.completed, meta.error
    assert meta.home and Path(meta.home).parent.parent == world.homes
    assert_clean_exit(world)
    kept = bundle.path / "agent-home"
    assert json.loads((kept / CLAUDE_REL).read_text()) == {"session": CLAUDE_REL}
    assert json.loads((kept / CODEX_REL).read_text()) == {"session": CODEX_REL}
    assert json.loads((kept / ARCHIVE_REL).read_text()) == {"session": ARCHIVE_REL}


def test_nothing_but_session_records_is_retained(world):
    bundle = world.run()
    retained = {
        str(p.relative_to(bundle.path / "agent-home"))
        for p in bundle_files(bundle)
        if "agent-home" in p.parts
    }
    assert retained == {CLAUDE_REL, CODEX_REL, ARCHIVE_REL}
    for p in bundle_files(bundle):
        assert p.name != "auth.json"
        assert SECRET not in p.read_text(errors="replace")


def test_codex_subscription_auth_still_syncs_back_and_is_not_kept(world, tmp_path):
    src = tmp_path / "codex-src" / "auth.json"
    src.parent.mkdir()
    src.write_text(
        json.dumps({"last_refresh": "2026-01-01T00:00:00Z", "tokens": {"refresh_token": "old"}})
    )
    world.config.agents.codex.auth = "subscription"
    world.config.agents.codex.auth_json = str(src)
    from harness.agents.base import get_adapter

    def agent(inv):
        home = Path(inv.home)
        if inv.phase == "task":
            copy = home / ".codex" / "auth.json"
            assert json.loads(copy.read_text())["tokens"]["refresh_token"] == "old"
            copy.write_text(
                json.dumps(
                    {"last_refresh": "2026-06-01T00:00:00Z", "tokens": {"refresh_token": "new"}}
                )
            )
            (home / ".codex" / "sessions").mkdir()
            (home / ".codex" / "sessions" / "rollout.jsonl").write_text("{}\n")
        get_adapter(inv.agent).sync_auth(inv)  # what run_invocation does after each turn
        return fake_invocation(inv)

    world.monkeypatch.setattr(runner_mod, "run_invocation", agent)
    bundle = world.run(agent="codex")
    assert bundle.meta().status == RunStatus.completed, bundle.meta().error
    assert json.loads(src.read_text())["tokens"]["refresh_token"] == "new"
    assert (bundle.path / "agent-home" / ".codex" / "sessions" / "rollout.jsonl").exists()
    assert not [p for p in bundle_files(bundle) if p.name == "auth.json"]
    assert_clean_exit(world)


def test_passthrough_and_shared_cache_targets_are_untouched(world):
    shared = world.state / "cache" / "shared" / "dagger"
    shared.mkdir(parents=True)
    (shared / "dagger-bin").write_text("cached")
    before = (tree(world.real_home), tree(shared))
    seen = {}

    def plant(home):
        seen["docker"] = (home / ".docker").is_symlink()
        seen["cache"] = (home / ".cache" / "dagger").is_symlink()

    world.plant = plant
    world.run()
    assert seen == {"docker": True, "cache": True}  # the links existed during the run
    assert (tree(world.real_home), tree(shared)) == before
    assert_clean_exit(world)


# --- runs that fail ---------------------------------------------------------------------


def test_agent_failure_cleans_up_and_keeps_the_failed_status(world):
    def boom(inv):
        world.agent(inv)
        raise RuntimeError("agent exploded")

    world.monkeypatch.setattr(runner_mod, "run_invocation", boom)
    bundle = world.run()
    meta = bundle.meta()
    assert meta.status == RunStatus.failed and "agent exploded" in meta.error
    assert_clean_exit(world)
    assert (bundle.path / "agent-home" / CLAUDE_REL).exists()  # what the agent wrote is kept


def test_verifier_failure_cleans_up_and_keeps_the_failed_status(world):
    def boom(ctx, attempt):
        raise RuntimeError("verifier exploded")

    world.monkeypatch.setattr(world.pack.verifier, "verify", boom)
    bundle = world.run()
    assert bundle.meta().status == RunStatus.failed and "verifier exploded" in bundle.meta().error
    assert_clean_exit(world)
    assert (bundle.path / "agent-home" / CODEX_REL).exists()


def test_interrupt_cleans_up_and_is_reraised(world):
    def interrupted(inv):
        world.agent(inv)
        raise KeyboardInterrupt

    world.monkeypatch.setattr(runner_mod, "run_invocation", interrupted)
    with pytest.raises(KeyboardInterrupt):
        world.run()
    (bundle,) = world.bundles()
    assert bundle.meta().status == RunStatus.failed and "KeyboardInterrupt" in bundle.meta().error
    assert_clean_exit(world)
    assert (bundle.path / "agent-home" / CLAUDE_REL).exists()


def _fail(msg):
    def fail(*a, **kw):
        raise RuntimeError(msg)

    return fail


def _partial_make_home(parent, config):
    home = make_home(parent, config)
    (home / ".claude" / "projects").mkdir()
    (home / ".claude" / "projects" / "half.jsonl").write_text("{}\n")
    raise OSError("disk full while building the home")


SETUP_FAILURES = {
    "install": lambda w: w.monkeypatch.setattr(
        w.pack.environment, "install", _fail("install failed")
    ),
    "prepare": lambda w: w.monkeypatch.setattr(
        w.pack.environment, "prepare", _fail("prepare failed")
    ),
    "reset": lambda w: w.monkeypatch.setattr(w.pack.environment, "reset", _fail("reset failed")),
    "setup_home": lambda w: w.monkeypatch.setattr(
        ClaudeAdapter, "setup_home", _fail("setup_home failed")
    ),
    "auth_env": lambda w: w.monkeypatch.setattr(
        ClaudeAdapter, "auth_env", _fail("auth_env failed")
    ),
    "slot_reset": lambda w: w.monkeypatch.setattr(SlotPool, "reset", _fail("slot reset failed")),
    "make_home": lambda w: w.monkeypatch.setattr(runner_mod, "make_home", _partial_make_home),
}


@pytest.mark.parametrize("where", SETUP_FAILURES)
def test_failure_inside_setup_cleans_up(world, where):
    SETUP_FAILURES[where](world)
    bundle = world.run()
    meta = bundle.meta()
    assert meta.status == RunStatus.failed
    assert meta.error and meta.finished_at
    assert_clean_exit(world)
    if where == "make_home":
        assert "disk full" in meta.error
        assert (bundle.path / "agent-home" / CLAUDE_REL.replace("-work-proj/s1", "half")).exists()
    else:
        assert (
            where.replace("slot_", "slot ").replace("_", " ") in meta.error or where in meta.error
        )


def test_lease_names_the_home_before_it_exists(world):
    seen = {}

    def spy(parent, config):
        (lease_file,) = world.leases()
        seen["lease_home"] = json.loads(lease_file.read_text())["home"]
        seen["parent"] = parent
        seen["dir_exists"] = parent.exists()
        seen["lock_exists"] = (parent.parent / f"{parent.name}.lock").exists()
        return make_home(parent, config)

    world.monkeypatch.setattr(runner_mod, "make_home", spy)
    world.run()
    assert seen["lease_home"] == seen["parent"].name
    assert seen["dir_exists"] is False and seen["lock_exists"] is True


# --- cleanup failures never change the outcome ------------------------------------------


@pytest.mark.parametrize("what", ["retain", "remove"])
def test_cleanup_failure_is_logged_and_teardown_goes_on(world, what):
    revoked = []
    world.monkeypatch.setattr(runner_mod.Runner, "_harness_token", lambda *a, **kw: "harness-tok")
    world.monkeypatch.setattr(runner_mod.github, "revoke", revoked.append)
    target, name = (
        (runner_mod, "retain_sessions") if what == "retain" else (homes_mod, "remove_entry")
    )
    world.monkeypatch.setattr(target, name, _fail(f"{what} broke"))
    bundle = world.run()
    assert bundle.meta().status == RunStatus.completed, bundle.meta().error
    assert any(f"{what} broke" in m for m in world.logs)
    assert revoked == ["harness-tok"]
    assert world.leases() == []


def test_cleanup_failure_does_not_mask_a_failed_run(world):
    world.monkeypatch.setattr(runner_mod, "retain_sessions", _fail("retain broke"))
    world.monkeypatch.setattr(world.pack.environment, "install", _fail("install failed"))
    bundle = world.run()
    assert bundle.meta().status == RunStatus.failed and "install failed" in bundle.meta().error
    assert any("retain broke" in m for m in world.logs)
    assert_clean_exit(world)


# --- symlinks are never followed --------------------------------------------------------


@pytest.fixture
def outside(tmp_path) -> Path:
    out = tmp_path / "outside"
    (out / "sessions" / "x").mkdir(parents=True)
    (out / "sessions" / "x" / "leak.jsonl").write_text(SECRET)
    (out / "projects").mkdir()
    (out / "projects" / "leak.jsonl").write_text(SECRET)
    (out / "auth.json").write_text(SECRET)
    return out


def _replace(path: Path, link_to: Path | None = None) -> None:
    import shutil

    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
    if link_to is not None:
        path.symlink_to(link_to)


def _plant_codex_dir_link(outside):
    def plant(home):
        _replace(home / ".codex", outside)
        (outside / "sessions").mkdir(exist_ok=True)

    return plant


def _plant_claude_dir_link(outside):
    def plant(home):
        _replace(home / ".claude", outside)

    return plant


def _plant_sessions_link(outside):
    def plant(home):
        _replace(home / ".codex" / "sessions", outside / "sessions")
        _replace(home / ".claude" / "projects", outside / "projects")
        _replace(home / ".codex" / "archived_sessions", outside / "projects")

    return plant


def _plant_nested_links(outside):
    def plant(home):
        sessions = home / ".codex" / "sessions"
        (sessions / "dirlink").symlink_to(outside / "sessions")
        (sessions / "filelink.jsonl").symlink_to(outside / "auth.json")
        (home / ".claude" / "projects" / "-work-proj" / "up").symlink_to(outside)
        (home / ".claude" / "projects" / "dangling").symlink_to(outside / "nonexistent")

    return plant


@pytest.mark.parametrize(
    "plant",
    [_plant_codex_dir_link, _plant_claude_dir_link, _plant_sessions_link, _plant_nested_links],
    ids=["codex-dir", "claude-dir", "session-dirs", "nested"],
)
def test_symlinks_in_the_home_are_not_followed(world, outside, plant):
    world.plant = plant(outside)
    before = tree(outside)
    bundle = world.run()
    assert bundle.meta().status == RunStatus.completed, bundle.meta().error
    assert tree(outside) == before  # targets and their contents unchanged
    assert_clean_exit(world)
    for dirpath, dirnames, filenames in os.walk(bundle.path, followlinks=False):
        for n in dirnames + filenames:
            p = Path(dirpath) / n
            assert not p.is_symlink(), p
            if p.is_file():
                assert SECRET not in p.read_text(errors="replace"), p


def test_nested_links_do_not_stop_the_real_records_being_kept(world, outside):
    world.plant = _plant_nested_links(outside)
    bundle = world.run()
    assert (bundle.path / "agent-home" / CODEX_REL).exists()
    assert (bundle.path / "agent-home" / CLAUDE_REL).exists()


def test_remove_entry_refuses_symlinks_and_strangers(tmp_path, outside):
    root = tmp_path / "homes"
    root.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(OSError, match="symlink"):
        remove_entry(root, "link")
    assert (outside / "auth.json").exists() and (root / "link").is_symlink()
    for bad in ("../outside", "a/b", "..", ""):
        with pytest.raises(ValueError):
            remove_entry(root, bad)
    assert (outside / "auth.json").exists()


def test_remove_entry_deletes_read_only_trees_without_following_links(tmp_path, outside):
    root = tmp_path / "homes"
    victim = root / "abc" / "home" / "go" / "pkg"
    victim.mkdir(parents=True)
    (victim / "mod.go").write_text("package x")
    (root / "abc" / "home" / "link").symlink_to(outside)
    victim.chmod(0o500)
    victim.parent.chmod(0o500)
    remove_entry(root, "abc")
    assert not (root / "abc").exists()
    assert (outside / "sessions" / "x" / "leak.jsonl").read_text() == SECRET


def test_claims_are_exclusive_and_released_with_the_holder(tmp_path):
    root = tmp_path / "homes"
    claim = HomeClaim.new(root, "aaa")
    assert HomeClaim.adopt(root, "aaa") is None  # a live owner
    claim.close()
    again = HomeClaim.adopt(root, "aaa")
    assert again is not None
    again.remove()
    assert list(root.iterdir()) == []


# --- crash recovery ---------------------------------------------------------------------


def crash(
    world: World,
    *,
    record_home: bool,
    name: str = "crashed1",
    run_id: str = "20260101T000000-toy-baseline-claude-aaaa",
    with_sessions: bool = True,
) -> tuple[RunBundle, Path]:
    """A run whose process died: stale lease, a bundle still 'running', its home on disk."""
    pool = SlotPool(world.config)
    lease = pool.lease(run_id, home=name)
    kill_owner(pool, lease)
    home = make_home(world.homes / name, world.config)
    if with_sessions:
        for rel in (CLAUDE_REL, CODEX_REL):
            (home / rel).parent.mkdir(parents=True, exist_ok=True)
            (home / rel).write_text(json.dumps({"session": rel}))
    (home / ".codex" / "auth.json").write_text(SECRET)
    (world.homes / name / "invocation-task-0.json").write_text("{}")
    meta = RunMeta(
        run_id=run_id,
        spec=world.spec(),
        repo_sha=world.entry.sha,
        home=str(home) if record_home else None,
    )
    meta.slot = lease.slot.name
    bundle = RunBundle.create(world.config.runs, meta)
    return bundle, home


class Recorder:
    name = "recorder"

    def __init__(self):
        self.homes: list[Path] = []
        self.owned_during = []

    def teardown(self, ctx):
        self.homes.append(ctx.home)
        root, name = ctx.home.parent.parent, ctx.home.parent.name
        claim = HomeClaim.adopt(root, name)
        self.owned_during.append(claim is None)  # still ours while recovery uses it
        if claim:
            claim.close()


@pytest.mark.parametrize(
    "record_home", [True, False], ids=["run-json-has-home", "run-json-has-no-home"]
)
def test_recovery_keeps_sessions_and_removes_every_home(world, record_home):
    shared = world.state / "cache" / "shared" / "dagger"
    shared.mkdir(parents=True)
    (shared / "cached").write_text("c")
    before = (tree(world.real_home), tree(shared))
    rec = Recorder()
    world.pack.integrations = [rec]
    bundle, home = crash(world, record_home=record_home)
    assert world.runner().recover() == [bundle.meta().run_id]
    assert world.left_in_homes() == []
    assert world.leases() == []
    meta = bundle.meta()
    assert meta.status == RunStatus.crashed
    kept = bundle.path / "agent-home"
    assert (kept / CLAUDE_REL).exists() and (kept / CODEX_REL).exists()
    assert not [p for p in bundle_files(bundle) if p.name == "auth.json"]
    # recovery's own teardown home: under homes/, owned while used, gone afterwards
    (used,) = rec.homes
    assert used.parent.parent == world.homes and used != home
    assert rec.owned_during == [True]
    # and nowhere else
    assert not (world.state / "recovery").exists()
    assert not list(bundle.path.rglob("home"))
    assert (tree(world.real_home), tree(shared)) == before


def test_recovery_of_a_bundle_less_crash_leaves_no_home_anywhere(world):
    pool = SlotPool(world.config)
    lease = pool.lease("ghost-run", home="ghost1")
    kill_owner(pool, lease)
    make_home(world.homes / "ghost1", world.config)
    assert world.runner().recover() == ["ghost-run"]
    assert world.left_in_homes() == []
    assert not (world.state / "recovery").exists()
    assert not list(world.state.rglob("home"))


def test_recovery_survives_a_failing_cleanup_and_still_releases_the_lease(world):
    bundle, _ = crash(world, record_home=True)
    world.monkeypatch.setattr(runner_mod, "retain_sessions", _fail("retain broke"))
    world.runner().recover()
    assert any("retain broke" in m for m in world.logs)
    assert world.leases() == [] and world.left_in_homes() == []
    assert bundle.meta().status == RunStatus.crashed


def test_recovery_reclaims_leftovers_nobody_owns(world, outside):
    # from before this change: no lock files, one matched to a bundle through run.json
    matched = make_home(world.homes / "oldmatched", world.config)
    (matched / CODEX_REL).parent.mkdir(parents=True)
    (matched / CODEX_REL).write_text("{}")
    meta = RunMeta(
        run_id="20250101T000000-toy-baseline-claude-bbbb",
        spec=world.spec(),
        status=RunStatus.completed,
        home=str(matched),
    )
    done = RunBundle.create(world.config.runs, meta)
    make_home(world.homes / "orphan", world.config)  # no bundle at all
    (world.homes / "stray-file").write_text("x")
    (world.homes / "stale.lock").write_text("")  # a lock whose directory is gone
    (world.homes / "linked").symlink_to(outside)  # never followed, never removed
    world.runner().recover()
    assert world.left_in_homes() == ["linked"]
    assert (world.homes / "linked").is_symlink()
    assert tree(outside)["sessions/x/leak.jsonl"] == SECRET
    assert (done.path / "agent-home" / CODEX_REL).exists()
    assert done.meta().status == RunStatus.completed  # a finished run is not touched otherwise


# --- recovery never deletes a live home -------------------------------------------------


class Pause:
    """Run a real ``Runner.run`` in a thread, blocked at a chosen point of its setup."""

    def __init__(self, world: World):
        self.reached, self.go = threading.Event(), threading.Event()
        self.result: dict = {}

    def point(self, *a, **kw):
        self.reached.set()
        assert self.go.wait(30), "test never released the paused run"

    def start(self, world: World) -> None:
        def target():
            try:
                self.result["bundle"] = world.run()
            except BaseException as e:  # noqa: BLE001
                self.result["error"] = e

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()
        assert self.reached.wait(30), "run never reached the pause"

    def finish(self) -> RunBundle:
        self.go.set()
        self.thread.join(30)
        assert not self.thread.is_alive()
        assert "error" not in self.result, self.result.get("error")
        return self.result["bundle"]


def test_recovery_leaves_a_paused_setups_home_alone_however_old(world):
    pause = Pause(world)
    real_install = world.pack.environment.install

    def install(version, ctx):
        pause.point()
        return real_install(version, ctx)

    world.monkeypatch.setattr(world.pack.environment, "install", install)
    pause.start(world)
    # setup is paused: the home exists, run.json does not name it yet
    (name,) = [p.name for p in world.homes.iterdir() if p.is_dir()]
    (bundle,) = world.bundles()
    assert bundle.meta().home is None
    ancient = 946_684_800  # 2000-01-01
    for p in [
        world.homes / name,
        world.homes / name / "home",
        *(world.homes / name / "home").iterdir(),
    ]:
        os.utime(p, (ancient, ancient), follow_symlinks=False)

    assert world.runner().recover() == []
    assert (world.homes / name / "home" / ".claude").is_dir()
    assert len(world.leases()) == 1

    done = pause.finish()  # the paused run finishes and its own teardown removes the home
    assert done.meta().status == RunStatus.completed, done.meta().error
    assert_clean_exit(world)
    assert (done.path / "agent-home" / CLAUDE_REL).exists()


def test_recovery_leaves_a_home_whose_directory_does_not_exist_yet(world):
    """Ownership is on record before the directory exists: a claim with no directory survives."""
    pause = Pause(world)
    real = runner_mod.make_home

    def slow_make_home(parent, config):
        pause.point()
        return real(parent, config)

    world.monkeypatch.setattr(runner_mod, "make_home", slow_make_home)
    pause.start(world)
    (lock,) = [p for p in world.homes.iterdir()]
    assert lock.name.endswith(".lock")
    world.runner().recover()
    assert lock.exists()
    done = pause.finish()
    assert done.meta().status == RunStatus.completed, done.meta().error
    assert_clean_exit(world)


def test_recovery_leaves_a_home_created_between_its_discovery_steps(world):
    pause = Pause(world)
    real_install = world.pack.environment.install

    def install(version, ctx):
        pause.point()
        return real_install(version, ctx)

    world.monkeypatch.setattr(world.pack.environment, "install", install)
    real_list = runner_mod.entry_names
    fired = []

    def list_then_publish(root):
        names = real_list(root)  # discovery step 1
        if not fired:
            fired.append(True)
            pause.start(world)  # a run starts and publishes its home right after the listing
        return names

    world.monkeypatch.setattr(runner_mod, "entry_names", list_then_publish)
    make_home(world.homes / "leftover", world.config)  # an unowned entry in the same pass
    world.runner().recover()  # discovery step 2: claim each listed entry
    assert fired
    live = [p.name for p in world.homes.iterdir() if p.is_dir()]
    assert len(live) == 1 and live != ["leftover"]  # the leftover went, the new home stayed
    assert (world.homes / live[0] / "home" / ".claude").is_dir()
    done = pause.finish()
    assert done.meta().status == RunStatus.completed, done.meta().error
    assert_clean_exit(world)


def test_recovery_leaves_a_listed_home_that_is_owned_by_a_live_process(world):
    claim = HomeClaim.new(world.homes, "livehome")
    make_home(claim.path, world.config)
    world.runner().recover()
    assert (claim.path / "home" / ".claude").is_dir()
    claim.close()
    world.runner().recover()  # its owner is gone now
    assert world.left_in_homes() == []
