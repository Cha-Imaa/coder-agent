"""The docs site is only as good as its nav: a renamed page must fail here before it fails in CI.

`mkdocs build --strict` in the docs workflow is the real gate; these checks catch the two
mistakes that would otherwise only show up there, without needing mkdocs installed in the dev
environment.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MKDOCS = ROOT / "mkdocs.yml"
DOCS = ROOT / "docs"
WORKFLOW = ROOT / ".github" / "workflows" / "docs.yml"


def _excluded_pages() -> set[str]:
    """`exclude_docs` keeps local-only notes out of the build, so the nav must not list them."""
    text = MKDOCS.read_text(encoding="utf-8")
    marker = "\nexclude_docs: |\n"
    if marker not in text:
        return set()
    block = text.split(marker, 1)[1].split("\n\n", 1)[0]
    return {line.strip() for line in block.splitlines() if line.strip()}


def _nav_pages() -> list[str]:
    text = MKDOCS.read_text(encoding="utf-8")
    nav = text.split("\nnav:\n", 1)[1].split("\n\n", 1)[0]
    return re.findall(r":\s*([\w./-]+\.md)\s*$", nav, flags=re.MULTILINE)


def test_every_nav_entry_is_a_file() -> None:
    pages = _nav_pages()
    assert pages, "nav is empty or not parsed"
    missing = [page for page in pages if not (DOCS / page).is_file()]
    assert not missing, f"nav points at files that do not exist: {missing}"


def test_every_doc_page_is_in_the_nav() -> None:
    on_disk = {path.name for path in DOCS.glob("*.md")} - _excluded_pages()
    missing = on_disk - set(_nav_pages())
    assert not missing, f"pages missing from nav: {missing}"


def test_excluded_pages_are_untracked() -> None:
    """An excluded page is a private note: publishing it by committing it is the real mistake."""
    tracked = subprocess.run(
        ["git", "ls-files", "--", "docs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    leaked = [name for name in _excluded_pages() if f"docs/{name}" in tracked]
    assert not leaked, f"excluded from the site but committed to the repository: {leaked}"


def test_workflow_builds_strict_from_the_lock() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "mkdocs build --strict" in text
    assert "uv sync --extra docs --frozen" in text
    assert "permissions:\n  contents: read" in text, "the build job must run read-only"


def _extra(key: str) -> list[str]:
    """`extra_css` / `extra_javascript`: paths mkdocs resolves inside docs_dir."""
    text = MKDOCS.read_text(encoding="utf-8")
    block = text.split(f"\n{key}:\n", 1)[1].split("\n\n", 1)[0]
    return [line.strip(" -") for line in block.splitlines() if line.strip()]


def test_extra_assets_exist() -> None:
    """mkdocs --strict does not check these: a missing one is a silent 404 on every page."""
    assets = _extra("extra_css") + _extra("extra_javascript")
    assert assets, "the player's stylesheet and script are not registered"
    missing = [asset for asset in assets if not (DOCS / asset).is_file()]
    assert not missing, f"extra_css/extra_javascript point at files that do not exist: {missing}"


def test_the_player_and_its_frames_are_tracked() -> None:
    """A published page is built from a checkout, not from this laptop.

    `docs/assets/` sat inside a `.gitignore` entry meant for the diagram sources, so the player
    worked locally and would have been a pair of 404s on the site.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "--", "docs"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    index = (DOCS / "index.md").read_text(encoding="utf-8")
    needed = _extra("extra_css") + _extra("extra_javascript")
    needed += re.findall(r'class="cast-player"\s+data-src="([^"]+)"', index)
    missing = [asset for asset in needed if f"docs/{asset}" not in tracked]
    assert not missing, f"the site needs these and git does not have them: {missing}"


def test_the_landing_page_embeds_the_player_and_its_frames() -> None:
    index = (DOCS / "index.md").read_text(encoding="utf-8")
    match = re.search(r'class="cast-player"\s+data-src="([^"]+)"', index)
    assert match, "the landing page no longer mounts the web player"
    # Relative to the page URL, which for index.md is the site root.
    assert (DOCS / match.group(1)).is_file(), f"the player's frames are missing: {match.group(1)}"
