"""Record one `coder` run to a terminal cast and render that cast to a GIF.

Why not asciinema, vhs or terminalizer: none of them runs natively on Windows, and the GIF
step needs ffmpeg or a headless browser on top. This file does both halves in Python with what
the project already installs (Rich decodes the colour codes, Pillow draws the frames), so the
README hero can be regenerated on the laptop that develops the agent.

The cast is the asciinema v2 format, one JSON header line then `[time, "o" | "i", text]` events,
so the recording also plays in asciinema or agg unchanged. Input events are written by the
recorder when it answers a prompt, because a pipe does not echo what was typed.

Usage:
  python scripts/demo.py prepare TASK_ID DEST                # fresh copy of a suite task, prints its prompt
  python scripts/demo.py record OUT.cast [--title TEXT] -- coder run DEST "prompt"
  python scripts/demo.py render IN.cast OUT.gif [--rows 28] [--max-gap 1.5]
"""

from __future__ import annotations

import argparse
import codecs
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.ansi import AnsiDecoder
from rich.style import Style
from rich.terminal_theme import MONOKAI, TerminalTheme

# Everything the screen model treats as a command rather than as text: line breaks, backspace,
# bell, CSI sequences (colours and cursor movement) and OSC sequences (window titles, links).
CONTROL = re.compile(r"\r\n|\r|\n|\x08|\x07|\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# The one prompt `coder run` asks. Extra `--answer PATTERN=TEXT` pairs extend this.
DEFAULT_ANSWERS = {r"Approve\?": "y"}

Cell = tuple[str, Style]


# --------------------------------------------------------------------------- recording


def _child_env(cols: int, rows: int) -> dict[str, str]:
    """Make the child believe it is in a colour terminal of a known size.

    Rich reads COLUMNS/LINES when it cannot query the terminal, and the CLI turns FORCE_COLOR
    into `force_terminal` (see `coder_agent.cli._console`). COLORTERM unlocks 24-bit colour so the
    diff highlighting comes through unchanged; PYTHONIOENCODING keeps box-drawing characters
    intact on a Windows pipe, which would otherwise be cp1252.
    """
    return {
        **os.environ,
        "FORCE_COLOR": "1",
        "COLORTERM": "truecolor",
        "TERM": "xterm-256color",
        "COLUMNS": str(cols),
        "LINES": str(rows),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }


def _resolve(command: list[str]) -> list[str]:
    """`coder` is a console script in the venv; a plain Popen on Windows would not find it."""
    exe = shutil.which(command[0])
    return [exe or command[0], *command[1:]]


def record(
    command: list[str],
    out: Path,
    *,
    answers: dict[str, str] | None = None,
    cols: int = 100,
    rows: int = 28,
    answer_delay: float = 0.8,
    timeout: float = 900.0,
    title: str | None = None,
    cwd: Path | None = None,
) -> int:
    """Run `command` with stdout piped, timestamp every chunk, answer prompts. Returns exit code.

    A reader thread moves chunks into a queue so the main loop can also watch the clock: a child
    that hangs on a rate limit is killed instead of blocking the recording forever.
    """
    answers = {**DEFAULT_ANSWERS, **(answers or {})}
    patterns = [(re.compile(p), text) for p, text in answers.items()]
    started = time.monotonic()
    proc = subprocess.Popen(
        _resolve(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=open(out.with_suffix(".stderr.log"), "wb"),  # noqa: SIM115 - closed by the OS with the child
        env=_child_env(cols, rows),
        cwd=cwd,
    )
    assert proc.stdout is not None and proc.stdin is not None

    chunks: queue.Queue[tuple[float, bytes] | None] = queue.Queue()

    def reader() -> None:
        while data := proc.stdout.read1(4096):
            chunks.put((time.monotonic() - started, data))
        chunks.put(None)

    threading.Thread(target=reader, daemon=True).start()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    header = {
        "version": 2, "width": cols, "height": rows, "timestamp": int(time.time()),
        "command": " ".join(command), "title": title or " ".join(command),
        "env": {"TERM": "xterm-256color", "SHELL": "powershell"},
    }
    tail = ""  # output since the last answered prompt, colour codes removed, for matching
    with out.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(header) + "\n")
        while True:
            try:
                item = chunks.get(timeout=0.2)
            except queue.Empty:
                if time.monotonic() - started > timeout:
                    proc.kill()
                    fh.write(json.dumps([time.monotonic() - started, "o",
                                         "\r\n[demo recorder] timeout, child killed\r\n"]) + "\n")
                    break
                continue
            if item is None:
                break
            at, data = item
            text = decoder.decode(data)
            fh.write(json.dumps([round(at, 3), "o", text]) + "\n")
            tail = (tail + ANSI.sub("", text))[-4000:]
            for pattern, reply in patterns:
                if pattern.search(tail):
                    time.sleep(answer_delay)
                    proc.stdin.write((reply + "\n").encode("utf-8"))
                    proc.stdin.flush()
                    fh.write(json.dumps([round(time.monotonic() - started, 3), "i", reply + "\n"])
                             + "\n")
                    tail = ""
                    break
    proc.stdin.close()
    return proc.wait()


# --------------------------------------------------------------------------- screen model


class Screen:
    """A minimal terminal: a growing list of lines of styled cells, a cursor, and the SGR state.

    Rich's `AnsiDecoder` keeps the current style between calls, so it is fed only the text runs
    and the colour codes; line breaks, carriage returns and cursor movement are handled here,
    which is what makes progress bars and re-drawn prompts come out right.
    """

    def __init__(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows
        self.lines: list[list[Cell]] = [[]]
        self.row = 0
        self.col = 0
        self.decoder = AnsiDecoder()

    # -- cursor helpers
    @property
    def top(self) -> int:
        return max(0, len(self.lines) - self.rows)

    def _line(self) -> list[Cell]:
        while self.row >= len(self.lines):
            self.lines.append([])
        return self.lines[self.row]

    def _newline(self) -> None:
        self.row += 1
        self.col = 0
        self._line()

    def _put(self, char: str, style: Style) -> None:
        if self.col >= self.cols:
            self._newline()
        line = self._line()
        while len(line) < self.col:
            line.append((" ", Style.null()))
        if self.col < len(line):
            line[self.col] = (char, style)
        else:
            line.append((char, style))
        self.col += 1

    # -- input
    def feed(self, data: str) -> None:
        pos = 0
        for m in CONTROL.finditer(data):
            self._text(data[pos:m.start()])
            self._control(m.group())
            pos = m.end()
        self._text(data[pos:])

    def _text(self, run: str) -> None:
        if not run:
            return
        text = self.decoder.decode_line(run)
        styles = [Style.null()] * len(text.plain)
        for span in text.spans:
            style = span.style if isinstance(span.style, Style) else Style.parse(str(span.style))
            for i in range(span.start, min(span.end, len(styles))):
                styles[i] = style
        for char, style in zip(text.plain, styles, strict=True):
            if char == "\t":
                for _ in range(8 - self.col % 8):
                    self._put(" ", style)
            else:
                self._put(char, style)

    def _control(self, seq: str) -> None:
        if seq in ("\n", "\r\n"):
            self._newline()
        elif seq == "\r":
            self.col = 0
        elif seq == "\x08":
            self.col = max(0, self.col - 1)
        elif seq == "\x07" or seq.startswith("\x1b]"):
            return
        elif seq.endswith("m"):
            self.decoder.decode_line(seq)  # updates decoder.style, returns empty text
        else:
            self._csi(seq)

    def _csi(self, seq: str) -> None:
        final = seq[-1]
        params = seq[2:-1]
        if params.startswith("?"):  # cursor visibility, alternate screen: nothing to draw
            return
        n = int(params.split(";")[0]) if params.split(";")[0].isdigit() else None
        if final == "K":
            line = self._line()
            if n in (None, 0):
                del line[self.col:]
            elif n == 1:
                for i in range(min(self.col + 1, len(line))):
                    line[i] = (" ", Style.null())
            else:
                line.clear()
        elif final == "A":
            self.row = max(self.top, self.row - (n or 1))
        elif final == "B":
            self.row += n or 1
        elif final == "C":
            self.col = min(self.cols - 1, self.col + (n or 1))
        elif final == "D":
            self.col = max(0, self.col - (n or 1))
        elif final == "G":
            self.col = max(0, (n or 1) - 1)
        elif final in "Hf":
            parts = [int(p) if p.isdigit() else 1 for p in params.split(";")] + [1]
            self.row = self.top + parts[0] - 1
            self.col = parts[1] - 1
        elif final == "J" and n == 2:
            self.lines = [[]]
            self.row = self.col = 0

    # -- output
    def snapshot(self) -> list[list[Cell]]:
        """The visible rows, padded to `rows` so every frame has the same shape."""
        visible = [list(line) for line in self.lines[self.top:]]
        return visible + [[] for _ in range(self.rows - len(visible))]

    def plain(self) -> list[str]:
        return ["".join(c for c, _ in line).rstrip() for line in self.lines]


# --------------------------------------------------------------------------- frames


@dataclass
class Frame:
    cells: list[list[Cell]]
    duration: float  # seconds this frame stays on screen


@dataclass
class Cast:
    header: dict
    events: list[tuple[float, str, str]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Cast:
        lines = path.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])
        events = [tuple(json.loads(line)) for line in lines[1:] if line.strip()]
        return cls(header, events)  # type: ignore[arg-type]


def frames_from_cast(
    cast: Cast,
    *,
    rows: int | None = None,
    max_gap: float = 1.5,
    min_frame: float = 0.06,
    hold: float = 3.0,
    typing_delay: float = 0.04,
) -> list[Frame]:
    """Replay the cast through the screen model and emit one frame per visible change.

    Time between events is capped at `max_gap`, because a model call that took twelve seconds
    makes a bad GIF; identical screens extend the previous frame instead of adding one; a frame
    that has not yet been shown for `min_frame` takes the newer screen instead of a new slot, so
    a burst of tiny writes becomes a few visible frames rather than dozens the eye cannot
    follow. The test is on the previous frame's duration, not the new one's: testing the new
    one merged every 40 ms typing step into a single frame, and the whole typed command sat
    still for eight seconds.
    """
    cols = int(cast.header.get("width", 100))
    screen = Screen(cols, rows or int(cast.header.get("height", 28)))
    frames: list[Frame] = []

    def emit(duration: float) -> None:
        cells = screen.snapshot()
        if frames and frames[-1].cells == cells:
            frames[-1].duration += duration
        elif frames and frames[-1].duration < min_frame:
            frames[-1].cells = cells  # too quick to see: show the newer state in the same slot
            frames[-1].duration += duration
        else:
            frames.append(Frame(cells, duration))

    # Prelude: the command typed at a prompt, since the pipe never showed it.
    screen.feed("\x1b[1;32m$\x1b[0m ")
    emit(0.6)
    for char in cast.header.get("title", cast.header.get("command", "")):
        screen.feed(f"\x1b[1m{char}\x1b[0m")
        emit(typing_delay)
    emit(0.5)
    screen.feed("\r\n")

    previous = cast.events[0][0] if cast.events else 0.0
    for at, kind, text in cast.events:
        gap = min(max(at - previous, 0.0), max_gap)
        previous = at
        if kind == "o":
            emit(gap)
            screen.feed(text)
        elif kind == "i":
            emit(gap)
            for char in text.rstrip("\n"):
                screen.feed(f"\x1b[1m{char}\x1b[0m")
                emit(typing_delay * 3)
            emit(0.3)
            screen.feed("\r\n")
    emit(hold)
    return frames


# --------------------------------------------------------------------------- drawing

BACKGROUND = (30, 30, 46)
FOREGROUND = (205, 214, 244)
PADDING = 18


def find_font(bold: bool = False) -> Path:
    """A monospace face with box-drawing glyphs: matplotlib ships DejaVu Sans Mono everywhere."""
    candidates: list[Path] = []
    spec = importlib.util.find_spec("matplotlib")
    if spec and spec.origin:
        ttf = Path(spec.origin).parent / "mpl-data" / "fonts" / "ttf"
        candidates.append(ttf / ("DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"))
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates += [windows / ("consolab.ttf" if bold else "consola.ttf"),
                   Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("no monospace font found; pass --font PATH")


def _rgb(style: Style, theme: TerminalTheme) -> tuple[tuple[int, int, int], tuple[int, int, int] | None]:
    fg = tuple(style.color.get_truecolor(theme)) if style.color else FOREGROUND
    bg = tuple(style.bgcolor.get_truecolor(theme)) if style.bgcolor else None
    if style.dim:
        fg = tuple(int(f * 0.55 + b * 0.45) for f, b in zip(fg, BACKGROUND, strict=True))
    return fg, bg  # type: ignore[return-value]


def render_gif(
    frames: list[Frame],
    out: Path,
    *,
    cols: int,
    font_size: int = 15,
    font: Path | None = None,
    theme: TerminalTheme = MONOKAI,
) -> Path:
    """Draw every frame with Pillow and write an animated GIF with per-frame durations."""
    from PIL import Image, ImageDraw, ImageFont

    regular = ImageFont.truetype(str(font or find_font()), font_size)
    try:
        strong = ImageFont.truetype(str(font or find_font(bold=True)), font_size)
    except (FileNotFoundError, OSError):
        strong = regular
    cw = max(1, round(regular.getlength("M")))
    ascent, descent = regular.getmetrics()
    lh = ascent + descent  # no leading, so box-drawing glyphs on adjacent rows join up
    rows = len(frames[0].cells)
    size = (cols * cw + 2 * PADDING, rows * lh + 2 * PADDING)

    images = []
    for frame in frames:
        img = Image.new("RGB", size, BACKGROUND)
        draw = ImageDraw.Draw(img)
        for r, line in enumerate(frame.cells):
            y = PADDING + r * lh
            c = 0
            while c < len(line):
                style = line[c][1]
                end = c
                while end < len(line) and line[end][1] == style:
                    end += 1
                text = "".join(ch for ch, _ in line[c:end])
                fg, bg = _rgb(style, theme)
                x = PADDING + c * cw
                if bg:
                    draw.rectangle([x, y, x + (end - c) * cw, y + lh], fill=bg)
                draw.text((x, y), text, font=strong if style.bold else regular, fill=fg)
                c = end
        images.append(img.quantize(colors=128))

    durations = [max(20, int(f.duration * 1000)) for f in frames]
    out.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(out, save_all=True, append_images=images[1:], duration=durations, loop=0)
    return out


# --------------------------------------------------------------------------- commands


def prepare(task_id: str, dest: Path) -> str:
    """Materialise one suite task into `dest` and return its prompt; the demo works on a copy."""
    from coder_agent.evals.tasks import load_suite

    task = next((t for t in load_suite() if t.id == task_id), None)
    if task is None:
        raise SystemExit(f"unknown suite task {task_id!r}")
    if dest.exists():
        raise SystemExit(f"{dest} exists; remove it or pick another directory")
    task.materialise(dest)
    return task.prompt.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="copy a suite task to a directory and print its prompt")
    p.add_argument("task_id")
    p.add_argument("dest", type=Path)

    r = sub.add_parser("record", help="run a command with stdout piped and write a .cast",
                       epilog="the command to record comes after --")
    r.add_argument("out", type=Path)
    r.add_argument("--title", help="what the GIF shows as the typed command (default: the command)")
    r.add_argument("--answer", action="append", default=[], metavar="PATTERN=TEXT",
                   help="reply TEXT when PATTERN appears; default answers y to Approve?")
    r.add_argument("--cols", type=int, default=100)
    r.add_argument("--rows", type=int, default=28)
    r.add_argument("--answer-delay", type=float, default=0.8)
    r.add_argument("--timeout", type=float, default=900.0)

    g = sub.add_parser("render", help="draw a .cast as an animated GIF")
    g.add_argument("cast", type=Path)
    g.add_argument("out", type=Path)
    g.add_argument("--rows", type=int, help="visible rows (default: the cast height)")
    g.add_argument("--max-gap", type=float, default=1.5, help="longest pause kept, seconds")
    g.add_argument("--hold", type=float, default=3.0, help="how long the last frame stays")
    g.add_argument("--font", type=Path)
    g.add_argument("--font-size", type=int, default=15)

    argv = sys.argv[1:] if argv is None else list(argv)
    # argparse's REMAINDER would also swallow the options before it, so split on `--` by hand.
    command: list[str] = []
    if "--" in argv:
        command = argv[argv.index("--") + 1:]
        argv = argv[: argv.index("--")]
    args = parser.parse_args(argv)
    if args.cmd == "prepare":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        print(prepare(args.task_id, args.dest))
        return 0
    if args.cmd == "record":
        if not command:
            parser.error("give the command to record after --")
        answers = dict(a.split("=", 1) for a in args.answer)
        code = record(command, args.out, answers=answers, cols=args.cols, rows=args.rows,
                      answer_delay=args.answer_delay, timeout=args.timeout, title=args.title)
        print(f"recorded {args.out} (exit {code})")
        return code
    cast = Cast.load(args.cast)
    frames = frames_from_cast(cast, rows=args.rows, max_gap=args.max_gap, hold=args.hold)
    render_gif(frames, args.out, cols=int(cast.header.get("width", 100)),
               font=args.font, font_size=args.font_size)
    print(f"wrote {args.out}: {len(frames)} frames, {args.out.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
