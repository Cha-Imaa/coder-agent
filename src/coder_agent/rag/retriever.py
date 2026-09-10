"""Hybrid retrieval: BM25 over identifiers plus dense vectors, fused by reciprocal rank.

Code queries come in two flavours. "Where is `write_ledger` called" names an exact identifier,
and an embedding model treats that token as noise while a lexical index finds it in one hop.
"Where do we record how many tokens a run used" names nothing in the code, and only the
embedding sees that `tokens_in` and `ledger` are about the same thing. Neither retriever is good
at the other's query, so the retriever runs both and merges the two rankings. Reciprocal rank
fusion is used because it needs no score calibration: a BM25 score of 12.3 and a cosine
similarity of 0.81 live on unrelated scales, but "third from the top" means the same in both.

BM25 is implemented here rather than imported. It is thirty lines, the corpus is small (one
repository), and owning it means the tokenizer can be tuned for code: `read_file` is indexed as
`read_file`, `read` and `file`, so a query for any of them finds it.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from coder_agent.config import settings
from coder_agent.rag.chunker import Chunk
from coder_agent.rag.index import Hit, RepoIndex

Mode = Literal["hybrid", "dense", "bm25"]
MODES: tuple[Mode, ...] = ("hybrid", "dense", "bm25")

# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
# Tokens too generic to distinguish one chunk from another in almost any codebase.
_STOP = frozenset(
    [
        *("the", "a", "an", "and", "or", "of", "to", "in", "for", "is", "are", "be", "this"),
        *("that", "with", "as", "on", "at", "by", "from", "it", "its", "into", "if", "else"),
        *("return", "def", "class", "import", "self", "none", "true", "false"),
    ]
)


def tokenize(text: str) -> list[str]:
    """Identifiers, their snake_case and camelCase parts, and numbers; lower-cased, no stopwords.

    Both the whole identifier and its parts are emitted. The whole one makes an exact-name query
    score highest on the chunk that defines it; the parts let a natural-language query ("ledger
    write") reach `write_ledger` at all.
    """
    tokens: list[str] = []
    for word in _WORD.findall(text):
        lowered = word.lower()
        if lowered in _STOP:
            continue
        tokens.append(lowered)
        parts = [p.lower() for p in _CAMEL.findall(word)]
        if len(parts) > 1:
            tokens.extend(p for p in parts if len(p) > 1 and p not in _STOP)
    return tokens


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------


@dataclass
class BM25:
    """Okapi BM25 over an in-memory corpus of (doc_id, text).

    `k1` controls how quickly repeated occurrences of a term saturate; `b` how much long documents
    are penalised. The defaults are the ones every textbook and Lucene ship with. Scores are
    unnormalised and only comparable within one query, which is why fusion works on ranks.
    """

    k1: float = 1.5
    b: float = 0.75
    ids: list[str] = field(default_factory=list)
    _tf: list[Counter[str]] = field(default_factory=list)
    _lengths: list[int] = field(default_factory=list)
    _df: Counter[str] = field(default_factory=Counter)
    _avg_len: float = 0.0

    @classmethod
    def build(cls, docs: Iterable[tuple[str, str]], **params: float) -> BM25:
        index = cls(**params)
        for doc_id, text in docs:
            tokens = tokenize(text)
            counts = Counter(tokens)
            index.ids.append(doc_id)
            index._tf.append(counts)
            index._lengths.append(len(tokens))
            index._df.update(counts.keys())
        n = len(index.ids)
        index._avg_len = (sum(index._lengths) / n) if n else 0.0
        return index

    def __len__(self) -> int:
        return len(self.ids)

    def _idf(self, term: str) -> float:
        # The "+1" inside the log keeps idf positive even for a term in every document; Lucene
        # uses the same variant so a very common identifier is down-weighted, not negated.
        n, df = len(self.ids), self._df.get(term, 0)
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, k: int) -> list[tuple[str, float]]:
        """Top-k (doc_id, score) with score > 0, best first."""
        terms = [t for t in set(tokenize(query)) if t in self._df]
        if not terms or not self.ids:
            return []
        scores = [0.0] * len(self.ids)
        for term in terms:
            idf = self._idf(term)
            for i, tf in enumerate(self._tf):
                freq = tf.get(term)
                if not freq:
                    continue
                norm = self.k1 * (1.0 - self.b + self.b * self._lengths[i] / self._avg_len)
                scores[i] += idf * (freq * (self.k1 + 1.0)) / (freq + norm)
        ranked = sorted(
            ((self.ids[i], s) for i, s in enumerate(scores) if s > 0.0),
            key=lambda pair: pair[1],
            reverse=True,
        )
        return ranked[:k]


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------


def reciprocal_rank_fusion(rankings: Sequence[Sequence[str]], k: int = 60) -> dict[str, float]:
    """RRF (Cormack et al., 2009): score(d) = sum over rankings of 1 / (k + rank(d)).

    `k=60` is the value from the paper and is rarely tuned. Its job is to flatten the curve so
    that first place is worth only a little more than fifth, which keeps one retriever's
    confident mistake from burying the other's correct answer. A document found by both lists
    always beats one found by only one at the same rank.
    """
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return fused


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------


class HybridRetriever:
    """Search one `RepoIndex` lexically, densely, or both.

    The BM25 side is rebuilt from the Chroma collection on first use and cached for the life of
    the object: the whole corpus is one repository, so the build takes tens of milliseconds and
    a second store on disk would be one more thing to keep in step with the vectors.
    """

    def __init__(
        self,
        index: RepoIndex,
        mode: Mode | None = None,
        *,
        candidates: int | None = None,
        rrf_k: int = 60,
    ) -> None:
        if mode is not None and mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        self.index = index
        self.mode: Mode = mode or settings.retrieval_mode
        # Each side returns more than k so the fusion has something to reorder; a hit that is
        # sixth on both lists should beat one that is first on one list and absent from the other.
        self.candidates = candidates or settings.retrieval_candidates
        self.rrf_k = rrf_k
        self._bm25: BM25 | None = None
        self._chunks: dict[str, Chunk] | None = None

    # -- lexical side -------------------------------------------------------------------------

    def _load_corpus(self) -> tuple[BM25, dict[str, Chunk]]:
        if self._bm25 is None or self._chunks is None:
            chunks = self.index.all_chunks()
            self._chunks = {chunk.id: chunk for chunk in chunks}
            # Index the same text the embedder saw so the path and symbol are searchable too.
            self._bm25 = BM25.build((c.id, f"{c.path} {c.symbol or ''}\n{c.text}") for c in chunks)
        return self._bm25, self._chunks

    def invalidate(self) -> None:
        """Forget the cached corpus; call after `index.update()` changed anything."""
        self._bm25 = None
        self._chunks = None

    def _bm25_hits(self, query: str, k: int) -> list[Hit]:
        bm25, chunks = self._load_corpus()
        return [Hit(chunk=chunks[doc_id], score=score) for doc_id, score in bm25.search(query, k)]

    # -- public -------------------------------------------------------------------------------

    def search(self, query: str, k: int = 8, mode: Mode | None = None) -> list[Hit]:
        """Top-k chunks for `query`. In hybrid mode the score is the RRF score, not a similarity."""
        mode = mode or self.mode
        if mode == "dense":
            return self.index.search(query, k=k)
        if mode == "bm25":
            return self._bm25_hits(query, k)

        n = max(k, self.candidates)
        dense = self.index.search(query, k=n)
        lexical = self._bm25_hits(query, n)
        by_id = {hit.chunk.id: hit.chunk for hit in [*dense, *lexical]}
        fused = reciprocal_rank_fusion(
            [[h.chunk.id for h in dense], [h.chunk.id for h in lexical]], k=self.rrf_k
        )
        ranked = sorted(fused.items(), key=lambda pair: pair[1], reverse=True)[:k]
        return [Hit(chunk=by_id[doc_id], score=score) for doc_id, score in ranked]
