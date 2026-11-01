"""The numbers behind the web talk in `docs/talk/`, as one JSON file the page draws its charts from.

The talk's charts are drawn in the browser, not pasted in as PNGs, so they stay sharp at any
size and can animate in. The numbers still come from the same place as the README figures: the
committed results files, read through the same selection functions in `figures.py` (which run is
the baseline, which arms make up the ablation). That way the PNGs on the results page and the
charts in the talk cannot disagree about which runs they describe.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coder_agent.evals.figures import (
    ablation_suites,
    category_rows,
    comparison_suites,
    headline_suites,
    iteration_curve,
    node_costs,
    profiled_suite,
)
from coder_agent.evals.runner import RESULTS_DIR, SuiteResult

TALK_DATA = Path(__file__).resolve().parents[3] / "docs" / "talk" / "data.json"


def short_name(model: str) -> str:
    """`groq:openai/gpt-oss-120b` -> `gpt-oss-120b`: the provider and the org are noise on a slide."""
    return model.split(":", 1)[-1].rsplit("/", 1)[-1]


def _graded(suite: SuiteResult) -> list:
    """Tasks that ran to a verdict; a crashed run says nothing about tokens or time."""
    return [r for r in suite.results if not r.error]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _passed(suite: SuiteResult) -> int:
    return sum(r.passed for r in suite.results)


def model_section(suites: list[SuiteResult]) -> list[dict[str, Any]]:
    """One row per model on the baseline configuration: the model comparison chart."""
    rows = []
    for suite in comparison_suites(suites):
        graded = _graded(suite)
        rows.append(
            {
                "name": short_name(suite.model),
                "passed": _passed(suite),
                "tasks": len(suite.results),
                "tokens": round(_mean([r.total_tokens for r in graded])),
                "seconds": round(_mean([r.agent_seconds for r in graded]), 1),
                "steps": round(_mean([r.steps for r in graded]), 1),
            }
        )
    return rows


def headline_name(suite: SuiteResult) -> str:
    """A series name that says which suite it ran, since HumanEval and in-house share a model."""
    name = short_name(suite.model)
    return f"{name} · HumanEval" if suite.meta.get("suite") == "humaneval" else name


def category_section(suites: list[SuiteResult]) -> dict[str, Any]:
    """Passed / total per task category per headline run: the grouped pass-rate chart.

    Categories are listed in the order they first appear, so the in-house categories come
    first and HumanEval, which only one run has, comes last.
    """
    headline = headline_suites(suites)
    categories: list[str] = []
    series = []
    for suite in headline:
        rows = category_rows(suite)
        for name in rows:
            if name not in categories:
                categories.append(name)
        series.append(
            {
                "name": headline_name(suite),
                "values": {name: [row["passed"], row["tasks"]] for name, row in rows.items()},
            }
        )
    return {"categories": categories, "series": series}


def iteration_section(suites: list[SuiteResult]) -> list[dict[str, Any]]:
    """Share of tasks solved within k iterations, per headline run: the iteration curve."""
    return [
        {"name": headline_name(s), "curve": [round(v, 4) for v in iteration_curve(s)]}
        for s in headline_suites(suites)
    ]


def ablation_section(suites: list[SuiteResult]) -> dict[str, Any]:
    """Pass rate and tokens per retrieval mode, all arms on one model: the ablation chart."""
    arms = ablation_suites(suites)
    return {
        "model": short_name(arms[0].model) if arms else "",
        "arms": [
            {
                "mode": s.meta["retrieval_mode"],
                "passed": _passed(s),
                "tasks": len(s.results),
                "tokens": round(_mean([r.total_tokens for r in _graded(s)])),
            }
            for s in arms
        ],
    }


def node_section(suites: list[SuiteResult]) -> dict[str, Any]:
    """Average tokens per graph node per run, for the one profiled suite: where the tokens go."""
    suite = profiled_suite(suites)
    rows = node_costs(suite)
    return {
        "model": short_name(suite.model),
        "runs": rows[0]["runs"] if rows else 0,
        "nodes": [
            {
                "node": r["node"],
                "input": round(r["input_tokens"]),
                "output": round(r["output_tokens"]),
                # Rounded from the exact sum, so it matches the cost-profile PNG to the token.
                "total": round(r["input_tokens"] + r["output_tokens"]),
                "calls": round(r["calls"], 1),
            }
            for r in rows
        ],
    }


def latest_retrieval_summary(results_dir: Path = RESULTS_DIR) -> dict[str, dict[str, float]]:
    """recall@k and MRR per mode from the newest `retrieval_eval.py` file, keyed by mode."""
    newest: dict[str, Any] | None = None
    for path in sorted(results_dir.glob("*-retrieval.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "summary" in data:
            newest = data  # names start with a timestamp, so the last one wins
    if newest is None:
        return {}
    return {row["mode"]: row for row in newest["summary"]}


def retrieval_section(summary: dict[str, dict[str, float]]) -> dict[str, float]:
    """The three retrieval numbers the talk quotes: recall@3, and MRR without and with rerank."""
    if "hybrid" not in summary:
        return {}
    plain = summary["hybrid"]
    reranked = summary.get("hybrid+rerank", plain)
    return {
        "tasks": plain["tasks"],
        "recall3": round(plain["recall@3"], 2),
        "mrr": round(plain["mrr"], 2),
        "mrr_rerank": round(reranked["mrr"], 2),
    }


def stats_section(data: dict[str, Any]) -> dict[str, str]:
    """The figures quoted in the talk's prose, already formatted as the page shows them.

    The page carries each one as fallback text and swaps in the value from here, and a test
    checks the two match, so a sentence on a slide cannot keep quoting an old run.
    """
    stats: dict[str, str] = {}
    for i, row in enumerate(data["models"]):
        stats[f"model{i}_score"] = f"{row['passed']} / {row['tasks']}"
        stats[f"model{i}_name"] = row["name"]
    for series in data["categories"]["series"]:
        if "humaneval" in series["values"]:
            passed, tasks = series["values"]["humaneval"]
            stats["humaneval_score"] = f"{passed} / {tasks}"
    nodes = data["nodes"]["nodes"]
    total = sum(n["total"] for n in nodes)
    act = next((n for n in nodes if n["node"] == "act"), None)
    plan = next((n for n in nodes if n["node"] == "plan"), None)
    if act and total:
        stats["act_share"] = f"~{round(100 * act['total'] / total)}%"
        stats["act_calls"] = f"{act['calls']:.1f}"
    if plan:
        stats["plan_tokens"] = f"{plan['total']:,}"
    retrieval = data["retrieval"]
    if retrieval:
        stats["recall3"] = f"{retrieval['recall3']:.2f}"
        stats["mrr"] = f"{retrieval['mrr']:.2f}"
        stats["mrr_rerank"] = f"{retrieval['mrr_rerank']:.2f}"
    return stats


def talk_data(suites: list[SuiteResult], retrieval: dict[str, dict[str, float]]) -> dict[str, Any]:
    """Everything the talk draws, in one plain dictionary ready for `json.dumps`."""
    data: dict[str, Any] = {
        "models": model_section(suites),
        "categories": category_section(suites),
        "iterations": iteration_section(suites),
        "ablation": ablation_section(suites),
        "nodes": node_section(suites),
        "retrieval": retrieval_section(retrieval),
    }
    data["stats"] = stats_section(data)
    return data
