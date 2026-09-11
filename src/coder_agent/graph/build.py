r"""Assemble the graph.

    START -> prepare -> retrieve_context -> plan -> act -> (tools -> act)* -> run_tests -> finish
                                              ^          \--> approve --/  |      |
                                              |         (risky call, human says yes/no)
                                              +------- reflect <-- failed & iteration < max_iterations

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
from coder_agent.graph.approval import approve, pending_risky_calls, route_after_approve
from coder_agent.graph.nodes import (
    finish,
    make_act_node,
    make_plan_node,
    reflect,
    route_after_act,
    route_after_tests,
    run_tests,
)
from coder_agent.graph.retrieval import Retriever, make_retrieve_node
from coder_agent.graph.state import AgentState


def prepare(state: AgentState) -> dict[str, Any]:
    """Detect the test command unless the caller supplied one. Runs once, before planning."""
    if "test_command" in state:
        return {}
    return {"test_command": testing.detect_test_command(Path(state["repo"]))}


def build_graph(
    llm: BaseChatModel,
    tools: list[BaseTool],
    checkpointer=None,
    retriever: Retriever | None = None,
    require_approval: bool = False,
) -> CompiledStateGraph:
    """`retriever=None` leaves the retrieve_context node in place but inert (empty context).

    `require_approval=True` adds an `approve` node between `act` and `tools` that interrupts the
    run whenever the model asks for a tool in `approval.RISKY_TOOLS`; it needs a checkpointer,
    because an interrupted run is resumed from its checkpoint.
    """
    g = StateGraph(AgentState)
    g.add_node("prepare", prepare)
    g.add_node("retrieve_context", make_retrieve_node(retriever))
    g.add_node("plan", make_plan_node(llm))
    g.add_node("act", make_act_node(llm, tools))
    g.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    g.add_node("run_tests", run_tests)
    g.add_node("reflect", reflect)
    g.add_node("finish", finish)

    g.add_edge(START, "prepare")
    g.add_edge("prepare", "retrieve_context")
    g.add_edge("retrieve_context", "plan")
    g.add_edge("plan", "act")

    if require_approval:
        g.add_node("approve", approve)

        def route(state: AgentState) -> str:
            target = route_after_act(state)
            return "approve" if target == "tools" and pending_risky_calls(state) else target

        g.add_conditional_edges(
            "act", route,
            {"approve": "approve", "tools": "tools", "run_tests": "run_tests", "finish": "finish"},
        )
        g.add_conditional_edges("approve", route_after_approve, {"tools": "tools", "act": "act"})
    else:
        g.add_conditional_edges(
            "act", route_after_act, {"tools": "tools", "run_tests": "run_tests", "finish": "finish"}
        )
    g.add_edge("tools", "act")
    g.add_conditional_edges("run_tests", route_after_tests, {"reflect": "reflect", "finish": "finish"})
    g.add_edge("reflect", "plan")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)
