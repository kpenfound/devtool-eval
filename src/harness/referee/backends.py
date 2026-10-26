"""Referee model backends. The model is configurable; not every user has multiple vendors."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Any

from harness.config import HarnessConfig

DEFAULT_MODELS = {"claude_sdk": "claude-opus-5-5", "anthropic": "claude-opus-5-5"}


def model_name(config: HarnessConfig) -> str | None:
    return config.referee.model or DEFAULT_MODELS.get(config.referee.backend)


def complete(config: HarnessConfig, system: str, prompt: str, schema: dict[str, Any]) -> str:
    backend = config.referee.backend
    model = model_name(config)
    if backend == "openai" and not model:
        raise ValueError("referee.backend = 'openai' needs referee.model")
    if backend == "claude_sdk":
        return asyncio.run(_claude_sdk(config, system, prompt, schema, model))
    if backend == "anthropic":
        return _anthropic(system, prompt, model)
    if backend == "openai":
        return _openai(system, prompt, schema, model)
    if backend == "codex_sdk":
        return _codex_sdk(system, prompt, schema, model)
    raise ValueError(f"unknown referee backend {backend!r}")


async def _claude_sdk(config, system, prompt, schema, model) -> str:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    cfg = config.agents.claude
    env = {}
    if cfg.auth == "subscription" and os.environ.get(cfg.oauth_token_env):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ[cfg.oauth_token_env]
    with tempfile.TemporaryDirectory(prefix="referee-") as tmp:
        env["CLAUDE_CONFIG_DIR"] = tmp
        options = ClaudeAgentOptions(
            cwd=tmp,
            system_prompt=system,
            tools=[],
            setting_sources=[],
            model=model,
            max_turns=4,
            env=env,
            output_format={"type": "json_schema", "schema": schema},
        )
        result = None
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, ResultMessage):
                if msg.is_error:
                    raise RuntimeError(f"referee call failed: {msg.subtype} {msg.errors}")
                result = (
                    json.dumps(msg.structured_output)
                    if msg.structured_output is not None
                    else msg.result
                )
        if result is None:
            raise RuntimeError("referee returned no result")
        return result


def _anthropic(system, prompt, model) -> str:
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=32_000,
        system=system + "\n\nRespond with only the JSON object, no prose.",
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _openai(system, prompt, schema, model) -> str:
    import openai

    client = openai.OpenAI()
    resp = client.responses.create(
        model=model,
        instructions=system,
        input=prompt,
        text={"format": {"type": "json_schema", "name": "scorecard", "schema": schema, "strict": False}},
    )
    return resp.output_text


def _codex_sdk(system, prompt, schema, model) -> str:
    from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

    with tempfile.TemporaryDirectory(prefix="referee-") as tmp:
        with Codex(CodexConfig(cwd=tmp)) as codex:
            thread = codex.thread_start(
                approval_mode=ApprovalMode.deny_all,
                sandbox=Sandbox.read_only,
                base_instructions=system,
                model=model,
                ephemeral=True,
                cwd=tmp,
            )
            result = thread.run(prompt, output_schema=schema)
            return result.final_response or ""
