"""Renderer and CLI tests. The renderer is exercised with a scripted graph run captured by Rich."""

from __future__ import annotations

from langchain_core.messages import ToolMessage
from rich.console import Console
from typer.testing import CliRunner

from coder_agent import __version__
from coder_agent.cli import app
from coder_agent.graph import build_graph
from coder_agent.ui.render import Renderer, format_tool_call, format_tool_result, is_diff
from tests.fakes import echo, scripted, tool_call

runner = CliRunner()


def test_format_tool_call_trims_long_arguments():
    call = {"name": "edit_file", "args": {"path": "a.py", "old_string": "x" * 200, "new_string": "y"}}
    out = format_tool_call(call)
    assert out.startswith("edit_file(path='a.py', old_string='xxx")
    assert "…" in out and len(out) < 200


def test_format_tool_result_previews_first_lines():
    msg = ToolMessage(content="\n".join(str(i) for i in range(30)), tool_call_id="c")
    out = format_tool_result(msg, max_lines=5)
    assert out.splitlines()[:5] == ["0", "1", "2", "3", "4"]
    assert out.endswith("(25 more lines)")


def test_format_tool_result_handles_content_blocks():
    msg = ToolMessage(content=[{"type": "text", "text": "hello"}], tool_call_id="c")
    assert format_tool_result(msg) == "hello"


def test_is_diff_only_for_edit_results():
    assert is_diff("OK: edited a.py.\n--- a.py\n+++ a.py\n@@ -1 +1 @@\n-a\n+b")
    assert not is_diff("OK: created a.py (3 chars).")
    assert not is_diff("ERROR: old_string not found.")


def test_renderer_shows_plan_calls_results_and_status():
    console = Console(record=True, width=100, force_terminal=False, color_system=None)
    renderer = Renderer(console)
    llm = scripted("1. call echo\n2. finish", tool_call("echo", text="ping"), "Finished the task.")
    graph = build_graph(llm, [echo])

    for update in graph.stream({"task": "do it", "repo": "/r"}, stream_mode="updates"):
        for node, patch in update.items():
            renderer.update(node, patch)

    text = console.export_text()
    assert "Plan · iteration 1" in text and "1. call echo" in text
    assert "echo(text='ping')" in text
    assert "echo:ping" in text
    assert "passed" in text and "Finished the task." in text


def test_renderer_prints_diff_for_edits():
    console = Console(record=True, width=100, force_terminal=False, color_system=None)
    diff = "OK: edited a.py.\n--- a.py\n+++ a.py\n@@ -1 +1 @@\n-old\n+new"
    Renderer(console).update("tools", {"messages": [ToolMessage(content=diff, tool_call_id="c")]})
    text = console.export_text()
    assert "-old" in text and "+new" in text


def test_cli_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_cli_rejects_missing_repo(tmp_path):
    result = runner.invoke(app, ["run", str(tmp_path / "nope"), "task"])
    assert result.exit_code == 2
    assert "Not a directory" in result.output
