"""Turn graph events into terminal output.

The graph streams one update per node: `{"plan": {...}}`, `{"act": {...}}`, `{"tools": {...}}`,
`{"finish": {...}}`. This module knows how to render each of them with Rich. The formatting
helpers are pure functions on messages so they can be unit-tested without a terminal.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

ARG_PREVIEW = 80
RESULT_PREVIEW_LINES = 12


def _short(value: Any, limit: int = ARG_PREVIEW) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = s.replace("\n", "\\n")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def format_tool_call(call: dict[str, Any]) -> str:
    """`edit_file(path='a.py', old_string='...', new_string='...')`, one line, arguments trimmed."""
    args = ", ".join(f"{k}={_short(v)!r}" if isinstance(v, str) else f"{k}={_short(v)}"
                     for k, v in call["args"].items())
    return f"{call['name']}({args})"


def format_tool_result(msg: ToolMessage, max_lines: int = RESULT_PREVIEW_LINES) -> str:
    """First lines of a tool result, with a marker when the rest is hidden from the terminal.

    The model always sees the full result; this is only what the human sees.
    """
    lines = msg.text.splitlines() or [""]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines]) + f"\n… ({len(lines) - max_lines} more lines)"


def is_diff(text: str) -> bool:
    return text.startswith("OK: edited") and "\n@@" in text


class Renderer:
    """Stateful printer for a run: knows the console and keeps output compact."""

    def __init__(self, console: Console | None = None, verbose: bool = False) -> None:
        self.console = console or Console()
        self.verbose = verbose

    def header(self, repo: str, task: str, model: str) -> None:
        self.console.print(Panel(Text(task, style="bold"), title=f"coder run · {model}",
                                 subtitle=repo, border_style="cyan"))

    def update(self, node: str, patch: dict[str, Any]) -> None:
        handler = getattr(self, f"_on_{node}", None)
        if handler:
            handler(patch)

    def _on_plan(self, patch: dict[str, Any]) -> None:
        title = f"Plan · iteration {patch.get('iteration', 1)}"
        self.console.print(Panel(patch.get("plan", ""), title=title, border_style="magenta"))

    def _on_act(self, patch: dict[str, Any]) -> None:
        for msg in patch.get("messages", []):
            if not isinstance(msg, AIMessage):
                continue
            if msg.tool_calls:
                for call in msg.tool_calls:
                    self.console.print(Text("→ ", style="yellow") + Text(format_tool_call(call)))
            elif msg.text.strip():
                self.console.print(Panel(msg.text.strip(), title="Agent", border_style="green"))

    def _on_tools(self, patch: dict[str, Any]) -> None:
        for msg in patch.get("messages", []):
            if not isinstance(msg, ToolMessage):
                continue
            body = msg.text
            if is_diff(body):
                first, _, diff = body.partition("\n")
                self.console.print(Text("  " + first, style="dim"))
                self.console.print(Syntax(diff, "diff", theme="ansi_dark", word_wrap=True))
            else:
                style = "red" if body.startswith("ERROR") else "dim"
                preview = body if self.verbose else format_tool_result(msg)
                self.console.print(Text(indent(preview), style=style))

    def _on_finish(self, patch: dict[str, Any]) -> None:
        # The summary was already printed by `_on_act` as the model's last message; here we only
        # add the verdict so the run ends on one unambiguous line.
        status = patch.get("status", "?")
        color = {"passed": "green", "failed": "red", "gave_up": "yellow"}.get(status, "white")
        self.console.rule(f"[bold {color}]{status}[/bold {color}]", style=color)


def indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())
