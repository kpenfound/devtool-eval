"""Dagger environment: pinned CLI install, Dagger Cloud login state, engine and cache reset."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from harness.isolation import run
from harness.pack import RunContext, ToolInstall
from harness_dagger.common import dagger

INSTALL_SCRIPT = "https://dl.dagger.io/dagger/install.sh"
VERSION_RE = re.compile(r"version:\s*(v\S+)|dagger (v\d\S*)")


def installed_version(env: dict[str, str]) -> str | None:
    p = dagger(["version"], cwd=None, env=env, timeout=120)
    m = VERSION_RE.search(p.stdout + p.stderr)
    return (m.group(1) or m.group(2)) if m else None


class DaggerEnvironment:
    """Pins the CLI version for a run.

    Preferred: a release selector env var (``DAGGER_X_RELEASE`` by default) understood by the
    installed dagger binary, which downloads and runs the requested release. With no version
    requested, the harness's own selector value (if any) carries over, so runs match the
    host's shell. Fallback when the selector doesn't take: the official install script into a
    versioned directory prepended to PATH.
    """

    def __init__(self, settings: dict):
        self.settings = settings
        self.selector = settings.get("release_env", "DAGGER_X_RELEASE")

    def selector_env(self, version: str | None) -> dict[str, str]:
        want = version or os.environ.get(self.selector)
        return {self.selector: want} if self.selector and want else {}

    def install(self, version: str | None, ctx: RunContext) -> ToolInstall:
        sel = self.selector_env(version)
        current = installed_version({**ctx.env, **sel})
        if version is None or _same(version, current):
            if current is None:
                raise RuntimeError("dagger is not installed on PATH")
            return ToolInstall(version=current, env=sel)
        bin_dir = ctx.config.cache / "tools" / "dagger" / version
        if not (bin_dir / "dagger").exists():
            bin_dir.mkdir(parents=True, exist_ok=True)
            ctx.log(f"installing dagger {version} into {bin_dir}")
            run(
                f"curl -fsSL {INSTALL_SCRIPT} | BIN_DIR={bin_dir} DAGGER_VERSION={version.lstrip('v')} sh",
                env={**os.environ},
                timeout=600,
            )
        env = {k: v for k, v in ctx.env.items() if k != self.selector}
        env["PATH"] = f"{bin_dir}{os.pathsep}{ctx.env['PATH']}"
        got = installed_version(env)
        if not _same(version, got):
            raise RuntimeError(f"wanted dagger {version}, got {got}")
        return ToolInstall(version=got or version, path_prepend=[str(bin_dir)])

    def prepare(self, ctx: RunContext) -> dict[str, str]:
        """Ambient Dagger Cloud login when the run's cell has Cloud available; never prompted."""
        if not ctx.meta.spec.cloud:
            return {}
        cloud = ctx.config.cloud
        env: dict[str, str] = {}
        if cloud.token_env and os.environ.get(cloud.token_env):
            env["DAGGER_CLOUD_TOKEN"] = os.environ[cloud.token_env]
        if cloud.config_dir:
            src = Path(os.path.expanduser(cloud.config_dir))
            shutil.copytree(src, ctx.home / ".config" / "dagger", dirs_exist_ok=True)
        if not env and not cloud.config_dir:
            raise RuntimeError(
                "run has cloud=True but no Dagger Cloud login is configured "
                f"(set ${cloud.token_env} or cloud.config_dir), or pass --no-cloud"
            )
        return env

    def reset(self, ctx: RunContext) -> None:
        mode = self.settings.get("engine_reset", "prune")
        if mode == "none":
            return
        names = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=dagger-engine", "--format", "{{.Names}}"],
            capture_output=True, text=True, env=ctx.env, check=False,
        ).stdout.split()
        for name in names:
            if mode == "restart":
                subprocess.run(["docker", "restart", name], env=ctx.env, capture_output=True, check=False)
            else:
                # -v drops the engine's anonymous cache volume with it.
                subprocess.run(["docker", "rm", "-f", "-v", name], env=ctx.env, capture_output=True, check=False)
        ctx.log(f"engine reset ({mode}): {', '.join(names) or 'no engine containers'}")


def _same(a: str | None, b: str | None) -> bool:
    return bool(a and b and a.lstrip("v") == b.lstrip("v"))
