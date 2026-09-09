"""The eval runner is tested without a model, using agents whose verdict is known in advance.

A harness that cannot tell a correct fix from no fix would produce a meaningless pass rate, so
the two self-checks here (reference solution scores 100%, doing nothing scores 0%) are the
tests that matter most. The rest covers isolation, crash handling and the results file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from coder_agent.evals import (
    EvalTask,
    load_result,
    load_suite,
    noop_agent,
    run_suite,
    run_task,
    solution_agent,
)

SUITE = load_suite()
# Two cheap tasks from different categories keep the module under ten seconds.
SAMPLE = [
    next(t for t in SUITE if t.id == "fix-bug-duration-units"),
    next(t for t in SUITE if t.id == "add-feature-stack-peek"),
]


async def test_solution_agent_passes_every_task(tmp_path: Path):
    suite = await run_suite(
        SAMPLE, solution_agent, label="sol", model="ref", workdir=tmp_path / "w"
    )
    assert [r.passed for r in suite.results] == [True, True]
    assert suite.pass_rate == 1.0
    assert all(r.grade_exit_code == 0 for r in suite.results)


async def test_noop_agent_fails_every_task(tmp_path: Path):
    suite = await run_suite(
        SAMPLE, lambda t: noop_agent, label="noop", model="none", workdir=tmp_path / "w"
    )
    assert [r.passed for r in suite.results] == [False, False]
    assert suite.pass_rate == 0.0


async def test_agent_never_sees_hidden_tests_and_cannot_pass_by_editing_visible_ones(tmp_path: Path):
    task = SAMPLE[0]
    seen: dict[str, Any] = {}

    async def cheat(repo: Path, prompt: str) -> dict[str, Any]:
        seen["hidden_present"] = any(
            (repo / "tests" / p.name).exists() for p in task.hidden_tests_dir.glob("*.py")
        )
        seen["prompt"] = prompt
        # Delete every visible test so the agent's own test run would be green.
        for f in (repo / "tests").glob("test_*.py"):
            f.unlink()
        return {"status": "passed", "iteration": 1, "steps": 3}

    result = await run_task(task, cheat, tmp_path)
    assert seen["hidden_present"] is False
    assert seen["prompt"] == task.prompt
    assert result.agent_status == "passed"  # what the agent claimed
    assert result.passed is False  # what the hidden tests said


async def test_crashing_agent_is_recorded_not_raised(tmp_path: Path):
    async def boom(repo: Path, prompt: str) -> dict[str, Any]:
        raise RuntimeError("provider exploded")

    result = await run_task(SAMPLE[0], boom, tmp_path)
    assert result.passed is False
    assert result.agent_status == "error"
    assert result.error and "provider exploded" in result.error


async def test_each_task_gets_its_own_untouched_copy(tmp_path: Path):
    async def scribble(repo: Path, prompt: str) -> dict[str, Any]:
        (repo / "MARK").write_text(repo.name)
        return {"status": "passed"}

    suite = await run_suite(
        SAMPLE, lambda t: scribble, label="x", model="m", workdir=tmp_path / "w", keep_workdirs=True
    )
    for task in SAMPLE:
        assert (tmp_path / "w" / task.id / "MARK").read_text() == task.id
        assert not (task.repo_dir / "MARK").exists()
    assert suite.meta["workdir"] == str(tmp_path / "w")


async def test_workdir_is_removed_unless_kept(tmp_path: Path):
    work = tmp_path / "w"
    await run_suite(SAMPLE[:1], lambda t: noop_agent, label="x", model="m", workdir=work)
    assert not work.exists()


async def test_tokens_and_iterations_are_taken_from_the_final_state(tmp_path: Path):
    async def costly(repo: Path, prompt: str) -> dict[str, Any]:
        return {
            "status": "failed",
            "iteration": 3,
            "steps": 9,
            "usage": {
                "plan": {"calls": 3, "input_tokens": 300, "output_tokens": 30},
                "act": {"calls": 6, "input_tokens": 6000, "output_tokens": 600},
            },
        }

    result = await run_task(SAMPLE[0], costly, tmp_path)
    assert (result.iterations, result.steps) == (3, 9)
    assert (result.input_tokens, result.output_tokens) == (6300, 630)
    assert result.total_tokens == 6930


async def test_results_file_round_trips_and_table_has_total_row(tmp_path: Path):
    suite = await run_suite(
        SAMPLE, solution_agent, label="sol", model="ref", workdir=tmp_path / "w"
    )
    path = suite.write(tmp_path / "results")
    assert path.name.endswith("-sol.json")

    data = json.loads(path.read_text())
    assert data["pass_rate"] == 1.0
    assert data["meta"]["max_iterations"] >= 1
    assert data["by_category"][-1]["category"] == "total"
    assert {r["category"] for r in data["by_category"]} == {"fix-bug", "add-feature", "total"}

    again = load_result(path)
    assert again.pass_rate == 1.0
    assert [r.task_id for r in again.results] == [t.id for t in SAMPLE]

    md = suite.markdown_table()
    assert md.splitlines()[0].startswith("| Category |")
    assert "| total | 2 | 2 | 0 | 100% |" in md


async def test_merge_replaces_only_the_rerun_tasks(tmp_path: Path):
    async def boom(repo: Path, prompt: str) -> dict[str, Any]:
        raise RuntimeError("quota")

    first = await run_suite(
        SAMPLE, lambda t: boom, label="run", model="m", workdir=tmp_path / "a"
    )
    assert first.errors == 2 and first.by_category()[-1]["errors"] == 2

    errored = [t for t in SAMPLE if t.id in {r.task_id for r in first.results if r.error}][:1]
    fresh = await run_suite(
        errored, solution_agent, label="run", model="m", workdir=tmp_path / "b"
    )
    merged = first.merged_with(fresh)
    assert [r.task_id for r in merged.results] == [t.id for t in SAMPLE]
    assert merged.errors == 1 and merged.pass_rate == 0.5
    assert merged.results[0].passed and merged.results[0].error is None
    assert merged.results[1].error and "quota" in merged.results[1].error
    assert "merged_from" in merged.meta


def test_solution_agent_is_bound_to_its_task():
    task: EvalTask = SAMPLE[1]
    agent = solution_agent(task)
    assert callable(agent)


@pytest.mark.parametrize("task", SAMPLE, ids=[t.id for t in SAMPLE])
async def test_grade_output_is_capped(task: EvalTask, tmp_path: Path):
    result = await run_task(task, noop_agent, tmp_path)
    assert len(result.grade_output) <= 2000
    assert "failed" in result.grade_output or "error" in result.grade_output.lower()
