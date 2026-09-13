"""The files GitHub reads to describe the project: licence, templates, changelog, metadata.

A malformed issue form is silently ignored by GitHub (the template just does not appear), and a
licence the metadata does not mention is invisible on PyPI, so both are checked here.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"
BLOCK_TYPES = {"input", "textarea", "dropdown", "checkboxes", "markdown"}

# 260 (Windows MAX_PATH) minus room for a reasonably deep clone directory. The longest
# tracked path today is a HumanEval hidden test at 109 characters.
MAX_RELATIVE_PATH = 120


def test_licence_is_mit_and_declared_in_metadata() -> None:
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "Copyright (c) 2026" in text
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert meta["license"] == "MIT"
    assert meta["license-files"] == ["LICENSE"]


def test_issue_forms_are_valid() -> None:
    forms = sorted(p for p in TEMPLATES.glob("*.yml") if p.name != "config.yml")
    assert [p.name for p in forms] == ["bug_report.yml", "feature_request.yml"]
    for path in forms:
        form = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert form["name"] and form["description"], path.name
        ids = [block.get("id") for block in form["body"]]
        assert len(ids) == len(set(ids)), f"duplicate block id in {path.name}"
        for block in form["body"]:
            assert block["type"] in BLOCK_TYPES, f"{path.name}: {block['type']}"
            if block["type"] != "markdown":
                assert block["attributes"]["label"], f"{path.name}: {block.get('id')} has no label"
    config = yaml.safe_load((TEMPLATES / "config.yml").read_text(encoding="utf-8"))
    assert all({"name", "url", "about"} <= set(link) for link in config["contact_links"])


def test_pull_request_template_lists_the_checks_ci_runs() -> None:
    text = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    assert "uv run ruff check ." in text and "uv run pytest" in text
    assert "CHANGELOG.md" in text


def test_changelog_has_an_unreleased_section() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]" in text
    assert "### Added" in text


def test_contributing_names_the_same_commands_as_ci() -> None:
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for command in ("uv run ruff check .", "uv sync --extra dev"):
        assert command in text and command in ci


def test_tracked_paths_stay_short_enough_for_windows() -> None:
    """No tracked path may grow past `MAX_RELATIVE_PATH`.

    Windows caps a full path at 260 characters unless long paths are enabled, and `git clone`
    fails outright on the first file that crosses it. The repository's own paths are only half
    the sum; the rest is wherever someone cloned it. Capping the tracked half well under the
    limit leaves room for a reasonably deep checkout, and turns "please keep eval task names
    short" from advice in CONTRIBUTING into something that fails here first.
    """
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:  # not a git checkout (a source tarball, say): nothing to check
        pytest.skip("not a git checkout")
    too_long = sorted(
        (path for path in result.stdout.splitlines() if len(path) > MAX_RELATIVE_PATH),
        key=len,
        reverse=True,
    )
    assert not too_long, (
        f"tracked paths longer than {MAX_RELATIVE_PATH} characters: "
        + ", ".join(f"{path} ({len(path)})" for path in too_long[:5])
    )
