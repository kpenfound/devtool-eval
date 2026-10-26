"""Verify: the harness runs ``dagger check`` itself. It never trusts the agent's "done"."""

from __future__ import annotations

import re
import time

from harness.models import VerifyResult
from harness.pack import RunContext
from harness_dagger.common import dagger

ERROR_TAIL = 80
MAX_EXCERPT = 150
FAILED_LINK = re.compile(r"dag://\?check=([^&\s]+)\S*\s+ERROR")
ERROR_START = re.compile(r"^\d+\s+:\s[┆ ]*! |^Error:|^! ")
SUMMARY = re.compile(r"^== [A-Z ]+ ==$")


def parse_check_table(text: str) -> list[str]:
    """``dagger check -l`` table → ["module:check", ...]."""
    lines = [line for line in text.splitlines() if line.strip()]
    header_idx = next((i for i, line in enumerate(lines) if line.startswith("MODULE")), None)
    if header_idx is None:
        return []
    header = lines[header_idx]
    check_col = header.find("CHECK")
    desc_col = header.find("DESCRIPTION")
    out = []
    for line in lines[header_idx + 1 :]:
        if line.startswith("#"):
            continue
        module = line.split()[0]
        if check_col >= 0:
            end = desc_col if desc_col > check_col else None
            check = line[check_col:end].strip().split()[0] if line[check_col:end].strip() else ""
        else:
            check = ""
        out.append(f"{module}:{check}" if check else module)
    return out


class DaggerVerifier:
    def __init__(self, settings: dict):
        self.timeout = settings.get("verify_timeout_s", 3600)

    def verify(self, ctx: RunContext, attempt: int) -> VerifyResult:
        started = time.monotonic()
        listing = dagger(["check", "-l"], cwd=ctx.workdir, env=ctx.env, timeout=600)
        checks = parse_check_table(listing.stdout) if listing.returncode == 0 else []
        p = dagger(["check", "--progress=plain"], cwd=ctx.workdir, env=ctx.env, timeout=self.timeout)
        log_name = "verify.log" if attempt == 0 else f"verify.{attempt}.log"
        log = (
            f"$ dagger check -l  (exit {listing.returncode})\n{listing.stdout}\n{listing.stderr}\n"
            f"$ dagger check --progress=plain  (exit {p.returncode})\n{p.stdout}\n{p.stderr}\n"
        )
        ctx.run_dir.joinpath(log_name).write_text(log)
        # The latest attempt is always also at verify.log for the referee.
        if attempt:
            ctx.run_dir.joinpath("verify.log").write_text(log)
        combined = (p.stdout + "\n" + p.stderr).strip()
        failed_names = set(FAILED_LINK.findall(combined))
        failed = sorted({c for c in checks if c.split(":")[-1] in failed_names} or failed_names)
        passed = p.returncode == 0 and bool(checks)
        excerpt = ""
        if not passed:
            if p.returncode == 0 and not checks:
                excerpt = "`dagger check` found no checks to run.\n\n" + _tail(combined)
            else:
                excerpt = error_excerpt(combined)
        return VerifyResult(
            passed=passed,
            exit_code=p.returncode,
            checks=checks,
            failed_checks=failed,
            log_path=log_name,
            error_excerpt=excerpt,
            duration_s=time.monotonic() - started,
            details={"list_exit_code": listing.returncode},
            attempt=attempt,
        )


def _tail(text: str) -> str:
    return "\n".join(text.splitlines()[-ERROR_TAIL:])


def error_excerpt(text: str) -> str:
    """What a user would paste back: the error blocks and the final summary, not the trace."""
    lines = text.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        if ERROR_START.search(lines[i]):
            j = i + 1
            while j < len(lines) and (lines[j].startswith((" ", "-", "\t")) and not re.match(r"^\d+\s+:", lines[j])):
                j += 1
            # Nested spans repeat the same error output; keep each indented line once.
            seen = set(blocks)
            blocks.extend(
                line for line in lines[i : min(j, i + 40)]
                if not (line.startswith((" ", "\t")) and line in seen)
            )
            i = j
        else:
            i += 1
    summary_at = max((k for k, line in enumerate(lines) if SUMMARY.match(line)), default=None)
    summary = lines[summary_at:] if summary_at is not None else []
    if len(summary) > 60:
        summary = [line for line in summary if "✔" not in line]
    out = blocks + ([""] if blocks and summary else []) + summary
    if not out:
        return _tail(text)
    return "\n".join(out[:MAX_EXCERPT])
