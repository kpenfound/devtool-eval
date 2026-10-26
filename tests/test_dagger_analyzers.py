from conftest import make_actx, shell

from harness.models import Score
from harness_dagger.analyzers import (
    CIChoiceAnalyzer,
    CommandAnalyzer,
    FidelityAnalyzer,
    ModuleStrategyAnalyzer,
    diff_by_file,
    module_kind,
)
from harness_dagger.pack import DaggerPack
from harness_dagger.verify import parse_check_table


def facts(found):
    return {f.key: f.value for f in found}


def test_module_kind():
    assert module_kind("dagger.io/python/pytest") == "official"
    assert module_kind("github.com/dagger/python/pytest@v0.2.0") == "official"
    assert module_kind(".dagger/modules/ci") == "custom"
    assert module_kind("ci") == "custom"
    assert module_kind("github.com/someone/android") == "third_party"


OFFICIAL = '[modules.pytest]\nsource = "dagger.io/python/pytest"\n'
CUSTOM = '[modules.ci]\nsource = ".dagger/modules/ci"\n'


def test_strategy_official_only(tmp_path, entry):
    f = facts(ModuleStrategyAnalyzer({}).analyze(make_actx(tmp_path, entry, files={"dagger.toml": OFFICIAL})))
    assert f["dagger.module_strategy"] == "official_only"
    assert f["dagger.expected_coverage_missing"] == ["dagger.io/python/ruff"]
    assert f["dagger.decorative_modules"] == []
    assert f["dagger.bonus_coverage"] is None  # entry has no bonus coverage


def test_bonus_coverage(tmp_path, entry):
    entry = entry.model_copy(update={"bonus_coverage": {"dagger.io/python/pytest": "dev only",
                                                         "dagger.io/js/prettier": "docs site"}})
    f = facts(ModuleStrategyAnalyzer({}).analyze(make_actx(tmp_path, entry, files={"dagger.toml": OFFICIAL})))
    assert f["dagger.bonus_coverage_installed"] == ["dagger.io/python/pytest"]
    assert f["dagger.bonus_coverage"] == "1/2"


def test_strategy_custom_only_and_decorative(tmp_path, entry):
    actx = make_actx(tmp_path, entry, files={"dagger.toml": CUSTOM}, checks=["ci:test"])
    f = facts(ModuleStrategyAnalyzer({}).analyze(actx))
    assert f["dagger.module_strategy"] == "custom_only"
    actx = make_actx(tmp_path, entry, files={"dagger.toml": OFFICIAL + CUSTOM}, checks=["ci:test"])
    f = facts(ModuleStrategyAnalyzer({}).analyze(actx))
    assert f["dagger.module_strategy"] == "official_custom"
    assert f["dagger.decorative_modules"] == ["pytest"]


def test_strategy_skip_reimplementation(tmp_path, entry):
    toml = OFFICIAL + '[modules.pytest.settings]\n"check.skip" = ["test"]\n' + CUSTOM
    f = facts(ModuleStrategyAnalyzer({}).analyze(make_actx(tmp_path, entry, files={"dagger.toml": toml})))
    assert f["dagger.reimplementation_via_skip"] is True
    assert f["dagger.module_strategy"] == "official_custom_overlap"


def test_strategy_nothing_working(tmp_path, entry):
    f = facts(ModuleStrategyAnalyzer({}).analyze(make_actx(tmp_path, entry, files={"dagger.toml": OFFICIAL}, passed=False)))
    assert f["dagger.module_strategy"] == "nothing_working"


def test_combine_verdicts_uses_referee_overlap():
    from harness.models import EvidenceRef, Fact

    pack = DaggerPack({})
    fs = [Fact(key="dagger.module_strategy", value="official_custom")]
    ev = [EvidenceRef(event="e00001")]
    assert pack.combine_verdicts(fs, [])["module_strategy_verdict"] == "good"
    v = pack.combine_verdicts(fs, [Score(criterion="overlap", score=2, rationale="", evidence=ev)])
    assert v["module_strategy"] == "official_custom_overlap" and v["module_strategy_verdict"] == "partial"


DIFF = """diff --git a/.github/workflows/tests.yaml b/.github/workflows/tests.yaml
--- a/.github/workflows/tests.yaml
+++ b/.github/workflows/tests.yaml
@@ -10,2 +10,3 @@ jobs:
   steps:
-    - run: tox
+    - run: dagger check
+    - run: pytest -k fast || true
diff --git a/tests/test_cli.py b/tests/test_cli.py
deleted file mode 100644
--- a/tests/test_cli.py
+++ /dev/null
@@ -1,1 +0,0 @@
-def test_x(): pass
"""


def test_diff_by_file():
    files = diff_by_file(DIFF)
    assert files[".github/workflows/tests.yaml"]["added"] == [(11, "    - run: dagger check"), (12, "    - run: pytest -k fast || true")]
    assert files["tests/test_cli.py"]["status"] == "deleted"


def test_ci_choice_and_fidelity(tmp_path, entry):
    actx = make_actx(tmp_path, entry, diff=DIFF)
    f = facts(CIChoiceAnalyzer().analyze(actx))
    assert f["dagger.ci_choice"] == "dagger_check_in_old_ci"
    actx.events = [shell("e00001", "dagger cloud checks on")]
    assert facts(CIChoiceAnalyzer().analyze(actx))["dagger.ci_choice"] == "cloud_checks"
    fid = FidelityAnalyzer().analyze(actx)
    flags = next(x for x in fid if x.key == "dagger.fidelity_red_flags")
    assert any("|| true" in q for q in flags.value) and flags.evidence[0].line == 12
    assert facts(fid)["dagger.deleted_test_files"] == ["tests/test_cli.py"]


def test_command_analyzer(tmp_path, entry):
    events = [shell("e00001", "dagger instal x", 1, "Error: unknown command \"instal\""),
              shell("e00002", "dagger check", 0)]
    f = facts(CommandAnalyzer().analyze(make_actx(tmp_path, entry, events=events)))
    assert f["dagger.command_errors"] == 1
    assert f["dagger.unknown_command_hints"] == ["dagger instal x"]
    assert f["dagger.first_passing_check_event"] == "e00002"


def test_parse_check_table():
    table = """
MODULE  PYTEST-PROJECT  PYTEST-TEST-FILE  CHECK  DESCRIPTION
pytest  *               *                 test   Run this test file.
ruff    *                                 lint   Lint.
# Use --all to expand collections and list each item.
"""
    assert parse_check_table(table) == ["pytest:test", "ruff:lint"]


def test_custom_only_is_good_without_official_coverage():
    from harness.models import Fact

    pack = DaggerPack({})
    fs = [Fact(key="dagger.module_strategy", value="custom_only")]
    assert pack.combine_verdicts(fs, [])["module_strategy_verdict"] == "bad"
    fs.append(Fact(key="dagger.official_coverage_available", value=False))
    v = pack.combine_verdicts(fs, [])
    assert (v["module_strategy_verdict"], v["module_strategy_source"]) == ("good", "no_official_coverage")
