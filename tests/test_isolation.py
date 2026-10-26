import os
import subprocess
import sys
from pathlib import Path

from harness.isolation import clean_path, neutral_python


def test_neutral_python_hides_venv(tmp_path):
    py = neutral_python(tmp_path)
    assert py == str(tmp_path / "runtime" / "bin" / "python")
    assert neutral_python(tmp_path) == py  # idempotent
    out = subprocess.run(
        [py, "-P", "-c", "import sys, harness, claude_agent_sdk, codex_cli_bin; "
         "print(sys.prefix); print(claude_agent_sdk.__file__); print(codex_cli_bin.__file__)"],
        capture_output=True, text=True, check=True, cwd=tmp_path,
    ).stdout.split()
    assert all(line.startswith(str(tmp_path / "runtime")) for line in out), out
    assert "prompt" not in (tmp_path / "runtime" / "pyvenv.cfg").read_text()


def test_clean_path_drops_harness_venv():
    venv_bin = str(Path(sys.prefix) / "bin")
    cleaned = clean_path(os.pathsep.join([venv_bin, "/usr/bin", "/bin"]))
    assert cleaned == os.pathsep.join(["/usr/bin", "/bin"])


def test_codex_refreshed_auth_synced_back(tmp_path):
    import json

    from harness.agents.base import AgentInvocation
    from harness.agents.codex import CodexAdapter
    from harness.models import AgentKind, Budget, Phase

    src = tmp_path / "auth.json"
    src.write_text(json.dumps({"last_refresh": "2026-10-16T18:22:03Z", "tokens": {"refresh_token": "old"}}))
    copy = tmp_path / "home" / ".codex" / "auth.json"
    copy.parent.mkdir(parents=True)
    inv = AgentInvocation(agent=AgentKind.codex, phase=Phase.task, prompt="p", workdir=str(tmp_path),
                          home=str(tmp_path / "home"), run_dir=str(tmp_path), env={}, budget=Budget(),
                          agent_config={"auth_json": str(src)})
    copy.write_text(json.dumps({"last_refresh": "2026-10-01T00:00:00Z", "tokens": {"refresh_token": "older"}}))
    CodexAdapter().sync_auth(inv)
    assert json.loads(src.read_text())["tokens"]["refresh_token"] == "old"
    copy.write_text(json.dumps({"last_refresh": "2026-10-26T09:00:00.5Z", "tokens": {"refresh_token": "new"}}))
    CodexAdapter().sync_auth(inv)
    assert json.loads(src.read_text())["tokens"]["refresh_token"] == "new"
    assert src.stat().st_mode & 0o777 == 0o600
