"""Repo loader: the index sees what git tracks, hashed, with binaries and junk left out."""

from __future__ import annotations

from pathlib import Path

import pytest

from coder_agent.config import settings
from coder_agent.rag import RepoFile, detect_language, iter_repo_paths, load_file, load_repo


def _write(root: Path, rel: str, content: str | bytes = "x = 1\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _write(tmp_path, ".gitignore", "*.log\nbuild/\n/dist\nsecret.txt\n!keep.log\n")
    _write(tmp_path, "src/app.py", "def main():\n    return 1\n")
    _write(tmp_path, "src/util.py")
    _write(tmp_path, "README.md", "# hi\n")
    _write(tmp_path, "debug.log", "noise")
    _write(tmp_path, "keep.log", "kept by negation")
    _write(tmp_path, "build/out.py")
    _write(tmp_path, "dist/x.py")
    _write(tmp_path, "src/dist/inner.py")  # `/dist` is anchored: only the root one is ignored
    _write(tmp_path, "secret.txt", "shh")
    _write(tmp_path, ".git/HEAD", "ref: refs/heads/main\n")
    _write(tmp_path, "node_modules/lib/index.js", "module.exports = 1\n")
    _write(tmp_path, f"{settings.state_dir_name}/index.db", "our own state")
    return tmp_path


def rel_paths(repo: Path) -> list[str]:
    return [p.relative_to(repo).as_posix() for p in iter_repo_paths(repo)]


def test_gitignore_patterns_are_honoured(repo: Path) -> None:
    paths = rel_paths(repo)
    assert "src/app.py" in paths
    assert "README.md" in paths
    assert ".gitignore" in paths  # tracked by git, so indexed too
    assert "debug.log" not in paths
    assert "keep.log" in paths  # `!keep.log` re-includes it
    assert "build/out.py" not in paths
    assert "dist/x.py" not in paths
    assert "src/dist/inner.py" in paths  # anchored pattern does not reach nested dirs
    assert "secret.txt" not in paths


def test_always_ignored_dirs_and_own_state_are_skipped(repo: Path) -> None:
    paths = rel_paths(repo)
    assert not any(p.startswith(".git/") for p in paths)
    assert not any(p.startswith("node_modules/") for p in paths)
    assert not any(p.startswith(settings.state_dir_name) for p in paths)


def test_nested_gitignore_applies_to_its_subtree_only(repo: Path) -> None:
    _write(repo, "src/.gitignore", "generated_*.py\n")
    _write(repo, "src/generated_a.py")
    _write(repo, "generated_top.py")  # same name at root: the nested rule must not reach it
    paths = rel_paths(repo)
    assert "src/generated_a.py" not in paths
    assert "generated_top.py" in paths


def test_nested_negation_can_reinclude_what_root_ignored(repo: Path) -> None:
    _write(repo, "logs/.gitignore", "!important.log\n")
    _write(repo, "logs/important.log", "keep me")
    _write(repo, "logs/other.log", "drop me")
    paths = rel_paths(repo)
    assert "logs/important.log" in paths
    assert "logs/other.log" not in paths


def test_git_info_exclude_is_read(repo: Path) -> None:
    _write(repo, ".git/info/exclude", "scratch.py\n")
    _write(repo, "scratch.py")
    assert "scratch.py" not in rel_paths(repo)


def test_walk_order_is_deterministic(repo: Path) -> None:
    paths = rel_paths(repo)
    assert paths == rel_paths(repo)
    in_src = [p for p in paths if p.startswith("src/") and p.count("/") == 1]
    assert in_src == sorted(in_src)  # files within one directory come out sorted by name


def test_load_repo_reads_hashes_and_detects_language(repo: Path) -> None:
    files = load_repo(repo)
    by_path = {f.path: f for f in files}
    app = by_path["src/app.py"]
    assert isinstance(app, RepoFile)
    assert app.text == "def main():\n    return 1\n"
    assert app.language == "python"
    assert len(app.sha256) == 64
    assert by_path["README.md"].language == "markdown"
    assert app.line_count == 2


def test_hash_changes_with_content_only(repo: Path) -> None:
    before = load_file(repo, repo / "src" / "app.py")
    again = load_file(repo, repo / "src" / "app.py")
    _write(repo, "src/app.py", "def main():\n    return 2\n")
    after = load_file(repo, repo / "src" / "app.py")
    assert before is not None and again is not None and after is not None
    assert before.sha256 == again.sha256
    assert before.sha256 != after.sha256


def test_binary_and_oversized_files_are_skipped(repo: Path) -> None:
    _write(repo, "img.png", b"\x89PNG\r\n\x1a\n\x00\x00binary")
    _write(repo, "big.py", "x = 1\n" * 1000)
    assert load_file(repo, repo / "img.png") is None
    assert load_file(repo, repo / "big.py", max_bytes=100) is None
    assert load_file(repo, repo / "big.py") is not None


def test_undecodable_bytes_do_not_crash(repo: Path) -> None:
    _write(repo, "latin.py", "# caf\xe9\n".encode("latin-1"))
    loaded = load_file(repo, repo / "latin.py")
    assert loaded is not None
    assert loaded.text.startswith("# caf")


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("a.py", "python"),
        ("src/x.TS", "typescript"),
        ("c.tsx", "tsx"),
        ("main.go", "go"),
        ("lib.rs", "rust"),
        ("Makefile", None),
        ("notes.txt", None),
    ],
)
def test_detect_language(path: str, language: str | None) -> None:
    assert detect_language(path) == language
