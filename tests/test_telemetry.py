"""Usage accounting in state and the SQLite ledger."""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage
from typer.testing import CliRunner

from coder_agent.cli import app
from coder_agent.config import settings
from coder_agent.graph import build_graph
from coder_agent.telemetry import Ledger, RunRecord, merge_usage, usage_from_message
from tests.fakes import echo, scripted, tool_call


def ai(text: str, inp: int, out: int, model: str = "m1", **kw) -> AIMessage:
    return AIMessage(
        text,
        usage_metadata={"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out},
        response_metadata={"model_name": model},
        **kw,
    )


def test_usage_from_message_reads_tokens_and_model():
    u = usage_from_message("act", ai("x", 100, 20, model="groq-a"))
    assert u == {"act": {"calls": 1, "input_tokens": 100, "output_tokens": 20, "models": {"groq-a": 1}}}


def test_usage_from_message_without_metadata_is_zero():
    u = usage_from_message("plan", AIMessage("bare"))
    assert u["plan"]["calls"] == 1 and u["plan"]["input_tokens"] == 0


def test_merge_usage_sums_per_node_and_counts_models():
    a = usage_from_message("act", ai("x", 10, 1, model="A"))
    b = usage_from_message("act", ai("y", 20, 2, model="B"))
    c = usage_from_message("plan", ai("z", 5, 5, model="A"))
    merged = merge_usage(merge_usage(a, b), c)
    assert merged["act"] == {"calls": 2, "input_tokens": 30, "output_tokens": 3, "models": {"A": 1, "B": 1}}
    assert merged["plan"]["calls"] == 1
    # inputs are not mutated
    assert a["act"]["calls"] == 1


def test_graph_accumulates_usage_across_nodes():
    llm = scripted(
        ai("plan", 50, 10),
        ai("", 200, 5, tool_calls=tool_call("echo", text="hi").tool_calls),
        ai("done", 300, 8, model="fallback"),
    )
    out = build_graph(llm, [echo]).invoke({"task": "t", "repo": "/none", "test_command": None})
    usage = out["usage"]
    assert usage["plan"] == {"calls": 1, "input_tokens": 50, "output_tokens": 10, "models": {"m1": 1}}
    assert usage["act"]["calls"] == 2
    assert usage["act"]["input_tokens"] == 500
    assert usage["act"]["models"] == {"m1": 1, "fallback": 1}


def test_ledger_records_and_summarises(tmp_path: Path):
    ledger = Ledger(tmp_path / "l.sqlite")
    state = {
        "repo": "/r", "task": "fix it", "status": "passed", "iteration": 2, "steps": 5,
        "tests_passed": True, "test_command": "pytest",
        "usage": {"plan": {"calls": 2, "input_tokens": 100, "output_tokens": 20, "models": {"A": 2}},
                  "act": {"calls": 3, "input_tokens": 900, "output_tokens": 80, "models": {"A": 3}}},
    }
    rec = RunRecord.from_state(state, model="A", wall_seconds=12.5, tags={"suite": "unit"})
    ledger.record(rec)
    ledger.record(RunRecord.from_state({**state, "status": "failed", "tests_passed": False},
                                       model="A", wall_seconds=30))

    rows = ledger.recent()
    assert len(rows) == 2
    assert {r["status"] for r in rows} == {"passed", "failed"}
    assert rows[0]["input_tokens"] == 1000 and rows[0]["output_tokens"] == 100

    s = ledger.summary()
    assert s["runs"] == 2 and s["pass_rate"] == 0.5
    assert s["avg_tokens"] == 1100
    per_node = {r["node"]: r for r in s["per_node"]}
    assert per_node["act"]["calls"] == 6 and per_node["plan"]["input_tokens"] == 200

    assert rec.total_tokens == 1100
    assert "5 model calls" in rec.footer() and "2 iteration(s)" in rec.footer()


def test_ledger_is_idempotent_per_run_id(tmp_path: Path):
    ledger = Ledger(tmp_path / "l.sqlite")
    rec = RunRecord.from_state({"status": "passed"}, model="A", wall_seconds=1)
    ledger.record(rec)
    ledger.record(rec)
    assert ledger.summary()["runs"] == 1


def test_stats_command_reads_ledger(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "ledger_path", tmp_path / "l.sqlite")
    runner = CliRunner()
    assert "No runs recorded" in runner.invoke(app, ["stats"]).output

    Ledger(settings.ledger_path).record(
        RunRecord.from_state(
            {"status": "passed", "iteration": 1, "task": "add is_even",
             "usage": {"act": {"calls": 1, "input_tokens": 10, "output_tokens": 2, "models": {}}}},
            model="A", wall_seconds=3,
        )
    )
    out = runner.invoke(app, ["stats"]).output
    assert "1 runs" in out and "pass rate 100%" in out
    assert "add is_even" in out and "act" in out
