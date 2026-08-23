"""Retrieval eval: gold files from the reference solution, recall@k and MRR, the report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coder_agent.evals import EvalTask, load_task
from coder_agent.evals.retrieval import (
    RetrievalReport,
    RetrievalResult,
    evaluate,
    gold_files,
    load_report,
)
from coder_agent.rag import HashEmbedder


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def task(tmp_path: Path) -> EvalTask:
    root = tmp_path / "fix-bug-ledger-rounding"
    _write(
        root,
        "task.toml",
        'id = "fix-bug-ledger-rounding"\ncategory = "fix-bug"\n'
        'prompt = "write_ledger rounds the amount wrongly; fix the rounding in write_ledger"\n'
        'notes = "n"\n',
    )
    _write(root, "repo/src/ledger.py", "def write_ledger(row):\n    return round(row, 0)\n")
    _write(root, "repo/src/parser.py", "def parse_config(text):\n    return text\n")
    _write(root, "repo/src/util.py", "def helper():\n    return 42\n")
    _write(root, "repo/tests/test_ledger.py", "def test_x():\n    assert True\n")
    _write(root, "hidden_tests/test_hidden.py", "def test_h():\n    assert True\n")
    # Solution: one changed file, one unchanged copy (noise), one brand-new file (not retrievable).
    _write(root, "solution/src/ledger.py", "def write_ledger(row):\n    return round(row, 2)\n")
    _write(root, "solution/src/parser.py", "def parse_config(text):\n    return text\n")
    _write(root, "solution/src/new_module.py", "X = 1\n")
    return load_task(root)


# -- gold files -------------------------------------------------------------------------------


def test_gold_files_are_the_changed_existing_files_only(task: EvalTask) -> None:
    assert gold_files(task) == ["src/ledger.py"]


# -- metrics ----------------------------------------------------------------------------------


def test_recall_and_reciprocal_rank() -> None:
    r = RetrievalResult("t", "fix-bug", "bm25", gold=["a.py", "b.py"], ranked=["x.py", "a.py", "b.py"])
    assert r.recall_at(1) == 0.0
    assert r.recall_at(2) == 0.5
    assert r.recall_at(3) == 1.0
    assert r.reciprocal_rank() == pytest.approx(1 / 2)
    assert RetrievalResult("t", "c", "m", gold=["a.py"], ranked=["x.py"]).reciprocal_rank() == 0.0
    assert RetrievalResult("t", "c", "m", gold=[], ranked=["x.py"]).recall_at(5) == 0.0


def test_report_rows_average_per_mode_and_render_markdown() -> None:
    results = [
        RetrievalResult("t1", "c", "bm25", ["a.py"], ["a.py"]),
        RetrievalResult("t2", "c", "bm25", ["a.py"], ["b.py", "c.py", "a.py"]),
        RetrievalResult("t1", "c", "dense", ["a.py"], ["b.py"]),
    ]
    report = RetrievalReport(results, ks=(1, 3))
    rows = {row["mode"]: row for row in report.rows()}
    assert rows["bm25"]["tasks"] == 2
    assert rows["bm25"]["recall@1"] == 0.5
    assert rows["bm25"]["recall@3"] == 1.0
    assert rows["bm25"]["mrr"] == pytest.approx((1 + 1 / 3) / 2)
    assert rows["dense"]["recall@3"] == 0.0
    table = report.markdown_table()
    assert table.splitlines()[0] == "| mode | tasks | recall@1 | recall@3 | MRR |"
    assert "| bm25 | 2 | 0.50 | 1.00 | 0.67 |" in table


def test_report_round_trips_through_json(tmp_path: Path) -> None:
    report = RetrievalReport([RetrievalResult("t", "c", "hybrid", ["a.py"], ["a.py"])], ks=(1,))
    report.meta = {"suite": "test"}
    path = report.write(results_dir=tmp_path, label="unit")
    assert path.name.endswith("-unit.json")
    loaded = load_report(path)
    assert loaded.results == report.results and loaded.ks == (1,) and loaded.meta == report.meta
    assert json.loads(path.read_text())["summary"][0]["recall@1"] == 1.0


# -- end to end over a real index --------------------------------------------------------------


def test_evaluate_indexes_the_repo_and_scores_every_mode(task: EvalTask) -> None:
    seen: list[str] = []
    report = evaluate(
        [task], ("bm25", "dense", "hybrid"), ks=(1, 3), embedder=HashEmbedder(),
        progress=lambda t, i, n: seen.append(f"{t.id}:{i}/{n}"),
    )
    assert seen == ["fix-bug-ledger-rounding:1/1"]
    assert [r.mode for r in report.results] == ["bm25", "dense", "hybrid"]
    for r in report.results:
        assert r.gold == ["src/ledger.py"]
        assert 0.0 <= r.recall_at(3) <= 1.0
    # The prompt names the function twice; the lexical side must put its file first.
    bm25 = next(r for r in report.results if r.mode == "bm25")
    assert bm25.ranked[0] == "src/ledger.py" and bm25.recall_at(1) == 1.0
    # Nothing was written into the benchmark repo itself.
    assert not (task.repo_dir / ".coder-agent").exists()


def test_evaluate_reranked_mode_uses_the_given_reranker(task: EvalTask) -> None:
    class Inverting:
        """Prefers whatever does not mention the ledger, so a reranked row must look different."""

        calls = 0

        def score(self, query, texts):
            Inverting.calls += 1
            return [0.0 if "ledger" in t else 1.0 for t in texts]

    # Hybrid, not bm25: the lexical side alone returns only chunks that share a term with the
    # prompt, which here is the ledger file by itself, and one candidate cannot be reordered.
    report = evaluate(
        [task], ("hybrid", "hybrid+rerank"), ks=(1, 3), embedder=HashEmbedder(),
        reranker=Inverting(),
    )
    plain, reranked = report.results
    assert (plain.mode, reranked.mode) == ("hybrid", "hybrid+rerank")
    assert plain.ranked[0] == "src/ledger.py"
    assert reranked.ranked[0] != "src/ledger.py" and Inverting.calls == 1
    assert set(plain.ranked) == set(reranked.ranked), "same candidates, different order"
    assert [row["mode"] for row in report.rows()] == ["hybrid", "hybrid+rerank"]


def test_evaluate_skips_tasks_without_gold_files(task: EvalTask) -> None:
    (task.solution_dir / "src" / "ledger.py").write_text(
        (task.repo_dir / "src" / "ledger.py").read_text()
    )
    assert gold_files(task) == []
    assert evaluate([task], ("bm25",), embedder=HashEmbedder()).results == []
