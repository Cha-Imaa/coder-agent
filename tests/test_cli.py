"""Renderer and CLI tests. The renderer is exercised with a scripted graph run captured by Rich.

The stage grid is pinned by column, not by substring: the whole point of it is that `Plan`,
`Executing` and `Tests` start at the same cell on every line, and a regression there is invisible
to an `in text` assertion.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage
from rich.console import Console
from typer.testing import CliRunner

from coder_agent import __version__
from coder_agent.cli import app
from coder_agent.graph import build_graph
from coder_agent.ui.render import (
    GUTTER,
    ICON_WIDTH,
    LABEL_WIDTH,
    Renderer,
    _one_line,
    context_files,
    describe_call,
    format_tool_call,
    format_tool_result,
    is_diff,
    relative_paths,
    summarise_pytest,
)
from tests.fakes import echo, scripted, tool_call

runner = CliRunner()

LABEL_COLUMN = GUTTER + ICON_WIDTH
CONTENT_COLUMN = LABEL_COLUMN + LABEL_WIDTH

# What `python -m pytest` prints when a repository's suite fails: three lines about the machine
# it ran on for every line about the code.
NOISY_PYTEST = """\
exit_code=1
$ python -m pytest -q -x -p no:cacheprovider
============================= test session starts ==============================
platform win32 -- Python 3.11.9, pytest-8.3.3, pluggy-1.5.0
rootdir: C:\\Users\\chaim\\demo\\fix-bug-duration-units
plugins: anyio-4.4.0, cov-5.0.0
collected 18 items

tests/test_durations.py .........F                                       [ 55%]

=================================== FAILURES ===================================
_________________________________ test_minutes _________________________________
E       assert 1 == 60
tests/test_durations.py:12: AssertionError
=========================== short test summary info ============================
FAILED tests/test_durations.py::test_minutes - assert 1 == 60
1 failed, 9 passed in 0.31s
"""


def capture() -> tuple[Console, Renderer]:
    console = Console(record=True, width=100, force_terminal=False, color_system=None)
    return console, Renderer(console, repo=Path("/r"))


def rows_at(text: str, column: int, word: str) -> list[str]:
    """Lines whose `column`-th cell begins with `word`."""
    return [line for line in text.splitlines() if line[column:].startswith(word)]


# --------------------------------------------------------------------------- pure helpers


def test_format_tool_call_trims_long_arguments():
    call = {"name": "edit_file", "args": {"path": "a.py", "old_string": "x" * 200, "new_string": "y"}}
    out = format_tool_call(call)
    assert out.startswith("edit_file(path='a.py', old_string='xxx")
    assert "…" in out and len(out) < 200


def test_describe_call_names_the_file_not_the_signature():
    call = {"name": "edit_file", "args": {"path": "a.py", "old_string": "x" * 200, "new_string": "y"}}
    assert describe_call(call) == "Applying changes to a.py"
    assert describe_call({"name": "run_command", "args": {"command": "pytest -q"}}) == (
        "Running pytest -q"
    )


def test_describe_call_falls_back_to_the_signature_for_unknown_tools():
    assert describe_call({"name": "echo", "args": {"text": "ping"}}) == "echo(text='ping')"


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


def test_summarise_pytest_keeps_the_counts_and_the_failures():
    summary = summarise_pytest(NOISY_PYTEST)
    assert summary == (
        "1 failed, 9 passed in 0.31s\n"
        "FAILED tests/test_durations.py::test_minutes - assert 1 == 60"
    )


def test_summarise_pytest_reads_the_padded_counts_line():
    assert summarise_pytest("==== 18 passed in 0.42s ====") == "18 passed in 0.42s"


def test_summarise_pytest_caps_the_failure_list():
    failures = "\n".join(f"FAILED tests/test_x.py::test_{i}" for i in range(10))
    summary = summarise_pytest(f"{failures}\n3 failed in 1.0s", max_failures=2)
    assert summary.splitlines()[-1] == "… and 8 more"


def test_summarise_pytest_gives_up_without_a_verdict():
    # A pytest that died before collecting has no counts line, and every word of it matters.
    assert summarise_pytest("ImportError while loading conftest") is None


def test_one_line_cuts_at_a_sentence_when_one_ends_late_enough():
    text = "I corrected the unit mapping there. Then I ran the whole suite twice."
    assert _one_line(text, 40) == "I corrected the unit mapping there. …"


def test_one_line_prefers_a_word_to_an_early_sentence():
    # The full stop here sits at a quarter of the budget; cutting there would throw away the part
    # that says what happened, which in a recording is the frame the GIF rests on.
    text = "It failed. I corrected the mapping and then ran the suite again to confirm the fix."
    assert _one_line(text, 40) == "It failed. I corrected the mapping and…"


def test_relative_paths_strips_the_repo_prefix_in_either_slash():
    repo = Path("C:/Users/chaim/demo")
    assert relative_paths("rootdir: C:/Users/chaim/demo", repo) == "rootdir: "
    assert relative_paths(r"at C:\Users\chaim\demo\durations.py:4", repo) == "at durations.py:4"


def test_relative_paths_is_a_no_op_without_a_repo():
    assert relative_paths("C:/elsewhere/a.py", None) == "C:/elsewhere/a.py"


def test_context_files_dedupes_and_caps():
    locations = [f"f{i}.py:1-9" for i in range(8)] + ["f0.py:20-30"]
    assert context_files(locations, limit=3) == "f0.py, f1.py, f2.py, …"


# --------------------------------------------------------------------------- the stage grid


def test_stage_labels_and_their_content_share_one_column():
    console, renderer = capture()
    llm = scripted("1. call echo\n2. finish", tool_call("echo", text="ping"), "Finished the task.")
    graph = build_graph(llm, [echo])

    for update in graph.stream({"task": "do it", "repo": "/r"}, stream_mode="updates"):
        for node, patch in update.items():
            renderer.update(node, patch)

    text = console.export_text()
    for label in ("Plan", "Executing", "Tests", "passed"):
        assert rows_at(text, LABEL_COLUMN, label), f"{label} is not in the label column"
    assert rows_at(text, CONTENT_COLUMN, "1. call echo")
    assert rows_at(text, CONTENT_COLUMN, "→ echo(text='ping')")
    assert rows_at(text, CONTENT_COLUMN, "Finished the task.")


def test_header_states_the_model_the_repo_and_the_thread():
    console, renderer = capture()
    renderer.header("/r/demo", "make the tests pass", "groq:llama-3.3-70b", thread="7f2a")
    text = console.export_text()
    assert rows_at(text, LABEL_COLUMN, "coder-agent")
    assert rows_at(text, CONTENT_COLUMN, "groq:llama-3.3-70b · thread 7f2a")
    assert rows_at(text, CONTENT_COLUMN, "make the tests pass")


def test_successful_tool_output_is_hidden_but_the_call_is_not():
    console, renderer = capture()
    renderer.update("act", {"messages": [tool_call("read_file", path="durations.py")]})
    renderer.update("tools", {"messages": [
        ToolMessage(content="1|line one\n2|line two", tool_call_id="call_1")
    ]})
    text = console.export_text()
    assert "Reading durations.py" in text
    assert "line one" not in text


def test_verbose_restores_the_full_tool_output():
    console, _ = capture()
    renderer = Renderer(console, verbose=True, repo=Path("/r"))
    renderer.update("act", {"messages": [tool_call("read_file", path="durations.py")]})
    renderer.update("tools", {"messages": [
        ToolMessage(content="1|line one\n2|line two", tool_call_id="call_1")
    ]})
    text = console.export_text()
    assert "read_file(path='durations.py')" in text and "line two" in text


def test_a_failing_command_is_still_shown():
    console, renderer = capture()
    renderer.update("tools", {"messages": [
        ToolMessage(content="ERROR: Command blocked by sandbox policy", tool_call_id="c")
    ]})
    assert "blocked by sandbox policy" in console.export_text()


def test_renderer_prints_diff_for_edits():
    console, renderer = capture()
    diff = "OK: edited a.py.\n--- a.py\n+++ a.py\n@@ -1 +1 @@\n-old\n+new"
    renderer.update("tools", {"messages": [ToolMessage(content=diff, tool_call_id="c")]})
    text = console.export_text()
    assert "-old" in text and "+new" in text


def test_tests_stage_summarises_pytest_and_drops_the_machine():
    console, renderer = capture()
    renderer.update("run_tests", {"tests_passed": False, "test_output": NOISY_PYTEST})
    text = console.export_text()
    assert rows_at(text, LABEL_COLUMN, "Tests")
    assert rows_at(text, CONTENT_COLUMN, "1 failed, 9 passed in 0.31s")
    assert "FAILED tests/test_durations.py::test_minutes" in text
    for noise in ("platform win32", "plugins:", "rootdir", "test session starts"):
        assert noise not in text, f"{noise} survived into the terminal"


def test_tests_stage_falls_back_to_the_tail_for_other_runners():
    console, renderer = capture()
    renderer.update("run_tests", {"tests_passed": False, "test_output": "exit_code=1\nok 1\nnot ok 2"})
    assert "not ok 2" in console.export_text()


def test_paths_under_the_repo_are_shown_relative_to_it():
    console, renderer = capture()
    renderer.repo = Path("C:/Users/chaim/demo")
    renderer.update("run_tests", {"tests_passed": False, "test_output": NOISY_PYTEST})
    text = console.export_text()
    assert "C:\\Users\\chaim\\demo" not in text


def test_replanning_counts_the_next_attempt():
    console, renderer = capture()
    renderer.update("plan", {"plan": "1. try", "iteration": 1})
    renderer.update("reflect", {})
    text = console.export_text()
    assert rows_at(text, LABEL_COLUMN, "Re-planning")
    assert "attempt 2" in text


def test_a_rejected_call_says_so():
    console, renderer = capture()
    renderer.update("approve", {"messages": [
        ToolMessage(content="Rejected by the user (use pathlib).", tool_call_id="c")
    ]})
    assert "reconsider" in console.export_text()


def test_finish_ends_on_the_status_and_the_summary():
    console, renderer = capture()
    renderer.update("finish", {"status": "gave_up", "summary": "I could not find the parser."})
    text = console.export_text()
    assert rows_at(text, LABEL_COLUMN, "gave_up")
    assert rows_at(text, CONTENT_COLUMN, "I could not find the parser.")


def test_act_text_between_iterations_is_verbose_only():
    console, renderer = capture()
    renderer.update("act", {"messages": [AIMessage("Let me try the other file.")]})
    assert "other file" not in console.export_text()


# --------------------------------------------------------------------------- the CLI


def test_cli_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_cli_rejects_missing_repo(tmp_path):
    result = runner.invoke(app, ["run", str(tmp_path / "nope"), "task"])
    assert result.exit_code == 2
    assert "Not a directory" in result.output


def test_the_token_ledger_is_behind_a_flag():
    for command in ("run", "chat", "fix-issue"):
        result = runner.invoke(app, [command, "--help"])
        assert "--tokens" in result.output, command
