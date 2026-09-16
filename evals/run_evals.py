"""Run the in-house eval suite and print the pass-rate table.

    uv run python evals/run_evals.py                      # all 12 in-house tasks with the real agent
    uv run python evals/run_evals.py --quick              # the cheapest task in each of four categories
    uv run python evals/run_evals.py --parallel 4         # four tasks in flight (see run_suite)
    uv run python evals/run_evals.py --suite humaneval    # the 30-problem HumanEval slice
    uv run python evals/run_evals.py --suite all
    uv run python evals/run_evals.py --category fix-bug   # one category
    uv run python evals/run_evals.py --task add-feature-slugify --task fix-bug-duration-units
    uv run python evals/run_evals.py --agent solution     # harness self-check, must be 100%
    uv run python evals/run_evals.py --agent noop         # harness self-check, must be 0%
    uv run python evals/run_evals.py --retrieval off      # ablation: one run per mode, then
    uv run python evals/run_evals.py --retrieval hybrid   #   `evals/figures.py` draws the chart
    uv run python evals/run_evals.py --model ollama:qwen2.5-coder:7b   # model comparison arm

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
from coder_agent.evals import load_suites, quick_subset
from coder_agent.evals.runner import (
    RESULTS_DIR,
    SuiteResult,
    TaskResult,
    graph_agent,
    label_for_model,
    load_result,
    noop_agent,
    run_suite,
    solution_agent,
)
from coder_agent.sandbox import SandboxError
from coder_agent.sandbox import configure as configure_sandbox

logging.getLogger("langchain_google_genai").setLevel(logging.ERROR)
logging.getLogger("google_genai").setLevel(logging.ERROR)

console = Console()
app = typer.Typer(add_completion=False)


@app.command()
def main(
    suite: Annotated[str, typer.Option(help="inhouse | humaneval | all")] = "inhouse",
    quick: Annotated[
        bool,
        typer.Option(
            "--quick",
            help="Only the four-task quick subset, for a cheap experiment; the results file is "
            "labelled -quick and no figure draws it.",
        ),
    ] = False,
    full: Annotated[
        bool, typer.Option("--full", hidden=True, help="Accepted for older commands; the default.")
    ] = False,
    parallel: Annotated[
        int,
        typer.Option(
            help="Tasks in flight at once. A worker retires when its task hit a rate limit, so "
            "start wide and let the run narrow itself. 1 is the safe value for Groq's tier; "
            "K2 Think allows two requests a second and takes 4 comfortably."
        ),
    ] = 1,
    category: Annotated[str | None, typer.Option(help="Only tasks in this category.")] = None,
    task: Annotated[list[str] | None, typer.Option(help="Only these task ids (repeatable).")] = None,
    agent: Annotated[str, typer.Option(help="graph | solution | noop")] = "graph",
    label: Annotated[str | None, typer.Option(help="Name for the results file.")] = None,
    model: Annotated[str | None, typer.Option(help="provider:model, overrides CODER_MODEL.")] = None,
    max_iterations: Annotated[int | None, typer.Option(help="Plan/act/test cycles per task.")] = None,
    retrieval: Annotated[
        str | None,
        typer.Option(help="Retrieval mode for this run: hybrid, dense, bm25 or off (ablation)."),
    ] = None,
    sandbox: Annotated[
        str | None, typer.Option(help="Where the agent's commands run: local or docker.")
    ] = None,
    pause: Annotated[float, typer.Option(help="Minimum seconds between task starts.")] = 0.0,
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
    if retrieval is not None:
        if retrieval not in ("hybrid", "dense", "bm25", "off"):
            console.print(f"[red]Unknown retrieval mode:[/red] {retrieval}")
            raise typer.Exit(code=2)
        settings.retrieval_mode = retrieval
    try:
        configure_sandbox(sandbox)
    except SandboxError as exc:  # unknown mode, or Docker not usable on this machine
        console.print(f"[red]Sandbox:[/red] {exc}")
        raise typer.Exit(code=2) from None

    try:
        tasks = load_suites([suite], category=category)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from None
    # The full suite is the default: the headline numbers and every figure come from it, and
    # with K2 Think's quota one arm is minutes, not a day of the free tier. `--quick` opts into
    # the four-task subset for an experiment. An explicit `--task` or `--category` is a narrower
    # request already, so it is left alone, and `--rerun-errors` means "the ones that errored".
    explicit_selection = bool(task) or category is not None
    use_quick = quick and not explicit_selection and rerun_errors is None
    if use_quick:
        tasks = quick_subset(tasks)

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

    label = label or (agent if agent != "graph" else label_for_model(settings.model))
    if retrieval is not None and agent == "graph":
        label = f"{label}-retrieval-{retrieval}"  # one results label per ablation arm
    if suite != "inhouse":
        label = f"{suite}-{label}"
    if use_quick:
        # Part of the label, not just the metadata: a four-task pass rate must never sit in a
        # results directory looking like the twelve-task number the README quotes.
        label = f"{label}-quick"
    console.print(
        f"[bold]{len(tasks)} task(s)[/bold] · agent={agent} · model={model_name} · "
        f"max_iterations={settings.max_iterations} · retrieval={settings.retrieval_mode} · "
        f"sandbox={settings.sandbox_mode} · parallel={parallel}"
    )
    if use_quick:
        console.print(
            "[dim]Quick subset: the cheapest task in each of four categories, about 57k tokens; "
            "not the headline number, and no figure draws it.[/dim]"
        )

    def on_result(r: TaskResult) -> None:
        colour = "green" if r.passed else "red"
        note = f" [dim]{r.agent_status}[/dim]" if r.agent_status not in ("passed", "failed") else ""
        console.print(
            f"  [{colour}]{'PASS' if r.passed else 'FAIL'}[/{colour}] {r.task_id:<34} "
            f"{r.iterations} iter · {r.total_tokens:>7,} tok · {r.agent_seconds:5.0f}s{note}"
        )
        if r.provider_errors:
            failed = ", ".join(f"{n} {name}" for name, n in sorted(r.provider_errors.items()))
            console.print(f"       [yellow]provider errors:[/yellow] {failed}")

    suite_name = suite
    suite = asyncio.run(
        run_suite(
            tasks,
            agent_for,
            label=label,
            model=model_name,
            keep_workdirs=keep_workdirs,
            on_result=on_result,
            pause_seconds=pause,
            parallel=parallel,
        )
    )
    suite.meta["suite"] = suite_name  # figures compare models on the same suite only
    suite.meta["subset"] = "quick" if use_quick else "full"  # figures ignore anything but full
    if previous is not None:
        suite = previous.merged_with(suite)
    path = suite.write(results_dir)
    _print_table(suite)
    console.print(f"\n[dim]Results written to {path}[/dim]")
    if suite.meta.get("workers_retired"):
        console.print(
            f"[yellow]{suite.meta['workers_retired']} worker(s) retired after rate limits;[/yellow] "
            f"the run finished {suite.meta['parallel'] - suite.meta['workers_retired']} wide."
        )
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
