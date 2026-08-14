"""Run the in-house eval suite and print the pass-rate table.

    uv run python evals/run_evals.py                      # all 12 tasks with the real agent
    uv run python evals/run_evals.py --category fix-bug   # one category
    uv run python evals/run_evals.py --task add-feature-slugify --task fix-bug-duration-units
    uv run python evals/run_evals.py --agent solution     # harness self-check, must be 100%
    uv run python evals/run_evals.py --agent noop         # harness self-check, must be 0%

Results go to `evals/results/<timestamp>-<label>.json`; the table is also printed as Markdown
so it can be pasted into the README.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from coder_agent.config import settings
from coder_agent.evals import load_suite
from coder_agent.evals.runner import (
    RESULTS_DIR,
    SuiteResult,
    TaskResult,
    graph_agent,
    load_result,
    noop_agent,
    run_suite,
    solution_agent,
)

logging.getLogger("langchain_google_genai").setLevel(logging.ERROR)
logging.getLogger("google_genai").setLevel(logging.ERROR)

console = Console()
app = typer.Typer(add_completion=False)


@app.command()
def main(
    category: Annotated[str | None, typer.Option(help="Only tasks in this category.")] = None,
    task: Annotated[list[str] | None, typer.Option(help="Only these task ids (repeatable).")] = None,
    agent: Annotated[str, typer.Option(help="graph | solution | noop")] = "graph",
    label: Annotated[str | None, typer.Option(help="Name for the results file.")] = None,
    model: Annotated[str | None, typer.Option(help="provider:model, overrides CODER_MODEL.")] = None,
    max_iterations: Annotated[int | None, typer.Option(help="Plan/act/test cycles per task.")] = None,
    pause: Annotated[float, typer.Option(help="Seconds to wait between tasks (rate limits).")] = 0.0,
    keep_workdirs: Annotated[bool, typer.Option(help="Leave the materialised repos on disk.")] = False,
    results_dir: Annotated[Path, typer.Option(help="Where to write the JSON.")] = RESULTS_DIR,
    rerun_errors: Annotated[
        Path | None,
        typer.Option(help="Previous results file: rerun only its errored tasks and merge."),
    ] = None,
) -> None:
    if model:
        settings.model = model
    if max_iterations is not None:
        settings.max_iterations = max_iterations

    tasks = load_suite(category=category)
    previous = load_result(rerun_errors) if rerun_errors else None
    if previous is not None:
        errored = {r.task_id for r in previous.results if r.error}
        tasks = [t for t in tasks if t.id in errored]
        label = label or previous.label
        console.print(f"Rerunning {len(tasks)} errored task(s) from {rerun_errors.name}")
    if task:
        wanted = set(task)
        tasks = [t for t in tasks if t.id in wanted]
        missing = wanted - {t.id for t in tasks}
        if missing:
            console.print(f"[red]Unknown task id(s):[/red] {sorted(missing)}")
            raise typer.Exit(code=2)
    if not tasks:
        console.print("[red]No tasks selected.[/red]")
        raise typer.Exit(code=2)

    if agent == "graph":
        agent_for = lambda t: graph_agent
        model_name = settings.model
    elif agent == "solution":
        agent_for = solution_agent
        model_name = "reference-solution"
    elif agent == "noop":
        agent_for = lambda t: noop_agent
        model_name = "noop"
    else:
        console.print(f"[red]Unknown agent:[/red] {agent}")
        raise typer.Exit(code=2)

    label = label or (agent if agent != "graph" else settings.model.split(":")[-1].replace("/", "-"))
    console.print(
        f"[bold]{len(tasks)} task(s)[/bold] · agent={agent} · model={model_name} · "
        f"max_iterations={settings.max_iterations}"
    )

    def on_result(r: TaskResult) -> None:
        colour = "green" if r.passed else "red"
        note = f" [dim]{r.agent_status}[/dim]" if r.agent_status not in ("passed", "failed") else ""
        console.print(
            f"  [{colour}]{'PASS' if r.passed else 'FAIL'}[/{colour}] {r.task_id:<34} "
            f"{r.iterations} iter · {r.total_tokens:>7,} tok · {r.agent_seconds:5.0f}s{note}"
        )

    suite = asyncio.run(
        run_suite(
            tasks,
            agent_for,
            label=label,
            model=model_name,
            keep_workdirs=keep_workdirs,
            on_result=on_result,
            pause_seconds=pause,
        )
    )
    if previous is not None:
        suite = previous.merged_with(suite)
    path = suite.write(results_dir)
    _print_table(suite)
    console.print(f"\n[dim]Results written to {path}[/dim]")
    console.print("\n" + suite.markdown_table())
    if suite.meta.get("workdir"):
        console.print(f"[dim]Work directories kept under {suite.meta['workdir']}[/dim]")


def _print_table(suite: SuiteResult) -> None:
    table = Table(title=f"pass@1 · {suite.label} · {suite.model}", show_edge=False)
    cols = ("category", "tasks", "passed", "errors", "pass@1", "avg tokens", "avg iter", "avg secs")
    for col in cols:
        table.add_column(col, justify="left" if col == "category" else "right")
    for r in suite.by_category():
        style = "bold" if r["category"] == "total" else ""
        table.add_row(
            r["category"], str(r["tasks"]), str(r["passed"]), str(r["errors"]),
            f"{r['pass_rate']:.0%}",
            f"{r['avg_tokens']:,.0f}", f"{r['avg_iterations']:.1f}", f"{r['avg_seconds']:.0f}",
            style=style,
        )
    console.print(table)


if __name__ == "__main__":
    app()
