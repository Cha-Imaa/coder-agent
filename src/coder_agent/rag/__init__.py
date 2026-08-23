"""Retrieval over the target repository: load, chunk, index, search."""

from coder_agent.rag.chunker import Chunk, chunk_file, chunk_repo, supports_syntax_chunking
from coder_agent.rag.embeddings import Embedder, FastEmbedder, HashEmbedder, default_embedder
from coder_agent.rag.index import Hit, IndexStats, RepoIndex, embedding_text
from coder_agent.rag.loader import (
    RepoFile,
    detect_language,
    iter_repo_paths,
    load_file,
    load_repo,
)
from coder_agent.rag.reranker import (
    FastEmbedReranker,
    OverlapReranker,
    Reranker,
    default_reranker,
)
from coder_agent.rag.retriever import (
    BM25,
    MODES,
    HybridRetriever,
    Mode,
    reciprocal_rank_fusion,
    tokenize,
)

__all__ = [
    "BM25",
    "MODES",
    "Chunk",
    "Embedder",
    "FastEmbedReranker",
    "FastEmbedder",
    "HashEmbedder",
    "Hit",
    "HybridRetriever",
    "IndexStats",
    "Mode",
    "OverlapReranker",
    "RepoFile",
    "RepoIndex",
    "Reranker",
    "chunk_file",
    "chunk_repo",
    "default_embedder",
    "default_reranker",
    "detect_language",
    "embedding_text",
    "iter_repo_paths",
    "load_file",
    "load_repo",
    "reciprocal_rank_fusion",
    "supports_syntax_chunking",
    "tokenize",
]
