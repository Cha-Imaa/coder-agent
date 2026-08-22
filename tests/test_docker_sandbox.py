"""Docker sandbox tests.

The argv-level tests need no Docker: `run` is injected, so they pin what we ask the daemon for
(no network, resource caps, the repo as the only mount, a kill on timeout). The integration tests
at the end talk to a real daemon and are skipped when there is none; they prove the isolation
holds where it matters: no network, no host filesystem, writes land in the repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coder_agent.config import settings
from coder_agent.sandbox import configure, docker_limits, env_overrides
from coder_agent.sandbox import run_command as dispatch
from coder_agent.sandbox.docker import (
    DEFAULT_IMAGE,
    DockerLimits,
    DockerUnavailable,
    build_argv,
    docker_available,
    ensure_image,
)
from coder_agent.sandbox.docker import run_command as docker_run
from coder_agent.sandbox.local import SandboxError


class FakeRun:
    """Stands in for subprocess.run: records argv, replays a scripted result per call."""

    def __init__(self, *results: subprocess.CompletedProcess | BaseException) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def ok(stdout: str = "", code: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr="")


# --- argv -----------------------------------------------------------------------------------


def test_argv_isolates_the_container(tmp_path: Path) -> None:
    argv = build_argv(tmp_path, "python -m pytest -q", DockerLimits(), "coder-abc", timeout=30)
    joined = " ".join(argv)
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--network none" in joined
    assert "--memory 1g" in joined and "--cpus 1.0" in joined and "--pids-limit 256" in joined
    assert "--cap-drop ALL" in joined
    assert "--tmpfs /tmp" in joined
    assert argv[argv.index("-v") + 1] == f"{tmp_path.resolve()}:/work"
    assert argv.count("-v") == 1, "the repo must be the only mount"
    assert argv[argv.index("-w") + 1] == "/work"
    assert argv[-3:] == [DEFAULT_IMAGE, "sh", "-c"] or argv[-4:-1] == [DEFAULT_IMAGE, "sh", "-c"]
    assert argv[-1] == "python -m pytest -q"


def test_argv_honours_limits(tmp_path: Path) -> None:
    limits = DockerLimits(image="python:3.12", network="bridge", memory="512m", cpus=2.0, pids=64)
    joined = " ".join(build_argv(tmp_path, "true", limits, "n", timeout=5))
    assert "--network bridge" in joined and "--memory 512m" in joined
    assert "--cpus 2.0" in joined and "--pids-limit 64" in joined
    assert " python:3.12 sh -c true" in joined


# --- run_command -------------------------------------------------------------------------------


def test_run_returns_exit_code_and_output(tmp_path: Path) -> None:
    fake = FakeRun(ok("2 passed\n", code=0))
    result = docker_run(tmp_path, "python -m pytest -q", timeout=10, run=fake)
    assert result.exit_code == 0 and result.output == "2 passed" and not result.timed_out
    assert fake.calls[0][:2] == ["docker", "run"]
    assert fake.calls[0][-1] == "python -m pytest -q"


def test_timeout_kills_the_named_container(tmp_path: Path) -> None:
    fake = FakeRun(subprocess.TimeoutExpired(cmd="docker", timeout=1, output=b"partial"), ok())
    result = docker_run(tmp_path, "sleep 999", timeout=1, run=fake)
    assert result.timed_out and result.exit_code == -1 and result.output == "partial"
    run_argv, kill_argv = fake.calls
    name = run_argv[run_argv.index("--name") + 1]
    assert name.startswith("coder-")
    assert kill_argv == ["docker", "kill", name]


@pytest.mark.parametrize(
    ("code", "stderr"),
    [
        (125, 'docker: error during connect: Post "http://.../containers/create": EOF.\n'),
        (127, ("docker: error during connect: this error may indicate that the docker daemon "
               "is not running: open //./pipe/docker_engine: file not found.\n")),
        (125, "docker: Error response from daemon: No such image: coder-sandbox:latest.\n"),
    ],
)
def test_daemon_failure_is_an_error_not_a_command_result(
    tmp_path: Path, code: int, stderr: str
) -> None:
    proc = subprocess.CompletedProcess(args=[], returncode=code, stdout="", stderr=stderr)
    with pytest.raises(DockerUnavailable, match="docker sandbox unavailable"):
        docker_run(tmp_path, "python -m pytest", run=FakeRun(proc))
    assert isinstance(DockerUnavailable(""), SandboxError), "the tool server reports it as ERROR:"


def test_command_that_exits_125_is_still_a_command_result(tmp_path: Path) -> None:
    proc = subprocess.CompletedProcess(args=[], returncode=125, stdout="ran", stderr="boom")
    result = docker_run(tmp_path, "exit 125", run=FakeRun(proc))
    assert result.exit_code == 125 and "ran" in result.output


def test_denylist_applies_before_docker_is_called(tmp_path: Path) -> None:
    fake = FakeRun()
    with pytest.raises(SandboxError):
        docker_run(tmp_path, "git push origin main", run=fake)
    assert fake.calls == []


# --- availability probe ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "stdout", "stderr", "available"),
    [
        (0, "24.0.6\n", "", True),
        (0, "\n", "error during connect: daemon not running", False),  # the Windows case
        (1, "", "Cannot connect to the Docker daemon", False),
    ],
)
def test_docker_available_requires_a_server_version(
    monkeypatch, code: int, stdout: str, stderr: str, available: bool
) -> None:
    proc = subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)
    monkeypatch.setattr("coder_agent.sandbox.docker.subprocess.run", lambda *a, **k: proc)
    docker_available.cache_clear()
    try:
        assert (docker_available() is None) is available
    finally:
        docker_available.cache_clear()


# --- image ---------------------------------------------------------------------------------------


def test_ensure_image_builds_default_when_missing() -> None:
    fake = FakeRun(ok(code=1), ok())
    ensure_image(DEFAULT_IMAGE, run=fake)
    assert fake.calls[0][:3] == ["docker", "image", "inspect"]
    assert fake.calls[1][:2] == ["docker", "build"] and "-t" in fake.calls[1]
    assert fake.calls[1][fake.calls[1].index("-t") + 1] == DEFAULT_IMAGE


def test_ensure_image_never_builds_a_foreign_tag() -> None:
    fake = FakeRun(ok(code=1))
    with pytest.raises(DockerUnavailable, match="not found"):
        ensure_image("python:3.12", run=fake)
    assert len(fake.calls) == 1


def test_ensure_image_is_a_noop_when_present() -> None:
    fake = FakeRun(ok())
    ensure_image(DEFAULT_IMAGE, run=fake)
    assert len(fake.calls) == 1


# --- dispatch and configuration -------------------------------------------------------------


def test_dispatcher_picks_docker_from_settings(tmp_path: Path, monkeypatch) -> None:
    seen: dict = {}

    def fake_docker(repo, command, timeout, limits):
        seen.update(repo=repo, command=command, timeout=timeout, limits=limits)
        return "docker-result"

    monkeypatch.setattr(settings, "sandbox_mode", "docker")
    monkeypatch.setattr(settings, "sandbox_memory", "256m")
    monkeypatch.setattr("coder_agent.sandbox.docker.run_command", fake_docker)
    assert dispatch(tmp_path, "echo hi", timeout=7) == "docker-result"
    assert seen["command"] == "echo hi" and seen["timeout"] == 7
    assert seen["limits"].memory == "256m" and seen["limits"].network == "none"


def test_dispatcher_defaults_to_local(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "sandbox_mode", "local")
    result = dispatch(tmp_path, "python -c \"print('local')\"")
    assert result.exit_code == 0 and result.output == "local"


def test_dispatcher_rejects_unknown_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "sandbox_mode", "firecracker")
    with pytest.raises(SandboxError, match="Unknown sandbox mode"):
        dispatch(tmp_path, "true")


def test_env_overrides_mirror_settings_for_the_server_process(monkeypatch) -> None:
    monkeypatch.setattr(settings, "sandbox_mode", "docker")
    monkeypatch.setattr(settings, "sandbox_image", "custom:1")
    env = env_overrides()
    assert env["CODER_SANDBOX_MODE"] == "docker" and env["CODER_SANDBOX_IMAGE"] == "custom:1"
    assert set(env) >= {"CODER_SANDBOX_NETWORK", "CODER_SANDBOX_MEMORY", "CODER_SANDBOX_CPUS"}


def test_configure_fails_fast_without_a_daemon(monkeypatch) -> None:
    monkeypatch.setattr(settings, "sandbox_mode", "local")
    monkeypatch.setattr("coder_agent.sandbox.docker.docker_available", lambda: "daemon down")
    with pytest.raises(DockerUnavailable, match="daemon down"):
        configure("docker")
    assert settings.sandbox_mode == "docker", "the flag is applied before the check"


def test_configure_local_touches_nothing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "sandbox_mode", "docker")
    monkeypatch.setattr(
        "coder_agent.sandbox.docker.docker_available", lambda: pytest.fail("should not probe")
    )
    configure("local")
    assert settings.sandbox_mode == "local"


# --- integration: a real daemon ---------------------------------------------------------------

needs_docker = pytest.mark.skipif(
    docker_available() is not None, reason=docker_available() or "docker available"
)


@pytest.fixture(scope="module")
def image() -> str:
    if docker_available() is not None:
        pytest.skip("no docker daemon")
    ensure_image(DEFAULT_IMAGE)
    return DEFAULT_IMAGE


@needs_docker
def test_container_runs_pytest_against_the_mounted_repo(tmp_path: Path, image: str) -> None:
    (tmp_path / "test_x.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    result = docker_run(
        tmp_path, "python -m pytest -q -p no:cacheprovider", timeout=120,
        limits=DockerLimits(image=image),
    )
    assert result.exit_code == 0, result.output
    assert "1 passed" in result.output


@needs_docker
def test_container_writes_land_in_the_repo_and_nowhere_else(tmp_path: Path, image: str) -> None:
    result = docker_run(
        tmp_path, "echo made-inside > out.txt && ls / | grep -c Users", timeout=60,
        limits=DockerLimits(image=image),
    )
    assert (tmp_path / "out.txt").read_text().strip() == "made-inside"
    assert result.exit_code != 0, "no host directories are visible inside the container"


@needs_docker
def test_container_has_no_network(tmp_path: Path, image: str) -> None:
    probe = (
        "python -c \"import socket; socket.setdefaulttimeout(3); "
        "socket.create_connection(('1.1.1.1', 53))\""
    )
    result = docker_run(tmp_path, probe, timeout=60, limits=DockerLimits(image=image))
    assert result.exit_code != 0
    assert "Network is unreachable" in result.output or "OSError" in result.output


@needs_docker
def test_dispatcher_end_to_end_in_docker(tmp_path: Path, image: str, monkeypatch) -> None:
    monkeypatch.setattr(settings, "sandbox_mode", "docker")
    monkeypatch.setattr(settings, "sandbox_image", image)
    result = dispatch(tmp_path, "cat /etc/os-release | head -1", timeout=60)
    assert result.exit_code == 0 and "Debian" in result.output
    assert docker_limits().image == image
