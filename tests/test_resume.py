"""Checkpointing and `--resume`: a run that dies mid-way continues from its last finished node.

No network: the LLM is scripted, the tools are the in-process fakes, retrieval is off. The
checkpoint file is the real SQLite saver under the temp repo's `.coder-agent/`.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from coder_agent import agent as agent_mod
from coder_agent.agent import UnknownThread, checkpoint_path, resume_agent, run_agent
from coder_agent.cli import app
from coder_agent.telemetry import Ledger
from tests.fakes import echo, scripted, tool_call


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Route `run_agent` at a scripted model and in-process tools; return (repo, ledger, llm)."""
    import coder_agent.llm as llm_mod
    from coder_agent.graph import retrieval
    from coder_agent.tools import client

    llm = scripted("1. call echo\n2. answer", tool_call("echo", text="ping"), "Done.")

    async def fake_load_tools(repo):
        return [echo]

    monkeypatch.setattr(llm_mod, "get_llm", lambda: llm)
    monkeypatch.setattr(client, "load_tools", fake_load_tools)
    monkeypatch.setattr(retrieval, "default_retriever", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo, tmp_path / "ledger.sqlite", llm


async def test_crash_then_resume_continues_from_the_failed_node(wired):
    repo, ledger, llm = wired
    llm.fail_on_call = {2}  # plan succeeds, the first act call dies

    with pytest.raises(RuntimeError, match="call 2"):
        await run_agent(repo, "do it", thread_id="t1", ledger_path=ledger)
    assert checkpoint_path(repo).exists()
    crashed = Ledger(ledger).recent()  # the crashed attempt is recorded as an error, with why
    assert [r["status"] for r in crashed] == ["error"]
    assert json.loads(crashed[0]["tags_json"]) == {"thread_id": "t1", "error": "RuntimeError"}

    out = await resume_agent(repo, "t1", ledger_path=ledger)

    assert out.ran and out.status == "passed"
    assert len(llm.calls) == 4  # plan, act (failed), act (retried), act (final answer)
    # The retried act call was prompted with the plan from the checkpoint, not a new one.
    assert any("1. call echo" in str(m.content) for m in llm.calls[2])
    assert out.final["steps"] == 2  # the failed act call was never committed to state
    rows = Ledger(ledger).recent()
    assert [r["status"] for r in rows] == ["passed", "error"]
    assert json.loads(rows[0]["tags_json"]) == {"thread_id": "t1", "resumed": True}
    assert rows[0]["task"] == "do it"  # inputs come from the checkpoint too


async def test_resume_of_finished_thread_runs_nothing(wired):
    repo, ledger, llm = wired
    first = await run_agent(repo, "do it", thread_id="t2", ledger_path=ledger)
    assert first.status == "passed" and first.thread_id == "t2"
    calls = len(llm.calls)

    again = await resume_agent(repo, "t2", ledger_path=ledger)

    assert not again.ran and again.status == "passed"
    assert len(llm.calls) == calls
    assert len(Ledger(ledger).recent()) == 1  # no second record either


async def test_fresh_run_gets_a_thread_id_and_tags_it(wired):
    repo, ledger, _ = wired
    out = await run_agent(repo, "do it", ledger_path=ledger)
    assert len(out.thread_id) == 8
    assert json.loads(Ledger(ledger).recent()[0]["tags_json"]) == {"thread_id": out.thread_id}


async def test_resume_unknown_thread_raises(wired):
    repo, ledger, _ = wired
    with pytest.raises(UnknownThread):
        await resume_agent(repo, "nope", ledger_path=ledger)


def test_cli_resume_unknown_thread_exits_2(tmp_path):
    result = CliRunner().invoke(app, ["run", str(tmp_path), "--resume", "nope"])
    assert result.exit_code == 2
    assert "No checkpoint for thread" in result.output


def test_cli_needs_a_task_or_a_thread(tmp_path):
    result = CliRunner().invoke(app, ["run", str(tmp_path)])
    assert result.exit_code == 2
    assert "--resume" in result.output


def test_module_exposes_the_checkpoint_location(tmp_path):
    assert checkpoint_path(tmp_path) == tmp_path / ".coder-agent" / "checkpoints.sqlite"
    assert agent_mod.new_thread_id() != agent_mod.new_thread_id()
