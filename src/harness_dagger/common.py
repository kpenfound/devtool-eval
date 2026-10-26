from __future__ import annotations

import re
import subprocess
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
NOISE = re.compile(r"^\[dagger x-release\]")


def clean(text: str) -> str:
    return "\n".join(
        ANSI.sub("", line) for line in text.splitlines() if not NOISE.match(ANSI.sub("", line))
    )


def dagger(
    args: list[str],
    *,
    cwd: Path | None,
    env: dict[str, str],
    timeout: float | None = 600,
) -> subprocess.CompletedProcess[str]:
    """Run the dagger CLI with plain, uncolored output. Never raises on non-zero exit."""
    full_env = {**env, "NO_COLOR": "1", "TERM": "dumb"}
    try:
        p = subprocess.run(
            ["dagger", *args],
            cwd=cwd,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return subprocess.CompletedProcess(["dagger", *args], 124, out, f"timed out after {timeout}s")
    except FileNotFoundError:
        return subprocess.CompletedProcess(["dagger", *args], 127, "", "dagger: command not found")
    return subprocess.CompletedProcess(p.args, p.returncode, clean(p.stdout), clean(p.stderr))

