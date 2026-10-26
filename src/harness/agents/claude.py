"""Claude Code via the Claude Agent SDK.

Parity with the CLI: Claude Code's preset system prompt and tool set, a permission mode
from config, and settings loaded only from the run's fresh ``CLAUDE_CONFIG_DIR`` (which
holds nothing but the variant bundle) and the repo itself. ``harness parity`` diffs an
SDK run's init frame against a CLI run's to confirm this.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from harness.agents.base import AgentInvocation, RawWriter
from harness.bundle import instructions_text
from harness.config import ClaudeAgentConfig
from harness.events import read_raw, to_jsonable
from harness.models import AgentKind, AgentTurnResult, ResolvedBundle


class ClaudeAdapter:
    kind = AgentKind.claude

    def setup_home(self, home: Path, variant: ResolvedBundle, cfg: ClaudeAgentConfig) -> None:
        config_dir = home / ".claude"
        config_dir.mkdir(parents=True, exist_ok=True)
        text = instructions_text(variant)
        if text:
            (config_dir / "CLAUDE.md").write_text(text)
        if variant.spec.skills_dir:
            src = Path(variant.path) / variant.spec.skills_dir
            shutil.copytree(src, config_dir / "skills", dirs_exist_ok=True)
        settings = {
            "autoMemoryEnabled": False,
            "includeCoAuthoredBy": True,
            "env": {"DISABLE_AUTOUPDATER": "1"},
        }
        (config_dir / "settings.json").write_text(json.dumps(settings, indent=2))
        # Skip first-run onboarding prompts in the fresh config dir.
        (config_dir / ".claude.json").write_text(
            json.dumps({"hasCompletedOnboarding": True, "autoUpdates": False})
        )

    def sync_auth(self, inv: AgentInvocation) -> None:
        pass  # the OAuth token comes from the environment and isn't refreshed by the run

    def auth_env(self, home: Path, cfg: ClaudeAgentConfig) -> dict[str, str]:
        env = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "DISABLE_AUTOUPDATER": "1"}
        if cfg.auth == "api_key":
            key = os.environ.get(cfg.api_key_env)
            if not key:
                raise RuntimeError(f"claude auth=api_key but ${cfg.api_key_env} is not set")
            env["ANTHROPIC_API_KEY"] = key
        else:
            token = os.environ.get(cfg.oauth_token_env)
            if not token:
                raise RuntimeError(
                    f"claude auth=subscription needs ${cfg.oauth_token_env} "
                    "(create one with `claude setup-token`)"
                )
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        return env

    def versions(self) -> dict[str, str]:
        import claude_agent_sdk

        out = {"claude_agent_sdk": claude_agent_sdk.__version__}
        try:
            from claude_agent_sdk._cli_version import __cli_version__

            out["claude_code_bundled"] = __cli_version__
        except ImportError:
            pass
        return out

    def options(self, inv: AgentInvocation) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        cfg = ClaudeAgentConfig.model_validate(inv.agent_config)
        stderr_log = Path(inv.run_dir) / f"claude-{inv.phase.value}-{inv.attempt}.cli.log"

        def on_stderr(line: str) -> None:
            with stderr_log.open("a") as f:
                f.write(line.rstrip() + "\n")

        return ClaudeAgentOptions(
            cwd=inv.workdir,
            system_prompt={"type": "preset", "preset": "claude_code"},
            tools={"type": "preset", "preset": "claude_code"},
            setting_sources=["user", "project", "local"],
            permission_mode=cfg.permission_mode,
            max_turns=inv.budget.max_turns,
            max_budget_usd=inv.budget.max_cost_usd,
            model=inv.model or cfg.model,
            resume=inv.resume_session,
            mcp_servers=inv.mcp_servers,
            cli_path=cfg.cli_path,
            output_format=(
                {"type": "json_schema", "schema": inv.output_schema} if inv.output_schema else None
            ),
            stderr=on_stderr,
        )

    def invoke(self, inv: AgentInvocation, out: RawWriter) -> AgentTurnResult:
        return asyncio.run(self._invoke(inv, out))

    async def _invoke(self, inv: AgentInvocation, out: RawWriter) -> AgentTurnResult:
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, query

        started = time.monotonic()
        result = AgentTurnResult(phase=inv.phase, attempt=inv.attempt, prompt=inv.prompt)
        last_text: str | None = None
        prior_cost = session_cost(inv.raw_path, inv.resume_session)
        async for msg in query(prompt=inv.prompt, options=self.options(inv)):
            out.write(type(msg).__name__, to_jsonable(msg))
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                sid = msg.data.get("session_id")
                if sid:
                    result.session_id = sid
                    inv.session_file.write_text(sid)
            elif isinstance(msg, AssistantMessage):
                texts = [b.text for b in msg.content if hasattr(b, "text")]
                if texts:
                    last_text = "\n".join(texts)
            elif isinstance(msg, ResultMessage):
                result.session_id = msg.session_id
                inv.session_file.write_text(msg.session_id)
                result.turns = msg.num_turns
                if msg.total_cost_usd is not None:
                    result.cost_usd = msg.total_cost_usd - prior_cost
                result.stop_reason = msg.terminal_reason or msg.subtype
                result.tokens = {
                    k: v for k, v in (msg.usage or {}).items() if isinstance(v, int)
                }
                if msg.structured_output is not None:
                    result.final_text = json.dumps(msg.structured_output)
                else:
                    result.final_text = msg.result or last_text
                if msg.subtype in ("error_max_turns", "error_max_budget_usd"):
                    result.status = "budget_exceeded"
                elif msg.is_error:
                    result.status = "error"
                    result.error = "; ".join(msg.errors or []) or msg.subtype
        result.duration_s = time.monotonic() - started
        return result


def session_cost(raw_path: Path, session_id: str | None) -> float:
    """The session's cumulative cost as of its last result, for resumed invocations.

    A resumed session reports ``total_cost_usd`` including every earlier invocation, while
    ``usage`` and ``max_budget_usd`` cover only the current one. Subtracting the last
    reported total gives this invocation's cost; spend from a killed invocation (which
    never reported) lands on the next one, so the per-turn costs still sum to the total.
    """
    if not session_id:
        return 0.0
    cost = 0.0
    for r in read_raw(raw_path):
        d = r.get("data") or {}
        if r.get("type") == "ResultMessage" and d.get("session_id") == session_id:
            cost = d.get("total_cost_usd") or 0.0
    return cost
