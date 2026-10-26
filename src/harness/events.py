"""Normalized event schema and per-agent normalizers.

The raw transcript (``transcript.raw.jsonl``) is the source of truth; normalized events are
derived from it and can be regenerated at any time with ``harness normalize``. The schema
is provisional until it has been checked against captured Claude and Codex runs (see the
first milestone in docs/DESIGN.md).

Raw line format, written by the agent adapters::

    {"seq": 12, "ts": 1761300000.1, "agent": "claude", "phase": "task", "attempt": 0,
     "type": "AssistantMessage", "data": {...}}
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterable, Iterator
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from harness.models import Phase

OUTPUT_LIMIT = 20_000  # chars of tool output kept per event; the raw transcript keeps all


class EventKind(StrEnum):
    user_message = "user_message"
    assistant_message = "assistant_message"
    reasoning = "reasoning"
    shell = "shell"
    file_change = "file_change"
    file_read = "file_read"
    web_fetch = "web_fetch"
    web_search = "web_search"
    mcp_call = "mcp_call"
    tool_call = "tool_call"  # any other tool
    system = "system"
    result = "result"
    error = "error"


class Event(BaseModel):
    id: str = ""
    seq: int = 0
    raw_seq: int
    agent: str
    phase: Phase
    attempt: int = 0
    ts: float | None = None
    kind: EventKind
    tool: str | None = None
    call_id: str | None = None
    text: str | None = None  # message text, reasoning, or tool output
    input: dict[str, Any] | None = None
    command: str | None = None
    exit_code: int | None = None
    is_error: bool | None = None
    url: str | None = None
    query: str | None = None
    paths: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)  # kind-specific extras (usage, cost, ...)

    def brief(self, limit: int = 400) -> str:
        """One-line-ish rendering used by the referee and reports."""
        head = f"[{self.id} {self.phase}{self.attempt or ''} {self.kind}"
        if self.tool:
            head += f" {self.tool}"
        head += "]"
        body = self.command or self.url or self.query or ""
        if self.paths and not body:
            body = ", ".join(self.paths)
        if self.text:
            text = self.text if len(self.text) <= limit else self.text[:limit] + " …[truncated]"
            body = f"{body}\n{text}" if body else text
        status = ""
        if self.exit_code is not None:
            status = f" (exit {self.exit_code})"
        elif self.is_error:
            status = " (error)"
        return f"{head}{status} {body}".rstrip()


# --- Raw serialization ---------------------------------------------------------------------


def to_jsonable(obj: Any) -> Any:
    """Serialize SDK objects (dataclasses, pydantic models, enums) keeping their type names."""
    if isinstance(obj, BaseModel):
        out = obj.model_dump(mode="json", by_alias=True, exclude_none=True)
        return {"_type": type(obj).__name__, **out} if isinstance(out, dict) else out
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out = {"_type": type(obj).__name__}
        for f in dataclasses.fields(obj):
            out[f.name] = to_jsonable(getattr(obj, f.name))
        return out
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, str | int | float | bool) or obj is None:
        return obj
    if isinstance(obj, Path):
        return str(obj)
    return repr(obj)


def read_raw(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= OUTPUT_LIMIT else text[:OUTPUT_LIMIT] + "\n…[truncated]"


# --- Claude ---------------------------------------------------------------------------------

_CLAUDE_FILE_CHANGE = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_CLAUDE_FILE_READ = {"Read", "Glob", "Grep", "LS"}


def _claude_block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                parts.append(c.get("text") or c.get("content") or json.dumps(c)[:2000])
            else:
                parts.append(str(c))
        return "\n".join(str(p) for p in parts)
    if content is None:
        return ""
    return json.dumps(content)


def _claude_tool_event(base: dict[str, Any], block: dict[str, Any]) -> Event:
    name = block.get("name", "")
    inp = block.get("input") or {}
    ev = Event(**base, kind=EventKind.tool_call, tool=name, call_id=block.get("id"), input=inp)
    if name == "Bash":
        ev.kind = EventKind.shell
        ev.command = inp.get("command")
    elif name == "WebFetch":
        ev.kind = EventKind.web_fetch
        ev.url = inp.get("url")
    elif name == "WebSearch":
        ev.kind = EventKind.web_search
        ev.query = inp.get("query")
    elif name in _CLAUDE_FILE_CHANGE:
        ev.kind = EventKind.file_change
        p = inp.get("file_path") or inp.get("notebook_path")
        ev.paths = [p] if p else []
    elif name in _CLAUDE_FILE_READ:
        ev.kind = EventKind.file_read
        p = inp.get("file_path") or inp.get("path") or inp.get("pattern")
        ev.paths = [p] if p else []
    elif name.startswith("mcp__"):
        ev.kind = EventKind.mcp_call
    return ev


def normalize_claude(raw: Iterable[dict[str, Any]]) -> list[Event]:
    events: list[Event] = []
    by_call: dict[str, Event] = {}
    for line in raw:
        data = line.get("data") or {}
        base = {
            "raw_seq": line["seq"],
            "agent": "claude",
            "phase": line.get("phase", "task"),
            "attempt": line.get("attempt", 0),
            "ts": line.get("ts"),
        }
        typ = line.get("type")
        if typ == "harness_prompt":
            events.append(Event(**base, kind=EventKind.user_message, text=data.get("prompt")))
        elif typ == "AssistantMessage":
            for block in data.get("content", []):
                btype = block.get("_type")
                if btype == "TextBlock":
                    events.append(
                        Event(**base, kind=EventKind.assistant_message, text=block.get("text"))
                    )
                elif btype == "ThinkingBlock":
                    events.append(
                        Event(**base, kind=EventKind.reasoning, text=block.get("thinking"))
                    )
                elif btype in ("ToolUseBlock", "ServerToolUseBlock"):
                    ev = _claude_tool_event(base, block)
                    events.append(ev)
                    if ev.call_id:
                        by_call[ev.call_id] = ev
            if data.get("error"):
                events.append(Event(**base, kind=EventKind.error, text=str(data["error"])))
        elif typ == "UserMessage":
            content = data.get("content")
            if isinstance(content, list):
                for block in content:
                    if block.get("_type") == "ToolResultBlock":
                        ev = by_call.get(block.get("tool_use_id", ""))
                        if ev is None:
                            continue
                        ev.text = _clip(_claude_block_text(block.get("content")))
                        ev.is_error = bool(block.get("is_error"))
                        if ev.kind == EventKind.shell:
                            ev.exit_code = _claude_exit_code(ev.text, ev.is_error)
            # Plain-string user messages here are replays of our own prompt; skip.
        elif typ == "SystemMessage":
            sub = data.get("subtype")
            if sub == "init":
                d = data.get("data", {})
                events.append(
                    Event(
                        **base,
                        kind=EventKind.system,
                        text="init",
                        data={
                            k: d.get(k)
                            for k in (
                                "model",
                                "tools",
                                "mcp_servers",
                                "permissionMode",
                                "skills",
                                "slash_commands",
                                "claude_code_version",
                                "session_id",
                            )
                            if k in d
                        },
                    )
                )
        elif typ == "ResultMessage":
            events.append(
                Event(
                    **base,
                    kind=EventKind.result,
                    text=data.get("result"),
                    is_error=data.get("is_error"),
                    data={
                        k: data.get(k)
                        for k in (
                            "subtype",
                            "num_turns",
                            "total_cost_usd",
                            "duration_ms",
                            "stop_reason",
                            "terminal_reason",
                            "usage",
                        )
                    },
                )
            )
        elif typ == "harness_error":
            events.append(Event(**base, kind=EventKind.error, text=data.get("error")))
    return _number(events)


_EXIT_RE = re.compile(r"(?:exit code|Exit code)[: ]+(\d+)")


def _claude_exit_code(text: str | None, is_error: bool) -> int | None:
    if text:
        m = _EXIT_RE.search(text)
        if m:
            return int(m.group(1))
    return 1 if is_error else 0


# --- Codex ----------------------------------------------------------------------------------


def normalize_codex(raw: Iterable[dict[str, Any]]) -> list[Event]:
    events: list[Event] = []
    for line in raw:
        data = line.get("data") or {}
        base = {
            "raw_seq": line["seq"],
            "agent": "codex",
            "phase": line.get("phase", "task"),
            "attempt": line.get("attempt", 0),
            "ts": line.get("ts"),
        }
        typ = line.get("type")
        if typ == "harness_prompt":
            events.append(Event(**base, kind=EventKind.user_message, text=data.get("prompt")))
            continue
        if typ == "harness_error":
            events.append(Event(**base, kind=EventKind.error, text=data.get("error")))
            continue
        if typ != "item/completed" and typ != "turn/completed" and typ != "error":
            continue
        if typ == "turn/completed":
            turn = data.get("turn", {})
            events.append(
                Event(
                    **base,
                    kind=EventKind.result,
                    is_error=turn.get("status") == "failed",
                    text=(turn.get("error") or {}).get("message"),
                    data={"status": turn.get("status"), "duration_ms": turn.get("durationMs")},
                )
            )
            continue
        if typ == "error":
            events.append(Event(**base, kind=EventKind.error, text=json.dumps(data)[:4000]))
            continue
        item = data.get("item", {})
        itype = item.get("type")
        if itype == "agentMessage":
            events.append(Event(**base, kind=EventKind.assistant_message, text=item.get("text")))
        elif itype == "reasoning":
            text = "\n".join(item.get("summary") or []) or "\n".join(item.get("content") or [])
            events.append(Event(**base, kind=EventKind.reasoning, text=text))
        elif itype == "commandExecution":
            events.append(
                Event(
                    **base,
                    kind=EventKind.shell,
                    tool="shell",
                    call_id=item.get("id"),
                    command=item.get("command"),
                    text=_clip(item.get("aggregatedOutput")),
                    exit_code=item.get("exitCode"),
                    is_error=(item.get("exitCode") not in (0, None)),
                )
            )
        elif itype == "fileChange":
            changes = item.get("changes") or []
            events.append(
                Event(
                    **base,
                    kind=EventKind.file_change,
                    tool="apply_patch",
                    call_id=item.get("id"),
                    paths=[c.get("path", "") for c in changes],
                    text=_clip("\n".join(c.get("diff", "") for c in changes)),
                    is_error=item.get("status") == "failed",
                )
            )
        elif itype == "webSearch":
            action = item.get("action") or {}
            url = action.get("url")
            events.append(
                Event(
                    **base,
                    kind=EventKind.web_fetch if url else EventKind.web_search,
                    tool="web_search",
                    call_id=item.get("id"),
                    query=item.get("query"),
                    url=url,
                    data={"action": action},
                )
            )
        elif itype == "mcpToolCall":
            result = item.get("result")
            events.append(
                Event(
                    **base,
                    kind=EventKind.mcp_call,
                    tool=f"mcp__{item.get('server')}__{item.get('tool')}",
                    call_id=item.get("id"),
                    input=item.get("arguments") if isinstance(item.get("arguments"), dict) else None,
                    text=_clip(json.dumps(result) if result is not None else None),
                    is_error=item.get("error") is not None,
                )
            )
        elif itype == "userMessage":
            continue  # our own prompt, already recorded as harness_prompt
        else:
            events.append(
                Event(**base, kind=EventKind.tool_call, tool=itype, data={"item": item})
            )
    return _number(events)


def _number(events: list[Event]) -> list[Event]:
    for i, ev in enumerate(events, start=1):
        ev.seq = i
        ev.id = f"e{i:05d}"
    return events


NORMALIZERS = {"claude": normalize_claude, "codex": normalize_codex}


def normalize(agent: str, raw_path: Path) -> list[Event]:
    return NORMALIZERS[agent](read_raw(raw_path))


def write_events(events: list[Event], path: Path) -> None:
    with path.open("w") as f:
        for ev in events:
            f.write(ev.model_dump_json(exclude_none=True) + "\n")


def read_events(path: Path) -> list[Event]:
    if not path.exists():
        return []
    return [Event.model_validate_json(line) for line in path.read_text().splitlines() if line]
