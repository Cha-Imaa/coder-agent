"""Turn graph events into terminal output.

The graph streams one update per node: `{"plan": {...}}`, `{"act": {...}}`, `{"tools": {...}}`,
`{"finish": {...}}`. This module knows how to render each of them with Rich. The formatting
helpers are pure functions on messages so they can be unit-tested without a terminal.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.text import Text

from coder_agent.graph.approval import rejected

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


def preview_call(call: dict[str, Any]) -> tuple[str, str | None]:
    """(headline, body) for an approval prompt: a diff for edits, the content for writes, the
    command for shell. The body is what the human actually needs to read before saying yes."""
    args = call["args"]
    name = call["name"]
    if name == "edit_file":
        diff = difflib.unified_diff(
            str(args.get("old_string", "")).splitlines(),
            str(args.get("new_string", "")).splitlines(),
            fromfile=str(args.get("path", "")), tofile=str(args.get("path", "")), lineterm="",
        )
        return f"edit {args.get('path', '?')}", "\n".join(diff)
    if name == "write_file":
        content = str(args.get("content", ""))
        lines = content.splitlines()
        return f"write {args.get('path', '?')} ({len(lines)} lines)", content
    if name == "run_command":
        return "run command", f"$ {args.get('command', '')}"
    return format_tool_call(call), None


def parse_decision(answer: str) -> bool | str:
    """`y`/`yes`/empty approves; `n`/`no` rejects; anything else rejects with that text as the
    reason the model gets to read."""
    text = answer.strip()
    if text.lower() in ("", "y", "yes"):
        return True
    if text.lower() in ("n", "no"):
        return False
    return text


class Renderer:
    """Stateful printer for a run: knows the console and keeps output compact."""

    def __init__(self, console: Console | None = None, verbose: bool = False) -> None:
        self.console = console or Console()
        self.verbose = verbose

    def header(self, repo: str, task: str, model: str, thread: str | None = None) -> None:
        title = f"coder run · {model}" + (f" · thread {thread}" if thread else "")
        self.console.print(Panel(Text(task, style="bold"), title=title, subtitle=repo,
                                 border_style="cyan"))

    def update(self, node: str, patch: dict[str, Any]) -> None:
        handler = getattr(self, f"_on_{node}", None)
        if handler:
            handler(patch)

    def _on_retrieve_context(self, patch: dict[str, Any]) -> None:
        locations = patch.get("retrieved") or []
        if locations:
            listed = ", ".join(locations)
            self.console.print(f"[dim]Context · {len(locations)} chunks: {listed}[/dim]")

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

    def _on_run_tests(self, patch: dict[str, Any]) -> None:
        passed = patch.get("tests_passed")
        if passed is None:
            self.console.print(Text("tests: no test command detected, skipping", style="yellow"))
            return
        verdict = Text("tests: passed", style="bold green") if passed else Text("tests: failed", style="bold red")
        self.console.print(verdict)
        if not passed or self.verbose:
            lines = patch.get("test_output", "").splitlines()
            shown = lines if self.verbose else lines[-RESULT_PREVIEW_LINES:]
            self.console.print(Text(indent("\n".join(shown)), style="dim"))

    def ask_approval(self, payload: dict[str, Any]) -> bool | str:
        """Show each pending risky call with its diff / content / command and ask once for all.

        Passed to `run_agent(approve=...)`; the graph is paused (and checkpointed) while this
        blocks, so Ctrl+C here and `--resume` later asks the same question again.
        """
        for call in payload.get("tool_calls", []):
            headline, body = preview_call(call)
            self.console.print(Text("? ", style="bold yellow") + Text(headline, style="bold"))
            if body and call["name"] == "edit_file":
                self.console.print(Syntax(body, "diff", theme="ansi_dark", word_wrap=True))
            elif body:
                self.console.print(Text(indent(body), style="dim"))
        answer = Prompt.ask("[bold yellow]Approve?[/bold yellow] [dim]y / n / a reason[/dim]",
                            console=self.console, default="y", show_default=False)
        return parse_decision(answer)

    def _on_approve(self, patch: dict[str, Any]) -> None:
        # An approval returns an empty patch; only a rejection has something to show.
        for msg in patch.get("messages", []):
            if isinstance(msg, ToolMessage) and rejected(msg):
                self.console.print(Text("✗ rejected, asking the agent to reconsider", style="yellow"))
                break

    def _on_reflect(self, patch: dict[str, Any]) -> None:
        self.console.print(Text("↻ feeding the failures back and re-planning", style="magenta"))

    def _on_finish(self, patch: dict[str, Any]) -> None:
        # The summary was already printed by `_on_act` as the model's last message; here we only
        # add the verdict so the run ends on one unambiguous line.
        status = patch.get("status", "?")
        color = {"passed": "green", "failed": "red", "gave_up": "yellow"}.get(status, "white")
        self.console.rule(f"[bold {color}]{status}[/bold {color}]", style=color)


def indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())
