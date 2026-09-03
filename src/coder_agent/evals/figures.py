"""Figures from eval results: pass-rate bars, iteration curve, cost profile, two comparisons.

The numbers are computed here from `SuiteResult` objects; drawing is a thin matplotlib layer on
top. Keeping the two apart means the arithmetic (which is what a reader of the README trusts)
is unit-tested without a display, and the charts can be regenerated after any change with one
command. Every figure is drawn from committed results files, never from a live run, so the PNG
in the README always corresponds to a JSON someone can open.
"""

from __future__ import annotations

import json
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

# The model comparison holds everything else fixed: the in-house suite, the default retrieval mode.
# Results files from before `meta["suite"]` and `meta["retrieval_mode"]` were written are all
# in-house runs in the default mode, so a missing key counts as the default.
COMPARISON_SUITE = "inhouse"
COMPARISON_RETRIEVAL = "hybrid"


def is_full_suite(suite: SuiteResult) -> bool:
    """False for a `run_evals.py` run that only did the quick subset.

    The quick subset exists so an experiment costs a fraction of the daily free tier, which makes
    it the wrong thing to draw: four tasks and twelve tasks produce pass rates that look alike and
    mean different things. Results written before the flag existed carry no `subset` and were all
    full runs.
    """
    return suite.meta.get("subset", "full") == "full"

# One light palette, applied by role: a deep navy, a bright royal blue and a teal on a white card,
# with every non-data element a step of the same blue-grey. Categorical hues are used in this
# fixed order, never cycled by rank; the pale blue marks the share of a bar that was never graded.
SURFACE = "#f5f8fc"       # page behind the card
CARD = "#ffffff"          # the card the chart sits on
CARD_EDGE = "#e6ecf5"     # its hairline border
CHIP = "#eaf0f7"          # badge and callout fills
GRID = "#d8e1ec"          # dashed horizontal guides
INK = "#102a56"           # title and value labels
INK_SOFT = "#40557d"      # subtitles and axis labels
MUTED = "#7c8da8"         # tick labels
SERIES = ["#1e4a8a", "#2f80ed", "#1fb8a6", "#6b5bd2", "#e8973a", "#d9536f", "#0f766e", "#9333ea"]
NOT_GRADED = "#c7d8ef"

SERIF = ["Georgia", "Times New Roman", "DejaVu Serif"]
SANS = ["Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans"]


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
        if not is_suite_file(path):
            continue  # the retrieval eval writes its own JSON into the same directory
        suite = load_result(path)
        if suite.model in SELF_CHECK_MODELS or not is_full_suite(suite):
            continue
        if suite.label not in newest or suite.started_at >= newest[suite.label].started_at:
            newest[suite.label] = suite
    return sorted(newest.values(), key=lambda s: s.started_at)


def is_suite_file(path: Path) -> bool:
    """True for a `run_evals.py` results file, which alone carries a label and a model."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and {"label", "model", "results"} <= data.keys()


def ablation_suites(suites: list[SuiteResult]) -> list[SuiteResult]:
    """The newest suite per retrieval mode for one model, in `ABLATION_MODES` order.

    A suite belongs to the ablation when its `meta` carries `retrieval_mode`, which `run_evals.py`
    writes for every run (a plain run is the hybrid arm), and it ran the in-house suite. Suites
    from before that key existed have no mode and are not guessed at. The arms must come from
    one model, or the figure compares models as much as retrieval: the model with the most arms
    is chosen, the newest run on a tie, and other models' arms are left out even when they are
    more recent.
    """
    by_model: dict[str, dict[str, SuiteResult]] = {}
    for suite in suites:
        mode = suite.meta.get("retrieval_mode")
        if mode not in ABLATION_MODES or not is_full_suite(suite):
            continue
        if suite.meta.get("suite", COMPARISON_SUITE) != COMPARISON_SUITE:
            continue  # a HumanEval run in hybrid mode is not the hybrid arm of this ablation
        arms = by_model.setdefault(suite.model, {})
        if mode not in arms or suite.started_at >= arms[mode].started_at:
            arms[mode] = suite
    if not by_model:
        return []
    newest = {model: max(s.started_at for s in arms.values()) for model, arms in by_model.items()}
    chosen = max(by_model, key=lambda m: (len(by_model[m]), newest[m]))
    return [by_model[chosen][m] for m in ABLATION_MODES if m in by_model[chosen]]


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


def comparison_suites(suites: list[SuiteResult]) -> list[SuiteResult]:
    """The newest suite per model in the baseline configuration, oldest model first.

    Ablation arms other than hybrid, HumanEval runs and self-checks are left out: a model
    comparison is only fair when the model is the one thing that changed. Suites are keyed by
    `model` (the "provider:model" string), not by label, so a rerun of the same model replaces
    its older numbers the way `latest_results` does per label.
    """
    newest: dict[str, SuiteResult] = {}
    for suite in suites:
        if suite.model in SELF_CHECK_MODELS or not is_full_suite(suite):
            continue
        if suite.meta.get("suite", COMPARISON_SUITE) != COMPARISON_SUITE:
            continue
        if suite.meta.get("retrieval_mode", COMPARISON_RETRIEVAL) != COMPARISON_RETRIEVAL:
            continue
        if suite.model not in newest or suite.started_at >= newest[suite.model].started_at:
            newest[suite.model] = suite
    return sorted(newest.values(), key=lambda s: s.started_at)


def headline_suites(suites: list[SuiteResult]) -> list[SuiteResult]:
    """The suites the pass-rate and iteration figures draw: every full run in the default
    retrieval mode, whatever the model or the task suite, oldest first.

    Ablation arms are left out because they exist to be compared with each other in their own
    figure; drawn here they would stand next to the baseline as if they were other agents. What
    remains is one bar group per (model, suite): the in-house baseline per model and the
    HumanEval slice per model, which is what a reader means by "the pass rate".
    """
    return [
        s
        for s in suites
        if s.model not in SELF_CHECK_MODELS
        and is_full_suite(s)
        and s.meta.get("retrieval_mode", COMPARISON_RETRIEVAL) == COMPARISON_RETRIEVAL
    ]


def model_rows(suites: list[SuiteResult]) -> list[dict[str, Any]]:
    """Per model: pass rate, passed/total, mean tokens and mean wall seconds per graded task.

    Seconds are reported next to tokens because they answer different questions for a local
    model: tokens are free on this machine, so what a reader wants to know is how much slower
    the run is than the hosted provider, not how much it cost.
    """
    rows = []
    for suite in comparison_suites(suites):
        graded = [r for r in suite.results if not r.error]
        n = len(graded) or 1
        rows.append(
            {
                "model": suite.model,
                "label": suite.label,
                "tasks": len(suite.results),
                "passed": sum(r.passed for r in suite.results),
                "errors": suite.errors,
                "pass_rate": suite.pass_rate,
                "avg_tokens": sum(r.total_tokens for r in graded) / n,
                "avg_seconds": sum(r.agent_seconds for r in graded) / n,
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
#
# Every figure is one card: a rounded white panel with a hairline border, a header (badge, title,
# subtitle, and a callout that says which direction is good), the plot, and direct labels on the
# marks. Layout is done in inches rather than by `tight_layout`, because the decorations - the
# card, the badge, the category icons - are drawn in a transparent overlay axes whose coordinates
# *are* inches, so a circle stays a circle and a margin means the same thing in every figure.

PAD = 0.14  # figure edge to card edge, inches
INSET = 0.30  # card edge to its contents


def _plt():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": SANS,
            "font.serif": SERIF,
            "font.size": 10,
            "text.color": INK,
            "axes.labelcolor": INK_SOFT,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "axes.facecolor": "none",
            "figure.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "legend.frameon": False,
            "legend.fontsize": 9,
        }
    )
    return plt


def _canvas(width: float, height: float):
    """A figure whose only content so far is the card, plus an overlay axes measured in inches."""
    from matplotlib.patches import FancyBboxPatch

    plt = _plt()
    fig = plt.figure(figsize=(width, height), dpi=170)
    deco = fig.add_axes([0, 0, 1, 1])
    deco.set_xlim(0, width)
    deco.set_ylim(0, height)
    deco.set_axis_off()
    deco.patch.set_alpha(0)
    deco.add_patch(
        FancyBboxPatch(
            (PAD, PAD), width - 2 * PAD, height - 2 * PAD,
            boxstyle="round,pad=0,rounding_size=0.16",
            facecolor=CARD, edgecolor=CARD_EDGE, linewidth=1.0,
        )
    )
    return fig, deco


def _header(
    deco, width: float, height: float, title: str, subtitle: str,
    callout: tuple[str, str] | None = None, size: float = 15.5,
) -> None:
    """Title and subtitle on the left; the callout chip, if any, on the right.

    The callout says which way is better and what the numbers on the marks mean, so a reader who
    only looks at the picture is not left inferring the direction of the axis."""
    from matplotlib.patches import FancyBboxPatch

    top = height - PAD - INSET
    tx = PAD + INSET
    deco.text(tx, top - 0.20, title, ha="left", va="center", family="serif", fontsize=size,
              color=INK)
    deco.text(tx, top - 0.53, subtitle, ha="left", va="center", fontsize=8.8, color=INK_SOFT)
    if not callout:
        return
    lead, note = callout
    box_w = 0.30 + 0.062 * max(len(lead), len(note))
    box_h = 0.62
    cx = width - PAD - INSET - box_w
    deco.add_patch(
        FancyBboxPatch(
            (cx, top - box_h - 0.02), box_w, box_h, boxstyle="round,pad=0,rounding_size=0.10",
            facecolor=CHIP, edgecolor="none",
        )
    )
    deco.text(cx + 0.16, top - 0.23, lead, ha="left", va="center", fontsize=8.8,
              color=SERIES[1], fontweight="semibold")
    deco.text(cx + 0.16, top - 0.45, note, ha="left", va="center", fontsize=7.8, color=INK_SOFT)


def _axes(fig, width: float, height: float, left: float, right: float, top: float, bottom: float):
    """One plotting axes placed by inch margins, styled down to grid and baseline."""
    ax = fig.add_axes(
        [left / width, bottom / height, 1 - (left + right) / width, 1 - (top + bottom) / height]
    )
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.spines["bottom"].set_linewidth(1.0)
    ax.tick_params(length=0, labelsize=9)
    ax.set_facecolor("none")
    ax.set_axisbelow(True)
    return ax


def _grid(ax, axis: str = "y") -> None:
    (ax.yaxis if axis == "y" else ax.xaxis).grid(
        True, color=GRID, linewidth=0.9, linestyle=(0, (4, 4))
    )


def _percent_axis(ax, axis: str = "y") -> None:
    from matplotlib.ticker import PercentFormatter

    target = ax.yaxis if axis == "y" else ax.xaxis
    target.set_major_formatter(PercentFormatter(1.0, decimals=0))
    _grid(ax, axis)
    (ax.set_ylim if axis == "y" else ax.set_xlim)(0, 1.05)


def _radius(ax, points: float) -> tuple[float, float]:
    """`points` of corner radius expressed in data units on each axis.

    Bars are drawn as paths rather than `ax.bar` rectangles so the end that carries the value can
    be rounded. The radius has to be the same number of pixels on both axes or the corner reads
    as an ellipse, which means converting through the axes transform - so the limits must be
    final before a bar is drawn. Every figure here sets them first for that reason."""
    pixels = points * ax.figure.dpi / 72
    inv = ax.transData.inverted()
    (x0, y0), (x1, y1) = inv.transform([(0, 0), (pixels, pixels)])
    return abs(x1 - x0), abs(y1 - y0)


def _bar(ax, x: float, height: float, width: float, colour: str, bottom: float = 0.0,
         radius: float = 5.0, round_end: bool = True):
    """A vertical bar with its top corners rounded and its base square on the baseline."""
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MPath

    if height <= 0:
        return None
    x0, x1 = x - width / 2, x + width / 2
    y0, y1 = bottom, bottom + height
    rx, ry = _radius(ax, radius) if round_end else (0.0, 0.0)
    rx, ry = min(rx, width / 2), min(ry, height)
    verts = [
        (x0, y0), (x0, y1 - ry), (x0, y1), (x0 + rx, y1),
        (x1 - rx, y1), (x1, y1), (x1, y1 - ry), (x1, y0), (x0, y0),
    ]
    codes = [
        MPath.MOVETO, MPath.LINETO, MPath.CURVE3, MPath.CURVE3,
        MPath.LINETO, MPath.CURVE3, MPath.CURVE3, MPath.LINETO, MPath.CLOSEPOLY,
    ]
    patch = PathPatch(MPath(verts, codes), facecolor=colour, edgecolor="none", zorder=3)
    ax.add_patch(patch)
    return patch


def _hbar(ax, y: float, length: float, thickness: float, colour: str, left: float = 0.0,
          radius: float = 5.0, round_end: bool = True):
    """The same shape lying down: rounded at the far end, square where it meets the axis."""
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MPath

    if length <= 0:
        return None
    y0, y1 = y - thickness / 2, y + thickness / 2
    x0, x1 = left, left + length
    rx, ry = _radius(ax, radius) if round_end else (0.0, 0.0)
    rx, ry = min(rx, length), min(ry, thickness / 2)
    verts = [
        (x0, y0), (x1 - rx, y0), (x1, y0), (x1, y0 + ry),
        (x1, y1 - ry), (x1, y1), (x1 - rx, y1), (x0, y1), (x0, y0),
    ]
    codes = [
        MPath.MOVETO, MPath.LINETO, MPath.CURVE3, MPath.CURVE3,
        MPath.LINETO, MPath.CURVE3, MPath.CURVE3, MPath.LINETO, MPath.CLOSEPOLY,
    ]
    patch = PathPatch(MPath(verts, codes), facecolor=colour, edgecolor="none", zorder=3)
    ax.add_patch(patch)
    return patch


def _value(ax, x: float, y: float, text: str, dy: float = 0.02) -> None:
    ax.text(x, y + dy, text, ha="center", va="bottom", fontsize=8.8, color=INK,
            fontweight="semibold")


def _legend(deco, width: float, y: float, entries: list[tuple[str, str]]) -> None:
    """A centred row of rounded swatches in a bordered pill, matching the header chips.

    Drawn by hand rather than with `ax.legend` because the swatch is a rounded rectangle and the
    row has to sit at a known height inside the card; widths are estimated from the label length,
    which is close enough for centring."""
    from matplotlib.patches import FancyBboxPatch

    swatch, gap, lead = 0.30, 0.42, 0.14
    widths = [swatch + lead + 0.058 * len(label) for _, label in entries]
    total = sum(widths) + gap * (len(entries) - 1)
    box_h = 0.42
    x = (width - total) / 2
    deco.add_patch(
        FancyBboxPatch(
            (x - 0.28, y - box_h / 2), total + 0.56, box_h,
            boxstyle="round,pad=0,rounding_size=0.10",
            facecolor=CARD, edgecolor=CARD_EDGE, linewidth=1.0,
        )
    )
    for (colour, label), entry_w in zip(entries, widths, strict=True):
        deco.add_patch(
            FancyBboxPatch(
                (x, y - 0.075), swatch, 0.15, boxstyle="round,pad=0,rounding_size=0.055",
                facecolor=colour, edgecolor="none",
            )
        )
        deco.text(x + swatch + lead, y, label, ha="left", va="center", fontsize=8.6, color=INK)
        x += entry_w + gap


# -- category glyphs ---------------------------------------------------------------------------
# Six line drawings, one per task category, sitting between the axis and its labels. They are
# built from primitives in the inch-measured overlay, so they never depend on an icon font being
# installed on the machine that regenerates the figures.


def _stroke(deco, xs, ys, linewidth: float = 1.2) -> None:
    from matplotlib.lines import Line2D

    deco.add_line(Line2D(xs, ys, color=INK, linewidth=linewidth, solid_capstyle="round"))


def _ring(deco, cx: float, cy: float, r: float, linewidth: float = 1.2) -> None:
    from matplotlib.patches import Circle

    deco.add_patch(Circle((cx, cy), r, facecolor="none", edgecolor=INK, linewidth=linewidth))


def _icon_bug(deco, cx, cy, s):
    from matplotlib.patches import Ellipse

    deco.add_patch(
        Ellipse((cx, cy - 0.1 * s), 1.15 * s, 1.5 * s, facecolor="none", edgecolor=INK,
                linewidth=1.2)
    )
    _ring(deco, cx, cy + 0.85 * s, 0.3 * s)
    _stroke(deco, [cx - 0.25 * s, cx - 0.45 * s], [cy + 1.05 * s, cy + 1.35 * s])
    _stroke(deco, [cx + 0.25 * s, cx + 0.45 * s], [cy + 1.05 * s, cy + 1.35 * s])
    for dy in (0.35, 0.0, -0.35):
        _stroke(deco, [cx - 0.58 * s, cx - 1.0 * s], [cy + dy * s, cy + (dy + 0.2) * s])
        _stroke(deco, [cx + 0.58 * s, cx + 1.0 * s], [cy + dy * s, cy + (dy + 0.2) * s])


def _icon_plus(deco, cx, cy, s):
    _ring(deco, cx, cy, 0.95 * s)
    _stroke(deco, [cx - 0.45 * s, cx + 0.45 * s], [cy, cy])
    _stroke(deco, [cx, cx], [cy - 0.45 * s, cy + 0.45 * s])


def _icon_refresh(deco, cx, cy, s):
    from matplotlib.patches import Arc, Polygon

    deco.add_patch(
        Arc((cx, cy), 1.7 * s, 1.7 * s, theta1=45, theta2=340, edgecolor=INK, linewidth=1.2)
    )
    tx, ty = cx + 0.85 * s * 0.71, cy + 0.85 * s * 0.71  # the open end, at 45 degrees
    deco.add_patch(
        Polygon(
            [(tx - 0.34 * s, ty + 0.10 * s), (tx + 0.30 * s, ty + 0.22 * s),
             (tx + 0.10 * s, ty - 0.36 * s)],
            closed=True, facecolor=INK, edgecolor="none",
        )
    )


def _icon_doc(deco, cx, cy, s):
    from matplotlib.patches import FancyBboxPatch

    deco.add_patch(
        FancyBboxPatch(
            (cx - 0.62 * s, cy - 0.9 * s), 1.24 * s, 1.8 * s,
            boxstyle="round,pad=0,rounding_size=0.012", facecolor="none", edgecolor=INK,
            linewidth=1.2,
        )
    )
    for dy in (0.42, 0.06, -0.3):
        _stroke(deco, [cx - 0.34 * s, cx + 0.34 * s], [cy + dy * s, cy + dy * s], linewidth=1.0)


def _icon_layers(deco, cx, cy, s):
    from matplotlib.patches import Polygon

    for i, dy in enumerate((0.62, 0.0, -0.62)):
        deco.add_patch(
            Polygon(
                [(cx, cy + (dy + 0.34) * s), (cx + 1.0 * s, cy + dy * s),
                 (cx, cy + (dy - 0.34) * s), (cx - 1.0 * s, cy + dy * s)],
                closed=True, facecolor=INK if i == 0 else "none", edgecolor=INK, linewidth=1.1,
            )
        )


def _icon_brain(deco, cx, cy, s):
    """A ring around a wave: a plus or a pair of arcs at this size just reads as a cross."""
    import math

    _ring(deco, cx, cy, 0.95 * s)
    xs = [cx + (-0.6 + 1.2 * k / 24) * s for k in range(25)]
    ys = [cy + 0.30 * s * math.sin(2 * math.pi * (k / 24) * 1.5) for k in range(25)]
    _stroke(deco, xs, ys, linewidth=1.1)


# A marker per suite, and the dash a suite falls back to when its curve repeats an earlier one.
MARKERS = ["o", "s", "D", "^"]
REPEAT_DASH = (0, (6, 4))

CATEGORY_ICONS = {
    "fix-bug": _icon_bug,
    "add-feature": _icon_plus,
    "refactor": _icon_refresh,
    "add-test": _icon_doc,
    "multi-file": _icon_layers,
    "humaneval": _icon_brain,
}


def _inches(fig, ax, x: float, y: float) -> tuple[float, float]:
    """A point in data coordinates, as inches on the overlay axes."""
    px, py = ax.transData.transform((x, y))
    return px / fig.dpi, py / fig.dpi


# --------------------------------------------------------------------------------------------


def draw_pass_rate(suites: list[SuiteResult], out: Path) -> Path:
    """Grouped bars: pass@1 per category, one group of bars per suite. The pale segment stacked
    on top is the share of tasks that errored before grading (quota, crash), so the eye can
    separate "the agent failed" from "the agent never ran"."""
    cats = [c for c in CATEGORIES if any(c in category_rows(s) for s in suites)]
    width, height = 11.0, 5.2
    fig, deco = _canvas(width, height)
    models = ", ".join(sorted({s.model for s in suites}))
    _header(
        deco, width, height, "pass@1 by task category", f"Hidden-test pass rate · {models}",
        callout=("Higher is better", "Numbers show passes / total"),
    )
    ax = _axes(fig, width, height, left=1.0, right=0.45, top=1.20, bottom=1.65)
    ax.set_xlim(-0.6, len(cats) - 0.4)
    ax.set_xticks(range(len(cats)), cats)
    _percent_axis(ax)
    ax.set_ylabel("pass rate", fontsize=9)
    ax.tick_params(axis="x", pad=28)

    slot = 0.78 / max(len(suites), 1)
    errored = False
    for i, suite in enumerate(suites):
        rows = category_rows(suite)
        colour = SERIES[i % len(SERIES)]
        for j, cat in enumerate(cats):
            row = rows.get(cat)
            if not row:
                continue  # a HumanEval run has no fix-bug bar
            x = j - 0.39 + slot * (i + 0.5)
            passed, errs = row["pass_rate"], row["errors"] / row["tasks"]
            _bar(ax, x, passed, slot * 0.88, colour, round_end=not errs)
            if errs:
                _bar(ax, x, errs, slot * 0.88, NOT_GRADED, bottom=passed)
                errored = True
            _value(ax, x, passed + errs, f"{row['passed']}/{row['tasks']}")

    for j, cat in enumerate(cats):
        icon = CATEGORY_ICONS.get(cat)
        if icon:
            cx, cy = _inches(fig, ax, j, 0)
            icon(deco, cx, cy - 0.23, 0.10)

    entries = [(SERIES[i % len(SERIES)], s.label) for i, s in enumerate(suites)]
    if errored:
        entries.append((NOT_GRADED, "errored, not graded"))
    _legend(deco, width, PAD + INSET + 0.13, entries)
    fig.savefig(out)
    _plt().close(fig)
    return out


def draw_iteration_curve(suites: list[SuiteResult], out: Path) -> Path:
    """Cumulative share of tasks solved after k plan/act/test cycles. Flat means more cycles
    would not have helped; still rising at the ceiling means `max_iterations` is binding."""
    width, height = 8.0, 4.8
    fig, deco = _canvas(width, height)
    _header(
        deco, width, height, "Tasks solved within k iterations",
        "Cumulative; the last point is pass@1",
        callout=("Higher is better", "Flat means more cycles do not help"),
    )
    ax = _axes(fig, width, height, left=1.0, right=0.85, top=1.20,
               bottom=1.30 if len(suites) > 1 else 0.85)
    k_max = max(max_iterations(s) for s in suites)
    ks = list(range(1, k_max + 1))
    ax.set_xticks(ks)
    ax.set_xlim(0.75, k_max + 0.35)
    ax.set_xlabel("iterations allowed", fontsize=9)
    _percent_axis(ax)
    ax.set_ylabel("tasks solved", fontsize=9)
    # Two suites can trace exactly the same curve - a full pass rate at every k is common on a
    # small suite - and the one drawn last would hide the other entirely. A repeated curve is
    # drawn dashed so the line underneath shows through; distinct curves stay solid.
    drawn: list[list[float]] = []
    ends: dict[float, str] = {}  # final value -> the colour of the only suite that reaches it
    for i, suite in enumerate(suites):
        ys = iteration_curve(suite, k_max)
        colour = SERIES[i % len(SERIES)]
        style = REPEAT_DASH if ys in drawn else "solid"
        drawn.append(ys)
        ax.plot(ks, ys, color=colour, linewidth=2.2, linestyle=style,
                marker=MARKERS[i % len(MARKERS)], markersize=8, markeredgecolor=CARD,
                markeredgewidth=1.8, zorder=3 + i, label=suite.label)
        # One label per distinct end value, in ink when it belongs to more than one suite:
        # stacking two identical percentages would just render the text twice.
        ends[ys[-1]] = colour if ys[-1] not in ends else INK
    for value, colour in ends.items():
        ax.text(ks[-1] + 0.14, value, f"{value:.0%}", va="center", fontsize=9, color=colour,
                fontweight="semibold")
    if len(suites) > 1:
        _legend(deco, width, PAD + INSET + 0.13,
                [(SERIES[i % len(SERIES)], s.label) for i, s in enumerate(suites)])
    fig.savefig(out)
    _plt().close(fig)
    return out


def _kilo(value: float) -> str:
    """1234 -> "1.2k", 23456 -> "23k": one decimal only while it carries information."""
    if value < 1:
        return "0"
    return f"{value / 1000:.1f}k" if value < 10_000 else f"{value / 1000:.0f}k"


def _bar_panels(
    rows: list[dict[str, Any]],
    labels: list[str],
    colours: list[str],
    title: str,
    subtitle: str,
    panels: list[tuple[str, str, str]],
    out: Path,
    callout: tuple[str, str] | None = None,
) -> Path:
    """One card, one row of bar panels, the same bar colour per row across every panel.

    `panels` lists (title, subtitle, key); the "pass_rate" key gets a percent axis and a
    passed/total annotation, every other key a compact count axis. Each quantity sits in its own
    panel rather than on a second y-axis: two scales on one plot invite reading a crossing as
    meaningful. Colour, plus the label under every bar, ties a model or mode across the panels.
    """
    plt = _plt()
    n = len(panels)
    width, height = 0.9 + 3.6 * n, 4.8
    fig, deco = _canvas(width, height)
    _header(deco, width, height, title, subtitle, callout=callout)

    left, right, gap = 0.95, 0.45, 0.9
    span = (width - left - right - gap * (n - 1)) / n
    xs = list(range(len(rows)))
    for i, (panel_title, panel_sub, key) in enumerate(panels):
        panel_left = left + i * (span + gap)
        ax = _axes(fig, width, height, left=panel_left, right=width - panel_left - span,
                   top=1.80, bottom=0.85)
        ax.set_xlim(-0.7, len(rows) - 0.3)
        ax.set_xticks(xs, labels)
        if key == "pass_rate":
            _percent_axis(ax)
        else:
            ax.set_ylim(0, max((r[key] for r in rows), default=1) * 1.22 or 1)
            _grid(ax)
            fmt = (lambda v, _: f"{v:.0f}s") if key == "avg_seconds" else (lambda v, _: _kilo(v))
            ax.yaxis.set_major_formatter(plt.FuncFormatter(fmt))
        headroom = ax.get_ylim()[1] * 0.015
        for x, row, colour in zip(xs, rows, colours, strict=True):
            value = row[key]
            _bar(ax, x, value, 0.52, colour)
            if key == "pass_rate":
                text = f"{row['passed']}/{row['tasks']}"
            elif key == "avg_seconds":
                text = f"{value:.0f}s"
            else:
                text = _kilo(value)
            _value(ax, x, value, text, dy=headroom)
        px, _py = _inches(fig, ax, -0.7, 0)
        title_y = height - PAD - INSET - 0.98
        deco.text(px, title_y, panel_title, ha="left", va="center", family="serif", fontsize=11.5,
                  color=INK)
        deco.text(px, title_y - 0.22, panel_sub, ha="left", va="center", fontsize=7.8,
                  color=INK_SOFT)
    fig.savefig(out)
    plt.close(fig)
    return out


def draw_ablation(suites: list[SuiteResult], out: Path) -> Path:
    """Two panels: pass@1 per retrieval mode, and mean tokens per task, one colour per mode."""
    rows = ablation_rows(suites)
    colours = {mode: SERIES[i] for i, mode in enumerate(ABLATION_MODES)}
    models = sorted({s.model for s in ablation_suites(suites)})
    return _bar_panels(
        rows,
        labels=[r["mode"] for r in rows],
        colours=[colours[r["mode"]] for r in rows],
        title="Retrieval ablation",
        subtitle=f"Same agent, same tasks · {', '.join(models)}",
        panels=[
            ("pass@1 by retrieval mode", "Hidden-test pass rate", "pass_rate"),
            ("tokens per task", "Mean over graded tasks, all model calls", "avg_tokens"),
        ],
        out=out,
        callout=("One run per arm", "Gaps here sit under the noise floor"),
    )


def short_model_name(model: str) -> str:
    """`groq:openai/gpt-oss-120b` -> `gpt-oss-120b`; `k2think:MBZUAI-IFM/K2-Think-v2` ->
    `K2-Think-v2`. The provider is in the subtitle and the organisation is not what a reader
    calls the model; two full names side by side do not fit under a bar."""
    return model.split(":", 1)[-1].rsplit("/", 1)[-1]


def draw_model_comparison(suites: list[SuiteResult], out: Path) -> Path:
    """Three panels: pass@1, tokens and wall seconds per task, one colour per model.

    The x labels are the model names without their provider, since that is how a reader knows
    them; the provider is in the subtitle. Seconds include tool calls and test runs, so the panel
    reads as "how long a task takes with this model", which is the number a local model changes.
    """
    rows = model_rows(suites)
    providers = sorted({r["model"].split(":", 1)[0] for r in rows})
    return _bar_panels(
        rows,
        labels=[short_model_name(r["model"]) for r in rows],
        colours=[SERIES[i % len(SERIES)] for i in range(len(rows))],
        title="Model comparison",
        subtitle=f"Same suite, graph and retrieval · {', '.join(providers)}",
        panels=[
            ("pass@1 by model", "Hidden-test pass rate", "pass_rate"),
            ("tokens per task", "Mean over graded tasks, all model calls", "avg_tokens"),
            ("seconds per task", "Mean wall time over graded tasks", "avg_seconds"),
        ],
        out=out,
        callout=("Left: higher is better", "Middle and right: lower is better"),
    )


def draw_cost_profile(suite: SuiteResult, out: Path, ledger: Ledger | None = None) -> Path:
    """Where the context budget goes: average input and output tokens per node per run."""
    rows = node_costs(suite, ledger)
    width = 8.8
    height = 2.85 + 0.52 * max(len(rows), 1)
    fig, deco = _canvas(width, height)
    runs = rows[0]["runs"] if rows else 0
    _header(
        deco, width, height, "Tokens per run, by graph node",
        f"Average over {runs} run{'s' if runs != 1 else ''} · {suite.model}",
        callout=("Lower is better", "Input + output, every call in the node"),
    )
    ax = _axes(fig, width, height, left=1.15, right=0.45, top=1.35, bottom=1.10)
    inp = [r["input_tokens"] for r in rows]
    outp = [r["output_tokens"] for r in rows]
    y = list(range(len(rows)))[::-1]
    ax.set_ylim(-0.7, len(rows) - 0.3)
    ax.set_xlim(0, max([a + b for a, b in zip(inp, outp)], default=1) * 1.34)
    ax.set_yticks(y, [r["node"] for r in rows])
    _grid(ax, "x")
    ax.xaxis.set_major_formatter(lambda v, _pos: f"{v / 1000:.0f}k" if v else "0")
    for yi, row, i_tok, o_tok in zip(y, rows, inp, outp):
        _hbar(ax, yi, i_tok, 0.5, SERIES[0], round_end=not o_tok)
        _hbar(ax, yi, o_tok, 0.5, SERIES[1], left=i_tok)
        ax.text(
            i_tok + o_tok + ax.get_xlim()[1] * 0.015, yi,
            f"{i_tok + o_tok:,.0f}  ({row['calls']:.1f} calls)",
            va="center", fontsize=8.8, color=INK, fontweight="semibold",
        )
    _legend(deco, width, PAD + INSET + 0.13,
            [(SERIES[0], "input tokens"), (SERIES[1], "output tokens")])
    fig.savefig(out)
    _plt().close(fig)
    return out


def profiled_suite(suites: list[SuiteResult]) -> SuiteResult:
    """The one suite the cost profile describes: the newest in-house baseline run, or the
    newest suite of all when no run qualifies as a baseline."""
    baselines = comparison_suites(suites)
    return baselines[-1] if baselines else suites[-1]


def render_all(
    suites: list[SuiteResult], out_dir: Path = FIGURES_DIR, ledger: Ledger | None = None
) -> list[Path]:
    """The three standing figures, plus the retrieval ablation once at least two modes have
    results and the model comparison once two models have run the baseline configuration.

    The pass-rate and iteration figures draw the `headline_suites`; the ablation arms have
    their own chart. The cost profile describes one suite only, the newest in-house baseline
    run (or the newest suite of all when there is none): stacking several configurations' node
    costs into one chart would hide which one the budget belongs to."""
    out_dir.mkdir(parents=True, exist_ok=True)
    headline = headline_suites(suites) or suites
    paths = [
        draw_pass_rate(headline, out_dir / "pass_rate.png"),
        draw_iteration_curve(headline, out_dir / "iteration_curve.png"),
        draw_cost_profile(profiled_suite(suites), out_dir / "cost_profile.png", ledger),
    ]
    if len(ablation_suites(suites)) >= 2:
        paths.append(draw_ablation(suites, out_dir / "retrieval_ablation.png"))
    if len(comparison_suites(suites)) >= 2:
        paths.append(draw_model_comparison(suites, out_dir / "model_comparison.png"))
    return paths
