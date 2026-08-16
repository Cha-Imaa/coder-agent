"""Command-line entry point: `coder run <repo> "<task>"`, `coder index <repo>`, `coder stats`.

The CLI is deliberately thin. It resolves the repo, loads the MCP tools, builds the graph, and
streams node updates to the renderer. All behaviour lives in the graph so the same code path is
used by the eval runner and, later, by `coder chat`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from coder_agent import __version__
from coder_agent.config import settings

# langchain-google-genai warns once per message when it drops another provider's reasoning
# blocks during a fallback; correct behaviour, but noise in a terminal UI.
logging.getLogger("langchain_google_genai").setLevel(logging.ERROR)
logging.getLogger("google_genai").setLevel(logging.ERROR)

app = typer.Typer(no_args_is_help=True, add_completion=False, rich_markup_mode="rich")
console = Console()


def _version(value: bool) -> None:
    if value:
        console.print(f"coder-agent {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _root(
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show version.")
    ] = False,
) -> None:
    """A terminal coding agent: plan, edit, test, iterate."""


@app.command()
def run(
    repo: Annotated[Path, typer.Argument(help="Path to the repository to work in.")],
    task: Annotated[str, typer.Argument(help="What to do, in plain English.")],
    model: Annotated[str | None, typer.Option(help="provider:model, overrides CODER_MODEL.")] = None,
    max_iterations: Annotated[int | None, typer.Option(help="Plan/act/test cycles.")] = None,
    test_cmd: Annotated[
        str | None, typer.Option(help="Command that runs the tests; auto-detected if omitted.")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show full tool output.")] = False,
) -> None:
    """Plan a change, edit files with tools, run the tests, iterate."""
    repo = repo.resolve()
    if not repo.is_dir():
        console.print(f"[red]Not a directory:[/red] {repo}")
        raise typer.Exit(code=2)
    if model:
        settings.model = model
    if max_iterations is not None:
        settings.max_iterations = max_iterations

    status = asyncio.run(_run(repo, task, verbose, test_cmd))
    raise typer.Exit(code=0 if status == "passed" else 1)


async def _run(repo: Path, task: str, verbose: bool, test_cmd: str | None) -> str:
    # Imports here so `coder --version` stays fast and does not need provider packages.
    from coder_agent.agent import run_agent
    from coder_agent.ui.render import Renderer

    renderer = Renderer(console, verbose=verbose)
    renderer.header(str(repo), task, settings.model)

    outcome = await run_agent(repo, task, test_cmd=test_cmd, on_update=renderer.update)
    console.print(f"[dim]{outcome.record.footer()}[/dim]")
    return outcome.status


@app.command()
def index(
    repo: Annotated[Path, typer.Argument(help="Repository to index.")] = Path("."),
    rebuild: Annotated[
        bool, typer.Option("--rebuild", help="Drop the existing index and embed everything.")
    ] = False,
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="After indexing, show the top hits for this."),
    ] = None,
    k: Annotated[int, typer.Option(help="How many hits to show with --query.")] = 5,
) -> None:
    """Build or refresh the local vector index of a repository (incremental by file hash)."""
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

    from coder_agent.rag import RepoIndex

    repo = repo.resolve()
    if not repo.is_dir():
        console.print(f"[red]Not a directory:[/red] {repo}")
        raise typer.Exit(code=2)

    store = RepoIndex(repo)
    if rebuild:
        store.clear()
    console.print(
        f"[bold]Indexing[/bold] {repo}  [dim]model={settings.embedding_model} "
        f"store={store.index_dir.relative_to(repo)}[/dim]"
    )

    progress = Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )
    tasks: dict[str, int] = {}
    labels = {"chunk": "chunking files", "embed": "embedding chunks", "write": "writing vectors"}

    def report(phase: str, done: int, total: int) -> None:
        if phase not in tasks:
            tasks[phase] = progress.add_task(labels[phase], total=total)
        progress.update(tasks[phase], completed=done, total=total)

    with progress:
        stats = store.update(progress=report)
    console.print(f"[green]Done.[/green] {stats.summary()}")

    if query:
        hits = store.search(query, k=k)
        table = Table(title=f"Top {len(hits)} for: {query}", show_edge=False)
        table.add_column("score", justify="right")
        table.add_column("kind")
        table.add_column("location")
        for hit in hits:
            table.add_row(f"{hit.score:.3f}", hit.chunk.kind, hit.location)
        console.print(table)


@app.command()
def stats(
    limit: Annotated[int, typer.Option(help="How many recent runs to list.")] = 10,
) -> None:
    """Show recent runs and aggregate cost from the local ledger."""
    from coder_agent.telemetry import Ledger

    ledger = Ledger(settings.ledger_path)
    summary = ledger.summary()
    if not summary["runs"]:
        console.print(f"No runs recorded yet in {settings.ledger_path}")
        raise typer.Exit()

    console.print(
        f"[bold]{summary['runs']} runs[/bold] · pass rate {summary['pass_rate']:.0%} · "
        f"avg {summary['avg_tokens']:,.0f} tokens · avg {summary['avg_seconds']:.0f}s · "
        f"avg {summary['avg_iterations']:.1f} iterations"
    )

    per_node = Table(title="Tokens by node", show_edge=False)
    for col in ("node", "calls", "input", "output"):
        per_node.add_column(col, justify="left" if col == "node" else "right")
    for row in summary["per_node"]:
        per_node.add_row(
            row["node"], str(row["calls"]), f"{row['input_tokens']:,}", f"{row['output_tokens']:,}"
        )
    console.print(per_node)

    recent = Table(title=f"Last {limit} runs", show_edge=False)
    for col in ("when", "status", "iter", "tokens", "secs", "task"):
        recent.add_column(col)
    for r in ledger.recent(limit):
        color = {"passed": "green", "failed": "red", "gave_up": "yellow"}.get(r["status"], "white")
        recent.add_row(
            time.strftime("%m-%d %H:%M", time.localtime(r["started_at"])),
            f"[{color}]{r['status']}[/{color}]",
            str(r["iterations"]),
            f"{(r['input_tokens'] or 0) + (r['output_tokens'] or 0):,}",
            f"{r['wall_seconds']:.0f}",
            (r["task"] or "")[:60],
        )
    console.print(recent)


if __name__ == "__main__":
    app()
