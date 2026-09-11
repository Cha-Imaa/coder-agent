"""Human-in-the-loop gate: pause before tools that change the repo or run commands.

`interrupt()` is LangGraph's way to stop a run and hand a value to the caller. It raises inside
the node, the checkpointer saves the state with this node still pending, and the stream ends with
an `__interrupt__` event carrying the payload. The caller answers with `Command(resume=value)`;
LangGraph re-runs the node from the top and `interrupt()` now returns that value instead of
raising. That is why the node does nothing before the call: everything before it would run twice.

A rejection is answered *inside the conversation*. Every tool call on the model's last message
gets a ToolMessage saying the user declined, so the provider sees a complete turn, and the graph
routes back to `act` for the model to try something else.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import interrupt

from coder_agent.graph.state import AgentState

# Tools that change files or execute code. Reading and searching never need a human.
RISKY_TOOLS = frozenset({"edit_file", "write_file", "run_command"})

# What the caller receives: the risky calls as {"name", "args", "id"} dicts, plus the step so a
# terminal can say "step 3 of this run".
Payload = dict[str, Any]

# What the caller answers with. `True` approves every call on the message; `False` or a string
# rejects them all, the string being feedback the model gets to read.
Decision = bool | str


def pending_risky_calls(state: AgentState) -> list[dict[str, Any]]:
    """The tool calls on the last AI message that need a human before they run."""
    last = state["messages"][-1] if state.get("messages") else None
    if not isinstance(last, AIMessage):
        return []
    return [c for c in last.tool_calls if c["name"] in RISKY_TOOLS]


def approve(state: AgentState) -> dict[str, Any]:
    """Interrupt with the risky calls; on approval change nothing, on rejection answer the calls."""
    risky = pending_risky_calls(state)
    payload: Payload = {"tool_calls": risky, "steps": state.get("steps", 0)}
    decision: Decision = interrupt(payload)
    if decision is True:
        return {}
    reason = decision if isinstance(decision, str) and decision.strip() else "no reason given"
    text = (
        f"Rejected by the user ({reason}). This tool call was not executed. "
        "Do not retry the same change; take the feedback into account or ask what to do."
    )
    last: AIMessage = state["messages"][-1]
    return {"messages": [ToolMessage(content=text, tool_call_id=c["id"]) for c in last.tool_calls]}


def route_after_approve(state: AgentState) -> str:
    """Approved: the AI message with its calls is still last, run them. Rejected: back to `act`."""
    return "tools" if isinstance(state["messages"][-1], AIMessage) else "act"


def rejected(message: ToolMessage) -> bool:
    return message.text.startswith("Rejected by the user")
