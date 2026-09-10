"""Run the eval suite: one isolated repo per task, hidden tests as the grade, JSON results.

The runner does not know how the agent works. It takes an `Agent` callable, hands it a fresh
copy of the task repo and the prompt, waits, then installs the hidden tests and runs them. That
separation is what makes the harness testable without a model (see `solution_agent` and
`noop_agent`) and reusable for ablations: swap the callable, keep the grading.

Every task is graded by the same rule the suite itself is validated with: hidden pytest exit
code 0 is a pass. Whatever the agent *claims* about its own test run is recorded but ignored.
"""

from __future__ import annotations

import asyncio
import json
import platform
import re
import shutil
import subprocess
import tempfile
import time
import traceback
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from coder_agent.evals.tasks import CATEGORIES, EvalTask, grade

# An agent receives (materialised repo, prompt) and returns a dict with at least `status`;
# `iteration`, `steps`, `usage` are read if present. The real one is `graph_agent`.
Agent = Callable[[Path, str], Awaitable[dict[str, Any]]]

RESULTS_DIR = Path(__file__).resolve().parents[3] / "evals" / "results"


@dataclass
class TaskResult:
    task_id: str
    category: str
    passed: bool
    agent_status: str
    iterations: int
    steps: int
    input_tokens: int
    output_tokens: int
    agent_seconds: float
    grade_exit_code: int | None
    grade_output: str
    error: str | None = None
    # Per-node token counters in the ledger's shape, so the cost profile can be drawn from the
    # committed results file alone. Files written before this field existed load with `{}`.
    usage: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class SuiteResult:
    label: str
    model: str
    started_at: float
    results: list[TaskResult] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def pass_rate(self) -> float:
        return sum(r.passed for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def errors(self) -> int:
        """Tasks where the agent crashed (rate limit, tool server, bug). Counted as failures in
        the pass rate but reported separately, because they say nothing about the agent's skill."""
        return sum(1 for r in self.results if r.error)

    def merged_with(self, fresh: SuiteResult) -> SuiteResult:
        """Replace this suite's results with `fresh` ones for the same task ids.

        Used to rerun only the errored tasks after a quota reset: the graded ones keep their
        original numbers, so the merged file still describes one configuration of the agent.
        """
        replaced = {r.task_id: r for r in fresh.results}
        results = [replaced.pop(r.task_id, r) for r in self.results] + list(replaced.values())
        meta = {**self.meta, "merged_from": [self.meta.get("commit"), fresh.meta.get("commit")]}
        return SuiteResult(
            label=self.label, model=self.model, started_at=self.started_at, results=results, meta=meta
        )

    def by_category(self) -> list[dict[str, Any]]:
        """Rows for the pass-rate table, in the canonical category order, plus a total row."""
        rows = []
        for cat in CATEGORIES:
            rs = [r for r in self.results if r.category == cat]
            if rs:
                rows.append(_row(cat, rs))
        rows.append(_row("total", self.results))
        return rows

    def to_json(self) -> str:
        payload = {
            "label": self.label,
            "model": self.model,
            "started_at": self.started_at,
            "meta": self.meta,
            "pass_rate": self.pass_rate,
            "errors": self.errors,
            "by_category": self.by_category(),
            "results": [asdict(r) for r in self.results],
        }
        return json.dumps(payload, indent=2)

    def write(self, results_dir: Path = RESULTS_DIR) -> Path:
        results_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started_at))
        path = results_dir / f"{stamp}-{self.label}.json"
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    def markdown_table(self) -> str:
        lines = [
            "| Category | Tasks | Passed | Errors | pass@1 | Avg tokens | Avg iterations |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in self.by_category():
            lines.append(
                f"| {r['category']} | {r['tasks']} | {r['passed']} | {r['errors']} | "
                f"{r['pass_rate']:.0%} | {r['avg_tokens']:,.0f} | {r['avg_iterations']:.1f} |"
            )
        return "\n".join(lines)


def _row(name: str, rs: list[TaskResult]) -> dict[str, Any]:
    n = len(rs)
    return {
        "category": name,
        "tasks": n,
        "passed": sum(r.passed for r in rs),
        "errors": sum(1 for r in rs if r.error),
        "pass_rate": (sum(r.passed for r in rs) / n) if n else 0.0,
        "avg_tokens": (sum(r.total_tokens for r in rs) / n) if n else 0.0,
        "avg_iterations": (sum(r.iterations for r in rs) / n) if n else 0.0,
        "avg_seconds": (sum(r.agent_seconds for r in rs) / n) if n else 0.0,
    }


def load_result(path: Path) -> SuiteResult:
    """Read a results file back; used by the figure script and by tests."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return SuiteResult(
        label=data["label"],
        model=data["model"],
        started_at=data["started_at"],
        results=[TaskResult(**r) for r in data["results"]],
        meta=data.get("meta", {}),
    )


# --------------------------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------------------------


async def graph_agent(repo: Path, prompt: str) -> dict[str, Any]:
    """The real thing: the same `run_agent` the CLI uses, tagged as an eval run in the ledger."""
    from coder_agent.agent import run_agent

    outcome = await run_agent(repo, prompt, tags={"suite": "in-house", "task_id": repo.name})
    return {**outcome.final, "usage": outcome.record.usage}


def solution_agent(task: EvalTask) -> Agent:
    """Overlay the reference solution. Proves the harness scores a correct fix as a pass."""

    async def agent(repo: Path, prompt: str) -> dict[str, Any]:
        task.apply_solution(repo)
        return {"status": "passed", "iteration": 1, "steps": 0}

    return agent


async def noop_agent(repo: Path, prompt: str) -> dict[str, Any]:
    """Do nothing. Proves the harness scores an untouched repo as a fail."""
    return {"status": "failed", "iteration": 0, "steps": 0}


# --------------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------------


async def run_task(
    task: EvalTask, agent: Agent, workdir: Path, *, grade_timeout: int = 300
) -> TaskResult:
    """Materialise, run the agent, install hidden tests, grade. Never raises: a crashing agent
    is a failed task with `error` set, so one bad run cannot take the whole suite down."""
    # The directory is named after the task so the ledger's `repo` column and traces read well.
    repo = task.materialise(workdir / task.id)
    started = time.time()
    final: dict[str, Any] = {}
    error: str | None = None
    try:
        final = await agent(repo, task.prompt)
    except Exception:  # noqa: BLE001 - the whole point is to keep the suite running
        error = traceback.format_exc()
    agent_seconds = time.time() - started

    # Install hidden tests only after the agent is done: it must never have seen them.
    task.install_hidden_tests(repo)
    verdict = grade(repo, timeout=grade_timeout)

    usage = final.get("usage") or {}
    inp = sum(v.get("input_tokens", 0) for v in usage.values())
    outp = sum(v.get("output_tokens", 0) for v in usage.values())
    return TaskResult(
        task_id=task.id,
        category=task.category,
        passed=verdict.exit_code == 0,
        agent_status="error" if error else str(final.get("status", "unknown")),
        iterations=int(final.get("iteration", 0)),
        steps=int(final.get("steps", 0)),
        input_tokens=inp,
        output_tokens=outp,
        agent_seconds=agent_seconds,
        grade_exit_code=verdict.exit_code,
        grade_output=verdict.output[-2000:],
        error=_scrub(error),
        usage=usage,
    )


def _scrub(text: str | None) -> str | None:
    """Results files are committed; tracebacks must not leak the machine's home directory."""
    if not text:
        return text
    home = str(Path.home())
    text = text.replace(home, "~").replace(home.replace("\\", "/"), "~")
    return re.sub(r"~[^\s\"']*?site-packages", "<site-packages>", text)


async def run_suite(
    tasks: Iterable[EvalTask],
    agent_for: Callable[[EvalTask], Agent],
    *,
    label: str,
    model: str,
    workdir: Path | None = None,
    keep_workdirs: bool = False,
    on_result: Callable[[TaskResult], None] | None = None,
    pause_seconds: float = 0.0,
) -> SuiteResult:
    """Run every task sequentially and collect a `SuiteResult`.

    Sequential on purpose: free-tier rate limits are per minute, and a parallel runner would
    spend its budget on 429s. `pause_seconds` between tasks is the crude way to stay under them.
    """
    suite = SuiteResult(label=label, model=model, started_at=time.time(), meta=_meta())
    root = workdir or Path(tempfile.mkdtemp(prefix="coder-evals-"))
    root.mkdir(parents=True, exist_ok=True)
    try:
        for i, task in enumerate(tasks):
            if i and pause_seconds:
                await asyncio.sleep(pause_seconds)
            result = await run_task(task, agent_for(task), root)
            suite.results.append(result)
            if on_result is not None:
                on_result(result)
    finally:
        if not keep_workdirs:
            shutil.rmtree(root, ignore_errors=True)
        else:
            suite.meta["workdir"] = str(root)
    return suite


def _meta() -> dict[str, Any]:
    """Enough provenance to reproduce a number: commit, platform, and the loop settings."""
    from coder_agent.config import settings

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
        ).stdout.strip()
    except OSError:
        commit = ""
    return {
        "commit": commit,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "max_iterations": settings.max_iterations,
        "max_steps": settings.max_steps,
        "fallback_model": settings.fallback_model,
    }
