"""Figures from eval results: pass-rate bars, iteration curve, cost profile.

The numbers are computed here from `SuiteResult` objects; drawing is a thin matplotlib layer on
top. Keeping the two apart means the arithmetic (which is what a reader of the README trusts)
is unit-tested without a display, and the charts can be regenerated after any change with one
command. Every figure is drawn from committed results files, never from a live run, so the PNG
in the README always corresponds to a JSON someone can open.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from coder_agent.evals.runner import RESULTS_DIR, SuiteResult, load_result
from coder_agent.evals.tasks import CATEGORIES
from coder_agent.telemetry.ledger import Ledger, Usage, merge_usage

FIGURES_DIR = Path(__file__).resolve().parents[3] / "docs" / "figures"

# Results written by the harness self-checks describe the grader, not the agent.
SELF_CHECK_MODELS = {"reference-solution", "noop"}

# The retrieval ablation compares the same agent under four retrieval settings. Fixed order and a
# fixed colour per mode, so the bars read the same in every regeneration of the figure.
ABLATION_MODES: tuple[str, ...] = ("off", "bm25", "dense", "hybrid")

# One light palette, applied by role. Categorical hues are used in this fixed order; the lighter
# blue is a step of the same ramp and marks the share of a bar that was never graded.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SOFT = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
NOT_GRADED = "#9ec5f4"


# --------------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------------


def latest_results(results_dir: Path = RESULTS_DIR) -> list[SuiteResult]:
    """Newest results file per label, oldest label first.

    A label names one configuration (model, suite); reruns of the same configuration overwrite
    older numbers rather than sitting next to them, so a chart compares configurations.
    """
    newest: dict[str, SuiteResult] = {}
    for path in sorted(results_dir.glob("*.json")):
        suite = load_result(path)
        if suite.model in SELF_CHECK_MODELS:
            continue
        if suite.label not in newest or suite.started_at >= newest[suite.label].started_at:
            newest[suite.label] = suite
    return sorted(newest.values(), key=lambda s: s.started_at)


def ablation_suites(suites: list[SuiteResult]) -> list[SuiteResult]:
    """The newest suite per retrieval mode, in `ABLATION_MODES` order; empty if none recorded one.

    A suite belongs to the ablation when its `meta` carries `retrieval_mode`, which `run_evals.py
    --retrieval <mode>` writes. Suites from before that flag existed have no mode and are not
    guessed at.
    """
    newest: dict[str, SuiteResult] = {}
    for suite in suites:
        mode = suite.meta.get("retrieval_mode")
        if mode not in ABLATION_MODES:
            continue
        if mode not in newest or suite.started_at >= newest[mode].started_at:
            newest[mode] = suite
    return [newest[m] for m in ABLATION_MODES if m in newest]


def ablation_rows(suites: list[SuiteResult]) -> list[dict[str, Any]]:
    """Per mode: pass rate, passed/total, and mean tokens per graded task."""
    rows = []
    for suite in ablation_suites(suites):
        graded = [r for r in suite.results if not r.error]
        tokens = sum(r.total_tokens for r in graded) / len(graded) if graded else 0.0
        rows.append(
            {
                "mode": suite.meta["retrieval_mode"],
                "label": suite.label,
                "tasks": len(suite.results),
                "passed": sum(r.passed for r in suite.results),
                "pass_rate": suite.pass_rate,
                "avg_tokens": tokens,
            }
        )
    return rows


def max_iterations(suite: SuiteResult) -> int:
    """The loop's configured ceiling, or the largest observed if the file predates `meta`."""
    observed = max((r.iterations for r in suite.results), default=1)
    return max(int(suite.meta.get("max_iterations", 0)), observed, 1)


def iteration_curve(suite: SuiteResult, k_max: int | None = None) -> list[float]:
    """Share of all tasks solved after at most k iterations, for k = 1..k_max.

    Cumulative on purpose: the curve answers "how much does another plan/act/test cycle buy?",
    and its final value equals the pass rate. Errored tasks count in the denominator, like they
    do in the pass rate, so the two figures agree.
    """
    k_max = k_max or max_iterations(suite)
    n = len(suite.results)
    if not n:
        return [0.0] * k_max
    return [
        sum(1 for r in suite.results if r.passed and r.iterations <= k) / n
        for k in range(1, k_max + 1)
    ]


def node_costs(suite: SuiteResult, ledger: Ledger | None = None) -> list[dict[str, Any]]:
    """Average tokens per node per run, over the runs that recorded usage.

    Usage comes from the results file; when a task predates the `usage` field the ledger is
    asked for the most recent run tagged with that task id. Errored runs that never called a
    model contribute nothing and are not counted, so the average describes a run that happened.
    """
    total: Usage = {}
    runs = 0
    for r in suite.results:
        usage = r.usage or (ledger.usage_for(task_id=r.task_id) if ledger else None)
        if not usage:
            continue
        runs += 1
        total = merge_usage(total, usage)
    rows = [
        {
            "node": node,
            "runs": runs,
            "calls": v.get("calls", 0) / runs,
            "input_tokens": v.get("input_tokens", 0) / runs,
            "output_tokens": v.get("output_tokens", 0) / runs,
        }
        for node, v in total.items()
    ]
    rows.sort(key=lambda row: row["input_tokens"] + row["output_tokens"], reverse=True)
    return rows


def category_rows(suite: SuiteResult) -> dict[str, dict[str, Any]]:
    """`by_category()` keyed by name, without the total row; the bar chart wants a lookup."""
    return {row["category"]: row for row in suite.by_category() if row["category"] != "total"}


# --------------------------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------------------------


def _figure(width: float, height: float):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans"],
            "font.size": 10,
            "text.color": INK,
            "axes.labelcolor": INK_SOFT,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "axes.edgecolor": AXIS,
            "axes.facecolor": SURFACE,
            "figure.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "legend.frameon": False,
            "legend.fontsize": 9,
        }
    )
    fig, ax = plt.subplots(figsize=(width, height), dpi=160)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0)
    return fig, ax


def _title(ax, title: str, subtitle: str) -> None:
    ax.set_title(title, loc="left", fontsize=12, fontweight="semibold", color=INK, pad=22)
    ax.text(0, 1.03, subtitle, transform=ax.transAxes, fontsize=9, color=INK_SOFT, va="bottom")


def _percent_axis(ax, axis: str = "y") -> None:
    from matplotlib.ticker import PercentFormatter

    target = ax.yaxis if axis == "y" else ax.xaxis
    target.set_major_formatter(PercentFormatter(1.0, decimals=0))
    target.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    (ax.set_ylim if axis == "y" else ax.set_xlim)(0, 1.05)


def draw_pass_rate(suites: list[SuiteResult], out: Path) -> Path:
    """Grouped bars: pass@1 per category, one group of bars per suite. The pale segment stacked
    on top is the share of tasks that errored before grading (quota, crash), so the eye can
    separate "the agent failed" from "the agent never ran"."""
    fig, ax = _figure(8, 4.2)
    cats = [c for c in CATEGORIES if any(c in category_rows(s) for s in suites)]
    width = 0.8 / max(len(suites), 1)
    for i, suite in enumerate(suites):
        rows = category_rows(suite)
        colour = SERIES[i % len(SERIES)]
        for j, cat in enumerate(cats):
            row = rows.get(cat)
            if not row:
                continue
            x = j - 0.4 + width * (i + 0.5)
            passed, errs = row["pass_rate"], row["errors"] / row["tasks"]
            ax.bar(
                x, passed, width * 0.9, color=colour, edgecolor=SURFACE, linewidth=1.5,
                label=suite.label if j == 0 else None,
            )
            if errs:
                ax.bar(
                    x, errs, width * 0.9, bottom=passed, color=NOT_GRADED, edgecolor=SURFACE,
                    linewidth=1.5, label="errored, not graded" if (i, j) == (0, 0) else None,
                )
            ax.text(
                x, passed + errs + 0.02, f"{row['passed']}/{row['tasks']}", ha="center",
                va="bottom", fontsize=8.5, color=INK_SOFT,
            )
    ax.set_xticks(range(len(cats)), cats)
    _percent_axis(ax)
    ax.spines["left"].set_visible(False)
    names = ", ".join(f"{s.label} ({s.model})" for s in suites)
    _title(ax, "pass@1 by task category", f"Hidden-test pass rate · {names}")
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.12), ncol=3)
    fig.tight_layout()
    fig.savefig(out)
    return out


def draw_iteration_curve(suites: list[SuiteResult], out: Path) -> Path:
    """Cumulative share of tasks solved after k plan/act/test cycles. Flat means more cycles
    would not have helped; still rising at the ceiling means `max_iterations` is binding."""
    fig, ax = _figure(6.5, 4)
    k_max = max(max_iterations(s) for s in suites)
    ks = list(range(1, k_max + 1))
    for i, suite in enumerate(suites):
        ys = iteration_curve(suite, k_max)
        colour = SERIES[i % len(SERIES)]
        ax.plot(
            ks, ys, color=colour, linewidth=2, marker="o", markersize=7,
            markeredgecolor=SURFACE, markeredgewidth=1.5, label=suite.label,
        )
        ax.text(ks[-1] + 0.08, ys[-1], f"{ys[-1]:.0%}", va="center", fontsize=9, color=INK_SOFT)
    ax.set_xticks(ks)
    ax.set_xlim(0.8, k_max + 0.5)
    ax.set_xlabel("iterations allowed")
    _percent_axis(ax)
    ax.spines["left"].set_visible(False)
    _title(ax, "Tasks solved within k iterations", "Cumulative; the last point is pass@1")
    if len(suites) > 1:
        ax.legend(loc="lower right")
    fig.tight_layout()
    fig.savefig(out)
    return out


def _kilo(value: float) -> str:
    """1234 -> "1.2k", 23456 -> "23k": one decimal only while it carries information."""
    return f"{value / 1000:.1f}k" if value < 10_000 else f"{value / 1000:.0f}k"


def draw_ablation(suites: list[SuiteResult], out: Path) -> Path:
    """Two panels, one axis each: pass@1 per retrieval mode, and mean tokens per task.

    Tokens sit in their own panel rather than on a second y-axis: two scales on one plot invite
    reading a crossing as meaningful. Bars keep one colour per mode across both panels so the eye
    can match them without a legend.
    """
    rows = ablation_rows(suites)
    fig, ax = _figure(8.5, 4)
    import matplotlib.pyplot as plt

    fig.clf()
    ax, ax2 = fig.subplots(1, 2, gridspec_kw={"width_ratios": [1.15, 1], "wspace": 0.35})
    colours = {mode: SERIES[i] for i, mode in enumerate(ABLATION_MODES)}
    xs = list(range(len(rows)))
    labels = [r["mode"] for r in rows]

    for panel in (ax, ax2):
        for side in ("top", "right", "left"):
            panel.spines[side].set_visible(False)
        panel.tick_params(length=0)
        panel.set_xticks(xs, labels)

    for x, row in zip(xs, rows, strict=True):
        colour = colours[row["mode"]]
        ax.bar(x, row["pass_rate"], 0.62, color=colour, edgecolor=SURFACE, linewidth=1.5)
        ax.text(
            x, row["pass_rate"] + 0.02, f"{row['passed']}/{row['tasks']}", ha="center",
            va="bottom", fontsize=9, color=INK_SOFT,
        )
        ax2.bar(x, row["avg_tokens"], 0.62, color=colour, edgecolor=SURFACE, linewidth=1.5)
        ax2.text(
            x, row["avg_tokens"], _kilo(row["avg_tokens"]), ha="center", va="bottom",
            fontsize=9, color=INK_SOFT,
        )
    _percent_axis(ax)
    ax2.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax2.set_axisbelow(True)
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _kilo(v)))
    ax2.set_ylim(0, max((r["avg_tokens"] for r in rows), default=1) * 1.2 or 1)
    models = sorted({s.model for s in ablation_suites(suites)})
    _title(ax, "pass@1 by retrieval mode", f"Same agent and tasks · {', '.join(models)}")
    _title(ax2, "tokens per task", "Mean over graded tasks, all model calls")
    fig.savefig(out, bbox_inches="tight")
    return out


def draw_cost_profile(suite: SuiteResult, out: Path, ledger: Ledger | None = None) -> Path:
    """Where the context budget goes: average input and output tokens per node per run."""
    rows = node_costs(suite, ledger)
    fig, ax = _figure(7, 1.6 + 0.55 * max(len(rows), 1))
    nodes = [r["node"] for r in rows]
    inp = [r["input_tokens"] for r in rows]
    outp = [r["output_tokens"] for r in rows]
    y = list(range(len(rows)))[::-1]
    ax.barh(y, inp, 0.6, color=SERIES[0], edgecolor=SURFACE, linewidth=1.5, label="input tokens")
    ax.barh(
        y, outp, 0.6, left=inp, color=SERIES[1], edgecolor=SURFACE, linewidth=1.5,
        label="output tokens",
    )
    for yi, row, i_tok, o_tok in zip(y, rows, inp, outp):
        ax.text(
            i_tok + o_tok, yi, f"  {i_tok + o_tok:,.0f}  ({row['calls']:.1f} calls)",
            va="center", fontsize=9, color=INK_SOFT,
        )
    ax.set_yticks(y, nodes)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.spines["left"].set_visible(False)
    ax.set_xlim(0, max([a + b for a, b in zip(inp, outp)], default=1) * 1.35)
    ax.xaxis.set_major_formatter(lambda v, _pos: f"{v / 1000:.0f}k" if v else "0")
    runs = rows[0]["runs"] if rows else 0
    _title(
        ax, "Tokens per run, by graph node",
        f"Average over {runs} run(s) · {suite.label} ({suite.model})",
    )
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.18), ncol=2)
    fig.tight_layout()
    fig.savefig(out)
    return out


def render_all(
    suites: list[SuiteResult], out_dir: Path = FIGURES_DIR, ledger: Ledger | None = None
) -> list[Path]:
    """The three standing figures, plus the retrieval ablation once at least two modes have
    results. The cost profile describes the newest suite only: stacking several configurations'
    node costs into one chart would hide which one the budget belongs to."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [
        draw_pass_rate(suites, out_dir / "pass_rate.png"),
        draw_iteration_curve(suites, out_dir / "iteration_curve.png"),
        draw_cost_profile(suites[-1], out_dir / "cost_profile.png", ledger),
    ]
    if len(ablation_suites(suites)) >= 2:
        paths.append(draw_ablation(suites, out_dir / "retrieval_ablation.png"))
    return paths
