"""MCP server exposing the coding tools.

This is the "hands" of the agent, deliberately kept as a separate process that speaks the Model
Context Protocol over stdio. The agent (or any other MCP client: an IDE, an inspector) launches it
with the target repository as an argument and discovers the tools at runtime.

Design choices copied from production coding agents:
- Few, general tools (read, edit, write, list, search, run) rather than task-specific ones.
- `edit_file` is search-and-replace with a uniqueness check, not a whole-file rewrite.
- Tool descriptions are prescriptive: they tell the model when and how to use each tool. That text
  is part of the prompt, so it is written for the model, not for humans.
- Every path and command goes through the sandbox.

Run standalone:  python -m coder_agent.mcp_server.server <repo_path>
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from coder_agent.sandbox.local import SandboxError, resolve_in_repo
from coder_agent.sandbox.local import run_command as sandbox_run

# The repo root is fixed for the lifetime of the server process. It comes from argv so the client
# controls it and the model cannot change it.
REPO = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CODER_REPO", ".")).resolve()

mcp = FastMCP(
    "coder-tools",
    instructions=(
        "File and shell tools scoped to a single repository. Paths are relative to the repo root. "
        "Read a file before editing it. Prefer edit_file over write_file for existing files."
    ),
)

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", "dist", "build", ".coder-agent", ".chroma", ".idea", ".vscode",
}
MAX_READ_CHARS = 60_000
MAX_SEARCH_RESULTS = 200


def _rel(p: Path) -> str:
    return p.relative_to(REPO).as_posix()


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return b"\0" in f.read(1024)
    except OSError:
        return True


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
    """Read a text file from the repository, with line numbers.

    Use this before editing any file so your edit matches the file exactly. For large files pass
    start_line and end_line to read only the region you need; reading whole large files wastes
    context. Output format is `LINE_NUMBER|content`. Paths are relative to the repo root.
    """
    try:
        target = resolve_in_repo(REPO, path)
    except SandboxError as e:
        return f"ERROR: {e}"
    if not target.is_file():
        return f"ERROR: '{path}' is not a file."
    if _is_binary(target):
        return f"ERROR: '{path}' looks binary; refusing to read."
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    start = max(1, start_line)
    end = min(total, end_line) if end_line else total
    selected = lines[start - 1 : end]
    body = "\n".join(f"{i}|{line}" for i, line in enumerate(selected, start=start))
    if len(body) > MAX_READ_CHARS:
        body = body[:MAX_READ_CHARS] + f"\n... [truncated; file has {total} lines, use start_line/end_line]"
    header = f"# {path} (lines {start}-{end} of {total})\n"
    return header + body


@mcp.tool()
def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace one exact occurrence of old_string with new_string in an existing file.

    This is the preferred way to change code. old_string must match the file text exactly,
    including indentation and whitespace, and must occur exactly once; include a few surrounding
    lines to make it unique. Do not include line-number prefixes from read_file. Fails without
    changing anything if the match is missing or ambiguous.
    """
    try:
        target = resolve_in_repo(REPO, path)
    except SandboxError as e:
        return f"ERROR: {e}"
    if not target.is_file():
        return f"ERROR: '{path}' does not exist. Use write_file to create a new file."
    if old_string == new_string:
        return "ERROR: old_string and new_string are identical; nothing to do."
    content = target.read_text(encoding="utf-8")
    count = content.count(old_string)
    if count == 0:
        return (
            "ERROR: old_string not found. Re-read the file and copy the text exactly "
            "(check indentation and whitespace)."
        )
    if count > 1:
        return (
            f"ERROR: old_string occurs {count} times; include more surrounding context "
            "so it matches exactly once."
        )
    updated = content.replace(old_string, new_string, 1)
    target.write_text(updated, encoding="utf-8")
    # A unified diff lets the model verify the edit landed where intended, and lets the UI show
    # the change without re-reading the file.
    diff = difflib.unified_diff(
        content.splitlines(), updated.splitlines(), fromfile=path, tofile=path, lineterm="", n=2
    )
    return f"OK: edited {path}.\n" + "\n".join(diff)


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Create a new file or completely overwrite an existing one with content.

    Use for new files. For modifying existing files prefer edit_file, which is safer and cheaper.
    Parent directories are created as needed.
    """
    try:
        target = resolve_in_repo(REPO, path)
    except SandboxError as e:
        return f"ERROR: {e}"
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    verb = "overwrote" if existed else "created"
    return f"OK: {verb} {path} ({len(content)} chars)."


@mcp.tool()
def list_dir(path: str = ".", max_depth: int = 2) -> str:
    """List files and directories under path as an indented tree.

    Start with the repo root to orient yourself. Common noise directories (.git, node_modules,
    virtualenvs, caches) are skipped. Increase max_depth to see deeper.
    """
    try:
        root = resolve_in_repo(REPO, path)
    except SandboxError as e:
        return f"ERROR: {e}"
    if not root.is_dir():
        return f"ERROR: '{path}' is not a directory."

    lines: list[str] = [f"{_rel(root) or '.'}/"]

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for entry in entries:
            if entry.name in SKIP_DIRS:
                continue
            indent = "  " * depth
            if entry.is_dir():
                lines.append(f"{indent}{entry.name}/")
                walk(entry, depth + 1)
            else:
                lines.append(f"{indent}{entry.name}")

    walk(root, 1)
    return "\n".join(lines)


@mcp.tool()
def search_code(pattern: str, path: str = ".", glob: str = "*", max_results: int = 50) -> str:
    """Search file contents with a regular expression and return matching lines.

    Use this to find where a symbol is defined or used before reading files; it is far cheaper
    than reading files one by one. Results are `relative/path:line_number:content`. Narrow with
    glob (for example "*.py") when the repo is large. Case-insensitive.
    """
    try:
        root = resolve_in_repo(REPO, path)
    except SandboxError as e:
        return f"ERROR: {e}"
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"ERROR: invalid regex: {e}"

    limit = min(max_results, MAX_SEARCH_RESULTS)
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not fnmatch.fnmatch(name, glob):
                continue
            fp = Path(dirpath) / name
            if _is_binary(fp):
                continue
            try:
                for i, line in enumerate(fp.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if regex.search(line):
                        hits.append(f"{_rel(fp)}:{i}:{line.strip()[:200]}")
                        if len(hits) >= limit:
                            return "\n".join(hits) + f"\n... [stopped at {limit} results; narrow the search]"
            except OSError:
                continue
    return "\n".join(hits) if hits else "No matches."


@mcp.tool()
def run_command(command: str, timeout: int = 120) -> str:
    """Run a shell command in the repository root and return exit code and output.

    Use it to run tests (for example `python -m pytest -q`), linters, or scripts. Destructive
    commands (recursive deletes, git push, package installs) are blocked by policy. Output is
    truncated in the middle if very long; the exit code is always on the first line.
    """
    try:
        result = sandbox_run(REPO, command, timeout=timeout)
    except SandboxError as e:
        return f"ERROR: {e}"
    return result.as_text()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
