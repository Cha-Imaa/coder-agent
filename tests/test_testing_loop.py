"""Test-command detection, the run_tests node and the reflect loop."""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool

from coder_agent.config import settings
from coder_agent.graph import build_graph
from coder_agent.graph.nodes import route_after_tests, run_tests
from coder_agent.graph.testing import detect_test_command
from tests.fakes import scripted, tool_call

# --- detection ------------------------------------------------------------------------------


def test_detects_pytest_from_ini(tmp_path: Path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    assert "pytest" in detect_test_command(tmp_path)


def test_detects_pytest_from_tests_dir(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    assert "pytest" in detect_test_command(tmp_path)


def test_detects_pytest_from_pyproject(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths=['t']\n")
    assert "pytest" in detect_test_command(tmp_path)


def test_detects_npm_test_only_when_script_is_real(tmp_path: Path):
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"scripts": {"test": "echo 'Error: no test specified' && exit 1"}}))
    assert detect_test_command(tmp_path) is None
    pkg.write_text(json.dumps({"scripts": {"test": "jest"}}))
    assert detect_test_command(tmp_path) == "npm test --silent"


def test_detects_go_and_cargo(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module x\n")
    assert detect_test_command(tmp_path).startswith("go test")
    (tmp_path / "go.mod").unlink()
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert detect_test_command(tmp_path).startswith("cargo test")


def test_unknown_repo_has_no_test_command(tmp_path: Path):
    (tmp_path / "notes.txt").write_text("hi")
    assert detect_test_command(tmp_path) is None


# --- run_tests node -------------------------------------------------------------------------


def _pytest_repo(root: Path, expected: int) -> Path:
    root.mkdir(exist_ok=True)
    (root / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    (root / "calc.py").write_text("def double(x):\n    return x + x\n")
    (root / "test_calc.py").write_text(f"from calc import double\n\ndef test_double():\n    assert double(2) == {expected}\n")
    return root


def test_run_tests_node_records_failure(tmp_path: Path):
    repo = _pytest_repo(tmp_path / "r", expected=5)
    out = run_tests({"repo": str(repo), "test_command": detect_test_command(repo)})
    assert out["tests_passed"] is False
    assert out["status"] == "failed"
    assert "assert 4 == 5" in out["test_output"]


def test_run_tests_node_records_success(tmp_path: Path):
    repo = _pytest_repo(tmp_path / "r", expected=4)
    out = run_tests({"repo": str(repo), "test_command": detect_test_command(repo)})
    assert out["tests_passed"] is True
    assert out["status"] == "passed"


def test_run_tests_node_without_command_skips():
    out = run_tests({"repo": "/nowhere", "test_command": None})
    assert out["tests_passed"] is None
    assert out["status"] == "passed"


# --- routing --------------------------------------------------------------------------------


def test_route_after_tests():
    assert route_after_tests({"tests_passed": True}) == "finish"
    assert route_after_tests({"tests_passed": None}) == "finish"
    assert route_after_tests({"tests_passed": False, "iteration": 1}) == "reflect"
    assert route_after_tests({"tests_passed": False, "iteration": settings.max_iterations}) == "finish"


# --- whole loop -----------------------------------------------------------------------------


def test_loop_reflects_and_fixes_on_second_iteration(tmp_path: Path):
    repo = _pytest_repo(tmp_path / "r", expected=4)
    (repo / "calc.py").write_text("def double(x):\n    return x * 3\n")  # bug

    @tool
    def fix_calc() -> str:
        """Fix the bug."""
        (repo / "calc.py").write_text("def double(x):\n    return x * 2\n")
        return "fixed"

    llm = scripted(
        "plan 1",
        "I believe it is done.",  # iteration 1: no tool calls -> tests fail
        "plan 2",
        tool_call("fix_calc"),  # iteration 2: fix, then stop
        "Fixed the multiplier.",
    )
    out = build_graph(llm, [fix_calc]).invoke({"task": "fix double()", "repo": str(repo)})

    assert out["iteration"] == 2
    assert out["tests_passed"] is True
    assert out["status"] == "passed"
    assert out["summary"] == "Fixed the multiplier."
    # The reflect turn carried the failing output back to the model.
    reflect_msgs = [m for m in out["messages"] if isinstance(m, HumanMessage) and "Test output" in m.content]
    assert len(reflect_msgs) == 1 and "assert 6 == 4" in reflect_msgs[0].content
    # Plan 2 saw the failure too.
    assert "assert 6 == 4" in llm.calls[2][1].content
    assert any(isinstance(m, ToolMessage) and m.content == "fixed" for m in out["messages"])


def test_loop_gives_up_after_max_iterations(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "max_iterations", 2)
    repo = _pytest_repo(tmp_path / "r", expected=5)  # can never pass
    llm = scripted("plan 1", "done?", "plan 2", "done??")
    out = build_graph(llm, []).invoke({"task": "t", "repo": str(repo)})
    assert out["iteration"] == 2
    assert out["status"] == "failed"
    assert out["tests_passed"] is False


def test_caller_supplied_test_command_wins(tmp_path: Path):
    llm = scripted("plan", "done")
    out = build_graph(llm, []).invoke(
        {"task": "t", "repo": str(tmp_path), "test_command": "python -c \"import sys; sys.exit(0)\""}
    )
    assert out["test_command"].startswith("python -c")
    assert out["tests_passed"] is True
