"""The HumanEval packager is tested offline on a hand-written problem: no dataset, no network.

The committed slice under `evals/humaneval/` is validated task by task in `test_eval_suite.py`
(stub fails, canonical passes). Here we check the rendering rules themselves: file layout,
doctest as the visible test, the original `check` as the hidden test, and that the selection
loop drops problems whose docstring examples are wrong instead of shipping them.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from coder_agent.evals import grade, load_task
from coder_agent.evals.humaneval import (
    Problem,
    build_slice,
    eligible,
    is_well_posed,
    load_problems,
    render_task,
)

GOOD = Problem(
    number=7,
    entry_point="double_it",
    prompt=(
        "def double_it(n: int) -> int:\n"
        '    """Return twice n.\n'
        "    >>> double_it(2)\n"
        "    4\n"
        '    """\n'
    ),
    canonical_solution="    return n * 2\n",
    test="def check(candidate):\n    assert candidate(3) == 6\n    assert candidate(0) == 0\n",
)
# Same function, but the docstring example is wrong: the canonical solution cannot pass it.
BAD_DOCTEST = Problem(
    number=8,
    entry_point="triple_it",
    prompt='def triple_it(n: int) -> int:\n    """\n    >>> triple_it(2)\n    7\n    """\n',
    canonical_solution="    return n * 3\n",
    test="def check(candidate):\n    assert candidate(2) == 6\n",
)
NO_DOCTEST = Problem(
    number=9, entry_point="noop", prompt='def noop():\n    """Nothing."""\n',
    canonical_solution="    return None\n", test="def check(candidate):\n    candidate()\n",
)


def test_render_task_layout(tmp_path: Path):
    task_dir = render_task(GOOD, tmp_path)
    assert task_dir.name == "humaneval-007-double-it"
    task = load_task(task_dir)  # the in-house loader accepts it unchanged
    assert task.category == "humaneval"
    assert "double_it.py" in task.prompt and "Do not change the tests" in task.prompt

    stub = (task.repo_dir / "double_it.py").read_text()
    assert stub.endswith("raise NotImplementedError\n") and ">>> double_it(2)" in stub
    visible = (task.repo_dir / "tests" / "test_double_it.py").read_text()
    assert "doctest.testmod(double_it" in visible
    hidden = (task.hidden_tests_dir / "test_double_it_hidden.py").read_text()
    assert "def check(candidate)" in hidden and "check(double_it)" in hidden
    assert (task.solution_dir / "double_it.py").read_text().endswith("return n * 2\n")


def test_stub_fails_visible_test_and_solution_passes_hidden(tmp_path: Path):
    task = load_task(render_task(GOOD, tmp_path))
    before = task.materialise(tmp_path / "before")
    assert grade(before).exit_code != 0  # doctest raises NotImplementedError
    after = task.materialise(tmp_path / "after")
    task.apply_solution(after)
    task.install_hidden_tests(after)
    assert grade(after).exit_code == 0
    assert is_well_posed(task.path)


def test_build_slice_skips_problems_whose_examples_do_not_doctest(tmp_path: Path):
    decisions = []
    kept = build_slice(
        [NO_DOCTEST, BAD_DOCTEST, GOOD], tmp_path / "out", count=5,
        on_decision=lambda p, ok: decisions.append((p.number, ok)),
    )
    assert [p.number for p in eligible([NO_DOCTEST, BAD_DOCTEST, GOOD])] == [8, 7]
    assert decisions == [(8, False), (7, True)]
    assert [k.name for k in kept] == ["humaneval-007-double-it"]
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["humaneval-007-double-it"]


def test_load_problems_reads_gzipped_jsonl(tmp_path: Path):
    row = {
        "task_id": "HumanEval/42", "entry_point": "f", "prompt": "p", "canonical_solution": "c",
        "test": "t",
    }
    path = tmp_path / "HumanEval.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n\n")
    (problems,) = load_problems(path)
    assert problems.number == 42 and problems.task_id == "humaneval-042-f"
