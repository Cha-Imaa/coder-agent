"""Draw the README figures from committed eval results.

    uv run python evals/figures.py                                  # newest file per label
    uv run python evals/figures.py evals/results/20260909-*.json    # specific file(s)
    uv run python evals/figures.py --out docs/figures --no-ledger

Writes `pass_rate.png`, `iteration_curve.png` and `cost_profile.png` to `docs/figures/`, plus
`retrieval_ablation.png` once results for two or more `--retrieval` modes exist. The
cost profile needs per-node usage; results written before it was stored in the file fall back to
the local run ledger, matched by task id.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from coder_agent.config import settings
from coder_agent.evals.figures import FIGURES_DIR, latest_results, node_costs, render_all
from coder_agent.evals.runner import load_result
from coder_agent.telemetry.ledger import Ledger

console = Console()
app = typer.Typer(add_completion=False)


@app.command()
def main(
    results: Annotated[list[Path] | None, typer.Argument(help="Results JSON files.")] = None,
    out: Annotated[Path, typer.Option(help="Output directory.")] = FIGURES_DIR,
    ledger: Annotated[bool, typer.Option(help="Fill missing per-node usage from the ledger.")] = True,
) -> None:
    suites = [load_result(p) for p in results] if results else latest_results()
    if not suites:
        console.print("[red]No results files found.[/red]")
        raise typer.Exit(code=2)
    book = Ledger(settings.ledger_path) if ledger and settings.ledger_path.exists() else None
    for s in suites:
        console.print(
            f"  {s.label:<36} {s.model:<30} pass@1 {s.pass_rate:.0%}  ({len(s.results)} tasks)"
        )
    for path in render_all(suites, out, book):
        console.print(f"[green]wrote[/green] {path}")
    rows = node_costs(suites[-1], book)
    if rows:
        console.print(f"\nTokens per run by node, {suites[-1].label} ({rows[0]['runs']} runs):")
        for r in rows:
            console.print(
                f"  {r['node']:<12} {r['input_tokens']:>9,.0f} in  {r['output_tokens']:>7,.0f} out"
                f"  {r['calls']:.1f} calls"
            )


if __name__ == "__main__":
    app()
