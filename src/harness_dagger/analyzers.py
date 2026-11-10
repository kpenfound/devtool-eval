"""Deterministic Dagger facts: module strategy, CI choice, fidelity red flags, command errors."""

from __future__ import annotations

import json
import re
import tomllib
from typing import Any

from harness.events import EventKind
from harness.models import EvidenceRef, Fact
from harness.pack import AnalysisContext

OFFICIAL_PREFIXES = ("dagger.io/", "github.com/dagger/")
# Official modules that serve the agent rather than the project: they generate files but
# contribute no checks, so they don't count toward module strategy or decorative modules.
AGENT_SKILL_MODULES = ("dagger.io/skill", "github.com/dagger/skill")
SKILLS_DIRS = (".agents/skills", ".claude/skills", ".cursor/skills", ".gemini/skills",
               ".github/skills", ".opencode/skills")


def is_agent_skill_module(source: str) -> bool:
    s = source.removeprefix("https://").split("@", 1)[0].rstrip("/")
    return s in AGENT_SKILL_MODULES


def module_kind(source: str, official_prefixes: tuple[str, ...] = OFFICIAL_PREFIXES) -> str:
    s = source.removeprefix("https://").removeprefix("git@")
    if s.startswith(official_prefixes):
        return "official"
    first = s.split("/", 1)[0]
    if s.startswith((".", "/")) or "." not in first:
        return "custom"
    return "third_party"


def _find_skips(node: Any, path: str = "") -> list[tuple[str, Any]]:
    """Every key mentioning "skip" anywhere in the config (``check.skip`` and friends)."""
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else k
            if "skip" in k.lower():
                out.append((p, v))
            out.extend(_find_skips(v, p))
    return out


def _line_of(text: str, needle: str) -> int | None:
    for i, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return i
    return None


class ModuleStrategyAnalyzer:
    """Classifies module strategy from dagger.toml (falling back to legacy dagger.json).

    Verdicts (docs/DESIGN.md): official only = good; official + custom without overlap =
    good; with overlap = partial; custom only = bad; nothing working = fail. Overlap is
    deterministic only for the ``check.skip`` reimplementation pattern; otherwise the
    referee's overlap score decides (see ``DaggerPack.combine_verdicts``).
    """

    name = "dagger.modules"

    def __init__(self, settings: dict):
        self.official = tuple(settings.get("official_prefixes", OFFICIAL_PREFIXES))

    def analyze(self, actx: AnalysisContext) -> list[Fact]:
        files = actx.final_files
        toml_text = files.get("dagger.toml")
        legacy = "dagger.json" in files
        modules: list[dict] = []
        skips: list[tuple[str, Any]] = []
        parse_error = None
        if toml_text is not None:
            try:
                cfg = tomllib.loads(toml_text)
            except tomllib.TOMLDecodeError as e:
                cfg, parse_error = {}, str(e)
            for name, mod in (cfg.get("modules") or {}).items():
                source = mod.get("source", "") if isinstance(mod, dict) else str(mod)
                modules.append({"name": name, "source": source, "kind": module_kind(source, self.official)})
            skips = _find_skips(cfg)
        elif legacy:
            try:
                cfg = json.loads(files["dagger.json"])
            except json.JSONDecodeError as e:
                cfg, parse_error = {}, str(e)
            if cfg.get("sdk"):
                modules.append({"name": cfg.get("name", "?"), "source": ".", "kind": "custom"})
            for dep in cfg.get("dependencies") or []:
                source = dep.get("source", "") if isinstance(dep, dict) else str(dep)
                modules.append({"name": dep.get("name", source) if isinstance(dep, dict) else source,
                                "source": source, "kind": module_kind(source, self.official)})
        # Custom modules that live in the repo but aren't installed still count as custom work.
        for path in files:
            m = re.match(r"\.dagger/modules/([^/]+)/", path)
            if m and not any(mod["name"] == m.group(1) for mod in modules):
                modules.append({"name": m.group(1), "source": f".dagger/modules/{m.group(1)}",
                                "kind": "custom", "installed": False})

        for m in modules:
            if is_agent_skill_module(m["source"]):
                m["kind"] = "agent_skill"
        kinds = {k: [m for m in modules if m["kind"] == k] for k in ("official", "custom", "third_party")}
        verify_passed = bool(actx.meta.final_verify and actx.meta.final_verify.passed)
        checks = actx.meta.final_verify.checks if actx.meta.final_verify else []
        check_modules = {c.split(":")[0] for c in checks}

        skip_official = [(k, v) for k, v in skips if any(k.startswith(f"modules.{m['name']}.") for m in kinds["official"])]
        reimplementation = bool(skip_official and kinds["custom"])
        decorative = [m["name"] for m in modules if m.get("installed", True) and m["kind"] != "agent_skill"
                      and m["name"] not in check_modules]

        if not verify_passed:
            strategy = "nothing_working"
        elif kinds["official"] and not kinds["custom"]:
            strategy = "official_only"
        elif kinds["official"] and kinds["custom"]:
            strategy = "official_custom_overlap" if reimplementation else "official_custom"
        elif kinds["custom"]:
            strategy = "custom_only"
        elif kinds["third_party"]:
            strategy = "third_party_only"
        else:
            strategy = "nothing_working"

        expected = actx.entry.expected_coverage
        installed_sources = {m["source"] for m in modules}
        missing = [e for e in expected if e not in installed_sources]
        bonus = list(actx.entry.bonus_coverage)
        bonus_installed = [b for b in bonus if b in installed_sources]

        def ev(needle: str) -> list[EvidenceRef]:
            if toml_text is None:
                return []
            line = _line_of(toml_text, needle)
            return [EvidenceRef(path="dagger.toml", line=line)] if line else []

        tracked = set((_git_state(actx).get("tracked")) or [])
        facts = [
            Fact(key="dagger.modules", value=modules),
            Fact(key="dagger.module_strategy", value=strategy,
                 detail="official_custom is provisional until the referee's overlap score is in"),
            Fact(key="dagger.skips", value=[{"key": k, "value": v} for k, v in skips],
                 evidence=[e for k, _ in skips for e in ev(k.split(".")[-1])]),
            Fact(key="dagger.reimplementation_via_skip", value=reimplementation),
            Fact(key="dagger.decorative_modules", value=decorative,
                 detail="installed but contributing no checks to `dagger check`"),
            Fact(key="dagger.third_party_modules", value=[m["source"] for m in kinds["third_party"]],
                 detail="report-only until a module index exists"),
            Fact(key="dagger.expected_coverage_missing", value=missing),
            Fact(key="dagger.official_coverage_available", value=bool(expected),
                 detail="false when no official module fits the repo (e.g. no Java module yet)"),
            Fact(key="dagger.bonus_coverage_installed", value=bonus_installed,
                 detail="dev tools the repo's CI doesn't run, now covered locally and in CI"),
            Fact(key="dagger.bonus_coverage", value=f"{len(bonus_installed)}/{len(bonus)}" if bonus else None),
            Fact(key="dagger.lock_committed", value="dagger.lock" in tracked),
            Fact(key="dagger.toml_committed", value="dagger.toml" in tracked),
            Fact(key="dagger.legacy_dagger_json", value=legacy),
        ]
        if parse_error:
            facts.append(Fact(key="dagger.config_parse_error", value=parse_error))
        return facts


def _git_state(actx: AnalysisContext) -> dict:
    p = actx.run_dir / "git_state.json"
    return json.loads(p.read_text()) if p.exists() else {}


CI_FILES = re.compile(r"^(\.github/workflows/.+\.ya?ml|\.gitlab-ci\.yml|Jenkinsfile|\.circleci/config\.yml|azure-pipelines\.yml)$")


def diff_by_file(diff: str) -> dict[str, dict]:
    """Per new-side file: status and added lines with their new line numbers."""
    files: dict[str, dict] = {}
    current = None
    new_line = 0
    old_path = None
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            current, old_path = None, None
        elif line.startswith("--- "):
            old_path = line[4:].removeprefix("a/")
        elif line.startswith("+++ "):
            path = line[4:].strip()
            if path == "/dev/null":
                current = old_path
                files[current] = {"status": "deleted", "added": []}
            else:
                current = path.removeprefix("b/")
                files[current] = {"status": "added" if old_path == "/dev/null" else "modified", "added": []}
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            new_line = int(m.group(1)) if m else 0
        elif current and line.startswith("+") and not line.startswith("+++"):
            files[current]["added"].append((new_line, line[1:]))
            new_line += 1
        elif current and line.startswith(" "):
            new_line += 1
    return files


class CIChoiceAnalyzer:
    """CI choice: Cloud Checks, ``dagger check`` in old CI, unchanged, or other."""

    name = "dagger.ci"

    def analyze(self, actx: AnalysisContext) -> list[Fact]:
        files = diff_by_file(actx.diff)
        ci_changes = {p: f for p, f in files.items() if CI_FILES.match(p)}
        evidence: list[EvidenceRef] = []
        dagger_check_ci = False
        other_dagger_ci = False
        for path, f in ci_changes.items():
            for n, text in f["added"]:
                if re.search(r"\bdagger\s+check\b", text):
                    dagger_check_ci = True
                    evidence.append(EvidenceRef(path=path, line=n, quote=text.strip()[:120]))
                elif re.search(r"\bdagger\b", text):
                    other_dagger_ci = True
                    evidence.append(EvidenceRef(path=path, line=n, quote=text.strip()[:120]))
        cloud_cmd = [ev for ev in actx.events if ev.kind == EventKind.shell and ev.command
                     and re.search(r"dagger\s+cloud\s+checks\s+on\b", ev.command)]
        cloud_ok = [ev for ev in cloud_cmd if ev.exit_code == 0]
        integ = next((i for i in actx.meta.integration if i.name == "cloud_checks"), None)
        cloud_enabled = bool(integ and integ.enabled) or bool(cloud_ok)
        evidence += [EvidenceRef(event=ev.id) for ev in cloud_cmd]

        if cloud_enabled:
            choice = "cloud_checks"
        elif dagger_check_ci:
            choice = "dagger_check_in_old_ci"
        elif not ci_changes:
            choice = "unchanged"
        else:
            choice = "other"

        # Without Cloud access the agent can't enable it; still record whether it recommended it.
        mentions = []
        for ev in actx.events:
            if ev.kind == EventKind.assistant_message and ev.text and re.search(r"cloud\s*checks", ev.text, re.I):
                mentions.append(ev)
        fb_path = actx.run_dir / "feedback.json"
        fb_mention = fb_path.exists() and bool(re.search(r"cloud\s*checks", fb_path.read_text(), re.I))
        return [
            Fact(key="dagger.ci_choice", value=choice, evidence=evidence),
            Fact(key="dagger.ci_files_changed", value={p: f["status"] for p, f in ci_changes.items()}),
            Fact(key="dagger.ci_also_old_ci", value=cloud_enabled and (dagger_check_ci or other_dagger_ci)),
            Fact(key="dagger.cloud_checks_attempted", value=bool(cloud_cmd)),
            Fact(key="dagger.recommended_cloud_checks", value=bool(mentions) or fb_mention,
                 evidence=[EvidenceRef(event=ev.id) for ev in mentions[:5]]),
        ]


RED_FLAGS = [
    (r"\|\|\s*true\b", "|| true"),
    (r"--deselect\b", "--deselect"),
    (r"(^|\s)-k\s", "pytest -k filter"),
    (r"pytest\.mark\.skip|@skip|\.skip\(", "skip marker"),
    (r"continue-on-error:\s*true", "continue-on-error"),
    (r"--ignore[= ]", "--ignore"),
    (r"exit\s+0\b", "exit 0"),
]
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(_test\.go|\.test\.[jt]sx?|\.spec\.[jt]sx?|test_[^/]+\.py)$")


class FidelityAnalyzer:
    """Red flags for narrowed test scope. The referee's fidelity score makes the call."""

    name = "dagger.fidelity"

    def analyze(self, actx: AnalysisContext) -> list[Fact]:
        files = diff_by_file(actx.diff)
        flags = []
        for path, f in files.items():
            if path.startswith(".github/") and not CI_FILES.match(path):
                continue
            for n, text in f["added"]:
                for pat, label in RED_FLAGS:
                    if re.search(pat, text):
                        flags.append(EvidenceRef(path=path, line=n, quote=f"{label}: {text.strip()[:100]}"))
        deleted_tests = [p for p, f in files.items() if f["status"] == "deleted" and TEST_PATH.search(p)]
        modified_tests = [p for p, f in files.items() if f["status"] == "modified" and TEST_PATH.search(p)]
        return [
            Fact(key="dagger.fidelity_red_flags", value=[e.quote for e in flags], evidence=flags),
            Fact(key="dagger.deleted_test_files", value=deleted_tests),
            Fact(key="dagger.modified_test_files", value=modified_tests),
        ]


HALLUCINATION_HINTS = re.compile(r"unknown (command|flag|shorthand flag)|no such (command|function|field)|"
                                 r"Cannot query field|unknown field|not a valid", re.I)


class CommandAnalyzer:
    """How the agent drove the CLI: dagger invocations, errors, unknown commands/flags."""

    name = "dagger.commands"

    def analyze(self, actx: AnalysisContext) -> list[Fact]:
        cmds = [ev for ev in actx.events if ev.kind == EventKind.shell and ev.command
                and re.search(r"(^|[\s;&|(])dagger\b", ev.command)]
        errors = [ev for ev in cmds if ev.exit_code not in (0, None)]
        unknown = [ev for ev in cmds if ev.text and HALLUCINATION_HINTS.search(ev.text)]
        first_ok_check = next((ev for ev in cmds if re.search(r"dagger\s+check\b", ev.command)
                               and ev.exit_code == 0), None)
        installs = [ev for ev in cmds if re.search(r"dagger\s+(module\s+)?install\b", ev.command)]
        recommend = [ev for ev in cmds if "module recommend" in ev.command]
        return [
            Fact(key="dagger.commands_run", value=len(cmds)),
            Fact(key="dagger.command_errors", value=len(errors), evidence=[EvidenceRef(event=e.id) for e in errors[:20]]),
            Fact(key="dagger.unknown_command_hints", value=[e.command for e in unknown][:20],
                 evidence=[EvidenceRef(event=e.id) for e in unknown[:20]]),
            Fact(key="dagger.first_passing_check_event", value=first_ok_check.id if first_ok_check else None),
            Fact(key="dagger.used_module_recommend", value=bool(recommend)),
            Fact(key="dagger.install_commands", value=[e.command for e in installs][:20]),
        ]


# The skill module writes <skills dir>/dagger/ with this ownership marker beside SKILL.md.
SKILL_MARKER = ".dagger-managed"
# The project skills dir each agent loads (mirrors the adapters' project_skills_dir).
AGENT_SKILLS_DIR = {"claude": ".claude/skills", "codex": ".agents/skills"}


def skill_adoption(module_installed: bool, skill_installed: bool) -> str:
    if module_installed and skill_installed:
        return "installed"
    if module_installed:
        return "module_only"  # installed dagger.io/skill but never generated the skill
    if skill_installed:
        return "skill_only"  # skill files without the module (copied or hand-written)
    return "not_installed"


class SkillAnalyzer:
    """Whether the agent took up Dagger's own agent skill, in two steps: the module
    (``dagger.io/skill`` in dagger.toml) and the skill itself (``dagger generate`` writing
    ``<skills dir>/dagger/SKILL.md``).

    Meaningful mainly for variants that give the repo a project skills directory, which is
    what makes ``dagger module recommend`` offer the skill module.
    """

    name = "dagger.skill"

    def analyze(self, actx: AnalysisContext) -> list[Fact]:
        module_installed = False
        toml_text = actx.final_files.get("dagger.toml")
        if toml_text:
            try:
                mods = tomllib.loads(toml_text).get("modules") or {}
            except tomllib.TOMLDecodeError:
                mods = {}
            module_installed = any(
                is_agent_skill_module(m.get("source", "") if isinstance(m, dict) else str(m))
                for m in mods.values())
        files = diff_by_file(actx.diff)
        written = sorted(p for p, f in files.items()
                         if f["status"] != "deleted" and p.startswith(tuple(d + "/" for d in SKILLS_DIRS)))
        # Installed = SKILL.md generated under a skills dir. The snapshotted marker covers
        # skills dirs the repo gitignores (which the diff can't see).
        dirs = {d for d in SKILLS_DIRS if f"{d}/dagger/SKILL.md" in written}
        dirs |= {d for d in SKILLS_DIRS
                 if is_agent_skill_module(actx.final_files.get(f"{d}/dagger/{SKILL_MARKER}", "").strip())}
        skill_dirs = sorted(dirs)
        agent_dir = AGENT_SKILLS_DIR.get(str(actx.meta.spec.agent))
        skill_marker = tuple(f"{d}/dagger/" for d in SKILLS_DIRS)
        reads = [ev for ev in actx.events if ev.kind != EventKind.system and (
            any(m in p for p in ev.paths for m in skill_marker)
            or (ev.command and any(m in ev.command for m in skill_marker)))]
        return [
            Fact(key="dagger.skill_module_installed", value=module_installed,
                 detail="dagger.io/skill in dagger.toml",
                 evidence=[EvidenceRef(path="dagger.toml", line=_line_of(toml_text or "", "dagger.io/skill"))]
                 if module_installed else []),
            Fact(key="dagger.skill_installed", value=bool(skill_dirs),
                 detail="the dagger skill was generated into a project skills dir",
                 evidence=[EvidenceRef(path=f"{d}/dagger/SKILL.md") for d in skill_dirs]),
            Fact(key="dagger.skill_dirs", value=skill_dirs),
            Fact(key="dagger.skill_in_agent_dir", value=agent_dir in skill_dirs if agent_dir else None,
                 detail=f"generated into {agent_dir}, where this agent loads project skills"),
            Fact(key="dagger.skill_adoption", value=skill_adoption(module_installed, bool(skill_dirs))),
            Fact(key="dagger.skill_files_written", value=written),
            Fact(key="dagger.skill_used", value=bool(reads),
                 detail="read or ran something from a generated dagger skill",
                 evidence=[EvidenceRef(event=e.id) for e in reads[:20]]),
        ]
