"""`coder fix-issue <url>`: from a GitHub issue to a pull request.

The pipeline is deliberate and mostly not the model's to decide:

    parse URL -> fetch issue -> clone or fetch the repo -> branch -> run the agent
             -> (tests pass) -> commit -> push -> open pull request

The model gets one job in that chain, the fourth arrow, and it runs exactly as `coder run`
does: same graph, same tools, same approval gate, plus the read-only GitHub tool so it can
re-read the issue. Everything around it is plain git run by this process on the host. The
sandbox denylist blocks `git push` for the model; nothing blocks it for the user, and
`fix-issue --pr` is the user asking for the push. Splitting the work this way keeps the
outward-facing steps (a branch on someone's repository, a pull request under the user's name)
predictable: they happen when asked, in a fixed order, or not at all.

There is one shortcut through the chain. A passed run without `--pr` leaves its edits
uncommitted on the fix branch and says "review them, then rerun with --pr". That rerun finds
the branch checked out and dirty, and takes it at its word: it runs the tests on what is there
and, if they pass, commits, pushes and opens the pull request without running the agent again.
Re-running the agent on an already-fixed tree costs minutes and tens of thousands of tokens to
learn that there is nothing to do, and its summary ("no changes were required") would become
the pull request's description.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from coder_agent import agent as _agent
from coder_agent.config import settings
from coder_agent.mcp_server.github import GitHubAPI, Issue

_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?"
    r"/issues/(?P<number>\d+)/?(?:[#?].*)?$"
)
_SHORT = re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)#(?P<number>\d+)$")


class FixIssueError(RuntimeError):
    """Something outside the agent run went wrong (bad URL, dirty tree, git failure)."""


@dataclass(frozen=True)
class IssueRef:
    owner: str
    repo: str
    number: int

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/issues/{self.number}"

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}.git"

    @property
    def branch(self) -> str:
        return f"coder/issue-{self.number}"

    @property
    def slug(self) -> str:
        return f"{self.owner}__{self.repo}"


def parse_issue_url(text: str) -> IssueRef:
    """Accept `https://github.com/o/r/issues/12` (with or without scheme, `.git`, a trailing
    slash or a `#issuecomment` fragment) and the short `o/r#12` form."""
    text = text.strip()
    match = _URL.match(text) or _SHORT.match(text)
    if not match:
        raise FixIssueError(
            f"Not a GitHub issue URL: {text!r}. Expected https://github.com/OWNER/REPO/issues/N."
        )
    return IssueRef(match["owner"], match["repo"], int(match["number"]))


def issue_task(issue: Issue) -> str:
    """The task text the graph receives. The issue is quoted whole so the planner sees the
    reproduction steps; the framing tells the agent what "done" means for an issue."""
    return (
        "Fix the GitHub issue below. Reproduce it with a test where practical, make the smallest "
        "change that resolves it, and keep the existing tests passing.\n\n" + issue.render()
    )


def commit_message(issue: Issue) -> str:
    return f"fix: {issue.title.strip()} (#{issue.number})"


def pull_request_body(issue: Issue, summary: str) -> str:
    """`Closes #N` is the GitHub keyword that links and auto-closes the issue on merge."""
    summary = summary.strip() or "See the diff."
    return f"Closes #{issue.number}.\n\n{summary}\n"


# ---------------------------------------------------------------------------
# Host git
# ---------------------------------------------------------------------------


def git(repo: Path | None, *args: str, check: bool = True) -> str:
    """Run git on the host with output captured. Errors surface as `FixIssueError` with git's
    own message, which is usually the most useful thing to show."""
    cmd = ["git", *(["-C", str(repo)] if repo else []), *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )
    except FileNotFoundError as exc:
        raise FixIssueError("git is not installed or not on PATH.") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise FixIssueError(f"`{' '.join(cmd)}` failed: {detail}")
    return result.stdout.strip()


def is_git_repo(path: Path) -> bool:
    return path.is_dir() and git(path, "rev-parse", "--is-inside-work-tree", check=False) == "true"


def is_dirty(repo: Path) -> bool:
    return bool(git(repo, "status", "--porcelain"))


def current_branch(repo: Path) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def ensure_checkout(ref: IssueRef, path: Path | None) -> Path:
    """Return a local working tree for the issue's repository.

    With `path` the user names an existing clone and it is used as-is. Without it the repository
    is cloned under `settings.checkouts_dir`, or fetched when a clone from an earlier run is
    already there, so repeated `fix-issue` calls on one project do not download it repeatedly.
    """
    if path is not None:
        path = path.resolve()
        if not is_git_repo(path):
            raise FixIssueError(f"{path} is not a git working tree.")
        return path
    target = settings.checkouts_dir / ref.slug
    if is_git_repo(target):
        git(target, "fetch", "--quiet", "origin")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    git(None, "clone", "--quiet", ref.clone_url, str(target))
    return target


def start_branch(repo: Path, branch: str) -> str:
    """Check out `branch`, creating it from HEAD when new. Refuses a dirty tree on another
    branch: the agent's edits should be the only thing on the fix branch."""
    if current_branch(repo) == branch:
        return branch
    if is_dirty(repo):
        raise FixIssueError(
            f"{repo} has uncommitted changes; commit or stash them before starting a fix branch."
        )
    exists = git(repo, "branch", "--list", branch)
    git(repo, "checkout", "--quiet", *([] if exists else ["-b"]), branch)
    return branch


def commit_all(repo: Path, message: str) -> bool:
    """Stage everything and commit; False when the tree had nothing to commit."""
    if not is_dirty(repo):
        return False
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "--message", message)
    return True


def push_branch(repo: Path, branch: str) -> None:
    git(repo, "push", "--quiet", "--set-upstream", "origin", branch)


def has_reviewed_changes(repo: Path, branch: str) -> bool:
    """True when `repo` is already on the fix branch with uncommitted edits: the state a passed
    run without `--pr` leaves behind, and the only state in which the agent is not run again."""
    return current_branch(repo) == branch and is_dirty(repo)


def verify_reviewed_changes(repo: Path, task: str) -> _agent.RunOutcome:
    """Run the repository's tests on edits left by an earlier run, and report the verdict in the
    shape `run_agent` uses so the rest of the pipeline and the CLI need no second code path.

    The test command is detected the same deterministic way the graph detects it, and runs in
    the same sandbox. The summary is `git diff --stat`, which is what the pull request body gets
    instead of a model's account of the change.
    """
    from coder_agent.graph.testing import detect_test_command, run_tests

    command = detect_test_command(repo)
    if command is None:
        raise FixIssueError(
            f"{repo} is on the fix branch with uncommitted edits, but no test command was found "
            "to verify them. Commit and push by hand, or run without --pr first."
        )
    started = time.monotonic()
    result = run_tests(repo, command)
    passed = result.exit_code == 0 and not result.timed_out
    stat = git(repo, "diff", "--stat")
    final: dict[str, object] = {
        "repo": str(repo),
        "task": task,
        "status": "passed" if passed else "failed",
        "tests_passed": passed,
        "test_command": command,
        "test_output": result.output,
        "summary": f"Reviewed locally; `{command}` passes.\n\n```\n{stat}\n```",
    }
    record = _agent.RunRecord.from_state(
        final, model="no model, reviewed edits", wall_seconds=time.monotonic() - started
    )
    return _agent.RunOutcome(final=final, record=record, thread_id="", ran=False)


# ---------------------------------------------------------------------------
# The whole pipeline
# ---------------------------------------------------------------------------


@dataclass
class FixResult:
    ref: IssueRef
    issue: Issue
    repo: Path
    branch: str
    outcome: _agent.RunOutcome
    committed: bool = False
    pushed: bool = False
    pull_request: dict | None = None

    @property
    def passed(self) -> bool:
        return self.outcome.status == "passed"


async def fix_issue(
    url: str,
    *,
    repo_path: Path | None = None,
    open_pr: bool = False,
    base: str | None = None,
    api: GitHubAPI | None = None,
    on_update: _agent.UpdateHook | None = None,
    approve: _agent.ApproveHook | None = None,
    on_stage: Callable[[str, str], None] | None = None,
    ledger_path: Path | None = None,
) -> FixResult:
    """Run the pipeline. `on_stage(name, detail)` is called before each step so a terminal can
    say what is happening while a clone or the agent runs. The agent step raises like
    `run_agent` does; the caller decides how to report it."""
    api = api or GitHubAPI.from_env()
    say = on_stage or (lambda name, detail: None)
    ref = parse_issue_url(url)

    say("issue", ref.url)
    issue = api.get_issue(ref.owner, ref.repo, ref.number)

    say("checkout", str(repo_path or settings.checkouts_dir / ref.slug))
    repo = ensure_checkout(ref, repo_path)
    if open_pr and has_reviewed_changes(repo, ref.branch):
        branch = ref.branch
        say("verify", branch)
        outcome = verify_reviewed_changes(repo, issue_task(issue))
    else:
        branch = start_branch(repo, ref.branch)
        say("agent", branch)
        outcome = await _agent.run_agent(
            repo, issue_task(issue), on_update=on_update, approve=approve, github=True,
            tags={"source": "github", "issue": ref.url}, ledger_path=ledger_path,
        )
    result = FixResult(ref=ref, issue=issue, repo=repo, branch=branch, outcome=outcome)
    if not result.passed or not open_pr:
        return result

    if not api.authenticated:
        raise FixIssueError("Opening a pull request needs GITHUB_TOKEN in the environment.")
    say("commit", commit_message(issue))
    result.committed = commit_all(repo, commit_message(issue))
    if not result.committed:
        raise FixIssueError(
            "The agent reported success but the working tree is unchanged; nothing to open a "
            f"pull request from. If an earlier run already committed on {branch}, push it by hand."
        )
    say("push", f"origin/{branch}")
    push_branch(repo, branch)
    result.pushed = True
    say("pull-request", f"{branch} -> {base or 'default branch'}")
    result.pull_request = api.create_pull_request(
        ref.owner, ref.repo,
        title=f"Fix #{ref.number}: {issue.title.strip()}",
        body=pull_request_body(issue, str(outcome.final.get("summary", ""))),
        head=branch,
        base=base or api.default_branch(ref.owner, ref.repo),
    )
    return result
