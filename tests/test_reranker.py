"""Reranking: the second stage reorders the first stage's candidates and nothing else."""

from __future__ import annotations

from pathlib import Path

import pytest

from coder_agent.config import settings
from coder_agent.evals.retrieval import all_modes, split_mode
from coder_agent.rag import (
    HashEmbedder,
    HybridRetriever,
    OverlapReranker,
    RepoIndex,
    default_reranker,
)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def store(tmp_path: Path) -> RepoIndex:
    # Three chunks that all mention "duration"; only one is about minutes and hours.
    _write(tmp_path, "src/duration.py",
           "def parse_duration(text):\n    # minutes hours seconds duration parsing\n    return 0\n")
    _write(tmp_path, "src/format.py",
           "def format_duration(seconds):\n    # duration to string\n    return str(seconds)\n")
    _write(tmp_path, "src/util.py", "def helper(duration):\n    return duration\n")
    index = RepoIndex(tmp_path, HashEmbedder(), index_dir=tmp_path / ".coder-agent" / "chroma")
    index.update()
    return index


class RecordingReranker:
    """Scores by a fixed preference and remembers what it was asked, so tests can inspect it."""

    def __init__(self, favourite: str) -> None:
        self.favourite = favourite
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, texts: list[str]) -> list[float]:
        self.calls.append((query, list(texts)))
        return [1.0 if self.favourite in t else 0.0 for t in texts]


# -- the stand-in reranker ---------------------------------------------------------------------


def test_overlap_reranker_scores_shared_tokens() -> None:
    scores = OverlapReranker().score("parse minutes", ["def parse(): minutes", "unrelated", ""])
    assert scores == [1.0, 0.0, 0.0]
    assert OverlapReranker().score("", ["x"]) == [0.0]


# -- retriever integration ------------------------------------------------------------------------


def test_reranker_reorders_candidates_and_reports_its_scores(store: RepoIndex) -> None:
    reranker = RecordingReranker("helper")
    plain = HybridRetriever(store, mode="bm25").search("duration", k=3)
    assert plain[0].chunk.symbol != "helper", "the fixture only makes sense if bm25 ranks helper lower"

    hits = HybridRetriever(store, mode="bm25", reranker=reranker, rerank_candidates=10).search(
        "duration", k=3
    )
    assert hits[0].chunk.symbol == "helper" and hits[0].score == 1.0
    assert [h.chunk.id for h in hits[1:]] == [h.chunk.id for h in plain if h.chunk.symbol != "helper"]
    query, texts = reranker.calls[0]
    assert query == "duration" and len(texts) == 3
    assert all(t.startswith("src/") for t in texts), "the reranker sees the path header too"


def test_reranker_sees_more_candidates_than_k(store: RepoIndex) -> None:
    reranker = RecordingReranker("helper")
    hits = HybridRetriever(store, mode="dense", reranker=reranker, rerank_candidates=3).search(
        "parse_duration minutes", k=1
    )
    assert len(hits) == 1 and hits[0].chunk.symbol == "helper"
    assert len(reranker.calls[0][1]) == 3, "first stage widened to rerank_candidates, then cut to k"


def test_rerank_can_be_switched_off_per_call(store: RepoIndex) -> None:
    reranker = RecordingReranker("helper")
    retriever = HybridRetriever(store, mode="bm25", reranker=reranker)
    plain = retriever.search("duration", k=3, rerank=False)
    assert reranker.calls == [] and plain[0].chunk.symbol != "helper"
    with pytest.raises(ValueError, match="no reranker"):
        HybridRetriever(store, mode="bm25").search("duration", k=3, rerank=True)


def test_reranking_nothing_is_nothing(store: RepoIndex) -> None:
    reranker = RecordingReranker("x")
    assert HybridRetriever(store, mode="bm25", reranker=reranker).search("kubernetes", k=3) == []
    assert reranker.calls == []


def test_ties_keep_first_stage_order(store: RepoIndex) -> None:
    class Flat:
        def score(self, query, texts):
            return [0.5] * len(texts)

    plain = HybridRetriever(store, mode="bm25").search("duration", k=3)
    tied = HybridRetriever(store, mode="bm25", reranker=Flat()).search("duration", k=3)
    assert [h.chunk.id for h in tied] == [h.chunk.id for h in plain]


# -- configuration and eval modes ----------------------------------------------------------------


def test_default_reranker_follows_the_setting(monkeypatch) -> None:
    monkeypatch.setattr(settings, "rerank", False)
    assert default_reranker() is None
    monkeypatch.setattr(settings, "rerank", True)
    monkeypatch.setattr(settings, "rerank_model", "some/model")
    reranker = default_reranker()
    assert reranker is not None and reranker.model_name == "some/model"  # lazy: nothing loaded


def test_eval_mode_names_split_into_stage_and_flag() -> None:
    assert split_mode("hybrid") == ("hybrid", False)
    assert split_mode("bm25+rerank") == ("bm25", True)
    assert all_modes() == ["hybrid", "dense", "bm25", "hybrid+rerank", "dense+rerank", "bm25+rerank"]
    with pytest.raises(ValueError):
        split_mode("fuzzy+rerank")
