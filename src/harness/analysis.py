"""Deterministic measurement: core facts every run has, plus the pack's analyzers.

The referee only judges what needs judgment; everything computable lives here.
"""

from __future__ import annotations

import json
import re

from harness import docsmap
from harness.bundle import ANALYSIS, DOCS_MAP, RunBundle
from harness.events import Event
from harness.models import EvidenceRef, Fact, Phase
from harness.pack import AnalysisContext, ToolPack

# The harness repo is public. An agent that finds it can read the rubric, expected coverage
# and referee prompt, so a run whose transcript mentions the harness by name (a fetch, a
# clone, a search result, or a path that leaked it) is flagged.
HARNESS_REPO_RE = re.compile(r"(?i)devtool[-_ ]?eval")


def analysis_context(bundle: RunBundle, pack: ToolPack) -> AnalysisContext:
    meta = bundle.meta()
    entry = pack.corpus().get(meta.spec.repo)
    if entry is None:
        raise KeyError(f"run {bundle.run_id} references unknown corpus repo {meta.spec.repo}")
    return AnalysisContext(
        run_dir=bundle.path,
        workdir=None,
        meta=meta,
        entry=entry,
        events=bundle.events(),
        diff=bundle.diff(),
        final_files=bundle.final_files(),
    )


def core_facts(actx: AnalysisContext, bundle: RunBundle) -> list[Fact]:
    meta = actx.meta
    work_turns = [t for t in meta.turns if t.phase != Phase.feedback]
    facts = [
        Fact(key="verify.passed", value=bool(meta.final_verify and meta.final_verify.passed)),
        Fact(key="verify.attempts", value=len(meta.verifies)),
        Fact(key="verify.checks", value=meta.final_verify.checks if meta.final_verify else []),
        Fact(
            key="retries_needed",
            value=next((v.attempt for v in meta.verifies if v.passed), None),
            detail="attempt index of the first passing verify (0 = no retry needed); null = never passed",
        ),
        Fact(key="nudges", value=len(meta.nudges)),
        Fact(key="cost_usd", value=round(sum(t.cost_usd or 0 for t in work_turns), 4)),
        Fact(key="duration_s", value=round(sum(t.duration_s for t in work_turns), 1)),
        Fact(key="turns", value=sum(t.turns for t in work_turns)),
        Fact(key="agent.status", value=[t.status for t in work_turns]),
        Fact(key="pushed", value=bool(meta.pushed_shas)),
        Fact(
            key="feedback_turn_changed_files",
            value=bundle.file("diff.feedback-turn.patch").exists(),
        ),
    ]
    git_state = bundle.read_json("git_state.json") or {}
    facts.append(Fact(key="uncommitted_changes", value=bool(git_state.get("status"))))
    for integ in meta.integration:
        facts.append(Fact(key=f"integration.{integ.name}.enabled", value=integ.enabled))
        facts.append(Fact(key=f"integration.{integ.name}.green", value=integ.green))
    fb = bundle.feedback()
    facts.append(Fact(key="feedback.parsed", value=bool(fb and not fb.parse_error)))
    facts.append(harness_exposure(actx.events))
    return facts


def harness_exposure(events: list[Event]) -> Fact:
    def text(ev: Event) -> str:
        parts = [ev.command, ev.url, ev.query, ev.text, *ev.paths]
        if ev.input:
            parts.append(json.dumps(ev.input, default=str))
        return "\n".join(p for p in parts if p)

    hits = [ev.id for ev in events if HARNESS_REPO_RE.search(text(ev))]
    return Fact(
        key="harness_exposure",
        value=bool(hits),
        detail=f"{len(hits)} events mention the harness by name" if hits else "",
        evidence=[EvidenceRef(event=h) for h in hits],
    )


def analyze_bundle(bundle: RunBundle, pack: ToolPack) -> list[Fact]:
    actx = analysis_context(bundle, pack)
    facts = core_facts(actx, bundle)
    for analyzer in pack.analyzers:
        try:
            facts.extend(analyzer.analyze(actx))
        except Exception as e:  # noqa: BLE001 - one broken analyzer shouldn't hide the rest
            facts.append(Fact(key=f"{analyzer.name}.error", value=f"{type(e).__name__}: {e}"))
    bundle.write_json(ANALYSIS, [f.model_dump(mode="json") for f in facts])
    outcome_cmd = getattr(pack, "outcome_command", None)
    visits = docsmap.build(actx.events, pack.docs_sources(), outcome_cmd)
    bundle.write_json(DOCS_MAP, [v.model_dump(mode="json") for v in visits])
    return facts


def load_facts(bundle: RunBundle) -> list[Fact]:
    data = bundle.read_json(ANALYSIS) or []
    return [Fact.model_validate(f) for f in data]  # type: ignore[union-attr]
