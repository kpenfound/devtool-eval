"""Harness configuration, loaded from ``harness.toml`` (see ``harness.example.toml``)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from harness.models import Budget

DEFAULT_CONFIG = "harness.toml"


class ClaudeAgentConfig(BaseModel):
    auth: Literal["subscription", "api_key"] = "subscription"
    # Subscription auth with a fresh config dir needs a long-lived token from `claude setup-token`.
    oauth_token_env: str = "CLAUDE_CODE_OAUTH_TOKEN"
    api_key_env: str = "ANTHROPIC_API_KEY"
    model: str | None = None
    permission_mode: str = "bypassPermissions"
    cli_path: str | None = None  # None: the CLI bundled with claude-agent-sdk


class CodexAgentConfig(BaseModel):
    auth: Literal["subscription", "api_key"] = "subscription"
    # Subscription auth: auth.json from a CODEX_HOME that has run `codex login`.
    auth_json: str = "~/.codex/auth.json"
    api_key_env: str = "OPENAI_API_KEY"
    model: str | None = None
    sandbox: Literal["read-only", "workspace-write", "full-access"] = "full-access"
    codex_bin: str | None = None
    # USD per 1M tokens, for notional cost budgets (the SDK reports tokens, not cost).
    price_input: float = 1.25
    price_cached_input: float = 0.125
    price_output: float = 10.0


class AgentsConfig(BaseModel):
    claude: ClaudeAgentConfig = Field(default_factory=ClaudeAgentConfig)
    codex: CodexAgentConfig = Field(default_factory=CodexAgentConfig)


class SlotConfig(BaseModel):
    name: str  # slot-01
    repo: str | None = None  # owner/slot-01 on GitHub; None for local-only runs


class GitHubConfig(BaseModel):
    org: str | None = None
    app_id: int | None = None
    installation_id: int | None = None
    private_key_path: str | None = None
    # The agent's git identity. Agents see it, so it shouldn't name the eval.
    bot_name: str = "Developer"
    bot_email: str = "developer@users.noreply.github.com"
    # Permissions granted to the agent's slot-scoped token.
    agent_permissions: dict[str, str] = Field(
        default_factory=lambda: {
            "contents": "write",
            "pull_requests": "write",
            "workflows": "write",
            "metadata": "read",
            "checks": "read",
            "statuses": "read",
        }
    )

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.installation_id and self.private_key_path)


class IsolationConfig(BaseModel):
    # Entries of the real HOME made visible inside the run's fake HOME (symlinked).
    home_passthrough: list[str] = Field(default_factory=lambda: [".docker", ".orbstack"])
    # Env vars inherited from the harness process; everything else is dropped.
    env_passthrough: list[str] = Field(
        default_factory=lambda: [
            "PATH",
            "LANG",
            "LC_ALL",
            "TERM",
            "SHELL",
            "USER",
            "LOGNAME",
            "TMPDIR",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "SSL_CERT_FILE",
        ]
    )
    # Shared caches (uv, go, npm, dagger CLI downloads) across runs. Engine cache is reset
    # separately by the pack. Set to false for fully cold runs.
    share_caches: bool = True


class RefereeConfig(BaseModel):
    backend: Literal["claude_sdk", "anthropic", "openai", "codex_sdk"] = "claude_sdk"
    model: str | None = None  # None: the backend's default (see referee.backends.DEFAULT_MODELS)
    max_event_chars: int = 1500
    max_prompt_chars: int = 600_000


class CloudConfig(BaseModel):
    """Ambient tool-cloud login for the "cloud available" cell. Packs interpret this."""

    token_env: str | None = "DAGGER_CLOUD_TOKEN"
    # Alternative to a token: a pre-logged-in tool config dir copied into the run's HOME.
    config_dir: str | None = None
    org: str | None = None


class HarnessConfig(BaseModel):
    # Agents see paths under here (their working copy and HOME), so keep the name neutral.
    state_dir: str = "~/.local/share/workspaces"
    runs_dir: str | None = None  # default: <state_dir>/runs
    work_dir: str | None = None  # default: <state_dir>/work
    db_path: str | None = None  # default: <state_dir>/harness.duckdb
    default_pack: str = "dagger"
    retries: int = 2
    budget: Budget = Field(default_factory=Budget)
    feedback_budget: Budget = Field(
        default_factory=lambda: Budget(wall_clock_s=600, max_turns=10, max_cost_usd=2.0)
    )
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    slots: list[SlotConfig] = Field(default_factory=lambda: [SlotConfig(name="local-01")])
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    isolation: IsolationConfig = Field(default_factory=IsolationConfig)
    referee: RefereeConfig = Field(default_factory=RefereeConfig)
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    # Pack-specific settings, keyed by pack name.
    packs: dict[str, dict] = Field(default_factory=dict)

    def _dir(self, value: str | None, default: str) -> Path:
        p = Path(os.path.expanduser(value)) if value else self.state / default
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def state(self) -> Path:
        p = Path(os.path.expanduser(self.state_dir))
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def runs(self) -> Path:
        return self._dir(self.runs_dir, "runs")

    @property
    def work(self) -> Path:
        return self._dir(self.work_dir, "work")

    @property
    def cache(self) -> Path:
        return self._dir(None, "cache")

    @property
    def db(self) -> Path:
        return Path(os.path.expanduser(self.db_path)) if self.db_path else self.state / "harness.duckdb"

    def pack_settings(self, pack: str) -> dict:
        return self.packs.get(pack, {})


def load_config(path: str | Path | None = None) -> HarnessConfig:
    path = Path(path or os.environ.get("HARNESS_CONFIG", DEFAULT_CONFIG))
    if not path.exists():
        return HarnessConfig()
    with path.open("rb") as f:
        return HarnessConfig.model_validate(tomllib.load(f))
