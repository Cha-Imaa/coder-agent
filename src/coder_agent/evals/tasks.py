"""Load and grade the in-house evaluation suite under `evals/suite/`.

A benchmark task is a directory:

    <task-id>/
        task.toml        id, category, prompt (what the agent is told), notes (why it is here)
        repo/            the repository the agent works in; copied fresh for every run
        hidden_tests/    copied into repo/tests *after* the run; their verdict is the grade
        solution/        a reference fix, overlaid on repo/ only to validate the suite itself

The agent never sees `hidden_tests/`, so it cannot pass by editing the tests or by satisfying
only the visible assertion. `solution/` exists so a test can prove every task is well-posed:
hidden tests must fail on the untouched repo and pass once the reference fix is applied.
"""

from __future__ import annotations

import shutil
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from coder_agent.sandbox.local import CommandResult, run_command

# The in-house categories, plus the public HumanEval slice which is its own category so the
# pass-rate table keeps the two kinds of number apart.
CATEGORIES: tuple[str, ...] = (
    "fix-bug", "add-feature", "refactor", "add-test", "multi-file", "humaneval",
)

# src/coder_agent/evals/tasks.py -> repo root is three parents up from the package directory.
_EVALS_DIR = Path(__file__).resolve().parents[3] / "evals"
SUITE_DIR = _EVALS_DIR / "suite"
SUITES: dict[str, Path] = {"inhouse": SUITE_DIR, "humaneval": _EVALS_DIR / "humaneval"}

# Same flags the agent's own `run_tests` node uses, so grading and self-verification agree.
GRADE_COMMAND = "python -m pytest -q --no-header -p no:cacheprovider tests"

_IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")


@dataclass(frozen=True)
class EvalTask:
    id: str
    category: str
    prompt: str
    notes: str
    path: Path

    @property
    def repo_dir(self) -> Path:
        return self.path / "repo"

    @property
    def hidden_tests_dir(self) -> Path:
        return self.path / "hidden_tests"

    @property
    def solution_dir(self) -> Path:
        return self.path / "solution"

    def materialise(self, dest: Path) -> Path:
        """Copy the starting repository to `dest`; every run gets its own untouched copy."""
        shutil.copytree(self.repo_dir, dest, ignore=_IGNORE)
        return dest

    def install_hidden_tests(self, dest: Path) -> list[Path]:
        """Drop the hidden tests into `dest/tests`, replacing same-named visible files.

        Replacing matters for tasks where the agent is asked to edit an existing test file: the
        grade must come from our assertions, not from whatever the agent rewrote them to.
        """
        tests = dest / "tests"
        tests.mkdir(exist_ok=True)
        installed = []
        for src in sorted(self.hidden_tests_dir.glob("*.py")):
            target = tests / src.name
            shutil.copy2(src, target)
            installed.append(target)
        return installed

    def apply_solution(self, dest: Path) -> None:
        """Overlay the reference solution on a materialised repo. For suite validation only."""
        shutil.copytree(self.solution_dir, dest, ignore=_IGNORE, dirs_exist_ok=True)


def load_task(path: Path) -> EvalTask:
    """Read one task directory, failing loudly on a malformed one rather than skipping it."""
    spec_path = path / "task.toml"
    with spec_path.open("rb") as fh:
        spec = tomllib.load(fh)

    missing = {"id", "category", "prompt"} - spec.keys()
    if missing:
        raise ValueError(f"{spec_path}: missing keys {sorted(missing)}")
    if spec["id"] != path.name:
        raise ValueError(f"{spec_path}: id {spec['id']!r} does not match directory {path.name!r}")
    if spec["category"] not in CATEGORIES:
        raise ValueError(f"{spec_path}: unknown category {spec['category']!r}")
    for sub in ("repo", "hidden_tests", "solution"):
        if not (path / sub).is_dir():
            raise ValueError(f"{path}: missing {sub}/ directory")
    if not list((path / "hidden_tests").glob("*.py")):
        raise ValueError(f"{path}: hidden_tests/ has no test files")

    return EvalTask(
        id=spec["id"],
        category=spec["category"],
        prompt=spec["prompt"].strip(),
        notes=spec.get("notes", "").strip(),
        path=path,
    )


def load_suite(root: Path = SUITE_DIR, category: str | None = None) -> list[EvalTask]:
    """All tasks under `root`, sorted by id, optionally restricted to one category."""
    if not root.is_dir():
        return []
    tasks = [load_task(p) for p in sorted(root.iterdir()) if (p / "task.toml").is_file()]
    if category is not None:
        tasks = [t for t in tasks if t.category == category]
    return tasks


def grade(repo: Path, timeout: int = 300) -> CommandResult:
    """Run the (hidden) test suite in a materialised repo; exit code 0 is a pass.

    Goes through the sandbox like every other command the project runs, so the same jail,
    timeout and output cap apply to grading as to the agent's own test runs.
    """
    return run_command(repo, GRADE_COMMAND, timeout=timeout)


def load_suites(names: Iterable[str], category: str | None = None) -> list[EvalTask]:
    """Concatenate the named suites (`SUITES` keys, or "all") in the order given."""
    chosen = list(SUITES) if "all" in names else list(names)
    unknown = [n for n in chosen if n not in SUITES]
    if unknown:
        raise ValueError(f"unknown suite(s) {unknown}; choose from {sorted(SUITES)} or 'all'")
    tasks: list[EvalTask] = []
    for name in chosen:
        tasks.extend(load_suite(SUITES[name], category=category))
    return tasks
