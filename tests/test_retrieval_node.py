"""retrieve_context node: what the planner and the actor are shown, and when retrieval runs."""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import SystemMessage

from coder_agent.config import settings
from coder_agent.graph import build_graph
from coder_agent.graph.retrieval import format_context, make_retrieve_node
from coder_agent.rag.chunker import Chunk
from coder_agent.rag.index import Hit
from tests.fakes import echo, scripted


def _hit(path: str, text: str, symbol: str | None = None, start: int = 1) -> Hit:
    lines = text.count("\n") + 1
    chunk = Chunk(
        path=path,
        language="python",
        start_line=start,
        end_line=start + lines - 1,
        text=text,
        kind="function",
        symbol=symbol,
    )
    return Hit(chunk=chunk, score=0.9)


HITS = [
    _hit("src/ledger.py", "def write_ledger(row):\n    pass", "write_ledger", start=10),
    _hit("src/parser.py", "def parse_config(text):\n    pass", "parse_config"),
]


class FakeRetriever:
    def __init__(self, hits: list[Hit]) -> None:
        self.hits = hits
        self.calls: list[tuple[Path, str]] = []

    def __call__(self, repo: Path, query: str) -> list[Hit]:
        self.calls.append((repo, query))
        return self.hits


# -- formatting -------------------------------------------------------------------------------


def test_format_context_labels_each_chunk_with_its_location() -> None:
    text = format_context(HITS, max_chars=10_000)
    assert "### src/ledger.py:10-11 (write_ledger)" in text
    assert "```python\ndef write_ledger(row):" in text
    assert text.index("ledger.py") < text.index("parser.py")


def test_format_context_stops_at_the_character_budget() -> None:
    one_block = len(format_context(HITS[:1], max_chars=10_000))
    text = format_context(HITS, max_chars=one_block + 5)
    assert "ledger.py" in text and "parser.py" not in text


def test_format_context_truncates_a_single_oversized_hit_instead_of_dropping_it() -> None:
    text = format_context(HITS[:1], max_chars=40)
    assert text.startswith("### src/ledger.py") and len(text) <= 44


# -- node -------------------------------------------------------------------------------------


def test_node_without_retriever_leaves_context_empty() -> None:
    node = make_retrieve_node(None)
    assert node({"task": "t", "repo": "/none"}) == {"context": "", "retrieved": []}


def test_node_queries_with_the_task_and_records_locations() -> None:
    retriever = FakeRetriever(HITS)
    patch = make_retrieve_node(retriever)({"task": "fix the ledger", "repo": "/some/repo"})
    assert retriever.calls == [(Path("/some/repo"), "fix the ledger")]
    assert patch["retrieved"] == ["src/ledger.py:10-11 (write_ledger)", "src/parser.py:1-2 (parse_config)"]
    assert "def parse_config" in patch["context"]


# -- in the graph -------------------------------------------------------------------------------


def test_planner_and_actor_both_see_the_retrieved_code() -> None:
    llm = scripted("1. edit ledger", "done")
    retriever = FakeRetriever(HITS)
    graph = build_graph(llm, [echo], retriever=retriever)
    graph.invoke({"task": "fix the ledger", "repo": "/some/repo", "test_command": None})

    plan_prompt = llm.calls[0]
    assert "def write_ledger" in plan_prompt[-1].content  # human turn of the plan call
    act_system = next(m for m in llm.calls[1] if isinstance(m, SystemMessage))
    assert "src/ledger.py:10-11" in act_system.content
    assert "read a file before you change it" in act_system.content


def test_graph_without_retriever_adds_no_context_section() -> None:
    llm = scripted("plan", "done")
    build_graph(llm, [echo]).invoke({"task": "t", "repo": "/none", "test_command": None})
    act_system = next(m for m in llm.calls[1] if isinstance(m, SystemMessage))
    assert "Relevant code retrieved" not in act_system.content
    assert "Relevant code retrieved" not in llm.calls[0][-1].content


def test_retrieval_runs_once_even_when_the_loop_iterates(monkeypatch, tmp_path: Path) -> None:
    # Two iterations: the first act reply is followed by a failing test run, then reflect -> plan.
    monkeypatch.setattr(settings, "max_iterations", 2)
    llm = scripted("plan 1", "tried", "plan 2", "done")
    retriever = FakeRetriever(HITS)
    graph = build_graph(llm, [echo], retriever=retriever)
    failing = "python -c \"import sys; sys.exit(1)\""
    out = graph.invoke({"task": "t", "repo": str(tmp_path), "test_command": failing})
    assert out["iteration"] == 2
    assert len(retriever.calls) == 1
    assert "def write_ledger" in llm.calls[2][-1].content  # second plan still sees the context
