"""Retry, fallback and provider metrics, driven with scripted models (no network).

`get_llm` is exercised with `init_chat_model` replaced by a lookup into fake models and
`TRANSIENT_ERRORS` set to the fake's `RuntimeError`, so the same code path a 429 or a Groq
`tool_use_failed` takes in production runs here in milliseconds.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableWithFallbacks
from typer.testing import CliRunner

from coder_agent import llm as llm_mod
from coder_agent.agent import run_agent
from coder_agent.cli import app
from coder_agent.config import settings
from coder_agent.evals.runner import label_for_model
from coder_agent.k2think import AsyncSanitizingTransport, SanitizingTransport
from coder_agent.llm import (
    K2_KEY_ENV,
    ChatModelRetry,
    get_llm,
    mute_tracing_without_a_key,
    provider_of,
    resolve,
)
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


def test_unbuildable_fallback_is_dropped_instead_of_failing_the_run(providers, monkeypatch, caplog):
    """A local run on a machine with no cloud keys must still start.

    `init_chat_model` validates credentials when the model is constructed, not when it is called,
    so an unusable fallback used to raise before the primary was ever asked anything.
    """
    primary = providers[0]

    def build(name, **kw):
        if name == "google_genai:fallback":
            raise ValueError("API key required for Gemini Developer API")
        return primary

    monkeypatch.setattr(llm_mod, "init_chat_model", build)
    with caplog.at_level(logging.WARNING, logger="coder_agent.llm"):
        llm = get_llm()
    assert isinstance(llm, ChatModelRetry)  # retried, but with nothing behind it
    assert llm.invoke([HumanMessage("hi")]).content == "from primary"
    assert "running without a fallback" in caplog.text


def test_without_fallback_the_retry_wrapper_is_returned(providers, monkeypatch):
    monkeypatch.setattr(settings, "fallback_model", None)
    assert isinstance(get_llm(), ChatModelRetry)
    monkeypatch.setattr(settings, "llm_attempts", 1)
    assert get_llm() is providers[0]  # one attempt means no wrapper at all


# --- tracing ---------------------------------------------------------------------------------


@pytest.mark.parametrize("key_name", ["LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"])
def test_tracing_stays_on_when_a_key_is_configured(monkeypatch, key_name):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    for name in ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(key_name, "ls-something")
    assert mute_tracing_without_a_key() is False
    assert os.environ["LANGSMITH_TRACING"] == "true"


def test_tracing_is_muted_when_the_key_is_empty(monkeypatch):
    """The copied `.env.example` state: tracing requested, key not filled in yet."""
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "   ")
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    assert mute_tracing_without_a_key() is True
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_tracing_already_off_is_left_alone(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    assert mute_tracing_without_a_key() is False


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
    assert resolve("ollama:qwen2.5-coder:7b") == (
        "ollama:qwen2.5-coder:7b", {"base_url": "http://box:11434", "num_ctx": 4096}
    )
    assert resolve("groq:openai/gpt-oss-120b") == (
        "groq:openai/gpt-oss-120b", {"max_retries": settings.llm_sdk_retries}
    )


# --- K2 Think: an OpenAI-compatible endpoint behind a provider alias ------------------------


def test_k2think_resolves_to_the_openai_integration_at_the_k2_endpoint(monkeypatch):
    monkeypatch.setenv(K2_KEY_ENV, " k2-secret ")
    monkeypatch.setattr(settings, "k2_reasoning_effort", "low")

    name, kwargs = resolve("k2think:MBZUAI-IFM/K2-Think-v2")

    assert name == "openai:MBZUAI-IFM/K2-Think-v2"  # the model name after the alias, untouched
    clients = {k: kwargs.pop(k) for k in ("http_client", "http_async_client")}
    assert kwargs == {
        "base_url": settings.k2_base_url,
        "api_key": "k2-secret",  # stripped: a trailing space in .env must not become a 401
        "reasoning_effort": "low",
        "max_retries": settings.llm_sdk_retries,
    }
    # Both of the openai client's paths go through the firewall rewrite (see k2think.py).
    assert isinstance(clients["http_client"]._transport, SanitizingTransport)
    assert isinstance(clients["http_async_client"]._transport, AsyncSanitizingTransport)


def test_k2think_without_a_key_says_which_variable_to_set(monkeypatch):
    monkeypatch.delenv(K2_KEY_ENV, raising=False)
    with pytest.raises(ValueError, match=K2_KEY_ENV):
        resolve("k2think:MBZUAI-IFM/K2-Think-v2")


def test_k2think_as_an_unconfigured_fallback_is_dropped_like_any_other(providers, monkeypatch, caplog):
    monkeypatch.delenv(K2_KEY_ENV, raising=False)
    monkeypatch.setattr(settings, "fallback_model", "k2think:MBZUAI-IFM/K2-Think-v2")
    with caplog.at_level(logging.WARNING, logger="coder_agent.llm"):
        llm = get_llm()
    assert isinstance(llm, ChatModelRetry)
    assert "running without a fallback" in caplog.text


def test_k2think_model_builds_offline_with_the_key_and_the_effort_in_place(monkeypatch):
    """Constructing the client is offline; only `invoke` talks to the endpoint."""
    pytest.importorskip("langchain_openai")
    monkeypatch.setenv(K2_KEY_ENV, "k2-secret")
    monkeypatch.setattr(settings, "model", "k2think:MBZUAI-IFM/K2-Think-v2")
    monkeypatch.setattr(settings, "fallback_model", None)
    llm = get_llm()
    assert isinstance(llm, ChatModelRetry)
    assert llm.bound.model_name == "MBZUAI-IFM/K2-Think-v2"
    assert str(llm.bound.openai_api_base).rstrip("/") == settings.k2_base_url.rstrip("/")
    assert llm.bound.reasoning_effort == settings.k2_reasoning_effort
    assert isinstance(llm.bound.http_client._transport, SanitizingTransport)  # it reached the model
    import openai

    assert openai.APIError in llm_mod.TRANSIENT_ERRORS


def test_context_budget_is_capped_by_the_provider_window(monkeypatch):
    """K2 rejects prompt plus completion over 64k; Groq's window is twice the budget."""
    monkeypatch.setattr(settings, "context_budget_tokens", 60_000)
    monkeypatch.setattr(settings, "context_windows", {"k2think": 65_536})
    monkeypatch.setattr(settings, "context_reserve_tokens", 24_000)
    assert settings.context_budget("groq:openai/gpt-oss-120b") == 60_000
    assert settings.context_budget("k2think:MBZUAI-IFM/K2-Think-v2") == 65_536 - 24_000
    monkeypatch.setattr(settings, "model", "k2think:MBZUAI-IFM/K2-Think-v2")
    assert settings.context_budget() == 65_536 - 24_000  # the primary by default
    monkeypatch.setattr(settings, "context_budget_tokens", 12_000)
    assert settings.context_budget() == 12_000  # an explicit lower budget still wins


def test_k2think_results_label_drops_the_alias_and_the_slash():
    assert label_for_model("k2think:MBZUAI-IFM/K2-Think-v2") == "MBZUAI-IFM-K2-Think-v2"


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
