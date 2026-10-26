"""Agent adapter interface and the out-of-process invocation used for hard kills.

Each agent invocation (task, each retry nudge, the feedback turn) runs in a worker
subprocess started in its own session with the run's scrubbed environment. The SDKs spawn
the agent CLIs as children of that worker, so the wall-clock budget is enforced by killing
the whole process group. Turn and cost budgets are enforced inside the worker.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from harness.models import AgentKind, AgentTurnResult, Budget, Phase, ResolvedBundle


class AgentInvocation(BaseModel):
    agent: AgentKind
    phase: Phase
    attempt: int = 0
    prompt: str
    resume_session: str | None = None
    workdir: str
    home: str
    run_dir: str
    env: dict[str, str]
    budget: Budget
    model: str | None = None
    agent_config: dict[str, Any] = Field(default_factory=dict)
    mcp_servers: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    python: str | None = None  # worker interpreter; see isolation.neutral_python

    @property
    def raw_path(self) -> Path:
        return Path(self.run_dir) / "transcript.raw.jsonl"

    @property
    def session_file(self) -> Path:
        return Path(self.run_dir) / "session_id"


class RawWriter:
    """Appends raw SDK messages to the run's transcript, numbering them across invocations."""

    def __init__(self, inv: AgentInvocation):
        self.inv = inv
        self.path = inv.raw_path
        self.seq = sum(1 for _ in self.path.open()) if self.path.exists() else 0
        self._f = self.path.open("a")

    def write(self, typ: str, data: Any) -> None:
        self.seq += 1
        line = {
            "seq": self.seq,
            "ts": time.time(),
            "agent": self.inv.agent.value,
            "phase": self.inv.phase.value,
            "attempt": self.inv.attempt,
            "type": typ,
            "data": data,
        }
        self._f.write(json.dumps(line, default=str) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


class AgentAdapter(Protocol):
    kind: AgentKind

    def setup_home(self, home: Path, variant: ResolvedBundle, cfg: Any) -> None:
        """Write the variant bundle (instructions, skills, MCP) into the fresh config dir."""

    def auth_env(self, home: Path, cfg: Any) -> dict[str, str]:
        """Env vars for subscription or API-key auth, per config."""

    def invoke(self, inv: AgentInvocation, out: RawWriter) -> AgentTurnResult:
        """Run one turn in-process (called inside the worker)."""

    def sync_auth(self, inv: AgentInvocation) -> None:
        """After a turn, carry credentials the agent refreshed back to their source."""

    def versions(self) -> dict[str, str]: ...


def get_adapter(kind: AgentKind | str) -> AgentAdapter:
    kind = AgentKind(kind)
    if kind == AgentKind.claude:
        from harness.agents.claude import ClaudeAdapter

        return ClaudeAdapter()
    from harness.agents.codex import CodexAdapter

    return CodexAdapter()


KILL_GRACE_S = 15


def run_invocation(inv: AgentInvocation) -> AgentTurnResult:
    """Run an invocation in a worker process group, killing it at the wall-clock budget."""
    run_dir = Path(inv.run_dir)
    tag = f"{inv.phase.value}-{inv.attempt}"
    # The worker's argv is visible to the agent (ps), and the run dir's name includes the
    # variant, so the spec and result files live next to the run's neutral HOME instead.
    scratch = Path(inv.home).parent
    spec_path = scratch / f"invocation-{tag}.json"
    result_path = scratch / f"result-{tag}.json"
    spec_path.write_text(inv.model_dump_json())
    result_path.unlink(missing_ok=True)
    started = time.monotonic()
    with (run_dir / f"agent-{tag}.stderr.log").open("w") as stderr:
        # -P: never import from the repo checkout we're running in. No PYTHONPATH either,
        # since the agent's own commands inherit this environment.
        proc = subprocess.Popen(
            [inv.python or sys.executable, "-P", "-m", "harness.agents.worker", str(spec_path), str(result_path)],
            cwd=inv.workdir,
            env=inv.env,
            stdout=stderr,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        status = None
        try:
            proc.wait(timeout=inv.budget.wall_clock_s)
        except subprocess.TimeoutExpired:
            status = "killed"
            _kill_group(proc)
    spec_path.unlink(missing_ok=True)
    try:
        get_adapter(inv.agent).sync_auth(inv)  # in the harness process, with the real HOME
    except OSError as e:
        print(f"warning: couldn't sync {inv.agent.value} auth back: {e}", file=sys.stderr)
    session = inv.session_file.read_text().strip() if inv.session_file.exists() else None
    if status is None and result_path.exists():
        result = AgentTurnResult.model_validate_json(result_path.read_text())
        result_path.unlink()
        return result
    return AgentTurnResult(
        phase=inv.phase,
        attempt=inv.attempt,
        prompt=inv.prompt,
        session_id=session or inv.resume_session,
        status="killed" if status == "killed" else "error",
        stop_reason="wall_clock" if status == "killed" else "worker_exit",
        duration_s=time.monotonic() - started,
        error=None if status == "killed" else f"worker exited with code {proc.returncode}",
    )


def _kill_group(proc: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, KILL_GRACE_S), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue
