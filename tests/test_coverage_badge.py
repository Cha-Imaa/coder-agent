"""The badge script is the only thing between CI's coverage number and the README."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "coverage_badge.py"


@pytest.mark.parametrize(
    ("percent", "colour"),
    [(95.0, "brightgreen"), (84.6, "green"), (72.0, "yellowgreen"), (61.0, "yellow"), (12.0, "red")],
)
def test_colour_follows_thresholds(percent: float, colour: str) -> None:
    sys.path.insert(0, str(SCRIPT.parent))
    from coverage_badge import badge

    assert badge(percent) == {
        "schemaVersion": 1, "label": "coverage", "message": f"{percent:.0f}%", "color": colour,
    }


def test_cli_reads_coverage_json_and_writes_the_endpoint_file(tmp_path: Path) -> None:
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"totals": {"percent_covered": 87.44}}), encoding="utf-8")
    out = tmp_path / "badge" / "coverage.json"

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(report), str(out)],
        capture_output=True, text=True, check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["message"] == "87%"
    assert "87.4%" in proc.stdout


def test_cli_usage_error_without_two_paths() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 2
    assert "Usage" in proc.stderr
