"""LLM factory: primary model, retried, with a fallback provider behind it.

`init_chat_model` is LangChain's provider-agnostic constructor: the string
"groq:openai/gpt-oss-120b" selects both the integration package and the model. Swapping providers
is then a config change, not a code change.

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

Which model actually answered is visible in each message's `response_metadata` and lands in the
ledger through `usage_from_message`; failed attempts are counted by `telemetry.ProviderEvents`.
"""

from __future__ import annotations

from typing import Any

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable
from langchain_core.runnables.retry import RunnableRetry

from coder_agent.config import settings

load_dotenv()


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


def get_llm(temperature: float = 0.0) -> Runnable:
    """The configured chat model, retried on transient errors, with the fallback behind it."""
    primary: Runnable = init_chat_model(
        settings.model, temperature=temperature, max_retries=settings.llm_sdk_retries
    )
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
    fallback = init_chat_model(
        settings.fallback_model, temperature=temperature, max_retries=settings.llm_sdk_retries
    )
    return primary.with_fallbacks([fallback])
