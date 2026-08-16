"""Text embedders behind one small interface.

The index only needs "list of strings in, list of vectors out". Keeping that as a protocol means
the tests can run against a deterministic hashing embedder in milliseconds, while the real thing
(fastembed, an ONNX runtime with no torch dependency) is loaded lazily and only when `coder
index` or a retrieval actually runs. Swapping the model is one setting; swapping the library is
one class.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from coder_agent.config import settings

Vector = list[float]


class Embedder(Protocol):
    """Anything that turns text into fixed-size vectors.

    Documents and queries are embedded separately because some models (bge among them) expect a
    query instruction prefix on one side only; fastembed handles that per model.
    """

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]: ...

    def embed_query(self, text: str) -> Vector: ...


_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")


class HashEmbedder:
    """Bag-of-words hashed into a fixed number of buckets, L2-normalised.

    No model, no download, deterministic. Two texts that share identifiers score high on cosine
    similarity, which is exactly enough to test that the index stores, updates and retrieves the
    right chunks. Not meant for real retrieval.
    """

    def __init__(self, dims: int = 64) -> None:
        self.dims = dims

    def _embed(self, text: str) -> Vector:
        vec = [0.0] * self.dims
        for token in _TOKEN.findall(text.lower()):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
            vec[int.from_bytes(digest, "big") % self.dims] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> Vector:
        return self._embed(text)


class FastEmbedder:
    """fastembed wrapper: CPU inference through ONNX Runtime, model cached under `models_dir`.

    The model object is created on first use so importing this module (or constructing the index
    to look at its stats) never pays the load time.
    """

    def __init__(
        self,
        model_name: str | None = None,
        cache_dir: Path | None = None,
        batch_size: int | None = None,
    ) -> None:
        self.model_name = model_name or settings.embedding_model
        self.cache_dir = cache_dir or settings.models_dir
        self.batch_size = batch_size or settings.embed_batch_size
        self._model = None

    def _load(self):  # untyped on purpose: fastembed types are not worth a module-level import
        if self._model is None:
            from fastembed import TextEmbedding

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._model = TextEmbedding(model_name=self.model_name, cache_dir=str(self.cache_dir))
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        model = self._load()
        return [vec.tolist() for vec in model.embed(list(texts), batch_size=self.batch_size)]

    def embed_query(self, text: str) -> Vector:
        model = self._load()
        return next(iter(model.query_embed(text))).tolist()


def default_embedder() -> Embedder:
    return FastEmbedder()
