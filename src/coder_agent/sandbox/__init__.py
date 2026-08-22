"""Sandbox: where the model's commands actually run.

Two implementations share one interface. `local` runs on the host behind a denylist and a
timeout; `docker` runs in a disposable container with the repo as its only mount. `run_command`
here picks one from `settings.sandbox_mode`, so the MCP server and the `run_tests` node never
know which they got. The path jail is the same in both: file tools always run on the host.
"""

from __future__ import annotations

from pathlib import Path

from coder_agent.config import settings
from coder_agent.sandbox import docker, local
from coder_agent.sandbox.local import CommandResult, SandboxError

__all__ = ["CommandResult", "SandboxError", "configure", "docker_limits", "env_overrides", "run_command"]

# Settings the tool server process must agree with the client on. Everything else it reads from
# the same `.env`; these are the ones a CLI flag can change after `.env` was loaded.
_MIRRORED = ("sandbox_mode", "sandbox_image", "sandbox_network", "sandbox_memory", "sandbox_cpus")


def env_overrides() -> dict[str, str]:
    """The sandbox settings as `CODER_*` environment variables for the MCP server subprocess."""
    return {f"CODER_{name.upper()}": str(getattr(settings, name)) for name in _MIRRORED}


def configure(mode: str | None) -> None:
    """Apply a `--sandbox` flag and fail fast if Docker mode cannot work on this machine.

    Raises `SandboxError` for an unknown mode and `docker.DockerUnavailable` when the daemon is
    down or the image cannot be built. Checking here, before the model is called, means a
    missing daemon costs zero tokens instead of failing at the first `run_command`.
    """
    if mode is not None:
        settings.sandbox_mode = mode
    if settings.sandbox_mode == "local":
        return
    if settings.sandbox_mode != "docker":
        raise SandboxError(f"Unknown sandbox mode '{settings.sandbox_mode}' (local | docker).")
    reason = docker.docker_available()
    if reason:
        raise docker.DockerUnavailable(reason)
    docker.ensure_image(settings.sandbox_image)


def docker_limits() -> docker.DockerLimits:
    """The container caps as configured; one place so the CLI banner and the runner agree."""
    return docker.DockerLimits(
        image=settings.sandbox_image,
        network=settings.sandbox_network,
        memory=settings.sandbox_memory,
        cpus=settings.sandbox_cpus,
    )


def run_command(repo: Path, command: str, timeout: int | None = None) -> CommandResult:
    """Run a shell command in the configured sandbox with the repo as working directory."""
    timeout = settings.command_timeout if timeout is None else timeout
    if settings.sandbox_mode == "docker":
        return docker.run_command(repo, command, timeout=timeout, limits=docker_limits())
    if settings.sandbox_mode != "local":
        raise SandboxError(f"Unknown sandbox mode '{settings.sandbox_mode}' (local | docker).")
    return local.run_command(repo, command, timeout=timeout)
