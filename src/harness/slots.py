"""Slot pool: leases, reset to a corpus repo's pinned SHA, teardown, crash recovery.

A slot is a private generic GitHub repo (``slot-01..N``) plus a local working copy the
agent runs in. Slots with no ``repo`` are local-only: no pushes, no integrations.
Slot history carries over between runs, so results are always looked up by commit SHA.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from harness import github
from harness.config import HarnessConfig, SlotConfig
from harness.isolation import git_credential_env, run
from harness.models import CorpusEntry


@dataclass
class Lease:
    slot: SlotConfig
    run_id: str
    path: Path
    agent_token: str | None = None

    def record_token(self, token: str) -> None:
        self.agent_token = token
        data = json.loads(self.path.read_text())
        data["agent_token"] = token
        self.path.write_text(json.dumps(data))


class SlotBusy(RuntimeError):
    pass


class SlotPool:
    def __init__(self, config: HarnessConfig):
        self.config = config
        self.dir = config.state / "slots"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _lease_path(self, name: str) -> Path:
        return self.dir / f"{name}.lease.json"

    def get(self, name: str) -> SlotConfig:
        for s in self.config.slots:
            if s.name == name:
                return s
        raise KeyError(f"no slot named {name!r}")

    def lease(self, run_id: str, preferred: str | None = None) -> Lease:
        candidates = [self.get(preferred)] if preferred else self.config.slots
        for slot in candidates:
            path = self._lease_path(slot.name)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(fd, "w") as f:
                json.dump(
                    {
                        "run_id": run_id,
                        "pid": os.getpid(),
                        "host": socket.gethostname(),
                        "started": time.time(),
                    },
                    f,
                )
            return Lease(slot=slot, run_id=run_id, path=path)
        raise SlotBusy("no free slot" + (f" ({preferred} is leased)" if preferred else ""))

    def release(self, lease: Lease) -> None:
        lease.path.unlink(missing_ok=True)

    def stale_leases(self) -> list[tuple[SlotConfig, dict]]:
        """Leases whose owning process is gone: a crashed run to clean up."""
        out = []
        for slot in self.config.slots:
            path = self._lease_path(slot.name)
            if not path.exists():
                continue
            data = json.loads(path.read_text())
            if data.get("host") != socket.gethostname():
                continue
            if not _pid_alive(data.get("pid", -1)):
                out.append((slot, data))
        return out

    def status(self) -> list[dict]:
        out = []
        for slot in self.config.slots:
            path = self._lease_path(slot.name)
            data = json.loads(path.read_text()) if path.exists() else None
            if data:
                data.pop("agent_token", None)
            out.append(
                {
                    "slot": slot.name,
                    "repo": slot.repo,
                    "lease": data,
                    "alive": _pid_alive(data["pid"]) if data else None,
                }
            )
        return out

    def force_release(self, name: str) -> dict | None:
        path = self._lease_path(name)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        path.unlink()
        return data

    # --- working copies -------------------------------------------------------------------

    def workdir(self, slot: SlotConfig, entry: CorpusEntry) -> Path:
        return self.config.work / slot.name / entry.name

    def git_env(self, token: str | None = None) -> dict[str, str]:
        gitconfig = self.config.state / "harness.gitconfig"
        if not gitconfig.exists():
            gh = self.config.github
            gitconfig.write_text(f"[user]\n\tname = {gh.bot_name}\n\temail = {gh.bot_email}\n")
        env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        if token:
            env.update(git_credential_env(token))
        return env

    def cached_repo(self, entry: CorpusEntry, token: str | None = None) -> Path:
        """A bare clone of the corpus repo (or its mirror) containing the pinned SHA."""
        if not entry.sha:
            raise ValueError(f"corpus entry {entry.name} has no pinned SHA")
        cache = self.config.cache / "repos" / f"{entry.name}.git"
        src = f"https://github.com/{entry.mirror}.git" if entry.mirror else entry.url
        env = self.git_env(token if entry.mirror else None)
        if not cache.exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "clone", "--bare", src, str(cache)], env=env)
        has = run(["git", "cat-file", "-e", f"{entry.sha}^{{commit}}"], cwd=cache, env=env, check=False)
        if has.returncode != 0:
            run(["git", "fetch", "origin", entry.sha], cwd=cache, env=env)
        run(["git", "update-ref", f"refs/pins/{entry.sha}", entry.sha], cwd=cache, env=env)
        return cache

    def reset(self, slot: SlotConfig, entry: CorpusEntry, harness_token: str | None) -> Path:
        """Recreate the working copy at the pinned SHA and force the slot repo to match."""
        cache = self.cached_repo(entry, harness_token)
        wd = self.workdir(slot, entry)
        if wd.parent.exists():
            shutil.rmtree(wd.parent)
        wd.mkdir(parents=True)
        env = self.git_env(harness_token)
        run(["git", "init", "-q", "-b", "main"], cwd=wd, env=env)
        run(["git", "fetch", "-q", "--no-tags", str(cache), f"refs/pins/{entry.sha}"], cwd=wd, env=env)
        run(["git", "reset", "-q", "--hard", entry.sha], cwd=wd, env=env)
        if slot.repo:
            origin = f"https://github.com/{slot.repo}.git"
        else:
            # Local-only: a bare repo stands in for the slot, so pushing still works.
            origin = str(self.local_origin(slot))
            run(["git", "init", "-q", "--bare", "-b", "main", origin], env=env)
        run(["git", "remote", "add", "origin", origin], cwd=wd, env=env)
        run(["git", "push", "-q", "--force", "origin", "main"], cwd=wd, env=env)
        self._clean_remote(slot, harness_token)
        run(["git", "fetch", "-q", "origin"], cwd=wd, env=env)
        run(["git", "branch", "-q", "--set-upstream-to=origin/main", "main"], cwd=wd, env=env)
        return wd

    def local_origin(self, slot: SlotConfig) -> Path:
        return self.config.work / slot.name / "origin.git"

    def _clean_remote(self, slot: SlotConfig, token: str | None) -> None:
        if not (slot.repo and token):
            return
        repo = github.Repo(slot.repo, token)
        for pr in repo.open_prs():
            repo.close_pr(pr["number"])
        for b in repo.branches():
            if b != "main":
                repo.delete_branch(b)

    def pushed_shas(self, slot: SlotConfig, entry: CorpusEntry, token: str | None) -> dict[str, str]:
        """Remote branch heads that differ from the pinned SHA: what the agent pushed."""
        url = f"https://github.com/{slot.repo}.git" if slot.repo else str(self.local_origin(slot))
        out = run(
            ["git", "ls-remote", "--heads", url],
            env=self.git_env(token),
            check=False,
        )
        heads = {}
        for line in out.stdout.splitlines():
            if "\t" not in line:
                continue
            sha, ref = line.split("\t", 1)
            if sha != entry.sha:
                heads[ref.removeprefix("refs/heads/")] = sha
        return heads

    def teardown(self, slot: SlotConfig, entry: CorpusEntry, harness_token: str | None) -> None:
        """Back to the pinned SHA, no branches, no open PRs. Safe to repeat."""
        if not slot.repo or not harness_token:
            return
        self._clean_remote(slot, harness_token)
        cache = self.cached_repo(entry, harness_token)
        env = self.git_env(harness_token)
        run(
            ["git", "push", "-q", "--force", f"https://github.com/{slot.repo}.git",
             f"refs/pins/{entry.sha}:refs/heads/main"],
            cwd=cache,
            env=env,
        )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
