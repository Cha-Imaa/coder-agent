"""The docs site is only as good as its nav: a renamed page must fail here before it fails in CI.

`mkdocs build --strict` in the docs workflow is the real gate; these checks catch the two
mistakes that would otherwise only show up there, without needing mkdocs installed in the dev
environment.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MKDOCS = ROOT / "mkdocs.yml"
DOCS = ROOT / "docs"
WORKFLOW = ROOT / ".github" / "workflows" / "docs.yml"


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
    on_disk = {path.name for path in DOCS.glob("*.md")}
    missing = on_disk - set(_nav_pages())
    assert not missing, f"pages missing from nav: {missing}"


def test_workflow_builds_strict_from_the_lock() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "mkdocs build --strict" in text
    assert "uv sync --extra docs --frozen" in text
    assert "permissions:\n  contents: read" in text, "the build job must run read-only"
