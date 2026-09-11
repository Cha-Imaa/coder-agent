"""Docker sandbox: run the model's commands in a throwaway container, not on the host.

The local sandbox (`local.py`) is a guardrail against accidents: a denylist can only block the
patterns it knows. This module is the security boundary the plan promises. Every command runs in a
fresh container that sees exactly one thing from the host, the target repository, mounted at
`/work`. No network, a memory cap, a CPU cap, a process cap, and the container is deleted when the
command ends. A `curl | sh` the denylist missed downloads nothing; a runaway script cannot fill
the host's memory; a file outside the repo does not exist to be deleted.

The container is driven through the `docker` CLI rather than the Docker SDK: the CLI is already
installed wherever Docker is, it speaks to Docker Desktop on Windows and to the daemon socket on
Linux without configuration, and its argv is easy to read in a test.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from coder_agent.sandbox.local import (
    DEFAULT_OUTPUT_CAP,
    CommandResult,
    SandboxError,
    _truncate,
    check_command,
)

DEFAULT_IMAGE = "coder-sandbox"
DOCKERFILE_DIR = Path(__file__).parent  # holds the Dockerfile that builds DEFAULT_IMAGE
WORKDIR = "/work"

# Injected in tests so the argv can be checked without a daemon. Same signature as subprocess.run.
Runner = Callable[..., subprocess.CompletedProcess]


class DockerUnavailable(SandboxError):
    """The docker CLI, daemon or image is missing.

    A `SandboxError` so the tool server reports it the same way as a blocked command, prefixed
    `ERROR:`, rather than as a failed test run the model would then try to "fix". `configure`
    raises it before a run starts; `run_command` raises it if the daemon dies mid-run, which
    stops the run with a checkpoint to resume from once Docker is back.
    """


# The docker CLI exits 125 (or 127 on Windows when the named pipe is gone) when the daemon, not
# the contained command, failed. A user command can exit with those codes too, so the CLI's own
# stderr wording and an empty stdout are required as well before a result is treated as infra.
_DAEMON_FAILURE_EXITS = frozenset({125, 127})
_DAEMON_FAILURE_MARKERS = (
    "error during connect",
    "Cannot connect to the Docker daemon",
    "Error response from daemon",
)


def _daemon_failed(returncode: int, stdout: str, stderr: str) -> bool:
    return (
        returncode in _DAEMON_FAILURE_EXITS
        and not stdout.strip()
        and any(marker in stderr for marker in _DAEMON_FAILURE_MARKERS)
    )


@dataclass(frozen=True)
class DockerLimits:
    """Resource caps for one command. Defaults are generous for a test suite, tight for abuse."""

    image: str = DEFAULT_IMAGE
    network: str = "none"  # "none" is the point; "bridge" only for tasks that must fetch
    memory: str = "1g"
    cpus: float = 1.0
    pids: int = 256


@lru_cache(maxsize=1)
def docker_available() -> str | None:
    """None when the daemon answers `docker info`, otherwise a one-line reason for the user.

    `docker info --format` exits 0 even when the daemon is down: the client half of the report
    succeeded, the server fields are simply empty and the connection error goes to stderr. So the
    check is "did we get a server version", not "did the command exit 0". Cached: the probe spawns
    a process and the answer does not change within a run (`run_command` clears the cache if the
    daemon disappears).
    """
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except FileNotFoundError:
        return "docker CLI not found on PATH; install Docker Desktop or docker-ce."
    except subprocess.TimeoutExpired:
        return "docker info timed out; is the daemon starting?"
    if proc.returncode != 0 or not proc.stdout.strip():
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return "docker daemon not reachable: " + (detail[-1] if detail else "unknown error")
    return None


def ensure_image(image: str = DEFAULT_IMAGE, run: Runner = subprocess.run) -> None:
    """Build our default image the first time it is needed; other images must already exist.

    Only the image we ship a Dockerfile for is built automatically. Building an arbitrary tag from
    an unknown context is exactly the kind of surprise a sandbox should not produce.
    """
    probe = run(["docker", "image", "inspect", image], capture_output=True, text=True, check=False)
    if probe.returncode == 0:
        return
    if image != DEFAULT_IMAGE:
        raise DockerUnavailable(f"docker image '{image}' not found; pull or build it first.")
    build = run(
        ["docker", "build", "--quiet", "-t", DEFAULT_IMAGE, str(DOCKERFILE_DIR)],
        capture_output=True, text=True, check=False,
    )
    if build.returncode != 0:
        raise DockerUnavailable(f"building {DEFAULT_IMAGE} failed:\n{build.stderr.strip()}")


def build_argv(
    repo: Path,
    command: str,
    limits: DockerLimits,
    name: str,
    timeout: int,
) -> list[str]:
    """The `docker run` invocation for one command. Pure, so it can be tested without Docker.

    The repo is the only mount and is read-write: the tests the agent runs need to write
    `__pycache__`, coverage files, build output. `/tmp` is a tmpfs so scratch files never land
    in the repo. On Linux the container runs as the calling user so anything it writes into the
    mount is owned by them, not by root; Docker Desktop on Windows and macOS remaps ownership
    itself and has no uid to pass.
    """
    argv = [
        "docker", "run", "--rm",
        "--name", name,
        "--network", limits.network,
        "--memory", limits.memory,
        "--cpus", str(limits.cpus),
        "--pids-limit", str(limits.pids),
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp",
        "--stop-timeout", str(timeout),
        "-v", f"{repo.resolve()}:{WORKDIR}",
        "-w", WORKDIR,
        "-e", "PYTHONUNBUFFERED=1", "-e", "NO_COLOR=1", "-e", "PYTHONDONTWRITEBYTECODE=1",
    ]
    if hasattr(os, "getuid"):
        argv += ["--user", f"{os.getuid()}:{os.getgid()}"]  # type: ignore[attr-defined]
    argv += [limits.image, "sh", "-c", command]
    return argv


def run_command(
    repo: Path,
    command: str,
    timeout: int = 120,
    output_cap: int = DEFAULT_OUTPUT_CAP,
    allow_installs: bool = False,
    limits: DockerLimits | None = None,
    run: Runner = subprocess.run,
) -> CommandResult:
    """Run `command` inside a fresh container with `repo` mounted at /work.

    The denylist still applies first: a blocked `git push` should be refused with the same message
    in both modes, and a container with no network would otherwise turn it into a confusing
    "could not resolve host". On timeout `subprocess` kills the docker CLI, but that does not
    stop the container, which would keep burning CPU until the daemon noticed. So the container
    gets a name and an explicit `docker kill`.
    """
    check_command(command, allow_installs=allow_installs)
    limits = limits or DockerLimits()
    name = f"coder-{uuid.uuid4().hex[:12]}"
    argv = build_argv(repo, command, limits, name, timeout)
    try:
        proc = run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        run(["docker", "kill", name], capture_output=True, text=True, check=False)
        partial = exc.stdout or b""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        output, truncated = _truncate(partial.strip(), output_cap)
        return CommandResult(command, -1, output, timed_out=True, truncated=truncated)
    stderr = proc.stderr or ""
    if _daemon_failed(proc.returncode, proc.stdout or "", stderr):
        docker_available.cache_clear()  # the cached "available" answer is now wrong
        raise DockerUnavailable(
            "docker sandbox unavailable: " + stderr.strip().splitlines()[0]
            + " (is the daemon running? restart it and resume the run)"
        )
    combined = proc.stdout + (("\n" + stderr) if stderr else "")
    output, truncated = _truncate(combined.strip(), output_cap)
    return CommandResult(command, proc.returncode, output, timed_out=False, truncated=truncated)
