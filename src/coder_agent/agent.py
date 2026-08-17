"""One entry point for a full agent run, shared by the CLI and the eval runner.

Both callers need the same sequence: load the MCP tools for the repo, build the graph, stream
it, fold the values the ledger needs, and write one `RunRecord`. Keeping that in a single
function means a benchmark number is produced by exactly the code path a user runs, so the
pass rate in the README describes the tool people actually get.

Every run is checkpointed. LangGraph saves the state after each node into a SQLite file under
the repo's `.coder-agent/` directory, keyed by a thread id. A run that dies on a rate limit or a
crash therefore leaves its plan, its edits-so-far and the conversation behind, and
`resume_agent` picks up at the node that did not finish instead of starting over.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coder_agent.config import settings
from coder_agent.telemetry import Ledger, RunRecord, merge_usage

# Called once per node update with (node_name, patch). The CLI renders; the eval runner ignores.
UpdateHook = Callable[[str, dict[str, Any]], None]

# Keys whose final value the ledger needs. Folded while streaming so the run needs no second
# pass over the state to read them back.
_TRACKED = (
    "status", "iteration", "steps", "tests_passed", "test_command", "summary", "retrieved",
)


class UnknownThread(LookupError):
    """`--resume` named a thread this repo has no checkpoint for."""


@dataclass
class RunOutcome:
    final: dict[str, Any]
    record: RunRecord
    thread_id: str
    ran: bool = True  # False when a resumed thread had already finished; nothing was executed

    @property
    def status(self) -> str:
        return str(self.final.get("status", "failed"))


def new_thread_id() -> str:
    return uuid.uuid4().hex[:8]


def checkpoint_path(repo: Path) -> Path:
    return settings.state_dir(repo) / "checkpoints.sqlite"


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


@asynccontextmanager
async def _open_saver(repo: Path) -> AsyncIterator[Any]:
    # Imported here so `coder --version` stays fast and does not need provider packages.
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = checkpoint_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        yield saver


async def _build(repo: Path, saver: Any):
    from coder_agent.graph import build_graph
    from coder_agent.graph.retrieval import default_retriever
    from coder_agent.llm import get_llm
    from coder_agent.tools.client import load_tools

    tools = await load_tools(repo)
    return build_graph(get_llm(), tools, checkpointer=saver, retriever=default_retriever())


async def run_agent(
    repo: Path,
    task: str,
    *,
    thread_id: str | None = None,
    test_cmd: str | None = None,
    on_update: UpdateHook | None = None,
    tags: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
) -> RunOutcome:
    """Run the graph on `repo` for `task`, record the run in the ledger, return the outcome.

    `tags` are stored alongside the run so eval runs can be told apart from interactive ones
    (`{"suite": ..., "task_id": ...}`) when the figures are drawn from the ledger later.
    """
    thread_id = thread_id or new_thread_id()
    state: dict[str, Any] = {"task": task, "repo": str(repo)}
    if test_cmd:
        state["test_command"] = test_cmd

    async with _open_saver(repo) as saver:
        graph = await _build(repo, saver)
        return await _drive(
            graph, thread_id, state, seed=state, on_update=on_update,
            tags={**(tags or {}), "thread_id": thread_id}, ledger_path=ledger_path,
        )


async def resume_agent(
    repo: Path,
    thread_id: str,
    *,
    on_update: UpdateHook | None = None,
    tags: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
) -> RunOutcome:
    """Continue an interrupted run from its last checkpoint.

    Passing `None` as the input tells LangGraph to take the saved state and run whatever nodes
    were scheduled next, so the node that failed (typically `act` on a 429) is retried with the
    conversation intact. A thread whose graph already reached END is returned as-is.
    """
    async with _open_saver(repo) as saver:
        if await saver.aget_tuple(_config(thread_id)) is None:
            raise UnknownThread(thread_id)
        graph = await _build(repo, saver)
        snapshot = await graph.aget_state(_config(thread_id))
        values = dict(snapshot.values)
        if not snapshot.next:
            record = RunRecord.from_state(values, model=settings.model, wall_seconds=0.0)
            return RunOutcome(final=values, record=record, thread_id=thread_id, ran=False)
        return await _drive(
            graph, thread_id, None, seed=values, on_update=on_update,
            tags={**(tags or {}), "thread_id": thread_id, "resumed": True},
            ledger_path=ledger_path,
        )


async def _drive(
    graph: Any,
    thread_id: str,
    graph_input: dict[str, Any] | None,
    *,
    seed: dict[str, Any],
    on_update: UpdateHook | None,
    tags: dict[str, Any],
    ledger_path: Path | None,
) -> RunOutcome:
    """Stream the graph, fold the ledger values, write one record. `seed` is the state already
    known before streaming: the inputs for a fresh run, the checkpoint for a resumed one, so the
    record covers the whole thread and not only the part that ran now."""
    started = time.time()
    final: dict[str, Any] = {k: seed[k] for k in _TRACKED if k in seed}
    if seed.get("usage"):
        final["usage"] = merge_usage(None, seed["usage"])

    async for update in graph.astream(graph_input, _config(thread_id), stream_mode="updates"):
        for node, patch in update.items():
            if on_update is not None:
                on_update(node, patch)
            for key in _TRACKED:
                if key in patch:
                    final[key] = patch[key]
            if "usage" in patch:
                final["usage"] = merge_usage(final.get("usage"), patch["usage"])

    record = RunRecord.from_state(
        {**seed, **final}, model=settings.model, wall_seconds=time.time() - started, tags=tags
    )
    Ledger(ledger_path or settings.ledger_path).record(record)
    return RunOutcome(final=final, record=record, thread_id=thread_id)
