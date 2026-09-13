"""Recover tool calls that a model wrote as text.

Hosted models return tool calls as a structured field. Small local models served by Ollama
often know *what* to call but not *how* to say it: qwen2.5-coder-7b answers with the call as a
bare JSON object in the message text (`{"name": "read_file", "arguments": {...}}`), sometimes
after a paragraph of prose or inside a code fence, and without the `<tool_call>` tags its chat
template needs for the server to parse it. The graph then sees a message with no tool calls,
treats it as "the model is done", runs the tests, and every iteration ends the same way.

This module is the repair step between the model and the router: when a reply carries no
structured tool calls, the text is scanned for JSON objects that name a bound tool and carry an
arguments object, and those become real tool calls. Prose around them is kept as the content.
Nothing is done to a reply that already has structured calls, so hosted providers are untouched.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from langchain_core.messages import AIMessage

# Wrappers models put around a call. Fences and tags are stripped before scanning so the
# decoder sees the object itself; the closing counterparts are removed the same way.
WRAPPERS = re.compile(r"</?tool_call>|</?function_call>|```(?:json|python)?")
ARGUMENT_KEYS = ("arguments", "args", "parameters", "input")


def _objects(text: str) -> list[tuple[int, int, dict[str, Any]]]:
    """Every top-level JSON object in `text` with its span; nested objects stay inside their parent."""
    decoder = json.JSONDecoder()
    found: list[tuple[int, int, dict[str, Any]]] = []
    pos = 0
    while (start := text.find("{", pos)) != -1:
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        if isinstance(value, dict):
            found.append((start, end, value))
        pos = end
    return found


def _as_call(obj: dict[str, Any], tool_names: set[str]) -> dict[str, Any] | None:
    """A tool call dict if `obj` names a known tool with an arguments object, else None."""
    if isinstance(obj.get("function"), dict):  # OpenAI's nesting
        obj = obj["function"]
    name = obj.get("name")
    if name not in tool_names:
        return None
    args: Any = next((obj[key] for key in ARGUMENT_KEYS if key in obj), None)
    if isinstance(args, str):  # arguments serialised twice, as OpenAI does
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    if not isinstance(args, dict):
        return None
    return {"name": name, "args": args, "id": f"repaired_{uuid.uuid4().hex[:8]}", "type": "tool_call"}


def repair_tool_calls(message: AIMessage, tool_names: set[str]) -> AIMessage:
    """Return `message` with tool calls parsed out of its text when it had none.

    The returned message keeps id, usage and response metadata, so the ledger and the
    "which model answered" bookkeeping see the same object they would have without repair.
    """
    if message.tool_calls or not isinstance(message.content, str) or "{" not in message.content:
        return message
    text = WRAPPERS.sub("", message.content)
    calls: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []
    for start, end, obj in _objects(text):
        call = _as_call(obj, tool_names)
        if call:
            calls.append(call)
            spans.append((start, end))
    if not calls:
        return message
    prose = text
    for start, end in reversed(spans):
        prose = prose[:start] + prose[end:]
    return message.model_copy(update={"content": prose.strip(), "tool_calls": calls})
