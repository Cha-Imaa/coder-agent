"""The README is the first thing a visitor reads; a broken image or link there is a bug.

GitHub renders relative links and images against the repository root, so every relative target
must exist on disk. The README stays short for a skimming reader, so the result charts live on
the results page and only the two architecture diagrams and the headline chart are checked to be
in the README itself; code fences are checked for the one mistake a renderer will not flag
loudly: an unclosed fence, which swallows the rest of the page.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"

_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
# The hero image is centred, which GitHub only does for HTML, so an <img> counts as much as a
# Markdown image here.
_IMG = re.compile(r'<img[^>]*?src="([^"]+)"', re.DOTALL)


def _relative_targets() -> list[str]:
    text = README.read_text(encoding="utf-8")
    found = _LINK.findall(text) + _IMG.findall(text)
    return [t for t in found if not t.startswith(("http://", "https://", "#"))]


def test_relative_links_and_images_exist() -> None:
    targets = _relative_targets()
    assert targets, "no relative links found; the regex or the README changed shape"
    missing = [t for t in targets if not (ROOT / t.split("#", 1)[0]).exists()]
    assert not missing, f"README points at files that do not exist: {missing}"


def test_every_figure_is_shown() -> None:
    """A figure nobody links to is a figure nobody checks, so each one is on a page a reader reaches."""
    pages = [README, ROOT / "docs" / "results.md"]
    shown = {
        Path(m).name
        for page in pages
        for m in _LINK.findall(page.read_text(encoding="utf-8"))
        + _IMG.findall(page.read_text(encoding="utf-8"))
        if "figures/" in m
    }
    on_disk = {p.name for p in (ROOT / "docs" / "figures").glob("*.png")}
    assert on_disk <= shown, f"figures shown on no page: {on_disk - shown}"


def test_code_fences_are_balanced() -> None:
    lines = README.read_text(encoding="utf-8").splitlines()
    fences = [line for line in lines if line.startswith("```")]
    assert len(fences) % 2 == 0, "unclosed code fence in README"


def test_architecture_diagrams_are_shown() -> None:
    """The run loop and the system map are the two pictures a visitor reads before the prose."""
    shown = set(_relative_targets())
    for diagram in ("docs/figures/run_loop.png", "docs/figures/system_map.png"):
        assert diagram in shown, f"README no longer shows {diagram}"


def test_readme_links_to_the_web_player() -> None:
    """The hero is a picture of a run; the player is the run, so the README points at it."""
    text = README.read_text(encoding="utf-8")
    assert "cha-imaa.github.io/coder-agent/#watch-a-run" in text
    assert "watch a run" in text
