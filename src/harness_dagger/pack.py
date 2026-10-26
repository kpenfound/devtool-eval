"""The Dagger pack: wires the Dagger implementations into the harness's pack interfaces."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from harness.models import Fact, Score
from harness.pack import BasePack
from harness_dagger.analyzers import (
    CIChoiceAnalyzer,
    CommandAnalyzer,
    FidelityAnalyzer,
    ModuleStrategyAnalyzer,
)
from harness_dagger.common import dagger
from harness_dagger.environment import DaggerEnvironment, installed_version
from harness_dagger.integration import CloudChecks
from harness_dagger.verify import DaggerVerifier

DATA = Path(__file__).parent / "data"

RECOMMEND_RE = re.compile(r"dagger module install (\S+)")

# Referee overlap score at or below this means official coverage was reimplemented.
OVERLAP_PARTIAL_AT = 2

STRATEGY_VERDICT = {
    "official_only": "good",
    "official_custom": "good",
    "official_custom_overlap": "partial",
    "custom_only": "bad",
    "third_party_only": "report_only",
    "nothing_working": "fail",
}


class DaggerPack(BasePack):
    name = "dagger"
    report_facts = [
        "dagger.module_strategy", "dagger.ci_choice", "dagger.bonus_coverage",
        "integration.cloud_checks.green",
    ]
    version = "0.1.0"
    data_dir = DATA
    outcome_command = "dagger"  # docs-map outcome: the next dagger invocation after a visit

    def __init__(self, settings: dict | None = None):
        settings = settings or {}
        self.settings = settings
        self.environment = DaggerEnvironment(settings)
        self.verifier = DaggerVerifier(settings)
        self.analyzers = [
            ModuleStrategyAnalyzer(settings),
            CIChoiceAnalyzer(),
            FidelityAnalyzer(),
            CommandAnalyzer(),
        ]
        self.integrations = [CloudChecks(settings)]

    def snapshot_paths(self) -> list[str]:
        return [
            "dagger.toml",
            "dagger.lock",
            "dagger.json",
            ".dagger/**/*",
            ".github/workflows/*",
            ".gitlab-ci.yml",
            "Jenkinsfile",
            ".env.dagger*",
        ]

    def capture_coverage(self, workdir: Path, env: dict[str, str]) -> tuple[list[str], str]:
        """Expected official coverage, from ``dagger module recommend`` on a clean checkout."""
        env = {**env, **self.environment.selector_env(self.settings.get("coverage_version"))}
        version = installed_version(env)
        if not (workdir / "dagger.toml").exists():
            init = dagger(["init"], cwd=workdir, env=env, timeout=600)
            if init.returncode:
                raise RuntimeError(f"dagger init failed: {init.stderr or init.stdout}")
        p = dagger(["module", "recommend", "--progress=plain"], cwd=workdir, env=env, timeout=900)
        raw = p.stdout + "\n" + p.stderr
        if p.returncode:
            raise RuntimeError(f"dagger module recommend failed:\n{raw[-3000:]}")
        sources = sorted(set(RECOMMEND_RE.findall(raw)))
        found = [line.split("|", 1)[1].strip() for line in raw.splitlines()
                 if "|" in line and ": found " in line]
        return sources, f"dagger {version} module recommend\n" + "\n".join(found)

    def coverage_in_ci(self, module: str, ci_text: str) -> bool | None:
        """Language base modules (``dagger.io/go``) build and test, which CI always does;
        a tool module counts if its tool's name appears in the CI config."""
        parts = module.split("/")
        if len(parts) <= 2:
            return True
        return re.search(rf"\b{re.escape(parts[-1])}\b", ci_text, re.I) is not None

    def combine_verdicts(self, facts: list[Fact], scores: list[Score]) -> dict[str, Any]:
        f = {x.key: x.value for x in facts}
        strategy = f.get("dagger.module_strategy", "nothing_working")
        overlap = next((s for s in scores if s.criterion == "overlap"), None)
        source = "deterministic"
        if strategy == "official_custom" and overlap is not None:
            source = "referee:overlap"
            if overlap.score <= OVERLAP_PARTIAL_AT:
                strategy = "official_custom_overlap"
        elif strategy == "official_custom_overlap":
            source = "check.skip"
        verdict = STRATEGY_VERDICT.get(strategy, "unknown")
        if strategy == "custom_only" and f.get("dagger.official_coverage_available") is False:
            # Nothing official to install, so a custom module is the right call.
            verdict, source = "good", "no_official_coverage"
        return {
            "module_strategy": strategy,
            "module_strategy_verdict": verdict,
            "module_strategy_source": source,
            "decorative_modules": f.get("dagger.decorative_modules", []),
            "ci_choice": f.get("dagger.ci_choice"),
        }
