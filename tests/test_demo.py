"""The demo recorder and renderer: a pipe in, a cast file and a GIF out, no model involved."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import demo

RED = "\x1b[31m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"


def test_screen_writes_styled_text_and_wraps_long_lines() -> None:
    screen = demo.Screen(cols=10, rows=5)
    screen.feed(f"{RED}abc{RESET} def\r\n" + "x" * 12)
    assert screen.plain() == ["abc def", "xxxxxxxxxx", "xx"]
    assert screen.lines[0][0][1].color.number == 1  # ANSI red
    assert screen.lines[0][4][1].color is None


def test_screen_handles_carriage_return_erase_and_cursor_up() -> None:
    screen = demo.Screen(cols=20, rows=5)
    screen.feed("loading 10%\rloading 90%\n")  # a progress line redrawn in place
    screen.feed("first\nsecond\x1b[1A\x1b[2K\rredone")  # cursor up, erase the line, rewrite
    assert screen.plain() == ["loading 90%", "redone", "second"]
    screen.feed("\x1b[?25l\t|")  # hide-cursor is ignored; a tab expands to the next stop
    assert screen.plain()[1] == "redone  |"


def test_snapshot_shows_the_last_rows_padded_to_height() -> None:
    screen = demo.Screen(cols=5, rows=2)
    screen.feed("a\nb\nc")
    visible = screen.snapshot()
    assert ["".join(ch for ch, _ in line) for line in visible] == ["b", "c"]
    assert len(demo.Screen(5, 3).snapshot()) == 3


def _cast(tmp_path: Path, events: list, **header) -> Path:
    path = tmp_path / "x.cast"
    lines = [json.dumps({"version": 2, "width": 40, "height": 6, "title": "ls", **header})]
    lines += [json.dumps(e) for e in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _plain(frame: demo.Frame) -> list[str]:
    return ["".join(ch for ch, _ in line).rstrip() for line in frame.cells]


def test_frames_cap_long_gaps_and_merge_identical_screens(tmp_path: Path) -> None:
    cast = demo.Cast.load(_cast(tmp_path, [
        [0.0, "o", "hello\r\n"],
        [12.0, "o", ""],  # a long silent wait: nothing changes on screen
        [12.5, "o", "world\r\n"],
        [13.0, "i", "y\n"],
    ]))
    frames = demo.frames_from_cast(cast, max_gap=1.5, hold=2.0)
    assert sum(f.duration for f in frames) < 12.0, "the twelve-second gap must be capped"
    assert frames[-1].duration >= 2.0, "the last frame holds"
    assert _plain(frames[-1])[:4] == ["$ ls", "hello", "world", "y"]
    for a, b in pairwise(frames):
        assert a.cells != b.cells, "identical consecutive frames are merged"


def test_render_writes_an_animated_gif(tmp_path: Path) -> None:
    from PIL import Image, ImageFont

    cast = demo.Cast.load(_cast(tmp_path, [[0.0, "o", f"{BOLD}ok{RESET} \x1b[42m bg \x1b[0m\r\n"]]))
    frames = demo.frames_from_cast(cast, rows=4, typing_delay=0.2)
    out = demo.render_gif(frames, tmp_path / "out" / "demo.gif", cols=40, font_size=12)
    ascent, descent = ImageFont.truetype(str(demo.find_font()), 12).getmetrics()
    with Image.open(out) as img:
        assert img.format == "GIF"
        assert img.n_frames == len(frames) > 2
        assert img.size[1] == 4 * (ascent + descent) + 2 * demo.PADDING


CHILD = """
import os, sys
sys.stdout.write("\\x1b[31mstart\\x1b[0m " + os.environ.get("FORCE_COLOR", "unset") + "\\n")
sys.stdout.write("Approve? ")
sys.stdout.flush()
answer = input()
sys.stdout.write(f"got {answer}\\n")
"""


def test_record_captures_output_answers_the_prompt_and_logs_input(tmp_path: Path) -> None:
    out = tmp_path / "run.cast"
    code = demo.record([sys.executable, "-c", CHILD], out, answer_delay=0.0, title="demo")
    assert code == 0
    header, *events = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert header["version"] == 2 and header["title"] == "demo"
    kinds = [(kind, text) for _, kind, text in events]
    outputs = "".join(text for kind, text in kinds if kind == "o")
    assert "\x1b[31mstart\x1b[0m 1" in outputs, "the child saw FORCE_COLOR and kept its colours"
    assert ("i", "y\n") in kinds
    assert outputs.index("Approve?") < outputs.index("got y")
    assert out.with_suffix(".stderr.log").exists()


def test_record_kills_a_child_that_hangs(tmp_path: Path) -> None:
    out = tmp_path / "hang.cast"
    code = demo.record([sys.executable, "-c", "import time; time.sleep(30)"], out, timeout=0.5)
    assert code != 0
    assert "timeout" in out.read_text(encoding="utf-8")


def test_prepare_copies_a_suite_task_and_returns_its_prompt(tmp_path: Path) -> None:
    prompt = demo.prepare("fix-bug-duration-units", tmp_path / "repo")
    assert "parse_duration" in prompt
    assert (tmp_path / "repo" / "durations.py").exists()
    assert not (tmp_path / "repo" / "hidden_tests").exists()
    with pytest.raises(SystemExit):
        demo.prepare("fix-bug-duration-units", tmp_path / "repo")


@pytest.mark.parametrize("forced", [True, False])
def test_cli_colours_a_pipe_only_when_force_color_is_set(forced: bool) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR", "NO_COLOR")}
    if forced:
        env["FORCE_COLOR"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", "from coder_agent.cli import app; app()", "run", "no-such-dir"],
        capture_output=True, text=True, env=env, cwd=ROOT, check=False,
    )
    assert proc.returncode == 2
    assert "Not a directory" in proc.stdout
    assert ("\x1b[31m" in proc.stdout) is forced
