"""Evaluation harness: task suites, runner, metrics."""

from coder_agent.evals.retrieval import (
    RetrievalReport,
    RetrievalResult,
    evaluate,
    gold_files,
    load_report,
)
from coder_agent.evals.runner import (
    RESULTS_DIR,
    SuiteResult,
    TaskResult,
    graph_agent,
    load_result,
    noop_agent,
    run_suite,
    run_task,
    solution_agent,
)
from coder_agent.evals.tasks import (
    CATEGORIES,
    SUITE_DIR,
    SUITES,
    EvalTask,
    grade,
    load_suite,
    load_suites,
    load_task,
)

__all__ = [
    "CATEGORIES",
    "RESULTS_DIR",
    "SUITES",
    "SUITE_DIR",
    "EvalTask",
    "RetrievalReport",
    "RetrievalResult",
    "SuiteResult",
    "TaskResult",
    "evaluate",
    "gold_files",
    "grade",
    "graph_agent",
    "load_report",
    "load_result",
    "load_suite",
    "load_suites",
    "load_task",
    "noop_agent",
    "run_suite",
    "run_task",
    "solution_agent",
]
