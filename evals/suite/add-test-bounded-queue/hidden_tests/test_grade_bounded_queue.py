"""Hidden grader for an add-test task.

The agent was asked to write tests for bounded_queue.py. A test file is only worth something if it
fails when the code is wrong, so this grader runs the agent's tests against the real module
(must pass) and against several mutants with one planted bug each (must fail).
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = "bounded_queue.py"
MUTANTS = {
'is_full_off_by_one': ('return len(self._items) >= self.capacity', 'return len(self._items) > self.capacity'),
    'push_drops_silently_when_full': ('raise OverflowError("queue is full")', 'return None'),
    'pop_is_lifo': ('return self._items.pop(0)', 'return self._items.pop()'),
    'accepts_zero_capacity': ('if capacity <= 0:', 'if capacity < 0:'),
}


def _agent_test_files() -> list[Path]:
    return [p for p in (ROOT / "tests").glob("test_bounded_queue*.py") if p.name != Path(__file__).name]


def _run_agent_tests(module_source: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "repo"
        shutil.copytree(ROOT, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        (dest / MODULE).write_text(module_source, encoding="utf-8")
        (dest / "tests" / Path(__file__).name).unlink()
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
            cwd=dest, capture_output=True, text=True, timeout=120,
        )
        return proc.returncode


def test_agent_wrote_tests_that_pass_on_the_real_module():
    assert _agent_test_files(), "no test file matching test_bounded_queue*.py was written"
    original = (ROOT / MODULE).read_text(encoding="utf-8")
    assert _run_agent_tests(original) == 0


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_agent_tests_catch_mutant(name):
    before, after = MUTANTS[name]
    original = (ROOT / MODULE).read_text(encoding="utf-8")
    assert original.count(before) == 1, "grader out of sync with the module"
    assert _run_agent_tests(original.replace(before, after)) != 0, f"mutant survived: {name}"
