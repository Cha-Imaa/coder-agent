"""Graph state: the single object every node reads and returns a patch of.

LangGraph keeps one state per run. Each node receives the whole state and returns only the keys
it changed; LangGraph merges them. For `messages` the merge is `add_messages`, which appends (and
replaces by id), so the conversation with the model grows across nodes without any node having
to copy the list.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

Status = Literal["running", "passed", "failed", "gave_up"]


class AgentState(TypedDict, total=False):
    # Inputs
    task: str
    repo: str

    # Conversation with the model: tool calls and tool results live here.
    messages: Annotated[list[AnyMessage], add_messages]

    # Produced by `plan`, injected into every `act` prompt.
    plan: str

    # Loop bookkeeping. `iteration` counts plan->act->test cycles, `steps` counts model calls in
    # `act` so a model stuck calling tools forever is cut off.
    iteration: int
    steps: int

    # Verification. `test_command` is detected from the repo (or given by the user) before the
    # run starts; `run_tests` fills the rest after each act phase.
    test_command: str | None
    test_output: str
    tests_passed: bool | None

    # Filled by run_tests and finish.
    status: Status
    summary: str
