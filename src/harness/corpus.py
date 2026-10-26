"""Corpus capture: CI inventory and expected official coverage for a pinned repo.

Out-of-scope tags are proposed from obvious signals (publish/release/deploy jobs → ``cd``,
Windows/macOS runners → ``non-linux``, repo bots triggered by issues or named like
stale/lock/label → ``automation``) and marked for review. Re-capturing replaces earlier
proposals but keeps any entry edited by hand.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import tomllib
from pathlib import Path

import yaml

from harness.config import HarnessConfig
from harness.isolation import base_env, make_home, run
from harness.models import CIJob, CorpusEntry, OutOfScopeReason, PartialOutOfScope
from harness.pack import ToolPack
from harness.slots import SlotPool

CD_HINT = re.compile(r"\b(publish|release|deploy|pypi|npm publish|docker push|goreleaser|upload)\b", re.I)
NON_LINUX = re.compile(r"\b(windows|macos|mac-os|osx)\b", re.I)
# Publishing commands in a job body; only trusted for workflows that don't also run on PRs,
# since PR-triggered builds often upload artifacts too.
CD_STEP = re.compile(r"\b(twine|npm publish|docker push|goreleaser|gh release|pypi)\b", re.I)
BOT_HINT = re.compile(
    r"\b(stale|lock|label(er)?|close|duplicates?|backport|triage|cleanup|translations?|greet|welcome|assign)\b",
    re.I,
)
# Events that run on code changes. A workflow with none of these is a bot if it's named like one;
# one triggered only by issue/discussion activity is a bot whatever its name.
CODE_EVENTS = {"push", "pull_request", "merge_group", "workflow_call"}
REPO_EVENTS = {
    "issues", "issue_comment", "discussion", "discussion_comment", "label", "milestone",
    "pull_request_review", "pull_request_review_comment",
}
AUTO_NOTE = "auto-proposed: review"


def _events(doc: dict) -> set[str]:
    # PyYAML (YAML 1.1) reads a bare `on:` key as the boolean True.
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(e) for e in on}
    return {str(e) for e in on} if isinstance(on, dict) else set()


def _is_bot(events: set[str], name: str) -> bool:
    if events and events <= REPO_EVENTS:
        return True
    return bool(BOT_HINT.search(name)) and not (events & CODE_EVENTS - {"pull_request"})


def inventory_ci(repo: Path) -> list[CIJob]:
    jobs: list[CIJob] = []
    wf_dir = repo / ".github" / "workflows"
    for wf in sorted([*wf_dir.glob("*.yml"), *wf_dir.glob("*.yaml")]):
        try:
            doc = yaml.safe_load(wf.read_text()) or {}
        except yaml.YAMLError:
            continue
        rel = str(wf.relative_to(repo))
        events = _events(doc)
        on_tags = "tags" in yaml.safe_dump(doc.get("on", doc.get(True)))
        for job_id, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            text = yaml.safe_dump(job)
            runs_on = str(job.get("runs-on", ""))
            ci = CIJob(workflow=rel, job=job_id, summary=str(job.get("name", "")))
            names = f"{wf.stem} {job_id} {job.get('name', '')}"
            matrix = yaml.safe_dump(((job.get("strategy") or {}).get("matrix")) or {})
            if _is_bot(events, names):
                ci.out_of_scope = OutOfScopeReason.automation
            elif CD_HINT.search(f"{rel} {job_id} {job.get('name', '')}") or (
                on_tags and "pull_request" not in events and CD_STEP.search(text)
            ):
                ci.out_of_scope = OutOfScopeReason.cd
            elif NON_LINUX.search(runs_on) and "matrix" not in runs_on:
                ci.out_of_scope = OutOfScopeReason.non_linux
            elif NON_LINUX.search(matrix):
                for m in sorted({m.lower() for m in NON_LINUX.findall(matrix)}):
                    ci.partial_out_of_scope.append(PartialOutOfScope(match=m, reason=OutOfScopeReason.non_linux))
            if ci.out_of_scope or ci.partial_out_of_scope:
                ci.note = AUTO_NOTE
            jobs.append(ci)
    for other in (".gitlab-ci.yml", "Jenkinsfile", ".circleci/config.yml", "azure-pipelines.yml"):
        if (repo / other).exists():
            jobs.append(CIJob(workflow=other, job="*", summary="non-GitHub CI config; inventory by hand"))
    return jobs


def _hand_made(old: CIJob, new: CIJob) -> bool:
    if old.note == AUTO_NOTE:
        return False
    return bool(old.out_of_scope or old.partial_out_of_scope or old.note) or old.summary != new.summary


def merge_ci(existing: list[CIJob], found: list[CIJob]) -> list[CIJob]:
    by_key = {(j.workflow, j.job): j for j in existing}
    out = []
    for j in found:
        old = by_key.pop((j.workflow, j.job), None)
        out.append(old if old and _hand_made(old, j) else j)
    out.extend(by_key.values())  # keep hand-added entries the scan didn't see
    return out


# Files that hold CI commands, and the task runners CI may call into. Tools reachable only
# outside CI (a package.json script CI never runs, a hand-run formatter) are bonus coverage.
CI_GLOBS = (
    ".github/**/*", ".gitlab-ci.yml", ".circleci/config.yml", "Jenkinsfile", "azure-pipelines.yml",
    "Makefile", "noxfile.py", ".ci/*", "ci/*",
)
RUNNERS = {
    "pre-commit": (".pre-commit-config.yaml",),
    "tox": ("tox.ini",),
    "mise": ("mise.toml", ".mise.toml"),
    "just": ("justfile", "Justfile"),
    "task": ("Taskfile.yml", "Taskfile.yaml"),
}
SKIP_DIRS = {".git", "node_modules", "vendor", ".venv"}


def _files(repo: Path, name: str) -> list[Path]:
    return sorted(f for f in repo.rglob(name) if f.is_file() and not SKIP_DIRS & set(f.relative_to(repo).parts))


def ci_text(repo: Path) -> str:
    parts = [f.read_text(errors="replace") for g in CI_GLOBS for f in sorted(repo.glob(g)) if f.is_file()]
    text = "\n".join(parts)
    pending = dict(RUNNERS)
    while hit := [r for r in pending if re.search(rf"\b{re.escape(r)}\b", text)]:  # runners call runners
        for runner in hit:
            parts += [f.read_text(errors="replace") for n in pending.pop(runner) for f in _files(repo, n)]
        text = "\n".join(parts)
    if re.search(r"\btox\b", text) and (repo / "pyproject.toml").exists():
        tox = tomllib.loads((repo / "pyproject.toml").read_text()).get("tool", {}).get("tox")
        if tox:
            parts.append(json.dumps(tox))
    if re.search(r"\b(npm|pnpm|yarn|bun|nub)\b", text):
        for f in _files(repo, "package.json"):
            try:
                parts.append(json.dumps(json.loads(f.read_text()).get("scripts", {})))
            except (json.JSONDecodeError, AttributeError):
                continue
    return "\n".join(parts)


def classify_coverage(entry: CorpusEntry, recommended: list[str], in_ci, text: str) -> None:
    """Split recommended modules into expected (CI runs it) and bonus (dev-only) coverage.

    Modules classified on an earlier capture keep their place, so hand moves stick; new
    ones are proposed from ``in_ci`` and marked for review. Hand-added bonus entries stay.
    """
    recommended = [m for m in recommended if m not in entry.coverage_exclude]
    known = set(entry.expected_coverage) | set(entry.bonus_coverage)
    expected, bonus = [], {}
    for m in recommended:
        if m in entry.bonus_coverage:
            bonus[m] = entry.bonus_coverage[m]
        elif m in known or in_ci(m, text) is not False:
            expected.append(m)
        else:
            bonus[m] = f"not run in CI ({AUTO_NOTE})"
    for m, why in entry.bonus_coverage.items():
        if m not in bonus and AUTO_NOTE not in why:
            bonus[m] = why
    entry.expected_coverage, entry.bonus_coverage = expected, bonus


def capture(entry: CorpusEntry, pack: ToolPack, config: HarnessConfig, log=print) -> CorpusEntry:
    if not entry.sha:
        raise ValueError(f"{entry.name}: pin a SHA first")
    pool = SlotPool(config)
    cache = pool.cached_repo(entry, upstream=True)
    with tempfile.TemporaryDirectory(prefix=f"capture-{entry.name}-", dir=config.cache) as tmp:
        tmp_path = Path(tmp)
        wd = tmp_path / entry.name
        run(["git", "clone", "-q", "--no-checkout", str(cache), str(wd)])
        run(["git", "checkout", "-q", entry.sha], cwd=wd)
        log(f"inventorying CI in {entry.name}@{entry.sha[:12]}")
        entry.ci = merge_ci(entry.ci, inventory_ci(wd))
        home = make_home(tmp_path, config)
        env = base_env(home, config, git_name="harness", git_email="harness@localhost")
        log("capturing expected coverage")
        coverage, entry.expected_coverage_raw = pack.capture_coverage(wd, env)
        classify_coverage(entry, coverage, pack.coverage_in_ci, ci_text(wd))
        shutil.rmtree(wd, ignore_errors=True)
    return entry


def write_entry(path: Path, entry: CorpusEntry) -> None:
    data = entry.model_dump(mode="json", exclude_defaults=False)
    for job in data["ci"]:
        for k in ("partial_out_of_scope", "note", "out_of_scope"):
            if not job.get(k):
                job.pop(k, None)
    for k in ("extra", "coverage_exclude", "bonus_coverage"):
        if not data.get(k):
            data.pop(k, None)
    path.write_text(yaml.safe_dump(data, sort_keys=False, width=100))
