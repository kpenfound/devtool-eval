import json

from harness.events import EventKind, normalize_claude, normalize_codex


def _line(seq, typ, data, phase="task"):
    return {"seq": seq, "ts": 0.0, "agent": "x", "phase": phase, "attempt": 0, "type": typ, "data": data}


def test_claude_tool_call_merges_result():
    raw = [
        _line(1, "harness_prompt", {"prompt": "do it"}),
        _line(2, "SystemMessage", {"subtype": "init", "data": {"model": "m", "tools": ["Bash"]}}),
        _line(3, "AssistantMessage", {"content": [
            {"_type": "TextBlock", "text": "running"},
            {"_type": "ToolUseBlock", "id": "t1", "name": "Bash", "input": {"command": "dagger check"}},
            {"_type": "ToolUseBlock", "id": "t2", "name": "WebFetch", "input": {"url": "https://docs.dagger.io/x"}},
        ]}),
        _line(4, "UserMessage", {"content": [
            {"_type": "ToolResultBlock", "tool_use_id": "t1", "content": "boom\nExit code 2", "is_error": True},
            {"_type": "ToolResultBlock", "tool_use_id": "t2", "content": [{"type": "text", "text": "page"}]},
        ]}),
        _line(5, "ResultMessage", {"subtype": "success", "num_turns": 3, "total_cost_usd": 0.5, "result": "done"}),
    ]
    events = normalize_claude(raw)
    kinds = [e.kind for e in events]
    assert kinds == [EventKind.user_message, EventKind.system, EventKind.assistant_message,
                     EventKind.shell, EventKind.web_fetch, EventKind.result]
    shell = events[3]
    assert shell.command == "dagger check" and shell.exit_code == 2 and shell.is_error
    assert events[4].url == "https://docs.dagger.io/x" and events[4].text == "page"
    assert [e.id for e in events][:2] == ["e00001", "e00002"]


def test_codex_items():
    raw = [
        _line(1, "harness_prompt", {"prompt": "do it"}),
        _line(2, "item/completed", {"item": {"type": "commandExecution", "id": "c1", "command": "dagger check",
                                             "aggregatedOutput": "ok", "exitCode": 0}}),
        _line(3, "item/completed", {"item": {"type": "fileChange", "id": "f1", "status": "completed",
                                             "changes": [{"path": "dagger.toml", "diff": "+x", "kind": "add"}]}}),
        _line(4, "item/completed", {"item": {"type": "webSearch", "id": "w1", "query": "dagger check",
                                             "action": {"type": "openPage", "url": "https://docs.dagger.io/a"}}}),
        _line(5, "item/completed", {"item": {"type": "agentMessage", "id": "a1", "text": "done"}}),
        _line(6, "turn/completed", {"turn": {"status": "completed"}}),
    ]
    events = normalize_codex(raw)
    assert [e.kind for e in events] == [EventKind.user_message, EventKind.shell, EventKind.file_change,
                                        EventKind.web_fetch, EventKind.assistant_message, EventKind.result]
    assert events[1].exit_code == 0 and not events[1].is_error
    assert events[2].paths == ["dagger.toml"]
    assert events[3].url == "https://docs.dagger.io/a"


def test_claude_resumed_session_cost(tmp_path):
    from harness.agents.claude import session_cost

    raw = tmp_path / "transcript.raw.jsonl"
    rows = [
        {"type": "ResultMessage", "data": {"session_id": "s1", "total_cost_usd": 0.21}},
        {"type": "ResultMessage", "data": {"session_id": "other", "total_cost_usd": 9.0}},
        {"type": "ResultMessage", "data": {"session_id": "s1", "total_cost_usd": 0.40}},
    ]
    raw.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert session_cost(raw, "s1") == 0.40
    assert session_cost(raw, None) == 0.0
    assert session_cost(tmp_path / "missing.jsonl", "s1") == 0.0
