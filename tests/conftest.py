from __future__ import annotations

from pathlib import Path

import pytest

from harness.events import Event, EventKind
from harness.models import AgentKind, CorpusEntry, RunMeta, RunSpec, VerifyResult
from harness.pack import AnalysisContext


@pytest.fixture
def entry() -> CorpusEntry:
    return CorpusEntry(
        name="flask", url="https://github.com/pallets/flask", sha="a" * 40,
        expected_coverage=["dagger.io/python/pytest", "dagger.io/python/ruff"],
    )


def make_actx(tmp_path: Path, entry: CorpusEntry, *, files=None, diff="", events=None,
              passed=True, checks=("pytest:test",)) -> AnalysisContext:
    meta = RunMeta(run_id="r1", spec=RunSpec(repo="flask", bundle="baseline", agent=AgentKind.claude))
    meta.verifies.append(VerifyResult(passed=passed, exit_code=0 if passed else 1, checks=list(checks)))
    return AnalysisContext(
        run_dir=tmp_path, workdir=None, meta=meta, entry=entry,
        events=events or [], diff=diff, final_files=files or {},
    )


def shell(id_: str, command: str, exit_code: int = 0, text: str = "") -> Event:
    return Event(id=id_, raw_seq=1, agent="claude", phase="task", kind=EventKind.shell,
                 command=command, exit_code=exit_code, text=text)
