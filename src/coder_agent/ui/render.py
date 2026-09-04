"""Turn graph events into terminal output.

The graph streams one update per node: `{"plan": {...}}`, `{"act": {...}}`, `{"tools": {...}}`,
`{"finish": {...}}`. This module knows how to render each of them with Rich.

Everything a run prints goes through one grid - gutter, icon, label, content - so the stages
(`Context`, `Plan`, `Executing`, `Tests`) line up down the left edge and their content down a
single column, whichever node happens to be speaking. The alternative, a panel per node, boxed
the same information three times over and made a two-minute run unreadable at a glance.

The quiet default is deliberate: a successful `read_file` or `search_code` tells the reader
nothing they cannot infer from the call that produced it, so only diffs, errors and test verdicts
are printed. `--verbose` restores the full tool output. The model always sees everything either
way; this module only decides what the human sees.

The formatting helpers are pure functions on text and messages so they can be unit-tested
without a terminal.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from rich.console import Console, RenderableType
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from coder_agent.graph.approval import rejected

ARG_PREVIEW = 80
RESULT_PREVIEW_LINES = 12
TAIL_LINES = 8
SUMMARY_PREVIEW = 400

# Stage grid geometry. The first three columns are fixed so that separately printed rows still
# align: the run is a stream, not one table built at the end.
GUTTER = 2
ICON_WIDTH = 3
LABEL_WIDTH = 13

# Icon and colour per stage. Single-width glyphs only: a double-width emoji shifts the content
# column by one cell in some terminals and by two in others, which the recording would show.
STAGES: dict[str, tuple[str, str]] = {
    "coder-agent": ("\u25c6", "cyan"),
    "Context": ("\u25c7", "blue"),
    "Plan": ("\u2261", "magenta"),
    "Executing": ("\u25b8", "yellow"),
    "Tests": ("\u2713", "green"),
}

# Verb and the argument that names what is being acted on, for the one-line form of a tool call.
_ACTIONS: dict[str, tuple[str, str]] = {
    "read_file": ("Reading", "path"),
    "edit_file": ("Applying changes to", "path"),
    "write_file": ("Writing", "path"),
    "list_dir": ("Listing", "path"),
    "search_code": ("Searching for", "pattern"),
    "run_command": ("Running", "command"),
}

# `1 failed, 9 passed in 0.31s`, with or without the `=` padding pytest wraps it in.
_COUNTS = re.compile(r"^(?:no tests ran|\d+ [a-z]+(?:, \d+ [a-z]+)*) in \d[\d.]*s")
_FAILURE = re.compile(r"^(?:FAILED|ERROR)\s+\S")
_EXIT_CODE = re.compile(r"^exit_code=(\d+)")
_PYTEST_HINTS = ("test session starts", "short test summary info", "FAILURES")


def _short(value: Any, limit: int = ARG_PREVIEW) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = s.replace("\n", "\\n")
    return s if len(s) <= limit else s[: limit - 1] + "\u2026"


def _one_line(text: str, limit: int = 160) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "\u2026"


def format_tool_call(call: dict[str, Any]) -> str:
    """`edit_file(path='a.py', old_string='...', new_string='...')`, one line, arguments trimmed."""
    args = ", ".join(f"{k}={_short(v)!r}" if isinstance(v, str) else f"{k}={_short(v)}"
                     for k, v in call["args"].items())
    return f"{call['name']}({args})"


def describe_call(call: dict[str, Any]) -> str:
    """A verb and its subject: `Reading durations.py`.

    What a reader wants from a running agent is which file is being touched, not the signature of
    the call that touches it; an `edit_file` whose two string arguments are trimmed to eighty
    characters each is three lines of noise for one fact. The exact call is a `--verbose` thing,
    and the diff that follows an edit already says what changed.
    """
    action = _ACTIONS.get(call["name"])
    if action is None:
        return format_tool_call(call)
    verb, key = action
    subject = call["args"].get(key)
    if subject is None:
        return format_tool_call(call)
    return f"{verb} {_short(subject, 60)}"


def format_tool_result(msg: ToolMessage, max_lines: int = RESULT_PREVIEW_LINES) -> str:
    """First lines of a tool result, with a marker when the rest is hidden from the terminal.

    The model always sees the full result; this is only what the human sees.
    """
    lines = msg.text.splitlines() or [""]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines]) + f"\n\u2026 ({len(lines) - max_lines} more lines)"


def tail(text: str, lines: int = TAIL_LINES) -> str:
    """The last few lines, which is where a command that is not pytest puts its verdict."""
    return "\n".join(text.splitlines()[-lines:])


def is_diff(text: str) -> bool:
    return text.startswith("OK: edited") and "\n@@" in text


def failed_command(text: str) -> bool:
    """True for a tool result that reports a failure: a sandbox refusal or a non-zero exit."""
    if text.startswith("ERROR"):
        return True
    match = _EXIT_CODE.match(text)
    return bool(match) and match.group(1) != "0"


def looks_like_pytest(text: str) -> bool:
    if any(hint in text for hint in _PYTEST_HINTS):
        return True
    return any(_COUNTS.match(line.strip("= ").strip()) for line in text.splitlines())


def summarise_pytest(text: str, max_failures: int = 6) -> str | None:
    """Counts and failed test ids, without the session banner, the plugin list or the rootdir.

    Those lines say where pytest was installed, not what it found, and in a recording they are
    the widest thing on screen: an absolute rootdir and a plugin list pin the demo to one laptop.
    Returns None when the text has no recognisable verdict - a pytest that died before collecting
    has no counts line and every word of it matters, so the caller falls back to the raw tail
    rather than showing nothing.
    """
    counts: str | None = None
    failures: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        bare = line.strip("=").strip()
        if _COUNTS.match(bare):
            counts = bare
        elif _FAILURE.match(line):
            failures.append(line)
    if counts is None and not failures:
        return None
    shown = failures[:max_failures]
    if len(failures) > max_failures:
        shown.append(f"\u2026 and {len(failures) - max_failures} more")
    return "\n".join([counts or "tests failed", *shown])


def relative_paths(text: str, repo: Path | None) -> str:
    """Strip the repository prefix from any absolute path in `text`.

    A run rooted at `C:\\Users\\me\\projects\\demo` puts that prefix on every traceback frame and
    on every path the model spelled out in full. Inside a run there is only one repository, so
    the prefix carries no information and costs half the width of the terminal.
    """
    if repo is None:
        return text
    root = str(repo)
    variants = {root, root.replace("\\", "/"), root.replace("/", "\\")}
    pattern = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
    return re.sub(rf"(?:{pattern})[\\/]?", "", text, flags=re.IGNORECASE)


def context_files(locations: list[str], limit: int = 6) -> str:
    """The distinct files behind a list of `path:lines (symbol)` chunk locations."""
    files: list[str] = []
    for location in locations:
        name = location.split(":", 1)[0]
        if name not in files:
            files.append(name)
    listed = ", ".join(files[:limit])
    return f"{listed}, \u2026" if len(files) > limit else listed


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


def home_relative(path: str) -> str:
    """`~/projects/demo` rather than the full path, when it is under the home directory."""
    try:
        return "~/" + Path(path).relative_to(Path.home()).as_posix()
    except (ValueError, OSError, RuntimeError):
        return path


class Renderer:
    """Stateful printer for a run: knows the console, the repository, and which stage is open."""

    def __init__(
        self,
        console: Console | None = None,
        verbose: bool = False,
        repo: Path | str | None = None,
    ) -> None:
        self.console = console or Console()
        self.verbose = verbose
        self.repo = Path(repo) if repo else None
        # The stage whose icon and label are already on screen: a second `Executing` row in the
        # same act phase continues under the first instead of repeating itself.
        self._open: str | None = None
        self._iteration = 0

    # ------------------------------------------------------------------- the grid

    def _stage(
        self,
        label: str,
        lines: list[RenderableType],
        icon: str | None = None,
        style: str | None = None,
    ) -> None:
        """Print `lines` in the content column, under `label` if it is not already open."""
        if not lines:
            return
        default = STAGES.get(label, ("\u00b7", "white"))
        icon = icon or default[0]
        style = style or default[1]
        head = self._open != label
        if head:
            self.console.print()
            self._open = label
        # `ratio=1` on the content column is what keeps the first three fixed: an expanding
        # Rich grid with no flexible column spreads the spare width over every column instead,
        # and the labels drift a little further right on every line.
        grid = Table.grid(expand=True)
        grid.add_column(width=GUTTER)
        grid.add_column(width=ICON_WIDTH)
        grid.add_column(width=LABEL_WIDTH)
        grid.add_column(ratio=1, overflow="fold")
        for line in lines:
            cells: tuple[RenderableType, RenderableType] = (
                (Text(icon, style=style), Text(label, style=f"bold {style}")) if head
                else (Text(""), Text(""))
            )
            grid.add_row("", *cells, Text(line) if isinstance(line, str) else line)
            head = False
        self.console.print(grid)

    def _dim(self, text: str) -> list[RenderableType]:
        return [Text(line, style="dim") for line in (text.splitlines() or [""])]

    # ------------------------------------------------------------------- entry points

    def header(self, repo: str, task: str | None, model: str, thread: str | None = None) -> None:
        meta = model + (f" \u00b7 thread {thread}" if thread else "")
        lines: list[RenderableType] = [
            Text(meta, style="dim"), Text(home_relative(repo), style="dim")
        ]
        if task:
            lines.append(Text(_one_line(task), style="dim"))
        self._stage("coder-agent", lines)

    def note(self, label: str, detail: str) -> None:
        """A stage from outside the graph, for pipelines that wrap a run (`coder fix-issue`)."""
        self._stage(label, self._dim(detail), icon="\u00b7", style="cyan")

    def footer(self, text: str) -> None:
        """The token ledger under the last stage, in the content column, behind `--tokens`."""
        self._stage("", [Text(text, style="dim")], icon=" ", style="dim")

    def update(self, node: str, patch: dict[str, Any]) -> None:
        handler = getattr(self, f"_on_{node}", None)
        if handler:
            handler(patch)

    # ------------------------------------------------------------------- one per node

    def _on_retrieve_context(self, patch: dict[str, Any]) -> None:
        locations = patch.get("retrieved") or []
        if not locations:
            return
        self._stage("Context", [
            Text(f"{len(locations)} relevant chunks found", style="dim"),
            Text(context_files(locations), style="dim"),
        ])

    def _on_plan(self, patch: dict[str, Any]) -> None:
        self._iteration = patch.get("iteration", self._iteration + 1)
        plan = relative_paths(patch.get("plan", "").strip(), self.repo)
        self._stage("Plan", [Text(line) for line in plan.splitlines() if line.strip()])

    def _on_act(self, patch: dict[str, Any]) -> None:
        for msg in patch.get("messages", []):
            if not isinstance(msg, AIMessage):
                continue
            for call in msg.tool_calls:
                call_text = format_tool_call(call) if self.verbose else describe_call(call)
                self._stage("Executing", [Text("\u2192 " + relative_paths(call_text, self.repo))])
            # A text-only reply ends the act phase, so `finish` prints it as the run summary.
            # Anything the model says on the way to a failing test run is only shown verbosely.
            if self.verbose and msg.text.strip():
                self._stage("Executing", self._dim(msg.text.strip()))

    def _on_tools(self, patch: dict[str, Any]) -> None:
        for msg in patch.get("messages", []):
            if not isinstance(msg, ToolMessage):
                continue
            body = relative_paths(msg.text, self.repo).rstrip()
            if is_diff(body):
                _, _, diff = body.partition("\n")
                self._stage("Executing", [Syntax(diff, "diff", theme="ansi_dark", word_wrap=True)])
                continue
            failed = failed_command(body)
            if self.verbose:
                shown = body
            elif not failed:
                # A successful read or search adds nothing to the call that asked for it, and a
                # green pytest the model ran itself is about to be confirmed by the Tests stage.
                continue
            elif looks_like_pytest(body):
                # The agent running the tests itself: its headline only. The `Tests` stage a few
                # lines below is the graph's own verdict and carries the failing test ids, and
                # printing them twice in a row is how the old output read.
                summary = summarise_pytest(body)
                shown = summary.splitlines()[0] if summary else tail(body)
            else:
                shown = tail(body, 6)
            style = "red" if failed else "dim"
            self._stage("Executing", [Text(line, style=style) for line in shown.splitlines()])

    def _on_run_tests(self, patch: dict[str, Any]) -> None:
        passed = patch.get("tests_passed")
        if passed is None:
            self._stage("Tests", [Text("no test command detected, skipping", style="yellow")],
                        icon="\u00b7", style="yellow")
            return
        output = relative_paths(patch.get("test_output", ""), self.repo)
        shown = output if self.verbose else (summarise_pytest(output) or tail(output))
        lines = shown.splitlines() or [""]
        icon, style = ("\u2713", "green") if passed else ("\u2717", "red")
        self._stage(
            "Tests",
            [Text(lines[0], style=f"bold {style}"), *(Text(x, style="dim") for x in lines[1:])],
            icon=icon, style=style,
        )

    def ask_approval(self, payload: dict[str, Any]) -> bool | str:
        """Show each pending risky call with its diff / content / command and ask once for all.

        Passed to `run_agent(approve=...)`; the graph is paused (and checkpointed) while this
        blocks, so Ctrl+C here and `--resume` later asks the same question again.
        """
        for call in payload.get("tool_calls", []):
            headline, body = preview_call(call)
            lines: list[RenderableType] = [Text(relative_paths(headline, self.repo), style="bold")]
            if body and call["name"] == "edit_file":
                lines.append(Syntax(relative_paths(body, self.repo), "diff", theme="ansi_dark",
                                    word_wrap=True))
            elif body:
                lines.extend(self._dim(relative_paths(body, self.repo)))
            self._stage("Approve", lines, icon="?", style="yellow")
        answer = Prompt.ask(
            " " * GUTTER + "[bold yellow]Approve?[/bold yellow] [dim]y / n / a reason[/dim]",
            console=self.console, default="y", show_default=False,
        )
        # The answer was typed on a line of its own, so the next stage starts its own block.
        self._open = None
        return parse_decision(answer)

    def _on_approve(self, patch: dict[str, Any]) -> None:
        # An approval returns an empty patch; only a rejection has something to show.
        for msg in patch.get("messages", []):
            if isinstance(msg, ToolMessage) and rejected(msg):
                self._stage("Rejected", [Text("asking the agent to reconsider", style="dim")],
                            icon="\u2717", style="yellow")
                break

    def _on_reflect(self, patch: dict[str, Any]) -> None:
        self._stage(
            "Re-planning",
            [Text(f"tests failed \u00b7 attempt {self._iteration + 1}", style="dim")],
            icon="\u21bb", style="magenta",
        )

    def _on_finish(self, patch: dict[str, Any]) -> None:
        status = patch.get("status", "?")
        icon, style = {
            "passed": ("\u2713", "green"), "failed": ("\u2717", "red"), "gave_up": ("!", "yellow"),
        }.get(status, ("\u00b7", "white"))
        summary = relative_paths(patch.get("summary", "").strip(), self.repo)
        self._stage(status, [Text(_one_line(summary, SUMMARY_PREVIEW))], icon=icon, style=style)
