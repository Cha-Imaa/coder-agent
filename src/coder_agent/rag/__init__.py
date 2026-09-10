"""Retrieval over the target repository: load, chunk, index, search."""

from coder_agent.rag.chunker import Chunk, chunk_file, chunk_repo, supports_syntax_chunking
from coder_agent.rag.loader import (
    RepoFile,
    detect_language,
    iter_repo_paths,
    load_file,
    load_repo,
)

__all__ = [
    "Chunk",
    "RepoFile",
    "chunk_file",
    "chunk_repo",
    "detect_language",
    "iter_repo_paths",
    "load_file",
    "load_repo",
    "supports_syntax_chunking",
]
