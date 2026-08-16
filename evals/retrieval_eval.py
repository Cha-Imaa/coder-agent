"""Measure retrieval on the benchmark tasks: recall@k of the files the reference fix touches.

    uv run python evals/retrieval_eval.py                     # in-house suite, all three modes
    uv run python evals/retrieval_eval.py --suite all         # plus the HumanEval slice
    uv run python evals/retrieval_eval.py --mode dense --mode hybrid
    uv run python evals/retrieval_eval.py --k 1 --k 3 --k 5 --k 10

No model calls and no API quota: this runs the real embedder on the CPU and takes about a minute
for the in-house suite. Results go to `evals/results/<timestamp>-retrieval.json`; the table is
printed as Markdown too.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from coder_agent.config import settings
from coder_agent.evals import load_suites
from coder_agent.evals.retrieval import DEFAULT_KS, evaluate
from coder_agent.rag.retriever import MODES

console = Console()
app = typer.Typer(add_completion=False)


@app.command()
def main(
    suite: Annotated[str, typer.Option(help="inhouse | humaneval | all")] = "inhouse",
    mode: Annotated[list[str] | None, typer.Option(help="hybrid, dense, bm25 (repeatable).")] = None,
    k: Annotated[list[int] | None, typer.Option(help="Cut-offs for recall@k (repeatable).")] = None,
    label: Annotated[str, typer.Option(help="Suffix for the results file.")] = "retrieval",
) -> None:
    modes = tuple(mode) if mode else MODES
    unknown = [m for m in modes if m not in MODES]
    if unknown:
        console.print(f"[red]Unknown mode(s):[/red] {unknown}; choose from {', '.join(MODES)}")
        raise typer.Exit(code=2)
    ks = tuple(sorted(k)) if k else DEFAULT_KS

    tasks = load_suites([suite])
    console.print(
        f"[bold]Retrieval eval[/bold] · {len(tasks)} tasks · modes {', '.join(modes)} · "
        f"[dim]model={settings.embedding_model}[/dim]"
    )
    progress = Progress(
        TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeElapsedColumn(),
        console=console, transient=True,
    )
    with progress:
        bar = progress.add_task("indexing and querying", total=len(tasks))
        report = evaluate(
            tasks, modes, ks=ks,  # type: ignore[arg-type]
            progress=lambda task, i, n: progress.update(bar, completed=i, total=n),
        )
    report.meta = {"suite": suite, "embedding_model": settings.embedding_model}

    table = Table(title=f"Recall of gold files · {suite}", show_edge=False)
    table.add_column("mode")
    table.add_column("tasks", justify="right")
    for kk in ks:
        table.add_column(f"recall@{kk}", justify="right")
    table.add_column("MRR", justify="right")
    for row in report.rows():
        table.add_row(
            row["mode"], str(row["tasks"]),
            *[f"{row[f'recall@{kk}']:.2f}" for kk in ks], f"{row['mrr']:.2f}",
        )
    console.print(table)
    path = report.write(label=label)
    console.print(f"\n[dim]written {path}[/dim]\n")
    console.print(report.markdown_table())


if __name__ == "__main__":
    app()
