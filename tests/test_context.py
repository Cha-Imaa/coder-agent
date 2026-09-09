"""Context management: compaction of old tool outputs and summarisation when over budget."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from coder_agent.config import settings
from coder_agent.graph import build_graph
from coder_agent.graph.context import (
    STUB_MARKER,
    compact_tool_outputs,
    manage_context,
    summarize_if_needed,
)
from tests.fakes import echo, scripted, tool_call


def conversation(n_tool_rounds: int, size: int = 1000) -> list:
    """task, then n rounds of (AI tool call, big ToolMessage)."""
    msgs = [HumanMessage("the task")]
    for i in range(n_tool_rounds):
        msgs.append(tool_call("read_file", call_id=f"c{i}", path=f"f{i}.py"))
        msgs.append(ToolMessage(content=f"content-{i} " + "x" * size, tool_call_id=f"c{i}", id=f"t{i}"))
    return msgs


def pairs_intact(msgs: list) -> bool:
    """Every ToolMessage must directly follow an AI message that requested its call id."""
    for i, m in enumerate(msgs):
        if isinstance(m, ToolMessage):
            prev = msgs[i - 1]
            while isinstance(prev, ToolMessage):
                i -= 1
                prev = msgs[i - 1]
            if not (isinstance(prev, AIMessage) and m.tool_call_id in {c["id"] for c in prev.tool_calls}):
                return False
    return True


# --- compaction ------------------------------------------------------------------------------


def test_compaction_stubs_old_outputs_and_keeps_recent():
    msgs = conversation(5)
    out = compact_tool_outputs(msgs, keep_recent=2, stub_chars=50)

    tools = [m for m in out if isinstance(m, ToolMessage)]
    assert all(STUB_MARKER in m.text for m in tools[:3])
    assert all(STUB_MARKER not in m.text for m in tools[3:])
    assert tools[0].text.startswith("content-0")
    assert len(tools[0].text) < 200
    # Same ids and positions: tool_call pairing untouched.
    assert [m.id for m in out] == [m.id for m in msgs]
    assert pairs_intact(out)


def test_compaction_leaves_short_outputs_alone():
    msgs = conversation(4, size=10)
    out = compact_tool_outputs(msgs, keep_recent=1, stub_chars=400)
    assert all(a is b for a, b in zip(out, msgs, strict=True))


def test_compaction_is_idempotent():
    once = compact_tool_outputs(conversation(4), keep_recent=1, stub_chars=50)
    twice = compact_tool_outputs(once, keep_recent=1, stub_chars=50)
    assert [m.text for m in once] == [m.text for m in twice]


# --- summarisation ---------------------------------------------------------------------------


def test_no_summary_under_budget():
    llm = scripted("SHOULD NOT BE CALLED")
    _, made = summarize_if_needed(llm, conversation(3), budget_tokens=10**9)
    assert made is False and llm.calls == []


def test_summary_replaces_middle_and_keeps_task_and_tail():
    msgs = conversation(10)
    llm = scripted("- read f0..f5\n- nothing changed yet")
    out, made = summarize_if_needed(llm, msgs, budget_tokens=100, keep_tail=4)

    assert made is True
    assert out[0] is msgs[0]  # the task
    assert isinstance(out[1], HumanMessage) and "Summary of the work so far" in out[1].content
    assert "read f0..f5" in out[1].content
    assert out[-4:] == msgs[-4:]
    assert pairs_intact(out)
    # The summariser saw the middle transcript, not the tail.
    transcript = llm.calls[0][1].content
    assert "content-0" in transcript and "content-9" not in transcript


def test_summary_cut_never_orphans_a_tool_result():
    msgs = conversation(6)
    llm = scripted("summary")
    # keep_tail=3 would land on a ToolMessage; the cut must move past it.
    out, _ = summarize_if_needed(llm, msgs, budget_tokens=10, keep_tail=3)
    assert pairs_intact(out)
    assert not isinstance(out[2], ToolMessage)


# --- inside the graph ------------------------------------------------------------------------


def test_act_node_rewrites_state_when_context_is_compacted(monkeypatch):
    monkeypatch.setattr(settings, "keep_recent_tool_outputs", 1)
    monkeypatch.setattr(settings, "tool_output_stub_chars", 30)
    big = "y" * 500
    llm = scripted(
        "plan",
        tool_call("echo", call_id="a", text=big),
        tool_call("echo", call_id="b", text=big),
        tool_call("echo", call_id="c", text=big),
        "done",
    )
    out = build_graph(llm, [echo]).invoke({"task": "t", "repo": "/none", "test_command": None})

    tools = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert len(tools) == 3
    assert STUB_MARKER in tools[0].text and STUB_MARKER in tools[1].text
    assert STUB_MARKER not in tools[2].text
    assert pairs_intact(out["messages"])
    # The model was shown the compacted history on its last call.
    last_prompt = llm.calls[-1]
    assert sum(STUB_MARKER in m.text for m in last_prompt if isinstance(m, ToolMessage)) == 2


def test_manage_context_reports_no_change_for_small_history():
    llm = scripted("unused")
    msgs = conversation(2, size=10)
    out, changed = manage_context(llm, msgs)
    assert changed is False and out == msgs
