"""End-to-end test of the MCP server through the real stdio transport.

We spawn the server as a subprocess exactly as the agent will, list its tools, and call each one.
This proves the protocol plumbing, the schemas, and the sandbox integration together.
"""

from pathlib import Path

import pytest

from coder_agent.tools.client import load_tools

pytestmark = pytest.mark.asyncio


def text(result) -> str:
    """MCP tools return a list of content blocks; flatten to the text the model would see."""
    if isinstance(result, str):
        return result
    return "\n".join(block.get("text", "") for block in result if isinstance(block, dict))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "math_utils.py").write_text(
        "def add(a, b):\n    return a - b  # bug\n\n\ndef sub(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
async def tools(repo: Path) -> dict:
    loaded = await load_tools(repo)
    return {t.name: t for t in loaded}


async def test_server_advertises_expected_tools(tools: dict) -> None:
    assert set(tools) == {"read_file", "edit_file", "write_file", "list_dir", "search_code", "run_command"}
    # Descriptions are part of the prompt; make sure they made it across the wire.
    assert "before editing" in tools["read_file"].description


async def test_read_file_has_line_numbers(tools: dict) -> None:
    out = text(await tools["read_file"].ainvoke({"path": "pkg/math_utils.py"}))
    assert "1|def add(a, b):" in out
    assert "lines 1-6 of 6" in out


async def test_read_file_range(tools: dict) -> None:
    out = text(await tools["read_file"].ainvoke({"path": "pkg/math_utils.py", "start_line": 5, "end_line": 6}))
    assert "5|def sub(a, b):" in out
    assert "1|def add" not in out


async def test_edit_file_replaces_unique_match(tools: dict, repo: Path) -> None:
    out = text(await tools["edit_file"].ainvoke(
        {"path": "pkg/math_utils.py", "old_string": "return a - b  # bug", "new_string": "return a + b"}
    ))
    assert out.startswith("OK")
    assert "return a + b" in (repo / "pkg" / "math_utils.py").read_text(encoding="utf-8")


async def test_edit_file_rejects_ambiguous_match(tools: dict, repo: Path) -> None:
    before = (repo / "pkg" / "math_utils.py").read_text(encoding="utf-8")
    out = text(await tools["edit_file"].ainvoke(
        {"path": "pkg/math_utils.py", "old_string": "return a - b", "new_string": "return 0"}
    ))
    assert "occurs 2 times" in out
    assert (repo / "pkg" / "math_utils.py").read_text(encoding="utf-8") == before


async def test_edit_file_rejects_missing_match(tools: dict) -> None:
    out = text(await tools["edit_file"].ainvoke(
        {"path": "pkg/math_utils.py", "old_string": "nope", "new_string": "x"}
    ))
    assert "not found" in out


async def test_write_file_creates_with_parents(tools: dict, repo: Path) -> None:
    out = text(await tools["write_file"].ainvoke({"path": "new/dir/file.txt", "content": "hello"}))
    assert out.startswith("OK: created")
    assert (repo / "new" / "dir" / "file.txt").read_text(encoding="utf-8") == "hello"


async def test_list_dir_tree(tools: dict) -> None:
    out = text(await tools["list_dir"].ainvoke({}))
    assert "pkg/" in out
    assert "math_utils.py" in out
    assert "README.md" in out


async def test_search_code_finds_definitions(tools: dict) -> None:
    out = text(await tools["search_code"].ainvoke({"pattern": r"def \w+", "glob": "*.py"}))
    assert "pkg/math_utils.py:1:def add(a, b):" in out
    assert "pkg/math_utils.py:5:def sub(a, b):" in out


async def test_run_command_returns_exit_code(tools: dict) -> None:
    out = text(await tools["run_command"].ainvoke({"command": "python -c \"print(6*7)\""}))
    assert out.startswith("exit_code=0")
    assert "42" in out


async def test_sandbox_errors_are_returned_not_raised(tools: dict) -> None:
    out = text(await tools["read_file"].ainvoke({"path": "../outside.txt"}))
    assert out.startswith("ERROR")
    out = text(await tools["run_command"].ainvoke({"command": "git push"}))
    assert out.startswith("ERROR")


async def test_unknown_argument_names_are_rejected_with_hint(tools: dict) -> None:
    out = text(await tools["read_file"].ainvoke({"path": "pkg/math_utils.py", "line_start": 5}))
    assert out.startswith("ERROR: unknown argument(s) ['line_start']")
    assert "start_line" in out


async def test_edit_file_returns_unified_diff(tools: dict) -> None:
    out = text(await tools["edit_file"].ainvoke(
        {"path": "pkg/math_utils.py", "old_string": "return a - b  # bug", "new_string": "return a + b"}
    ))
    assert "-    return a - b  # bug" in out
    assert "+    return a + b" in out
