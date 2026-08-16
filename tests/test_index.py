"""Incremental index: only files whose hash changed are re-embedded; deletions are dropped."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from coder_agent.rag import HashEmbedder, RepoIndex, embedding_text
from coder_agent.rag.chunker import Chunk


class CountingEmbedder(HashEmbedder):
    """Records every document it embeds so tests can assert on what work was done."""

    def __init__(self) -> None:
        super().__init__(dims=64)
        self.calls: list[list[str]] = []

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return super().embed_documents(texts)

    @property
    def embedded(self) -> list[str]:
        return [t for call in self.calls for t in call]


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _write(tmp_path, ".gitignore", "*.log\n")
    _write(tmp_path, "src/ledger.py", "def write_ledger(row):\n    return row\n")
    _write(tmp_path, "src/parser.py", "def parse_config(text):\n    return text\n")
    _write(tmp_path, "README.md", "# demo\nA small repo for the index tests.\n")
    _write(tmp_path, "noise.log", "ignored")
    return tmp_path


@pytest.fixture
def store(repo: Path) -> RepoIndex:
    return RepoIndex(repo, CountingEmbedder(), index_dir=repo / ".coder-agent" / "chroma")


def _embedder(store: RepoIndex) -> CountingEmbedder:
    assert isinstance(store.embedder, CountingEmbedder)
    return store.embedder


def test_first_update_embeds_every_file(store: RepoIndex) -> None:
    stats = store.update()
    assert stats.files_total == 4  # .gitignore, ledger, parser, README; the log is ignored
    assert stats.files_added == 4
    assert stats.files_changed == stats.files_removed == stats.files_unchanged == 0
    assert stats.chunks_added == stats.chunks_total == store.count() > 0
    assert set(store.indexed_files()) == {
        ".gitignore",
        "src/ledger.py",
        "src/parser.py",
        "README.md",
    }


def test_second_update_with_no_changes_does_no_work(store: RepoIndex) -> None:
    store.update()
    before = store.count()
    _embedder(store).calls.clear()

    stats = store.update()
    assert stats.files_unchanged == 4
    assert stats.files_added == stats.files_changed == stats.files_removed == 0
    assert stats.chunks_added == stats.chunks_removed == 0
    assert _embedder(store).calls == []
    assert store.count() == before


def test_changed_file_is_the_only_one_re_embedded(store: RepoIndex, repo: Path) -> None:
    store.update()
    _embedder(store).calls.clear()
    old_sha = store.indexed_files()["src/ledger.py"]

    _write(repo, "src/ledger.py", "def write_ledger(row):\n    return row * 2\n")
    stats = store.update()

    assert stats.files_changed == 1 and stats.files_unchanged == 3
    assert stats.chunks_removed >= 1 and stats.chunks_added >= 1
    assert all(text.startswith("src/ledger.py") for text in _embedder(store).embedded)
    assert store.indexed_files()["src/ledger.py"] != old_sha
    # No stale chunk of the old version survives.
    docs = store.collection.get(where={"path": "src/ledger.py"}, include=["documents"])
    assert all("row * 2" in d for d in docs["documents"] or [])


def test_deleted_file_is_dropped_from_the_index(store: RepoIndex, repo: Path) -> None:
    store.update()
    (repo / "src" / "parser.py").unlink()
    stats = store.update()
    assert stats.files_removed == 1
    assert stats.chunks_removed >= 1
    assert "src/parser.py" not in store.indexed_files()


def test_new_file_is_added_without_touching_the_rest(store: RepoIndex, repo: Path) -> None:
    store.update()
    _embedder(store).calls.clear()
    _write(repo, "src/new_module.py", "def brand_new():\n    return 42\n")
    stats = store.update()
    assert stats.files_added == 1 and stats.files_unchanged == 4
    assert all(text.startswith("src/new_module.py") for text in _embedder(store).embedded)


def test_index_persists_across_instances(store: RepoIndex, repo: Path) -> None:
    store.update()
    again = RepoIndex(repo, CountingEmbedder(), index_dir=store.index_dir)
    assert again.exists()
    assert again.count() == store.count()
    assert again.update().files_unchanged == 4


def test_clear_forces_a_full_rebuild(store: RepoIndex) -> None:
    store.update()
    store.clear()
    assert not store.exists()
    stats = store.update()
    assert stats.files_added == 4 and stats.files_unchanged == 0


def test_search_returns_the_matching_chunk_with_its_location(store: RepoIndex) -> None:
    store.update()
    hits = store.search("write_ledger row", k=3)
    assert hits
    top = hits[0]
    assert top.chunk.path == "src/ledger.py"
    assert top.chunk.symbol == "write_ledger"
    assert top.location == "src/ledger.py:1-2 (write_ledger)"
    assert 0.0 <= top.score <= 1.0
    assert hits == sorted(hits, key=lambda h: h.score, reverse=True)


def test_search_on_empty_index_returns_nothing(store: RepoIndex) -> None:
    assert store.search("anything") == []


def test_progress_callback_sees_every_phase(store: RepoIndex) -> None:
    seen: list[tuple[str, int, int]] = []
    store.update(progress=lambda phase, done, total: seen.append((phase, done, total)))
    phases = {phase for phase, _, _ in seen}
    assert phases == {"chunk", "embed", "write"}
    chunk_steps = [(done, total) for phase, done, total in seen if phase == "chunk"]
    assert chunk_steps[-1] == (4, 4)


def test_embedding_text_prepends_path_and_symbol() -> None:
    chunk = Chunk("src/a.py", "python", 1, 2, "def f():\n    pass", "function", "f")
    assert embedding_text(chunk) == "src/a.py f\ndef f():\n    pass"
    window = Chunk("README.md", "markdown", 1, 1, "# hi", "window", None)
    assert embedding_text(window) == "README.md\n# hi"


def test_hash_embedder_is_deterministic_and_normalised() -> None:
    emb = HashEmbedder(dims=32)
    a, b = emb.embed_documents(["def foo(): pass", "def foo(): pass"])
    assert a == b
    assert abs(sum(v * v for v in a) - 1.0) < 1e-9
    assert emb.embed_documents([""]) == [[0.0] * 32]


def test_fast_embedder_prefixes_queries_but_not_documents() -> None:
    import numpy as np

    from coder_agent.rag import FastEmbedder

    class FakeModel:
        seen: list[tuple[str, list[str]]] = []

        def embed(self, texts, batch_size=None):
            self.seen.append(("embed", list(texts)))
            return [np.zeros(3) for _ in texts]

        def query_embed(self, text):
            self.seen.append(("query", [text]))
            return [np.zeros(3)]

    embedder = FastEmbedder(query_prefix="Q: ")
    embedder._model = FakeModel()
    embedder.embed_documents(["doc one"])
    embedder.embed_query("find it")
    assert FakeModel.seen == [("embed", ["doc one"]), ("query", ["Q: find it"])]
