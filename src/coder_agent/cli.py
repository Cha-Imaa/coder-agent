"""Command-line entry point: `coder run <repo> "<task>"`.

The CLI is deliberately thin. It resolves the repo, loads the MCP tools, builds the graph, and
streams node updates to the renderer. All behaviour lives in the graph so the same code path is
used by the eval runner and, later, by `coder chat`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

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
    from coder_agent.tools.client import load_tools
    from coder_agent.ui.render import Renderer

    renderer = Renderer(console, verbose=verbose)
    renderer.header(str(repo), task, settings.model)

    tools = await load_tools(repo)
    graph = build_graph(get_llm(), tools)

    final_status = "failed"
    state: dict = {"task": task, "repo": str(repo)}
    if test_cmd:
        state["test_command"] = test_cmd
    async for update in graph.astream(state, stream_mode="updates"):
        for node, patch in update.items():
            renderer.update(node, patch)
            if node == "finish":
                final_status = patch.get("status", final_status)
    return final_status


if __name__ == "__main__":
    app()
