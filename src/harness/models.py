"""Core schemas shared by every stage. Everything on disk in a run bundle is one of these."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


# --- Corpus -------------------------------------------------------------------------------


class OutOfScopeReason(StrEnum):
    cd = "cd"  # Cloud Checks doesn't do CD
    non_linux = "non-linux"
    automation = "automation"  # repo bots (issue locking etc.), not CI


class CIJob(BaseModel):
    """One job from the corpus repo's existing CI."""

    workflow: str  # e.g. .github/workflows/tests.yaml
    job: str
    summary: str = ""
    out_of_scope: OutOfScopeReason | None = None
    # Parts of an in-scope job that are out of scope, e.g. Windows/macOS matrix entries.
    partial_out_of_scope: list[PartialOutOfScope] = Field(default_factory=list)
    note: str = ""


class PartialOutOfScope(BaseModel):
    match: str
    reason: OutOfScopeReason


class CorpusEntry(BaseModel):
    name: str
    url: str
    sha: str | None = None  # pinned SHA; None until pinned
    stack: list[str] = Field(default_factory=list)
    tier: str = "1"
    tests: str = ""
    ci: list[CIJob] = Field(default_factory=list)
    expected_coverage: list[str] = Field(default_factory=list)
    expected_coverage_raw: str = ""  # tool output the coverage was captured from
    # Hand-reviewed false positives in the tool's recommendation: module -> why. Capture
    # leaves these out of expected_coverage.
    coverage_exclude: dict[str, str] = Field(default_factory=dict)
    # Recommended modules for dev tools the repo uses but its CI doesn't run: module -> why.
    # Not required (not in expected_coverage); covering them is credit, since the tool's
    # setup brings the same checks to local dev and CI.
    bonus_coverage: dict[str, str] = Field(default_factory=dict)
    # Pack-specific extras (corpus extensions).
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.sha is not None


# --- Bundles ------------------------------------------------------------------------------


class BundleSpec(BaseModel):
    """A versioned variant config bundle: skills, instructions, MCP config, docs pointers."""

    name: str
    version: str = "1"
    description: str = ""
    # Files are relative to the bundle directory.
    instructions: str | None = None  # becomes CLAUDE.md / AGENTS.md at user level
    skills_dir: str | None = None  # contains <skill>/SKILL.md
    mcp: dict[str, Any] = Field(default_factory=dict)  # name -> server config
    docs_pointers: list[str] = Field(default_factory=list)


class ResolvedBundle(BaseModel):
    spec: BundleSpec
    path: str
    content_hash: str
    # Strings the referee must never see (variant name, skill names, skill content lines).
    redactions: list[str] = Field(default_factory=list)


# --- Runs ---------------------------------------------------------------------------------


class AgentKind(StrEnum):
    claude = "claude"
    codex = "codex"


class RunSpec(BaseModel):
    repo: str
    bundle: str
    agent: AgentKind
    pack: str = "dagger"
    tool_version: str | None = None  # None: whatever the environment has; recorded either way
    cloud: bool = True
    retries: int | None = None  # None: config default
    slot: str | None = None  # None: any free slot
    model: str | None = None


class Budget(BaseModel):
    wall_clock_s: int = 3600
    max_turns: int = 200
    max_cost_usd: float = 25.0


class Phase(StrEnum):
    task = "task"
    retry = "retry"
    feedback = "feedback"


class AgentTurnResult(BaseModel):
    """Outcome of one agent invocation (initial task, one retry nudge, or the feedback turn)."""

    phase: Phase
    attempt: int = 0
    prompt: str
    session_id: str | None = None
    status: Literal["completed", "error", "budget_exceeded", "killed"] = "completed"
    stop_reason: str | None = None
    final_text: str | None = None
    turns: int = 0
    cost_usd: float | None = None
    tokens: dict[str, int] = Field(default_factory=dict)
    duration_s: float = 0.0
    error: str | None = None
    started_at: datetime = Field(default_factory=utcnow)


class VerifyResult(BaseModel):
    passed: bool
    exit_code: int | None
    checks: list[str] = Field(default_factory=list)  # which checks exist
    failed_checks: list[str] = Field(default_factory=list)
    log_path: str = "verify.log"
    error_excerpt: str = ""  # what the simulated user feeds back
    duration_s: float = 0.0
    details: dict[str, Any] = Field(default_factory=dict)
    attempt: int = 0


class Nudge(BaseModel):
    attempt: int
    message: str
    verify_exit_code: int | None


class IntegrationResult(BaseModel):
    name: str
    enabled: bool | None = None  # None: unknown
    green: bool | None = None
    shas: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class RunStatus(StrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"  # harness error, not agent failure
    crashed = "crashed"  # found dead on a later invocation


class RunMeta(BaseModel):
    """run.json: metadata and versions."""

    run_id: str
    spec: RunSpec
    status: RunStatus = RunStatus.running
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    slot: str | None = None
    repo_sha: str | None = None
    bundle_version: str | None = None
    bundle_hash: str | None = None
    budget: Budget = Field(default_factory=Budget)
    versions: dict[str, str] = Field(default_factory=dict)  # harness, pack, tool, agent cli/sdk, ...
    session_id: str | None = None
    home: str | None = None  # the run's fake HOME (outside the bundle, see isolation.make_home)
    turns: list[AgentTurnResult] = Field(default_factory=list)
    verifies: list[VerifyResult] = Field(default_factory=list)
    nudges: list[Nudge] = Field(default_factory=list)
    pushed_shas: list[str] = Field(default_factory=list)
    integration: list[IntegrationResult] = Field(default_factory=list)
    error: str | None = None
    host: str | None = None

    @property
    def final_verify(self) -> VerifyResult | None:
        return self.verifies[-1] if self.verifies else None


# --- Feedback turn ------------------------------------------------------------------------


class FrictionPoint(BaseModel):
    description: str
    where: str = ""  # docs page, command, API, error message
    cause_guess: str = ""


class FeedbackReport(BaseModel):
    """The agent's self-report, asked for only after the task."""

    summary: str = ""
    difficulty: int | None = None  # 1 (trivial) .. 5 (very hard)
    what_i_set_up: list[str] = Field(default_factory=list)
    what_worked: list[str] = Field(default_factory=list)
    friction: list[FrictionPoint] = Field(default_factory=list)
    docs_used: list[str] = Field(default_factory=list)
    docs_missing: list[str] = Field(default_factory=list)
    confusing_errors: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    ci_recommendation: str = ""
    raw_text: str = ""
    parse_error: str | None = None


# --- Measurement --------------------------------------------------------------------------


class Fact(BaseModel):
    """A deterministic analyzer output."""

    key: str
    value: Any
    detail: str = ""
    evidence: list[EvidenceRef] = Field(default_factory=list)


class EvidenceRef(BaseModel):
    """Either a transcript event (``event``) or a file location (``path`` + ``line``)."""

    event: str | None = None
    path: str | None = None
    line: int | None = None
    quote: str = ""

    def label(self) -> str:
        if self.event:
            return self.event
        return f"{self.path}:{self.line}" if self.line else str(self.path)


class Score(BaseModel):
    criterion: str
    score: int  # 1..4
    rationale: str
    evidence: list[EvidenceRef]


class RootCause(StrEnum):
    docs_gap = "docs_gap"
    api_confusion = "api_confusion"
    error_message = "error_message"
    engine_bug = "engine_bug"
    sdk_gap = "sdk_gap"
    agent_mistake = "agent_mistake"


class Friction(BaseModel):
    summary: str
    root_cause: RootCause
    evidence: list[EvidenceRef]


class Finding(BaseModel):
    """Generic descriptive item: CI gap, hallucinated API, legacy confusion, docs judgment."""

    summary: str
    detail: str = ""
    evidence: list[EvidenceRef] = Field(default_factory=list)


class ClaimCheck(BaseModel):
    claim: str
    verdict: Literal["confirmed", "refuted", "unverifiable"]
    evidence: list[EvidenceRef] = Field(default_factory=list)


class IssueCandidate(BaseModel):
    title: str
    body: str
    category: RootCause | Literal["other"] = "other"
    evidence: list[EvidenceRef] = Field(default_factory=list)
    fingerprint: str = ""


class Rejected(BaseModel):
    section: str
    item: dict[str, Any]
    reason: str


class Scorecard(BaseModel):
    run_id: str
    rubric_version: str
    referee_model: str
    referee_prompt_version: str
    created_at: datetime = Field(default_factory=utcnow)
    facts: list[Fact] = Field(default_factory=list)  # deterministic, copied in for convenience
    scores: list[Score] = Field(default_factory=list)
    ci_gaps: list[Finding] = Field(default_factory=list)
    friction: list[Friction] = Field(default_factory=list)
    hallucinated_apis: list[Finding] = Field(default_factory=list)
    legacy_confusion: list[Finding] = Field(default_factory=list)
    docs_judgments: list[Finding] = Field(default_factory=list)
    claims: list[ClaimCheck] = Field(default_factory=list)
    issue_candidates: list[IssueCandidate] = Field(default_factory=list)
    rejected: list[Rejected] = Field(default_factory=list)  # failed the evidence rule
    verdicts: dict[str, Any] = Field(default_factory=dict)  # pack-level combined verdicts


class HumanScores(BaseModel):
    """human-scores.yaml written by a person next to a run bundle."""

    rater: str
    rubric_version: str
    scores: dict[str, int]
    notes: dict[str, str] = Field(default_factory=dict)


# --- Docs map -----------------------------------------------------------------------------


class DocVisit(BaseModel):
    event: str  # event id of the fetch
    url: str
    source: str  # docs source name from the pack, or "other"
    doc_version: str | None = None
    kind: Literal["fetch", "search", "local"] = "fetch"
    outcome: Literal["success", "error", "none"] = "none"
    outcome_event: str | None = None


CIJob.model_rebuild()
Fact.model_rebuild()
