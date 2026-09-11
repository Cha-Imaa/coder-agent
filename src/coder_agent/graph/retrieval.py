"""The `retrieve_context` node: put the most relevant code in front of the planner.

Without retrieval the model starts every task blind and spends its first several tool calls on
`list_dir` and `search_code` just to find out where things are. Retrieval turns the task text into
a query, runs it over the repository index, and hands the planner the handful of chunks that
look most related, each labelled with its file and line range. The model still verifies with
`read_file` before editing; the point is to start the search from the right place.

The node runs once, between `prepare` and `plan`. Later iterations (after failing tests) reuse
the same context: what changed is the code the agent wrote, and that is already in the
conversation.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from coder_agent.config import settings
from coder_agent.graph.state import AgentState
from coder_agent.rag.index import Hit

Retriever = Callable[[Path, str], list[Hit]]
"""`retriever(repo, query) -> hits`. The graph does not care how the hits were found."""


def format_context(hits: list[Hit], max_chars: int | None = None) -> str:
    """Render hits as fenced blocks headed by `path:lines (symbol)`, within a character budget.

    The header format is the same one the tools print, so the model can copy a location straight
    into `read_file`. Hits are added in rank order until the budget is spent; a budget is needed
    because six chunks of a generated file can be 20 kB, and the plan prompt is not the place for
    that.
    """
    budget = max_chars or settings.retrieval_context_chars
    blocks: list[str] = []
    used = 0
    for hit in hits:
        lang = hit.chunk.language or ""
        block = f"### {hit.location}\n```{lang}\n{hit.chunk.text.rstrip()}\n```"
        if used + len(block) > budget:
            if not blocks:  # even the best hit is over budget: keep a truncated head of it
                blocks.append(block[: max(0, budget - 4)] + "\n```")
            break
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(blocks)


def repo_retriever(repo: Path, query: str) -> list[Hit]:
    """Default retriever: bring the repo's index up to date, then search it in the configured mode.

    Imported lazily so the graph can be built (and tested) without Chroma or the embedding model
    installed. The first call on a repository embeds everything; later calls only re-embed files
    whose hash changed, which for a repo the agent just edited is one or two.
    """
    from coder_agent.rag import HybridRetriever, RepoIndex, default_reranker

    index = RepoIndex(repo)
    index.update()
    retriever = HybridRetriever(
        index, settings.retrieval_mode, reranker=default_reranker()  # type: ignore[arg-type]
    )
    return retriever.search(query, k=settings.retrieval_k)


def default_retriever() -> Retriever | None:
    """`None` when retrieval is switched off, which is how the ablation's baseline runs."""
    return None if settings.retrieval_mode == "off" else repo_retriever


def make_retrieve_node(retriever: Retriever | None):
    """Node factory. With `retriever=None` the node is a no-op that leaves `context` empty."""

    def retrieve_context(state: AgentState) -> dict[str, Any]:
        if retriever is None:
            return {"context": "", "retrieved": []}
        hits = retriever(Path(state["repo"]), state["task"])
        return {"context": format_context(hits), "retrieved": [h.location for h in hits]}

    return retrieve_context
