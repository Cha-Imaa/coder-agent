"""Hidden grader for an add-test task.

The agent was asked to write tests for mathx.py. A test file is only worth something if it
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
MODULE = "mathx.py"
MUTANTS = {
'is_prime_accepts_one': ('if n < 2:\n        return False', 'if n < 1:\n        return False'),
    'gcd_loses_sign': ('a, b = abs(a), abs(b)', 'a, b = a, b'),
    'fibonacci_off_by_one': ('a, b = 0, 1\n    for _ in range(n):', 'a, b = 1, 1\n    for _ in range(n):'),
}


def _agent_test_files() -> list[Path]:
    return [p for p in (ROOT / "tests").glob("test_mathx*.py") if p.name != Path(__file__).name]


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
    assert _agent_test_files(), "no test file matching test_mathx*.py was written"
    original = (ROOT / MODULE).read_text(encoding="utf-8")
    assert _run_agent_tests(original) == 0


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_agent_tests_catch_mutant(name):
    before, after = MUTANTS[name]
    original = (ROOT / MODULE).read_text(encoding="utf-8")
    assert original.count(before) == 1, "grader out of sync with the module"
    assert _run_agent_tests(original.replace(before, after)) != 0, f"mutant survived: {name}"
