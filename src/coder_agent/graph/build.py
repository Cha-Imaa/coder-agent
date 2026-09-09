"""Assemble the graph.

    START -> prepare -> plan -> act -> (tools -> act)* -> run_tests -> finish -> END
                          ^                                   |
                          +---------- reflect <-- failed & iteration < max_iterations

`ToolNode` is LangGraph's prebuilt node that reads the tool calls on the last AI message, runs
each tool, and appends one `ToolMessage` per call. It works with sync or async tools, which is
what lets our MCP tools (async, over stdio) plug in unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from coder_agent.graph import testing
from coder_agent.graph.nodes import (
    finish,
    make_act_node,
    make_plan_node,
    reflect,
    route_after_act,
    route_after_tests,
    run_tests,
)
from coder_agent.graph.state import AgentState


def prepare(state: AgentState) -> dict[str, Any]:
    """Detect the test command unless the caller supplied one. Runs once, before planning."""
    if "test_command" in state:
        return {}
    return {"test_command": testing.detect_test_command(Path(state["repo"]))}


def build_graph(
    llm: BaseChatModel, tools: list[BaseTool], checkpointer=None
) -> CompiledStateGraph:
    g = StateGraph(AgentState)
    g.add_node("prepare", prepare)
    g.add_node("plan", make_plan_node(llm))
    g.add_node("act", make_act_node(llm, tools))
    g.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    g.add_node("run_tests", run_tests)
    g.add_node("reflect", reflect)
    g.add_node("finish", finish)

    g.add_edge(START, "prepare")
    g.add_edge("prepare", "plan")
    g.add_edge("plan", "act")
    g.add_conditional_edges(
        "act", route_after_act, {"tools": "tools", "run_tests": "run_tests", "finish": "finish"}
    )
    g.add_edge("tools", "act")
    g.add_conditional_edges("run_tests", route_after_tests, {"reflect": "reflect", "finish": "finish"})
    g.add_edge("reflect", "plan")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)
