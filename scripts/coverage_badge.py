"""Turn a coverage.py JSON report into a shields.io endpoint badge.

Why not a coverage service: Codecov and friends want an upload token and a third account for one
number. shields.io can render any JSON that follows its endpoint schema, so CI writes that JSON
to a branch and the README points shields at the raw file. No secrets, no signup.

Usage: python scripts/coverage_badge.py coverage.json out/coverage.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Rough conventional thresholds; the colour is a glance, the number is the fact.
THRESHOLDS = [(90, "brightgreen"), (80, "green"), (70, "yellowgreen"), (60, "yellow"), (0, "red")]


def badge(percent: float) -> dict[str, object]:
    colour = next(c for floor, c in THRESHOLDS if percent >= floor)
    return {
        "schemaVersion": 1,
        "label": "coverage",
        "message": f"{percent:.0f}%",
        "color": colour,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    percent = float(report["totals"]["percent_covered"])
    out = Path(argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(badge(percent), indent=2) + "\n", encoding="utf-8")
    print(f"coverage {percent:.1f}% -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
