"""Node functions and routing for the agent graph.

Nodes are plain functions `state -> partial state`. They are built by factories that close over
the model and the tools so the graph can be assembled with a real LLM in production and a fake
one in tests. Nothing here talks to the network directly; the model and the tools do.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from coder_agent.config import settings
from coder_agent.graph.prompts import ACT_SYSTEM, PLAN_SYSTEM
from coder_agent.graph.state import AgentState

Node = Callable[[AgentState], dict[str, Any]]


def make_plan_node(llm: BaseChatModel) -> Node:
    """One model call, no tools: turn the task into a numbered plan stored in `state.plan`.

    Planning separately from acting gives the model a moment to think before it can touch files,
    and gives the user something to read (and later approve) before any edit happens.
    """

    def plan(state: AgentState) -> dict[str, Any]:
        user = f"Repository: {state['repo']}\n\nTask:\n{state['task']}"
        if state.get("test_output"):
            user += (
                "\n\nA previous attempt was made and the tests failed with:\n"
                f"{state['test_output']}\n\nRevise the plan accordingly."
            )
        response = llm.invoke([SystemMessage(PLAN_SYSTEM), HumanMessage(user)])
        patch: dict[str, Any] = {
            "plan": str(response.content).strip(),
            "iteration": state.get("iteration", 0) + 1,
            "status": "running",
        }
        # On the first pass the conversation is empty: seed it with the task so `act` has a
        # human turn to respond to. On later passes the history already exists.
        if not state.get("messages"):
            patch["messages"] = [HumanMessage(state["task"])]
        return patch

    return plan


def make_act_node(llm: BaseChatModel, tools: list[BaseTool]) -> Node:
    """The ReAct step: model sees the plan and the conversation, answers with tool calls or text.

    The system prompt is rebuilt on every call rather than stored in `messages`, so the plan can
    change between iterations without editing history.
    """
    model = llm.bind_tools(tools)

    def act(state: AgentState) -> dict[str, Any]:
        system = SystemMessage(ACT_SYSTEM.format(task=state["task"], plan=state.get("plan", "")))
        response = model.invoke([system, *state["messages"]])
        return {"messages": [response], "steps": state.get("steps", 0) + 1}

    return act


def finish(state: AgentState) -> dict[str, Any]:
    """Terminal node: derive a status and a summary from the last model message."""
    messages = state.get("messages", [])
    last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    summary = str(last_ai.content).strip() if last_ai else "No response from the model."
    status = state.get("status", "running")
    if status == "running":
        # Milestone 3 sets passed/failed from the tests. Until then, "done" means the model
        # stopped calling tools; hitting the step cap means it never converged.
        status = "gave_up" if state.get("steps", 0) >= settings.max_steps else "passed"
    return {"summary": summary, "status": status}


def route_after_act(state: AgentState) -> str:
    """After `act`: run the requested tools, or stop if the model answered in prose or ran too long."""
    if state.get("steps", 0) >= settings.max_steps:
        return "finish"
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "finish"
