"""Referee framework: blinding, the evidence rule, and versioning.

- **Blinding:** variant names and bundle content are redacted from everything the referee
  reads (``Redactor``), along with pack-supplied patterns.
- **Evidence rule:** every score and finding must cite at least one transcript event id or
  ``file:line`` that exists; anything else is moved to ``rejected`` and not recorded.
- **Versioning:** scorecards carry the rubric version, referee model, and prompt version.
  An existing scorecard is never re-scored unless forced; reports never compare across
  versions.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from harness.analysis import analysis_context, load_facts
from harness.bundle import SCORECARD, RunBundle
from harness.config import HarnessConfig
from harness.events import Event, EventKind
from harness.models import (
    ClaimCheck,
    EvidenceRef,
    Finding,
    Friction,
    IssueCandidate,
    Rejected,
    ResolvedBundle,
    RootCause,
    Score,
    Scorecard,
)
from harness.pack import AnalysisContext, Rubric, ToolPack
from harness.referee import backends
from harness.schema import drop_nulls, strict

REFEREE_PROMPT_VERSION = "2"
REDACTED = "[REDACTED]"
VERIFY_LOG_TAIL = 150


class Redactor:
    def __init__(self, literals: list[str], patterns: list[str], paths: list[str]):
        self.literals = [s for s in sorted(set(literals), key=len, reverse=True) if s]
        self.patterns = [re.compile(p) for p in patterns]
        self.paths = [p for p in paths if p]

    def __call__(self, text: str | None) -> str:
        if not text:
            return text or ""
        for p in self.paths:
            text = text.replace(p, "~")
        for s in self.literals:
            if len(s) < 4:
                text = re.sub(rf"\b{re.escape(s)}\b", REDACTED, text)
            else:
                text = text.replace(s, REDACTED)
        for p in self.patterns:
            text = p.sub(REDACTED, text)
        return text


def redactor_for(bundle: RunBundle, rubric: Rubric) -> Redactor:
    variant_data = bundle.read_json("variant.json")
    literals: list[str] = []
    if variant_data:
        variant = ResolvedBundle.model_validate(variant_data)
        literals = variant.redactions
    meta = bundle.meta()
    paths = [meta.home or str(bundle.path / "home"), str(bundle.path)]
    return Redactor(literals, rubric.redaction_patterns, paths)


_BUNDLE_PATH_MARKERS = ("/.claude/skills/", "/.codex/skills/", "/.claude/CLAUDE.md", "/.codex/AGENTS.md")


def render_events(events: list[Event], redact: Redactor, max_chars: int) -> str:
    lines = []
    for ev in events:
        if ev.kind == EventKind.system:
            continue  # init frames list skills/MCP by name: not blind
        if any(m in p for p in ev.paths for m in _BUNDLE_PATH_MARKERS) or (
            ev.command and any(m in ev.command for m in _BUNDLE_PATH_MARKERS)
        ):
            lines.append(f"[{ev.id} {ev.phase} {ev.kind}] (read of provided guidance: redacted)")
            continue
        lines.append(redact(ev.brief(max_chars)))
    return "\n".join(lines)


def number_file(path: str, content: str, redact: Redactor) -> str:
    body = "\n".join(f"{i:5d} | {line}" for i, line in enumerate(content.splitlines(), 1))
    return f"=== {path} ===\n{redact(body)}"


def diff_files(diff: str) -> dict[str, int]:
    """New-side files in a unified diff, mapped to the highest line number seen."""
    files: dict[str, int] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            current = None if path == "/dev/null" else path.removeprefix("b/")
            if current:
                files.setdefault(current, 0)
        elif line.startswith("@@") and current:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                end = int(m.group(1)) + int(m.group(2) or 1)
                files[current] = max(files[current], end)
    return files


def referee_diff(diff: str) -> str:
    """The diff minus generated/vendored files, which are noise and can be huge."""
    from harness.runner import SNAPSHOT_SKIP_DIRS, SNAPSHOT_SKIP_FILES

    out: list[str] = []
    for section in re.split(r"(?m)^(?=diff --git )", diff):
        m = re.match(r"diff --git a/(\S+) b/(\S+)", section)
        if m:
            parts = Path(m.group(2)).parts
            if SNAPSHOT_SKIP_DIRS & set(parts[:-1]) or parts[-1] in SNAPSHOT_SKIP_FILES:
                out.append(f"diff --git a/{m.group(1)} b/{m.group(2)}\n(generated or vendored file: {section.count(chr(10))} diff lines omitted)\n")
                continue
        out.append(section)
    return "".join(out)


class RefereeOutput(BaseModel):
    scores: list[dict[str, Any]] = Field(default_factory=list)
    ci_gaps: list[dict[str, Any]] = Field(default_factory=list)
    friction: list[dict[str, Any]] = Field(default_factory=list)
    hallucinated_apis: list[dict[str, Any]] = Field(default_factory=list)
    legacy_confusion: list[dict[str, Any]] = Field(default_factory=list)
    docs_judgments: list[dict[str, Any]] = Field(default_factory=list)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    issue_candidates: list[dict[str, Any]] = Field(default_factory=list)


def output_schema(rubric: Rubric) -> dict[str, Any]:
    evidence = {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "object",
            "properties": {
                "event": {"type": "string", "description": "event id like e00042"},
                "path": {"type": "string", "description": "repo-relative file path"},
                "line": {"type": "integer"},
                "quote": {"type": "string", "description": "short excerpt"},
            },
        },
    }
    finding = {
        "type": "object",
        "properties": {"summary": {"type": "string"}, "detail": {"type": "string"}, "evidence": evidence},
        "required": ["summary", "evidence"],
    }
    return {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "criterion": {"type": "string", "enum": [c.id for c in rubric.criteria]},
                        "score": {"type": "integer", "minimum": 1, "maximum": 4},
                        "rationale": {"type": "string"},
                        "evidence": evidence,
                    },
                    "required": ["criterion", "score", "rationale", "evidence"],
                },
            },
            "ci_gaps": {"type": "array", "items": finding},
            "friction": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "root_cause": {
                            "type": "string",
                            "enum": [
                                "docs_gap",
                                "api_confusion",
                                "error_message",
                                "engine_bug",
                                "sdk_gap",
                                "agent_mistake",
                            ],
                        },
                        "evidence": evidence,
                    },
                    "required": ["summary", "root_cause", "evidence"],
                },
            },
            "hallucinated_apis": {"type": "array", "items": finding},
            "legacy_confusion": {"type": "array", "items": finding},
            "docs_judgments": {"type": "array", "items": finding},
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["confirmed", "refuted", "unverifiable"]},
                        "evidence": evidence,
                    },
                    "required": ["claim", "verdict", "evidence"],
                },
            },
            "issue_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "category": {"type": "string"},
                        "evidence": evidence,
                    },
                    "required": ["title", "body", "evidence"],
                },
            },
        },
        "required": ["scores", "ci_gaps", "friction", "claims", "issue_candidates"],
    }


SYSTEM = """You are the referee for an evaluation of coding agents adopting a developer tool \
in a real repository. You judge only what needs expert judgment; deterministic facts are \
already computed and given to you. Be strict and specific.

Evidence rule: every score and every finding MUST cite at least one piece of evidence: a \
transcript event id exactly as shown (e.g. "e00042") or a repo-relative file path and line \
number from the numbered final files or the diff. Items without valid evidence are discarded. \
Do not guess the configuration the agent was given; some text is redacted as [REDACTED] on \
purpose.

Return a single JSON object matching the provided schema."""


def build_prompt(actx: AnalysisContext, bundle: RunBundle, pack: ToolPack, config: HarnessConfig,
                 rubric: Rubric) -> tuple[str, set[str], dict[str, int]]:
    redact = redactor_for(bundle, rubric)
    facts = load_facts(bundle)
    entry = actx.entry
    criteria = []
    for c in rubric.criteria:
        anchors = "\n".join(f"    {k}: {v}" for k, v in sorted(c.anchors.items())) or "    (anchors TBD: use 1=bad, 2=weak, 3=good, 4=excellent)"
        criteria.append(f"- id: {c.id}\n  name: {c.name}\n  question: {c.question}\n  guidance: {c.guidance}\n  anchors:\n{anchors}")
    ci = []
    for job in entry.ci:
        scope = f"OUT OF SCOPE ({job.out_of_scope})" if job.out_of_scope else "in scope"
        partial = "; ".join(f"{p.match} out of scope ({p.reason})" for p in job.partial_out_of_scope)
        ci.append(f"- {job.workflow} :: {job.job} [{scope}] {job.summary} {partial}".rstrip())
    verify_tail = ""
    log = bundle.file("verify.log")
    if log.exists():
        verify_tail = "\n".join(log.read_text(errors="replace").splitlines()[-VERIFY_LOG_TAIL:])
    feedback = bundle.feedback()
    files = actx.final_files
    numbered = "\n\n".join(number_file(p, c, redact) for p, c in files.items())
    max_chars = config.referee.max_event_chars
    transcript = render_events(actx.events, redact, max_chars)
    while len(transcript) > config.referee.max_prompt_chars // 2 and max_chars > 200:
        max_chars //= 2
        transcript = render_events(actx.events, redact, max_chars)

    prompt = f"""# Rubric (version {rubric.version})

Score each criterion 1-4. If a criterion does not apply (e.g. no custom module exists), \
omit it rather than inventing evidence.

{chr(10).join(criteria)}

# Expert context

{rubric.expert_context}

# Descriptive judgments (not scored)

- ci_gaps: what the in-scope existing CI does that nothing in the new setup covers. \
Do NOT map CI steps 1:1; report meaningful coverage gaps only. Out-of-scope jobs are not gaps.
- friction: each point where the agent got stuck or went wrong, with a root cause: \
docs_gap, api_confusion, error_message, engine_bug, sdk_gap, or agent_mistake.
- hallucinated_apis: commands, flags, config keys, or API calls that do not exist.
- legacy_confusion: use of patterns from older tool versions.
- docs_judgments: notable docs the agent relied on or contradicted (see docs map).
- claims: each concrete claim in the agent's self-report, marked confirmed, refuted, \
or unverifiable against the transcript and files.
- issue_candidates: problems worth filing against the tool (docs, API, errors, modules), \
written as a concise issue title and body for a maintainer to review. Not agent mistakes.

# Repo

{entry.name} ({entry.url} @ {entry.sha}); stack: {", ".join(entry.stack)}; tier {entry.tier}.
Existing CI inventory:
{chr(10).join(ci) or "- (none recorded)"}
Expected official coverage: {", ".join(entry.expected_coverage) or ("(none: no official module fits this repo, so custom modules are expected)" if entry.expected_coverage_raw else "(not captured)")}
Bonus coverage (dev tools the CI doesn't run; credit, not required): \
{", ".join(entry.bonus_coverage) or "(none)"}

# Deterministic facts

{redact(json.dumps([f.model_dump(mode="json") for f in facts], indent=1))}

# Final files (numbered)

{numbered or "(none)"}

# Diff against the pinned SHA

{redact(referee_diff(actx.diff)[: config.referee.max_prompt_chars // 4])}

# Harness verification log (tail)

{redact(verify_tail)}

# Agent self-report (feedback turn)

{redact(feedback.model_dump_json(indent=1, exclude={"raw_text"}) if feedback else "(none)")}

# Docs map

{redact(json.dumps(bundle.read_json("docs_map.json") or [], indent=0)[:20000])}

# Transcript

{transcript}
"""
    valid_events = {ev.id for ev in actx.events}
    valid_files = {p: len(c.splitlines()) for p, c in files.items()}
    for p, n in diff_files(actx.diff).items():
        valid_files[p] = max(valid_files.get(p, 0), n)
    for job in entry.ci:
        valid_files.setdefault(job.workflow, 10_000)  # CI files exist upstream at the pinned SHA
    return prompt, valid_events, valid_files


def check_evidence(refs: list[dict] | None, events: set[str], files: dict[str, int]) -> tuple[list[EvidenceRef], list[str]]:
    good, bad = [], []
    for raw in refs or []:
        try:
            ref = EvidenceRef.model_validate(raw)
        except ValidationError:
            bad.append(f"malformed: {raw}")
            continue
        if ref.event:
            if ref.event in events:
                good.append(ref)
            else:
                bad.append(f"unknown event {ref.event}")
        elif ref.path:
            path = ref.path.removeprefix("./").removeprefix("b/")
            if path not in files:
                bad.append(f"unknown file {ref.path}")
            elif ref.line is not None and not (1 <= ref.line <= max(files[path], 1)):
                bad.append(f"line out of range {ref.path}:{ref.line}")
            else:
                ref.path = path
                good.append(ref)
        else:
            bad.append(f"empty evidence: {raw}")
    return good, bad


def fingerprint(title: str, category: str) -> str:
    words = sorted(set(re.findall(r"[a-z0-9.]{3,}", title.lower())))
    return hashlib.sha1(f"{category}:{' '.join(words)}".encode()).hexdigest()[:12]


def similar(a: str, b: str) -> bool:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.75


def enforce(raw: RefereeOutput, rubric: Rubric, events: set[str], files: dict[str, int], card: Scorecard) -> None:
    criteria = {c.id for c in rubric.criteria}

    def take(section: str, items: list[dict], build):
        out = []
        for item in items:
            good, bad = check_evidence(item.get("evidence"), events, files)
            if not good:
                card.rejected.append(
                    Rejected(section=section, item=item, reason="no valid evidence: " + "; ".join(bad))
                )
                continue
            try:
                out.append(build(item, good))
            except (ValidationError, ValueError, KeyError) as e:
                card.rejected.append(Rejected(section=section, item=item, reason=f"invalid: {e}"))
        return out

    def score(item, ev):
        if item["criterion"] not in criteria:
            raise ValueError(f"unknown criterion {item['criterion']}")
        if not 1 <= int(item["score"]) <= 4:
            raise ValueError("score out of range")
        return Score(criterion=item["criterion"], score=int(item["score"]),
                     rationale=item.get("rationale", ""), evidence=ev)

    def finding(item, ev):
        return Finding(summary=item["summary"], detail=item.get("detail", ""), evidence=ev)

    def issue(item, ev):
        cat = item.get("category") or "other"
        cand = IssueCandidate(title=item["title"], body=item["body"], evidence=ev,
                              category=cat if cat in {r.value for r in RootCause} else "other")
        cand.fingerprint = fingerprint(cand.title, str(cand.category))
        return cand

    scores = take("scores", raw.scores, score)
    seen: set[str] = set()
    for s in scores:  # one score per criterion; keep the first
        if s.criterion in seen:
            card.rejected.append(Rejected(section="scores", item=s.model_dump(mode="json"), reason="duplicate criterion"))
            continue
        seen.add(s.criterion)
        card.scores.append(s)
    card.ci_gaps = take("ci_gaps", raw.ci_gaps, finding)
    card.friction = take("friction", raw.friction, lambda i, ev: Friction(summary=i["summary"], root_cause=i["root_cause"], evidence=ev))
    card.hallucinated_apis = take("hallucinated_apis", raw.hallucinated_apis, finding)
    card.legacy_confusion = take("legacy_confusion", raw.legacy_confusion, finding)
    card.docs_judgments = take("docs_judgments", raw.docs_judgments, finding)
    card.claims = take("claims", raw.claims, lambda i, ev: ClaimCheck(claim=i["claim"], verdict=i["verdict"], evidence=ev))
    card.issue_candidates = take("issue_candidates", raw.issue_candidates, issue)


def parse_output(text: str) -> RefereeOutput:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json")
    start, end = text.find("{"), text.rfind("}")
    return RefereeOutput.model_validate(drop_nulls(json.loads(text[start : end + 1])))


class ScorecardExists(RuntimeError):
    pass


def referee_bundle(bundle: RunBundle, pack: ToolPack, config: HarnessConfig, *, force: bool = False,
                   dry_run: bool = False) -> Scorecard | str:
    rubric = pack.rubric()
    existing = bundle.scorecard()
    if existing and not force:
        raise ScorecardExists(
            f"{bundle.run_id} already scored (rubric {existing.rubric_version}, "
            f"{existing.referee_model}); scores are not re-computed without --force"
        )
    actx = analysis_context(bundle, pack)
    prompt, events, files = build_prompt(actx, bundle, pack, config, rubric)
    if dry_run:
        return prompt
    (bundle.path / "referee.prompt.md").write_text(prompt)
    text = backends.complete(config, SYSTEM, prompt, strict(output_schema(rubric)))
    (bundle.path / "referee.raw.json").write_text(text)
    raw = parse_output(text)
    card = Scorecard(
        run_id=bundle.run_id,
        rubric_version=rubric.version,
        referee_model=f"{config.referee.backend}:{backends.model_name(config) or 'default'}",
        referee_prompt_version=REFEREE_PROMPT_VERSION,
        facts=load_facts(bundle),
    )
    enforce(raw, rubric, events, files, card)
    card.verdicts = pack.combine_verdicts(card.facts, card.scores)
    bundle.write_model(SCORECARD, card)
    return card


def prompt_path(bundle: RunBundle) -> Path:
    return bundle.path / "referee.prompt.md"
