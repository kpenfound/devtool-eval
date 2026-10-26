"""Per-run isolation on a shared host.

Every run gets a fake HOME under its run directory, so agent memory, user-level
instructions, gh/git credentials, and tool logins from the real account never leak in.
``CLAUDE_CONFIG_DIR`` and ``CODEX_HOME`` live inside it. Only an allowlist of env vars is
inherited from the harness process.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from harness.config import HarnessConfig


def make_home(parent: Path, config: HarnessConfig) -> Path:
    """A fresh HOME under ``parent``. Agent-visible paths must not name the run or variant,
    so runs put it under ``<state>/homes/<random>`` rather than inside the run bundle."""
    home = parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    real_home = Path.home()
    for name in config.isolation.home_passthrough:
        src = real_home / name
        dst = home / name
        if src.exists() and not dst.exists():
            dst.symlink_to(src)
    for d in (".config", ".cache", ".local/share", ".claude", ".codex", ".config/gh", "tmp"):
        (home / d).mkdir(parents=True, exist_ok=True)
    return home


def _harness_dirs() -> list[Path]:
    """The harness's venv and checkout: agent-visible paths under them name the harness."""
    import harness

    return [Path(sys.prefix).resolve(), Path(harness.__file__).resolve().parents[2]]


def clean_path(path: str) -> str:
    """PATH without the harness's own venv (``uv run`` prepends it), which would also hand
    the agent the harness's python, pytest and ruff."""
    dirs = _harness_dirs()
    keep = [d for d in path.split(os.pathsep) if d and not any(Path(d).resolve().is_relative_to(h) for h in dirs)]
    return os.pathsep.join(keep)


def _ensure_link(link: Path, target: Path) -> None:
    if link.is_symlink() and Path(os.readlink(link)) == target:
        return
    tmp = link.with_name(f".{link.name}.{os.getpid()}")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target)
    os.replace(tmp, link)


def neutral_python(state: Path) -> str:
    """This interpreter, reached through ``<state>/runtime`` rather than the harness's venv.

    Agents can see the worker's command line (``ps``) and the paths of the SDK-bundled CLIs
    it starts, which the SDKs find relative to their own package. Under the real venv, both
    name the harness checkout. The runtime dir is a venv that shares the real one's
    packages through ``lib`` symlinks.
    """
    venv = Path(sys.prefix)
    if venv == Path(sys.base_prefix):
        return sys.executable
    rt = state / "runtime"
    (rt / "bin").mkdir(parents=True, exist_ok=True)
    cfg = "".join(
        line for line in (venv / "pyvenv.cfg").read_text().splitlines(keepends=True)
        if not line.startswith("prompt")
    )
    cfg_path = rt / "pyvenv.cfg"
    if not cfg_path.exists() or cfg_path.read_text() != cfg:
        tmp = rt / f".pyvenv.cfg.{os.getpid()}"
        tmp.write_text(cfg)
        os.replace(tmp, cfg_path)
    _ensure_link(rt / "bin" / "python", Path(os.path.realpath(sys.executable)))
    for lib in ("lib", "lib64"):
        if (venv / lib).exists() and not (venv / lib).is_symlink():
            _ensure_link(rt / lib, venv / lib)
    return str(rt / "bin" / "python")


def base_env(home: Path, config: HarnessConfig, *, git_name: str, git_email: str) -> dict[str, str]:
    env = {k: os.environ[k] for k in config.isolation.env_passthrough if k in os.environ}
    env["PATH"] = clean_path(env.get("PATH") or "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    gitconfig = home / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text(
            f"[user]\n\tname = {git_name}\n\temail = {git_email}\n"
            "[init]\n\tdefaultBranch = main\n"
            "[advice]\n\tdetachedHead = false\n"
        )
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "TMPDIR": str(home / "tmp"),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GH_CONFIG_DIR": str(home / ".config/gh"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "CODEX_HOME": str(home / ".codex"),
        }
    )
    if config.isolation.share_caches:
        shared = config.cache / "shared"
        for name in ("uv", "go-build", "pip", "npm", "dagger"):
            (shared / name).mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "UV_CACHE_DIR": str(shared / "uv"),
                "GOCACHE": str(shared / "go-build"),
                "PIP_CACHE_DIR": str(shared / "pip"),
                "npm_config_cache": str(shared / "npm"),
            }
        )
        dagger_cache = home / ".cache/dagger"
        if not dagger_cache.exists():
            dagger_cache.symlink_to(shared / "dagger")
    return env


def git_credential_env(token: str) -> dict[str, str]:
    """Git auth for github.com from a token, without writing it into any repo config."""
    helper = "!f() { echo username=x-access-token; echo \"password=$GH_TOKEN\"; }; f"
    return {
        "GH_TOKEN": token,
        "GITHUB_TOKEN": token,
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_1": helper,
    }


def run(
    cmd: list[str] | str,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: float | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """subprocess.run with text output; env is the *complete* environment when given."""
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        check=check,
        timeout=timeout,
        text=True,
        capture_output=capture,
        shell=isinstance(cmd, str),
    )


def which(name: str, env: dict[str, str]) -> str | None:
    return shutil.which(name, path=env.get("PATH"))
