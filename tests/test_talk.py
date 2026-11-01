"""The web talk in `docs/talk/` quotes numbers and draws charts; these keep both honest.

The page is static HTML served by GitHub Pages, so nothing recomputes it at deploy time. What
can go wrong is drift: a new results file lands and `data.json` is not regenerated, or a sentence
on a slide keeps quoting an old run, or a renamed asset leaves the page with a 404.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from coder_agent.evals.figures import latest_results
from coder_agent.evals.runner import SuiteResult, TaskResult
from coder_agent.evals.talk import (
    TALK_DATA,
    latest_retrieval_summary,
    retrieval_section,
    short_name,
    talk_data,
)

ROOT = Path(__file__).resolve().parents[1]
TALK = ROOT / "docs" / "talk"
PAGE = TALK / "index.html"


def _task(task_id: str, category: str, passed: bool, iterations: int = 1) -> TaskResult:
    return TaskResult(
        task_id=task_id, category=category, passed=passed, agent_status="done",
        iterations=iterations, steps=10, input_tokens=900, output_tokens=100,
        agent_seconds=30.0, grade_exit_code=0 if passed else 1, grade_output="",
        usage={"act": {"calls": 3, "input_tokens": 800, "output_tokens": 90},
               "plan": {"calls": 1, "input_tokens": 100, "output_tokens": 10}},
    )


def _suite(model: str, passes: list[bool], started: float, **meta) -> SuiteResult:
    results = [_task(f"fix-bug-{i}", "fix-bug", p) for i, p in enumerate(passes)]
    return SuiteResult(label=model, model=model, started_at=started, results=results, meta=meta)


def test_short_name_drops_provider_and_org() -> None:
    assert short_name("groq:openai/gpt-oss-120b") == "gpt-oss-120b"
    assert short_name("k2think:MBZUAI-IFM/K2-Think-v2") == "K2-Think-v2"
    assert short_name("ollama:qwen2.5-coder:7b") == "qwen2.5-coder:7b"


def test_talk_data_from_synthetic_suites() -> None:
    """Two models on the baseline: scores, token means and the node split come out as quoted."""
    suites = [
        _suite("groq:a/one", [True, True], 1.0, suite="inhouse", retrieval_mode="hybrid"),
        _suite("groq:b/two", [True, False], 2.0, suite="inhouse", retrieval_mode="hybrid"),
    ]
    data = talk_data(suites, {})
    assert [m["name"] for m in data["models"]] == ["one", "two"]
    assert data["models"][1]["passed"] == 1
    assert data["models"][0]["tokens"] == 1000
    assert data["stats"]["model0_score"] == "2 / 2"
    assert data["stats"]["model1_score"] == "1 / 2"
    # 890 of 1,000 tokens per run are in act.
    assert data["stats"]["act_share"] == "~89%"
    assert data["stats"]["plan_tokens"] == "110"
    assert data["retrieval"] == {}


def test_retrieval_section_reads_hybrid_with_and_without_rerank() -> None:
    summary = {
        "hybrid": {"tasks": 40, "recall@3": 0.9916, "mrr": 0.5625},
        "hybrid+rerank": {"tasks": 40, "recall@3": 0.9916, "mrr": 0.7625},
    }
    assert retrieval_section(summary) == {"tasks": 40, "recall3": 0.99, "mrr": 0.56, "mrr_rerank": 0.76}


def test_committed_data_matches_the_results_files() -> None:
    """`docs/talk/data.json` is what `evals/talk_data.py` writes from the committed results."""
    expected = talk_data(latest_results(), latest_retrieval_summary())
    committed = json.loads(TALK_DATA.read_text(encoding="utf-8"))
    assert committed == expected, "stale: run `uv run python evals/talk_data.py`"


def test_quoted_numbers_match_the_data() -> None:
    """Each `data-stat` element's fallback text is the value the script would swap in.

    The fallback is what a reader sees before the data loads (and what a search engine reads),
    so it must already be right.
    """
    stats = json.loads(TALK_DATA.read_text(encoding="utf-8"))["stats"]
    page = PAGE.read_text(encoding="utf-8")
    quoted = re.findall(r'data-stat="([\w]+)">([^<]*)<', page)
    assert quoted, "the page quotes no numbers from the data"
    wrong = [(key, text, stats.get(key)) for key, text in quoted if stats.get(key) != text]
    assert not wrong, f"page text differs from data.json (key, page, data): {wrong}"


def test_every_chart_on_the_page_has_a_drawer() -> None:
    """A `data-chart` name the script does not know would leave an empty box on a slide."""
    page = PAGE.read_text(encoding="utf-8")
    script = (TALK / "talk.js").read_text(encoding="utf-8")
    names = set(re.findall(r'data-chart="([\w-]+)"', page))
    assert names
    missing = [name for name in names if f'"{name}":' not in script and f"\n    {name}:" not in script]
    assert not missing, f"charts with no drawing function: {missing}"


def test_local_references_exist() -> None:
    """Stylesheets, scripts, the recording's frames and in-site links all resolve on disk."""
    page = PAGE.read_text(encoding="utf-8")
    refs = re.findall(r'(?:href|src|data-src)="([^"#]+)"', page)
    local = [ref for ref in refs if not re.match(r"^[a-z]+:", ref)]
    assert local
    missing = []
    for ref in local:
        target = (TALK / ref).resolve()
        # `../results/` is the results page as mkdocs publishes it: docs/results.md.
        if ref.endswith("/") and target != TALK.parent.resolve():
            if not target.with_suffix(".md").is_file() and not (target / "index.html").is_file():
                missing.append(ref)
        elif not ref.endswith("/") and not target.is_file():
            missing.append(ref)
    assert not missing, f"the talk points at files that do not exist: {missing}"


def test_the_docs_landing_page_links_to_the_talk() -> None:
    index = (ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    assert "talk/" in index, "the talk is not reachable from the docs site"
