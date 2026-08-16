"""Repo loader: decide which files are worth indexing and read them once.

The retriever should see what a developer would `grep`: source, config and docs that are tracked
by git. Not the virtualenv, not build output, not the `.git` object store, not a 40 MB fixture.
Git already encodes that judgement in `.gitignore`, so we reuse it instead of inventing a second
list of exclusions that would drift from the first.

Every file is hashed on the way in. The incremental index (step 5.2) keys on that hash: an
unchanged file is skipped, a changed file is re-chunked, a deleted file is dropped.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pathspec import GitIgnoreSpec

from coder_agent.config import settings

# Directories never worth reading even when a repo forgot to ignore them. Kept short: the point is
# to honour the repo's own `.gitignore`, not to replace it.
ALWAYS_IGNORED_DIRS: frozenset[str] = frozenset(
    {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".chroma"}
)

# Extension -> language name as tree-sitter-language-pack spells it. Languages listed here but
# absent from `chunker.DEFINITIONS` are still detected (useful metadata for the retriever) but
# chunked by line window.
LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".cs": "c_sharp",
    ".md": "markdown",
    ".rst": "rst",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".sh": "bash",
    ".ps1": "powershell",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
}

_BINARY_SNIFF_BYTES = 8_000


@dataclass(frozen=True)
class RepoFile:
    """One indexable file. `path` is POSIX-relative to the repo so ids are stable across OSes."""

    path: str
    text: str
    sha256: str
    language: str | None

    @property
    def line_count(self) -> int:
        return self.text.count("\n") + (0 if self.text.endswith("\n") or not self.text else 1)


def detect_language(path: str) -> str | None:
    return LANGUAGE_BY_SUFFIX.get(PurePosixPath(path).suffix.lower())


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_binary(sample: bytes) -> bool:
    """Git's own heuristic: a NUL byte in the first few kilobytes means binary."""
    return b"\0" in sample[:_BINARY_SNIFF_BYTES]


class _IgnoreRules:
    """Nested `.gitignore` semantics: each file's patterns apply to its own subtree only.

    A path is judged by every `.gitignore` on the way down from the repo root to its parent
    directory, outermost first. Patterns are matched against the path relative to the directory
    holding that `.gitignore`, which is what makes an anchored pattern like `/build` mean "build at
    this level" and not "any build anywhere". A deeper file can re-include with `!` what an outer
    one ignored, so the last file that has an opinion wins.
    """

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self._cache: dict[PurePosixPath, GitIgnoreSpec | None] = {}

    def _spec_for(self, rel_dir: PurePosixPath) -> GitIgnoreSpec | None:
        if rel_dir in self._cache:
            return self._cache[rel_dir]
        lines: list[str] = []
        if rel_dir == PurePosixPath("."):
            lines.append(f"{settings.state_dir_name}/")  # our own index must never index itself
            exclude = self.repo / ".git" / "info" / "exclude"
            if exclude.is_file():
                lines.extend(exclude.read_text(encoding="utf-8", errors="replace").splitlines())
        gitignore = self.repo / rel_dir / ".gitignore"
        if gitignore.is_file():
            lines.extend(gitignore.read_text(encoding="utf-8", errors="replace").splitlines())
        spec = GitIgnoreSpec.from_lines(lines) if lines else None
        self._cache[rel_dir] = spec
        return spec

    def ignored(self, rel_path: PurePosixPath, *, is_dir: bool) -> bool:
        verdict = False
        # Ancestors from the root down to the parent directory, e.g. ".", "src", "src/pkg".
        chain = [PurePosixPath(".")] + [
            PurePosixPath(*rel_path.parts[:i]) for i in range(1, len(rel_path.parts))
        ]
        for base in chain:
            spec = self._spec_for(base)
            if spec is None:
                continue
            local = rel_path if base == PurePosixPath(".") else rel_path.relative_to(base)
            probe = f"{local.as_posix()}/" if is_dir else local.as_posix()
            result = spec.check_file(probe)
            if result.include is not None:  # None: no pattern in this file matched
                verdict = result.include
        return verdict


def iter_repo_paths(repo: Path) -> Iterator[Path]:
    """Yield every file under `repo` that git would track, in a deterministic order.

    Walks top-down so ignored directories are pruned before we ever list their contents; a
    `node_modules` with 40k files costs nothing.
    """
    repo = repo.resolve()
    rules = _IgnoreRules(repo)
    for dirpath, dirnames, filenames in os.walk(repo, topdown=True):
        rel_dir = PurePosixPath(Path(dirpath).relative_to(repo).as_posix())

        kept: list[str] = []
        for name in sorted(dirnames):
            if name in ALWAYS_IGNORED_DIRS:
                continue
            if rules.ignored(rel_dir / name, is_dir=True):
                continue
            kept.append(name)
        dirnames[:] = kept  # os.walk reads this list back to decide where to descend

        for name in sorted(filenames):
            rel = rel_dir / name
            if not rules.ignored(rel, is_dir=False):
                yield repo / rel


def load_file(repo: Path, path: Path, *, max_bytes: int | None = None) -> RepoFile | None:
    """Read one file, or return None if it is binary or over the size cap."""
    cap = max_bytes if max_bytes is not None else settings.index_max_file_kb * 1024
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > cap or is_binary(data):
        return None
    rel = path.resolve().relative_to(repo.resolve()).as_posix()
    # Normalise line endings so a checkout with `core.autocrlf` chunks identically to one without;
    # the hash stays on the raw bytes because that is what "did the file change" means on disk.
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    return RepoFile(
        path=rel,
        text=text,
        sha256=file_hash(data),
        language=detect_language(rel),
    )


def load_repo(repo: Path, *, max_bytes: int | None = None) -> list[RepoFile]:
    """Everything `iter_repo_paths` yields, read and hashed, minus binaries and oversized files."""
    files: list[RepoFile] = []
    for path in iter_repo_paths(repo):
        loaded = load_file(repo, path, max_bytes=max_bytes)
        if loaded is not None:
            files.append(loaded)
    return files
