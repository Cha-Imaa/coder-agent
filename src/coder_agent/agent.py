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
from coder_agent.telemetry.events import ProviderEvents

# Called once per node update with (node_name, patch). The CLI renders; the eval runner ignores.
UpdateHook = Callable[[str, dict[str, Any]], None]
# Called when the graph interrupts before a risky tool call, with the interrupt payload
# (`approval.Payload`). Returns True to run the calls, False or a reason string to reject them.
# `None` means nobody is watching: the gate is not built and pending interrupts are approved.
ApproveHook = Callable[[dict[str, Any]], bool | str]

# Keys whose final value the ledger needs. Folded while streaming so the run needs no second
# pass over the state to read them back.
_TRACKED = (
    "status", "iteration", "steps", "tests_passed", "test_command", "summary", "retrieved",
)


class UnknownThread(LookupError):
    """`--resume` named a thread this repo has no checkpoint for."""


class ThreadInProgress(RuntimeError):
    """A new task was given to a thread whose last run has not finished; resume it first."""


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


async def _build(repo: Path, saver: Any, require_approval: bool, github: bool = False):
    from coder_agent.graph import build_graph
    from coder_agent.graph.retrieval import default_retriever
    from coder_agent.llm import get_llm
    from coder_agent.tools.client import load_tools

    tools = await load_tools(repo, github=github)
    return build_graph(
        get_llm(), tools, checkpointer=saver, retriever=default_retriever(),
        require_approval=require_approval,
    )


async def run_agent(
    repo: Path,
    task: str,
    *,
    thread_id: str | None = None,
    test_cmd: str | None = None,
    on_update: UpdateHook | None = None,
    approve: ApproveHook | None = None,
    tags: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
    github: bool = False,
) -> RunOutcome:
    """Run the graph on `repo` for `task`, record the run in the ledger, return the outcome.

    `github=True` also starts the GitHub tool server so the model can re-read the issue a task
    came from; `coder fix-issue` sets it, plain runs do not pay for a second process.

    `tags` are stored alongside the run so eval runs can be told apart from interactive ones
    (`{"suite": ..., "task_id": ...}`) when the figures are drawn from the ledger later.

    A `thread_id` that already finished a run makes this a follow-up turn: the new task is
    appended to the same conversation, the loop counters start over, and the model sees what it
    did before. That is what `coder chat` is built on. A thread that is still mid-run (crashed
    or waiting at an approval prompt) raises `ThreadInProgress`; it wants `resume_agent`.
    """
    thread_id = thread_id or new_thread_id()
    state: dict[str, Any] = {"task": task, "repo": str(repo), "turn": 1}
    if test_cmd:
        state["test_command"] = test_cmd
    tags = {**(tags or {}), "thread_id": thread_id}

    async with _open_saver(repo) as saver:
        graph = await _build(repo, saver, require_approval=approve is not None, github=github)
        seed = state
        if await saver.aget_tuple(_config(thread_id)) is not None:
            snapshot = await graph.aget_state(_config(thread_id))
            if snapshot.next:
                raise ThreadInProgress(thread_id)
            turn = int(snapshot.values.get("turn", 1)) + 1
            state = _new_turn(task, test_cmd, turn)
            # The checkpoint overlaid with this turn's inputs, minus usage: the ledger record
            # should name the new task and cost this turn only, while the graph state keeps
            # accumulating across turns for the thread total.
            seed = {k: v for k, v in snapshot.values.items() if k != "usage"} | state
            tags["turn"] = turn
        return await _drive(
            graph, thread_id, state, seed=seed, on_update=on_update, approve=approve,
            tags=tags, ledger_path=ledger_path,
        )


def _new_turn(task: str, test_cmd: str | None, turn: int) -> dict[str, Any]:
    """The state patch that starts another task on a finished thread.

    `messages` is appended by its reducer, so the conversation keeps the earlier turns; the loop
    counters and verdict fields are plain values and must be reset explicitly or the second turn
    would inherit `iteration == max_iterations` and never retry. The human turn is seeded here
    because `plan` only does so for an empty conversation.
    """
    from langchain_core.messages import HumanMessage

    patch: dict[str, Any] = {
        "task": task,
        "turn": turn,
        "messages": [HumanMessage(task)],
        "plan": "",
        "iteration": 0,
        "steps": 0,
        "status": "running",
        "tests_passed": None,
        "test_output": "",
        "summary": "",
    }
    if test_cmd:
        patch["test_command"] = test_cmd
    return patch


async def resume_agent(
    repo: Path,
    thread_id: str,
    *,
    on_update: UpdateHook | None = None,
    approve: ApproveHook | None = None,
    tags: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
) -> RunOutcome:
    """Continue an interrupted run from its last checkpoint.

    Passing `None` as the input tells LangGraph to take the saved state and run whatever nodes
    were scheduled next, so the node that failed (typically `act` on a 429) is retried with the
    conversation intact. A thread whose graph already reached END is returned as-is. A thread
    that stopped at an approval prompt asks again (with `approve=None` it is waved through).
    """
    async with _open_saver(repo) as saver:
        if await saver.aget_tuple(_config(thread_id)) is None:
            raise UnknownThread(thread_id)
        graph = await _build(repo, saver, require_approval=approve is not None)
        snapshot = await graph.aget_state(_config(thread_id))
        values = dict(snapshot.values)
        if not snapshot.next:
            record = RunRecord.from_state(values, model=settings.model, wall_seconds=0.0)
            return RunOutcome(final=values, record=record, thread_id=thread_id, ran=False)
        return await _drive(
            graph, thread_id, None, seed=values, on_update=on_update, approve=approve,
            tags={**(tags or {}), "thread_id": thread_id, "resumed": True},
            ledger_path=ledger_path,
        )


async def _drive(
    graph: Any,
    thread_id: str,
    graph_input: Any,
    *,
    seed: dict[str, Any],
    on_update: UpdateHook | None,
    approve: ApproveHook | None,
    tags: dict[str, Any],
    ledger_path: Path | None,
) -> RunOutcome:
    """Stream the graph, fold the ledger values, write one record. `seed` is the state already
    known before streaming: the inputs for a fresh run, the checkpoint for a resumed one, so the
    record covers the whole thread and not only the part that ran now.

    An interrupt ends the stream early with an `__interrupt__` event. The decision is asked for,
    and the stream is restarted with `Command(resume=decision)` until the graph reaches END."""
    from langgraph.types import Command

    started = time.time()
    final: dict[str, Any] = {k: seed[k] for k in _TRACKED if k in seed}
    if seed.get("usage"):
        final["usage"] = merge_usage(None, seed["usage"])
    # Callbacks in the run config reach every node's model call: this is where failed attempts
    # (retried or handed to the fallback) get counted for the ledger.
    events = ProviderEvents()
    config = {**_config(thread_id), "callbacks": [events]}

    def make_record(status: str | None = None, **extra_tags: Any) -> RunRecord:
        state = {**seed, **final}
        if status:
            state["status"] = status
        return RunRecord.from_state(
            state, model=settings.model, wall_seconds=time.time() - started,
            tags={**tags, **extra_tags}, provider_errors=events.errors,
        )

    try:
        while True:
            pending: list[Any] = []
            async for update in graph.astream(graph_input, config, stream_mode="updates"):
                for node, patch in update.items():
                    if node == "__interrupt__":
                        pending.extend(patch)
                        continue
                    patch = patch or {}  # a node that changes nothing (approve) streams as None
                    if on_update is not None:
                        on_update(node, patch)
                    for key in _TRACKED:
                        if key in patch:
                            final[key] = patch[key]
                    if "usage" in patch:
                        final["usage"] = merge_usage(final.get("usage"), patch["usage"])
            if not pending:
                break
            decision = approve(pending[0].value) if approve is not None else True
            graph_input = Command(resume=decision)
    except Exception as exc:
        # A run that dies still cost tokens and still says something about the providers, so
        # it gets a ledger row with status "error" and the exception type before propagating.
        # The record rides along on the exception: `with_fallbacks` re-raises only the primary's
        # error, and the record's provider_errors is where the fallback's failure shows up.
        record = make_record("error", error=type(exc).__name__)
        Ledger(ledger_path or settings.ledger_path).record(record)
        exc.run_record = record  # type: ignore[attr-defined]
        raise

    record = make_record()
    Ledger(ledger_path or settings.ledger_path).record(record)
    return RunOutcome(final=final, record=record, thread_id=thread_id)
