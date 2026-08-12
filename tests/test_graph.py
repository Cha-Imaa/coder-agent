"""Graph routing and state tests with a scripted LLM (no network, no API key)."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from coder_agent.config import settings
from coder_agent.graph import build_graph
from coder_agent.graph.nodes import route_after_act
from coder_agent.tools.client import load_tools
from tests.fakes import echo, fail, scripted, tool_call

INPUT = {"task": "add a greet() function", "repo": "/tmp/repo"}


def run(llm, tools=(echo, fail)):
    return build_graph(llm, list(tools)).invoke(INPUT)


# --- routing -------------------------------------------------------------------------------


def test_route_to_tools_when_ai_requests_a_call():
    state = {"messages": [tool_call("echo", text="x")], "steps": 1}
    assert route_after_act(state) == "tools"


def test_route_to_finish_on_plain_text():
    state = {"messages": [AIMessage("all done")], "steps": 1}
    assert route_after_act(state) == "finish"


def test_route_to_finish_when_step_cap_reached():
    state = {"messages": [tool_call("echo", text="x")], "steps": settings.max_steps}
    assert route_after_act(state) == "finish"


# --- whole graph ---------------------------------------------------------------------------


def test_plan_then_answer_without_tools():
    llm = scripted("1. read\n2. edit", "Done, added greet().")
    out = run(llm)

    assert out["plan"] == "1. read\n2. edit"
    assert out["iteration"] == 1
    assert out["steps"] == 1
    assert out["status"] == "passed"
    assert out["summary"] == "Done, added greet()."
    # messages: task, final answer
    assert [type(m) for m in out["messages"]] == [HumanMessage, AIMessage]


def test_plan_call_sees_task_and_no_tools_prompt():
    llm = scripted("plan", "done")
    run(llm)
    plan_prompt = llm.calls[0]
    assert isinstance(plan_prompt[0], SystemMessage)
    assert "add a greet() function" in plan_prompt[1].content
    assert "Repository: /tmp/repo" in plan_prompt[1].content


def test_act_prompt_contains_plan_and_conversation():
    llm = scripted("THE PLAN", "done")
    run(llm)
    act_prompt = llm.calls[1]
    assert "THE PLAN" in act_prompt[0].content
    assert isinstance(act_prompt[1], HumanMessage)
    assert act_prompt[1].content == INPUT["task"]


def test_tool_call_is_executed_and_result_fed_back():
    llm = scripted("plan", tool_call("echo", text="hi"), "finished")
    out = run(llm)

    msgs = out["messages"]
    assert [type(m) for m in msgs] == [HumanMessage, AIMessage, ToolMessage, AIMessage]
    assert msgs[2].content == "echo:hi"
    assert msgs[2].tool_call_id == "call_1"
    assert out["steps"] == 2
    # The second act call must see the tool result.
    assert any(isinstance(m, ToolMessage) for m in llm.calls[2])


def test_tool_error_becomes_a_message_not_a_crash():
    llm = scripted("plan", tool_call("fail", text="x"), "gave up")
    out = run(llm)
    tool_msg = out["messages"][2]
    assert isinstance(tool_msg, ToolMessage)
    assert "boom:x" in tool_msg.content
    assert out["status"] == "passed"  # the model still produced a final answer


def test_step_cap_stops_a_tool_loop(monkeypatch):
    monkeypatch.setattr(settings, "max_steps", 3)
    llm = scripted("plan", *[tool_call("echo", call_id=f"c{i}", text="x") for i in range(10)])
    out = run(llm)
    assert out["steps"] == 3
    assert out["status"] == "gave_up"


# --- with the real MCP tools ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_drives_real_mcp_tools(tmp_path: Path):
    (tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    tools = await load_tools(tmp_path)

    llm = scripted(
        "1. look around",
        tool_call("list_dir", call_id="c1"),
        tool_call("write_file", call_id="c2", path="new.txt", content="made by agent"),
        "Created new.txt",
    )
    out = await build_graph(llm, tools).ainvoke({"task": "create new.txt", "repo": str(tmp_path)})

    tool_results = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    # MCP tools return a list of content blocks, not a bare string; `.text` joins the text ones.
    assert "hello.py" in tool_results[0].text
    assert tool_results[1].text.startswith("OK: created new.txt")
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "made by agent"
    assert out["status"] == "passed"
