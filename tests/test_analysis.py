from harness.analysis import harness_exposure
from harness.events import Event, EventKind


def test_harness_exposure():
    def ev(id, **kw):
        return Event(id=id, raw_seq=1, agent="c", phase="task", **kw)

    clean = [ev("e1", kind=EventKind.shell, command="dagger module recommend", text="dagger.io/python/pytest")]
    assert harness_exposure(clean).value is False
    events = clean + [
        ev("e2", kind=EventKind.web_search, query="dagger flask ci", text="github.com/kpenfound/devtool-eval - ..."),
        ev("e3", kind=EventKind.shell, command="git clone https://github.com/kpenfound/devtool-eval"),
        ev("e4", kind=EventKind.mcp_call, input={"url": "https://github.com/kpenfound/DevTool_Eval/blob/main/rubric.yaml"}),
    ]
    fact = harness_exposure(events)
    assert fact.value is True
    assert [e.event for e in fact.evidence] == ["e2", "e3", "e4"]
