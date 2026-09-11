"""Multi-turn sessions: a second task on the same thread sees the first one's conversation.

`run_agent` with an existing, finished thread is a follow-up turn; `coder chat` is a loop around
that. The graph runs with a scripted model and in-process tools; the CLI loop is tested with the
agent functions replaced by fakes, because the loop's job is dispatch, not planning.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from typer.testing import CliRunner

from coder_agent import agent as agent_mod
from coder_agent.agent import ThreadInProgress, run_agent
from coder_agent.cli import app
from coder_agent.telemetry import Ledger
from tests.fakes import echo, scripted


@pytest.fixture
def wired(monkeypatch, tmp_path):
    import coder_agent.llm as llm_mod
    from coder_agent.graph import retrieval
    from coder_agent.tools import client

    llm = scripted("plan one", "done one", "plan two", "done two")

    async def fake_load_tools(repo, github=False):
        return [echo]

    monkeypatch.setattr(llm_mod, "get_llm", lambda: llm)
    monkeypatch.setattr(client, "load_tools", fake_load_tools)
    monkeypatch.setattr(retrieval, "default_retriever", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo, tmp_path / "ledger.sqlite", llm


async def test_second_task_on_a_finished_thread_keeps_the_history(wired):
    repo, ledger, llm = wired

    first = await run_agent(repo, "first task", thread_id="c1", ledger_path=ledger)
    second = await run_agent(repo, "second task", thread_id="c1", ledger_path=ledger)

    assert first.status == "passed" and second.status == "passed"
    assert second.final["iteration"] == 1 and second.final["steps"] == 1  # counters restarted
    # The second plan was asked about the second task, and the second act saw the whole story.
    assert "second task" in str(llm.calls[2][-1].content)
    history = [(type(m), str(m.content)) for m in llm.calls[3][1:]]
    assert history == [
        (HumanMessage, "first task"), (AIMessage, "done one"), (HumanMessage, "second task"),
    ]
    rows = Ledger(ledger).recent()
    assert [json.loads(r["tags_json"]) for r in rows] == [
        {"thread_id": "c1", "turn": 2}, {"thread_id": "c1"},
    ]
    assert rows[0]["task"] == "second task"


async def test_new_task_on_an_unfinished_thread_is_refused(wired):
    repo, ledger, llm = wired
    llm.fail_on_call = {2}
    with pytest.raises(RuntimeError, match="call 2"):
        await run_agent(repo, "first task", thread_id="c2", ledger_path=ledger)

    with pytest.raises(ThreadInProgress):
        await run_agent(repo, "second task", thread_id="c2", ledger_path=ledger)


def test_chat_loop_dispatches_messages_and_quits(monkeypatch, tmp_path):
    seen: list[tuple[str, str]] = []

    async def fake_run(repo, task, *, thread_id=None, **kwargs):
        seen.append((task, thread_id))
        record = SimpleNamespace(footer=lambda: "0 model calls")
        return SimpleNamespace(ran=True, status="passed", record=record, thread_id=thread_id)

    async def fake_resume(repo, thread_id, **kwargs):
        seen.append(("/resume", thread_id))
        return SimpleNamespace(ran=False, status="passed", record=None, thread_id=thread_id)

    monkeypatch.setattr(agent_mod, "run_agent", fake_run)
    monkeypatch.setattr(agent_mod, "resume_agent", fake_resume)

    result = CliRunner().invoke(
        app, ["chat", str(tmp_path), "--thread", "abc", "--yes"],
        input="add a greet function\n\n/resume\n/quit\n",
    )

    assert result.exit_code == 0, result.output
    assert seen == [("add a greet function", "abc"), ("/resume", "abc")]
    assert "thread abc" in result.output and "0 model calls" in result.output


def test_chat_loop_survives_a_failed_turn(monkeypatch, tmp_path):
    async def failing_run(repo, task, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(agent_mod, "run_agent", failing_run)
    result = CliRunner().invoke(app, ["chat", str(tmp_path), "-y"], input="do it\n/quit\n")

    assert result.exit_code == 0
    assert "Turn stopped" in result.output and "provider down" in result.output
    assert "/resume" in result.output


def test_chat_rejects_missing_repo(tmp_path):
    result = CliRunner().invoke(app, ["chat", str(tmp_path / "nope")])
    assert result.exit_code == 2
