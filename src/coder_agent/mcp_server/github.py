"""Second MCP server: GitHub issues and pull requests.

The file tools in `server.py` know one repository on disk. This server knows one remote: it reads
issues (the task source for `coder fix-issue`) and opens pull requests (where the fix ends up).
Keeping it a separate process with its own credentials is the point of MCP's server model: the
agent process never holds the GitHub token, the tool server does, and the same server can be
plugged into an IDE or the MCP Inspector unchanged.

Authentication is the standard `GITHUB_TOKEN` variable (the one `gh` and Actions use). Without
it, reads of public repositories still work at GitHub's anonymous rate limit; opening a pull
request needs a token with `repo` (classic) or `pull_requests: write` (fine-grained) scope.
`GITHUB_API_URL` overrides the endpoint, for GitHub Enterprise and for the tests, which point it
at a local fake.

Run standalone:  python -m coder_agent.mcp_server.github
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

DEFAULT_API_URL = "https://api.github.com"
# Issue bodies are pasted screenshots and stack traces as often as prose; this cap keeps one
# issue well under a tenth of the context budget, comments included.
MAX_BODY_CHARS = 8_000
MAX_COMMENTS = 5
MAX_COMMENT_CHARS = 1_500


class GitHubError(RuntimeError):
    """An API call failed; the message is already phrased for the model or the user."""


@dataclass(frozen=True)
class Issue:
    owner: str
    repo: str
    number: int
    title: str
    body: str
    state: str
    author: str
    labels: tuple[str, ...]
    url: str
    comments: tuple[tuple[str, str], ...] = ()  # (author, body), oldest first, capped

    @classmethod
    def from_api(cls, owner: str, repo: str, data: dict[str, Any], comments: list[dict]) -> Issue:
        return cls(
            owner=owner,
            repo=repo,
            number=int(data["number"]),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            state=str(data.get("state") or "open"),
            author=str((data.get("user") or {}).get("login") or "unknown"),
            labels=tuple(str(lb.get("name")) for lb in data.get("labels") or [] if lb.get("name")),
            url=str(data.get("html_url") or f"https://github.com/{owner}/{repo}/issues/{data['number']}"),
            comments=tuple(
                (str((c.get("user") or {}).get("login") or "unknown"), str(c.get("body") or ""))
                for c in comments[-MAX_COMMENTS:]
            ),
        )

    def render(self) -> str:
        """The issue as the model reads it: header, labels, body, then the latest comments.

        Comments are included because the reproduction steps and the maintainer's verdict on the
        right fix are usually there, not in the opening post."""
        lines = [
            f"# {self.title} (#{self.number})",
            f"Repository: {self.owner}/{self.repo} · opened by @{self.author} · state: {self.state}",
        ]
        if self.labels:
            lines.append("Labels: " + ", ".join(self.labels))
        lines += [f"URL: {self.url}", "", _clip(self.body, MAX_BODY_CHARS) or "(no description)"]
        if self.comments:
            lines += ["", f"## Latest comments ({len(self.comments)})"]
            for author, body in self.comments:
                lines += [f"### @{author}", _clip(body, MAX_COMMENT_CHARS), ""]
        return "\n".join(lines).rstrip() + "\n"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n... [truncated, {len(text) - limit} more characters]"


class GitHubAPI:
    """Thin REST client. One class so the MCP tools and the `fix-issue` command share the
    endpoint, the headers and the error wording, and so tests can point both at a fake."""

    def __init__(self, base_url: str = DEFAULT_API_URL, token: str | None = None, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.token = token or None
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> GitHubAPI:
        return cls(
            base_url=os.environ.get("GITHUB_API_URL") or DEFAULT_API_URL,
            token=os.environ.get("GITHUB_TOKEN"),
        )

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "coder-agent",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = httpx.request(method, url, headers=self._headers(), timeout=self.timeout, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub request failed: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise GitHubError(_explain(response))
        return response.json() if response.content else {}

    def get_issue(self, owner: str, repo: str, number: int) -> Issue:
        data = self._request("GET", f"/repos/{owner}/{repo}/issues/{number}")
        if "pull_request" in data:
            raise GitHubError(f"#{number} in {owner}/{repo} is a pull request, not an issue.")
        comments: list[dict] = []
        if int(data.get("comments") or 0) > 0:
            comments = self._request(
                "GET", f"/repos/{owner}/{repo}/issues/{number}/comments", params={"per_page": 100}
            )
        return Issue.from_api(owner, repo, data, comments)

    def default_branch(self, owner: str, repo: str) -> str:
        data = self._request("GET", f"/repos/{owner}/{repo}")
        return str(data.get("default_branch") or "main")

    def create_pull_request(
        self, owner: str, repo: str, *, title: str, body: str, head: str, base: str
    ) -> dict[str, Any]:
        if not self.token:
            raise GitHubError("Opening a pull request needs GITHUB_TOKEN in the environment.")
        data = self._request(
            "POST", f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        return {"number": data.get("number"), "url": data.get("html_url"), "state": data.get("state")}


def _explain(response: httpx.Response) -> str:
    """Turn GitHub's error JSON into one line that says what to do about it."""
    try:
        detail = response.json()
        message = str(detail.get("message") or "")
        errors = detail.get("errors") or []
        extra = "; ".join(str(e.get("message") or e.get("code") or "") for e in errors if isinstance(e, dict))
    except ValueError:
        message, extra = response.text[:200], ""
    hint = ""
    if response.status_code == 401:
        hint = " (GITHUB_TOKEN is missing or invalid)"
    elif response.status_code == 403 and "rate limit" in message.lower():
        hint = " (anonymous rate limit; set GITHUB_TOKEN)"
    elif response.status_code == 404:
        hint = " (private repository or wrong owner/repo/number; a token may be needed)"
    text = f"GitHub API {response.status_code}: {message}{hint}"
    return f"{text}. {extra}" if extra else text


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "github-tools",
    log_level="WARNING",
    instructions=(
        "Read GitHub issues and open pull requests. Reads work on public repositories without a "
        "token; writes need GITHUB_TOKEN."
    ),
)


def _api() -> GitHubAPI:
    # Built per call rather than at import so a token exported after the server started (or
    # patched by a test) is honoured.
    return GitHubAPI.from_env()


@mcp.tool()
def get_issue(owner: str, repo: str, number: int) -> str:
    """Read a GitHub issue: title, labels, description and the latest comments.

    Use it when the task refers to an issue and you need the exact reproduction steps, error
    text or the maintainers' decision from the comments. owner and repo are the two path
    segments of the repository URL (github.com/OWNER/REPO); number is the issue number.
    """
    try:
        return _api().get_issue(owner, repo, number).render()
    except GitHubError as exc:
        return f"ERROR: {exc}"


@mcp.tool()
def open_pull_request(owner: str, repo: str, title: str, body: str, head: str, base: str = "") -> str:
    """Open a pull request from branch `head` into `base` (the default branch when omitted).

    The head branch must already exist on GitHub; pushing is not something this tool does.
    Requires GITHUB_TOKEN. Returns the pull request URL.
    """
    api = _api()
    try:
        base = base or api.default_branch(owner, repo)
        pr = api.create_pull_request(owner, repo, title=title, body=body, head=head, base=base)
    except GitHubError as exc:
        return f"ERROR: {exc}"
    return f"OK: opened pull request #{pr['number']} ({head} -> {base}): {pr['url']}"


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
