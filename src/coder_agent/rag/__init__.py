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

__all__ = [
    "Chunk",
    "Embedder",
    "FastEmbedder",
    "HashEmbedder",
    "Hit",
    "IndexStats",
    "RepoFile",
    "RepoIndex",
    "chunk_file",
    "chunk_repo",
    "default_embedder",
    "detect_language",
    "embedding_text",
    "iter_repo_paths",
    "load_file",
    "load_repo",
    "supports_syntax_chunking",
]
