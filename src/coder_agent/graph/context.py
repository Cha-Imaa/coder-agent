"""Keep the conversation inside the model's context window.

Two techniques, applied before every `act` call:

1. *Compaction.* Old tool outputs are the bulk of any coding conversation (file reads, test
   logs). Once the model has acted on them they are rarely needed verbatim, so all but the most
   recent ones are replaced by a short stub that says how to get the content back (re-read).
   The message stays in place, so every `tool_call` still has its `ToolMessage` partner.

2. *Summarisation.* If the conversation is still above the token budget, the middle is replaced
   by one model-written summary. The first message (the task) and a recent tail are kept
   verbatim. The cut is made at a message boundary that keeps tool calls and results together.

Both return a rebuilt message list; the caller decides whether to write it back to state.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately

from coder_agent.config import settings

STUB_MARKER = "[earlier tool output trimmed"

SUMMARY_SYSTEM = """\
Summarise the following portion of a coding session so the agent can continue without it.
Keep, in this order: files read or changed and what was changed; commands run and their exit
codes; errors seen and how they were resolved; anything still unresolved. Be concrete: use
file paths, function names, exact error text. Use bullet points. No preamble."""


def compact_tool_outputs(
    messages: list[AnyMessage],
    keep_recent: int | None = None,
    stub_chars: int | None = None,
) -> list[AnyMessage]:
    """Replace all but the `keep_recent` most recent long tool outputs with a short stub."""
    keep_recent = settings.keep_recent_tool_outputs if keep_recent is None else keep_recent
    stub_chars = settings.tool_output_stub_chars if stub_chars is None else stub_chars

    tool_indexes = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    to_compact = set(tool_indexes[:-keep_recent] if keep_recent else tool_indexes)

    out: list[AnyMessage] = []
    for i, m in enumerate(messages):
        if i in to_compact and isinstance(m, ToolMessage):
            text = m.text
            if len(text) > stub_chars and STUB_MARKER not in text:
                head = text[:stub_chars].rstrip()
                stub = f"{head}\n... {STUB_MARKER}, {len(text) - stub_chars} chars omitted; call the tool again if you need it]"
                m = m.model_copy(update={"content": stub})
        out.append(m)
    return out


def _cut_index(messages: list[AnyMessage], keep_tail: int) -> int:
    """Index where the kept tail starts. Never split an AI tool call from its ToolMessages."""
    idx = max(1, len(messages) - keep_tail)
    while idx < len(messages) and isinstance(messages[idx], ToolMessage):
        idx += 1
    return idx


def summarize_if_needed(
    llm: BaseChatModel,
    messages: list[AnyMessage],
    budget_tokens: int | None = None,
    keep_tail: int = 8,
) -> tuple[list[AnyMessage], bool]:
    """Fold the middle of an over-budget conversation into one summary message.

    Returns the (possibly rebuilt) list and whether a summary was made. The first message is
    always kept: it is the task.
    """
    budget_tokens = settings.context_budget_tokens if budget_tokens is None else budget_tokens
    if count_tokens_approximately(messages) <= budget_tokens:
        return messages, False

    cut = _cut_index(messages, keep_tail)
    middle = messages[1:cut]
    if not middle:
        return messages, False

    transcript = "\n\n".join(f"[{m.type}] {m.text}" for m in middle if m.text)
    summary = llm.invoke([SystemMessage(SUMMARY_SYSTEM), HumanMessage(transcript)])
    note = HumanMessage(
        "Summary of the work so far (earlier messages were condensed to save context):\n"
        f"{summary.text.strip()}"
    )
    return [messages[0], note, *messages[cut:]], True


def manage_context(llm: BaseChatModel, messages: list[AnyMessage]) -> tuple[list[AnyMessage], bool]:
    """Compaction first (free), then summarisation (one model call) only if still over budget."""
    compacted = compact_tool_outputs(messages)
    changed = any(a is not b for a, b in zip(compacted, messages, strict=True))
    result, summarized = summarize_if_needed(llm, compacted)
    return result, changed or summarized


def is_ai_with_tool_calls(m: AnyMessage) -> bool:
    return isinstance(m, AIMessage) and bool(m.tool_calls)
