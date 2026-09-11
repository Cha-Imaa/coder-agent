"""Command-line entry point: `coder run <repo> "<task>"`, `coder run <repo> --resume <thread>`,
`coder chat <repo>`, `coder fix-issue <url>`, `coder index <repo>`, `coder stats`.

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


def _configure_sandbox(mode: str | None) -> None:
    """Apply `--sandbox`, verify Docker is usable before any token is spent, and say what runs where.

    Exits with a plain reason when the daemon is down or the image will not build; a missing
    Docker should never surface as a failed tool call halfway through a run.
    """
    from coder_agent.sandbox import SandboxError, configure, docker_limits

    try:
        configure(mode)
    except SandboxError as exc:  # includes DockerUnavailable
        console.print(f"[red]Sandbox:[/red] {exc}")
        raise typer.Exit(code=2) from None
    if settings.sandbox_mode == "docker":
        limits = docker_limits()
        console.print(
            f"[dim]Sandbox · docker · image={limits.image} network={limits.network} "
            f"memory={limits.memory} cpus={limits.cpus}[/dim]"
        )


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
    task: Annotated[
        str | None, typer.Argument(help="What to do, in plain English. Omit with --resume.")
    ] = None,
    model: Annotated[str | None, typer.Option(help="provider:model, overrides CODER_MODEL.")] = None,
    max_iterations: Annotated[int | None, typer.Option(help="Plan/act/test cycles.")] = None,
    test_cmd: Annotated[
        str | None, typer.Option(help="Command that runs the tests; auto-detected if omitted.")
    ] = None,
    resume: Annotated[
        str | None,
        typer.Option("--resume", metavar="THREAD", help="Continue an interrupted run by thread id."),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask before file edits and shell commands.")
    ] = False,
    sandbox: Annotated[
        str | None,
        typer.Option(help="Where commands run: local (host, denylist) or docker (container)."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show full tool output.")] = False,
) -> None:
    """Plan a change, edit files with tools, run the tests, iterate.

    Before every file edit, file write or shell command the run pauses and shows what the agent
    wants to do; answer y, n, or type a reason to reject and steer it. `--yes` skips the prompt.
    With `--sandbox docker` every command, including the test run, executes in a throwaway
    container with no network and only the repo mounted.
    """
    repo = repo.resolve()
    if not repo.is_dir():
        console.print(f"[red]Not a directory:[/red] {repo}")
        raise typer.Exit(code=2)
    _configure_sandbox(sandbox)
    if (task is None) == (resume is None):
        console.print("[red]Give a task to start a run, or --resume THREAD to continue one.[/red]")
        raise typer.Exit(code=2)
    if model:
        settings.model = model
    if max_iterations is not None:
        settings.max_iterations = max_iterations

    from coder_agent.agent import new_thread_id

    thread = resume or new_thread_id()
    try:
        code = asyncio.run(_run(repo, task, verbose, test_cmd, resume, thread, yes))
    except KeyboardInterrupt:
        # Ctrl+C cancels the event loop, not the coroutine, so the hint is printed here. The
        # checkpoint of the last finished node is already on disk.
        console.print()
        console.print(f'[dim]Interrupted. Resume with:[/dim] coder run "{repo}" --resume {thread}')
        code = 130
    raise typer.Exit(code=code)


async def _run(
    repo: Path,
    task: str | None,
    verbose: bool,
    test_cmd: str | None,
    resume: str | None,
    thread: str,
    yes: bool,
) -> int:
    # Imports here so `coder --version` stays fast and does not need provider packages.
    from coder_agent.agent import UnknownThread, resume_agent, run_agent
    from coder_agent.ui.render import Renderer

    renderer = Renderer(console, verbose=verbose)
    renderer.header(str(repo), task or f"resume thread {thread}", settings.model, thread=thread)
    approve = None if yes else renderer.ask_approval

    try:
        if resume:
            outcome = await resume_agent(
                repo, resume, on_update=renderer.update, approve=approve
            )
        else:
            outcome = await run_agent(
                repo, task or "", thread_id=thread, test_cmd=test_cmd,
                on_update=renderer.update, approve=approve,
            )
    except UnknownThread:
        console.print(f"[red]No checkpoint for thread[/red] {resume} in {repo}")
        return 2
    except Exception as exc:  # noqa: BLE001 - the whole point is to tell the user how to go on
        console.print(f"[red]Run stopped:[/red] {type(exc).__name__}: {exc}")
        _print_failed_record(exc)
        console.print(f'[dim]Resume with:[/dim] coder run "{repo}" --resume {thread}')
        return 1

    if not outcome.ran:
        console.print(
            f"[yellow]Thread {thread} already finished[/yellow] with status {outcome.status}."
        )
    else:
        console.print(f"[dim]{outcome.record.footer()} · thread {thread}[/dim]")
    return 0 if outcome.status == "passed" else 1


@app.command()
def chat(
    repo: Annotated[Path, typer.Argument(help="Path to the repository to work in.")] = Path("."),
    thread: Annotated[
        str | None,
        typer.Option("--thread", metavar="THREAD", help="Continue an earlier chat by thread id."),
    ] = None,
    model: Annotated[str | None, typer.Option(help="provider:model, overrides CODER_MODEL.")] = None,
    max_iterations: Annotated[int | None, typer.Option(help="Plan/act/test cycles per turn.")] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask before file edits and shell commands.")
    ] = False,
    sandbox: Annotated[
        str | None,
        typer.Option(help="Where commands run: local (host, denylist) or docker (container)."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show full tool output.")] = False,
) -> None:
    """Multi-turn session: every message is a task run on one thread, so the agent remembers.

    Each message goes through the full plan/act/test loop; the next message sees the whole
    history. `/resume` continues a turn that was interrupted, `/quit` leaves. The thread id is
    printed at the start; `--thread` reopens it later, even after a crash.
    """
    repo = repo.resolve()
    if not repo.is_dir():
        console.print(f"[red]Not a directory:[/red] {repo}")
        raise typer.Exit(code=2)
    _configure_sandbox(sandbox)
    if model:
        settings.model = model
    if max_iterations is not None:
        settings.max_iterations = max_iterations

    from coder_agent.agent import new_thread_id

    thread = thread or new_thread_id()
    try:
        code = asyncio.run(_chat(repo, thread, yes, verbose))
    except KeyboardInterrupt:
        console.print()
        console.print(
            f'[dim]Interrupted. Come back with:[/dim] coder chat "{repo}" --thread {thread}'
            "  [dim]then type /resume[/dim]"
        )
        code = 130
    raise typer.Exit(code=code)


async def _chat(repo: Path, thread: str, yes: bool, verbose: bool) -> int:
    from rich.panel import Panel
    from rich.prompt import Prompt

    from coder_agent.agent import ThreadInProgress, UnknownThread, resume_agent, run_agent
    from coder_agent.ui.render import Renderer

    renderer = Renderer(console, verbose=verbose)
    approve = None if yes else renderer.ask_approval
    console.print(Panel(
        f"thread [bold]{thread}[/bold] · /resume continues an interrupted turn · /quit leaves\n"
        f"[dim]reopen later with: coder chat \"{repo}\" --thread {thread}[/dim]",
        title=f"coder chat · {settings.model}", subtitle=str(repo), border_style="cyan",
    ))

    while True:
        try:
            text = Prompt.ask("[bold cyan]you[/bold cyan]", console=console).strip()
        except EOFError:
            return 0
        if not text:
            continue
        if text in ("/quit", "/exit", "/q"):
            return 0
        try:
            if text == "/resume":
                outcome = await resume_agent(repo, thread, on_update=renderer.update, approve=approve)
            else:
                outcome = await run_agent(
                    repo, text, thread_id=thread, on_update=renderer.update, approve=approve
                )
        except ThreadInProgress:
            console.print("[yellow]The last turn did not finish; type /resume to continue it.[/yellow]")
            continue
        except UnknownThread:
            console.print("[yellow]Nothing to resume yet; give the agent a task first.[/yellow]")
            continue
        except Exception as exc:  # noqa: BLE001 - keep the session alive, the checkpoint is safe
            console.print(f"[red]Turn stopped:[/red] {type(exc).__name__}: {exc}")
            _print_failed_record(exc)
            console.print("[dim]Type /resume to continue it, or give a new task.[/dim]")
            continue
        if outcome.ran:
            console.print(f"[dim]{outcome.record.footer()}[/dim]")


@app.command(name="fix-issue")
def fix_issue(
    url: Annotated[str, typer.Argument(help="GitHub issue URL, or OWNER/REPO#N.")],
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Existing clone to work in; cloned under ~/.coder-agent otherwise."),
    ] = None,
    pr: Annotated[
        bool, typer.Option("--pr", help="After the tests pass: commit, push and open a pull request.")
    ] = False,
    base: Annotated[
        str | None, typer.Option("--base", help="Base branch for the pull request (default branch).")
    ] = None,
    model: Annotated[str | None, typer.Option(help="provider:model, overrides CODER_MODEL.")] = None,
    max_iterations: Annotated[int | None, typer.Option(help="Plan/act/test cycles.")] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask before file edits and shell commands.")
    ] = False,
    sandbox: Annotated[
        str | None,
        typer.Option(help="Where commands run: local (host, denylist) or docker (container)."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show full tool output.")] = False,
) -> None:
    """Fix a GitHub issue: read it, branch, run the agent, and with --pr open the pull request.

    The issue text becomes the task. Work happens on a `coder/issue-N` branch of the clone (a
    fresh one under ~/.coder-agent/checkouts unless --repo names yours). Without --pr the edits
    are left uncommitted on that branch for review. Reads of public repositories need no token;
    --pr needs GITHUB_TOKEN with permission to push and open pull requests.
    """
    _configure_sandbox(sandbox)
    if model:
        settings.model = model
    if max_iterations is not None:
        settings.max_iterations = max_iterations
    try:
        code = asyncio.run(_fix_issue(url, repo, pr, base, yes, verbose))
    except KeyboardInterrupt:
        console.print()
        console.print("[dim]Interrupted. The clone keeps the branch; rerun the same command.[/dim]")
        code = 130
    raise typer.Exit(code=code)


async def _fix_issue(
    url: str, repo: Path | None, pr: bool, base: str | None, yes: bool, verbose: bool
) -> int:
    from coder_agent.fix_issue import FixIssueError, fix_issue, parse_issue_url
    from coder_agent.mcp_server.github import GitHubAPI, GitHubError
    from coder_agent.ui.render import Renderer

    renderer = Renderer(console, verbose=verbose)
    api = GitHubAPI.from_env()
    if pr and not api.authenticated:
        console.print("[red]--pr needs GITHUB_TOKEN in the environment[/red] (push and open the PR).")
        return 2

    def stage(name: str, detail: str) -> None:
        labels = {
            "issue": "Reading issue", "checkout": "Checkout", "agent": "Running the agent on branch",
            "commit": "Committing", "push": "Pushing", "pull-request": "Opening pull request",
        }
        console.print(f"[bold cyan]{labels.get(name, name)}[/bold cyan] · {detail}")

    try:
        ref = parse_issue_url(url)
        renderer.header(ref.url, f"fix issue #{ref.number}", settings.model)
        result = await fix_issue(
            url, repo_path=repo, open_pr=pr, base=base, api=api, on_stage=stage,
            on_update=renderer.update, approve=None if yes else renderer.ask_approval,
        )
    except (FixIssueError, GitHubError) as exc:
        console.print(f"[red]fix-issue stopped:[/red] {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - the branch and checkpoint survive; say how to go on
        console.print(f"[red]Run stopped:[/red] {type(exc).__name__}: {exc}")
        _print_failed_record(exc)
        return 1

    console.print(f"[dim]{result.outcome.record.footer()} · thread {result.outcome.thread_id}[/dim]")
    if not result.passed:
        console.print(
            f"[yellow]Tests did not pass[/yellow] (status {result.outcome.status}). "
            f"The attempt is on branch {result.branch} in {result.repo}."
        )
        return 1
    if result.pull_request:
        console.print(f"[green]Pull request opened:[/green] {result.pull_request['url']}")
        return 0
    console.print(
        f"[green]Tests pass.[/green] Changes are uncommitted on branch {result.branch} in "
        f"{result.repo}. [dim]Review them, then rerun with --pr to commit, push and open the "
        "pull request.[/dim]"
    )
    return 0


def _print_failed_record(exc: BaseException) -> None:
    """The ledger record of a run that died, if the agent attached one: it names every failed
    provider attempt, including the fallback's, which the exception itself does not."""
    record = getattr(exc, "run_record", None)
    if record is not None:
        console.print(f"[dim]{record.footer()}[/dim]")


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
    mode: Annotated[
        str, typer.Option("--mode", help="Retrieval mode for --query: hybrid, dense or bm25.")
    ] = settings.retrieval_mode,
) -> None:
    """Build or refresh the local vector index of a repository (incremental by file hash)."""
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

    from coder_agent.rag import MODES, HybridRetriever, RepoIndex

    repo = repo.resolve()
    if not repo.is_dir():
        console.print(f"[red]Not a directory:[/red] {repo}")
        raise typer.Exit(code=2)
    if mode not in MODES:
        console.print(f"[red]Unknown mode:[/red] {mode} (expected one of {', '.join(MODES)})")
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
        hits = HybridRetriever(store, mode=mode).search(query, k=k)  # type: ignore[arg-type]
        table = Table(title=f"Top {len(hits)} for: {query}  [dim]({mode})[/dim]", show_edge=False)
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
    errors = summary["provider_errors"]
    failed = ", ".join(f"{n} {name}" for name, n in sorted(errors.items())) or "none"
    console.print(
        f"[dim]providers · fallback answered in {summary['fallback_runs']} run(s) · "
        f"failed attempts: {failed}[/dim]"
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
