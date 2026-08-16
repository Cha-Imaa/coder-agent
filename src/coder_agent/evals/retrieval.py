"""Retrieval evaluation: does the retriever surface the files the fix has to touch?

The agent benchmark measures the whole system and is slow and noisy (one model call can go
either way). Retrieval can be measured on its own, offline, in seconds: for every task we know
which files the reference solution changes, so we ask "given only the task prompt, at what rank
does each of those files first appear?". Recall@k over those gold files, per retrieval mode, is
the number that tells us whether hybrid search is worth its complexity before we spend a day of
API quota finding out through pass rates.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from coder_agent.evals.tasks import EvalTask
from coder_agent.rag.embeddings import Embedder, default_embedder
from coder_agent.rag.index import RepoIndex
from coder_agent.rag.retriever import MODES, HybridRetriever, Mode

RESULTS_DIR = Path(__file__).resolve().parents[3] / "evals" / "results"
DEFAULT_KS: tuple[int, ...] = (1, 3, 5)


def gold_files(task: EvalTask) -> list[str]:
    """Files the reference solution changes and that already exist in the starting repo.

    A file the solution creates from scratch cannot be retrieved, so it is not a target; a
    solution file identical to the original is packaging noise, not a change.
    """
    gold: list[str] = []
    for src in sorted(task.solution_dir.rglob("*")):
        if not src.is_file() or "__pycache__" in src.parts:
            continue
        rel = src.relative_to(task.solution_dir)
        original = task.repo_dir / rel
        if original.is_file() and original.read_bytes() != src.read_bytes():
            gold.append(rel.as_posix())
    return gold


@dataclass
class RetrievalResult:
    """One (task, mode) measurement: the gold files and the files in the order retrieval ranked them."""

    task_id: str
    category: str
    mode: str
    gold: list[str]
    ranked: list[str]  # distinct file paths, in order of their best-ranked chunk

    def recall_at(self, k: int) -> float:
        if not self.gold:
            return 0.0
        found = set(self.gold) & set(self.ranked[:k])
        return len(found) / len(self.gold)

    def reciprocal_rank(self) -> float:
        """1 / rank of the first gold file (0 when none is retrieved). Averaged, this is MRR."""
        for rank, path in enumerate(self.ranked, start=1):
            if path in self.gold:
                return 1.0 / rank
        return 0.0


@dataclass
class RetrievalReport:
    results: list[RetrievalResult]
    ks: tuple[int, ...] = DEFAULT_KS
    meta: dict[str, Any] = field(default_factory=dict)

    def modes(self) -> list[str]:
        seen: dict[str, None] = {}
        for r in self.results:
            seen.setdefault(r.mode, None)
        return list(seen)

    def rows(self) -> list[dict[str, Any]]:
        """One row per mode: mean recall at each k, MRR, number of tasks."""
        rows = []
        for mode in self.modes():
            rs = [r for r in self.results if r.mode == mode]
            n = len(rs)
            row: dict[str, Any] = {"mode": mode, "tasks": n}
            for k in self.ks:
                row[f"recall@{k}"] = sum(r.recall_at(k) for r in rs) / n if n else 0.0
            row["mrr"] = sum(r.reciprocal_rank() for r in rs) / n if n else 0.0
            rows.append(row)
        return rows

    def markdown_table(self) -> str:
        heads = ["mode", "tasks", *[f"recall@{k}" for k in self.ks], "MRR"]
        lines = ["| " + " | ".join(heads) + " |", "|" + "---|" * len(heads)]
        for row in self.rows():
            cells = [row["mode"], str(row["tasks"])]
            cells += [f"{row[f'recall@{k}']:.2f}" for k in self.ks]
            cells.append(f"{row['mrr']:.2f}")
            lines.append("| " + " | ".join(cells) + " |")
        return "\n".join(lines)

    def to_json(self) -> str:
        payload = {
            "meta": self.meta,
            "ks": list(self.ks),
            "summary": self.rows(),
            "results": [asdict(r) for r in self.results],
        }
        return json.dumps(payload, indent=2)

    def write(self, results_dir: Path = RESULTS_DIR, label: str = "retrieval") -> Path:
        results_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = results_dir / f"{stamp}-{label}.json"
        path.write_text(self.to_json(), encoding="utf-8")
        return path


def load_report(path: Path) -> RetrievalReport:
    data = json.loads(path.read_text(encoding="utf-8"))
    return RetrievalReport(
        results=[RetrievalResult(**r) for r in data["results"]],
        ks=tuple(data["ks"]),
        meta=data.get("meta", {}),
    )


def _ranked_files(hits) -> list[str]:
    files: dict[str, None] = {}
    for hit in hits:
        files.setdefault(hit.chunk.path, None)
    return list(files)


ProgressFn = Callable[[EvalTask, int, int], None]


def evaluate(
    tasks: Sequence[EvalTask],
    modes: Sequence[Mode] = MODES,
    *,
    ks: tuple[int, ...] = DEFAULT_KS,
    embedder: Embedder | None = None,
    depth: int | None = None,
    progress: ProgressFn | None = None,
) -> RetrievalReport:
    """Index each task's starting repo once, query it with the task prompt in every mode.

    The index lives in a temp directory so the benchmark repos stay untouched, and one embedder
    is shared across tasks so the model loads once. `depth` is how many chunks to retrieve; the
    deepest k in `ks` is enough, since ranks past it never count.
    """
    depth = depth or max(ks)
    embedder = embedder or default_embedder()  # once, not once per task: the model load is slow
    results: list[RetrievalResult] = []
    tasks = [t for t in tasks if gold_files(t)]
    # Chroma keeps segment files mapped while the process lives; on Windows that makes the
    # cleanup fail, which is not worth failing the eval over.
    with tempfile.TemporaryDirectory(prefix="coder-retrieval-", ignore_cleanup_errors=True) as tmp:
        for i, task in enumerate(tasks, start=1):
            gold = gold_files(task)
            index = RepoIndex(task.repo_dir, embedder, index_dir=Path(tmp) / task.id)
            index.update()
            for mode in modes:
                hits = HybridRetriever(index, mode, candidates=depth * 3).search(
                    task.prompt.strip(), k=depth * 3
                )
                results.append(
                    RetrievalResult(task.id, task.category, mode, gold, _ranked_files(hits))
                )
            if progress is not None:
                progress(task, i, len(tasks))
    return RetrievalReport(results, ks=ks)
