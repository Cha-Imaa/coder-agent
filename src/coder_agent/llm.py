"""LLM factory: primary model, retried, with a fallback provider behind it.

`init_chat_model` is LangChain's provider-agnostic constructor: the string
"groq:openai/gpt-oss-120b" selects both the integration package and the model. Swapping providers
is then a config change, not a code change: `ollama:qwen2.5-coder:7b` runs the same graph against
a model served on this machine, with no key and no quota, and `k2think:MBZUAI-IFM/K2-Think-v2`
against a hosted reasoning model that speaks the OpenAI dialect (see `resolve` for the one place
the providers differ).

Three layers stand between a node's `llm.invoke(...)` and the network, innermost first:

1. The provider SDK's own retries (`max_retries`), which honour `Retry-After` on 429s and cover
   connection errors and 5xx. Groq defaults to 2, Gemini to 6; both are pinned to
   `settings.llm_sdk_retries` so a dead provider cannot stall a run for minutes.
2. `ChatModelRetry`, a LangChain-level retry with exponential jitter. It exists for the errors
   the SDK rightly does not retry but that are transient for us: Groq's 400 `tool_use_failed`,
   raised when the model emits malformed JSON for tool arguments, is stochastic and usually
   succeeds on the next attempt.
3. `with_fallbacks`: when the primary has exhausted its attempts, the same call goes to the
   fallback model (a different provider, so a quota exhausted on one does not block the other).
   A fallback whose provider is not configured on this machine is dropped with a warning rather
   than raised: `--model ollama:<tag>` has to work with no cloud keys at all.

Which model actually answered is visible in each message's `response_metadata` and lands in the
ledger through `usage_from_message`; failed attempts are counted by `telemetry.ProviderEvents`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable
from langchain_core.runnables.retry import RunnableRetry

from coder_agent.config import provider_of, settings
from coder_agent.k2think import http_clients

load_dotenv()

# Where the K2 Think key is read from. Not a `CODER_` setting: like GROQ_API_KEY and
# GOOGLE_API_KEY it is a credential the provider client needs, not a knob the agent has.
K2_KEY_ENV = "K2_API_KEY"

__all__ = ["K2_KEY_ENV", "ChatModelRetry", "get_llm", "provider_of", "resolve"]

logger = logging.getLogger(__name__)

# Either spelling of the key is accepted by the langsmith SDK, so either one counts as "configured".
_TRACING_KEYS = ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")
_TRUTHY = {"1", "true", "yes", "on"}


def mute_tracing_without_a_key() -> bool:
    """Switch LangSmith tracing off when no key is set. True if it was switched off.

    `.env.example` ships `LANGSMITH_TRACING=true` so that tracing starts working the moment a key
    is pasted in. Until then the copied file has an empty key, and LangChain still tries to ship
    every run, printing a 401 traceback per batch: on a first run that is most of what scrolls
    past. Tracing without a key is not a degraded mode, it is only noise, so turn it off and say
    so once.
    """
    if os.environ.get("LANGSMITH_TRACING", "").strip().lower() not in _TRUTHY:
        return False
    if any(os.environ.get(name, "").strip() for name in _TRACING_KEYS):
        return False
    os.environ["LANGSMITH_TRACING"] = "false"
    logger.warning(
        "LANGSMITH_TRACING is on but no LANGSMITH_API_KEY is set; tracing disabled for this run."
    )
    return True


# Runs at import, before anything builds a tracer: every entry point imports this module.
mute_tracing_without_a_key()


def _provider_errors() -> tuple[type[BaseException], ...]:
    """Exception base classes worth a retry, for whichever provider packages are installed."""
    types: list[type[BaseException]] = []
    try:
        import groq

        types.append(groq.APIError)  # root of every groq error, connection errors included
    except ImportError:  # pragma: no cover - depends on the installed extras
        pass
    try:
        from google.api_core import exceptions as google_exceptions

        types.append(google_exceptions.GoogleAPICallError)
    except ImportError:  # pragma: no cover
        pass
    try:
        import openai

        types.append(openai.APIError)  # K2 Think is reached through the openai client
    except ImportError:  # pragma: no cover
        pass
    try:
        import ollama

        # The daemon answered with an error (model still loading, out of memory for the context
        # size): worth one more try. `ollama.RequestError` (nothing listening) is left out, since
        # retrying a daemon that is not running only delays the fallback.
        types.append(ollama.ResponseError)
    except ImportError:  # pragma: no cover
        pass
    return tuple(types)


# Tests swap this for `(RuntimeError,)` to drive the retry path with a fake model.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = _provider_errors()


class ChatModelRetry(RunnableRetry):
    """`RunnableRetry` that still behaves like a chat model.

    `Runnable.with_retry()` returns a wrapper that forwards `invoke` but not chat-model methods
    such as `bind_tools`, so the graph's `act` node could not bind its tools to it. This subclass
    forwards any attribute to the wrapped model and, when the result is another runnable (as
    `bind_tools` returns), wraps that in the same retry policy. The annotated return type is
    what `RunnableWithFallbacks.__getattr__` checks before applying the same call to the
    fallbacks, so tools end up bound on both sides.
    """

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.bound, name)
        if not callable(attr):
            return attr

        def proxied(*args: Any, **kwargs: Any) -> Runnable:
            out = attr(*args, **kwargs)
            return self.model_copy(update={"bound": out}) if isinstance(out, Runnable) else out

        return proxied


def resolve(model: str) -> tuple[str, dict[str, Any]]:
    """The `init_chat_model` name and constructor arguments for a "provider:model" string.

    This is the one place the providers differ. Groq and Gemini are what `init_chat_model`
    already knows, plus `max_retries` for the SDK's own retry loop. Ollama's client has no such
    knob (a local daemon does not rate-limit) and instead needs the context size and the
    daemon's address, which are per-call options in its API rather than model settings. K2 Think
    has no LangChain integration of its own and needs none: its endpoint is OpenAI-compatible,
    so the `openai` integration is pointed at a different base URL with the K2 key, and the
    model name after the alias goes through untouched. `reasoning_effort` is an OpenAI-dialect
    parameter the endpoint honours, so it rides along the same way. The one thing this endpoint
    needs that the others do not is the transport in `k2think.py` under the client, which
    rewrites the request body so the endpoint's edge firewall does not refuse it.
    """
    provider = provider_of(model)
    if provider == "ollama":
        return model, {"base_url": settings.ollama_base_url, "num_ctx": settings.ollama_num_ctx}
    if provider == "k2think":
        key = os.environ.get(K2_KEY_ENV, "").strip()
        if not key:
            raise ValueError(f"{K2_KEY_ENV} is not set; {model} needs it (see .env.example).")
        return f"openai:{model.split(':', 1)[1]}", {
            "base_url": settings.k2_base_url,
            "api_key": key,
            "reasoning_effort": settings.k2_reasoning_effort,
            "max_retries": settings.llm_sdk_retries,
            **http_clients(),
        }
    return model, {"max_retries": settings.llm_sdk_retries}


def _build(model: str, temperature: float) -> Runnable:
    name, kwargs = resolve(model)
    return init_chat_model(name, temperature=temperature, **kwargs)


def get_llm(temperature: float = 0.0) -> Runnable:
    """The configured chat model, retried on transient errors, with the fallback behind it."""
    primary: Runnable = _build(settings.model, temperature)
    if TRANSIENT_ERRORS and settings.llm_attempts > 1:
        primary = ChatModelRetry(
            bound=primary,
            retry_exception_types=TRANSIENT_ERRORS,
            max_attempt_number=settings.llm_attempts,
            wait_exponential_jitter=True,
            exponential_jitter_params={
                "initial": settings.llm_retry_initial_seconds, "max": 8.0, "exp_base": 2.0, "jitter": 1.0
            },
        )
    if not settings.fallback_model:
        return primary
    try:
        fallback = _build(settings.fallback_model, temperature)
    except Exception as exc:
        # Caught broadly on purpose: "this provider is not configured here" has no common base
        # class (pydantic raises ValidationError, groq raises GroqError, a missing extra raises
        # ImportError, a missing K2 key raises ValueError), and any of them mean the same thing
        # here. Only the *name* is logged,
        # because the provider's own message is a multi-line dump that would take over a
        # terminal UI laid out by hand; the full traceback goes to the debug record.
        logger.warning(
            "Fallback model %s is not configured here (%s); running without a fallback.",
            settings.fallback_model, type(exc).__name__,
        )
        logger.debug("Fallback model %s failed to build", settings.fallback_model, exc_info=exc)
        return primary
    return primary.with_fallbacks([fallback])
