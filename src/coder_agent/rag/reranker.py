"""Rerankers: a second, slower look at the top candidates.

Embedding retrieval scores a query against every chunk with one dot product, which is what
makes it fast and what makes it shallow: the query and the chunk are encoded separately and
never see each other. A cross-encoder reads the pair together and outputs one relevance score,
so it can tell that "convert minutes correctly" matches the function whose docstring says
"minutes are treated as hours" even though the two share no identifier. It costs a full
transformer forward pass per (query, chunk) pair, so it is never run over the corpus; it reorders
the twenty or so candidates the first stage already found.

Same shape as `embeddings.py`: one protocol, a deterministic stand-in for tests, and a lazy
fastembed wrapper for the real model.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from coder_agent.config import settings


class Reranker(Protocol):
    """Anything that scores each of `texts` against `query`; higher means more relevant."""

    def score(self, query: str, texts: Sequence[str]) -> list[float]: ...


_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")


class OverlapReranker:
    """Fraction of the query's tokens that appear in the text. Deterministic, no model.

    Exists so the retriever's reranking path can be tested in milliseconds; it is a caricature
    of what a cross-encoder does (judge the pair, not the parts) and is not meant for real use.
    """

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        wanted = {t.lower() for t in _TOKEN.findall(query)}
        if not wanted:
            return [0.0] * len(texts)
        scores = []
        for text in texts:
            have = {t.lower() for t in _TOKEN.findall(text)}
            scores.append(len(wanted & have) / len(wanted))
        return scores


class FastEmbedReranker:
    """fastembed's `TextCrossEncoder`: an ONNX cross-encoder on the CPU, cached under `models_dir`.

    The default, `Xenova/ms-marco-MiniLM-L-6-v2`, is 80 MB, six layers, and scores twenty code
    chunks in about 1.5 s on a laptop CPU; the twelve-layer variant is roughly twice as slow for a
    small gain on MS MARCO. Scores are raw logits (negative for irrelevant pairs), only comparable
    within one query, which is fine: they are used to sort, never to threshold.
    """

    def __init__(self, model_name: str | None = None, cache_dir: Path | None = None) -> None:
        self.model_name = model_name or settings.rerank_model
        self.cache_dir = cache_dir or settings.models_dir
        self._model = None

    def _load(self):  # untyped on purpose, as in embeddings.py
        if self._model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._model = TextCrossEncoder(model_name=self.model_name, cache_dir=str(self.cache_dir))
        return self._model

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        return [float(s) for s in self._load().rerank(query, list(texts))]


def default_reranker() -> Reranker | None:
    """The configured reranker, or None when reranking is off (the default)."""
    return FastEmbedReranker() if settings.rerank else None
