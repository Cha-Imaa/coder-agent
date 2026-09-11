"""`coder fix-issue`: URL parsing, host git, and the pipeline against a bare remote.

The agent step is replaced by a fake that edits a file and reports success, so these tests
prove the plumbing around it: the issue becomes the task, the work lands on `coder/issue-N`,
`--pr` commits, pushes to a local bare repository and posts the pull request to the fake API.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from coder_agent import agent as agent_module
from coder_agent import fix_issue as fx
from coder_agent.agent import RunOutcome
from coder_agent.cli import app
from coder_agent.config import settings
from coder_agent.mcp_server.github import GitHubAPI
from coder_agent.telemetry import RunRecord
from tests.fake_github import FakeGitHub

runner = CliRunner()


# -- URL parsing --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/octo/widgets/issues/7",
        "http://www.github.com/octo/widgets/issues/7/",
        "github.com/octo/widgets/issues/7#issuecomment-123",
        "https://github.com/octo/widgets.git/issues/7?foo=bar",
        "octo/widgets#7",
    ],
)
def test_parse_issue_url_accepts_common_forms(url: str) -> None:
    ref = fx.parse_issue_url(url)
    assert (ref.owner, ref.repo, ref.number) == ("octo", "widgets", 7)
    assert ref.branch == "coder/issue-7"
    assert ref.clone_url == "https://github.com/octo/widgets.git"


@pytest.mark.parametrize(
    "url",
    ["https://github.com/octo/widgets/pull/7", "https://gitlab.com/o/r/issues/1", "octo/widgets", "7"],
)
def test_parse_issue_url_rejects_other_things(url: str) -> None:
    with pytest.raises(fx.FixIssueError, match="Not a GitHub issue URL"):
        fx.parse_issue_url(url)


# -- Host git ------------------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return fx.git(repo, *args)


@pytest.fixture
def remote_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A bare 'origin' and a working clone with one commit on main, like a fresh checkout."""
    origin = tmp_path / "origin.git"
    fx.git(None, "init", "--quiet", "--bare", "--initial-branch=main", str(origin))
    work = tmp_path / "work"
    fx.git(None, "clone", "--quiet", str(origin), str(work))
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "durations.py").write_text("def parse(s):\n    return int(s[:-1])\n", encoding="utf-8")
    _git(work, "add", "--all")
    _git(work, "commit", "--quiet", "-m", "init")
    _git(work, "push", "--quiet", "--set-upstream", "origin", "main")
    return origin, work


def test_start_branch_creates_then_reuses(remote_and_clone: tuple[Path, Path]) -> None:
    _, work = remote_and_clone
    assert fx.start_branch(work, "coder/issue-7") == "coder/issue-7"
    assert fx.current_branch(work) == "coder/issue-7"
    _git(work, "checkout", "--quiet", "main")
    fx.start_branch(work, "coder/issue-7")  # exists: plain checkout, no -b
    assert fx.current_branch(work) == "coder/issue-7"


def test_start_branch_refuses_a_dirty_tree(remote_and_clone: tuple[Path, Path]) -> None:
    _, work = remote_and_clone
    (work / "durations.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(fx.FixIssueError, match="uncommitted changes"):
        fx.start_branch(work, "coder/issue-7")


def test_ensure_checkout_requires_a_git_tree(tmp_path: Path) -> None:
    ref = fx.parse_issue_url("octo/widgets#7")
    with pytest.raises(fx.FixIssueError, match="not a git working tree"):
        fx.ensure_checkout(ref, tmp_path)


def test_ensure_checkout_clones_once_then_fetches(
    remote_and_clone: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, _ = remote_and_clone
    ref = fx.parse_issue_url("octo/widgets#7")
    monkeypatch.setattr(settings, "checkouts_dir", tmp_path / "checkouts")
    # The clone URL is GitHub's; point it at the local bare repo for the test.
    monkeypatch.setattr(fx.IssueRef, "clone_url", property(lambda self: str(origin)))
    first = fx.ensure_checkout(ref, None)
    assert first == tmp_path / "checkouts" / "octo__widgets"
    assert (first / "durations.py").is_file()
    assert fx.ensure_checkout(ref, None) == first  # second call fetches instead of cloning


def test_commit_all_is_a_noop_on_a_clean_tree(remote_and_clone: tuple[Path, Path]) -> None:
    _, work = remote_and_clone
    assert fx.commit_all(work, "nothing") is False
    (work / "new.txt").write_text("x", encoding="utf-8")
    assert fx.commit_all(work, "fix: something (#7)") is True
    assert _git(work, "log", "-1", "--format=%s") == "fix: something (#7)"


# -- The pipeline ------------------------------------------------------------------------------


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch):
    server = FakeGitHub().start()
    server.add_issue(
        "octo", "widgets", 7, "parse drops the unit", body="parse('5m') should be 300.",
        labels=("bug",),
    )
    monkeypatch.setenv("GITHUB_API_URL", server.url)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    yield server
    server.stop()


def _fake_run_agent(status: str = "passed", edit: bool = True):
    """Stand-in for `run_agent`: edits the file the issue is about and reports `status`."""
    seen: dict = {}

    async def run_agent(repo: Path, task: str, **kwargs) -> RunOutcome:
        seen.update(repo=repo, task=task, **kwargs)
        if edit:
            (repo / "durations.py").write_text(
                "UNITS = {'s': 1, 'm': 60}\n\ndef parse(s):\n    return int(s[:-1]) * UNITS[s[-1]]\n",
                encoding="utf-8",
            )
        state = {"repo": str(repo), "task": task, "status": status, "summary": "Multiplied by the unit."}
        record = RunRecord.from_state(state, model="fake", wall_seconds=0.1)
        return RunOutcome(final=state, record=record, thread_id="t1")

    run_agent.seen = seen  # type: ignore[attr-defined]
    return run_agent


async def test_fix_issue_without_pr_leaves_edits_on_the_branch(
    fake: FakeGitHub, remote_and_clone: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work = remote_and_clone
    run_agent = _fake_run_agent()
    monkeypatch.setattr(agent_module, "run_agent", run_agent)
    stages: list[str] = []

    result = await fx.fix_issue(
        "https://github.com/octo/widgets/issues/7", repo_path=work, api=GitHubAPI.from_env(),
        on_stage=lambda name, _: stages.append(name),
    )

    assert result.passed and not result.committed and result.pull_request is None
    assert stages == ["issue", "checkout", "agent"]
    assert fx.current_branch(work) == "coder/issue-7" and fx.is_dirty(work)
    task = run_agent.seen["task"]
    assert task.startswith("Fix the GitHub issue below.")
    assert "# parse drops the unit (#7)" in task and "should be 300" in task
    assert run_agent.seen["github"] is True
    assert run_agent.seen["tags"] == {"source": "github", "issue": result.ref.url}
    assert not fake.pull_requests


async def test_fix_issue_with_pr_commits_pushes_and_opens(
    fake: FakeGitHub, remote_and_clone: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, work = remote_and_clone
    monkeypatch.setattr(agent_module, "run_agent", _fake_run_agent())

    result = await fx.fix_issue("octo/widgets#7", repo_path=work, open_pr=True, api=GitHubAPI.from_env())

    assert result.committed and result.pushed
    assert result.pull_request["url"] == "https://github.com/octo/widgets/pull/100"
    assert _git(work, "log", "-1", "--format=%s") == "fix: parse drops the unit (#7)"
    # The branch reached the remote, which is what makes the pull request valid.
    assert "coder/issue-7" in fx.git(origin, "branch", "--list", "coder/issue-7")
    pr = fake.pull_requests[0]
    assert pr["title"] == "Fix #7: parse drops the unit"
    assert pr["body"].startswith("Closes #7.") and "Multiplied by the unit." in pr["body"]
    assert (pr["head"], pr["base"]) == ("coder/issue-7", "main")


async def test_fix_issue_with_pr_stops_when_tests_fail(
    fake: FakeGitHub, remote_and_clone: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work = remote_and_clone
    monkeypatch.setattr(agent_module, "run_agent", _fake_run_agent(status="failed"))
    result = await fx.fix_issue("octo/widgets#7", repo_path=work, open_pr=True, api=GitHubAPI.from_env())
    assert not result.passed and not result.committed and not fake.pull_requests
    assert fx.is_dirty(work)  # the attempt is left for inspection


async def test_fix_issue_with_pr_refuses_an_unchanged_tree(
    fake: FakeGitHub, remote_and_clone: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work = remote_and_clone
    monkeypatch.setattr(agent_module, "run_agent", _fake_run_agent(edit=False))
    with pytest.raises(fx.FixIssueError, match="working tree is unchanged"):
        await fx.fix_issue("octo/widgets#7", repo_path=work, open_pr=True, api=GitHubAPI.from_env())
    assert not fake.pull_requests


async def test_fix_issue_with_pr_needs_a_token(
    fake: FakeGitHub, remote_and_clone: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work = remote_and_clone
    monkeypatch.setattr(agent_module, "run_agent", _fake_run_agent())
    with pytest.raises(fx.FixIssueError, match="GITHUB_TOKEN"):
        await fx.fix_issue("octo/widgets#7", repo_path=work, open_pr=True, api=GitHubAPI(fake.url))


# -- CLI ------------------------------------------------------------------------------------------


def test_cli_fix_issue_reports_the_pull_request(
    fake: FakeGitHub, remote_and_clone: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work = remote_and_clone
    monkeypatch.setattr(agent_module, "run_agent", _fake_run_agent())
    result = runner.invoke(
        app, ["fix-issue", "octo/widgets#7", "--repo", str(work), "--pr", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "Reading issue" in result.output and "Opening pull request" in result.output
    assert "https://github.com/octo/widgets/pull/100" in result.output


def test_cli_fix_issue_without_pr_says_where_the_changes_are(
    fake: FakeGitHub, remote_and_clone: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work = remote_and_clone
    monkeypatch.setattr(agent_module, "run_agent", _fake_run_agent())
    result = runner.invoke(app, ["fix-issue", "octo/widgets#7", "--repo", str(work), "--yes"])
    assert result.exit_code == 0, result.output
    assert "uncommitted on branch coder/issue-7" in result.output
    assert "rerun with --pr" in result.output


def test_cli_fix_issue_bad_url_and_missing_token_exit_early(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = runner.invoke(app, ["fix-issue", "not-a-url", "--yes"])
    assert result.exit_code == 2 and "Not a GitHub issue URL" in result.output
    monkeypatch.delenv("GITHUB_TOKEN")
    result = runner.invoke(app, ["fix-issue", "octo/widgets#7", "--repo", str(tmp_path), "--pr"])
    assert result.exit_code == 2 and "GITHUB_TOKEN" in result.output


def test_cli_fix_issue_help_lists_the_flags() -> None:
    result = runner.invoke(app, ["fix-issue", "--help"])
    assert result.exit_code == 0
    import re

    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    for flag in ("--repo", "--pr", "--base", "--yes", "--sandbox"):
        assert flag in plain
