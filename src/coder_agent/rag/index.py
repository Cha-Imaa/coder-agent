"""Incremental vector index of one repository, persisted in Chroma under `.coder-agent/chroma`.

The unit of work is a file, and the question asked of each file is "did its hash change?". Every
chunk is stored with the SHA-256 of the file it came from, so the set of (path, hash) pairs
already in the collection *is* the manifest: there is no second bookkeeping file that could
disagree with the vectors. An update then reduces to three set differences between the files on
disk and the files in the index: added, changed, removed. Unchanged files cost a hash comparison
and nothing else, which is what makes re-indexing after a one-line edit take a second instead of
the minutes a full rebuild needs on the CPU.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coder_agent.config import settings
from coder_agent.rag.chunker import Chunk, chunk_file
from coder_agent.rag.embeddings import Embedder, Vector, default_embedder
from coder_agent.rag.loader import RepoFile, load_repo

if TYPE_CHECKING:
    from chromadb.api import ClientAPI
    from chromadb.api.models.Collection import Collection

# Chroma refuses batches above a server-side limit (5461 today); stay far below it so a batch is
# also a reasonable unit of progress reporting.
UPSERT_BATCH = 256

ProgressFn = Callable[[str, int, int], None]
"""Called as progress(phase, done, total) where phase is "chunk", "embed" or "write"."""


@dataclass
class IndexStats:
    """What one `update()` did. `seconds` covers the whole call, embedding included."""

    files_total: int = 0
    files_added: int = 0
    files_changed: int = 0
    files_removed: int = 0
    files_unchanged: int = 0
    chunks_added: int = 0
    chunks_removed: int = 0
    chunks_total: int = 0
    seconds: float = 0.0

    @property
    def files_embedded(self) -> int:
        return self.files_added + self.files_changed

    def summary(self) -> str:
        return (
            f"{self.files_total} files: {self.files_added} added, {self.files_changed} changed, "
            f"{self.files_removed} removed, {self.files_unchanged} unchanged · "
            f"{self.chunks_added} chunks embedded, {self.chunks_removed} dropped, "
            f"{self.chunks_total} in index · {self.seconds:.1f}s"
        )


@dataclass(frozen=True)
class Hit:
    """One retrieved chunk. `score` is cosine similarity in [0, 1], higher is better."""

    chunk: Chunk
    score: float

    @property
    def location(self) -> str:
        return self.chunk.location


@dataclass
class _Plan:
    added: list[RepoFile] = field(default_factory=list)
    changed: list[RepoFile] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0


def embedding_text(chunk: Chunk) -> str:
    """What the model sees: a path-and-symbol header, then the code.

    The header lets a query like "where is the ledger written" match `telemetry/ledger.py` even
    when the chunk body never says "ledger". The stored document stays the raw text so what the
    agent reads back is exactly what is in the file.
    """
    header = chunk.path if chunk.symbol is None else f"{chunk.path} {chunk.symbol}"
    return f"{header}\n{chunk.text}"


def _metadata(chunk: Chunk, sha256: str) -> dict[str, Any]:
    # Chroma metadata values must be str/int/float/bool, never None: omit the optional fields.
    meta: dict[str, Any] = {
        "path": chunk.path,
        "sha256": sha256,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "kind": chunk.kind,
    }
    if chunk.language is not None:
        meta["language"] = chunk.language
    if chunk.symbol is not None:
        meta["symbol"] = chunk.symbol
    return meta


def _chunk_from_record(document: str, meta: dict[str, Any]) -> Chunk:
    return Chunk(
        path=meta["path"],
        language=meta.get("language"),
        start_line=int(meta["start_line"]),
        end_line=int(meta["end_line"]),
        text=document,
        kind=meta["kind"],
        symbol=meta.get("symbol"),
    )


def _batched(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class RepoIndex:
    """Chunks of one repo in one Chroma collection, kept in step with the files on disk."""

    def __init__(
        self,
        repo: Path,
        embedder: Embedder | None = None,
        *,
        index_dir: Path | None = None,
        collection_name: str | None = None,
    ) -> None:
        self.repo = repo.resolve()
        self.index_dir = index_dir or settings.index_dir(self.repo)
        self.collection_name = collection_name or settings.index_collection
        self._embedder = embedder
        self._chroma: ClientAPI | None = None
        self._collection: Collection | None = None

    # -- storage -----------------------------------------------------------------------------

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = default_embedder()
        return self._embedder

    def _client(self) -> ClientAPI:
        if self._chroma is None:
            import chromadb

            self.index_dir.mkdir(parents=True, exist_ok=True)
            self._chroma = chromadb.PersistentClient(
                path=str(self.index_dir),
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
        return self._chroma

    @property
    def collection(self) -> Collection:
        if self._collection is None:
            # Cosine distance: bge vectors are normalised, and similarity in [0, 1] is easier to
            # read (and later to fuse with BM25) than raw L2.
            self._collection = self._client().get_or_create_collection(
                self.collection_name, metadata={"hnsw:space": "cosine"}
            )
        return self._collection

    def exists(self) -> bool:
        """True when a collection for this repo has been created, even if it holds no chunks."""
        if not self.index_dir.is_dir():
            return False
        names = [getattr(c, "name", c) for c in self._client().list_collections()]
        return self.collection_name in names

    def count(self) -> int:
        return self.collection.count()

    def all_chunks(self) -> list[Chunk]:
        """Every chunk in the collection, for retrievers that need the corpus (BM25)."""
        result = self.collection.get(include=["documents", "metadatas"])
        docs = result["documents"] or []
        metas = result["metadatas"] or []
        return [_chunk_from_record(doc, dict(meta)) for doc, meta in zip(docs, metas, strict=True)]

    def indexed_files(self) -> dict[str, str]:
        """path -> sha256 for every file that has at least one chunk in the collection."""
        result = self.collection.get(include=["metadatas"])
        files: dict[str, str] = {}
        for meta in result["metadatas"] or []:
            files[meta["path"]] = meta["sha256"]
        return files

    def clear(self) -> None:
        """Drop the collection; the next `update()` is a full rebuild.

        Goes through the client rather than deleting the directory: Chroma keeps its segment
        files memory-mapped while the process lives, and Windows refuses to unlink an open file.
        """
        if self.exists():
            self._client().delete_collection(self.collection_name)
        self._collection = None

    # -- update ------------------------------------------------------------------------------

    def plan(self, files: list[RepoFile] | None = None) -> _Plan:
        """Diff the repo against the index without touching either."""
        files = load_repo(self.repo) if files is None else files
        indexed = self.indexed_files()
        plan = _Plan()
        seen: set[str] = set()
        for file in files:
            seen.add(file.path)
            old = indexed.get(file.path)
            if old is None:
                plan.added.append(file)
            elif old != file.sha256:
                plan.changed.append(file)
            else:
                plan.unchanged += 1
        plan.removed = sorted(path for path in indexed if path not in seen)
        return plan

    def update(self, *, progress: ProgressFn | None = None) -> IndexStats:
        """Bring the index in step with the working tree; returns what changed."""
        started = time.perf_counter()
        report = progress or (lambda phase, done, total: None)
        files = load_repo(self.repo)
        plan = self.plan(files)
        stats = IndexStats(
            files_total=len(files),
            files_added=len(plan.added),
            files_changed=len(plan.changed),
            files_removed=len(plan.removed),
            files_unchanged=plan.unchanged,
        )

        # Changed files are deleted wholesale and re-added: a chunk's id includes its line range,
        # so an edit near the top of a file shifts every chunk below it anyway.
        stale_paths = plan.removed + [f.path for f in plan.changed]
        if stale_paths:
            stats.chunks_removed = self._delete_paths(stale_paths)

        to_embed = plan.added + plan.changed
        chunks: list[tuple[Chunk, str]] = []
        for i, file in enumerate(to_embed, start=1):
            chunks.extend((chunk, file.sha256) for chunk in chunk_file(file))
            report("chunk", i, len(to_embed))

        if chunks:
            self._upsert(chunks, report)
            stats.chunks_added = len(chunks)

        stats.chunks_total = self.count()
        stats.seconds = time.perf_counter() - started
        return stats

    def _delete_paths(self, paths: list[str]) -> int:
        removed = 0
        for batch in _batched(paths, UPSERT_BATCH):
            before = self.collection.count()
            self.collection.delete(where={"path": {"$in": batch}})
            removed += before - self.collection.count()
        return removed

    def _upsert(self, chunks: list[tuple[Chunk, str]], report: ProgressFn) -> None:
        # Two chunks can only share an id if they have the same path, range and text, which the
        # chunker never emits; the guard is here because Chroma rejects duplicate ids in a batch.
        unique: dict[str, tuple[Chunk, str]] = {}
        for chunk, sha in chunks:
            unique.setdefault(chunk.id, (chunk, sha))
        records = list(unique.values())
        total = len(records)
        done = 0
        for batch in _batched(records, UPSERT_BATCH):
            vectors = self.embedder.embed_documents([embedding_text(c) for c, _ in batch])
            done += len(batch)
            report("embed", done, total)
            self.collection.upsert(
                ids=[c.id for c, _ in batch],
                documents=[c.text for c, _ in batch],
                embeddings=vectors,
                metadatas=[_metadata(c, sha) for c, sha in batch],
            )
            report("write", done, total)

    # -- search ------------------------------------------------------------------------------

    def search(self, query: str, k: int = 8) -> list[Hit]:
        """Dense nearest neighbours by cosine similarity. The hybrid retriever builds on this."""
        if self.count() == 0:
            return []
        vector: Vector = self.embedder.embed_query(query)
        result = self.collection.query(
            query_embeddings=[vector],
            n_results=min(k, self.count()),
            include=["documents", "metadatas", "distances"],
        )
        hits: list[Hit] = []
        docs = (result["documents"] or [[]])[0]
        metas = (result["metadatas"] or [[]])[0]
        dists = (result["distances"] or [[]])[0]
        for doc, meta, dist in zip(docs, metas, dists, strict=True):
            hits.append(Hit(chunk=_chunk_from_record(doc, dict(meta)), score=1.0 - float(dist)))
        return hits
