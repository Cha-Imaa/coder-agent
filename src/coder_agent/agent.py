"""One entry point for a full agent run, shared by the CLI and the eval runner.

Both callers need the same sequence: load the MCP tools for the repo, build the graph, stream
it, fold the values the ledger needs, and write one `RunRecord`. Keeping that in a single
function means a benchmark number is produced by exactly the code path a user runs, so the
pass rate in the README describes the tool people actually get.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coder_agent.config import settings
from coder_agent.telemetry import Ledger, RunRecord, merge_usage

# Called once per node update with (node_name, patch). The CLI renders; the eval runner ignores.
UpdateHook = Callable[[str, dict[str, Any]], None]

# Keys whose final value the ledger needs. Folded while streaming so the run needs no second
# pass over the state (and no checkpointer) to read them back.
_TRACKED = ("status", "iteration", "steps", "tests_passed", "test_command", "summary")


@dataclass
class RunOutcome:
    final: dict[str, Any]
    record: RunRecord

    @property
    def status(self) -> str:
        return str(self.final.get("status", "failed"))


async def run_agent(
    repo: Path,
    task: str,
    *,
    test_cmd: str | None = None,
    on_update: UpdateHook | None = None,
    tags: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
) -> RunOutcome:
    """Run the graph on `repo` for `task`, record the run in the ledger, return the outcome.

    `tags` are stored alongside the run so eval runs can be told apart from interactive ones
    (`{"suite": ..., "task_id": ...}`) when the figures are drawn from the ledger later.
    """
    # Imports here so `coder --version` stays fast and does not need provider packages.
    from coder_agent.graph import build_graph
    from coder_agent.llm import get_llm
    from coder_agent.tools.client import load_tools

    tools = await load_tools(repo)
    graph = build_graph(get_llm(), tools)

    state: dict[str, Any] = {"task": task, "repo": str(repo)}
    if test_cmd:
        state["test_command"] = test_cmd

    started = time.time()
    final: dict[str, Any] = {}
    async for update in graph.astream(state, stream_mode="updates"):
        for node, patch in update.items():
            if on_update is not None:
                on_update(node, patch)
            for key in _TRACKED:
                if key in patch:
                    final[key] = patch[key]
            if "usage" in patch:
                final["usage"] = merge_usage(final.get("usage"), patch["usage"])

    record = RunRecord.from_state(
        {**state, **final}, model=settings.model, wall_seconds=time.time() - started, tags=tags
    )
    Ledger(ledger_path or settings.ledger_path).record(record)
    return RunOutcome(final=final, record=record)
