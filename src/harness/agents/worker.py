"""Worker entry point: ``python -m harness.agents.worker <invocation.json> <result.json>``."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

from harness.agents.base import AgentInvocation, RawWriter, get_adapter
from harness.models import AgentTurnResult


def main() -> int:
    spec_path, result_path = Path(sys.argv[1]), Path(sys.argv[2])
    inv = AgentInvocation.model_validate_json(spec_path.read_text())
    out = RawWriter(inv)
    out.write("harness_prompt", {"prompt": inv.prompt, "resume": inv.resume_session})
    try:
        result = get_adapter(inv.agent).invoke(inv, out)
    except BaseException as e:  # noqa: BLE001 - everything is recorded, nothing is swallowed
        out.write("harness_error", {"error": f"{type(e).__name__}: {e}", "tb": traceback.format_exc()})
        session = inv.session_file.read_text().strip() if inv.session_file.exists() else None
        result = AgentTurnResult(
            phase=inv.phase,
            attempt=inv.attempt,
            prompt=inv.prompt,
            session_id=session or inv.resume_session,
            status="error",
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        out.close()
    result_path.write_text(result.model_dump_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
