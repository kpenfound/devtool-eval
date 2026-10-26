"""Run bundles on disk, and variant (config) bundles.

Two different things share the word "bundle" in the design:

- a **run bundle** is a run's output directory (``RunBundle``), passed between stages;
- a **variant bundle** is the skills/instructions/MCP config given to the agent
  (``resolve_variant``).
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel

from harness.events import Event, normalize, read_events, write_events
from harness.models import (
    FeedbackReport,
    HumanScores,
    ResolvedBundle,
    RunMeta,
    RunSpec,
    Scorecard,
)
from harness.pack import load_bundle_spec

RUN_JSON = "run.json"
RAW = "transcript.raw.jsonl"
EVENTS = "events.jsonl"
DIFF = "diff.patch"
FEEDBACK = "feedback.json"
ANALYSIS = "analysis.json"
SCORECARD = "scorecard.json"
DOCS_MAP = "docs_map.json"
HUMAN = "human-scores.yaml"
SNAPSHOT = "final"  # directory of snapshotted final-tree files


def new_run_id(spec: RunSpec) -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{spec.repo}-{spec.bundle}-{spec.agent}-{secrets.token_hex(2)}"


class RunBundle:
    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def create(cls, runs_dir: Path, meta: RunMeta) -> RunBundle:
        path = runs_dir / meta.run_id
        path.mkdir(parents=True, exist_ok=False)
        b = cls(path)
        b.write_meta(meta)
        return b

    @property
    def run_id(self) -> str:
        return self.path.name

    def file(self, name: str) -> Path:
        return self.path / name

    def meta(self) -> RunMeta:
        return RunMeta.model_validate_json(self.file(RUN_JSON).read_text())

    def write_meta(self, meta: RunMeta) -> None:
        tmp = self.file(RUN_JSON + ".tmp")
        tmp.write_text(meta.model_dump_json(indent=2))
        tmp.replace(self.file(RUN_JSON))

    def write_model(self, name: str, model: BaseModel) -> None:
        self.file(name).write_text(model.model_dump_json(indent=2))

    def write_json(self, name: str, data: object) -> None:
        self.file(name).write_text(json.dumps(data, indent=2, default=str))

    def read_json(self, name: str) -> object | None:
        p = self.file(name)
        return json.loads(p.read_text()) if p.exists() else None

    def events(self) -> list[Event]:
        if not self.file(EVENTS).exists():
            self.renormalize()
        return read_events(self.file(EVENTS))

    def renormalize(self) -> list[Event]:
        events = normalize(self.meta().spec.agent, self.file(RAW))
        write_events(events, self.file(EVENTS))
        return events

    def diff(self) -> str:
        p = self.file(DIFF)
        return p.read_text() if p.exists() else ""

    def feedback(self) -> FeedbackReport | None:
        p = self.file(FEEDBACK)
        return FeedbackReport.model_validate_json(p.read_text()) if p.exists() else None

    def scorecard(self) -> Scorecard | None:
        p = self.file(SCORECARD)
        return Scorecard.model_validate_json(p.read_text()) if p.exists() else None

    def human_scores(self) -> HumanScores | None:
        p = self.file(HUMAN)
        return HumanScores.model_validate(yaml.safe_load(p.read_text())) if p.exists() else None

    def final_files(self) -> dict[str, str]:
        root = self.file(SNAPSHOT)
        if not root.exists():
            return {}
        return {
            str(p.relative_to(root)): p.read_text(errors="replace")
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }


def list_bundles(runs_dir: Path) -> list[RunBundle]:
    return [RunBundle(p) for p in sorted(runs_dir.iterdir()) if (p / RUN_JSON).exists()]


def find_bundle(runs_dir: Path, run_id: str) -> RunBundle:
    exact = runs_dir / run_id
    if (exact / RUN_JSON).exists():
        return RunBundle(exact)
    matches = [b for b in list_bundles(runs_dir) if run_id in b.run_id]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(f"no run bundle matching {run_id!r} in {runs_dir}")
    raise ValueError(f"{run_id!r} is ambiguous: {', '.join(b.run_id for b in matches)}")


# --- Variant bundles ------------------------------------------------------------------------


def _hash_dir(path: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(path)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def resolve_variant(bundle_dir: Path) -> ResolvedBundle:
    if not (bundle_dir / "bundle.yaml").exists():
        raise FileNotFoundError(f"variant bundle not found: {bundle_dir}")
    spec = load_bundle_spec(bundle_dir)
    redactions = {spec.name, bundle_dir.name}
    if spec.skills_dir:
        for skill in (bundle_dir / spec.skills_dir).iterdir():
            if skill.is_dir():
                redactions.add(skill.name)
    # Distinctive lines of any bundle content: if the agent echoes them, the referee would
    # learn the variant. Short or generic lines aren't worth redacting.
    for p in bundle_dir.rglob("*"):
        if p.is_file() and p.suffix in (".md", ".txt", ".yaml", ".json") and p.name != "bundle.yaml":
            for line in p.read_text(errors="replace").splitlines():
                line = line.strip().lstrip("#-* ").strip()
                if len(line) >= 40:
                    redactions.add(line)
    return ResolvedBundle(
        spec=spec,
        path=str(bundle_dir),
        content_hash=_hash_dir(bundle_dir),
        redactions=sorted(redactions, key=len, reverse=True),
    )


def instructions_text(rb: ResolvedBundle) -> str | None:
    """User-level instructions (CLAUDE.md / AGENTS.md) for a variant, including docs pointers."""
    parts = []
    if rb.spec.instructions:
        parts.append((Path(rb.path) / rb.spec.instructions).read_text().strip())
    if rb.spec.docs_pointers:
        parts.append("Useful documentation:\n" + "\n".join(f"- {u}" for u in rb.spec.docs_pointers))
    return "\n\n".join(parts) + "\n" if parts else None
