"""Detect how to run a repository's tests, and run them.

The agent verifies its own work by running the test suite after every act phase. It does not ask
the model which command to use: detection is deterministic, so the verdict cannot be gamed by a
model that "forgets" to run the failing test, and it costs zero tokens.
"""

from __future__ import annotations

import json
from pathlib import Path

from coder_agent.config import settings
from coder_agent.sandbox import CommandResult, run_command

# Ordered: the first matching rule wins. Python first because it is the primary target.
_RULES: list[tuple[str, str]] = [
    ("pytest.ini", "python -m pytest -q -x --no-header -p no:cacheprovider"),
    ("conftest.py", "python -m pytest -q -x --no-header -p no:cacheprovider"),
    ("go.mod", "go test ./..."),
    ("Cargo.toml", "cargo test --quiet"),
]
_PYTEST = _RULES[0][1]


def detect_test_command(repo: Path) -> str | None:
    """Return the shell command that runs this repository's tests, or None if unknown."""
    for marker, command in _RULES:
        if (repo / marker).exists():
            return command

    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        if "[tool.pytest" in text or "pytest" in text:
            return _PYTEST

    package_json = repo / "package.json"
    if package_json.exists():
        try:
            scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
        except (json.JSONDecodeError, OSError):
            scripts = {}
        if "test" in scripts and "no test specified" not in scripts["test"]:
            return "npm test --silent"

    if (repo / "tests").is_dir() or (repo / "test").is_dir() or list(repo.glob("test_*.py")):
        return _PYTEST
    return None


def run_tests(repo: Path, command: str) -> CommandResult:
    """Run the test command under the sandbox with the configured timeout."""
    return run_command(repo, command, timeout=settings.command_timeout)
