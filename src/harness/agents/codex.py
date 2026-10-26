"""Codex via the ``openai-codex`` SDK (``codex app-server`` over stdio).

The run's fresh ``CODEX_HOME`` holds the variant bundle (AGENTS.md, skills, MCP config),
memories off, and the auth state. The SDK reports tokens rather than cost, so the cost
budget uses notional prices from config.

Subscription auth copies ``auth.json`` into the run. ChatGPT refresh tokens are single-use,
so a token the run refreshes is copied back, or the next run's copy would fail to sign in.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from harness.agents.base import AgentInvocation, RawWriter
from harness.bundle import instructions_text
from harness.config import CodexAgentConfig
from harness.events import to_jsonable
from harness.models import AgentKind, AgentTurnResult, ResolvedBundle

# Items that count as a "turn" for the turn budget: one model action each.
_TURN_ITEMS = {"commandExecution", "fileChange", "mcpToolCall", "webSearch", "agentMessage"}


def _ints(d: dict | None) -> dict[str, int]:
    return {k: v for k, v in (d or {}).items() if isinstance(v, int)}


def _toml_value(v: object) -> str:
    return json.dumps(v)  # JSON strings, numbers, bools and arrays are valid TOML values


def _last_refresh(path: Path) -> datetime:
    try:
        dt = datetime.fromisoformat(json.loads(path.read_text())["last_refresh"])
    except (OSError, ValueError, KeyError, TypeError):
        return datetime.min.replace(tzinfo=UTC)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class CodexAdapter:
    kind = AgentKind.codex

    def setup_home(self, home: Path, variant: ResolvedBundle, cfg: CodexAgentConfig) -> None:
        codex_home = home / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        text = instructions_text(variant)
        if text:
            (codex_home / "AGENTS.md").write_text(text)
        if variant.spec.skills_dir:
            src = Path(variant.path) / variant.spec.skills_dir
            shutil.copytree(src, codex_home / "skills", dirs_exist_ok=True)
        lines = ["check_for_update_on_startup = false", "", "[features]", "memories = false", ""]
        for name, server in variant.spec.mcp.items():
            lines.append(f"[mcp_servers.{name}]")
            for k, v in server.items():
                lines.append(f"{k} = {_toml_value(v)}")
            lines.append("")
        (codex_home / "config.toml").write_text("\n".join(lines))
        if cfg.auth == "subscription":
            src = Path(os.path.expanduser(cfg.auth_json))
            if not src.exists():
                raise RuntimeError(f"codex auth=subscription needs {src} (run `codex login`)")
            shutil.copy2(src, codex_home / "auth.json")
            (codex_home / "auth.json").chmod(0o600)

    def sync_auth(self, inv: AgentInvocation) -> None:
        cfg = CodexAgentConfig.model_validate(inv.agent_config)
        if cfg.auth != "subscription":
            return
        copy = Path(inv.home) / ".codex" / "auth.json"
        src = Path(os.path.expanduser(cfg.auth_json))
        # Runs on other slots may sync at the same time; the newest refresh wins.
        with src.with_name(f".{src.name}.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if copy.exists() and _last_refresh(copy) > _last_refresh(src):
                tmp = src.with_name(f".{src.name}.tmp")
                shutil.copy2(copy, tmp)
                tmp.chmod(0o600)
                os.replace(tmp, src)

    def auth_env(self, home: Path, cfg: CodexAgentConfig) -> dict[str, str]:
        if cfg.auth == "api_key":
            key = os.environ.get(cfg.api_key_env)
            if not key:
                raise RuntimeError(f"codex auth=api_key but ${cfg.api_key_env} is not set")
            return {"OPENAI_API_KEY": key, "CODEX_API_KEY": key}
        return {}

    def versions(self) -> dict[str, str]:
        import openai_codex

        return {"openai_codex": openai_codex.__version__}

    def invoke(self, inv: AgentInvocation, out: RawWriter) -> AgentTurnResult:
        from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

        cfg = CodexAgentConfig.model_validate(inv.agent_config)
        sandbox = {
            "read-only": Sandbox.read_only,
            "workspace-write": Sandbox.workspace_write,
            "full-access": Sandbox.full_access,
        }[cfg.sandbox]
        started = time.monotonic()
        result = AgentTurnResult(phase=inv.phase, attempt=inv.attempt, prompt=inv.prompt)
        model = inv.model or cfg.model
        with Codex(CodexConfig(codex_bin=cfg.codex_bin, cwd=inv.workdir)) as codex:
            meta = codex.metadata
            out.write("harness_metadata", to_jsonable(meta))
            if cfg.auth == "api_key":
                codex.login_api_key(os.environ["OPENAI_API_KEY"])
            common = {"cwd": inv.workdir, "sandbox": sandbox, "model": model}
            if inv.resume_session:
                thread = codex.thread_resume(inv.resume_session, **common)
            else:
                thread = codex.thread_start(approval_mode=ApprovalMode.deny_all, **common)
            result.session_id = thread.id
            inv.session_file.write_text(thread.id)
            handle = thread.turn(inv.prompt, output_schema=inv.output_schema)
            turns = 0
            final_text = None
            prior: dict[str, int] | None = None
            for note in handle.stream():
                payload = to_jsonable(note.payload)
                # Streaming deltas are dropped: item/completed carries the full content.
                if not note.method.endswith(("/delta", "Delta")):
                    out.write(note.method, payload)
                if note.method == "item/completed":
                    item = payload.get("item", {}) if isinstance(payload, dict) else {}
                    if item.get("type") in _TURN_ITEMS:
                        turns += 1
                    if item.get("type") == "agentMessage":
                        final_text = item.get("text")
                elif note.method == "thread/tokenUsage/updated":
                    # Totals are per thread, so a resumed thread includes earlier turns.
                    # The thread total before this turn is the first total minus its delta.
                    usage = payload.get("tokenUsage") or {}
                    total = _ints(usage.get("total"))
                    if prior is None:
                        last = _ints(usage.get("last"))
                        prior = {k: total.get(k, 0) - last.get(k, 0) for k in total}
                    result.tokens = {k: v - prior.get(k, 0) for k, v in total.items()}
                    result.cost_usd = self._cost(result.tokens, cfg)
                elif note.method == "turn/completed":
                    turn = payload.get("turn", {})
                    result.stop_reason = turn.get("status")
                    if turn.get("status") == "failed":
                        result.status = "error"
                        result.error = (turn.get("error") or {}).get("message")
                over_turns = turns > inv.budget.max_turns
                over_cost = (result.cost_usd or 0) > inv.budget.max_cost_usd
                if (over_turns or over_cost) and result.status != "budget_exceeded":
                    result.status = "budget_exceeded"
                    result.stop_reason = "max_turns" if over_turns else "max_cost"
                    handle.interrupt()
            result.turns = turns
            result.final_text = final_text
        result.duration_s = time.monotonic() - started
        return result

    @staticmethod
    def _cost(tokens: dict[str, int], cfg: CodexAgentConfig) -> float:
        cached = tokens.get("cachedInputTokens", 0)
        uncached = max(tokens.get("inputTokens", 0) - cached, 0)
        out = tokens.get("outputTokens", 0)
        return (
            uncached * cfg.price_input + cached * cfg.price_cached_input + out * cfg.price_output
        ) / 1_000_000
