"""Human-in-the-loop gate: the graph pauses before risky tools and continues with the answer.

The graph is built with `require_approval=True` and an in-memory checkpointer (interrupts need
one). The scripted model asks for a fake `edit_file` tool; the tests answer the interrupt with
`Command(resume=...)` directly, and through `run_agent(approve=...)` for the wiring.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from typer.testing import CliRunner

from coder_agent.agent import resume_agent, run_agent
from coder_agent.cli import app
from coder_agent.graph import build_graph
from coder_agent.graph.approval import RISKY_TOOLS, pending_risky_calls, route_after_approve
from coder_agent.ui.render import parse_decision, preview_call
from tests.fakes import echo, scripted, tool_call

CFG = {"configurable": {"thread_id": "t"}}
INPUT = {"task": "change a.py", "repo": "/tmp/repo"}


@pytest.fixture
def edit_file():
    """A stand-in for the MCP edit tool that records whether it ever ran: `.tool` and `.calls`."""
    calls: list[dict] = []

    @tool
    def edit_file(path: str, old_string: str, new_string: str) -> str:
        """Replace text in a file (fake)."""
        calls.append({"path": path, "old": old_string, "new": new_string})
        return f"OK: edited {path}."

    return SimpleNamespace(tool=edit_file, calls=calls)


def stream(graph, graph_input):
    """Collect (node, patch) pairs and the interrupts of one stream pass."""
    events, interrupts = [], []
    for update in graph.stream(graph_input, CFG, stream_mode="updates"):
        for node, patch in update.items():
            (interrupts.extend(patch) if node == "__interrupt__" else events.append((node, patch)))
    return events, interrupts


# --- pure pieces ---------------------------------------------------------------------------


def test_only_risky_tools_need_approval():
    assert RISKY_TOOLS == {"edit_file", "write_file", "run_command"}
    msg = AIMessage(content="", tool_calls=[
        {"name": "read_file", "args": {"path": "a"}, "id": "1"},
        {"name": "edit_file", "args": {"path": "a", "old_string": "x", "new_string": "y"}, "id": "2"},
        {"name": "run_command", "args": {"command": "pytest"}, "id": "3"},
    ])
    assert [c["id"] for c in pending_risky_calls({"messages": [msg]})] == ["2", "3"]
    assert pending_risky_calls({"messages": [ToolMessage(content="x", tool_call_id="1")]}) == []
    assert pending_risky_calls({}) == []


def test_route_after_approve_by_last_message_type():
    assert route_after_approve({"messages": [AIMessage(content="", tool_calls=[])]}) == "tools"
    assert route_after_approve({"messages": [ToolMessage(content="Rejected", tool_call_id="1")]}) == "act"


def test_parse_decision():
    assert parse_decision("") is True and parse_decision("Y") is True and parse_decision("yes") is True
    assert parse_decision("n") is False and parse_decision("No ") is False
    assert parse_decision("edit b.py instead") == "edit b.py instead"


def test_preview_call_shows_a_diff_for_edits_and_the_command_for_shell():
    head, body = preview_call({"name": "edit_file", "args": {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}})
    assert head == "edit a.py" and "-x = 1" in body and "+x = 2" in body
    head, body = preview_call({"name": "run_command", "args": {"command": "pytest -q"}})
    assert head == "run command" and body == "$ pytest -q"
    head, body = preview_call({"name": "write_file", "args": {"path": "n.py", "content": "a\nb"}})
    assert head == "write n.py (2 lines)" and body == "a\nb"
    assert preview_call({"name": "echo", "args": {"text": "hi"}}) == ("echo(text='hi')", None)


# --- the gate in the graph ----------------------------------------------------------------


def test_risky_call_interrupts_and_approval_runs_it(edit_file):
    llm = scripted("plan", tool_call("edit_file", path="a.py", old_string="x", new_string="y"), "done")
    graph = build_graph(llm, [echo, edit_file.tool], checkpointer=MemorySaver(), require_approval=True)

    events, interrupts = stream(graph, INPUT)

    assert [n for n, _ in events] == ["prepare", "retrieve_context", "plan", "act"]
    assert len(interrupts) == 1
    payload = interrupts[0].value
    assert [c["name"] for c in payload["tool_calls"]] == ["edit_file"]
    assert payload["tool_calls"][0]["args"]["new_string"] == "y" and payload["steps"] == 1
    assert edit_file.calls == []  # nothing ran while the question is open
    assert graph.get_state(CFG).next == ("approve",)

    events, interrupts = stream(graph, Command(resume=True))

    assert interrupts == []
    assert [n for n, _ in events] == ["approve", "tools", "act", "run_tests", "finish"]
    assert edit_file.calls == [{"path": "a.py", "old": "x", "new": "y"}]
    assert graph.get_state(CFG).values["status"] == "passed"


def test_rejection_answers_every_call_and_returns_to_act(edit_file):
    ai = AIMessage(content="", tool_calls=[
        {"name": "echo", "args": {"text": "hi"}, "id": "c1"},
        {"name": "edit_file", "args": {"path": "a.py", "old_string": "x", "new_string": "y"}, "id": "c2"},
    ])
    llm = scripted("plan", ai, "ok, leaving a.py alone")
    graph = build_graph(llm, [echo, edit_file.tool], checkpointer=MemorySaver(), require_approval=True)
    stream(graph, INPUT)

    events, interrupts = stream(graph, Command(resume="a.py is generated, edit the template"))

    assert interrupts == []
    assert [n for n, _ in events] == ["approve", "act", "run_tests", "finish"]
    replies = events[0][1]["messages"]
    assert [m.tool_call_id for m in replies] == ["c1", "c2"]  # the safe call is answered too
    assert all("a.py is generated, edit the template" in m.text for m in replies)
    assert edit_file.calls == []
    # The model's next call saw the rejection as a tool result in its history.
    assert any(isinstance(m, ToolMessage) and "Rejected" in m.text for m in llm.calls[-1])
    assert graph.get_state(CFG).values["status"] == "passed"


def test_safe_calls_skip_the_gate(edit_file):
    llm = scripted("plan", tool_call("echo", text="hi"), "done")
    graph = build_graph(llm, [echo, edit_file.tool], checkpointer=MemorySaver(), require_approval=True)
    events, interrupts = stream(graph, INPUT)
    assert interrupts == []
    assert "approve" not in [n for n, _ in events] and "tools" in [n for n, _ in events]


def test_without_the_flag_there_is_no_gate(edit_file):
    llm = scripted("plan", tool_call("edit_file", path="a.py", old_string="x", new_string="y"), "done")
    graph = build_graph(llm, [echo, edit_file.tool], checkpointer=MemorySaver())
    _, interrupts = stream(graph, INPUT)
    assert interrupts == [] and len(edit_file.calls) == 1


# --- through run_agent -------------------------------------------------------------------


@pytest.fixture
def wired(monkeypatch, tmp_path, edit_file):
    import coder_agent.llm as llm_mod
    from coder_agent.graph import retrieval
    from coder_agent.tools import client

    llm = scripted("plan", tool_call("edit_file", path="a.py", old_string="x", new_string="y"), "done")

    async def fake_load_tools(repo, github=False):
        return [echo, edit_file.tool]

    monkeypatch.setattr(llm_mod, "get_llm", lambda: llm)
    monkeypatch.setattr(client, "load_tools", fake_load_tools)
    monkeypatch.setattr(retrieval, "default_retriever", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo, tmp_path / "ledger.sqlite", llm


async def test_run_agent_asks_the_hook_and_continues(wired, edit_file):
    repo, ledger, _ = wired
    asked: list[dict] = []

    def approve(payload):
        asked.append(payload)
        return True

    out = await run_agent(repo, "change a.py", approve=approve, ledger_path=ledger)

    assert out.status == "passed"
    assert [c["name"] for c in asked[0]["tool_calls"]] == ["edit_file"]
    assert len(edit_file.calls) == 1


async def test_run_agent_without_hook_never_asks(wired, edit_file):
    repo, ledger, _ = wired
    out = await run_agent(repo, "change a.py", ledger_path=ledger)
    assert out.status == "passed" and len(edit_file.calls) == 1


async def test_interrupted_at_the_prompt_then_resumed_asks_again(wired, edit_file):
    """Ctrl+C while the question is open leaves the checkpoint at `approve`; resuming re-asks."""
    repo, ledger, _ = wired

    def walk_away(payload):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await run_agent(repo, "change a.py", thread_id="t9", approve=walk_away, ledger_path=ledger)
    assert edit_file.calls == []

    asked: list[dict] = []
    out = await resume_agent(repo, "t9", approve=lambda p: asked.append(p) or True, ledger_path=ledger)

    assert len(asked) == 1 and out.status == "passed" and len(edit_file.calls) == 1


def test_cli_has_a_yes_flag():
    result = CliRunner().invoke(app, ["run", "--help"])
    # Typer forces coloured help when GITHUB_ACTIONS is set and splices escape codes into the
    # option names, so compare the plain text.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--yes" in plain and "-y" in plain
