"""The GitHub tool server: REST client, issue rendering, and the tools over real stdio.

The fake GitHub from `fake_github.py` stands in for api.github.com. The MCP server is spawned
as a subprocess by the same client the agent uses, so what is tested is the tool list the model
would see and the text it would read, including the policy that hides the write tool.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coder_agent.mcp_server import github as gh
from coder_agent.mcp_server.github import GitHubAPI, GitHubError
from coder_agent.tools.client import load_tools
from tests.fake_github import FakeGitHub
from tests.test_mcp_tools import text


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch):
    server = FakeGitHub().start()
    server.add_issue(
        "octo", "widgets", 7, "parse_duration drops the unit",
        body="`parse_duration('5m')` returns 5 instead of 300.\n\nSteps:\n1. call it\n2. compare",
        labels=("bug", "good first issue"),
        comments=(("maintainer", "Confirmed; the fix belongs in durations.py."),),
    )
    monkeypatch.setenv("GITHUB_API_URL", server.url)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    yield server
    server.stop()


# -- REST client ------------------------------------------------------------------------------


def test_get_issue_renders_header_body_labels_and_comments(fake: FakeGitHub) -> None:
    issue = GitHubAPI(fake.url).get_issue("octo", "widgets", 7)
    rendered = issue.render()
    assert rendered.startswith("# parse_duration drops the unit (#7)")
    assert "Labels: bug, good first issue" in rendered
    assert "returns 5 instead of 300" in rendered
    assert "### @maintainer" in rendered and "durations.py" in rendered
    assert issue.author == "reporter" and issue.state == "open"


def test_missing_issue_is_a_clear_error(fake: FakeGitHub) -> None:
    with pytest.raises(GitHubError, match="404.*wrong owner/repo/number"):
        GitHubAPI(fake.url).get_issue("octo", "widgets", 99)


def test_pull_request_of_a_pull_request_number_is_refused(fake: FakeGitHub) -> None:
    fake.issues[("octo", "widgets", 8)] = {
        "number": 8, "title": "a PR", "pull_request": {"url": "x"}, "user": {"login": "u"},
    }
    with pytest.raises(GitHubError, match="pull request, not an issue"):
        GitHubAPI(fake.url).get_issue("octo", "widgets", 8)


def test_create_pull_request_sends_bearer_token_and_returns_url(fake: FakeGitHub) -> None:
    fake.require_token = "ghp_test"
    api = GitHubAPI(fake.url, token="ghp_test")
    pr = api.create_pull_request(
        "octo", "widgets", title="Fix #7", body="Closes #7.", head="coder/issue-7", base="main"
    )
    assert pr["url"] == "https://github.com/octo/widgets/pull/100"
    assert fake.pull_requests[0]["head"] == "coder/issue-7"
    method, path, headers = fake.requests[-1]
    assert (method, path) == ("POST", "/repos/octo/widgets/pulls")
    assert headers["Authorization"] == "Bearer ghp_test"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_create_pull_request_without_token_fails_before_any_request(fake: FakeGitHub) -> None:
    with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
        GitHubAPI(fake.url).create_pull_request(
            "octo", "widgets", title="t", body="b", head="h", base="main"
        )
    assert not any(m == "POST" for m, _, _ in fake.requests)


def test_bad_credentials_and_validation_errors_are_explained(fake: FakeGitHub) -> None:
    fake.require_token = "right"
    with pytest.raises(GitHubError, match="401.*GITHUB_TOKEN is missing or invalid"):
        GitHubAPI(fake.url, token="wrong").create_pull_request(
            "octo", "widgets", title="t", body="b", head="h", base="main"
        )
    with pytest.raises(GitHubError, match="422.*missing_field"):
        GitHubAPI(fake.url, token="right").create_pull_request(
            "octo", "widgets", title="", body="b", head="h", base="main"
        )


def test_from_env_reads_standard_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_API_URL", "http://ghe.local/api/v3/")
    monkeypatch.setenv("GITHUB_TOKEN", "abc")
    api = GitHubAPI.from_env()
    assert api.base_url == "http://ghe.local/api/v3" and api.authenticated
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.delenv("GITHUB_API_URL")
    api = GitHubAPI.from_env()
    assert api.base_url == gh.DEFAULT_API_URL and not api.authenticated


def test_render_clips_long_bodies_and_keeps_latest_comments() -> None:
    issue = gh.Issue(
        owner="o", repo="r", number=1, title="t", body="x" * 10_000, state="open", author="a",
        labels=(), url="u", comments=tuple((f"u{i}", f"c{i}") for i in range(3)),
    )
    rendered = issue.render()
    assert "[truncated, 2000 more characters]" in rendered
    assert "## Latest comments (3)" in rendered
    data = {"number": 1, "title": "t", "body": None, "user": None, "labels": []}
    comments = [{"user": {"login": f"u{i}"}, "body": f"c{i}"} for i in range(9)]
    issue = gh.Issue.from_api("o", "r", data, comments)
    assert [who for who, _ in issue.comments] == ["u4", "u5", "u6", "u7", "u8"]
    assert "(no description)" in issue.render() and issue.author == "unknown"


# -- The tools, in-process and over stdio -------------------------------------------------------


def test_tool_functions_return_model_facing_errors(fake: FakeGitHub) -> None:
    assert gh.get_issue("octo", "widgets", 404).startswith("ERROR: GitHub API 404")
    assert gh.open_pull_request("octo", "widgets", "t", "b", "h") == (
        "ERROR: Opening a pull request needs GITHUB_TOKEN in the environment."
    )


def test_open_pull_request_tool_defaults_to_the_default_branch(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    fake.default_branches[("octo", "widgets")] = "develop"
    out = gh.open_pull_request("octo", "widgets", "Fix #7", "Closes #7.", "coder/issue-7")
    assert out.startswith("OK: opened pull request #100 (coder/issue-7 -> develop)")
    assert fake.pull_requests[0]["base"] == "develop"


@pytest.mark.asyncio
async def test_agent_sees_both_servers_but_not_the_write_tool(fake: FakeGitHub, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    tools = {t.name: t for t in await load_tools(tmp_path, github=True)}
    assert {"read_file", "run_command", "get_issue"} <= set(tools)
    assert "open_pull_request" not in tools
    assert "reproduction steps" in tools["get_issue"].description

    out = text(await tools["get_issue"].ainvoke({"owner": "octo", "repo": "widgets", "number": 7}))
    assert out.startswith("# parse_duration drops the unit (#7)")
    # The subprocess found the fake through the inherited environment, not a real network call.
    assert any(path == "/repos/octo/widgets/issues/7" for _, path, _ in fake.requests)


@pytest.mark.asyncio
async def test_plain_runs_do_not_start_the_github_server(tmp_path: Path) -> None:
    tools = {t.name for t in await load_tools(tmp_path)}
    assert "get_issue" not in tools
