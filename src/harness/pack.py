"""Tool pack interfaces.

A tool pack is a Python object implementing ``ToolPack``, registered under the
``harness.packs`` entry point group, plus YAML/Markdown data (rubric, prompts, bundles,
corpus). Interfaces are defined here up front, but the only pack so far is Dagger: expect
the seams to move when a second pack shows where they're wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, Field

from harness.models import (
    BundleSpec,
    CorpusEntry,
    Fact,
    IntegrationResult,
    RunMeta,
    Score,
    VerifyResult,
)

if TYPE_CHECKING:
    from harness.config import HarnessConfig
    from harness.events import Event


@dataclass
class RunContext:
    """What a pack sees while a run is in progress."""

    run_id: str
    run_dir: Path
    workdir: Path  # the slot's working copy; the agent's cwd
    home: Path  # the run's fake HOME
    entry: CorpusEntry
    meta: RunMeta
    config: HarnessConfig
    env: dict[str, str] = field(default_factory=dict)  # env for tool commands (agent and harness)
    slot_repo: str | None = None  # owner/name on GitHub, None in local-only mode
    harness_token: str | None = None  # broad token for harness-side GitHub calls

    def log(self, msg: str) -> None:
        with (self.run_dir / "harness.log").open("a") as f:
            f.write(msg.rstrip() + "\n")


@dataclass
class AnalysisContext:
    """What analyzers and the referee see after a run."""

    run_dir: Path
    workdir: Path | None  # final tree if still available, else None (use diff)
    meta: RunMeta
    entry: CorpusEntry
    events: list[Event]
    diff: str
    final_files: dict[str, str]  # path -> content, for files the pack asked to snapshot


@dataclass
class ToolInstall:
    version: str
    path_prepend: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)  # e.g. a release selector


@runtime_checkable
class Environment(Protocol):
    def install(self, version: str | None, ctx: RunContext) -> ToolInstall:
        """Make sure the pinned tool version is available; return what was installed."""

    def prepare(self, ctx: RunContext) -> dict[str, str]:
        """Auth state etc. Return env vars to add for both agent and harness tool commands."""

    def reset(self, ctx: RunContext) -> None:
        """Engine and cache reset, run before the agent starts."""


@runtime_checkable
class Verifier(Protocol):
    def verify(self, ctx: RunContext, attempt: int) -> VerifyResult:
        """Run the standard pass/fail check in ``ctx.workdir``. Write the log into run_dir."""


@runtime_checkable
class Analyzer(Protocol):
    name: str

    def analyze(self, actx: AnalysisContext) -> list[Fact]: ...


@runtime_checkable
class IntegrationHooks(Protocol):
    name: str

    def enable(self, ctx: RunContext) -> None: ...

    def verify(self, ctx: RunContext, shas: list[str]) -> IntegrationResult:
        """Check the integration's result for these commit SHAs (never "latest")."""

    def teardown(self, ctx: RunContext) -> None:
        """Turn the integration off for the slot. Must be safe to call repeatedly."""


class Criterion(BaseModel):
    id: str
    name: str
    question: str
    guidance: str = ""
    anchors: dict[int, str] = Field(default_factory=dict)  # 1..4; TBD until runs are graded


class Rubric(BaseModel):
    version: str
    criteria: list[Criterion]
    expert_context: str = ""
    redaction_patterns: list[str] = Field(default_factory=list)  # regexes, pack-supplied


class DocsVersionRule(BaseModel):
    pattern: str  # regex on the URL
    version: str  # literal version or "\\1" style group reference


class DocsSource(BaseModel):
    name: str
    domains: list[str]
    path_prefixes: list[str] = Field(default_factory=list)  # optional, e.g. github.com/dagger/
    versions: list[DocsVersionRule] = Field(default_factory=list)
    default_version: str | None = None
    sitemap: str | None = None


class ToolPack(Protocol):
    name: str
    version: str
    data_dir: Path

    environment: Environment
    verifier: Verifier
    analyzers: list[Analyzer]
    integrations: list[IntegrationHooks]

    def corpus(self) -> dict[str, CorpusEntry]: ...

    def rubric(self) -> Rubric: ...

    def docs_sources(self) -> list[DocsSource]: ...

    def prompt(self, name: str) -> str:
        """Prompt templates: ``task``, ``retry`` (with ``{error}``), ``feedback``."""

    def bundle_dir(self, name: str) -> Path: ...

    def snapshot_paths(self) -> list[str]:
        """Glob patterns of files the referee and analyzers should see from the final tree."""

    def capture_coverage(self, workdir: Path, env: dict[str, str]) -> tuple[list[str], str]:
        """Expected official coverage for a repo checkout: (module sources, raw output)."""

    def coverage_in_ci(self, module: str, ci_text: str) -> bool | None:
        """Whether the repo's CI (``ci_text``) runs what ``module`` covers; None if unknown."""

    def combine_verdicts(self, facts: list[Fact], scores: list[Score]) -> dict[str, Any]:
        """Pack-level verdicts that need both deterministic facts and referee scores."""


class BasePack:
    """Shared file-backed behavior for packs that keep their data in ``data_dir``."""

    name: str = ""
    version: str = "0"
    data_dir: Path

    def corpus(self) -> dict[str, CorpusEntry]:
        out = {}
        for p in sorted((self.data_dir / "corpus").glob("*.yaml")):
            entry = CorpusEntry.model_validate(yaml.safe_load(p.read_text()))
            out[entry.name] = entry
        return out

    def corpus_path(self, name: str) -> Path:
        return self.data_dir / "corpus" / f"{name}.yaml"

    def rubric(self) -> Rubric:
        raw = yaml.safe_load((self.data_dir / "rubric.yaml").read_text())
        expert = self.data_dir / "expert.md"
        if expert.exists():
            raw.setdefault("expert_context", expert.read_text())
        return Rubric.model_validate(raw)

    def docs_sources(self) -> list[DocsSource]:
        raw = yaml.safe_load((self.data_dir / "docs.yaml").read_text()) or {}
        return [DocsSource.model_validate(s) for s in raw.get("sources", [])]

    def prompt(self, name: str) -> str:
        return (self.data_dir / "prompts" / f"{name}.md").read_text().strip()

    def bundle_dir(self, name: str) -> Path:
        return self.data_dir / "bundles" / name

    def bundles(self) -> list[str]:
        return sorted(p.name for p in (self.data_dir / "bundles").iterdir() if p.is_dir())

    def snapshot_paths(self) -> list[str]:
        return []

    def coverage_in_ci(self, module: str, ci_text: str) -> bool | None:
        return None

    def combine_verdicts(self, facts: list[Fact], scores: list[Score]) -> dict[str, Any]:
        return {}


def load_bundle_spec(path: Path) -> BundleSpec:
    return BundleSpec.model_validate(yaml.safe_load((path / "bundle.yaml").read_text()))


def load_pack(name: str, settings: dict | None = None) -> ToolPack:
    """Instantiate a pack, passing its ``[packs.<name>]`` settings from harness.toml."""
    for ep in entry_points(group="harness.packs"):
        if ep.name == name:
            cls = ep.load()
            return cls(settings or {})
    available = [ep.name for ep in entry_points(group="harness.packs")]
    raise KeyError(f"no tool pack named {name!r} (available: {', '.join(available) or 'none'})")


def available_packs() -> list[str]:
    return [ep.name for ep in entry_points(group="harness.packs")]
