"""Assemble the graph.

    START -> plan -> act -> (tools -> act)* -> finish -> END

`ToolNode` is LangGraph's prebuilt node that reads the tool calls on the last AI message, runs
each tool, and appends one `ToolMessage` per call. It works with sync or async tools, which is
what lets our MCP tools (async, over stdio) plug in unchanged.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from coder_agent.graph.nodes import finish, make_act_node, make_plan_node, route_after_act
from coder_agent.graph.state import AgentState


def build_graph(
    llm: BaseChatModel, tools: list[BaseTool], checkpointer=None
) -> CompiledStateGraph:
    g = StateGraph(AgentState)
    g.add_node("plan", make_plan_node(llm))
    g.add_node("act", make_act_node(llm, tools))
    g.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    g.add_node("finish", finish)

    g.add_edge(START, "plan")
    g.add_edge("plan", "act")
    g.add_conditional_edges("act", route_after_act, {"tools": "tools", "finish": "finish"})
    g.add_edge("tools", "act")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)
