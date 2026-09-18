"""The demo recorder and renderer: a pipe in, a cast file and a GIF out, no model involved."""

from __future__ import annotations

import json
import os
import re
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


def test_typing_the_command_animates_instead_of_one_long_frame(tmp_path: Path) -> None:
    cast = demo.Cast.load(_cast(tmp_path, [[0.0, "o", "hello\r\n"]], title="coder run ./repo fix"))
    frames = demo.frames_from_cast(cast, typing_delay=0.04, min_frame=0.06, hold=1.0)
    typing = [f for f in frames[:-1] if _plain(f)[0].startswith("$ ") and _plain(f)[1] == ""]
    assert len(typing) >= 2, "the command is typed over several frames"
    assert max(f.duration for f in typing) < 1.0, "no typing frame sits still for seconds"
    assert all(f.duration >= 0.06 for f in frames[:-1]), "every shown frame is long enough to see"


def test_render_writes_an_animated_gif(tmp_path: Path) -> None:
    from PIL import Image, ImageFont

    cast = demo.Cast.load(_cast(tmp_path, [[0.0, "o", f"{BOLD}ok{RESET} \x1b[42m bg \x1b[0m\r\n"]]))
    frames = demo.frames_from_cast(cast, rows=4, typing_delay=0.2)
    out = demo.render_gif(frames, tmp_path / "out" / "demo.gif", cols=40, font_size=12)
    lh = sum(ImageFont.truetype(str(demo.find_font()), 12).getmetrics())
    with Image.open(out) as img:
        assert img.format == "GIF"
        assert img.n_frames == len(frames) > 2
        assert img.size[1] == (lh + 18) + 4 * lh + 2 * demo.PADDING, "title bar plus four rows"


def test_rendered_corners_are_transparent_so_either_readme_theme_shows_through(
    tmp_path: Path,
) -> None:
    """A square dark block is the look the window frame exists to avoid."""
    from PIL import Image

    cast = demo.Cast.load(_cast(tmp_path, [[0.0, "o", "ok\r\n"]]))
    frames = demo.frames_from_cast(cast, rows=3, typing_delay=0.2)
    out = demo.render_gif(frames, tmp_path / "demo.gif", cols=20, font_size=12)
    with Image.open(out) as img:
        # Pillow renumbers the palette when it optimises, so the index itself is not the contract.
        assert "transparency" in img.info
        corner = img.convert("RGBA").getpixel((0, 0))
        middle = img.convert("RGBA").getpixel((img.size[0] // 2, 2))
    assert corner[3] == 0, "the rounded corner is not painted"
    assert middle[3] == 255, "the title bar between the corners is"


CHILD = """
import os, sys
sys.stdout.write("\\x1b[31mstart\\x1b[0m " + os.environ.get("FORCE_COLOR", "unset") + "\\n")
sys.stdout.write("Approve? ")
sys.stdout.flush()
answer = input()
sys.stdout.write(f"got {answer}\\n")
"""


def test_row_spans_collapse_a_line_that_does_not_change(tmp_path: Path) -> None:
    # Three writes, so three frames, but the first line is written once and then stays: it must
    # cost one span, not one per frame. This is the whole reason the SVG is a tenth of the GIF.
    cast = demo.Cast.load(_cast(tmp_path, [
        [0.0, "o", "first\r\n"], [1.0, "o", "second\r\n"], [2.0, "o", "third\r\n"],
    ]))
    frames = demo.frames_from_cast(cast, rows=6, typing_delay=0.2, hold=1.0)
    spans = demo._row_spans(frames)
    first = [s for s in spans if "".join(ch for ch, _ in s[3]) == "first"]
    assert len(first) == 1, "a line that never changes is one group"
    assert first[0][2] == len(frames), "and it stays on screen to the end"
    assert all(any(ch != " " for ch, _ in cells) for _, _, _, cells in spans), "no blank groups"


def test_render_writes_an_animated_svg(tmp_path: Path) -> None:
    import xml.etree.ElementTree as ET

    cast = demo.Cast.load(_cast(tmp_path, [[0.0, "o", f"{BOLD}ok{RESET} \x1b[42m bg \x1b[0m\r\n"]]))
    frames = demo.frames_from_cast(cast, rows=4, typing_delay=0.2)
    out = demo.render_svg(frames, tmp_path / "out" / "demo.svg", cols=40, font_size=12)
    root = ET.parse(out).getroot()  # parses at all, so the text is XML-escaped
    style = root.find("{http://www.w3.org/2000/svg}style").text
    assert "@keyframes" in style and "infinite" in style
    assert "white-space:pre" in style, "without it textLength stretches the surviving glyphs"
    assert "prefers-reduced-motion" in style, "a still viewer gets the last frame"
    groups = [g for g in root.iter("{http://www.w3.org/2000/svg}g") if g.get("class")]
    assert groups, "one group per row per stretch it is unchanged for"
    assert style.count("@keyframes") <= len(groups), "groups sharing a window share a rule"
    assert "<script" not in out.read_text(encoding="utf-8"), "an <img> runs no script"


def test_svg_runs_are_trimmed_and_measured_in_whole_columns(tmp_path: Path) -> None:
    from PIL import ImageFont

    cast = demo.Cast.load(_cast(tmp_path, [[0.0, "o", "    indented\r\n"]]))
    frames = demo.frames_from_cast(cast, rows=3, typing_delay=0.2)
    out = demo.render_svg(frames, tmp_path / "demo.svg", cols=40, font_size=12)
    cw = round(ImageFont.truetype(str(demo.find_font()), 12).getlength("M"))
    match = re.search(r'<text x="(\d+)" y="\d+" textLength="(\d+)"[^>]*>indented<', out.read_text(encoding="utf-8"))
    assert match, "the run is emitted without its leading spaces"
    assert int(match.group(1)) == demo.PADDING + 4 * cw, "and starts at its own column"
    assert int(match.group(2)) == len("indented") * cw


def test_render_command_picks_the_renderer_from_the_suffix(tmp_path: Path) -> None:
    cast = _cast(tmp_path, [[0.0, "o", "hi\r\n"]])
    for name, head in (("out.svg", "<svg"), ("out.gif", "GIF8")):
        assert demo.main(["render", str(cast), str(tmp_path / name), "--rows", "3"]) == 0
        assert (tmp_path / name).read_bytes()[:4].decode("latin-1").startswith(head[:4])


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


def test_the_recorder_drops_no_color_from_the_child_environment(monkeypatch) -> None:
    # A shell that exports NO_COLOR would otherwise hand the agent a console that keeps bold and
    # dim and drops every colour, and the recording comes out grey without saying why.
    monkeypatch.setenv("NO_COLOR", "1")
    env = demo._child_env(88, 28)
    assert "NO_COLOR" not in env
    assert env["FORCE_COLOR"] == "1" and env["COLUMNS"] == "88"


def test_a_recorded_run_keeps_its_colours(tmp_path: Path, monkeypatch) -> None:
    """End to end: the cast of a coloured child carries colour, not only bold and dim.

    `NO_COLOR` is set here on purpose. It is what a recording made from this project's own shell
    inherits, and Rich gives it precedence over `FORCE_COLOR`, so this is the case that produced
    a grey hero and the one worth pinning.
    """
    monkeypatch.setenv("NO_COLOR", "1")
    child = tmp_path / "child.py"
    child.write_text(
        '''from coder_agent.cli import console
from rich.text import Text
console.print(Text("green", style="green"))
''',
        encoding="utf-8",
    )
    cast = tmp_path / "out.cast"
    assert demo.record([sys.executable, str(child)], cast, cols=40, rows=6) == 0
    text = "".join(json.loads(line)[2] for line in cast.read_text(encoding="utf-8").splitlines()[1:])
    assert re.search(r"\x1b\[3[12]m|\x1b\[38;", text), "the child's colour reached the cast"


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


def test_cli_survives_a_stdout_that_cannot_encode_the_ui() -> None:
    """A redirected stdout on a cp1252 Windows install must not kill a run mid-flight.

    The child writes the re-planning arrow through the CLI's own console with the locale
    encoding forced to cp1252, the situation `coder run ... > run.log` creates.
    """
    child = (
        "import sys\n"
        "from coder_agent.cli import console\n"
        "console.print('\u21bb re-planning')\n"
        "print('survived', file=sys.stderr)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env["PYTHONIOENCODING"] = "cp1252"
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True, env=env, cwd=ROOT, check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert b"survived" in proc.stderr
    assert "\u21bb re-planning" in proc.stdout.decode("utf-8", "replace")
