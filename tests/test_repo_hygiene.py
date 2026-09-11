"""The files GitHub reads to describe the project: licence, templates, changelog, metadata.

A malformed issue form is silently ignored by GitHub (the template just does not appear), and a
licence the metadata does not mention is invisible on PyPI, so both are checked here.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"
BLOCK_TYPES = {"input", "textarea", "dropdown", "checkboxes", "markdown"}


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
