"""Hybrid retrieval: code-aware tokens, BM25 ranking, rank fusion, and the three modes."""

from __future__ import annotations

from pathlib import Path

import pytest

from coder_agent.rag import (
    BM25,
    HashEmbedder,
    HybridRetriever,
    RepoIndex,
    reciprocal_rank_fusion,
    tokenize,
)

# -- tokenizer --------------------------------------------------------------------------------


def test_tokenize_splits_snake_and_camel_case_but_keeps_the_whole_identifier() -> None:
    tokens = tokenize("def write_ledger(row): return LedgerWriter")
    assert "write_ledger" in tokens
    assert {"write", "ledger"} <= set(tokens)
    assert "ledgerwriter" in tokens
    assert {"ledger", "writer"} <= set(tokens)


def test_tokenize_drops_stopwords_and_lowercases() -> None:
    assert tokenize("The Return of the Class") == []
    assert tokenize("HTTPServer") == ["httpserver", "http", "server"]


# -- bm25 --------------------------------------------------------------------------------------


@pytest.fixture
def corpus() -> BM25:
    return BM25.build(
        [
            ("ledger", "def write_ledger(row):\n    conn.execute('insert into runs', row)"),
            ("parser", "def parse_config(text):\n    return yaml.safe_load(text)"),
            ("both", "def write_and_parse(row, text):\n    write_ledger(row); parse_config(text)"),
            ("noise", "x = 1\ny = 2\n"),
        ]
    )


def test_bm25_ranks_the_defining_chunk_first_for_an_exact_identifier(corpus: BM25) -> None:
    ranked = corpus.search("write_ledger", k=3)
    assert ranked[0][0] == "ledger"
    assert {doc_id for doc_id, _ in ranked} == {"ledger", "both"}


def test_bm25_returns_nothing_for_unknown_terms(corpus: BM25) -> None:
    assert corpus.search("kubernetes", k=5) == []
    assert corpus.search("", k=5) == []


def test_bm25_scores_are_positive_and_sorted(corpus: BM25) -> None:
    ranked = corpus.search("parse config text", k=10)
    scores = [s for _, s in ranked]
    assert scores == sorted(scores, reverse=True)
    assert all(s > 0 for s in scores)


def test_bm25_term_in_every_document_still_has_positive_idf() -> None:
    index = BM25.build([("a", "foo bar"), ("b", "foo baz")])
    assert index._idf("foo") > 0


# -- fusion ------------------------------------------------------------------------------------


def test_rrf_prefers_documents_present_in_both_rankings() -> None:
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["c", "d", "e"]], k=60)
    # c is third in one list and first in the other; a is first in one and absent from the other.
    assert fused["c"] > fused["a"]
    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["c"] == pytest.approx(1 / 63 + 1 / 61)


def test_rrf_with_one_ranking_preserves_its_order() -> None:
    fused = reciprocal_rank_fusion([["x", "y", "z"]])
    assert sorted(fused, key=fused.get, reverse=True) == ["x", "y", "z"]


# -- retriever over a real index -----------------------------------------------------------------


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def store(tmp_path: Path) -> RepoIndex:
    _write(
        tmp_path,
        "src/ledger.py",
        "def write_ledger(row):\n    conn.execute('insert into runs', row)\n",
    )
    _write(tmp_path, "src/parser.py", "def parse_config(text):\n    return load(text)\n")
    _write(tmp_path, "src/util.py", "def helper():\n    return 42\n")
    index = RepoIndex(tmp_path, HashEmbedder(), index_dir=tmp_path / ".coder-agent" / "chroma")
    index.update()
    return index


def test_bm25_mode_finds_the_exact_identifier(store: RepoIndex) -> None:
    hits = HybridRetriever(store, mode="bm25").search("write_ledger", k=2)
    assert hits and hits[0].chunk.symbol == "write_ledger"


def test_dense_mode_delegates_to_the_vector_index(store: RepoIndex) -> None:
    dense = HybridRetriever(store, mode="dense").search("parse_config text", k=1)
    assert dense[0].chunk.symbol == "parse_config"
    assert 0.0 <= dense[0].score <= 1.0


def test_hybrid_mode_returns_at_most_k_and_puts_the_agreed_hit_first(store: RepoIndex) -> None:
    hits = HybridRetriever(store, mode="hybrid", candidates=10).search("write_ledger row", k=2)
    assert len(hits) == 2
    assert hits[0].chunk.symbol == "write_ledger"
    # Fused scores are RRF sums, so roughly 2/61 for a chunk both sides ranked first.
    assert hits[0].score == pytest.approx(2 / 61, rel=0.2)


def test_mode_can_be_overridden_per_call(store: RepoIndex) -> None:
    retriever = HybridRetriever(store, mode="dense")
    assert retriever.search("helper", k=1, mode="bm25")[0].chunk.symbol == "helper"


def test_invalid_mode_is_rejected(store: RepoIndex) -> None:
    with pytest.raises(ValueError):
        HybridRetriever(store, mode="fuzzy")  # type: ignore[arg-type]


def test_invalidate_picks_up_new_files(store: RepoIndex) -> None:
    retriever = HybridRetriever(store, mode="bm25")
    assert retriever.search("brand_new_symbol", k=1) == []
    _write(store.repo, "src/new.py", "def brand_new_symbol():\n    return 1\n")
    store.update()
    assert retriever.search("brand_new_symbol", k=1) == []  # stale cache, by design
    retriever.invalidate()
    assert retriever.search("brand_new_symbol", k=1)[0].chunk.symbol == "brand_new_symbol"
