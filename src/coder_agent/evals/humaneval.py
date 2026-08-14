"""Package HumanEval problems as repository tasks with the same shape as the in-house suite.

HumanEval (Chen et al., 2021, MIT licence) is 164 Python functions given as a signature plus a
docstring; the model writes the body and a hidden `check(candidate)` decides. Published numbers
are for one-shot completion: prompt in, code out. Our agent does not work that way: it lives in
a repository, runs tests, reads failures and edits again. So every problem becomes a tiny repo:

    <task-id>/
        task.toml                     prompt tells the agent which function and file to fill in
        repo/<entry_point>.py         the HumanEval prompt with `raise NotImplementedError` body
        repo/tests/test_<entry>.py    runs the docstring examples with doctest (the visible test)
        hidden_tests/test_<entry>_hidden.py   HumanEval's own `check(candidate)` as a pytest
        solution/<entry_point>.py     the canonical solution, for validating the task

Two rules shape the slice. Only problems whose docstring carries `>>>` examples are eligible,
so the agent has a real failing test to run, exactly as in the in-house tasks. And a problem is
kept only when the untouched stub fails both visible and hidden tests and the canonical solution
passes both; the generator checks this itself, so a docstring whose examples do not doctest
cleanly (float formatting, set ordering) is skipped rather than shipped as an unsolvable task.
Selection is deterministic: dataset order, first `n` eligible problems.
"""

from __future__ import annotations

import gzip
import json
import shutil
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from coder_agent.evals.tasks import grade, load_task

DATA_URL = "https://raw.githubusercontent.com/openai/human-eval/master/data/HumanEval.jsonl.gz"
HUMANEVAL_DIR = Path(__file__).resolve().parents[3] / "evals" / "humaneval"
CATEGORY = "humaneval"
DEFAULT_COUNT = 30

_STUB_BODY = "    raise NotImplementedError\n"


@dataclass(frozen=True)
class Problem:
    """One HumanEval row. `number` is the integer after `HumanEval/` in the dataset id."""

    number: int
    entry_point: str
    prompt: str
    canonical_solution: str
    test: str

    @property
    def task_id(self) -> str:
        return f"{CATEGORY}-{self.number:03d}-{self.entry_point.replace('_', '-')}"

    @property
    def module(self) -> str:
        return self.entry_point

    @property
    def has_doctest(self) -> bool:
        return ">>>" in self.prompt

    @property
    def stub_is_well_formed(self) -> bool:
        """We can only append a body when the prompt ends inside a function, at its docstring."""
        return self.prompt.rstrip().endswith('"""')


def load_problems(path: Path) -> list[Problem]:
    """Read `HumanEval.jsonl.gz` (or an uncompressed `.jsonl`) in dataset order."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
        rows = [json.loads(line) for line in fh if line.strip()]
    return [
        Problem(
            number=int(row["task_id"].split("/")[1]),
            entry_point=row["entry_point"],
            prompt=row["prompt"],
            canonical_solution=row["canonical_solution"],
            test=row["test"],
        )
        for row in rows
    ]


def download(dest: Path, url: str = DATA_URL) -> Path:
    """Fetch the dataset with urllib; kept separate so tests never touch the network."""
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as resp:
        dest.write_bytes(resp.read())
    return dest


# --------------------------------------------------------------------------------------------
# Rendering one problem as a task directory
# --------------------------------------------------------------------------------------------


def render_task(problem: Problem, root: Path) -> Path:
    """Write the task directory for `problem` under `root` and return it.

    The visible test is doctest over the module: it fails on the stub (NotImplementedError) and
    passes once the docstring examples hold, so the agent's own `run_tests` node gets a real
    signal without us hand-writing assertions. The hidden test is HumanEval's `check`, verbatim.
    """
    task_dir = root / problem.task_id
    if task_dir.exists():
        shutil.rmtree(task_dir)
    repo = task_dir / "repo"
    (repo / "tests").mkdir(parents=True)
    (task_dir / "hidden_tests").mkdir()
    (task_dir / "solution").mkdir()

    stub = problem.prompt.rstrip("\n") + "\n" + _STUB_BODY
    solution = problem.prompt.rstrip("\n") + "\n" + problem.canonical_solution.rstrip("\n") + "\n"
    (repo / f"{problem.module}.py").write_text(stub, encoding="utf-8")
    (task_dir / "solution" / f"{problem.module}.py").write_text(solution, encoding="utf-8")
    (repo / "conftest.py").write_text(
        '"""Puts the repository root on sys.path so tests can import the module under test."""\n',
        encoding="utf-8",
    )
    (repo / "tests" / f"test_{problem.module}.py").write_text(
        _visible_test(problem), encoding="utf-8"
    )
    (task_dir / "hidden_tests" / f"test_{problem.module}_hidden.py").write_text(
        _hidden_test(problem), encoding="utf-8"
    )
    (task_dir / "task.toml").write_text(_task_toml(problem), encoding="utf-8")
    return task_dir


def _visible_test(problem: Problem) -> str:
    return (
        "import doctest\n\n"
        f"import {problem.module}\n\n\n"
        "def test_docstring_examples():\n"
        f"    result = doctest.testmod({problem.module}, verbose=False)\n"
        "    assert result.attempted > 0\n"
        "    assert result.failed == 0\n"
    )


def _hidden_test(problem: Problem) -> str:
    return (
        f"from {problem.module} import {problem.entry_point}\n\n"
        f"{problem.test.strip()}\n\n\n"
        f"def test_humaneval_{problem.number}():\n"
        f"    check({problem.entry_point})\n"
    )


def _task_toml(problem: Problem) -> str:
    prompt = (
        f"Implement the function `{problem.entry_point}` in `{problem.module}.py`. "
        "Its docstring describes the behaviour and gives examples; the examples are run as "
        f"doctests by tests/test_{problem.module}.py, which currently fails. "
        "Do not change the tests."
    )
    notes = (
        f"HumanEval/{problem.number} packaged as a repository task. Visible test is the docstring "
        "examples via doctest; hidden test is the original check(candidate)."
    )
    return (
        f'id = "{problem.task_id}"\n'
        f'category = "{CATEGORY}"\n'
        f'source = "HumanEval/{problem.number}"\n'
        f'prompt = """\n{prompt}\n"""\n'
        f'notes = """\n{notes}\n"""\n'
    )


# --------------------------------------------------------------------------------------------
# Selecting and validating the slice
# --------------------------------------------------------------------------------------------


def is_well_posed(task_dir: Path) -> bool:
    """Stub fails, canonical passes, on both the visible doctest and the hidden check."""
    task = load_task(task_dir)
    with tempfile.TemporaryDirectory(prefix="humaneval-check-") as tmp:
        before = task.materialise(Path(tmp) / "before")
        if grade(before).exit_code == 0:  # visible test passes on the stub: no signal
            return False
        task.install_hidden_tests(before)
        if grade(before).exit_code == 0:
            return False
        after = task.materialise(Path(tmp) / "after")
        task.apply_solution(after)
        if grade(after).exit_code != 0:  # docstring examples do not doctest cleanly
            return False
        task.install_hidden_tests(after)
        return grade(after).exit_code == 0


def eligible(problems: Iterable[Problem]) -> Iterator[Problem]:
    for p in problems:
        if p.has_doctest and p.stub_is_well_formed:
            yield p


def build_slice(
    problems: Iterable[Problem],
    root: Path = HUMANEVAL_DIR,
    count: int = DEFAULT_COUNT,
    on_decision: Callable[[Problem, bool], None] | None = None,
) -> list[Path]:
    """Render eligible problems in order until `count` well-posed tasks exist under `root`.

    Rejected problems are removed again, so the directory only ever holds tasks that the suite
    tests will accept. Existing tasks under `root` are cleared first: the slice is a function
    of the dataset and `count`, not of what happened to be on disk.
    """
    if root.exists():
        for old in root.iterdir():
            if old.is_dir() and (old / "task.toml").is_file():
                shutil.rmtree(old)
    root.mkdir(parents=True, exist_ok=True)

    kept: list[Path] = []
    for problem in eligible(problems):
        if len(kept) >= count:
            break
        task_dir = render_task(problem, root)
        ok = is_well_posed(task_dir)
        if on_decision is not None:
            on_decision(problem, ok)
        if ok:
            kept.append(task_dir)
        else:
            shutil.rmtree(task_dir)
    return kept
