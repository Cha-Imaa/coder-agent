"""Write the numbers behind the web talk from committed eval results.

    uv run python evals/talk_data.py               # docs/talk/data.json from evals/results/
    uv run python evals/talk_data.py --out x.json  # somewhere else

Run it after `evals/figures.py` whenever a new results file lands, so the talk's charts and the
results page's figures describe the same runs. A test fails while the committed file is stale.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from coder_agent.evals.figures import latest_results
from coder_agent.evals.talk import TALK_DATA, latest_retrieval_summary, talk_data

console = Console()
app = typer.Typer(add_completion=False)


@app.command()
def main(
    out: Annotated[Path, typer.Option(help="Where to write the JSON.")] = TALK_DATA,
) -> None:
    suites = latest_results()
    if not suites:
        console.print("[red]No results files found.[/red]")
        raise typer.Exit(code=2)
    data = talk_data(suites, latest_retrieval_summary())
    out.parent.mkdir(parents=True, exist_ok=True)
    # Indented and key-ordered so a new run shows up as a readable diff in review.
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")
    for key, value in data["stats"].items():
        console.print(f"  {key:<16} {value}")


if __name__ == "__main__":
    app()
