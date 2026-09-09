"""LLM factory.

`init_chat_model` is LangChain's provider-agnostic constructor: the string "groq:openai/gpt-oss-120b"
selects both the integration package and the model. Swapping providers is then a config change,
not a code change.

`with_fallbacks` wraps the primary model so that if it raises (typically a 429 rate limit on a free
tier), the same call is retried transparently on the fallback model.
"""

from __future__ import annotations

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from coder_agent.config import settings

load_dotenv()


def get_llm(temperature: float = 0.0) -> BaseChatModel:
    """Return the configured chat model, with an automatic fallback if one is configured."""
    primary = init_chat_model(settings.model, temperature=temperature)
    if not settings.fallback_model:
        return primary
    fallback = init_chat_model(settings.fallback_model, temperature=temperature)
    return primary.with_fallbacks([fallback])
