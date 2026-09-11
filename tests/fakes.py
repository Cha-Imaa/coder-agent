"""Test doubles shared across test modules.

`ScriptedLLM` replays a fixed list of AI messages, one per call, and records every prompt it was
given. It accepts `bind_tools` (returning itself) so the graph can be built exactly as in
production. This lets us test routing, state updates and tool execution deterministically and
without an API key.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import tool
from pydantic import Field


class ScriptedLLM(FakeMessagesListChatModel):
    calls: list[list[BaseMessage]] = Field(default_factory=list)
    # 1-based call numbers that raise instead of answering; the scripted reply is kept for the
    # retry, which is how a rate-limited node behaves when the run is resumed.
    fail_on_call: set[int] = Field(default_factory=set)

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedLLM:  # type: ignore[override]
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any):  # type: ignore[override]
        self.calls.append(list(messages))
        if len(self.calls) in self.fail_on_call:
            raise RuntimeError(f"fake provider failure on call {len(self.calls)}")
        return super()._generate(messages, *args, **kwargs)


def scripted(*responses: str | AIMessage) -> ScriptedLLM:
    msgs = [r if isinstance(r, AIMessage) else AIMessage(r) for r in responses]
    return ScriptedLLM(responses=msgs, calls=[])


def tool_call(name: str, call_id: str = "call_1", **args: Any) -> AIMessage:
    """An AI message that asks for exactly one tool call."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


@tool
def echo(text: str) -> str:
    """Return the text unchanged."""
    return f"echo:{text}"


@tool
def fail(text: str) -> str:
    """Always raise, to test error handling."""
    raise RuntimeError(f"boom:{text}")
