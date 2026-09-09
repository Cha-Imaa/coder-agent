"""The in-house eval suite is itself under test: every task must be well-posed.

A benchmark task is only meaningful if (a) the hidden tests fail on the untouched repo, so
doing nothing scores zero, and (b) they pass once the reference solution is applied, so the task
is solvable and the grader is not broken. Both are checked for every task here.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from coder_agent.evals import CATEGORIES, EvalTask, grade, load_suite, load_task

SUITE = load_suite()


def test_suite_has_twelve_tasks_over_all_categories():
    assert len(SUITE) == 12
    per_category = Counter(t.category for t in SUITE)
    assert set(per_category) == set(CATEGORIES)
    assert min(per_category.values()) >= 2


def test_ids_are_unique_and_prompts_are_self_contained():
    ids = [t.id for t in SUITE]
    assert len(set(ids)) == len(ids)
    for task in SUITE:
        assert task.id.startswith(task.category)
        assert len(task.prompt) > 40, task.id
        assert task.notes, task.id


def test_load_task_rejects_malformed_spec(tmp_path: Path):
    bad = tmp_path / "fix-bug-x"
    bad.mkdir()
    (bad / "task.toml").write_text('id = "fix-bug-x"\ncategory = "nope"\nprompt = "p"\n')
    with pytest.raises(ValueError, match="unknown category"):
        load_task(bad)


def test_materialise_is_an_independent_copy(tmp_path: Path):
    task = SUITE[0]
    dest = task.materialise(tmp_path / "work")
    (dest / "scratch.txt").write_text("x")
    assert not (task.repo_dir / "scratch.txt").exists()
    assert not list(dest.rglob("__pycache__"))


def test_hidden_tests_replace_the_visible_file_of_the_same_name(tmp_path: Path):
    task = next(t for t in SUITE if t.id == "refactor-config-dataclass")
    dest = task.materialise(tmp_path / "work")
    (dest / "tests" / "test_appconfig.py").write_text("def test_agent_rewrote_me(): pass\n")
    task.install_hidden_tests(dest)
    assert "Config" in (dest / "tests" / "test_appconfig.py").read_text()


@pytest.mark.parametrize("task", SUITE, ids=[t.id for t in SUITE])
def test_hidden_tests_fail_before_and_pass_after_the_solution(task: EvalTask, tmp_path: Path):
    before = task.materialise(tmp_path / "before")
    task.install_hidden_tests(before)
    result = grade(before)
    assert result.exit_code != 0, f"{task.id}: hidden tests pass on the untouched repo"

    after = task.materialise(tmp_path / "after")
    task.apply_solution(after)
    task.install_hidden_tests(after)
    result = grade(after)
    assert result.exit_code == 0, f"{task.id}: reference solution fails\n{result.output}"
