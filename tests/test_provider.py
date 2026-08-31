"""Retry, fallback and provider metrics, driven with scripted models (no network).

`get_llm` is exercised with `init_chat_model` replaced by a lookup into fake models and
`TRANSIENT_ERRORS` set to the fake's `RuntimeError`, so the same code path a 429 or a Groq
`tool_use_failed` takes in production runs here in milliseconds.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableWithFallbacks
from typer.testing import CliRunner

from coder_agent import llm as llm_mod
from coder_agent.agent import run_agent
from coder_agent.cli import app
from coder_agent.config import settings
from coder_agent.llm import ChatModelRetry, get_llm, model_kwargs, provider_of
from coder_agent.telemetry import Ledger, RunRecord
from coder_agent.telemetry.events import ProviderEvents
from coder_agent.telemetry.ledger import fallback_calls
from tests.fakes import echo, scripted


@pytest.fixture
def providers(monkeypatch):
    """Fake primary and fallback behind `get_llm`; returns them so tests can script failures."""
    primary = scripted("from primary", "from primary again")
    fallback = scripted("from fallback")
    models = {"groq:primary": primary, "google_genai:fallback": fallback}

    monkeypatch.setattr(llm_mod, "init_chat_model", lambda name, **kw: models[name])
    monkeypatch.setattr(llm_mod, "TRANSIENT_ERRORS", (RuntimeError,))
    monkeypatch.setattr(settings, "model", "groq:primary")
    monkeypatch.setattr(settings, "fallback_model", "google_genai:fallback")
    monkeypatch.setattr(settings, "llm_attempts", 2)
    monkeypatch.setattr(settings, "llm_retry_initial_seconds", 0.0)
    return primary, fallback


def test_transient_error_is_retried_on_the_primary(providers):
    primary, fallback = providers
    primary.fail_on_call = {1}
    events = ProviderEvents()

    out = get_llm().invoke([HumanMessage("hi")], config={"callbacks": [events]})

    assert out.content == "from primary"
    assert len(primary.calls) == 2 and fallback.calls == []
    assert events.errors == {"RuntimeError": 1} and events.total == 1


def test_exhausted_primary_falls_back(providers):
    primary, fallback = providers
    primary.fail_on_call = {1, 2, 3}
    events = ProviderEvents()

    out = get_llm().invoke([HumanMessage("hi")], config={"callbacks": [events]})

    assert out.content == "from fallback"
    assert len(primary.calls) == 2  # llm_attempts, then the fallback
    assert len(fallback.calls) == 1
    assert events.errors == {"RuntimeError": 2}


def test_bind_tools_survives_retry_and_fallback_wrapping(providers):
    llm = get_llm()
    assert isinstance(llm, RunnableWithFallbacks)
    assert isinstance(llm.runnable, ChatModelRetry)

    bound = llm.bind_tools([echo])

    assert isinstance(bound, RunnableWithFallbacks)
    assert isinstance(bound.runnable, ChatModelRetry) and len(bound.fallbacks) == 1
    assert bound.invoke([HumanMessage("hi")]).content == "from primary"


def test_without_fallback_the_retry_wrapper_is_returned(providers, monkeypatch):
    monkeypatch.setattr(settings, "fallback_model", None)
    assert isinstance(get_llm(), ChatModelRetry)
    monkeypatch.setattr(settings, "llm_attempts", 1)
    assert get_llm() is providers[0]  # one attempt means no wrapper at all


# --- metrics ------------------------------------------------------------------------------


def test_fallback_calls_counts_models_other_than_the_configured_one():
    usage = {
        "plan": {"calls": 1, "models": {"openai/gpt-oss-120b": 1}},
        "act": {"calls": 3, "models": {"openai/gpt-oss-120b": 1, "gemini-2.5-flash": 2}},
    }
    assert fallback_calls(usage, "groq:openai/gpt-oss-120b") == 2
    assert fallback_calls(usage, "google_genai:gemini-2.5-flash") == 2
    assert fallback_calls({}, "groq:x") == 0


def test_record_footer_mentions_failures_and_fallback():
    rec = RunRecord.from_state(
        {"usage": {"act": {"calls": 2, "input_tokens": 10, "output_tokens": 5,
                           "models": {"gemini-2.5-flash": 2}}}},
        model="groq:openai/gpt-oss-120b", wall_seconds=1.0,
        provider_errors={"RateLimitError": 2},
    )
    assert rec.fallback_calls == 2
    assert "2 RateLimitError" in rec.footer() and "2 answered by the fallback" in rec.footer()


def test_ledger_migrates_an_old_file_and_summarises_provider_health(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with sqlite3.connect(path) as c:  # the schema as it was before provider_errors_json existed
        c.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at REAL NOT NULL, repo TEXT,"
            " task TEXT, model TEXT, status TEXT, iterations INTEGER, steps INTEGER,"
            " wall_seconds REAL, tests_passed INTEGER, test_command TEXT, input_tokens INTEGER,"
            " output_tokens INTEGER, usage_json TEXT, tags_json TEXT)"
        )
        c.execute(
            "INSERT INTO runs (run_id, started_at, model, status, usage_json, tags_json)"
            " VALUES ('old', 1.0, 'groq:m', 'passed', '{}', '{}')"
        )

    ledger = Ledger(path)
    ledger.record(RunRecord.from_state(
        {"status": "passed", "usage": {"act": {"calls": 1, "models": {"other": 1}}}},
        model="groq:m", wall_seconds=1.0, provider_errors={"RateLimitError": 3},
    ))

    rows = ledger.recent()
    assert {r["run_id"] for r in rows} == {"old", rows[0]["run_id"]}
    assert json.loads(rows[0]["provider_errors_json"]) == {"RateLimitError": 3}
    assert rows[1]["provider_errors_json"] is None  # the old row survived the migration
    summary = ledger.summary()
    assert summary["fallback_runs"] == 1 and summary["provider_errors"] == {"RateLimitError": 3}


def test_stats_shows_provider_line(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite"
    Ledger(path).record(RunRecord.from_state(
        {"status": "passed", "usage": {"act": {"calls": 1, "models": {"fb": 1}}}},
        model="groq:m", wall_seconds=1.0, provider_errors={"BadRequestError": 1},
    ))
    monkeypatch.setattr(settings, "ledger_path", path)

    result = CliRunner().invoke(app, ["stats"])

    assert result.exit_code == 0
    assert "fallback answered in 1 run(s)" in result.output
    assert "1 BadRequestError" in result.output


# --- through a run ------------------------------------------------------------------------


async def test_run_records_provider_errors_in_the_ledger(monkeypatch, tmp_path):
    from coder_agent.graph import retrieval
    from coder_agent.tools import client

    primary = scripted("plan", AIMessage("done"))
    primary.fail_on_call = {2}  # the act call fails once and is retried on the same model
    monkeypatch.setattr(llm_mod, "TRANSIENT_ERRORS", (RuntimeError,))
    monkeypatch.setattr(
        llm_mod, "get_llm",
        lambda: ChatModelRetry(bound=primary, retry_exception_types=(RuntimeError,),
                               max_attempt_number=2, wait_exponential_jitter=False),
    )

    async def fake_load_tools(repo, github=False):
        return [echo]

    monkeypatch.setattr(client, "load_tools", fake_load_tools)
    monkeypatch.setattr(retrieval, "default_retriever", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()

    out = await run_agent(repo, "do it", ledger_path=tmp_path / "ledger.sqlite")

    assert out.status == "passed"
    assert out.record.provider_errors == {"RuntimeError": 1}
    row = Ledger(tmp_path / "ledger.sqlite").recent()[0]
    assert json.loads(row["provider_errors_json"]) == {"RuntimeError": 1}


def test_ollama_gets_context_size_and_address_instead_of_sdk_retries(monkeypatch):
    monkeypatch.setattr(settings, "ollama_num_ctx", 4096)
    monkeypatch.setattr(settings, "ollama_base_url", "http://box:11434")
    assert provider_of("ollama:qwen2.5-coder:7b") == "ollama"
    assert model_kwargs("ollama:qwen2.5-coder:7b") == {"base_url": "http://box:11434", "num_ctx": 4096}
    assert model_kwargs("groq:openai/gpt-oss-120b") == {"max_retries": settings.llm_sdk_retries}


def test_ollama_model_builds_without_a_daemon_and_keeps_its_tag(monkeypatch):
    """Constructing the client is offline; only `invoke` talks to the daemon. The retry wrapper
    must still expose the model's own fields, since the graph reads them for the ledger."""
    pytest.importorskip("langchain_ollama")
    monkeypatch.setattr(settings, "model", "ollama:qwen2.5-coder:7b")
    monkeypatch.setattr(settings, "fallback_model", None)
    llm = get_llm()
    assert isinstance(llm, ChatModelRetry)
    assert llm.bound.model == "qwen2.5-coder:7b"  # the tag's second colon survives the split
    assert llm.bound.num_ctx == settings.ollama_num_ctx
    import ollama

    assert ollama.ResponseError in llm_mod.TRANSIENT_ERRORS
