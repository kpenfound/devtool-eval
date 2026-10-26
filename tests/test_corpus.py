from harness.corpus import AUTO_NOTE, inventory_ci, merge_ci
from harness.models import CIJob

WORKFLOWS = {
    "tests.yml": "on: [push, pull_request]\njobs:\n  test:\n    runs-on: ${{ matrix.os }}\n"
                 "    strategy:\n      matrix:\n        os: [ubuntu-latest, macOS-latest, macos-13]\n",
    "stale.yml": "on:\n  schedule:\n    - cron: '0 0 * * *'\njobs:\n  stale:\n    runs-on: ubuntu-latest\n",
    "auto-close.yml": "on:\n  issues:\n    types: [opened]\njobs:\n  close_template:\n    runs-on: ubuntu-latest\n",
    "pr-label-validation.yml": "on:\n  pull_request_target:\n    types: [labeled]\n"
                               "jobs:\n  validate-release-label:\n    runs-on: ubuntu-latest\n",
    "publish.yml": "on:\n  push:\n    tags: ['v*']\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
                   "    steps:\n      - run: twine upload dist/*\n",
}


def test_inventory_ci(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    for name, text in WORKFLOWS.items():
        (wf / name).write_text(text)
    jobs = {j.job: j for j in inventory_ci(tmp_path)}
    assert [(p.match, p.reason.value) for p in jobs["test"].partial_out_of_scope] == [("macos", "non-linux")]
    assert jobs["stale"].out_of_scope.value == "automation"
    assert jobs["close_template"].out_of_scope.value == "automation"
    assert jobs["validate-release-label"].out_of_scope.value == "automation"
    assert jobs["build"].out_of_scope.value == "cd"  # tag-triggered upload; the bare `on:` key parses as True


def test_merge_keeps_hand_edits_only():
    found = [CIJob(workflow="w", job="a", out_of_scope="cd", note=AUTO_NOTE), CIJob(workflow="w", job="b")]
    existing = [
        CIJob(workflow="w", job="a", out_of_scope="automation", note=AUTO_NOTE),  # old proposal: replaced
        CIJob(workflow="w", job="b", summary="hand-written summary"),  # hand edit: kept
        CIJob(workflow="w", job="gone", summary="added by hand"),  # not in scan: kept
    ]
    merged = {j.job: j for j in merge_ci(existing, found)}
    assert merged["a"].out_of_scope.value == "cd"
    assert merged["b"].summary == "hand-written summary"
    assert "gone" in merged


def test_capture_respects_coverage_exclude(tmp_path, monkeypatch):
    from harness import corpus
    from harness.config import HarnessConfig
    from harness.models import CorpusEntry

    class Pool:
        def __init__(self, config):
            pass

        def cached_repo(self, entry):
            return tmp_path / "cache.git"

    class Pack:
        def capture_coverage(self, wd, env):
            return ["dagger.io/go", "dagger.io/go/staticcheck"], "raw"

        def coverage_in_ci(self, module, text):
            return True

    monkeypatch.setattr(corpus, "SlotPool", Pool)
    monkeypatch.setattr(corpus, "run", lambda *a, **k: None)
    monkeypatch.setattr(corpus, "inventory_ci", lambda wd: [])
    entry = CorpusEntry(name="x", url="u", sha="abc", coverage_exclude={"dagger.io/go/staticcheck": "vendored"})
    out = corpus.capture(entry, Pack(), HarnessConfig(state_dir=str(tmp_path / "state")), log=lambda m: None)
    assert out.expected_coverage == ["dagger.io/go"]


def test_classify_coverage_bonus():
    from harness.corpus import classify_coverage
    from harness.models import CorpusEntry
    from harness_dagger.pack import DaggerPack

    pack = DaggerPack({})
    ci = "run: staticcheck ./...\nrun: go test ./..."
    entry = CorpusEntry(name="x", url="u", expected_coverage=["dagger.io/js/eslint"],
                        bonus_coverage={"dagger.io/js/oxlint": "hand-added"})
    rec = ["dagger.io/go", "dagger.io/go/staticcheck", "dagger.io/js/prettier", "dagger.io/js/eslint"]
    classify_coverage(entry, rec, pack.coverage_in_ci, ci)
    assert entry.expected_coverage == ["dagger.io/go", "dagger.io/go/staticcheck", "dagger.io/js/eslint"]
    assert AUTO_NOTE in entry.bonus_coverage["dagger.io/js/prettier"]  # not in CI: proposed bonus
    assert entry.bonus_coverage["dagger.io/js/oxlint"] == "hand-added"  # hand entries stay
    # a hand move from bonus to expected sticks on recapture
    entry.expected_coverage.append("dagger.io/js/prettier")
    del entry.bonus_coverage["dagger.io/js/prettier"]
    classify_coverage(entry, rec, pack.coverage_in_ci, ci)
    assert "dagger.io/js/prettier" in entry.expected_coverage


def test_ci_text_follows_runners(tmp_path):
    import json

    from harness.corpus import ci_text

    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/ci.yml").write_text("jobs:\n  t:\n    steps:\n      - run: tox -e lint && pnpm test\n")
    (tmp_path / "tox.ini").write_text("[testenv:lint]\ncommands = pre-commit run --all-files\n")
    (tmp_path / ".pre-commit-config.yaml").write_text("repos:\n  - hooks: [{id: ruff}]\n")
    (tmp_path / "web").mkdir()
    (tmp_path / "web/package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/.prettierrc").write_text("{}")
    text = ci_text(tmp_path)
    assert "ruff" in text and "vitest" in text  # tox -> pre-commit -> ruff; pnpm -> scripts
    assert "prettier" not in text
