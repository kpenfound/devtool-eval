"""Ownership, retention and removal of the per-run fake HOMEs under ``<state_dir>/homes/``.

A run's fake HOME lives at ``<state_dir>/homes/<name>/home``, next to the worker's
invocation/result scratch files in ``<state_dir>/homes/<name>/``. It is deliberately outside
the run bundle (agent-visible paths must not name the run or variant), so nothing but the
harness itself ties it to a run. That makes ownership the whole problem:

- **Ownership** is a lock file ``<state_dir>/homes/<name>.lock`` that the owner creates and
  ``flock``s *before* the home directory exists and holds until it has removed the directory.
  The kernel drops the lock when the owner dies, so "is the owner alive" needs no pid or age
  heuristics. A run, and a recovery pass that needs a temporary home to drive a crashed
  run's teardown, claim homes the same way.
- **Discovery cannot miss a live home.** A directory can only appear after its owner holds
  the lock, and a lock file is only unlinked by someone holding it. So a recovery pass that
  lists ``homes/`` and then takes each entry's lock (non-blocking) sees every home that was
  published before the listing as owned, and homes created after it are not in the listing.
  The lock file of a claim that has not yet created its directory is recognised the same way
  (``claim`` re-checks that its locked file is still the one on disk).
- **Retention.** The only content of a fake HOME worth keeping is the agent's own session
  records. They are copied into the run bundle under ``agent-home/`` (same relative paths),
  and then the whole ``homes/<name>/`` is removed. Nothing else, in particular no credential,
  is copied.
- **No symlink is ever followed**, neither when copying nor when removing: every component
  from ``homes/`` down to a session directory is opened with ``O_NOFOLLOW`` relative to its
  parent, and inside the tree only real directories and regular files are copied.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

HOME_DIR = "home"  # <homes>/<name>/home is the agent's HOME (see isolation.make_home)
LOCK_SUFFIX = ".lock"
# Retained in the run bundle under RETAINED_DIR, at the same path relative to the HOME.
RETAINED_DIR = "agent-home"
SESSION_DIRS = (
    (".claude", "projects"),  # Claude Code transcripts (CLAUDE_CONFIG_DIR)
    (".codex", "sessions"),  # Codex rollouts (CODEX_HOME)
    (".codex", "archived_sessions"),
)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

Log = Callable[[str], None]


def new_name() -> str:
    return secrets.token_hex(6)


def homes_root(state: Path) -> Path:
    return state / "homes"


class HomeClaim:
    """Exclusive ownership of ``<root>/<name>/`` for as long as the claim is held."""

    def __init__(self, root: Path, name: str, fd: int):
        self.root, self.name, self._fd = root, name, fd

    @property
    def path(self) -> Path:
        return self.root / self.name

    @property
    def home(self) -> Path:
        return self.path / HOME_DIR

    @staticmethod
    def _lock_path(root: Path, name: str) -> Path:
        return root / f"{name}{LOCK_SUFFIX}"

    @classmethod
    def new(cls, root: Path, name: str | None = None) -> HomeClaim:
        """Claim a fresh name. The directory itself is created by the caller (``make_home``)
        only after this returns, so ownership is on record before the directory exists."""
        root.mkdir(parents=True, exist_ok=True)
        claim = cls._lock(root, name or new_name())
        if claim is None:
            raise FileExistsError(f"home {name} is already claimed")
        return claim

    @classmethod
    def adopt(cls, root: Path, name: str) -> HomeClaim | None:
        """Claim an existing entry, or None while a live owner holds it."""
        return cls._lock(root, name)

    @classmethod
    def _lock(cls, root: Path, name: str) -> HomeClaim | None:
        path = cls._lock_path(root, name)
        while True:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return None
            except BaseException:
                os.close(fd)
                raise
            try:
                same = os.stat(path).st_ino == os.fstat(fd).st_ino
            except FileNotFoundError:
                same = False
            if same:
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, 0)
                    os.write(fd, json.dumps({"pid": os.getpid()}).encode())
                return cls(root, name, fd)
            # The file we locked was unlinked (by the cleanup of an earlier owner of this
            # name) between open and flock: lock the current one instead.
            os.close(fd)

    def remove(self) -> None:
        """Delete the directory, then (only if that worked) the lock file, then let go."""
        try:
            remove_entry(self.root, self.name)
            self._lock_path(self.root, self.name).unlink(missing_ok=True)
        finally:
            self.close()

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # drops the flock
            self._fd = None


# --- removal ------------------------------------------------------------------------------


def remove_entry(root: Path, name: str) -> None:
    """Remove ``<root>/<name>`` without following symlinks. Only a direct child of ``root``
    that is not itself a symlink is ever touched."""
    if not name or name in (".", "..") or "/" in name or os.sep in name:
        raise ValueError(f"not a homes entry name: {name!r}")
    path = root / name
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise OSError(f"refusing to remove {path}: it is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        path.unlink()
        return
    try:
        shutil.rmtree(path)
    except OSError:
        _make_removable(path)  # read-only trees (e.g. a Go module cache) need their modes opened up
        shutil.rmtree(path)


def _make_removable(path: Path) -> None:
    for dirpath, dirnames, _ in os.walk(path, followlinks=False):
        for d in [dirpath, *(os.path.join(dirpath, n) for n in dirnames)]:
            if not os.path.islink(d):
                with contextlib.suppress(OSError):
                    os.chmod(d, 0o700)


# --- retention ----------------------------------------------------------------------------


def retain_sessions(root: Path, name: str, dest: Path, log: Log = print) -> list[str]:
    """Copy the agent session records of ``<root>/<name>/home`` into ``dest/agent-home``.

    Returns the retained locations (relative to the HOME). A location is skipped, with a log
    line, when any component on the way to it (``<name>``, ``home``, ``.codex``, ``sessions``)
    is not a real directory. Symlinks, devices and the like inside it are skipped as well.
    """
    kept: list[str] = []
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except FileNotFoundError:
        return kept
    try:
        home_fd = _open_chain(root_fd, (name, HOME_DIR), log)
        if home_fd is None:
            return kept
        try:
            for parts in SESSION_DIRS:
                fd = _open_chain(home_fd, parts, log)
                if fd is None:
                    continue
                try:
                    out = dest / RETAINED_DIR / Path(*parts)
                    out.mkdir(parents=True, exist_ok=True)
                    _copy_dir(fd, out, log)
                    kept.append("/".join(parts))
                finally:
                    os.close(fd)
        finally:
            os.close(home_fd)
    finally:
        os.close(root_fd)
    return kept


def _open_chain(start_fd: int, parts: tuple[str, ...], log: Log) -> int | None:
    """Open ``parts`` below ``start_fd``, refusing symlinks at every step. None if absent."""
    fd = os.dup(start_fd)
    for i, part in enumerate(parts):
        try:
            nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
        except FileNotFoundError:
            os.close(fd)
            return None
        except OSError as e:  # ELOOP (symlink), ENOTDIR (file where a directory should be)
            log(f"  not retaining {'/'.join(parts[: i + 1])}: not a real directory ({e.strerror})")
            os.close(fd)
            return None
        os.close(fd)
        fd = nxt
    return fd


def _copy_dir(src_fd: int, dst: Path, log: Log) -> None:
    with os.scandir(src_fd) as it:
        entries = list(it)
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                sub = os.open(e.name, _DIR_FLAGS, dir_fd=src_fd)
                try:
                    (dst / e.name).mkdir(exist_ok=True)
                    _copy_dir(sub, dst / e.name, log)
                finally:
                    os.close(sub)
            elif stat.S_ISREG(st.st_mode):
                _copy_file(src_fd, e.name, dst / e.name)
            # else: a symlink, fifo, socket or device: never reproduced or dereferenced
        except OSError as err:
            log(f"  not retaining {e.name}: {err.strerror or err}")


def _copy_file(dir_fd: int, name: str, dst: Path) -> None:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as src:
        st = os.fstat(src.fileno())
        if not stat.S_ISREG(st.st_mode):
            return
        with dst.open("wb") as out:
            shutil.copyfileobj(src, out)
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))


# --- recovery -----------------------------------------------------------------------------


def entry_names(root: Path) -> list[str]:
    """Homes entries, as claimable names: directories and their lock files, and anything else."""
    try:
        listing = os.listdir(root)
    except FileNotFoundError:
        return []
    return sorted({n.removesuffix(LOCK_SUFFIX) for n in listing})


def name_of_home(root: Path, home: str | None) -> str | None:
    """The homes entry that ``home`` (a recorded ``<root>/<name>/home``) lives in, if any."""
    if not home:
        return None
    p = Path(os.path.abspath(home))
    if p.name == HOME_DIR and p.parent.parent == Path(os.path.abspath(root)):
        return p.parent.name
    return None
