"""Sandbox: the policy layer between what the model asks for and what actually runs.

The model is untrusted input. Every filesystem path and every shell command a tool receives passes
through here first. This mirrors the permission layer in production coding agents: the model
proposes, the harness decides.

Two guarantees:
- Path jail: no path may resolve outside the target repository.
- Command policy: a denylist of dangerous commands, a timeout, and a cap on captured output.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


class SandboxError(Exception):
    """Raised when a tool call violates the sandbox policy. The message is shown to the model."""


# ---------------------------------------------------------------------------
# Path jail
# ---------------------------------------------------------------------------


def resolve_in_repo(repo: Path, user_path: str) -> Path:
    """Resolve `user_path` (relative or absolute) and assert it lives inside `repo`.

    `Path.resolve()` collapses `..` segments and follows symlinks, so a path like
    `src/../../etc/passwd` becomes its real absolute location before the containment check.
    """
    repo_root = repo.resolve()
    candidate = Path(user_path)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError:
        raise SandboxError(
            f"Path '{user_path}' is outside the repository root '{repo_root}'."
        ) from None
    return resolved


# ---------------------------------------------------------------------------
# Command policy
# ---------------------------------------------------------------------------

# Patterns are matched against the normalised command string. Keep this list short and obvious;
# it is a guardrail against accidents, not a security boundary (Docker mode is that).
DENIED_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\b",  # rm -rf / rm -fr and variants
        r"\bRemove-Item\b.*-Recurse",
        r"\brmdir\s+/s",
        r"\bdel\s+/[sq]",
        r"\bgit\s+push\b",
        r"\bgit\s+reset\s+--hard\b",
        r"\bgit\s+clean\b",
        r"\bgit\s+checkout\s+--\s",
        r"\b(pip|pip3|uv\s+pip|npm|pnpm|yarn|poetry)\s+(install|add|i)\b",
        r"\bcurl\b.*\|\s*(ba)?sh\b",
        r"\bwget\b.*\|\s*(ba)?sh\b",
        r"\bshutdown\b|\breboot\b",
        r"\bformat\s+[a-z]:",
        r"\bmkfs\b",
        r":\(\)\s*\{.*\}\s*;\s*:",  # fork bomb
    )
)

DEFAULT_OUTPUT_CAP = 20_000  # characters kept from stdout+stderr


def check_command(command: str, allow_installs: bool = False) -> None:
    """Raise SandboxError if the command matches the denylist."""
    normalised = " ".join(command.split())
    for pattern in DENIED_PATTERNS:
        if allow_installs and "install" in pattern.pattern:
            continue
        if pattern.search(normalised):
            raise SandboxError(
                f"Command blocked by sandbox policy (matched /{pattern.pattern}/): {command}"
            )


@dataclass
class CommandResult:
    command: str
    exit_code: int
    output: str
    timed_out: bool
    truncated: bool

    def as_text(self) -> str:
        """Render for the model: exit code first, because that is what it should act on."""
        header = f"exit_code={self.exit_code}"
        if self.timed_out:
            header += " (TIMED OUT)"
        if self.truncated:
            header += " (output truncated)"
        return f"{header}\n{self.output}"


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    """Keep the head and tail of long output. Test failures are usually at the end; the command
    echo and first errors at the start. The middle is the least informative part."""
    if len(text) <= cap:
        return text, False
    head = text[: cap // 2]
    tail = text[-(cap // 2) :]
    omitted = len(text) - cap
    return f"{head}\n\n... [{omitted} characters omitted] ...\n\n{tail}", True


def run_command(
    repo: Path,
    command: str,
    timeout: int = 120,
    output_cap: int = DEFAULT_OUTPUT_CAP,
    allow_installs: bool = False,
) -> CommandResult:
    """Run a shell command with the repo as working directory, under the sandbox policy."""
    check_command(command, allow_installs=allow_installs)
    repo_root = repo.resolve()

    # Use the platform shell so the model can write natural commands (pipes, &&).
    # On Windows that is cmd.exe via shell=True; tests and tools should still prefer
    # cross-platform commands such as `python -m pytest`.
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "NO_COLOR": "1"}
    try:
        proc = subprocess.run(
            command,
            cwd=repo_root,
            shell=True,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        combined = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
        output, truncated = _truncate(combined.strip(), output_cap)
        return CommandResult(command, proc.returncode, output, timed_out=False, truncated=truncated)
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"")
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        output, truncated = _truncate(partial.strip(), output_cap)
        return CommandResult(command, -1, output, timed_out=True, truncated=truncated)


def split_for_display(command: str) -> list[str]:
    """Best-effort tokenisation for logging; never used for execution."""
    try:
        return shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return [command]
