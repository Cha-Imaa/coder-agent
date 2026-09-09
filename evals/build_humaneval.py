"""Build the HumanEval slice under `evals/humaneval/` from the public dataset.

    uv run python evals/build_humaneval.py                 # download, select 30, validate, write
    uv run python evals/build_humaneval.py --data path/to/HumanEval.jsonl.gz --count 30

The output is committed, so day-to-day runs need neither network nor this script; rerun it only
to change the slice size. Selection is deterministic (dataset order, first `count` problems that
have docstring examples and pass the well-posedness check), so rebuilding yields the same tasks.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from coder_agent.evals.humaneval import (
    DEFAULT_COUNT,
    HUMANEVAL_DIR,
    Problem,
    build_slice,
    download,
    load_problems,
)

console = Console()
app = typer.Typer(add_completion=False)


@app.command()
def main(
    data: Annotated[Path | None, typer.Option(help="Local HumanEval.jsonl(.gz); else download.")] = None,
    count: Annotated[int, typer.Option(help="How many problems to keep.")] = DEFAULT_COUNT,
    out: Annotated[Path, typer.Option(help="Where the task directories go.")] = HUMANEVAL_DIR,
) -> None:
    if data is None:
        data = download(Path(tempfile.gettempdir()) / "HumanEval.jsonl.gz")
        console.print(f"[dim]downloaded to {data}[/dim]")
    problems = load_problems(data)
    console.print(f"{len(problems)} problems in the dataset; selecting {count}")

    def report(problem: Problem, ok: bool) -> None:
        mark = "[green]keep[/green]" if ok else "[yellow]skip[/yellow]"
        console.print(f"  {mark} HumanEval/{problem.number:<4} {problem.entry_point}")

    kept = build_slice(problems, out, count=count, on_decision=report)
    console.print(f"[bold]{len(kept)} tasks written to {out}[/bold]")


if __name__ == "__main__":
    app()
