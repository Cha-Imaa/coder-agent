"""Node functions and routing for the agent graph.

Nodes are plain functions `state -> partial state`. They are built by factories that close over
the model and the tools so the graph can be assembled with a real LLM in production and a fake
one in tests. Nothing here talks to the network directly; the model and the tools do.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from coder_agent.config import settings
from coder_agent.graph import testing
from coder_agent.graph.context import manage_context
from coder_agent.graph.prompts import ACT_SYSTEM, CONTEXT_SECTION, PLAN_SYSTEM, REFLECT_SYSTEM
from coder_agent.graph.state import AgentState
from coder_agent.telemetry.ledger import usage_from_message

Node = Callable[[AgentState], dict[str, Any]]


def context_section(context: str) -> str:
    """The retrieved-code block for a prompt, or nothing when retrieval found nothing or is off."""
    return CONTEXT_SECTION.format(context=context) if context else ""


def make_plan_node(llm: BaseChatModel) -> Node:
    """One model call, no tools: turn the task into a numbered plan stored in `state.plan`.

    Planning separately from acting gives the model a moment to think before it can touch files,
    and gives the user something to read (and later approve) before any edit happens. On later
    iterations the failing test output is included so the plan is revised, not repeated.
    """

    def plan(state: AgentState) -> dict[str, Any]:
        user = f"Repository: {state['repo']}\n\nTask:\n{state['task']}"
        if state.get("context"):
            user += context_section(state["context"])
        if state.get("tests_passed") is False:
            user += (
                "\n\nA previous attempt was made and the tests failed with:\n"
                f"{state.get('test_output', '')}\n\nRevise the plan accordingly."
            )
        response = llm.invoke([SystemMessage(PLAN_SYSTEM), HumanMessage(user)])
        patch: dict[str, Any] = {
            "plan": str(response.text).strip(),
            "iteration": state.get("iteration", 0) + 1,
            "status": "running",
            "usage": usage_from_message("plan", response),
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
        history, rewritten = manage_context(llm, state["messages"])
        system = SystemMessage(
            ACT_SYSTEM.format(
                task=state["task"],
                plan=state.get("plan", ""),
                context=context_section(state.get("context", "")),
            )
        )
        response = model.invoke([system, *history])
        # If context management changed the history, replace the stored conversation with the
        # compacted one: `add_messages` cannot edit in place, so remove all and re-add in order.
        patch = [RemoveMessage(id=REMOVE_ALL_MESSAGES), *history] if rewritten else []
        return {
            "messages": [*patch, response],
            "steps": state.get("steps", 0) + 1,
            "usage": usage_from_message("act", response),
        }

    return act


def run_tests(state: AgentState) -> dict[str, Any]:
    """Run the repository's tests ourselves and record the verdict.

    This is the agent's ground truth. The model may claim the tests pass; only this node decides.
    With no detectable test command there is nothing to verify, so the run is accepted as-is and
    the summary says so.
    """
    command = state.get("test_command")
    if not command:
        return {"tests_passed": None, "test_output": "", "status": "passed"}
    result = testing.run_tests(Path(state["repo"]), command)
    passed = result.exit_code == 0
    return {
        "tests_passed": passed,
        "test_output": result.as_text(),
        "status": "passed" if passed else "failed",
    }


def reflect(state: AgentState) -> dict[str, Any]:
    """Feed the failing test output back into the conversation as a new human turn.

    No model call here: the value is in *what* the model is shown next. Appending the failure as
    a message keeps the full edit history visible, so the model can see what it already tried.
    """
    return {"messages": [HumanMessage(REFLECT_SYSTEM.format(test_output=state["test_output"]))]}


def finish(state: AgentState) -> dict[str, Any]:
    """Terminal node: derive the final status and a summary from the last model message."""
    messages = state.get("messages", [])
    last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    summary = str(last_ai.text).strip() if last_ai else "No response from the model."
    status = state.get("status", "running")
    if state.get("steps", 0) >= settings.max_steps and status != "passed":
        status = "gave_up"
    elif status == "running":
        status = "passed"
    if state.get("tests_passed") is None and state.get("test_command") is None:
        summary += "\n\n(No test command detected; the change was not verified by tests.)"
    return {"summary": summary, "status": status}


def route_after_act(state: AgentState) -> str:
    """After `act`: run the requested tools; if the model stopped, verify; if it ran too long, stop."""
    if state.get("steps", 0) >= settings.max_steps:
        return "finish"
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "run_tests"


def route_after_tests(state: AgentState) -> str:
    """After `run_tests`: done if green; otherwise loop back while iterations remain."""
    if state.get("tests_passed") is not False:
        return "finish"
    if state.get("iteration", 0) >= settings.max_iterations:
        return "finish"
    return "reflect"
