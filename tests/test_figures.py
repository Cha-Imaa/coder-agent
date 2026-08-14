"""Figure arithmetic is tested without a display; drawing is checked by producing the files.

A README chart is only as trustworthy as the numbers behind it, so the curve, the per-node
averages and the "newest file per label" rule get exact assertions on hand-built results.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coder_agent.evals.figures import (
    iteration_curve,
    latest_results,
    max_iterations,
    node_costs,
    render_all,
)
from coder_agent.evals.runner import SuiteResult, TaskResult, load_result
from coder_agent.telemetry.ledger import Ledger, RunRecord


def _task(task_id: str, category: str, passed: bool, iterations: int, usage=None, error=None):
    return TaskResult(
        task_id=task_id, category=category, passed=passed, agent_status="error" if error else "x",
        iterations=iterations, steps=iterations * 5, input_tokens=100, output_tokens=10,
        agent_seconds=1.0, grade_exit_code=0 if passed else 1, grade_output="",
        error=error, usage=usage or {},
    )


USAGE_A = {
    "plan": {"calls": 1, "input_tokens": 1000, "output_tokens": 200, "models": {"m": 1}},
    "act": {"calls": 3, "input_tokens": 6000, "output_tokens": 600, "models": {"m": 3}},
}
USAGE_B = {"act": {"calls": 1, "input_tokens": 2000, "output_tokens": 400, "models": {"m": 1}}}


def _suite(label="run", model="groq:m", started_at=1000.0, k=4) -> SuiteResult:
    return SuiteResult(
        label=label, model=model, started_at=started_at, meta={"max_iterations": k},
        results=[
            _task("a", "fix-bug", True, 1, USAGE_A),
            _task("b", "fix-bug", True, 2, USAGE_B),
            _task("c", "add-feature", True, 2),
            _task("d", "add-feature", False, 4),
            _task("e", "refactor", False, 0, error="boom"),
        ],
    )


def _record(usage, started_at: float) -> RunRecord:
    return RunRecord(
        repo="r", task="t", model="m", status="passed", iterations=1, steps=1, wall_seconds=1,
        tests_passed=True, test_command="pytest", usage=usage, tags={"task_id": "c"},
        started_at=started_at,
    )


def test_iteration_curve_is_cumulative_and_ends_at_pass_rate():
    suite = _suite()
    assert iteration_curve(suite) == [0.2, 0.6, 0.6, 0.6]
    assert iteration_curve(suite)[-1] == suite.pass_rate
    assert iteration_curve(suite, 2) == [0.2, 0.6]


def test_max_iterations_prefers_meta_but_never_below_observed():
    assert max_iterations(_suite(k=4)) == 4
    suite = _suite(k=0)
    suite.results[3].iterations = 6
    assert max_iterations(suite) == 6


def test_node_costs_average_over_runs_with_usage_only():
    rows = {r["node"]: r for r in node_costs(_suite())}
    # Two of five tasks carry usage; the errored one and the two without usage do not count.
    assert rows["act"]["runs"] == 2
    assert rows["act"]["input_tokens"] == (6000 + 2000) / 2
    assert rows["act"]["calls"] == 2.0
    assert rows["plan"]["input_tokens"] == 1000 / 2
    assert [r["node"] for r in node_costs(_suite())] == ["act", "plan"]


def test_node_costs_fall_back_to_ledger_by_task_id(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.record(_record(USAGE_B, started_at=1.0))
    ledger.record(_record(USAGE_A, started_at=2.0))
    assert ledger.usage_for(task_id="c") == USAGE_A  # the most recent run wins
    assert ledger.usage_for(task_id="zzz") is None
    rows = {r["node"]: r for r in node_costs(_suite(), ledger)}
    assert rows["act"]["runs"] == 3
    assert rows["plan"]["input_tokens"] == pytest.approx(2000 / 3)


def test_latest_results_keeps_newest_per_label_and_skips_self_checks(tmp_path: Path):
    _suite("run", started_at=1.0).write(tmp_path)
    _suite("run", started_at=2.0).write(tmp_path)
    _suite("other", started_at=1.5).write(tmp_path)
    _suite("solution", model="reference-solution", started_at=3.0).write(tmp_path)
    _suite("noop", model="noop", started_at=3.0).write(tmp_path)
    suites = latest_results(tmp_path)
    assert [(s.label, s.started_at) for s in suites] == [("other", 1.5), ("run", 2.0)]


def test_results_files_round_trip_usage_and_load_without_it(tmp_path: Path):
    path = _suite().write(tmp_path)
    loaded = load_result(path)
    assert loaded.results[0].usage == USAGE_A
    data = json.loads(path.read_text(encoding="utf-8"))
    for r in data["results"]:
        r.pop("usage")
    old = tmp_path / "old.json"
    old.write_text(json.dumps(data), encoding="utf-8")
    assert load_result(old).results[0].usage == {}


def test_render_all_writes_three_pngs(tmp_path: Path):
    pytest.importorskip("matplotlib")
    paths = render_all([_suite("a", started_at=1.0), _suite("b", started_at=2.0)], tmp_path)
    assert [p.name for p in paths] == ["pass_rate.png", "iteration_curve.png", "cost_profile.png"]
    assert all(p.stat().st_size > 1000 for p in paths)
