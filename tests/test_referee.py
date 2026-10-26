from harness.docsmap import build, classify
from harness.events import Event, EventKind
from harness.models import Scorecard
from harness.referee.framework import Redactor, RefereeOutput, check_evidence, diff_files, enforce
from harness_dagger.pack import DaggerPack


def test_evidence_rule():
    events = {"e00001"}
    files = {"dagger.toml": 5}
    good, bad = check_evidence(
        [{"event": "e00001"}, {"event": "e99999"}, {"path": "dagger.toml", "line": 3},
         {"path": "dagger.toml", "line": 50}, {"path": "nope.txt", "line": 1}, {}],
        events, files,
    )
    assert [g.label() for g in good] == ["e00001", "dagger.toml:3"]
    assert len(bad) == 4


def test_enforce_drops_unsupported_scores():
    rubric = DaggerPack({}).rubric()
    card = Scorecard(run_id="r", rubric_version="x", referee_model="m", referee_prompt_version="1")
    raw = RefereeOutput(
        scores=[
            {"criterion": "fidelity", "score": 3, "rationale": "ok", "evidence": [{"event": "e00001"}]},
            {"criterion": "overlap", "score": 4, "rationale": "trust me", "evidence": []},
            {"criterion": "fidelity", "score": 1, "rationale": "dup", "evidence": [{"event": "e00001"}]},
            {"criterion": "made_up", "score": 2, "rationale": "x", "evidence": [{"event": "e00001"}]},
        ],
        friction=[{"summary": "s", "root_cause": "docs_gap", "evidence": [{"path": "dagger.toml", "line": 1}]}],
        issue_candidates=[{"title": "t", "body": "b", "category": "docs_gap", "evidence": [{"event": "e00001"}]}],
    )
    enforce(raw, rubric, {"e00001"}, {"dagger.toml": 2}, card)
    assert [(s.criterion, s.score) for s in card.scores] == [("fidelity", 3)]
    assert {r.reason.split(":")[0] for r in card.rejected} >= {"no valid evidence", "duplicate criterion", "invalid"}
    assert card.friction[0].root_cause == "docs_gap"
    assert card.issue_candidates[0].fingerprint


def test_redactor():
    r = Redactor(["docs-pointers", "Always run dagger module recommend before anything else"],
                 ["(?i)devtool-eval"], ["/tmp/run/home"])
    out = r("cat /tmp/run/home/.claude/CLAUDE.md # docs-pointers devtool-eval\n"
            "Always run dagger module recommend before anything else")
    assert "docs-pointers" not in out and "devtool-eval" not in out and "/tmp/run/home" not in out
    assert "recommend before" not in out


def test_diff_files():
    diff = "+++ b/dagger.toml\n@@ -0,0 +1,4 @@\n+a\n+b\n+c\n+d\n+++ /dev/null\n"
    assert diff_files(diff) == {"dagger.toml": 5}


def test_docs_map():
    sources = DaggerPack({}).docs_sources()
    assert classify("https://docs.dagger.io/cloud-checks", sources) == ("dagger-docs", "1.0")
    assert classify("https://github.com/dagger/dagger/blob/v0.19.2/README.md", sources) == ("dagger-github", "v0.19.2")
    assert classify("https://github.com/pallets/flask", sources)[0] == "other"
    events = [
        Event(id="e1", raw_seq=1, agent="c", phase="task", kind=EventKind.web_fetch, url="https://docs.dagger.io/x"),
        Event(id="e2", raw_seq=2, agent="c", phase="task", kind=EventKind.shell, command="ls", exit_code=0),
        Event(id="e3", raw_seq=3, agent="c", phase="task", kind=EventKind.shell, command="cd x && dagger check", exit_code=1),
        Event(id="e4", raw_seq=4, agent="c", phase="task", kind=EventKind.shell,
              command="curl -s https://docs.dagger.io/y", exit_code=0),
        Event(id="e5", raw_seq=5, agent="c", phase="task", kind=EventKind.shell,
              command="cat > ci.yaml <<'EOF'\n- run: curl https://dl.dagger.io/dagger/install.sh | sh\nEOF", exit_code=0),
    ]
    visits = build(events, sources, "dagger")
    assert [(v.url, v.outcome, v.outcome_event) for v in visits] == [
        ("https://docs.dagger.io/x", "error", "e3"),
        ("https://docs.dagger.io/y", "none", None),
    ]


def test_strict_schema_and_null_parsing():
    from harness.runner import feedback_schema, parse_feedback

    schema = feedback_schema()
    assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])
    fb = parse_feedback('{"summary": null, "difficulty": 3, "friction": [{"description": "d", "where": null, "cause_guess": null}]}')
    assert fb.parse_error is None and fb.difficulty == 3 and fb.friction[0].where == ""
