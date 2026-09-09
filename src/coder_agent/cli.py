"""Command-line entry point: `coder run <repo> "<task>"` and `coder stats`.

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
    from coder_agent.graph import build_graph
    from coder_agent.llm import get_llm
    from coder_agent.telemetry import Ledger, RunRecord, merge_usage
    from coder_agent.tools.client import load_tools
    from coder_agent.ui.render import Renderer

    renderer = Renderer(console, verbose=verbose)
    renderer.header(str(repo), task, settings.model)

    tools = await load_tools(repo)
    graph = build_graph(get_llm(), tools)

    state: dict = {"task": task, "repo": str(repo)}
    if test_cmd:
        state["test_command"] = test_cmd

    started = time.time()
    final: dict = {}
    # `updates` streams one patch per node for the UI. The ledger needs the final values of a few
    # keys, so fold them as they arrive instead of paying for a second pass over the state.
    async for update in graph.astream(state, stream_mode="updates"):
        for node, patch in update.items():
            renderer.update(node, patch)
            for key in ("status", "iteration", "steps", "tests_passed", "test_command"):
                if key in patch:
                    final[key] = patch[key]
            if "usage" in patch:
                final["usage"] = merge_usage(final.get("usage"), patch["usage"])

    record = RunRecord.from_state(
        {**state, **final}, model=settings.model, wall_seconds=time.time() - started
    )
    Ledger(settings.ledger_path).record(record)
    console.print(f"[dim]{record.footer()}[/dim]")
    return final.get("status", "failed")


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
