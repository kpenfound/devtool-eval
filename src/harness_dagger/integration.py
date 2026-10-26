"""Cloud Checks integration hooks: enable, verify by commit SHA, teardown.

The GitHub side (Dagger's GitHub integration on the test org) is installed once, by hand,
ahead of time. Per run, the harness only looks at whether the agent turned Cloud Checks on
for the slot and whether they went green on the commits it pushed.

Open item (docs/DESIGN.md): confirm ``dagger cloud checks list <sha>`` resolves a commit
SHA (the CLI calls the argument "version"), and pin down machine-readable output; for now
this relies on exit codes and keeps raw output in the run bundle.
"""

from __future__ import annotations

import os
from pathlib import Path

from harness.models import IntegrationResult
from harness.pack import RunContext
from harness_dagger.common import dagger

# `dagger cloud checks status` prints a bare state word; "unconfigured" was observed for a
# repo with no Cloud Checks. Unknown words leave `enabled` as None with the raw output kept.
ON_STATES = {"on", "enabled", "active"}
OFF_STATES = {"off", "disabled", "unconfigured"}


class CloudChecks:
    name = "cloud_checks"

    def __init__(self, settings: dict):
        self.wait_s = settings.get("cloud_wait_s", 1800)
        self.interval = settings.get("cloud_poll_interval", "15s")

    def _env(self, ctx: RunContext) -> dict[str, str]:
        """Harness-side Cloud credentials, independent of whether the agent had them."""
        env = dict(ctx.env)
        token_env = ctx.config.cloud.token_env
        if token_env and os.environ.get(token_env):
            env["DAGGER_CLOUD_TOKEN"] = os.environ[token_env]
        if ctx.config.cloud.config_dir:
            env["XDG_CONFIG_HOME"] = str(Path(os.path.expanduser(ctx.config.cloud.config_dir)).parent)
        return env

    def _where(self, ctx: RunContext) -> tuple[Path | None, list[str]]:
        if ctx.workdir.exists():
            return ctx.workdir, []
        return None, ["-W", f"github.com/{ctx.slot_repo}"]

    def enable(self, ctx: RunContext) -> None:
        cwd, w = self._where(ctx)
        p = dagger(["cloud", "checks", "on", *w], cwd=cwd, env=self._env(ctx), timeout=120)
        if p.returncode:
            raise RuntimeError(f"dagger cloud checks on failed: {p.stderr or p.stdout}")

    def verify(self, ctx: RunContext, shas: list[str]) -> IntegrationResult:
        if not ctx.slot_repo:
            return IntegrationResult(name=self.name, error="local-only slot: no integration")
        cwd, w = self._where(ctx)
        env = self._env(ctx)
        status = dagger(["cloud", "checks", "status", *w], cwd=cwd, env=env, timeout=120)
        text = status.stdout + status.stderr
        enabled = None
        state = text.strip().split()[0].lower() if text.strip() else ""
        if status.returncode == 0 and state in ON_STATES:
            enabled = True
        elif status.returncode == 0 and state in OFF_STATES:
            enabled = False
        details: dict = {"status": {"exit": status.returncode, "output": text[-4000:]}, "shas": {}}
        green_all: bool | None = None
        if enabled and shas:
            green_all = True
            for sha in shas:
                p = dagger(
                    ["cloud", "checks", "list", sha, "--watch", f"--interval={self.interval}", *w],
                    cwd=cwd, env=env, timeout=self.wait_s,
                )
                details["shas"][sha] = {"exit": p.returncode, "output": (p.stdout + p.stderr)[-8000:]}
                green_all = green_all and p.returncode == 0
        return IntegrationResult(name=self.name, enabled=enabled, green=green_all, shas=shas, details=details)

    def teardown(self, ctx: RunContext) -> None:
        if not ctx.slot_repo:
            return
        cwd, w = self._where(ctx)
        dagger(["cloud", "checks", "off", *w], cwd=cwd, env=self._env(ctx), timeout=120)
