"""CLI vs SDK parity check for Claude Code.

Runs the same short prompt through the ``claude`` CLI (stream-json) and through the SDK
adapter, in identical fresh config dirs, and diffs the init frames: tools, model,
permission mode, MCP servers, skills, slash commands, and so on. Differences mean SDK
runs would not reflect what a real user's CLI does.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from harness.agents.base import AgentInvocation, run_invocation
from harness.agents.claude import ClaudeAdapter
from harness.bundle import resolve_variant
from harness.config import HarnessConfig
from harness.events import read_raw
from harness.isolation import base_env, make_home, neutral_python
from harness.models import AgentKind, Budget, Phase

COMPARE = (
    "tools",
    "model",
    "permissionMode",
    "mcp_servers",
    "skills",
    "slash_commands",
    "agents",
    "output_style",
    "apiKeySource",
    "claude_code_version",
    "plugins",
)
PROMPT = "Reply with the single word OK."


def _strip(init: dict) -> dict:
    return {k: init.get(k) for k in COMPARE}


def claude_parity(config: HarnessConfig, bundle_dir: Path, workdir: Path, cli: str = "claude") -> dict:
    root = config.state / "parity" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    adapter = ClaudeAdapter()
    variant = resolve_variant(bundle_dir)
    cfg = config.agents.claude
    gh = config.github
    results = {}
    for side in ("cli", "sdk"):
        run_dir = root / side
        run_dir.mkdir(parents=True)
        home = make_home(run_dir, config)
        env = base_env(home, config, git_name=gh.bot_name, git_email=gh.bot_email)
        adapter.setup_home(home, variant, cfg)
        env.update(adapter.auth_env(home, cfg))
        if side == "cli":
            cmd = [
                shutil.which(cli, path=env["PATH"]) or cli, "-p", PROMPT,
                "--output-format", "stream-json", "--verbose",
                "--permission-mode", cfg.permission_mode, "--max-turns", "2",
            ]
            if cfg.model:
                cmd += ["--model", cfg.model]
            out = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=300)
            (run_dir / "stream.jsonl").write_text(out.stdout)
            init = next(
                (json.loads(line) for line in out.stdout.splitlines()
                 if line.startswith("{") and '"subtype":"init"' in line.replace(" ", "")),
                {},
            )
        else:
            inv = AgentInvocation(
                agent=AgentKind.claude, phase=Phase.task, prompt=PROMPT, workdir=str(workdir),
                home=str(home), run_dir=str(run_dir), env=env,
                budget=Budget(wall_clock_s=300, max_turns=2, max_cost_usd=1.0),
                agent_config=cfg.model_dump(), mcp_servers=variant.spec.mcp,
                python=neutral_python(config.state),
            )
            run_invocation(inv)
            init = next(
                (r["data"].get("data", {}) for r in read_raw(inv.raw_path)
                 if r["type"] == "SystemMessage" and r["data"].get("subtype") == "init"),
                {},
            )
        results[side] = _strip(init)
    diffs = {
        k: {"cli": results["cli"].get(k), "sdk": results["sdk"].get(k)}
        for k in COMPARE
        if results["cli"].get(k) != results["sdk"].get(k)
    }
    report = {"dir": str(root), "match": not diffs, "diffs": diffs, "cli": results["cli"], "sdk": results["sdk"]}
    (root / "parity.json").write_text(json.dumps(report, indent=2))
    return report
